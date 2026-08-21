"""S6：benchmark report 统计与基线对比纯函数单测。"""

import pytest

from benchmarks.report import (
    CaseStats,
    RunResult,
    aggregate,
    compare_baseline,
    render_markdown,
)
from benchmarks.scorer import ScoreResult, WorkflowMetrics


def run(case_id, repeat, elapsed, passed, *, error=None, tokens=None, cost=0.0,
        full_chain=True, order_ok=True):
    score = None if error else ScoreResult(
        passed=passed,
        execute_hit=passed,
        params_ok=None,
        read_before_execute=passed,
        execution_unexpected=False,
        forbidden_attempts=0,
        answer_ok=None,
        workflow=WorkflowMetrics(total_calls=4, steps={"get_api": 1, "execute_api": 1},
                                 full_chain=full_chain, order_ok=order_ok, dup_get_api=0,
                                 tag_used=True, call_efficiency=1.0),
    )
    return RunResult(
        case_id=case_id, backend="stub", repeat=repeat,
        session_id=f"ses_{repeat}", model="maas/glm-5.2",
        elapsed_s=elapsed,
        tokens=tokens or {"input": 100, "output": 10, "reasoning": 5,
                          "cache_read": 50, "cache_write": 0},
        cost=cost, score=score, error=error,
    )


def test_aggregate_stats():
    results = [
        run("a", 0, 1.0, True),
        run("a", 1, 2.0, False, full_chain=False, order_ok=None),
        run("a", 2, 3.0, True),
    ]
    stats = aggregate(results)
    assert len(stats) == 1
    s = stats[0]
    assert s.case_id == "a"
    assert s.n == 3
    assert s.passed == 2
    assert s.pass_rate == pytest.approx(2 / 3)
    assert s.time_mean == pytest.approx(2.0)
    assert s.time_p50 == pytest.approx(2.0)
    assert s.time_p95 == pytest.approx(3.0)
    assert s.time_min == pytest.approx(1.0)
    assert s.time_max == pytest.approx(3.0)
    assert s.tokens_in_mean == 100
    assert s.cost_sum == pytest.approx(0.0)
    assert s.full_chain_rate == pytest.approx(2 / 3)
    assert s.order_ok_rate == pytest.approx(2 / 3)


def test_aggregate_ignores_error_elapsed():
    results = [
        run("a", 0, 1.0, True),
        run("a", 1, None, False, error="timeout"),
    ]
    s = aggregate(results)[0]
    assert s.n == 2
    assert s.time_mean == pytest.approx(1.0)
    assert s.error_count == 1


def test_aggregate_groups_by_case():
    results = [run("a", 0, 1.0, True), run("b", 0, 2.0, True)]
    stats = {s.case_id: s for s in aggregate(results)}
    assert set(stats) == {"a", "b"}
    assert stats["a"].time_p50 == pytest.approx(1.0)


def test_render_markdown_contains_summary():
    stats = aggregate([run("a", 0, 1.0, True)])
    md = render_markdown(stats, baseline=None, backend="stub", model="maas/glm-5.2")
    assert "stub" in md
    assert "maas/glm-5.2" in md
    assert "| a " in md or "| a |" in md
    assert "100.0%" in md or "1.000" in md or "100%" in md


def _baseline_of(stats):
    return {
        "meta": {"backend": "stub", "model": "maas/glm-5.2", "created_at": "2026-08-18T00:00:00"},
        "cases": {s.case_id: s.to_dict() for s in stats},
    }


def test_compare_baseline_deltas():
    cur = aggregate([run("a", 0, 2.0, True)])
    base_runs = [run("a", 0, 1.0, True)]
    base = _baseline_of(aggregate(base_runs))
    rows = compare_baseline(cur, base)
    by_metric = {(r["case_id"], r["metric"]): r for r in rows}
    t = by_metric[("a", "time_p50")]
    assert t["base"] == pytest.approx(1.0)
    assert t["cur"] == pytest.approx(2.0)
    assert t["delta_pct"] == pytest.approx(100.0)
    p = by_metric[("a", "pass_rate")]
    assert p["base"] == pytest.approx(1.0)
    assert p["cur"] == pytest.approx(1.0)
    assert p["delta_pct"] == pytest.approx(0.0)


def test_compare_baseline_skips_missing_cases():
    cur = aggregate([run("a", 0, 1.0, True), run("b", 0, 1.0, True)])
    base = _baseline_of(aggregate([run("a", 0, 1.0, True)]))
    rows = compare_baseline(cur, base)
    assert {r["case_id"] for r in rows} == {"a"}


def test_baseline_json_roundtrip():
    stats = aggregate([run("a", 0, 1.0, True)])
    base = _baseline_of(stats)
    loaded = CaseStats.from_dict(base["cases"]["a"])
    assert loaded.case_id == "a"
    assert loaded.time_p50 == pytest.approx(1.0)
