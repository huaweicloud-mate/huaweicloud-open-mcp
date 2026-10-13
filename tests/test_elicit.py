"""PolicyConsent 单测（E1）：safety policy 变更的 elicitation 交互语义。

独立真值：手写字面量 + safety.policy.parse_policy 交叉验证规则文本；
elicit adapter 以脚本化 fake 注入（ElicitFn 契约），不 mock 自有模块。
offer_grant 契约为 GrantOutcome（GrantRefused=未授予携带 denial 信封 /
GrantAccepted=已授予携带规则清单）；offer_grant_and_retry 为授予后
重放恰一次的包装层共用编排。DenialOffer 恒携带 coarse_rule 与
readonly_rules（必填）；问询表单恒为 GrantChoiceConfirm 五选一。
"""

import asyncio

import pytest

from common.elicit import (
    DenialOffer,
    ElicitOutcome,
    GrantAccepted,
    GrantChoiceConfirm,
    GrantRefused,
    PolicyConsent,
    confirmed_grant_and_retry,
    denial_message,
    fallback_hint,
    manage_policy_list_only,
    offer_grant_and_retry,
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


ACCEPT_API = ElicitOutcome(action="accept", choice="api")
ACCEPT_API_SESSION = ElicitOutcome(action="accept", choice="api_session")
ACCEPT_PRODUCT = ElicitOutcome(action="accept", choice="product")
ACCEPT_READONLY = ElicitOutcome(action="accept", choice="readonly")
CHOICE_NONE = ElicitOutcome(action="accept", choice="none")
DECLINE = ElicitOutcome(action="decline")
CANCEL = ElicitOutcome(action="cancel")
UNSUPPORTED = None

READONLY_RULES = ["VPC:*List*=allow", "VPC:*Show*=allow",
                  "VPC:*Get*=allow", "VPC:*Query*=allow"]

OFFER = DenialOffer(subject="ECS:ListServers",
                    rule="ECS:ListServers=allow",
                    reason="safety policy 拒绝执行 ECS:ListServers",
                    coarse_rule="ECS:*=allow",
                    readonly_rules=["ECS:*List*=allow", "ECS:*Show*=allow",
                                    "ECS:*Get*=allow", "ECS:*Query*=allow"])
DENIAL = {"ok": False, "reason": "safety policy 拒绝执行 ECS:ListServers"}

COARSE_OFFER = DenialOffer(subject="VPC:CreateVpc",
                           rule="VPC:CreateVpc=allow",
                           reason="safety policy 拒绝执行 VPC:CreateVpc",
                           coarse_rule="VPC:*=allow",
                           readonly_rules=READONLY_RULES)
DENIAL_VPC = {"ok": False, "reason": "safety policy 拒绝执行 VPC:CreateVpc"}

READONLY_OFFER = DenialOffer(subject="VPC:ListVpcs",
                             rule="VPC:ListVpcs=allow",
                             reason="safety policy 拒绝执行 VPC:ListVpcs",
                             coarse_rule="VPC:*=allow",
                             readonly_rules=READONLY_RULES)


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
    assert rule.product == "ECS"
    assert rule.api_pattern == "ListServers"
    assert rule.allow is True
    assert rule.once is False


# ---------- 产品级规则文本（coarse 授予选项用，parse_policy 交叉验证） ----------

def test_grant_rule_wildcard_is_product_level():
    """grant_rule(product, "*") 构造产品级 allow-all 规则，parse_policy 正确回环。"""
    text = policy.grant_rule("VPC", "*")
    assert text == "VPC:*=allow"
    rule = policy.parse_policy([text])[0]
    assert rule.product == "VPC"
    assert rule.api_pattern == "*"
    assert rule.allow is True
    assert rule.once is False


def test_product_wildcard_rule_covers_same_product_apis():
    """产品级规则按行序放行同产品全部 API，不越界到其它产品。"""
    rules = policy.parse_policy(["VPC:*=allow"])
    assert policy.evaluate(rules, "VPC", "CreateVpc") is True
    assert policy.evaluate(rules, "VPC", "ListVpcs") is True
    assert policy.evaluate(rules, "ECS", "ListServersDetails") is False


# ---------- GrantChoiceConfirm 表单 schema（拒绝提议五选一） ----------

def test_grant_choice_confirm_schema_is_primitive_enum():
    """五选一表单：单 primitive 枚举字段（MCP spec 兼容），无嵌套结构。"""
    schema = GrantChoiceConfirm.model_json_schema()
    props = schema["properties"]
    assert set(props) == {"choice"}
    choice = props["choice"]
    assert choice.get("enum") == ["api_session", "readonly", "product",
                                  "api", "none"]
    assert choice.get("type") == "string"
    assert schema.get("required") == ["choice"]


@pytest.mark.parametrize("picked", ["api", "api_session", "product",
                                    "readonly", "none"])
def test_grant_choice_confirm_parses_each_value(picked):
    form = GrantChoiceConfirm(choice=picked)  # type: ignore[arg-type]
    assert form.choice == picked


def test_grant_choice_confirm_rejects_unknown_value():
    with pytest.raises(ValueError):
        GrantChoiceConfirm(choice="all")  # type: ignore[arg-type]


# ---------- PolicyConsent.offer_grant（GrantOutcome 契约） ----------

def assert_refused(outcome, denial: dict, offer: DenialOffer, *,
                   hinted: bool = False, assertion_available: bool = False) -> None:
    """未授予契约：GrantRefused + envelope 保持 ok=False 且 reason 前缀为原拒绝；
    hinted=True 时 reason 以兜底指引收尾（assertion_available 按被测 consent
    mode 传入，off 档指引含 grant=<档位> 断言快路径，auto/required 降级明示
    参数被忽略），否则 reason 与原 denial 逐字一致（用户已表态不加指引）。"""
    assert isinstance(outcome, GrantRefused)
    out = outcome.envelope
    assert out["ok"] is False
    assert out["reason"].startswith(denial["reason"])
    if hinted:
        expected = fallback_hint(offer, assertion_available=assertion_available)
        assert out["reason"].endswith(expected)
        assert "policy" in out["reason"]        # 两分支统一指向操作员 policy 文件
    else:
        assert out["reason"] == denial["reason"]


def test_fallback_hint_coarse_lists_five_options():
    """off 档：指引并列五选项与各自语义（grant=<档位> 机制），统一指向操作员
    policy 文件（含 `_OPERATOR_FILE_NOTE` 永久档句，常量漂移/缺席即炸）。"""
    hint = fallback_hint(COARSE_OFFER, assertion_available=True)
    assert "VPC:CreateVpc=allow" in hint and "VPC:*=allow" in hint
    assert ("api_session" in hint and "readonly" in hint
            and "product" in hint and "api" in hint)
    assert "grant=" in hint and "policy 文件" in hint
    assert "文件规则为永久档" in hint and "跨重启生效" in hint  # 共享句
    assert "question" in hint          # 通用问询表述（对话/交互式问询）
    assert "elicitation" not in hint   # prompt 约定不走协议弹窗


def test_fallback_hint_degraded_states_params_ignored():
    """auto/required 降级（客户端不支持）：明示 confirm/grant 参数被忽略
    （重传不会改变结果，循环守卫），仅指向操作员 policy 文件并附三选项
    规则文本供直接补授（api_session/readonly/product），不指引参数断言为
    agent 行动项；永久档句在场（降级分支的共享句注入点）。"""
    hint = fallback_hint(COARSE_OFFER)                     # assertion_available 缺省 False
    assert "被忽略" in hint and "重传不会改变结果" in hint
    assert "grant=" not in hint and "confirm=true" not in hint
    assert ("api_session" in hint and "readonly" in hint
            and "product" in hint)                          # 三选项补授参考
    assert "api" not in hint.split("api_session")[0]        # 无一次性档断言指引
    assert "操作员" in hint and "policy 文件" in hint
    assert "文件规则为永久档" in hint                       # 共享句
    assert "VPC:CreateVpc=allow" in hint and "VPC:*=allow" in hint  # 操作员补授参考
    minimal = fallback_hint(OFFER)
    assert "被忽略" in minimal and "ECS:ListServers=allow" in minimal
    assert "文件规则为永久档" in minimal                    # 降级 minimal 路径


def test_offer_grant_off_never_elicts():
    elicit = make_elicit(ACCEPT_API)
    consent = PolicyConsent("off", elicit, make_grant())
    outcome = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert elicit.calls == []  # type: ignore[attr-defined]
    assert_refused(outcome, DENIAL, OFFER, hinted=True, assertion_available=True)


def test_offer_grant_accept_grants_and_reports_rules():
    elicit = make_elicit(ACCEPT_API)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    outcome = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert grant.calls == [("ECS:ListServers=allow", "once")]  # type: ignore[attr-defined]
    assert isinstance(outcome, GrantAccepted)
    assert outcome.rules == ("ECS:ListServers=allow",)
    assert "ECS:ListServers=allow" in elicit.calls[0][0]  # type: ignore[attr-defined]


# ---------- offer_grant 档位选择（api/api_session/product） ----------

def test_offer_grant_coarse_api_choice_grants_minimal_once():
    """choice=api：授予最小规则，scope 固定 once（api 档为一次性授权）。"""
    elicit = make_elicit(ACCEPT_API)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert grant.calls == [("VPC:CreateVpc=allow", "once")]  # type: ignore[attr-defined]
    assert isinstance(outcome, GrantAccepted)
    assert outcome.rules == ("VPC:CreateVpc=allow",)


def test_offer_grant_coarse_product_choice_grants_session():
    """choice=product：授予产品级规则（VPC:*=allow），scope 固定 session。"""
    elicit = make_elicit(ACCEPT_PRODUCT)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert grant.calls == [("VPC:*=allow", "session")]  # type: ignore[attr-defined]
    assert isinstance(outcome, GrantAccepted)
    assert outcome.rules == ("VPC:*=allow",)


def test_offer_grant_coarse_api_session_choice_grants_minimal_session():
    """choice=api_session：授予最小规则（单目标粒度不变），scope 固定 session
    （会话内持续放行该目标，重启即失）。"""
    elicit = make_elicit(ACCEPT_API_SESSION)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert grant.calls == [("VPC:CreateVpc=allow", "session")]  # type: ignore[attr-defined]
    assert isinstance(outcome, GrantAccepted)
    assert outcome.rules == ("VPC:CreateVpc=allow",)   # 规则仍是最小规则，非产品级


def test_offer_grant_coarse_none_decline_cancel_keep_denial():
    for outcome_in in (CHOICE_NONE, DECLINE, CANCEL):
        elicit = make_elicit(outcome_in)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
        assert_refused(outcome, DENIAL_VPC, COARSE_OFFER)
        assert grant.calls == []  # type: ignore[attr-defined]


def test_offer_grant_coarse_accept_without_choice_defensive():
    """accept 但 choice 缺失/未知：防御性保持 denial，不授予。"""
    for outcome_in in (ElicitOutcome(action="accept"),
                       ElicitOutcome(action="accept", choice="all")):
        elicit = make_elicit(outcome_in)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
        assert_refused(outcome, DENIAL_VPC, COARSE_OFFER)
        assert grant.calls == []  # type: ignore[attr-defined]


def test_offer_grant_coarse_product_choice_grant_failure_reports():
    elicit = make_elicit(ACCEPT_PRODUCT)
    grant = make_grant(fail=True)
    consent = PolicyConsent("auto", elicit, grant)
    outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert isinstance(outcome, GrantRefused)
    assert "规则已存在" in outcome.envelope["reason"]
    assert outcome.envelope["reason"].startswith(DENIAL_VPC["reason"])


def test_offer_grant_coarse_unsupported_auto_and_required_keep_denial():
    for mode in ("auto", "required"):
        elicit = make_elicit(UNSUPPORTED)
        grant = make_grant()
        consent = PolicyConsent(mode, elicit, grant)
        outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
        assert grant.calls == []  # type: ignore[attr-defined]
        assert_refused(outcome, DENIAL_VPC, COARSE_OFFER, hinted=True)


def test_offer_grant_coarse_off_never_elicts():
    elicit = make_elicit(ACCEPT_PRODUCT)
    consent = PolicyConsent("off", elicit, make_grant())
    outcome = run(consent.offer_grant(COARSE_OFFER, dict(DENIAL_VPC)))
    assert elicit.calls == []  # type: ignore[attr-defined]
    assert_refused(outcome, DENIAL_VPC, COARSE_OFFER, hinted=True,
                   assertion_available=True)


def test_offer_grant_none_decline_cancel_keep_denial():
    for outcome_in in (CHOICE_NONE, DECLINE, CANCEL):
        elicit = make_elicit(outcome_in)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        outcome = run(consent.offer_grant(OFFER, dict(DENIAL)))
        assert_refused(outcome, DENIAL, OFFER)
        assert grant.calls == []  # type: ignore[attr-defined]


def test_offer_grant_grant_failure_reports_but_keeps_denial():
    elicit = make_elicit(ACCEPT_API)
    grant = make_grant(fail=True)
    consent = PolicyConsent("auto", elicit, grant)
    outcome = run(consent.offer_grant(OFFER, dict(DENIAL)))
    assert isinstance(outcome, GrantRefused)
    assert "规则已存在" in outcome.envelope["reason"]
    assert outcome.envelope["reason"].startswith(DENIAL["reason"])


def test_offer_grant_unsupported_auto_and_required_keep_denial():
    for mode in ("auto", "required"):
        elicit = make_elicit(UNSUPPORTED)
        grant = make_grant()
        consent = PolicyConsent(mode, elicit, grant)
        outcome = run(consent.offer_grant(OFFER, dict(DENIAL)))
        assert grant.calls == []  # type: ignore[attr-defined]
        assert_refused(outcome, DENIAL, OFFER, hinted=True)


def test_offer_grant_ignores_non_denial_results():
    for result in ({"ok": True}, "not-a-dict", None, {}):
        elicit = make_elicit(ACCEPT_API)
        grant = make_grant()
        consent = PolicyConsent("auto", elicit, grant)
        outcome = run(consent.offer_grant(OFFER, result))  # type: ignore[arg-type]
        assert isinstance(outcome, GrantRefused)
        assert outcome.envelope == result
        assert elicit.calls == []  # type: ignore[attr-defined]
        assert grant.calls == []  # type: ignore[attr-defined]


# ---------- 表单 schema 与消息 ----------

def test_messages_mention_rule_and_action():
    assert "ECS:ListServers=allow" in denial_message(OFFER)
    assert "safety policy 拒绝执行" in denial_message(OFFER)


def test_denial_message_presents_five_options():
    """文案并列五选项并声明各自 scope 语义（readonly 恒在场，DenialOffer 必填）。"""
    msg = denial_message(COARSE_OFFER)
    assert "VPC:CreateVpc=allow" in msg       # 最小规则（api 与 api_session 同一规则）
    assert "VPC:*=allow" in msg               # 产品级规则
    assert ("- api：" in msg and "- api_session：" in msg
            and "- product：" in msg and "- readonly：" in msg
            and "- none：" in msg)  # 选项名可被表单 choice 对应
    assert "一次性" in msg and "用后即焚" in msg   # api 选项语义
    assert "会话" in msg and "重启" in msg         # api_session/product/readonly 选项语义
    assert "不授予" in msg or "none" in msg


def test_denial_message_coarse_orders_session_tier_first():
    """coarse 选项按推荐度排序：api_session/readonly 在 product/api 之前
    （首次问询即推荐会话档；api 一次性殿后，枚举顺序与文案顺序解耦）。"""
    msg = denial_message(READONLY_OFFER)   # 五选项全在场
    assert msg.index("- api_session：") < msg.index("- readonly：")
    assert msg.index("- readonly：") < msg.index("- product：")
    assert msg.index("- product：") < msg.index("- api：")
    assert "同一目标重复使用首选" in msg           # api_session 推荐语
    assert "浏览/多步任务首选" in msg              # readonly 推荐语
    assert "仅明确单次使用时选择" in msg           # api 档位限定语


def test_messages_state_once_scope_semantics():
    """文案契约：拒绝提议声明一次性语义（用后即焚、重启即失）；持久授权指向
    操作员 policy 文件（工具面无写路径）。"""
    msg = denial_message(OFFER)
    assert "一次性" in msg and "用后即焚" in msg and "重启" in msg
    assert "策略文件" not in msg
    assert "policy 文件" in msg          # 持久授权指向（product 选项文案）


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
        data = GrantChoiceConfirm(choice="api")

    ctx = FakeCtx([Accepted()])
    fn = ctx_elicit_fn(ctx)
    out = run(fn("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="accept", choice="api")

    class Declined:
        action = "decline"

    out = run(ctx_elicit_fn(FakeCtx([Declined()]))("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="decline")


def test_ctx_elicit_fn_normalizes_choice():
    """choice 归一化：accept 数据取 GrantChoiceConfirm 表单的 choice（五值枚举）。"""
    from common.elicit import ctx_elicit_fn

    class AcceptedChoice:
        action = "accept"
        data = GrantChoiceConfirm(choice="product")

    out = run(ctx_elicit_fn(FakeCtx([AcceptedChoice()]))("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="accept", choice="product")

    class AcceptedApiChoice:
        action = "accept"
        data = GrantChoiceConfirm(choice="api")

    out = run(ctx_elicit_fn(FakeCtx([AcceptedApiChoice()]))("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="accept", choice="api")

    class AcceptedApiSessionChoice:
        action = "accept"
        data = GrantChoiceConfirm(choice="api_session")

    out = run(ctx_elicit_fn(FakeCtx([AcceptedApiSessionChoice()]))("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="accept", choice="api_session")


def test_ctx_elicit_fn_maps_failure_to_unsupported():
    from common.elicit import ctx_elicit_fn

    fn = ctx_elicit_fn(FakeCtx([RuntimeError("client rejected")]))
    assert run(fn("m", GrantChoiceConfirm)) is None


# ---------- S-B：readonly 产品级只读档（五选一） ----------

def test_readonly_grant_rules_text_and_parse_roundtrip():
    rules = policy.readonly_grant_rules("VPC")
    assert rules == READONLY_RULES
    for line in rules:
        parsed = policy.parse_policy([line, "*=deny"])
        assert parsed[0].product == "VPC"
        assert parsed[0].allow is True
    # 产品名插值正确
    assert policy.readonly_grant_rules("OBS") == [
        "OBS:*List*=allow", "OBS:*Show*=allow",
        "OBS:*Get*=allow", "OBS:*Query*=allow"]


def test_offer_grant_readonly_choice_grants_four_session_rules():
    elicit = make_elicit(ACCEPT_READONLY)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    outcome = run(consent.offer_grant(READONLY_OFFER, dict(DENIAL_VPC)))
    assert grant.calls == [(r, "session") for r in READONLY_RULES]  # type: ignore[attr-defined]
    assert isinstance(outcome, GrantAccepted)
    assert outcome.rules == tuple(READONLY_RULES)


def test_offer_grant_readonly_partial_failure_accepts_granted_subset():
    """部分授予（前 2 条成功）：任一成功即 GrantAccepted，rules 为已成功子集
    （重试可能命中已授予规则）、failed 为失败子集（重试信封回执源）；
    失败明细经 WARNING 留痕。"""
    calls: list[tuple[str, str | None]] = []

    def grant_fn(line: str, scope: str | None = None) -> dict:
        calls.append((line, scope))
        if len(calls) >= 3:   # 第 3 条起失败
            return {"ok": False, "action": "add", "reason": "boom"}
        return {"ok": True, "action": "add"}

    elicit = make_elicit(ACCEPT_READONLY)
    consent = PolicyConsent("auto", elicit, grant_fn)
    outcome = run(consent.offer_grant(READONLY_OFFER, dict(DENIAL_VPC)))
    assert isinstance(outcome, GrantAccepted)
    assert outcome.rules == tuple(READONLY_RULES[:2])
    assert outcome.failed == tuple(READONLY_RULES[2:])


def test_offer_grant_readonly_all_failed_keeps_denial():
    """全部失败：GrantRefused，envelope 附失败明细与操作员补授指引。"""

    def grant_fn(line: str, scope: str | None = None) -> dict:
        return {"ok": False, "action": "add", "reason": "boom"}

    elicit = make_elicit(ACCEPT_READONLY)
    consent = PolicyConsent("auto", elicit, grant_fn)
    outcome = run(consent.offer_grant(READONLY_OFFER, dict(DENIAL_VPC)))
    assert isinstance(outcome, GrantRefused)
    assert outcome.envelope["reason"].startswith(DENIAL_VPC["reason"])
    assert "boom" in outcome.envelope["reason"]
    assert "policy 文件" in outcome.envelope["reason"]


def test_denial_offer_rejects_empty_readonly_rules():
    """readonly_rules 空集/空白条目/裸字符串构造期拒绝，字段随即元组化——
    空态不可表示（frozen dataclass 无原地变更通道，实例可 hash）；
    denial_message / fallback_hint / _grant_readonly 三处 join 渲染结构性
    安全（str 是 Sequence[str]，逐字符元组化是静默数据损坏，显式拒绝）。"""
    with pytest.raises(ValueError, match="readonly_rules"):
        DenialOffer(subject="X:Y", rule="X:Y=allow", reason="denied",
                    coarse_rule="X:*=allow", readonly_rules=[])
    with pytest.raises(ValueError, match="裸字符串"):
        DenialOffer(subject="X:Y", rule="X:Y=allow", reason="denied",
                    coarse_rule="X:*=allow", readonly_rules="X:*List*=allow")
    with pytest.raises(ValueError, match="空白"):
        DenialOffer(subject="X:Y", rule="X:Y=allow", reason="denied",
                    coarse_rule="X:*=allow", readonly_rules=["", "  "])
    offer = DenialOffer(subject="X:Y", rule="X:Y=allow", reason="denied",
                        coarse_rule="X:*=allow",
                        readonly_rules=["X:*List*=allow"])
    assert isinstance(offer.readonly_rules, tuple)
    hash(offer)                             # 可 hash（tuple 字段；不抛即钉住）
    with pytest.raises(AttributeError):
        offer.readonly_rules = []          # frozen：重绑定不可达
    with pytest.raises(AttributeError):
        offer.readonly_rules.clear()       # 元组：无原地变更通道


def test_fallback_hint_readonly_lists_five_options():
    hint = fallback_hint(READONLY_OFFER, assertion_available=True)
    assert "api_session=最小规则" in hint
    assert "product=产品级规则" in hint
    assert "readonly=产品级只读" in hint
    assert "api=最小规则" in hint
    assert "*List*" in hint and "*Query*" in hint
    assert "grant=" in hint


def test_fallback_hint_coarse_orders_session_tier_first():
    """指引选项与弹窗同序：api_session/readonly 在 product/api 之前。"""
    hint = fallback_hint(READONLY_OFFER, assertion_available=True)
    assert hint.index("api_session=") < hint.index("readonly=")
    assert hint.index("readonly=") < hint.index("product=")
    assert hint.index("product=") < hint.index("/api=")

    plain = fallback_hint(COARSE_OFFER, assertion_available=True)
    assert plain.index("api_session=") < plain.index("readonly=")
    assert plain.index("readonly=") < plain.index("product=")
    assert plain.index("product=") < plain.index("/api=")


def test_denial_message_readonly_presents_five_options():
    msg = denial_message(READONLY_OFFER)
    assert "- readonly：授予产品级只读规则" in msg
    assert "- none：不授予" in msg


def test_grant_choice_confirm_parses_readonly():
    assert GrantChoiceConfirm(choice="readonly").choice == "readonly"


def test_ctx_elicit_fn_normalizes_readonly_choice():
    from common.elicit import ctx_elicit_fn

    class AcceptedReadonlyChoice:
        action = "accept"
        data = GrantChoiceConfirm(choice="readonly")

    out = run(ctx_elicit_fn(FakeCtx([AcceptedReadonlyChoice()]))("m", GrantChoiceConfirm))
    assert out == ElicitOutcome(action="accept", choice="readonly")


# ---------- offer_grant_and_retry（授予后重放恰一次） ----------

def test_offer_grant_and_retry_refused_returns_envelope_without_retry():
    """未授予（decline）：返回原 denial，重放 thunk 不调用。"""
    elicit = make_elicit(DECLINE)
    consent = PolicyConsent("auto", elicit, make_grant())
    calls: list[str] = []

    def retry() -> dict:
        calls.append("x")
        return {"ok": True}

    out = run(offer_grant_and_retry(consent, OFFER, dict(DENIAL), retry))
    assert out == DENIAL
    assert calls == []


def test_offer_grant_and_retry_accept_replays_once_sync_thunk():
    """授予成功：thunk 恰调用一次，其结果原样返回（openapi 同步 service 形态）。"""
    elicit = make_elicit(ACCEPT_API)
    grant = make_grant()
    consent = PolicyConsent("auto", elicit, grant)
    calls: list[int] = []

    def retry() -> dict:
        calls.append(len(calls))
        return {"ok": True, "status": 200, "body": {"mock": True}}

    out = run(offer_grant_and_retry(consent, OFFER, dict(DENIAL), retry))
    assert out == {"ok": True, "status": 200, "body": {"mock": True}}
    assert calls == [0]
    assert grant.calls == [("ECS:ListServers=allow", "once")]  # type: ignore[attr-defined]


def test_offer_grant_and_retry_accept_replays_once_async_thunk():
    """授予成功：异步 thunk 同样恰重放一次。"""
    elicit = make_elicit(ACCEPT_API)
    consent = PolicyConsent("auto", elicit, make_grant())
    calls: list[int] = []

    async def retry() -> dict:
        calls.append(len(calls))
        return {"ok": True, "server": "s"}

    out = run(offer_grant_and_retry(consent, OFFER, dict(DENIAL), retry))
    assert out == {"ok": True, "server": "s"}
    assert calls == [0]


def test_offer_grant_and_retry_denied_again_appends_granted_context():
    """重试仍被拒（readonly 误选写接口 / once 被并发消费）：reason 追加已授予
    规则上下文与再调用指引，不二次弹窗。"""
    elicit = make_elicit(ACCEPT_API)
    consent = PolicyConsent("auto", elicit, make_grant())

    def retry() -> dict:
        return {"ok": False, "reason": "safety policy 拒绝执行 ECS:ListServers"}

    out = run(offer_grant_and_retry(consent, OFFER, dict(DENIAL), retry))
    assert out["ok"] is False
    assert out["reason"].startswith("safety policy 拒绝执行 ECS:ListServers")
    assert "ECS:ListServers=allow" in out["reason"]
    assert "再次调用将重新进入确认授予流程" in out["reason"]
    assert len(elicit.calls) == 1  # type: ignore[attr-defined]


def test_offer_grant_and_retry_partial_readonly_receipt_on_success():
    """readonly 部分授予 + 重试成功：成功信封 reason 披露失败子集回执
    （wire 可选字段加性；本次已按已授予子集放行）。"""

    def grant_fn(line: str, scope: str | None = None) -> dict:
        if line in ("VPC:*Show*=allow", "VPC:*Query*=allow"):
            return {"ok": False, "action": "add", "reason": "boom"}
        return {"ok": True, "action": "add"}

    consent = PolicyConsent("auto", make_elicit(ACCEPT_READONLY), grant_fn)

    def retry() -> dict:
        return {"ok": True, "status": 200, "body": {"mock": True}}

    out = run(offer_grant_and_retry(consent, READONLY_OFFER, dict(DENIAL_VPC), retry))
    assert out["ok"] is True and out["body"] == {"mock": True}
    assert "VPC:*Show*=allow" in out["reason"] and "VPC:*Query*=allow" in out["reason"]
    assert "boom" not in out["reason"]                    # 回执不携带 grant 内部失败原因
    assert "policy 文件" in out["reason"]                 # 补授指引（操作员）


def test_offer_grant_and_retry_partial_readonly_denied_appends_failed_subset():
    """readonly 部分授予 + 重试仍被 policy 拒绝：上下文同时披露已授予与失败子集。"""

    def grant_fn(line: str, scope: str | None = None) -> dict:
        if line in ("VPC:*Show*=allow", "VPC:*Query*=allow"):
            return {"ok": False, "action": "add", "reason": "boom"}
        return {"ok": True, "action": "add"}

    consent = PolicyConsent("auto", make_elicit(ACCEPT_READONLY), grant_fn)

    def retry() -> dict:
        return {"ok": False, "reason": "safety policy 拒绝执行 VPC:CreateVpc"}

    out = run(offer_grant_and_retry(consent, READONLY_OFFER, dict(DENIAL_VPC), retry))
    assert out["reason"].startswith("safety policy 拒绝执行 VPC:CreateVpc")
    assert "VPC:*List*=allow" in out["reason"]            # 已授予上下文
    assert "VPC:*Show*=allow" in out["reason"]            # 失败子集回执
    assert "再次调用将重新进入确认授予流程" in out["reason"]


def test_offer_grant_and_retry_non_policy_failure_returns_untouched():
    """重试失败非 policy 拒绝（参数校验 / 上游执行错误）：结果原样返回，
    不注授予上下文，自纠面不被污染，once 为修正后的下一次调用保留。"""

    def validation_fail() -> dict:
        return {"ok": False,
                "reason": "缺少必填 query 参数 action（get_api 可查参数定义）"}

    def upstream_fail() -> dict:
        return {"ok": False, "status": 400, "reason": "参数错误",
                "error_code": "E400"}

    for thunk in (validation_fail, upstream_fail):
        consent = PolicyConsent("auto", make_elicit(ACCEPT_API), make_grant())
        out = run(offer_grant_and_retry(consent, OFFER, dict(DENIAL), thunk))
        assert out == thunk()


# ---------- off 模式 confirm 快路径（confirm_grant / confirmed_grant_and_retry） ----------

def test_confirm_grant_off_grants_minimal_once_without_asking():
    """off 模式断言式确认：不经问询直接授予最小规则（缺省 choice=api，once 档）。"""
    elicit = make_elicit(DECLINE)
    grant = make_grant()
    consent = PolicyConsent("off", elicit, grant)
    outcome = consent.confirm_grant(OFFER, dict(DENIAL))
    assert grant.calls == [("ECS:ListServers=allow", "once")]  # type: ignore[attr-defined]
    assert isinstance(outcome, GrantAccepted)
    assert outcome.rules == ("ECS:ListServers=allow",)
    assert elicit.calls == []  # type: ignore[attr-defined]   # 断言式确认不经问询


def test_confirm_grant_ignores_non_denial_results():
    """防御路径（非 denial 形状输入）：原样透传（与 offer_grant 同口径）。"""
    for result in ({"ok": True}, "not-a-dict", None, {}):
        consent = PolicyConsent("off", make_elicit(DECLINE), make_grant())
        outcome = consent.confirm_grant(OFFER, result)  # type: ignore[arg-type]
        assert isinstance(outcome, GrantRefused)
        assert outcome.envelope == result


def test_confirm_grant_rejects_non_off_mode():
    """auto/required 误路由：结构化拒绝不授予——断言式确认不得绕过协议确认门。"""
    for mode in ("auto", "required"):
        grant = make_grant()
        consent = PolicyConsent(mode, make_elicit(ACCEPT_API), grant)
        outcome = consent.confirm_grant(OFFER, dict(DENIAL))
        assert isinstance(outcome, GrantRefused)
        assert grant.calls == []  # type: ignore[attr-defined]
        assert "off" in outcome.envelope["reason"]
        assert outcome.envelope["reason"].startswith(DENIAL["reason"])


def test_confirm_grant_failure_reports():
    consent = PolicyConsent("off", make_elicit(DECLINE), make_grant(fail=True))
    outcome = consent.confirm_grant(OFFER, dict(DENIAL))
    assert isinstance(outcome, GrantRefused)
    assert "规则已存在" in outcome.envelope["reason"]
    assert outcome.envelope["reason"].startswith(DENIAL["reason"])


def test_confirm_grant_failure_does_not_readvertise_mechanism():
    """授予失败信封不附 fallback_hint——刚失败的机制（grant/confirm 重传）
    不可再兜售（strict_sessions 拒绝等场景重传即无变化循环）。"""
    consent = PolicyConsent("off", make_elicit(DECLINE), make_grant(fail=True))
    outcome = consent.confirm_grant(COARSE_OFFER, dict(DENIAL_VPC))
    assert isinstance(outcome, GrantRefused)
    reason = outcome.envelope["reason"]
    assert "自动授予" in reason and "失败" in reason
    assert "grant=" not in reason and "confirm=true" not in reason


def test_confirm_grant_choice_matrix_matches_elicitation():
    """断言式 choice 派发与问询表单同源共享：api_session→最小规则 session、
    product→coarse session、readonly→只读规则集逐条 session、缺省 api→once。"""
    grant = make_grant()
    consent = PolicyConsent("off", make_elicit(DECLINE), grant)

    out = consent.confirm_grant(COARSE_OFFER, dict(DENIAL_VPC), choice="api_session")
    assert isinstance(out, GrantAccepted)
    assert grant.calls == [("VPC:CreateVpc=allow", "session")]  # type: ignore[attr-defined]

    out = consent.confirm_grant(COARSE_OFFER, dict(DENIAL_VPC), choice="product")
    assert isinstance(out, GrantAccepted)
    assert grant.calls[-1] == ("VPC:*=allow", "session")  # type: ignore[attr-defined]

    readonly = consent.confirm_grant(READONLY_OFFER, dict(DENIAL_VPC), choice="readonly")
    assert isinstance(readonly, GrantAccepted)
    assert readonly.rules == tuple(READONLY_RULES)
    assert grant.calls[2:] == [(r, "session") for r in READONLY_RULES]  # type: ignore[attr-defined]

    api = consent.confirm_grant(OFFER, dict(DENIAL))          # 缺省 choice="api"
    assert isinstance(api, GrantAccepted)
    assert grant.calls[-1] == ("ECS:ListServers=allow", "once")  # type: ignore[attr-defined]


def test_confirmed_grant_and_retry_executes_inline_once():
    """断言式授予后重放恰一次，结果原样返回。"""
    consent = PolicyConsent("off", make_elicit(DECLINE), make_grant())
    calls: list[int] = []

    def retry() -> dict:
        calls.append(len(calls))
        return {"ok": True, "body": {"mock": True}}

    out = run(confirmed_grant_and_retry(consent, OFFER, dict(DENIAL), retry))
    assert out == {"ok": True, "body": {"mock": True}}
    assert calls == [0]


def test_confirmed_grant_and_retry_choice_passthrough():
    """choice 穿透：confirmed_grant_and_retry 按 choice 档位授予（api_session
    → session）并重放恰一次。"""
    grant = make_grant()
    consent = PolicyConsent("off", make_elicit(DECLINE), grant)
    calls: list[int] = []

    def retry() -> dict:
        calls.append(len(calls))
        return {"ok": True, "body": {"mock": True}}

    out = run(confirmed_grant_and_retry(consent, COARSE_OFFER, dict(DENIAL_VPC),
                                        retry, choice="api_session"))
    assert out == {"ok": True, "body": {"mock": True}}
    assert calls == [0]
    assert grant.calls == [("VPC:CreateVpc=allow", "session")]  # type: ignore[attr-defined]


def test_confirmed_grant_and_retry_denied_again_appends_context():
    """重试仍被 policy 拒绝：附已授予上下文（共享尾部与问询路径同口径）。"""
    consent = PolicyConsent("off", make_elicit(DECLINE), make_grant())

    def retry() -> dict:
        return {"ok": False, "reason": "safety policy 拒绝执行 ECS:ListServers"}

    out = run(confirmed_grant_and_retry(consent, OFFER, dict(DENIAL), retry))
    assert out["ok"] is False
    assert "ECS:ListServers=allow" in out["reason"]
    assert "再次调用将重新进入确认授予流程" in out["reason"]


def test_fallback_hint_mode_aware():
    """兜底指引按断言参数可达性分流：off 档（assertion_available=True）含
    grant=<档位> 断言快路径与操作员 policy 文件指引；auto/required 降级
    （缺省 false）不提参数断言并明示被忽略——那些模式下 grant/confirm 被静态
    忽略，指引它即无变化重试循环，仅指向操作员 policy 文件。"""
    hint = fallback_hint(OFFER, assertion_available=True)
    assert "grant=" in hint
    assert "policy 文件" in hint and "question" in hint
    plain = fallback_hint(OFFER)                     # auto/required 降级口径
    assert "grant=" not in plain and "confirm=true" not in plain   # 循环守卫
    assert "policy 文件" in plain and "操作员" in plain
    coarse = fallback_hint(COARSE_OFFER, assertion_available=True)
    assert "grant=" in coarse


def test_fallback_hint_off_mentions_persistent_via_operator_file():
    """off 档指引以「持久/批量档位」指向操作员文件（无 agent 数组写面）。"""
    hint = fallback_hint(COARSE_OFFER, assertion_available=True)
    assert "持久" in hint and "policy 文件" in hint
    degraded = fallback_hint(OFFER)
    assert "confirm=true" not in degraded and "操作员" in degraded


# ---------- manage_policy_list_only（工具面 list-only 读契约） ----------

def test_manage_policy_list_only_list_passes_through():
    """action=list 原样委派 service 层（信封形状由 manage_policy_ops 决定）。"""
    calls: list[tuple] = []

    def manage(action, line=None, scope=None, ttl_seconds=None):
        calls.append((action, line, scope, ttl_seconds))
        return {"ok": True, "action": "list", "rules": []}

    out = manage_policy_list_only(manage, "list")
    assert out["ok"] is True
    assert calls == [("list", None, None, None)]


def test_manage_policy_list_only_add_remove_refused():
    """add/remove 结构化拒绝（mode 无关）：reason 指向三条合法授予路径；
    service 层 manage 不被调用（写面仅剩确认路径与操作员文件）。"""
    calls: list[tuple] = []

    def manage(action, line=None, scope=None, ttl_seconds=None):
        calls.append((action, line, scope, ttl_seconds))
        return {"ok": True, "action": action}

    for action in ("add", "remove", "ADD", " Remove "):
        out = manage_policy_list_only(manage, action, line="X:Y=allow")
        assert out["ok"] is False
        assert out["action"] == action
        assert "list" in out["reason"] and "confirm=true" in out["reason"]
        assert "policy 文件" in out["reason"]          # 操作员路径
        assert "elicitation" in out["reason"]          # 协议问询路径
    assert calls == []                                 # 写动作从未触达 service


def test_manage_policy_list_only_batch_add_refused():
    """批量 line（数组）add 同样拒绝——批量写面仅操作员文件可达。"""
    calls: list[tuple] = []

    def manage(action, line=None, scope=None, ttl_seconds=None):
        calls.append((action, line, scope, ttl_seconds))
        return {"ok": True, "action": action}

    out = manage_policy_list_only(manage, "add",
                                  line=["ECS:*List*=allow", "ECS:*Get*=allow"])
    assert out["ok"] is False
    assert "list" in out["reason"]
    assert calls == []
