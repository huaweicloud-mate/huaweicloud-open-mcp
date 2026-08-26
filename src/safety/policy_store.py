"""PolicyStore：safety policy 状态层 —— 内存与文件双向同步的唯一持有者。

职责（藏在实现内，调用者只面对小接口）：
- 文件 → 内存：每次取规则时 stat 探测（mtime/size/inode），变更才重读解析；
  文件被外部编辑即时生效，无需重启。
- 内存 → 文件：add/remove 先校验、原子落盘（tmp + os.replace）、再刷新内存，
  静止态恒满足 memory == file。
- 插入不变量：新 allow 规则插在首个会遮蔽它的 deny 规则之前
  （典型形态即 `*=deny` 兜底行之前），保证行序 first-match 语义下新增规则真实生效。
- 运行时降级：运行期文件被写坏/短暂消失时沿用最近合法版本并记 WARNING；
  文件恢复合法后自动重新采纳。启动时急切加载，坏文件快速失败（与既有行为一致）。
- 未配置路径（path=None）：store 只读不可变，「未配置即全拒」红线不变。

依赖边界：仅依赖标准库与本包 policy.py 纯函数，保持 safety 最底层零外部依赖。
"""

import fnmatch
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

from . import policy as safety_policy

logger = logging.getLogger("safety.policy_store")

NOT_CONFIGURED_REASON = (
    "未配置 safety policy 文件，manage_policy 不可用"
    "（启动参数 --policy 或环境变量 HUAWEICLOUD_MCP_POLICY_FILE）")

StatFn = Callable[[str], Any]


@dataclass(frozen=True)
class MutationResult:
    ok: bool
    reason: str | None = None


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


class PolicyStore:
    def __init__(self, path: str | None, *, stat_fn: StatFn | None = None):
        self.path = path
        self._stat_fn: StatFn = stat_fn or os.stat
        self._entries: list[str] | None = None
        self._is_json = False
        self._rules: tuple[safety_policy.PolicyRule, ...] = ()
        self._stamp: Any = None
        self._missing = False
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
        """当前生效规则（触发热重载检查）。未配置路径返回空元组。"""
        self._refresh()
        return self._rules

    def text(self) -> str:
        """当前生效策略全文（含注释，按当前磁盘格式渲染）。未配置返回空串。"""
        self._refresh()
        if not self._entries:
            return ""
        return "\n".join(self._entries) + "\n"

    def add_rule(self, line: str) -> MutationResult:
        """新增一条规则并持久化。幂等：语义重复时直接成功且不改写文件。

        插入位置保证新规则真实生效：置于首个会遮蔽它的 deny 规则之前。
        """
        if self.path is None or self._entries is None:
            return MutationResult(ok=False, reason=NOT_CONFIGURED_REASON)
        try:
            rule = safety_policy.parse_policy([line])[0]
        except ValueError as exc:
            return MutationResult(ok=False, reason=f"规则格式非法：{exc}")
        self._refresh()
        assert self._entries is not None
        for existing in self._rules:
            if _rule_key(existing) == _rule_key(rule):
                return MutationResult(ok=True, reason="规则已存在")
        pos = self._insert_position(self._entries, rule)
        new_entries = list(self._entries)
        new_entries.insert(pos, line.strip())
        error = self._persist(new_entries)
        if error is not None:
            return MutationResult(ok=False, reason=error)
        logger.info("policy add_rule %s -> ok", line.strip())
        return MutationResult(ok=True)

    def remove_rule(self, line: str) -> MutationResult:
        """删除首个语义匹配的规则并持久化；找不到匹配返回失败且文件不动。"""
        if self.path is None or self._entries is None:
            return MutationResult(ok=False, reason=NOT_CONFIGURED_REASON)
        try:
            rule = safety_policy.parse_policy([line])[0]
        except ValueError as exc:
            return MutationResult(ok=False, reason=f"规则格式非法：{exc}")
        self._refresh()
        assert self._entries is not None
        target = _rule_key(rule)
        for i in _semantic_positions(self._entries):
            parsed = safety_policy.parse_policy([self._entries[i]])
            if parsed and _rule_key(parsed[0]) == target:
                new_entries = list(self._entries)
                del new_entries[i]
                error = self._persist(new_entries)
                if error is not None:
                    return MutationResult(ok=False, reason=error)
                logger.info("policy remove_rule %s -> ok", line.strip())
                return MutationResult(ok=True)
        return MutationResult(ok=False, reason="未找到匹配的规则")

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
