"""PolicyStore：safety policy 状态层 —— 内存与文件双向同步的唯一持有者。

职责（藏在实现内，调用者只面对小接口）：
- 文件 → 内存：每次取规则时 stat 探测（mtime/size/inode），变更才重读解析；
  文件被外部编辑即时生效，无需重启。
- 内存 → 文件：add/remove 先校验、原子落盘（tmp + os.replace）、再刷新内存，
  静止态恒满足 memory == file。
- 三档 scope + 一次性：permanent（文件真值源，跨重启）/ temporary（内存 overlay
  + TTL，到期自动剪枝）/ session（内存 overlay，缺省档——本次 code agent 会话，
  stdio 单进程下等价进程存活期）/ once（内存 overlay，用后即焚——authorize 首次
  放行即焚毁）。
  生效规则 = overlay（插入序）++ 文件规则，整体行序 first-match；overlay allow
  穿透文件具体 deny 与兜底 deny（与落盘插位语义一致），overlay deny 可临时收紧。
  overlay 仅内存态，重启即失；未配置路径（path=None）全档全拒（红线不变）。
- 插入不变量：新 allow 规则插在首个会遮蔽它的 deny 规则之前
  （典型形态即 `*=deny` 兜底行之前），保证行序 first-match 语义下新增规则真实生效。
- 运行时降级：运行期文件被写坏/短暂消失时沿用最近合法版本并记 WARNING；
  文件恢复合法后自动重新采纳。启动时急切加载，坏文件快速失败（与既有行为一致）。

依赖边界：仅依赖标准库与本包 policy.py 纯函数，保持 safety 最底层零外部依赖。
"""

import dataclasses
import fnmatch
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import policy as safety_policy

logger = logging.getLogger("safety.policy_store")

NOT_CONFIGURED_REASON = (
    "未配置 safety policy 文件，manage_policy 不可用"
    "（启动参数 --policy 或环境变量 HUAWEICLOUD_MCP_POLICY_FILE）")

SCOPES: tuple[str, ...] = ("permanent", "temporary", "session", "once")
DEFAULT_SCOPE = "session"
DEFAULT_TTL_SECONDS = 3600

StatFn = Callable[[str], Any]
TimeFn = Callable[[], float]


@dataclass(frozen=True)
class MutationResult:
    ok: bool
    reason: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class RuleInfo:
    """规则的结构化视图（评估序）：scope 标明所属档位。"""

    line: str
    scope: str
    expires_in: int | None = None


def _read_entries(path: str) -> tuple[list[str], bool]:
    """读取策略文件的原始行（含注释/空行）。返回 (entries, 是否为 JSON 数组格式)。"""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return list(data), True
    except json.JSONDecodeError:
        pass
    return raw.splitlines(), False


def _rule_key(rule: safety_policy.PolicyRule) -> tuple[Any, ...]:
    """规则的语义匹配键（与 evaluate 的大小写不敏感口径一致）。"""
    return (rule.kind, rule.connect_only,
            rule.product.lower(), rule.api_pattern.lower(), rule.allow)


def _semantic_positions(entries: list[str]) -> list[int]:
    """语义规则行的下标列表（跳过注释与空行）。"""
    out: list[int] = []
    for i, line in enumerate(entries):
        text = line.strip()
        if text and not text.startswith("#"):
            out.append(i)
    return out


def _probe_args(rule: safety_policy.PolicyRule) -> tuple[str, str]:
    """构造用于遮蔽探测的 (product, api) 字面量：由规则自身模式推导。"""
    product = rule.product if rule.product != "*" else "ZzProbeProduct"
    api = "".join(ch for ch in rule.api_pattern if ch not in "*?[")
    return product, api or "ZzProbeApi"


def _shadows(rule: safety_policy.PolicyRule,
             probe_product: str, probe_api: str) -> bool:
    """该规则是否命中探测字面量且结果为 deny（与 evaluate 的匹配口径一致）。"""
    if rule.kind != "product":
        return False
    if rule.product != "*" and rule.product.lower() != probe_product.lower():
        return False
    if not fnmatch.fnmatch(probe_api.lower(), rule.api_pattern.lower()):
        return False
    return not rule.allow


@dataclass(frozen=True)
class _OverlayEntry:
    """内存规则（session/temporary/once）：仅进程内存活，永不落盘。

    expire_at=None 为 session/once（无 TTL）；否则为 temporary 的到期时刻。
    once 由 rule.once 标记（authorize 首次放行即焚毁）。
    """

    line: str
    rule: safety_policy.PolicyRule
    expire_at: float | None = None


def _overlay_scope(entry: _OverlayEntry) -> str:
    """overlay 条目所属档位：once / temporary / session。"""
    if entry.rule.once:
        return "once"
    return "temporary" if entry.expire_at is not None else "session"


class PolicyStore:
    def __init__(self, path: str | None, *, stat_fn: StatFn | None = None,
                 time_fn: TimeFn | None = None):
        self.path = path
        self._lock = threading.RLock()   # 序列化读改写与热重载，杜绝并发丢失更新
        self._stat_fn: StatFn = stat_fn or os.stat
        self._time_fn: TimeFn = time_fn or time.monotonic
        self._entries: list[str] | None = None
        self._is_json = False
        self._rules: tuple[safety_policy.PolicyRule, ...] = ()
        self._stamp: Any = None
        self._missing = False
        self._overlay: list[_OverlayEntry] = []   # session/temporary 规则（评估时前置）
        if path is not None:
            entries, is_json = _read_entries(path)          # 缺文件 → FileNotFoundError 快速失败
            rules = tuple(safety_policy.parse_policy(entries))  # 非法规则 → ValueError 快速失败
            self._apply(entries, is_json, rules, self._stamp_of(path))

    # ---------- 内部状态维护 ----------

    def _stamp_of(self, path: str) -> Any:
        st = self._stat_fn(path)
        return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))

    def _apply(self, entries: list[str], is_json: bool,
               rules: tuple[safety_policy.PolicyRule, ...], stamp: Any) -> None:
        self._entries = entries
        self._is_json = is_json
        self._rules = rules
        self._stamp = stamp

    def _refresh(self) -> None:
        """热重载：探测文件变化；变化则重读解析，失败沿用最近合法版本。"""
        if self.path is None or self._entries is None:
            return
        try:
            stamp = self._stamp_of(self.path)
        except OSError:
            if not self._missing:
                logger.warning("safety policy 文件暂时不可读 %s，沿用最近合法版本", self.path)
                self._missing = True
            return
        self._missing = False
        if stamp == self._stamp:
            return
        try:
            entries, is_json = _read_entries(self.path)
            rules = tuple(safety_policy.parse_policy(entries))
        except Exception as exc:
            logger.warning("safety policy 文件变更后解析失败 %s：%s（沿用最近合法版本）",
                           self.path, exc)
            self._stamp = stamp  # 推进水位避免逐次报错刷屏；文件再次变更时自动重试
            return
        logger.info("safety policy 热重载完成 %s：%d 条规则", self.path, len(rules))
        self._apply(entries, is_json, rules, stamp)

    # ---------- 对外接口 ----------

    def rules(self) -> tuple[safety_policy.PolicyRule, ...]:
        """当前生效规则 = overlay（插入序）++ 文件规则，整体 first-match。

        触发热重载检查。未配置路径返回空元组（红线：未配置即全拒）。
        """
        with self._lock:
            self._refresh()
            self._prune_expired()
            overlay = tuple(entry.rule for entry in self._overlay)
            return overlay + self._rules

    def text(self) -> str:
        """当前生效策略全文（含注释，按当前磁盘格式渲染；仅文件规则）。
        未配置返回空串。"""
        with self._lock:
            self._refresh()
            if not self._entries:
                return ""
            return "\n".join(self._entries) + "\n"

    def add_rule(self, line: str, *, scope: str | None = None,
                 ttl_seconds: int | None = None) -> MutationResult:
        """新增一条规则，按 scope 分派：permanent 落盘，session/temporary 入内存 overlay。

        scope=None 取缺省档 session（默认解析在本层，调用方零知识透传）。
        temporary 按 ttl_seconds（缺省 3600）到期，取规则时惰性剪枝。
        幂等：同 scope 层内语义重复时直接成功且不改写状态。
        插入位置保证新规则真实生效：置于首个会遮蔽它的 deny 规则之前
        （overlay 内与文件内各自维护该不变量）。
        整个读-改-写临界区互斥：并发调用（MCP 工具并行派发）不丢更新。
        """
        scope = scope or DEFAULT_SCOPE
        if scope not in SCOPES:
            return MutationResult(
                ok=False, scope=scope,
                reason=f"未知 scope: {scope}（可选 permanent/temporary/session）")
        if ttl_seconds is not None and scope != "temporary":
            return MutationResult(
                ok=False, scope=scope, reason="ttl_seconds 仅支持 scope=temporary")
        if ttl_seconds is not None and ttl_seconds <= 0:
            return MutationResult(
                ok=False, scope=scope, reason="ttl_seconds 必须为正整数（秒）")
        if self.path is None or self._entries is None:
            return MutationResult(ok=False, scope=scope, reason=NOT_CONFIGURED_REASON)
        try:
            rule = safety_policy.parse_policy([line])[0]
        except ValueError as exc:
            return MutationResult(ok=False, scope=scope, reason=f"规则格式非法：{exc}")
        if scope == "permanent":
            return self._add_permanent(line, rule)
        return self._add_overlay(line, rule, scope, ttl_seconds)

    def _add_permanent(self, line: str,
                       rule: safety_policy.PolicyRule) -> MutationResult:
        with self._lock:
            self._refresh()
            assert self._entries is not None
            for existing in self._rules:
                if _rule_key(existing) == _rule_key(rule):
                    return MutationResult(ok=True, scope="permanent", reason="规则已存在")
            pos = self._insert_position(self._entries, rule)
            new_entries = list(self._entries)
            new_entries.insert(pos, line.strip())
            error = self._persist(new_entries)
            if error is not None:
                return MutationResult(ok=False, scope="permanent", reason=error)
            logger.info("policy add_rule %s scope=permanent -> ok", line.strip())
            return MutationResult(ok=True, scope="permanent")

    def _add_overlay(self, line: str, rule: safety_policy.PolicyRule,
                     scope: str, ttl_seconds: int | None) -> MutationResult:
        with self._lock:
            self._prune_expired()
            for entry in self._overlay:
                if _rule_key(entry.rule) == _rule_key(rule):
                    return MutationResult(ok=True, scope=scope, reason="规则已存在")
            expire_at = ((self._time_fn() + (ttl_seconds if ttl_seconds is not None
                                             else DEFAULT_TTL_SECONDS))
                         if scope == "temporary" else None)
            if scope == "once":
                rule = dataclasses.replace(rule, once=True)
            pos = self._overlay_insert_position(rule)
            self._overlay.insert(pos, _OverlayEntry(line=line.strip(), rule=rule,
                                                    expire_at=expire_at))
            logger.info("policy add_rule %s scope=%s -> ok", line.strip(), scope)
            return MutationResult(ok=True, scope=scope)

    def _prune_expired(self) -> None:
        """惰性剪枝：剔除到期的 temporary 规则（取规则/新增前调用）。"""
        now = self._time_fn()
        kept = [e for e in self._overlay
                if e.expire_at is None or e.expire_at > now]
        if len(kept) != len(self._overlay):
            logger.info("policy overlay 临时规则到期剪枝：%d 条",
                        len(self._overlay) - len(kept))
            self._overlay = kept

    def _overlay_insert_position(self, rule: safety_policy.PolicyRule) -> int:
        """overlay 内插入点：首个「对探测字面量判 false」的 overlay 规则之前；无则末尾。"""
        probe_product, probe_api = _probe_args(rule)
        for i, entry in enumerate(self._overlay):
            if _shadows(entry.rule, probe_product, probe_api):
                return i
        return len(self._overlay)

    def remove_rule(self, line: str) -> MutationResult:
        """删除首个语义匹配的规则：先内存 overlay 后策略文件；scope 回报删除层。

        与 add_rule 共用同一把锁：并发增删互不覆盖。找不到匹配返回失败且状态不动。
        """
        if self.path is None or self._entries is None:
            return MutationResult(ok=False, reason=NOT_CONFIGURED_REASON)
        try:
            rule = safety_policy.parse_policy([line])[0]
        except ValueError as exc:
            return MutationResult(ok=False, reason=f"规则格式非法：{exc}")
        with self._lock:
            self._refresh()
            self._prune_expired()
            target = _rule_key(rule)
            for i, entry in enumerate(self._overlay):
                if _rule_key(entry.rule) == target:
                    scope = _overlay_scope(entry)
                    del self._overlay[i]
                    logger.info("policy remove_rule %s scope=%s -> ok", line.strip(), scope)
                    return MutationResult(ok=True, scope=scope)
            assert self._entries is not None
            for i in _semantic_positions(self._entries):
                parsed = safety_policy.parse_policy([self._entries[i]])
                if parsed and _rule_key(parsed[0]) == target:
                    new_entries = list(self._entries)
                    del new_entries[i]
                    error = self._persist(new_entries)
                    if error is not None:
                        return MutationResult(ok=False, reason=error)
                    logger.info("policy remove_rule %s scope=permanent -> ok", line.strip())
                    return MutationResult(ok=True, scope="permanent")
            return MutationResult(ok=False, reason="未找到匹配的规则")

    def list_rules(self) -> list[RuleInfo]:
        """规则的结构化视图（评估序）：overlay（session/temporary）前置，文件规则随后。

        temporary 的 expires_in 为剩余秒；其余 None。未配置路径返回空列表。
        """
        with self._lock:
            self._refresh()
            self._prune_expired()
            now = self._time_fn()
            infos: list[RuleInfo] = []
            for entry in self._overlay:
                expires_in = (max(0, int(entry.expire_at - now))
                              if entry.expire_at is not None else None)
                infos.append(RuleInfo(line=entry.line, scope=_overlay_scope(entry),
                                      expires_in=expires_in))
            if self._entries:
                for i in _semantic_positions(self._entries):
                    infos.append(RuleInfo(line=self._entries[i].strip(),
                                          scope="permanent"))
            return infos

    # ---------- 原子授权门（dispatch 前） ----------

    def authorize(self, product: str, api: str) -> str | None:
        """dispatch 前的原子授权门：评估与 once 焚毁在同一临界区内完成。

        interface 契约与 check 同构：None=放行（allow 与 allow_once 对调用方
        不可区分，焚毁仅记 store 内部 INFO）；str=拒绝原因（复用 check 文案）。
        调用顺序约束：早检 check 先行（廉价拒绝，元数据拉取之前）；本方法须在
        每次 dispatch 尝试前恰好调用一次——check 与 authorize 之间 once 规则
        被并发消费时，本方法落 deny（并发下恰一个请求放行）。
        deny 路径永不焚毁；未配置路径恒 deny（红线）。
        """
        if self.path is None:
            return safety_policy.check(None, product, api)
        with self._lock:
            self._refresh()
            self._prune_expired()
            rules = tuple(entry.rule for entry in self._overlay) + self._rules
            hit = safety_policy.match_first(rules, product, api)
            if hit is None or not hit.allow:
                return safety_policy.check(rules, product, api)
            if hit.once:
                self._overlay = [e for e in self._overlay if e.rule is not hit]
                logger.info("policy authorize %s:%s once 规则已消费", product, api)
            return None

    def authorize_server(self, server: str, tool: str | None = None) -> str | None:
        """authorize 的 server 规则版（discover call_tool / connect 前调用）。

        契约同 authorize：None=放行；str=拒绝原因（复用 check_server 文案）。
        connect（tool=None）与调用级（tool 非空）once 规则均首次放行即焚毁。
        """
        if self.path is None:
            return safety_policy.check_server(None, server, tool)
        with self._lock:
            self._refresh()
            self._prune_expired()
            rules = tuple(entry.rule for entry in self._overlay) + self._rules
            hit = safety_policy.match_server_first(rules, server, tool)
            if hit is None or not hit.allow:
                return safety_policy.check_server(rules, server, tool)
            if hit.once:
                self._overlay = [e for e in self._overlay if e.rule is not hit]
                logger.info("policy authorize_server %s:%s once 规则已消费",
                            server, tool or "-")
            return None

    # ---------- 持久化 ----------

    def _insert_position(self, entries: list[str], rule: safety_policy.PolicyRule) -> int:
        """插入点：首个「对探测字面量判 false」的语义规则之前；无则末尾。

        保证插入后 evaluate(新规则集, rule 自身) == rule.allow（行序 first-match 不变量）。
        """
        probe_product, probe_api = _probe_args(rule)
        for i in _semantic_positions(entries):
            parsed = safety_policy.parse_policy([entries[i]])
            if parsed and _shadows(parsed[0], probe_product, probe_api):
                return i
        return len(entries)

    def _persist(self, new_entries: list[str]) -> str | None:
        """原子落盘并刷新内存缓存。成功返回 None，失败返回错误描述（文件与缓存均不动）。"""
        path = self.path
        if path is None:
            return NOT_CONFIGURED_REASON
        if self._is_json:
            content = json.dumps(new_entries, ensure_ascii=False, indent=2) + "\n"
        else:
            content = ("\n".join(new_entries) + "\n") if new_entries else ""
        directory = os.path.dirname(path) or "."
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    dir=directory, mode="w", encoding="utf-8",
                    delete=False, suffix=".policy.tmp") as f:
                f.write(content)
                tmp_path = f.name
            os.replace(tmp_path, path)           # 同目录原子换名，无撕裂读
        except OSError as exc:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return f"写入 policy 文件失败：{exc}"
        try:
            rules = tuple(safety_policy.parse_policy(new_entries))
        except ValueError as exc:                  # 理论不可达：入口已校验，防御性回退
            return f"写入后校验失败：{exc}"
        self._apply(new_entries, self._is_json, rules, self._stamp_of(path))
        return None
