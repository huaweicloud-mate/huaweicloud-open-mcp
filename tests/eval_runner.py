"""金评集执行器：tests/fixtures/entity_eval.json → (passed, failed)。

测试门禁与 Phase 4 调参共用同一执行逻辑；标注语义见 fixture note
（top1 / top3 / family 三档，twin 归并后按行产品判定）。
"""

import json
from pathlib import Path


def run_eval(graph, eval_path: Path) -> tuple[int, list[tuple[str, str]]]:
    cases = json.loads(Path(eval_path).read_text(encoding="utf-8"))["cases"]
    passed = 0
    failed: list[tuple[str, str]] = []
    for case in cases:
        query, expect = case["query"], case["expect"]
        out = graph.search_apis(query, limit=8)
        tops = [r["product"] for r in out["products"][:3]]
        if "top1" in expect:
            ok = bool(tops) and tops[0] == expect["top1"]
        elif "top3" in expect:
            ok = any(t in expect["top3"] for t in tops)
        else:
            ok = any(t in expect["family"] for t in tops)
        if ok:
            passed += 1
        else:
            failed.append((query, f"expect={expect} got={tops}"))
    return passed, failed
