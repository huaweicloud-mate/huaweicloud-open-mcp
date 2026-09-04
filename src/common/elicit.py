"""PolicyConsent：safety policy 变更的 MCP elicitation 交互语义（深模块）。

Interface（调用者需知的全部）：
- ``PolicyConsent(mode, elicit, grant)`` + ``offer_grant`` / ``gate_change`` 两方法；
- ``ElicitFn`` adapter 契约：发送 elicitation 并归一化为 ``ElicitOutcome``，
  返回 None 表示客户端不支持 elicitation（capability 缺失 / 请求失败）；
- ``parse_elicit_mode`` 解析 --elicitation / HUAWEICLOUD_MCP_ELICIT。

mode 语义（implementation 内聚，不外泄）：
- off      从不 elicit：offer_grant 保持拒绝，reason 追加 fallback_hint
           （prompt 兜底指引：经交互式问询确认后 manage_policy 授予）；
- auto     尝试 elicit；客户端不支持 → 降级为 prompt 兜底（WARNING，
           reason 追加 fallback_hint，语义同 off）；
- required 客户端不支持 → gate_change 返回可操作 reason（fail-closed），
           offer_grant 拒绝路径不问询、reason 追加 fallback_hint。

缺省 off（2026-09 起）：各 code agent 对 elicitation 支持参差，auto 的降级路径使
确认门语义随客户端漂移；默认关闭保持跨客户端行为一致（fail-safe），需要交互
确认门的部署显式传 auto/required（--elicitation / HUAWEICLOUD_MCP_ELICIT），
理由详见 AGENTS.md「校验规则」。

模块不依赖 mcp SDK / safety / service：adapter（``ctx_elicit_fn``）以 duck-typed
Protocol 接收 MCP Context；规则文本知识在 ``safety.policy.grant_rule``。
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel

logger = logging.getLogger("common.elicit")

ElicitMode = Literal["auto", "required", "off"]
ELICIT_MODES: tuple[ElicitMode, ...] = ("auto", "required", "off")

GrantChoice = Literal["api", "api_session", "product", "none"]


class PolicyChangeConfirm(BaseModel):
    """elicitation 表单 schema：仅 primitive 字段（MCP spec 限制）。"""

    confirm: bool


class GrantChoiceConfirm(BaseModel):
    """拒绝提议四选一表单 schema：api=最小规则（一次性）/ api_session=最小规则
    （会话内）/ product=产品级规则（会话内）/ none=不授予。

    仅 primitive 枚举字段（MCP spec 限制）；拒绝路径独用，
    manage_policy 确认门仍用 PolicyChangeConfirm。
    """

    choice: GrantChoice


@dataclass(frozen=True)
class DenialOffer:
    """一次可授予的 policy 拒绝：主体 / 最小规则文本 / 原始拒绝 reason。

    coarse_rule 为可选的产品级（openapi）/服务级全工具（discover）通配规则，
    存在时弹窗三选一（api/product/none），None 时退化为二选一（api/none）。
    """

    subject: str
    rule: str
    reason: str
    coarse_rule: str | None = None


@dataclass(frozen=True)
class ElicitOutcome:
    """归一化 elicitation 结果；confirm 仅在 boolean 表单（PolicyChangeConfirm）、
    choice 仅在枚举表单（GrantChoiceConfirm）accept 时有意义。"""

    action: Literal["accept", "decline", "cancel"]
    confirm: bool | None = None
    choice: GrantChoice | None = None


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


def change_message(action: str, line: str) -> str:
    """manage_policy add/remove 确认弹窗文案。"""
    if action == "add":
        return (f"确认新增 safety policy 规则 '{line}'？"
                "缺省仅在当前会话内生效（重启即失，无需回收）；"
                '需跨重启持久时显式传 scope="permanent"（写入策略文件）。')
    if action == "remove":
        return f"确认移除 safety policy 规则 '{line}'？"
    return f"确认对 safety policy 执行 {action}：'{line}'？"


def denial_message(offer: DenialOffer) -> str:
    """拒绝路径提议授予弹窗文案。

    有 coarse_rule：并列 api/api_session/product/none 四选项（api=一次性最小规则，
    api_session=会话内最小规则，product=会话内产品级规则，none=不授予）；
    无 coarse_rule：单一最小规则二选一确认（一次性语义，与历史文案一致）。
    """
    if offer.coarse_rule:
        return (f"{offer.reason}\n\n"
                f"是否授予规则放行 {offer.subject}？\n"
                f"- api：仅授予最小规则 '{offer.rule}'"
                "（一次性：仅放行下一次执行，用后即焚）\n"
                f"- api_session：授予最小规则 '{offer.rule}'"
                "（会话内：本次会话内持续放行该目标，重启即失）\n"
                f"- product：授予产品级规则 '{offer.coarse_rule}'"
                "（会话内生效，覆盖该产品全部 API，重启即失；"
                "需持久授权请经 manage_policy 显式授予）\n"
                "- none：不授予")
    return (f"{offer.reason}\n\n"
            f"是否授予最小规则 '{offer.rule}' 放行 {offer.subject}？"
            "规则为一次性授权：仅放行下一次执行，用后即焚"
            "（重启即失，无需回收；需持久授权请经 manage_policy 显式授予）。")


def fallback_hint(offer: DenialOffer) -> str:
    """未发生问询路径（off 档 / 客户端不支持）的拒绝兜底指引。

    prompt 约定（软兜底）：引导调用方 LLM 经对话/交互式问询（如 question 工具）
    向用户确认后经 manage_policy 授予；选项语义与 elicitation 四选一表单一致
    （coarse 存在时 api/api_session/product/none，否则仅最小规则），
    不提及协议级 elicitation。
    """
    base = ("；如确需执行，请先经对话/交互式问询（如 question 工具）向用户确认后，"
            "调用 manage_policy 授予：")
    if offer.coarse_rule:
        return (base + f"api=最小规则 '{offer.rule}'（一次性）/"
                f"api_session=最小规则 '{offer.rule}'（会话内）/"
                f"product=产品级规则 '{offer.coarse_rule}'（会话内）/none=不授予")
    return base + f"规则 '{offer.rule}'"


class PolicyConsent:
    """safety policy 变更的确认机制：何时问、怎么问、如何解释回答。

    offer_grant 用于拒绝路径（accept → 自动授予并增强 denial；coarse_rule
    存在时四选一——api=最小规则（minimal_scope 档）、api_session=最小规则
    （固定 session 档）、product=产品级规则（固定 session 档）、none=不授予）；
    gate_change 用于 manage_policy add/remove（check 习语：None=放行）。

    scope 知识内聚于本模块：choice→scope 映射（api→minimal_scope，
    api_session/product→session）是接口不变量，调用方仅经 minimal_scope 注入
    最小授予档（openapi execute/discover call_tool 缺省 once；discover connect
    传 session）。
    session 档 = 本次 code agent 会话（进程存活期），非 discover 到远端 MCP
    server 的连接会话（断开/空闲回收后 session 档授权仍在）。
    """

    def __init__(self, mode: str, elicit: ElicitFn,
                 grant: GrantFn | None = None,
                 minimal_scope: str = "once"):
        self.mode = parse_elicit_mode(mode)
        self._elicit = elicit
        self._grant = grant
        self._minimal_scope = minimal_scope

    async def offer_grant(self, offer: DenialOffer,
                          denial: Mapping[str, Any]) -> Mapping[str, Any]:
        """对 policy 拒绝提议授予；返回原 denial 或增强后的 denial（防御非 dict 输入）。

        增强 result 为 dict（原 denial 键集 + granted_rule/reason 增强）；
        其余路径原样返回入参对象。coarse_rule 存在时以 GrantChoiceConfirm
        四选一问询，否则以 PolicyChangeConfirm 单一确认（向后兼容）。
        未发生问询即保持拒绝的路径（off 档 / 客户端不支持）——reason 追加
        ``fallback_hint`` 兜底指引（prompt 约定，经交互式问询后 manage_policy
        授予）；decline/cancel/refuse 与授予路径不加（用户已表态或已入流程）。
        """
        if not (isinstance(denial, dict) and denial.get("ok") is False):
            return denial
        if self.mode == "off":
            return self._with_fallback_hint(offer, denial)
        outcome = await self._ask(denial_message(offer),
                                  GrantChoiceConfirm if offer.coarse_rule
                                  else PolicyChangeConfirm)
        if outcome is None:
            logger.warning("grant offer skipped: client unsupported elicitation (mode=%s)",
                           self.mode)
            return self._with_fallback_hint(offer, denial)
        if outcome.action == "accept" and offer.coarse_rule:
            picked = self._pick_coarse(outcome, offer)
            if picked is None:   # none / 缺失 / 未知 choice：不授予
                return denial
            rule, scope = picked
        elif outcome.action == "accept" and outcome.confirm:
            rule, scope = offer.rule, self._minimal_scope
        else:
            logger.warning("grant offer declined: %s (action=%s)",
                           offer.coarse_rule or offer.rule, outcome.action)
            return denial
        grant = self._grant
        if grant is None:
            logger.warning("grant offer accepted but no grant fn wired: %s", rule)
            return denial
        result = grant(rule, scope)
        if result.get("ok"):
            logger.info("grant accepted: %s (scope=%s)", rule, scope)
            if (scope == "session" and offer.coarse_rule is not None
                    and rule == offer.coarse_rule):
                kind = "会话内产品级规则"
            elif scope == "session":
                kind = "会话内最小规则"
            else:
                kind = "一次性规则"
            return {**denial, "granted_rule": rule,
                    "reason": (f"{denial.get('reason', '')}"
                               f"；用户已通过确认授予{kind} {rule}"
                               f"（热生效），请重新调用")}
        reason = result.get("reason") or "未知原因"
        logger.warning("grant failed: %s: %s", rule, reason)
        return {**denial,
                "reason": f"{denial.get('reason', '')}；自动授予 {rule} 失败: {reason}"}

    @staticmethod
    def _with_fallback_hint(offer: DenialOffer,
                            denial: Mapping[str, Any]) -> Mapping[str, Any]:
        """未问询即保持拒绝：reason 追加兜底指引（拒绝本体不变）。"""
        return {**denial, "reason": denial.get("reason", "") + fallback_hint(offer)}

    @staticmethod
    def _pick_coarse(outcome: ElicitOutcome,
                     offer: DenialOffer) -> tuple[str, str] | None:
        """解释 coarse 四选一回答 → (规则文本, scope)；不授予返回 None。"""
        choice = outcome.choice
        if choice == "api":
            return offer.rule, "once"
        if choice == "api_session":
            return offer.rule, "session"
        if choice == "product" and offer.coarse_rule:
            return offer.coarse_rule, "session"
        if choice not in ("api", "api_session", "product", "none"):
            logger.warning("grant offer: unknown choice %r, keeping denial", choice)
        return None

    async def gate_change(self, action: str, line: str) -> str | None:
        """manage_policy add/remove 确认门。返回 None 放行，返回字符串为拒绝 reason。"""
        if self.mode == "off":
            return None
        outcome = await self._ask(change_message(action, line))
        if outcome is None:
            if self.mode == "required":
                return ("客户端不支持 elicitation，无法完成变更确认"
                        "（启动参数 --elicitation off 可显式关闭确认机制）")
            logger.warning("manage_policy proceeds without consent: "
                           "client unsupported elicitation (mode=auto)")
            return None
        if outcome.action == "accept" and outcome.confirm:
            return None
        logger.warning("manage_policy blocked: user did not confirm (%s)", action)
        return f"用户未确认该 safety policy 变更（{action}: {line}）"

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
                choice=choice if choice in ("api", "api_session", "product", "none")
                else None)
        if action == "decline":
            return ElicitOutcome(action="decline")
        return ElicitOutcome(action="cancel")

    return elicit
