"""PolicyConsent：safety policy 变更的 MCP elicitation 交互语义（深模块）。

Interface（调用者需知的全部）：
- ``PolicyConsent(mode, elicit, grant)`` + ``offer_grant`` / ``gate_change`` 两方法；
- ``ElicitFn`` adapter 契约：发送 elicitation 并归一化为 ``ElicitOutcome``，
  返回 None 表示客户端不支持 elicitation（capability 缺失 / 请求失败）；
- ``parse_elicit_mode`` 解析 --elicitation / HUAWEICLOUD_MCP_ELICIT。

mode 语义（implementation 内聚，不外泄）：
- off      从不 elicit：offer_grant 原样返回 denial，gate_change 恒放行；
- auto     尝试 elicit；客户端不支持 → 降级放行（WARNING，沿用 prompt 约定兜底）；
- required 客户端不支持 → gate_change 返回可操作 reason（fail-closed），
  offer_grant 保持原 denial（拒绝路径本就无操作可做）。

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


class PolicyChangeConfirm(BaseModel):
    """elicitation 表单 schema：仅 primitive 字段（MCP spec 限制）。"""

    confirm: bool


@dataclass(frozen=True)
class DenialOffer:
    """一次可授予的 policy 拒绝：主体 / 最小规则文本 / 原始拒绝 reason。"""

    subject: str
    rule: str
    reason: str


@dataclass(frozen=True)
class ElicitOutcome:
    """归一化 elicitation 结果；confirm 仅在 action="accept" 时有意义。"""

    action: Literal["accept", "decline", "cancel"]
    confirm: bool | None = None


ElicitFn: TypeAlias = Callable[[str, type[BaseModel]], Awaitable[ElicitOutcome | None]]
GrantFn: TypeAlias = Callable[[str], dict[str, Any]]


class ElicitContext(Protocol):
    """duck-typed MCP Context：真实 Context 的 ``elicit`` 满足本协议。"""

    async def elicit(self, message: str, schema: type[BaseModel]) -> Any: ...


def parse_elicit_mode(raw: str | None) -> ElicitMode:
    """解析 elicitation 模式；空/非法值宽容回退 auto。"""
    text = (raw or "").strip().lower()
    if text in ELICIT_MODES:
        return text
    return "auto"


def change_message(action: str, line: str) -> str:
    """manage_policy add/remove 确认弹窗文案。"""
    if action == "add":
        return (f"确认新增 safety policy 规则 '{line}'？"
                "规则写入策略文件并热生效，临时授权建议用完即回收（remove）。")
    if action == "remove":
        return f"确认移除 safety policy 规则 '{line}'？"
    return f"确认对 safety policy 执行 {action}：'{line}'？"


def denial_message(offer: DenialOffer) -> str:
    """拒绝路径提议授予弹窗文案。"""
    return (f"{offer.reason}\n\n"
            f"是否授予最小规则 '{offer.rule}' 放行 {offer.subject}？"
            "规则将写入策略文件并热生效。")


class PolicyConsent:
    """safety policy 变更的确认机制：何时问、怎么问、如何解释回答。

    offer_grant 用于拒绝路径（accept → 自动授予最小规则并增强 denial）；
    gate_change 用于 manage_policy add/remove（check 习语：None=放行）。
    """

    def __init__(self, mode: str, elicit: ElicitFn,
                 grant: GrantFn | None = None):
        self.mode = parse_elicit_mode(mode)
        self._elicit = elicit
        self._grant = grant

    async def offer_grant(self, offer: DenialOffer,
                          denial: Mapping[str, Any]) -> Mapping[str, Any]:
        """对 policy 拒绝提议授予；返回原 denial 或增强后的 denial（防御非 dict 输入）。

        增强 result 为 dict（原 denial 键集 + granted_rule/reason 增强）；
        其余路径原样返回入参对象。
        """
        if self.mode == "off":
            return denial
        if not (isinstance(denial, dict) and denial.get("ok") is False):
            return denial
        outcome = await self._ask(denial_message(offer))
        if outcome is None:
            logger.warning("grant offer skipped: client unsupported elicitation (mode=%s)",
                           self.mode)
            return denial
        if outcome.action != "accept" or not outcome.confirm:
            logger.warning("grant offer declined: %s (action=%s)", offer.rule, outcome.action)
            return denial
        grant = self._grant
        if grant is None:
            logger.warning("grant offer accepted but no grant fn wired: %s", offer.rule)
            return denial
        result = grant(offer.rule)
        if result.get("ok"):
            logger.info("grant accepted: %s", offer.rule)
            return {**denial, "granted_rule": offer.rule,
                    "reason": (f"{denial.get('reason', '')}"
                               f"；用户已通过确认授予规则 {offer.rule}（热生效），请重新调用")}
        reason = result.get("reason") or "未知原因"
        logger.warning("grant failed: %s: %s", offer.rule, reason)
        return {**denial,
                "reason": f"{denial.get('reason', '')}；自动授予 {offer.rule} 失败: {reason}"}

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

    async def _ask(self, message: str) -> ElicitOutcome | None:
        """发送 elicitation；adapter 归一化结果（None=客户端不支持）。"""
        return await self._elicit(message, PolicyChangeConfirm)


def ctx_elicit_fn(ctx: ElicitContext) -> ElicitFn:
    """MCP Context → ElicitFn adapter：归一化 SDK 结果，失败归一为 None（不支持）。"""

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
            return ElicitOutcome(action="accept",
                                 confirm=bool(confirm) if confirm is not None else None)
        if action == "decline":
            return ElicitOutcome(action="decline")
        return ElicitOutcome(action="cancel")

    return elicit
