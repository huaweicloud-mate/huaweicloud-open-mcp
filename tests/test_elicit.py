"""PolicyConsent 单测（E1）：safety policy 变更的 elicitation 交互语义。

独立真值：手写字面量 + safety.policy.parse_policy 交叉验证规则文本；
elicit adapter 以脚本化 fake 注入（ElicitFn 契约），不 mock 自有模块。
"""

import asyncio

from common.elicit import (
    DenialOffer,
    ElicitOutcome,
    PolicyChangeConfirm,
    PolicyConsent,
    denial_message,
    parse_elicit_mode,
)
from safety import policy

# ---------- 脚本化 elicit / grant 替身 ----------

def make_elicit(*outcomes: ElicitOutcome | None):
    """脚本化 ElicitFn：按序返回 outcomes，记录收到的 (message, schema)。"""
    calls: list[tuple[str, type]] = []

    async def elicit(message: str, schema: type) -> ElicitOutcome | None:
        calls.append((message, schema))
        return outcomes[len(calls) - 1] if len(calls) <= len(outcomes) else None

    elicit.calls = calls  # type: ignore[attr-defined]
    return elicit


ACCEPT = ElicitOutcome(action="accept", confirm=True)
REFUSE = ElicitOutcome(action="accept", confirm=False)
DECLINE = ElicitOutcome(action="decline")
CANCEL = ElicitOutcome(action="cancel")
UNSUPPORTED = None

OFFER = DenialOffer(subject="ECS:ListServers",
                    rule="ECS:ListServers=allow",
                    reason="safety policy 拒绝执行 ECS:ListServers")
DENIAL = {"ok": False, "reason": "safety policy 拒绝执行 ECS:ListServers"}


def run(coro):
    return asyncio.run(coro)


def make_grant(fail: bool = False):
    """记录型 grant 替身：模拟 manage_policy("add", line) 的返回形状。"""
    calls: list[str] = []

    def grant(line: str) -> dict:
        calls.append(line)
        if fail:
            return {"ok": False, "action": "add", "reason": "规则已存在"}
        return {"ok": True, "action": "add"}

    grant.calls = calls  # type: ignore[attr-defined]
    return grant


# ---------- parse_elicit_mode ----------

def test_parse_elicit_mode_valid_and_default():
    assert parse_elicit_mode(None) == "off"
    assert parse_elicit_mode("") == "off"
    assert parse_elicit_mode("auto") == "auto"
    assert parse_elicit_mode("REQUIRED") == "required"
    assert parse_elicit_mode(" off ") == "off"


def test_parse_elicit_mode_invalid_falls_back_off():
    assert parse_elicit_mode("yes") == "off"
    assert parse_elicit_mode("1") == "off"


# ---------- 规则文本（safety/policy 纯函数，parse_policy 交叉验证） ----------

def test_grant_rule_roundtrip():
    text = policy.grant_rule("ECS", "ListServers")
    assert text == "ECS:ListServers=allow"
    rules = policy.parse_policy([text])
    assert len(rules) == 1
    rule = rules[0]
    assert rule.kind == "product"
    assert rule.product == "ECS"
    assert rule.api_pattern == "ListServers"
    assert rule.allow is True
    assert rule.connect_only is False


def test_grant_server_rule_roundtrip():
    connect = policy.grant_server_rule("@huaweicloud/ecs")
    assert connect == "server:@huaweicloud/ecs=allow"
    rule = policy.parse_policy([connect])[0]
    assert rule.kind == "server"
    assert rule.product == "@huaweicloud/ecs"
    assert rule.connect_only is True

    call = policy.grant_server_rule("@huaweicloud/ecs", "ListInstances")
    assert call == "server:@huaweicloud/ecs:ListInstances=allow"
    rule = policy.parse_policy([call])[0]
    assert rule.kind == "server"
    assert rule.connect_only is False
    assert rule.api_pattern == "ListInstances"


# ---------- PolicyConsent.offer_grant ----------

def test_offer_grant_off_never_elicits():
    elicit = make_elicit(ACCEPT)
    consent = PolicyConsent("off", elicit, make_grant())
    out = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert out == DENIAL
    assert elicit.calls == []  # type: ignore[attr-defined]


def test_offer_grant_accept_grants_and_augments():
    elicit = make_elicit(ACCEPT)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    out = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert grant.calls == ["ECS:ListServers=allow"]  # type: ignore[attr-defined]
    assert out["ok"] is False
    assert out["granted_rule"] == "ECS:ListServers=allow"
    assert "请重新调用" in out["reason"]
    assert out["reason"].startswith(DENIAL["reason"])
    assert "ECS:ListServers=allow" in elicit.calls[0][0]  # type: ignore[attr-defined]


def test_offer_grant_decline_cancel_and_refuse_keep_denial():
    for outcome in (DECLINE, CANCEL, REFUSE):
        elicit = make_elicit(outcome)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        out = run(consent.offer_grant(OFFER, dict(DENIAL)))
        assert out == DENIAL
        assert grant.calls == []  # type: ignore[attr-defined]


def test_offer_grant_grant_failure_reports_but_keeps_denial():
    elicit = make_elicit(ACCEPT)
    grant = make_grant(fail=True)
    consent = PolicyConsent("auto", elicit, grant)
    out = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert "granted_rule" not in out
    assert "规则已存在" in out["reason"]
    assert out["reason"].startswith(DENIAL["reason"])


def test_offer_grant_unsupported_auto_and_required_keep_denial():
    for mode in ("auto", "required"):
        elicit = make_elicit(UNSUPPORTED)
        grant = make_grant()
        consent = PolicyConsent(mode, elicit, grant)
        out = run(consent.offer_grant(OFFER, dict(DENIAL)))
        assert out == DENIAL
        assert grant.calls == []  # type: ignore[attr-defined]


def test_offer_grant_ignores_non_denial_results():
    for result in ({"ok": True}, "not-a-dict", None, {}):
        elicit = make_elicit(ACCEPT)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        out = run(consent.offer_grant(OFFER, result))  # type: ignore[arg-type]
        assert out == result
        assert elicit.calls == []  # type: ignore[attr-defined]
        assert grant.calls == []  # type: ignore[attr-defined]


# ---------- PolicyConsent.gate_change ----------

def test_gate_change_off_proceeds_without_elicit():
    elicit = make_elicit(DECLINE)
    consent = PolicyConsent("off", elicit)
    assert run(consent.gate_change("add", "OBS:GetObject=allow")) is None
    assert elicit.calls == []  # type: ignore[attr-defined]


def test_gate_change_confirmed_proceeds():
    elicit = make_elicit(ACCEPT)
    consent = PolicyConsent("auto", elicit)
    assert run(consent.gate_change("add", "OBS:GetObject=allow")) is None
    assert "OBS:GetObject=allow" in elicit.calls[0][0]  # type: ignore[attr-defined]


def test_gate_change_refused_blocks():
    for outcome in (DECLINE, CANCEL, REFUSE):
        elicit = make_elicit(outcome)
        consent = PolicyConsent("auto", elicit)
        blocked = run(consent.gate_change("remove", "OBS:GetObject=allow"))
        assert blocked is not None
        assert "未确认" in blocked


def test_gate_change_unsupported():
    elicit = make_elicit(UNSUPPORTED)
    assert run(PolicyConsent("auto", elicit).gate_change("add", "X:Y=allow")) is None
    blocked = run(PolicyConsent("required", make_elicit(UNSUPPORTED))
                  .gate_change("add", "X:Y=allow"))
    assert blocked is not None
    assert "elicitation" in blocked


# ---------- 表单 schema 与消息 ----------

def test_policy_change_confirm_schema_is_primitive():
    schema = PolicyChangeConfirm.model_json_schema()
    props = schema["properties"]
    assert set(props) == {"confirm"}
    assert props["confirm"]["type"] == "boolean"
    assert schema.get("required") == ["confirm"]


def test_messages_mention_rule_and_action():
    assert "ECS:ListServers=allow" in denial_message(OFFER)
    assert "safety policy 拒绝执行" in denial_message(OFFER)


# ---------- ctx_elicit_fn adapter 归一化 ----------

class FakeCtx:
    """duck-typed MCP Context：elicit 按脚本返回/抛出。"""

    def __init__(self, script):
        self.script = script

    async def elicit(self, message, schema):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_ctx_elicit_fn_normalizes_accept_and_decline():
    from common.elicit import ctx_elicit_fn

    class Accepted:
        action = "accept"
        data = PolicyChangeConfirm(confirm=True)

    ctx = FakeCtx([Accepted()])
    fn = ctx_elicit_fn(ctx)
    out = run(fn("m", PolicyChangeConfirm))
    assert out == ElicitOutcome(action="accept", confirm=True)

    class Declined:
        action = "decline"

    out = run(ctx_elicit_fn(FakeCtx([Declined()]))("m", PolicyChangeConfirm))
    assert out == ElicitOutcome(action="decline")


def test_ctx_elicit_fn_maps_failure_to_unsupported():
    from common.elicit import ctx_elicit_fn

    fn = ctx_elicit_fn(FakeCtx([RuntimeError("client rejected")]))
    assert run(fn("m", PolicyChangeConfirm)) is None
