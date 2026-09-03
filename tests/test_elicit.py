"""PolicyConsent 单测（E1）：safety policy 变更的 elicitation 交互语义。

独立真值：手写字面量 + safety.policy.parse_policy 交叉验证规则文本；
elicit adapter 以脚本化 fake 注入（ElicitFn 契约），不 mock 自有模块。
"""

import asyncio
from collections.abc import Mapping

import pytest

from common.elicit import (
    DenialOffer,
    ElicitOutcome,
    GrantChoiceConfirm,
    PolicyChangeConfirm,
    PolicyConsent,
    denial_message,
    fallback_hint,
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
ACCEPT_API = ElicitOutcome(action="accept", choice="api")
ACCEPT_PRODUCT = ElicitOutcome(action="accept", choice="product")
CHOICE_NONE = ElicitOutcome(action="accept", choice="none")

OFFER = DenialOffer(subject="ECS:ListServers",
                    rule="ECS:ListServers=allow",
                    reason="safety policy 拒绝执行 ECS:ListServers")
DENIAL = {"ok": False, "reason": "safety policy 拒绝执行 ECS:ListServers"}

COARSE_OFFER = DenialOffer(subject="VPC:CreateVpc",
                           rule="VPC:CreateVpc=allow",
                           reason="safety policy 拒绝执行 VPC:CreateVpc",
                           coarse_rule="VPC:*=allow")
DENIAL_VPC = {"ok": False, "reason": "safety policy 拒绝执行 VPC:CreateVpc"}


def run(coro):
    return asyncio.run(coro)


def make_grant(fail: bool = False):
    """记录型 grant 替身：模拟 manage_policy("add", line, scope) 的返回形状。"""
    calls: list[tuple[str, str | None]] = []

    def grant(line: str, scope: str | None = None) -> dict:
        calls.append((line, scope))
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


# ---------- 产品级规则文本（coarse 授予选项用，parse_policy 交叉验证） ----------

def test_grant_rule_wildcard_is_product_level():
    """grant_rule(product, "*") 构造产品级 allow-all 规则，parse_policy 正确回环。"""
    text = policy.grant_rule("VPC", "*")
    assert text == "VPC:*=allow"
    rule = policy.parse_policy([text])[0]
    assert rule.kind == "product"
    assert rule.product == "VPC"
    assert rule.api_pattern == "*"
    assert rule.allow is True
    assert rule.connect_only is False


def test_grant_server_rule_wildcard_is_call_level_all_tools():
    """server:X:*=allow 为调用级全工具规则（connect_only=False，能匹配 tool 调用）。"""
    text = policy.grant_server_rule("@huaweicloud/ecs", "*")
    assert text == "server:@huaweicloud/ecs:*=allow"
    rule = policy.parse_policy([text])[0]
    assert rule.kind == "server"
    assert rule.product == "@huaweicloud/ecs"
    assert rule.api_pattern == "*"
    assert rule.connect_only is False
    assert rule.allow is True
    # 与 connect 级规则（server:X=allow）天然区分：connect_only
    connect_rule = policy.parse_policy(["server:@huaweicloud/ecs=allow"])[0]
    assert connect_rule.connect_only is True


def test_product_wildcard_rule_covers_same_product_apis():
    """产品级规则按行序放行同产品全部 API，不越界到其它产品。"""
    rules = policy.parse_policy(["VPC:*=allow"])
    assert policy.evaluate(rules, "VPC", "CreateVpc") is True
    assert policy.evaluate(rules, "VPC", "ListVpcs") is True
    assert policy.evaluate(rules, "ECS", "ListServersDetails") is False


def test_server_wildcard_rule_covers_all_tools_not_connect():
    """server:X:*=allow 放行该 server 全部工具调用，但不放行 connect 检查。"""
    rules = policy.parse_policy(["server:@huaweicloud/ecs:*=allow"])
    assert policy.evaluate_server(rules, "@huaweicloud/ecs", "list_servers") is True
    assert policy.evaluate_server(rules, "@huaweicloud/ecs", "other_tool") is True
    assert policy.evaluate_server(rules, "@huaweicloud/ecs") is False  # connect 不匹配


# ---------- GrantChoiceConfirm 表单 schema（拒绝提议三选一） ----------

def test_grant_choice_confirm_schema_is_primitive_enum():
    """三选一表单：单 primitive 枚举字段（MCP spec 兼容），无嵌套结构。"""
    schema = GrantChoiceConfirm.model_json_schema()
    props = schema["properties"]
    assert set(props) == {"choice"}
    choice = props["choice"]
    assert choice.get("enum") == ["api", "product", "none"]
    assert choice.get("type") == "string"
    assert schema.get("required") == ["choice"]


@pytest.mark.parametrize("picked", ["api", "product", "none"])
def test_grant_choice_confirm_parses_each_value(picked):
    form = GrantChoiceConfirm(choice=picked)  # type: ignore[arg-type]
    assert form.choice == picked


def test_grant_choice_confirm_rejects_unknown_value():
    with pytest.raises(ValueError):
        GrantChoiceConfirm(choice="all")  # type: ignore[arg-type]


# ---------- PolicyConsent.offer_grant ----------

def assert_hint_enhanced(out: Mapping, denial: dict, offer: DenialOffer) -> None:
    """off/不支持路径契约：拒绝保持、reason=原 reason + 兜底指引、无 granted_rule。"""
    hint = fallback_hint(offer)
    assert out["ok"] is False
    assert "granted_rule" not in out
    assert out["reason"].startswith(denial["reason"])
    assert out["reason"].endswith(hint)
    assert "manage_policy" in out["reason"] and "question" in out["reason"]


def test_fallback_hint_coarse_lists_three_options():
    """有 coarse_rule：指引并列三选项与各自 scope 语义，点名 manage_policy。"""
    hint = fallback_hint(COARSE_OFFER)
    assert "VPC:CreateVpc=allow" in hint and "VPC:*=allow" in hint
    assert "api" in hint and "product" in hint and "none" in hint
    assert "一次性" in hint and "会话" in hint
    assert "manage_policy" in hint
    assert "question" in hint          # 通用问询表述（对话/交互式问询）
    assert "elicitation" not in hint   # prompt 约定不走协议弹窗


def test_fallback_hint_minimal_lists_single_option():
    """无 coarse_rule（connect 场景）：仅最小规则单选项。"""
    hint = fallback_hint(OFFER)
    assert "ECS:ListServers=allow" in hint
    assert "manage_policy" in hint and "question" in hint
    assert "product" not in hint and "none" not in hint


def test_offer_grant_off_never_elicts():
    elicit = make_elicit(ACCEPT)
    consent = PolicyConsent("off", elicit, make_grant())
    out = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert elicit.calls == []  # type: ignore[attr-defined]
    assert_hint_enhanced(out, DENIAL, OFFER)


def test_offer_grant_accept_grants_and_augments():
    elicit = make_elicit(ACCEPT)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    out = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert grant.calls == [("ECS:ListServers=allow", "once")]  # type: ignore[attr-defined]
    assert out["ok"] is False
    assert out["granted_rule"] == "ECS:ListServers=allow"
    assert "请重新调用" in out["reason"]
    assert out["reason"].startswith(DENIAL["reason"])
    assert "ECS:ListServers=allow" in elicit.calls[0][0]  # type: ignore[attr-defined]


# ---------- offer_grant 三选一（coarse 产品级选项） ----------

def test_offer_grant_coarse_api_choice_grants_minimal_once():
    """choice=api：授予最小规则，scope 取 minimal_scope（默认 once）。"""
    elicit = make_elicit(ACCEPT_API)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    out = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert grant.calls == [("VPC:CreateVpc=allow", "once")]  # type: ignore[attr-defined]
    assert out["granted_rule"] == "VPC:CreateVpc=allow"
    assert "请重新调用" in out["reason"]
    assert out["reason"].startswith(DENIAL_VPC["reason"])


def test_offer_grant_coarse_product_choice_grants_session():
    """choice=product：授予产品级规则（VPC:*=allow），scope 固定 session。"""
    elicit = make_elicit(ACCEPT_PRODUCT)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    out = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert grant.calls == [("VPC:*=allow", "session")]  # type: ignore[attr-defined]
    assert out["granted_rule"] == "VPC:*=allow"
    assert "请重新调用" in out["reason"]
    assert "会话" in out["reason"]
    assert out["reason"].startswith(DENIAL_VPC["reason"])


def test_offer_grant_coarse_none_decline_cancel_refuse_keep_denial():
    for outcome in (CHOICE_NONE, DECLINE, CANCEL):
        elicit = make_elicit(outcome)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        out = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
        assert out == DENIAL_VPC
        assert grant.calls == []  # type: ignore[attr-defined]


def test_offer_grant_coarse_accept_without_choice_defensive():
    """accept 但 choice 缺失/未知：防御性保持 denial，不授予。"""
    for outcome in (ElicitOutcome(action="accept"),
                    ElicitOutcome(action="accept", choice="all")):
        elicit = make_elicit(outcome)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        out = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
        assert out == DENIAL_VPC
        assert grant.calls == []  # type: ignore[attr-defined]


def test_offer_grant_coarse_product_choice_grant_failure_reports():
    elicit = make_elicit(ACCEPT_PRODUCT)
    grant = make_grant(fail=True)
    consent = PolicyConsent("auto", elicit, grant)
    out = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert "granted_rule" not in out
    assert "规则已存在" in out["reason"]
    assert out["reason"].startswith(DENIAL_VPC["reason"])


def test_offer_grant_coarse_unsupported_auto_and_required_keep_denial():
    for mode in ("auto", "required"):
        elicit = make_elicit(UNSUPPORTED)
        grant = make_grant()
        consent = PolicyConsent(mode, elicit, grant)
        out = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
        assert grant.calls == []  # type: ignore[attr-defined]
        assert_hint_enhanced(out, DENIAL_VPC, COARSE_OFFER)


def test_offer_grant_coarse_off_never_elicts():
    elicit = make_elicit(ACCEPT_PRODUCT)
    consent = PolicyConsent("off", elicit, make_grant())
    out = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert elicit.calls == []  # type: ignore[attr-defined]
    assert_hint_enhanced(out, DENIAL_VPC, COARSE_OFFER)


def test_offer_grant_minimal_scope_session_wiring():
    """minimal_scope 注入（connect 场景）：最小授予按 wiring 走 session。"""
    elicit = make_elicit(ACCEPT)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant, minimal_scope="session")
    out = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert grant.calls == [("ECS:ListServers=allow", "session")]  # type: ignore[attr-defined]
    assert out["granted_rule"] == "ECS:ListServers=allow"


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
        assert grant.calls == []  # type: ignore[attr-defined]
        assert_hint_enhanced(out, DENIAL, OFFER)


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


def test_denial_message_minimal_only_when_no_coarse():
    """无 coarse_rule：文案不出现产品级选项，保留一次性口径。"""
    msg = denial_message(OFFER)
    assert "一次性" in msg and "用后即焚" in msg
    assert "product" not in msg.split("- api")[0]  # 选项清单不前置
    assert "VPC:*=allow" not in msg


def test_denial_message_presents_three_options_when_coarse():
    """有 coarse_rule：文案并列 api/product/none 三选项并声明各自 scope 语义。"""
    msg = denial_message(COARSE_OFFER)
    assert "VPC:CreateVpc=allow" in msg       # 最小规则
    assert "VPC:*=allow" in msg               # 产品级规则
    assert "api" in msg and "product" in msg  # 选项名可被表单 choice 对应
    assert "一次性" in msg and "用后即焚" in msg   # api 选项语义
    assert "会话" in msg and "重启" in msg         # product 选项语义
    assert "不授予" in msg or "none" in msg


def test_messages_state_once_scope_semantics():
    """文案契约：拒绝提议声明一次性语义（用后即焚、重启即失）；
    manage_policy 变更弹窗声明会话内语义；只有显式 permanent 才写文件。"""
    from common.elicit import change_message

    msg = denial_message(OFFER)
    assert "一次性" in msg and "用后即焚" in msg and "重启" in msg
    assert "策略文件" not in msg
    add_msg = change_message("add", "OBS:GetObject=allow")
    assert "会话" in add_msg
    assert "permanent" in add_msg      # 指引：需持久时显式传 scope
    assert "策略文件" in add_msg


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


def test_ctx_elicit_fn_normalizes_choice_and_keeps_confirm_independent():
    """choice 归一化：GrantChoiceConfirm 表单读 choice，confirm 不受影响（两 schema 共存）。"""
    from common.elicit import ctx_elicit_fn

    class AcceptedChoice:
        action = "accept"
        data = GrantChoiceConfirm(choice="product")

    out = run(ctx_elicit_fn(FakeCtx([AcceptedChoice()]))("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="accept", choice="product")
    assert out.confirm is None

    class AcceptedApiChoice:
        action = "accept"
        data = GrantChoiceConfirm(choice="api")

    out = run(ctx_elicit_fn(FakeCtx([AcceptedApiChoice()]))("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="accept", choice="api")


def test_ctx_elicit_fn_maps_failure_to_unsupported():
    from common.elicit import ctx_elicit_fn

    fn = ctx_elicit_fn(FakeCtx([RuntimeError("client rejected")]))
    assert run(fn("m", PolicyChangeConfirm)) is None
