"""benchmark 结果聚合统计、Markdown 报告与基线对比（S6 纯函数）。"""

import json
from dataclasses import dataclass
from typing import Any, cast

from tabulate import tabulate

from .scorer import ScoreResult


@dataclass(frozen=True)
class RunResult:
    case_id: str
    backend: str
    repeat: int
    session_id: str | None
    model: str
    elapsed_s: float | None
    tokens: dict[str, int | float | None]
    cost: float | None
    score: ScoreResult | None
    error: str | None


@dataclass(frozen=True)
class CaseStats:
    case_id: str
    n: int
    passed: int
    error_count: int
    pass_rate: float
    time_min: float | None
    time_max: float | None
    time_mean: float | None
    time_p50: float | None
    time_p95: float | None
    tokens_in_mean: int | None
    tokens_out_mean: int | None
    tokens_cache_read_mean: int | None
    cost_sum: float | None
    tool_calls_mean: float | None
    full_chain_rate: float | None
    order_ok_rate: float | None
    dup_get_api_mean: float | None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in self.__dataclass_fields__:
            d[f] = getattr(self, f)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseStats":
        fields = cls.__dataclass_fields__
        return cls(**{k: data[k] for k in fields if k in data})


def _percentile(values: list[float], p: float) -> float:
    """nearest-rank 百分位（样本数少时 p95 趋近最大值）。"""
    import math

    s = sorted(values)
    idx = max(0, math.ceil(p / 100.0 * len(s)) - 1)
    return s[idx]


def aggregate(results: list[RunResult]) -> list[CaseStats]:
    by_case: dict[str, list[RunResult]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)
    out: list[CaseStats] = []
    for case_id in sorted(by_case):
        rs = by_case[case_id]
        times = [r.elapsed_s for r in rs if r.elapsed_s is not None]
        errors = sum(1 for r in rs if r.error)
        scores = [r.score for r in rs if r.score is not None]
        token_in = [v for r in rs if (v := r.tokens.get("input")) is not None]
        token_out = [v for r in rs if (v := r.tokens.get("output")) is not None]
        cache_read = [v for r in rs if (v := r.tokens.get("cache_read")) is not None]
        costs = [r.cost for r in rs if r.cost is not None]
        calls = [s.workflow.total_calls for s in scores]
        chains = [s.workflow.full_chain for s in scores]
        orders = [s.workflow.order_ok is True for s in scores]
        dups = [s.workflow.dup_get_api for s in scores]
        passed_cnt = sum(1 for s in scores if s.passed)
        out.append(CaseStats(
            case_id=case_id,
            n=len(rs),
            passed=passed_cnt,
            error_count=errors,
            pass_rate=(passed_cnt / len(rs)) if rs else 0.0,
            time_min=min(times) if times else None,
            time_max=max(times) if times else None,
            time_mean=sum(times) / len(times) if times else None,
            time_p50=_percentile(times, 50) if times else None,
            time_p95=_percentile(times, 95) if times else None,
            tokens_in_mean=round(sum(token_in) / len(token_in)) if token_in else None,
            tokens_out_mean=round(sum(token_out) / len(token_out)) if token_out else None,
            tokens_cache_read_mean=round(sum(cache_read) / len(cache_read)) if cache_read else None,
            cost_sum=sum(costs) if costs else None,
            tool_calls_mean=sum(calls) / len(calls) if calls else None,
            full_chain_rate=sum(chains) / len(chains) if chains else None,
            order_ok_rate=sum(orders) / len(orders) if orders else None,
            dup_get_api_mean=sum(dups) / len(dups) if dups else None,
        ))
    return out


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:.0f}%" if v is not None else "-"


def _fmt(v: Any, nd: int = 1) -> str:
    if v is None:
        return "-"
    return f"{v:.{nd}f}"


def render_markdown(stats: list[CaseStats], *, baseline: dict[str, Any] | None,
                    backend: str, model: str) -> str:
    rows = [[
        s.case_id, f"{s.passed}/{s.n}", _fmt_pct(s.pass_rate), s.error_count,
        _fmt(s.time_mean), _fmt(s.time_p50), _fmt(s.time_p95),
        _fmt(s.tokens_in_mean, 0), _fmt(s.tokens_out_mean, 0), _fmt(s.tokens_cache_read_mean, 0),
        _fmt(s.cost_sum, 4), _fmt(s.tool_calls_mean), _fmt_pct(s.full_chain_rate),
        _fmt_pct(s.order_ok_rate), _fmt(s.dup_get_api_mean),
    ] for s in stats]
    table = tabulate(
        rows,
        headers=["case", "pass", "pass率", "错误", "耗时均值(s)", "p50", "p95",
                 "token入", "token出", "cache读", "成本", "工具调用", "全链率", "顺序率", "重复get_api"],
        tablefmt="github",
    )
    out = [f"# benchmark 结果（backend={backend} model={model}）", "", table, ""]
    if baseline is not None:
        rows_b = [[r["case_id"], r["metric"], _fmt(r["base"]), _fmt(r["cur"]),
                   f"{r['delta_pct']:+.1f}%"] for r in compare_baseline(stats, baseline)]
        out.append("## 与基线对比（delta = 当前 - 基线）")
        out.append("")
        out.append(tabulate(rows_b, headers=["case", "指标", "基线", "当前", "delta%"],
                            tablefmt="github"))
        out.append("")
    return "\n".join(out)


BASELINE_METRICS = ("pass_rate", "time_p50", "tokens_in_mean")


def compare_baseline(current: list[CaseStats], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    base_cases: dict[str, CaseStats] = {}
    for case_id, data in (baseline.get("cases") or {}).items():
        base_cases[case_id] = CaseStats.from_dict(data)
    rows: list[dict[str, Any]] = []
    for s in current:
        b = base_cases.get(s.case_id)
        if b is None:
            continue
        for metric in BASELINE_METRICS:
            bv, cv = getattr(b, metric), getattr(s, metric)
            if bv is None or cv is None:
                continue
            delta = (cv - bv) / bv * 100.0 if bv else 0.0
            rows.append({"case_id": s.case_id, "metric": metric,
                         "base": bv, "cur": cv, "delta_pct": delta})
    return rows


def dump_baseline(stats: list[CaseStats], backend: str, model: str) -> dict[str, Any]:
    return {
        "meta": {"backend": backend, "model": model, "created_at": ""},
        "cases": {s.case_id: s.to_dict() for s in stats},
    }


def load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return cast(dict[str, Any], json.load(f))
