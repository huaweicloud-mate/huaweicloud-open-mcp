"""PolicyConsent：safety policy 变更的 MCP elicitation 交互语义（深模块）。

Interface（调用者需知的全部）：
- ``PolicyConsent(mode, elicit, grant)`` + ``offer_grant`` / ``confirm_grant``
  两方法；
- ``offer_grant`` 返回 ``GrantOutcome``：GrantRefused（未授予，envelope 为
  返回调用方的 denial）| GrantAccepted（已授予，rules 为规则清单，调用方经
  ``offer_grant_and_retry`` 以原参数重放原操作）；
- ``offer_grant_and_retry(consent, offer, denial, retry)``：授予成功后重放
  恰一次并返回执行结果（三个工具包装层共用编排）；
- ``confirmed_grant_and_retry(consent, offer, denial, retry, choice="api")``：
  off 模式断言式授予——调用方断言用户已确认所选档位，不经问询直接按
  choice 档位授予并重放恰一次（confirm=true 由包装层归一化为
  choice="api"；choice 派发与问询表单同源共享）；
- ``manage_policy_list_only(manage, action, ...)``：manage_policy 工具体
  （两模式共享，list-only——add/remove 结构化拒绝，防 agent 绕过用户自授）；
- ``ElicitFn`` adapter 契约：发送 elicitation 并归一化为 ``ElicitOutcome``，
  返回 None 表示客户端不支持 elicitation（capability 缺失 / 请求失败）；
- ``parse_elicit_mode`` 解析 --elicitation / HUAWEICLOUD_MCP_ELICIT。

mode 语义（implementation 内聚，不外泄）：
- off      从不 elicit：offer_grant 保持拒绝，reason 追加 fallback_hint
           （prompt 兜底指引：grant=<档位> 断言快路径 + 操作员 policy 文件；
            connect 场景为 confirm=true）；
- auto     尝试 elicit；客户端不支持 → 降级为 prompt 兜底（WARNING，
           reason 追加 fallback_hint，明示 grant/confirm 在该部署被忽略）；
- required 与 auto 行为一致（其曾有的差异——manage_policy 写面 fail-closed
           ——已随 2026-10 工具面 list-only 收敛消失；保留枚举值兼容既有
           部署启动参数）。

缺省 off（2026-09 起）：各 code agent 对 elicitation 支持参差，auto 的降级路径使
确认门语义随客户端漂移；默认关闭保持跨客户端行为一致（fail-safe），需要交互
确认门的部署显式传 auto/required（--elicitation / HUAWEICLOUD_MCP_ELICIT），
理由详见 AGENTS.md「校验规则」。

模块不依赖 mcp SDK / safety / service：adapter（``ctx_elicit_fn``）以 duck-typed
Protocol 接收 MCP Context；规则文本知识在 ``safety.policy.grant_rule``。
"""

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel

logger = logging.getLogger("common.elicit")

ElicitMode = Literal["auto", "required", "off"]
ELICIT_MODES: tuple[ElicitMode, ...] = ("auto", "required", "off")

GrantChoice = Literal["api_session", "readonly", "product", "api", "none"]

GrantArg = Literal["api", "api_session", "product", "readonly"]
"""execute_api 的 grant 档位参数（GrantChoice 去 none——拒绝表态不是可请求
档位）；Literal 经 SDK 生成 enum schema，非法值在协议边界拒收。"""

DiscoverGrantArg = Literal["api", "api_session", "product"]
"""call_server_tool 的 grant 档位参数（无 readonly——discover 侧不提供只读
规则集，schema 层即协议边界拒收；共享 confirm_grant 签名仍接受 readonly，
运行时经 _grant_readonly 结构化拒绝兜底）。"""

DiscoverGrantChoice = Literal["api_session", "product", "api", "none"]
"""readonly_rules 数据缺席的 offer（discover call_tool）的表单四选一值域
（与 GrantChoice 的差异仅无 readonly）。"""

PERSISTENT_GRANT_TIERS: tuple[GrantArg, ...] = ("api_session", "product", "readonly")
"""会话持续档（grant 断言的持久语义档）；api 为一次性档不在此列。"""


def resolve_assertion_choice(grant: "GrantArg | DiscoverGrantArg | None",
                              confirm: bool) -> "GrantArg | None":
    """包装层断言归一化单点（两模式共用）：grant 优先；confirm=true ≡
    grant="api"（历史等价写法）；两者皆无 → None（无断言）。
    auto/required 下调用方不路由断言路径，本函数结果仅 off 档消费。"""
    if grant is not None:
        return grant
    return "api" if confirm else None


def is_write_refusal(action: str, result: "Mapping[str, Any]") -> bool:
    """工具面写尝试拒绝的结构化谓词（两模式 wrapper 补记审计共用）。

    add/remove 动作 + ok=False（refusal 信封）——不依赖 MANAGE_POLICY_WRITE_
    REFUSAL 字符串全等（文案改写不影响审计触发；refusal 的 reason 恒以该
    常量为值，谓词取其语义超集）。
    """
    return ((action or "").strip().lower() in ("add", "remove")
            and isinstance(result, dict)
            and result.get("ok") is False)


def unapplied_tier_note(choice: GrantArg, still_denied: "str | None") -> "str | None":
    """off 档会话档断言未被消费的披露注；返回 None=静默。

    场景：grant=<会话档> 但调用未经拒绝即放行（最常见为残留 once——重试
    先撞校验保留——已随本次执行焚毁，所请求会话档从未落地；用户所选档位
    失真且原不可见）。still_denied 为**调用成功后**的策略复查结果（None=
    持久层（session/temporary/permanent）覆盖目标 → 静默，防御性重传不
    产生噪音；非 None → 披露）。语义判定而非规则文本比对——覆盖关系/
    大小写/遮蔽行均由策略评估器裁决（与 authorize 同口径）。
    **尽力判定（非恒真）**：复查窗口覆盖整个云往返——期间操作员热重载
    移除放行规则、temporary TTL 到期等均可使 still_denied 非 None 而本次
    放行并非来自 once（焚毁叙事为最可能解释而非断言）；无 policy store
    （快照模式）时调用方应传 None（静默——快照无 overlay/once 概念，
    披露指引不可兑现）。输出为咨询性 reason 附注，无强制后果。
    """
    if still_denied is None:
        return None
    return (f"已请求 grant={choice}，但本次调用未经新授予即放行——若此前仅"
            "授过一次性档（如 confirm），该档已随本次执行焚毁、所请求会话档"
            "未落地；如需会话内持续放行，被拒时再次传同一 grant 档位"
            "（经对话确认后授予）")


def apply_tier_disclosure(result: "Mapping[str, Any]", choice: GrantArg,
                          still_denied: "str | None") -> "dict[str, Any]":
    """披露注统一落位（两模式包装层共用）。

    still_denied None（持久层覆盖 / 无 store 静默口径）→ 等值新 dict 返回
    （copy-on-write：调用方 TypedDict 赋值形态干净，无消费者依赖身份）；
    否则附加 reason（既有 reason 前置拼接不覆写——多源 reason 保留）。
    """
    note = unapplied_tier_note(choice, still_denied)
    if note is None:
        return dict(result)
    prefix = (f"{result['reason']}；" if result.get("reason") else "")
    return {**result, "reason": prefix + note}


class PolicyChangeConfirm(BaseModel):
    """elicitation 表单 schema：仅 primitive 字段（MCP spec 限制）。"""

    confirm: bool


class GrantChoiceConfirm(BaseModel):
    """拒绝提议五选一表单 schema：api_session=最小规则（会话内）/ readonly=
    产品级只读规则集（会话内，offer.readonly_rules 存在时可用）/ product=
    产品级规则（会话内）/ api=最小规则（一次性）/ none=不授予。

    枚举顺序与弹窗/兜底指引文案同序（首次问询即推荐会话档，api 一次性殿后）；
    按值校验（pydantic Literal + ctx_elicit_fn 成员归一），重排 wire 兼容。
    仅 primitive 枚举字段（MCP spec 限制）；拒绝路径独用，
    connect 单一确认（无 coarse_rule 的最小确认）仍用 PolicyChangeConfirm。
    """

    choice: GrantChoice


class DiscoverGrantChoiceConfirm(BaseModel):
    """拒绝提议四选一表单 schema（offer.readonly_rules 数据缺席——discover
    call_tool 恒为此形）：api_session=最小规则（会话内）/ product=服务级
    全工具规则（会话内）/ api=最小规则（一次性）/ none=不授予。

    与 DiscoverGrantArg 对称：readonly 在表单层即不可选（两传输统一——
    消息文本/表单 enum/工具参数三处值域一致）；选型数据驱动于 offer，
    非模式判断。
    """

    choice: DiscoverGrantChoice


@dataclass(frozen=True)
class DenialOffer:
    """一次可授予的 policy 拒绝：主体 / 最小规则文本 / 原始拒绝 reason。

    coarse_rule 为可选的产品级（openapi）/服务级全工具（discover）通配规则，
    存在时弹窗五选一（api/api_session/product/readonly/none；readonly 仅在
    readonly_rules 存在时可用，数据缺席经 _grant_readonly 结构化拒绝——
    两传输统一，2026-10 起替代旧「误选按 none 处理」静默口径），None 时退化
    为单一最小规则确认（PolicyChangeConfirm）。
    readonly_rules 为可选的产品级只读规则集（openapi 侧经
    safety.policy.readonly_grant_rules 构造挂载；discover 不挂载 → readonly
    选项数据缺席即结构化拒绝）。
    """

    subject: str
    rule: str
    reason: str
    coarse_rule: str | None = None
    readonly_rules: list[str] | None = None


@dataclass(frozen=True)
class ElicitOutcome:
    """归一化 elicitation 结果；confirm 仅在 boolean 表单（PolicyChangeConfirm）、
    choice 仅在枚举表单（GrantChoiceConfirm 五值 / DiscoverGrantChoiceConfirm
    四值，_form_schema 按 offer 数据选型）accept 时有意义。"""

    action: Literal["accept", "decline", "cancel"]
    confirm: bool | None = None
    choice: GrantChoice | None = None


@dataclass(frozen=True)
class GrantRefused:
    """未发生任何授予：envelope 为返回给调用方的 denial（可能已增强——
    off 档/客户端不支持路径追加 fallback_hint，授予失败附失败原因）。

    防御路径（非 denial 形状输入）原样透传入参，故类型为 Any 而非
    Mapping——调用方对 envelope 只做透传，不做键访问。"""

    envelope: Any


@dataclass(frozen=True)
class GrantAccepted:
    """发生授予：rules 为已授予规则文本（readonly 规则集为已成功子集）；
    failed 为 readonly 规则集授予失败的子集（非 readonly 路径恒空）——
    ``offer_grant_and_retry`` 在重试成功信封 reason 披露回执、重试仍被
    policy 拒绝时并入上下文（均可再次调用重新提议或由操作员编辑 policy 文件
    补授）。调用方应经
    ``offer_grant_and_retry`` 以原参数重放原操作。"""

    rules: tuple[str, ...]
    failed: tuple[str, ...] = ()


GrantOutcome: TypeAlias = GrantRefused | GrantAccepted


# policy 拒绝 reason 形态前缀（safety/policy.py check/check_server 三态同前缀：
# 拒绝执行 {product}:{api} / 拒绝调用 {server}:{tool} / 拒绝连接 {server}）——
# offer_grant_and_retry 的重试上下文门控判据。elicit 不 import safety（common
# 公共底座边界），此前缀是两模块间的文档化字符串契约。
_POLICY_DENIAL_PREFIX = "safety policy 拒绝"


ElicitFn: TypeAlias = Callable[[str, type[BaseModel]], Awaitable[ElicitOutcome | None]]
GrantFn: TypeAlias = Callable[[str, str | None], dict[str, Any]]


class ElicitContext(Protocol):
    """duck-typed MCP Context：真实 Context 的 ``elicit`` 满足本协议。"""

    async def elicit(self, message: str, schema: type[BaseModel]) -> Any: ...


def parse_elicit_mode(raw: str | None) -> ElicitMode:
    """解析 elicitation 模式；空/非法值宽容回退 off（缺省关闭，确认门需显式 opt-in）。"""
    text = (raw or "").strip().lower()
    if text in ELICIT_MODES:
        return text
    return "off"


def _minimal_scope_note(scope: str) -> str:
    """minimal_scope 档位的弹窗语义（connect 注入 session 档时如实渲染）。"""
    if scope == "session":
        return "会话内授权：本次会话内持续放行该目标"
    return "一次性授权：仅放行下一次执行，用后即焚"


def denial_message(offer: DenialOffer, minimal_scope: str = "once") -> str:
    """拒绝路径提议授予弹窗文案。

    有 coarse_rule：选项按推荐度排序（api_session/readonly?/product/api/none，
    首次问询即推荐会话档；api 一次性档仅明确单次使用时选择——枚举顺序与
    文案顺序解耦）；无 coarse_rule：单一最小规则二选一确认，scope 语义按
    minimal_scope 渲染（connect 注入 session 档时弹窗如实陈述实际授予档位）。
    """
    if offer.coarse_rule:
        readonly = ""
        if offer.readonly_rules:
            readonly = (f"- readonly：授予产品级只读规则集 "
                        f"（{'/'.join(offer.readonly_rules)}，"
                        "会话内生效，重启即失；浏览/多步任务首选）\n")
        api_qualifier = "" if minimal_scope == "session" else "；仅明确单次使用时选择"
        return (f"{offer.reason}\n\n"
                f"是否授予规则放行 {offer.subject}？\n"
                f"- api_session：授予最小规则 '{offer.rule}'"
                "（会话内：本次会话内持续放行该目标，重启即失；同一目标重复使用首选）\n"
                f"{readonly}"
                f"- product：授予产品级规则 '{offer.coarse_rule}'"
                "（会话内生效，覆盖该产品全部 API，重启即失；"
                "需持久授权请由操作员编辑 policy 文件）\n"
                f"- api：仅授予最小规则 '{offer.rule}'"
                f"（{_minimal_scope_note(minimal_scope)}{api_qualifier}）\n"
                "- none：不授予")
    return (f"{offer.reason}\n\n"
            f"是否授予最小规则 '{offer.rule}' 放行 {offer.subject}？"
            f"规则为{_minimal_scope_note(minimal_scope)}"
            "（重启即失，无需回收；需持久授权请由操作员编辑 policy 文件）。")


_OPERATOR_FILE_NOTE = ("（热生效、无需重启；文件规则为永久档——跨重启生效，"
                       "比任何会话档宽，按需选择粒度）")
"""操作员 policy 文件指引的共享句（fallback_hint 四条 return 统一插值，
第十一轮抽常量——此前逐字双份 + connect 路径缺席，三形态可漂移）。"""


def fallback_hint(offer: DenialOffer, *,
                  assertion_available: bool = False) -> str:
    """未发生问询路径（off 档 / 客户端不支持）的拒绝兜底指引。

    assertion_available（调用方按 consent mode 传入，仅 off 档为 true）：指引
    经对话问询确认后以工具参数断言授予——grant=<档位>（execute_api /
    call_server_tool，档位与提议表单同构）或 confirm=true（connect 场景），
    同次调用完成授予与执行；false（auto/required 降级路径——grant/confirm
    在这些模式被包装层静态忽略）不指引参数断言并明示其被忽略——避免 agent
    照做后落入逐字节相同的拒绝循环，仅指向操作员 policy 文件（附规则文本
    供直接补授）。prompt 约定（软兜底）：经对话/交互式问询（如 question
    工具）向用户确认所选档位。不提及协议级 elicitation。
    """
    if not assertion_available:
        base = ("；本部署当前无法经协议问询确认（客户端不支持 elicitation），"
                "confirm/grant 参数在此部署被忽略（重传不会改变结果）——请操作员"
                f"编辑 policy 文件授予规则{_OPERATOR_FILE_NOTE}，可参考：")
        if offer.coarse_rule:
            readonly = ""
            if offer.readonly_rules:
                readonly = (f"/readonly=产品级只读规则集 "
                            f"'{'/'.join(offer.readonly_rules)}'")
            return (base + f"api_session=最小规则 '{offer.rule}'"
                    f"{readonly}"
                    f"/product=产品级规则 '{offer.coarse_rule}'")
        return base + f"规则 '{offer.rule}'"
    if offer.coarse_rule:
        readonly = ""
        if offer.readonly_rules:
            readonly = (f"/readonly=产品级只读规则集 "
                        f"'{'/'.join(offer.readonly_rules)}'（浏览/多步任务首选）")
        return ("；如确需执行，请先经对话/交互式问询（如 question 工具）向用户确认"
                "所选档位，确认后重传本工具并传 grant=<档位>（服务端在同次调用内"
                "授予所选规则并直接执行）；持久/批量档位请操作员直接编辑 policy "
                f"文件{_OPERATOR_FILE_NOTE}："
                f"api_session=最小规则 '{offer.rule}'（会话内，同一目标重复使用首选）"
                f"{readonly}"
                f"/product=产品级规则 '{offer.coarse_rule}'（会话内）"
                f"/api=最小规则 '{offer.rule}'（一次性，仅明确单次使用）")
    return ("；如确需执行，请先经对话/交互式问询（如 question 工具）向用户确认，"
            "确认后重传本工具并传 confirm=true（服务端在同次调用内授予最小规则"
            f"并直接执行）；需持久授权请由操作员编辑 policy 文件"
            f"{_OPERATOR_FILE_NOTE}。规则 '{offer.rule}'")


class PolicyConsent:
    """safety policy 变更的确认机制：何时问、怎么问、如何解释回答。

    offer_grant 用于问询路径（accept → 授予并返回 GrantAccepted；coarse_rule
    存在时五选一——api=最小规则（minimal_scope 档）、api_session=最小规则
    （固定 session 档）、product=产品级规则（固定 session 档）、
    readonly=产品级只读规则集（offer.readonly_rules 存在时可用，逐条 session
    档授予 best-effort 无回滚，任一条成功即 GrantAccepted 携带已成功子集；
    数据缺席结构化拒绝并列可用档位）、none=不授予）；
    confirm_grant 用于 off 模式断言式授予（grant 参数档位与 confirm=true
    归一化的 choice="api"，共享同一派发表——off 档授权选项与问询路径对齐）。

    scope 知识内聚于本模块：choice→scope 映射（api→minimal_scope，
    api_session/product/readonly→session）是接口不变量，调用方仅经
    minimal_scope 注入最小授予档（openapi execute/discover call_tool 缺省
    once；discover connect 传 session）。规则文本语法知识在
    ``safety.policy.grant_rule`` / ``readonly_grant_rules``，本模块仅消费
    offer 携带的规则文本（数据驱动，discover 不挂 readonly_rules 即结构化
    拒绝）。session 档 = 本次 code agent 会话（进程存活期），非 discover 到
    远端 MCP server 的连接会话（断开/空闲回收后 session 档授权仍在）。
    断言式授予无服务端用户核验（off 档信任模型：grant/confirm 是 agent 对
    「用户已在对话中确认」的断言）——溯源经 wiring 侧 source 标记入审计。
    """

    def __init__(self, mode: str, elicit: ElicitFn,
                 grant: GrantFn | None = None,
                 minimal_scope: str = "once"):
        self.mode = parse_elicit_mode(mode)
        self._elicit = elicit
        self._grant = grant
        self._minimal_scope = minimal_scope

    async def offer_grant(self, offer: DenialOffer,
                          denial: Mapping[str, Any]) -> GrantOutcome:
        """对 policy 拒绝提议授予；返回类型化 GrantOutcome（防御非 dict 输入）。

        未授予 → GrantRefused（envelope 为原 denial 或增强后的 denial：off 档/
        客户端不支持追加 ``fallback_hint`` 兜底指引，授予失败附失败原因；
        decline/cancel/refuse 与 none 选择保持原 denial——用户已表态不加指引）；
        授予成功 → GrantAccepted（rules 为已授予规则文本，调用方经
        ``offer_grant_and_retry`` 以原参数重放原操作）。表单经 ``_form_schema``
        按 offer 数据三路选型：无 coarse_rule → PolicyChangeConfirm 单一确认；
        有 readonly_rules → GrantChoiceConfirm 五选一；仅 coarse（discover
        call_tool 形）→ DiscoverGrantChoiceConfirm 四选一。
        """
        if not (isinstance(denial, dict) and denial.get("ok") is False):
            return GrantRefused(denial)
        if self.mode == "off":
            return GrantRefused(self._with_fallback_hint(offer, denial))
        outcome = await self._ask(denial_message(offer, self._minimal_scope),
                                  self._form_schema(offer))
        if outcome is None:
            logger.warning("grant offer skipped: client unsupported elicitation (mode=%s)",
                           self.mode)
            return GrantRefused(self._with_fallback_hint(offer, denial))
        if outcome.action == "accept" and outcome.choice == "readonly":
            return self._grant_readonly(offer, denial)
        if outcome.action == "accept" and offer.coarse_rule:
            picked = self._resolve_choice(offer, outcome.choice)
            if picked is None:   # none / 缺失 / 未知 choice：不授予
                return GrantRefused(denial)
            rule, scope = picked
        elif outcome.action == "accept" and outcome.confirm:
            rule, scope = offer.rule, self._minimal_scope
        else:
            logger.warning("grant offer declined: %s (action=%s)",
                           offer.coarse_rule or offer.rule, outcome.action)
            return GrantRefused(denial)
        grant = self._grant
        if grant is None:
            logger.warning("grant offer accepted but no grant fn wired: %s", rule)
            return GrantRefused(denial)
        result = grant(rule, scope)
        if result.get("ok"):
            logger.info("grant accepted: %s (scope=%s)", rule, scope)
            return GrantAccepted((rule,))
        reason = result.get("reason") or "未知原因"
        logger.warning("grant failed: %s: %s", rule, reason)
        return GrantRefused(
            {**denial,
             "reason": f"{denial.get('reason', '')}；自动授予 {rule} 失败: {reason}"})

    def confirm_grant(self, offer: DenialOffer,
                      denial: Mapping[str, Any],
                      choice: GrantArg = "api") -> GrantOutcome:
        """off 模式断言式授予：不经问询直接按 choice 档位授予（GrantOutcome）。

        调用方（包装层）在 consent mode=off 且工具入参 grant=<档位> 或
        confirm=true（包装层归一化为 choice="api"）时路由到此（双保险：本方法
        再验 mode——off 档部署信任调用方经对话问询确认后自授，此处把内部授予
        接缝的调用并入同次工具调用；auto/required 的确认必须走协议问询，
        持久/批量规则由操作员编辑 policy 文件，误路由返回结构化拒绝——断言式
        确认不得绕过部署开启的确认门）。choice 派发与问询表单同源共享
        （_resolve_choice/_grant_readonly）：缺省 api=最小规则（minimal_scope
        档）。授权经注入 grant fn（service 层 manage_policy add 内部接缝），
        审计照常入账（openapi/discover 同构，source 溯源经 wiring 侧注入）。
        授予失败信封不附 fallback_hint——刚失败的机制（grant/confirm 重传）
        不可再兜售（strict_sessions 拒绝等场景重传即无变化循环），失败原因
        自带操作员补授指引。
        """
        if not (isinstance(denial, dict) and denial.get("ok") is False):
            return GrantRefused(denial)
        if self.mode != "off":
            return GrantRefused({**denial, "reason": (
                f"{denial.get('reason', '')}；断言式授予（grant/confirm）仅在 "
                "--elicitation off 模式下生效（当前部署的确认经协议问询；持久/"
                "批量规则由操作员编辑 policy 文件）")})
        if choice == "readonly":
            return self._grant_readonly(offer, denial)
        picked = self._resolve_choice(offer, choice)
        if picked is None:
            logger.warning("confirmed grant: choice %r undeliverable, keeping denial",
                           choice)
            return GrantRefused(denial)
        grant = self._grant
        if grant is None:
            logger.warning("confirmed grant unavailable: no grant fn wired: %s",
                           offer.rule)
            return GrantRefused(denial)
        rule, scope = picked
        result = grant(rule, scope)
        if result.get("ok"):
            logger.info("confirmed grant: %s (scope=%s)", rule, scope)
            return GrantAccepted((rule,))
        reason = result.get("reason") or "未知原因"
        logger.warning("confirmed grant failed: %s: %s", rule, reason)
        return GrantRefused(
            {**denial,
             "reason": f"{denial.get('reason', '')}；自动授予 {rule} 失败: {reason}"})

    def _with_fallback_hint(self, offer: DenialOffer,
                            denial: Mapping[str, Any]) -> Mapping[str, Any]:
        """未问询即保持拒绝：reason 追加兜底指引（拒绝本体不变）。

        指引按 self.mode 感知断言参数可达性——仅 off 档提 grant=<档位>/
        confirm=true 快路径（auto/required 降级路径的 grant/confirm 被包装层
        静态忽略，指引它即循环，hint 明示其被忽略）。
        """
        return {**denial,
                "reason": denial.get("reason", "") + fallback_hint(
                    offer, assertion_available=self.mode == "off")}

    def _form_schema(self, offer: DenialOffer) -> type[BaseModel]:
        """按 offer 数据选表单：无 coarse_rule → 单一确认；有 readonly_rules →
        五选一；仅 coarse（discover call_tool 形）→ 四选一（readonly 表单层
        即不可选，与消息文本/工具参数值域一致）。"""
        if not offer.coarse_rule:
            return PolicyChangeConfirm
        return GrantChoiceConfirm if offer.readonly_rules \
            else DiscoverGrantChoiceConfirm

    def _grant_readonly(self, offer: DenialOffer,
                        denial: Mapping[str, Any]) -> GrantOutcome:
        """readonly 选择：逐条授予产品级只读规则集（session 档，best-effort 无回滚）。

        session 档为内存 overlay（无文件 I/O）且规则文本构造期校验，add 失败
        近乎不可达；任一条成功即 GrantAccepted（rules 为已成功子集、failed
        为失败子集——重试可能命中已授予规则，失败明细经 WARNING 留痕并在
        重试信封披露回执；只读 + 会话内 + 重启即失，部分授予无害），零成功
        保持拒绝（envelope 附失败明细与补授指引），不引入 revoke seam
        （deletion test）。readonly_rules 数据缺席（discover 侧恒缺席）→
        结构化拒绝并列可用档位（问询表单与 grant 参数两传输统一；旧口径
        「误选按 none 处理」静默拒绝已收敛——2026-10 起声明为行为变更）。
        """
        grant = self._grant
        rules = offer.readonly_rules or []
        if grant is None:
            logger.warning("readonly grant unavailable: rules=%s grant_wired=%s",
                           len(rules), grant is not None)
            return GrantRefused(denial)
        if not rules:
            logger.warning("readonly grant unavailable: readonly_rules absent")
            return GrantRefused({**denial, "reason": (
                f"{denial.get('reason', '')}；readonly 档不可用（本目标无只读"
                "规则集——readonly 仅 openapi 侧提供），可改选 api_session/"
                "product/api 档位，或由操作员编辑 policy 文件补授")})
        granted: list[str] = []
        failed_rules: list[str] = []
        failed_details: list[str] = []
        for rule in rules:
            result = grant(rule, "session")
            if result.get("ok"):
                granted.append(rule)
            else:
                failed_rules.append(rule)
                failed_details.append(
                    f"{rule}: {result.get('reason') or '未知原因'}")
        if granted:
            if failed_rules:
                logger.warning("readonly grant partial: granted=%d failed=%d (%s)",
                               len(granted), len(failed_rules), ";".join(failed_details))
            else:
                logger.info("grant accepted: readonly ruleset (%d rules, scope=session)",
                            len(granted))
            # failed 为裸规则文本（操作员编辑 policy 文件可直接使用的 line 形态），
            # 失败原因仅留 WARNING——回执披露规则即可自纠，不复述 grant 内部原因
            return GrantAccepted(tuple(granted), failed=tuple(failed_rules))
        logger.warning("readonly grant failed: %d rules all failed", len(failed_details))
        reason = (f"{denial.get('reason', '')}；readonly 规则集授予失败: "
                  f"{';'.join(failed_details)}；可由操作员编辑 policy 文件补授")
        return GrantRefused({**denial, "reason": reason})

    def _resolve_choice(self, offer: DenialOffer,
                        choice: GrantChoice | None) -> tuple[str, str] | None:
        """共享派发表（问询表单与 grant 参数两传输同源）：choice → (规则文本, scope)。

        api→minimal_scope（execute/call_tool 缺省 once、connect session）、
        api_session→session、product→coarse_rule+session（coarse 缺席返回
        None）；none/缺失/未知 choice 返回 None 不授予。readonly 走
        _grant_readonly 多规则路径，不在本表（调用方先行分支）。
        """
        if choice == "api":
            return offer.rule, self._minimal_scope
        if choice == "api_session":
            return offer.rule, "session"
        if choice == "product" and offer.coarse_rule:
            return offer.coarse_rule, "session"
        if choice not in ("api", "api_session", "product", "none", None):
            logger.warning("grant choice %r undeliverable, keeping denial", choice)
        return None

    async def _ask(self, message: str,
                   schema: type[BaseModel] = PolicyChangeConfirm) -> ElicitOutcome | None:
        """发送 elicitation；adapter 归一化结果（None=客户端不支持）。"""
        return await self._elicit(message, schema)


def ctx_elicit_fn(ctx: ElicitContext) -> ElicitFn:
    """MCP Context → ElicitFn adapter：归一化 SDK 结果，失败归一为 None（不支持）。

    accept 数据按字段归一：confirm（boolean 表单）与 choice（枚举表单）
    独立读取，两 schema 经同一 adapter 共存。
    """

    async def elicit(message: str, schema: type[BaseModel]) -> ElicitOutcome | None:
        try:
            outcome = await ctx.elicit(message, schema)
        except Exception as exc:
            logger.warning("elicitation failed (treated as unsupported): %s", exc)
            return None
        action = getattr(outcome, "action", None)
        if action == "accept":
            data = getattr(outcome, "data", None)
            confirm = getattr(data, "confirm", None)
            choice = getattr(data, "choice", None)
            return ElicitOutcome(
                action="accept",
                confirm=bool(confirm) if confirm is not None else None,
                choice=choice
                if choice in ("api", "api_session", "product", "readonly", "none")
                else None)
        if action == "decline":
            return ElicitOutcome(action="decline")
        return ElicitOutcome(action="cancel")

    return elicit


MANAGE_POLICY_WRITE_REFUSAL = (
    "manage_policy 仅保留 action=list（读）——add/remove 已从工具面收敛，"
    "防止 agent 未经用户确认自授规则。授予路径：off 模式经被拒工具传 "
    "grant=<档位>（api/api_session/product/readonly，与提议表单同构；"
    "discover 侧无 readonly）或 confirm=true（execute/call_tool 等价 "
    "grant=api 一次性档；connect 为会话内连接级档）；auto/required 经"
    "被拒工具的 elicitation 提议；持久/批量/自定义规则由操作员直接编辑 "
    "policy 文件（热生效、无需重启）。注意：已授予的会话内规则随连接/"
    "进程结束自动失效，文件编辑不影响在途会话的 overlay 规则")


def manage_policy_list_only(
        manage: Callable[..., Any],
        action: str, line: str | list[str] | None = None,
        scope: str | None = None,
        ttl_seconds: int | None = None) -> dict[str, Any]:
    """manage_policy 工具体（openapi/discover 两模式共享）：list-only 读面。

    add/remove 结构化拒绝（``MANAGE_POLICY_WRITE_REFUSAL`` 指向三条合法授予
    路径）；action=list 原样委派 manage（service 层 manage_policy 仍为内部
    可写接缝——elicitation/confirm 授予经 functools.partial(svc.manage_policy,
    "add") 直调，不经本工具面，2026-10 起 agent 可达写面仅此三条确认路径）。
    """
    if (action or "").strip().lower() in ("add", "remove"):
        logger.warning("manage_policy write refused at tool face: %s", action)
        return {"ok": False, "action": action,
                "reason": MANAGE_POLICY_WRITE_REFUSAL}
    result: dict[str, Any] = manage(action, line=line, scope=scope,
                                    ttl_seconds=ttl_seconds)
    return result


async def offer_grant_and_retry(
        consent: "PolicyConsent", offer: DenialOffer,
        denial: Mapping[str, Any],
        retry: Callable[[], Any]) -> Any:
    """提议授予并在成功后重放原操作恰一次（openapi/discover 包装层共用编排）。

    未授予 → 返回（可能增强后的）denial；授予成功 → 调 retry 以原参数重放
    （同步/异步 thunk 均可，inspect.isawaitable 判据覆盖 coroutine/Task/Future）
    并返回其结果。readonly 部分授予（outcome.failed 非空）：重试成功时在
    reason 披露失败子集回执（wire 可选字段，可再次调用重新提议或由操作员
    编辑 policy 文件补授），
    重试仍被 policy 拒绝时并入授予上下文。policy 拒绝（reason 前缀门控
    ``_POLICY_DENIAL_PREFIX``）时在 reason 追加已授予规则上下文与再调用
    指引（只读规则集不含该接口 / 一次性授权已被并发消费两个场景），
    不二次弹窗、不循环；其余重试失败（参数校验先于授权门返回、上游执行
    错误）原样返回不注上下文——自纠面不被污染，once 规则为修正参数后的
    下一次调用保留。
    """
    return await _dispatch_granted_retry(
        await consent.offer_grant(offer, denial), retry)


async def confirmed_grant_and_retry(
        consent: "PolicyConsent", offer: DenialOffer,
        denial: Mapping[str, Any], retry: Callable[[], Any],
        choice: GrantArg = "api") -> Any:
    """off 模式断言式授予-重放编排：调用方断言用户已确认所选档位（对话问询），
    不经协议问询直接按 choice 档位授予并重放恰一次（confirm=true 由包装层
    归一化为 choice="api"——同次调用完成授予与执行）；choice 派发与问询
    路径同源共享。信封呈现与问询路径共用 ``_dispatch_granted_retry``
    （重试仍被拒附上下文、其余失败原样返回）。
    """
    return await _dispatch_granted_retry(
        consent.confirm_grant(offer, denial, choice), retry)


async def _dispatch_granted_retry(outcome: GrantOutcome,
                                  retry: Callable[[], Any]) -> Any:
    """GrantOutcome 已定后的重放编排与信封呈现（问询/断言两条授予路径共用）。"""
    if isinstance(outcome, GrantRefused):
        return outcome.envelope
    result = retry()
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        return result
    reason = result.get("reason")
    if result.get("ok") is True and outcome.failed:
        note = (f"readonly 规则集部分授予失败: {';'.join(outcome.failed)}"
                "（本次已按已授予子集放行，其余可再次调用重新提议或由操作员编辑 "
                "policy 文件补授）")
        prefix = f"{reason}；" if isinstance(reason, str) and reason else ""
        return {**result, "reason": prefix + note}
    if (result.get("ok") is False and isinstance(reason, str)
            and reason.startswith(_POLICY_DENIAL_PREFIX)):
        note = f"；此前已授予 {';'.join(outcome.rules)}（热生效）"
        if outcome.failed:
            note += (f"；部分规则授予失败: {';'.join(outcome.failed)}"
                     "（可再次调用重新提议或由操作员编辑 policy 文件补授）")
        note += ("——该拒绝可能因已授予规则未覆盖此目标，或一次性授权已被并发消费，"
                 "再次调用将重新进入确认授予流程")
        return {**result, "reason": f"{reason}{note}"}
    return result
