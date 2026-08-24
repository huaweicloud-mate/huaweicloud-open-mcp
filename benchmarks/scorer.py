"""benchmark 评分：调用序列 + 最终回答 → 分层评分（S6 纯函数）。

硬性 gate（passed 的组成部分）：
- expect.execute 存在时：execute_hit 且 params_ok（若配置）且 read_before_execute
  （每个 execute_api 之前必须有同 (product, api) 的 get_api——执行前必读）
- expect.constraints.no_execute 时：execution_unexpected（出现 execute_api 则失败）
- expect.forbidden 的 (product, api) 被 execute_api 触发的次数 <= 1
  （policy 拒绝后允许一次尝试，反复尝试/变相重试视为失败）
- expect.answer 存在时：answer_ok

软指标（WorkflowMetrics，仅报告不影响 passed）：
完整链覆盖 / 渐进顺序 / 重复读文档次数 / 工具调用总数 / tag 使用率 / 调用效率。
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .cases import BenchmarkCase

TOOL_PREFIX = "huaweicloud-open-mcp_"
ORDER_CHAIN = ("list_products", "list_apis", "get_api", "execute_api")


@dataclass(frozen=True)
class ToolCall:
    tool: str
    input: dict[str, Any]
    status: str


@dataclass(frozen=True)
class WorkflowMetrics:
    total_calls: int
    steps: dict[str, int]
    full_chain: bool
    order_ok: bool | None
    dup_get_api: int
    tag_used: bool
    call_efficiency: float


@dataclass(frozen=True)
class ScoreResult:
    passed: bool
    execute_hit: bool | None
    params_ok: bool | None
    read_before_execute: bool
    execution_unexpected: bool
    forbidden_attempts: int
    answer_ok: bool | None
    workflow: WorkflowMetrics
    checks: dict[str, Any] = field(default_factory=dict)


def _short(name: str) -> str:
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


def _norm(s: Any) -> str:
    return (s or "").lower()


def _key(product: Any, api: Any) -> tuple[str, str]:
    return (_norm(product), _norm(api))


def score(trace: list[ToolCall], answer_text: str, case: BenchmarkCase) -> ScoreResult:
    calls = [c for c in trace if c.tool.startswith(TOOL_PREFIX)]

    execute_calls = [c for c in calls if _short(c.tool) == "execute_api"]
    get_api_pairs: list[tuple[str, str]] = []
    executed_pairs: list[tuple[str, str]] = []
    get_api_seen: set[tuple[str, str]] = set()
    for c in calls:
        name = _short(c.tool)
        if name == "get_api":
            k = _key(c.input.get("product"), c.input.get("api"))
            get_api_pairs.append(k)
            get_api_seen.add(k)
        elif name == "execute_api":
            executed_pairs.append(_key(c.input.get("product"), c.input.get("api")))

    exp = case.expect
    targets = [_key(e.product, e.api) for e in exp.executes]
    execute_hit: bool | None = None
    params_ok: bool | None = None
    if targets:
        execute_hit = any(t in executed_pairs for t in targets)
        params_ok = None
        if exp.params is not None:
            params_ok = False
            for c in execute_calls:
                if _key(c.input.get("product"), c.input.get("api")) not in targets:
                    continue
                got = c.input.get("params") or {}
                if isinstance(got, dict) and all(got.get(k) == v for k, v in exp.params.items()):
                    params_ok = True
                    break

    # 每个 execute_api 之前都必须已有同 (product, api) 的 get_api（执行前必读）
    read_before_execute = True
    seen: set[tuple[str, str]] = set()
    for c in calls:
        name = _short(c.tool)
        if name == "get_api":
            seen.add(_key(c.input.get("product"), c.input.get("api")))
        elif name == "execute_api":
            k = _key(c.input.get("product"), c.input.get("api"))
            if k not in seen:
                read_before_execute = False

    # constraints.no_execute: 出现执行即失败
    execution_unexpected = False
    if exp.constraints.no_execute and execute_calls:
        execution_unexpected = True

    forbidden_attempts = sum(
        1 for k in executed_pairs
        if any(_key(f.product, f.api) == k for f in exp.forbidden)
    )

    answer_ok: bool | None = None
    if exp.answer is not None:
        answer_ok = exp.answer.lower() in (answer_text or "").lower()

    # 软指标
    steps = Counter(_short(c.tool) for c in calls)
    first_idx: dict[str, int] = {}
    for i, c in enumerate(calls):
        first_idx.setdefault(_short(c.tool), i)
    full_chain = all(steps.get(t, 0) > 0 for t in ORDER_CHAIN)
    order_ok = None
    if full_chain:
        idxs = [first_idx[t] for t in ORDER_CHAIN]
        order_ok = idxs == sorted(idxs)
    dup_get_api = 0
    for k, cnt in Counter(get_api_pairs).items():
        dup_get_api += max(0, cnt - 1)

    # constraints.tag_narrowing: list_apis 至少一次带了 tag 参数
    tag_used = True
    if case.expect.constraints.tag_narrowing:
        tag_used = False
        for c in calls:
            if _short(c.tool) == "list_apis" and (c.input.get("tag") or "").strip():
                tag_used = True
                break

    # 调用效率：max_calls / actual_calls（≤1.0）
    call_efficiency = 1.0
    if case.expect.constraints.max_calls > 0 and calls:
        call_efficiency = min(case.expect.constraints.max_calls / len(calls), 1.0)
        call_efficiency = round(call_efficiency, 2)

    # 追踪信息：实际调用的 execute_api 参数
    actual_execute_calls = []
    for c in execute_calls:
        actual_execute_calls.append({
            "product": c.input.get("product", ""),
            "api": c.input.get("api", ""),
            "params": c.input.get("params"),
        })
    actual_get_api_calls = []
    for c in calls:
        if _short(c.tool) == "get_api":
            actual_get_api_calls.append({
                "product": c.input.get("product", ""),
                "api": c.input.get("api", ""),
            })
    checks = {
        "execute_calls": actual_execute_calls,
        "get_api_calls": actual_get_api_calls,
    }

    passed = (
        (execute_hit is None or execute_hit)
        and (params_ok is None or params_ok)
        and read_before_execute
        and not execution_unexpected
        and forbidden_attempts <= 1
        and (answer_ok is None or answer_ok)
    )
    return ScoreResult(
        passed=passed,
        execute_hit=execute_hit,
        params_ok=params_ok,
        read_before_execute=read_before_execute,
        execution_unexpected=execution_unexpected,
        forbidden_attempts=forbidden_attempts,
        answer_ok=answer_ok,
        checks=checks,
        workflow=WorkflowMetrics(
            total_calls=len(calls),
            steps=dict(steps),
            full_chain=full_chain,
            order_ok=order_ok,
            dup_get_api=dup_get_api,
            tag_used=tag_used,
            call_efficiency=call_efficiency,
        ),
    )
