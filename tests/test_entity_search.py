"""S16a：实体检索引擎 TantivyEngine（BM25 + 手动 CJK 2-gram token 化）。

外部接缝：TantivyEngine.build(apis, boosts) → search(terms, limit) → DocHit。
断言独立真值：行为锚定（排序/归一化界/provenance/驼峰与 CJK 双轨），
不复算 BM25 内部公式。tantivy 为 in-process 依赖，测试用真库不 mock。
"""

import json
from pathlib import Path

import pytest

from mcp_openapi.entity_search import (
    DocHit,
    FieldBoosts,
    IndexedApi,
    TantivyEngine,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_INDEX = REPO_ROOT / "configs" / "entity-index.json"


def _apis():
    return [
        IndexedApi(product="ECS", name="NovaRebootServer", method="post",
                   summary="重启弹性云服务器", tags="实例管理",
                   keywords=("重启服务器", "开机")),
        IndexedApi(product="OBS", name="PutObject", method="put",
                   summary="上传对象文件", tags="对象操作",
                   keywords=("上传文件", "put 对象")),
        IndexedApi(product="CCE", name="CreateCluster", method="post",
                   summary="创建 k8s 集群", tags="集群管理",
                   keywords=("k8s集群", "创建集群")),
        IndexedApi(product="OBS", name="CopyObject", method="put",
                   summary="复制对象", tags="对象操作", keywords=()),
    ]


def _engine(**kw) -> TantivyEngine:
    return TantivyEngine.build(_apis(), **kw)


# ---------- build / search 基本契约 ----------

def test_search_returns_sorted_hits_within_unit_range():
    hits = _engine().search(("重启服务器",), limit=8)
    assert hits
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert hits[0].score == 1.0


def test_search_deterministic():
    e = _engine()
    a = e.search(("重启", "服务器"), limit=8)
    b = e.search(("重启", "服务器"), limit=8)
    assert [(h.product, h.name, h.score) for h in a] \
        == [(h.product, h.name, h.score) for h in b]


def test_empty_terms_raises():
    with pytest.raises(ValueError):
        _engine().search((), limit=8)


def test_punctuation_only_terms_no_hit():
    assert _engine().search(("！！！",), limit=8) == []


def test_no_match_terms_empty():
    assert _engine().search(("zzzqqq",), limit=8) == []


def test_limit_respected():
    hits = _engine().search(("对象",), limit=1)
    assert len(hits) <= 1


def test_empty_corpus_builds_and_searches_empty():
    e = TantivyEngine.build([], )
    assert e.search(("重启",), limit=8) == []


# ---------- CJK 2-gram 召回（子串近似的 token 语义） ----------

def test_cjk_gram_recall():
    hits = _engine().search(("服务器",), limit=8)
    assert any(h.product == "ECS" and h.name == "NovaRebootServer" for h in hits)


def test_cjk_phrase_term_recall():
    hits = _engine().search(("重启弹性云服务器",), limit=8)
    assert any(h.product == "ECS" for h in hits)


# ---------- ASCII：整词 + 驼峰切分 ----------

def test_camel_part_matches():
    hits = _engine().search(("reboot",), limit=8)
    assert [h.name for h in hits if h.product == "ECS"] == ["NovaRebootServer"]


def test_camel_lower_part_matches():
    hits = _engine().search(("nova",), limit=8)
    assert any(h.product == "ECS" for h in hits)


def test_whole_name_token_matches():
    hits = _engine().search(("novarebootserver",), limit=8)
    assert any(h.product == "ECS" for h in hits)


def test_ascii_run_in_summary_matches():
    hits = _engine().search(("k8s",), limit=8)
    assert any(h.product == "CCE" for h in hits)


# ---------- kw exact 通道与字段 boost ----------

def test_kw_exact_beats_summary_only():
    apis = [
        IndexedApi(product="A", name="DocA", summary="无关内容",
                   keywords=("重启服务器",)),
        IndexedApi(product="B", name="DocB", summary="重启服务器操作",
                   keywords=()),
    ]
    hits = TantivyEngine.build(apis).search(("重启服务器",), limit=2)
    assert [h.product for h in hits] == ["A", "B"]


def test_field_boost_changes_ranking():
    e = _engine()
    base = e.search(("reboot",), limit=8)
    strong = _engine(boosts=FieldBoosts(name=100.0)).search(("reboot",), limit=8)
    assert base and strong
    assert strong[0].product == "ECS"
    # 提升 name boost 后，name 命中文档的归一化份额只会更高
    assert strong[0].score >= base[0].score


# ---------- provenance（matched 证据） ----------

def test_matched_provenance_fields():
    hits = _engine().search(("重启", "服务器"), limit=8)
    ecs = next(h for h in hits if h.product == "ECS")
    assert ("summary", "重启") in ecs.matched
    assert ("summary", "服务器") in ecs.matched
    assert ("kw_grams", "服务器") in ecs.matched
    assert ("name", "重启") not in ecs.matched


def test_kw_exact_provenance():
    hits = _engine().search(("重启服务器",), limit=8)
    ecs = next(h for h in hits if h.product == "ECS")
    assert ("kw_exact", "重启服务器") in ecs.matched


# ---------- DocHit 原始字段回读 ----------

def test_doc_hit_carries_original_values():
    hits = _engine().search(("reboot",), limit=8)
    h = next(h for h in hits if h.product == "ECS")
    assert isinstance(h, DocHit)
    assert h.name == "NovaRebootServer"
    assert h.method == "post"
    assert h.summary == "重启弹性云服务器"
    assert h.tags == "实例管理"
    assert h.keywords == ("重启服务器", "开机")
    assert h.product == "ECS"


# ---------- tie-break 稳定序 ----------

def test_tie_break_sorted_by_product_then_name():
    apis = [
        IndexedApi(product="B", name="Same", summary="标签内容"),
        IndexedApi(product="A", name="Same", summary="标签内容"),
    ]
    hits = TantivyEngine.build(apis).search(("标签", "内容"), limit=8)
    assert [(h.product, h.name) for h in hits] == [("A", "Same"), ("B", "Same")]


# ---------- 真实数据 smoke（无网络） ----------

@pytest.mark.skipif(not REAL_INDEX.is_file(), reason="缺 configs/entity-index.json")
def test_real_index_smoke():
    raw = json.loads(REAL_INDEX.read_text(encoding="utf-8"))
    apis = [IndexedApi(product=a["p"], name=a["n"], summary=a.get("s") or "",
                       tags=a.get("t") or "", keywords=tuple(a.get("k") or []))
            for a in raw["apis"]]
    import time
    t0 = time.perf_counter()
    e = TantivyEngine.build(apis)
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"\nreal index build: {build_ms:.0f} ms / {len(apis)} docs")
    t0 = time.perf_counter()
    hits = e.search(("云服务器",), limit=16)
    dt = (time.perf_counter() - t0) * 1000
    print(f"real query: {dt:.1f} ms")
    products = [h.product for h in hits[:5]]
    assert "ECS" in products or "HCSECS" in products
