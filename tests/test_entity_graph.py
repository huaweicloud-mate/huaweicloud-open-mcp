"""S15b：实体图谱运行时检索 EntityGraph。

parse 严格校验 / empty / search_apis 评分矩阵（手写期望分数与证据，独立真值）/
load_entity_index 三分支（None→bundle 缺失静默空、off→显式禁用、路径→fail-fast）。
"""

import json

import pytest

from common import paths as common_paths
from common.types import SearchApisResult
from mcp_openapi.entity_graph import (
    EntityGraph,
    load_entity_index,
    parse_entity_index,
)

RAW = {
    "version": 1,
    "products": [
        {"product": "ECS", "name": "弹性云服务器", "category": "计算",
         "is_global": False, "link": "https://x/ecs",
         "aliases": ["云主机", "虚拟机"],
         "related": [{"product": "HCSECS", "kind": "twin"}]},
        {"product": "HCSECS", "name": "弹性云服务器", "category": "计算",
         "is_global": False, "link": None, "aliases": [],
         "related": [{"product": "ECS", "kind": "twin"}]},
        {"product": "TMS", "name": "标签管理服务", "category": "管理与治理",
         "is_global": False, "link": None, "aliases": [], "related": []},
        {"product": "EVS", "name": "云硬盘", "category": "存储",
         "is_global": False, "link": "https://x/evs", "aliases": ["磁盘"],
         "related": []},
    ],
    "apis": [
        {"n": "NovaRebootServers", "m": "post", "s": "重启弹性云服务器",
         "t": "云服务器生命周期管理", "p": "ECS",
         "k": ["重启服务器", "开机"]},
        {"n": "BatchCreateServerTags", "m": "post",
         "s": "为弹性云服务器批量添加标签", "t": "标签管理", "p": "ECS"},
        {"n": "ListVolumes", "m": "get", "s": "查询云硬盘列表",
         "t": "云硬盘", "p": "EVS"},
        {"n": "RebootCloudHost", "m": "post", "s": "重启云服务器",
         "t": "云服务器生命周期管理", "p": "HCSECS"},
        {"n": "CreateTags", "m": "post", "s": "创建资源标签",
         "t": "标签管理", "p": "TMS"},
    ],
    "tag_products": {"标签管理": 3, "云服务器生命周期管理": 2, "云硬盘": 1},
}


def _graph() -> EntityGraph:
    return parse_entity_index(RAW)


# ---------- parse 严格校验 ----------

def test_parse_ok_and_empty():
    g = _graph()
    assert g.version == 1
    e = EntityGraph.empty()
    assert e.search_apis("任何词")["total"] == 0


def test_parse_rejects_bad_shapes():
    bad = [
        None, [], "x",
        {},                                  # 缺 version
        dict(RAW, version="1"),              # version 非整数
        dict(RAW, unknown=1),                # 未知顶层键
        dict(RAW, products={}),
        dict(RAW, products=[{"name": "x"}]),          # 缺 product
        dict(RAW, products=[dict(RAW["products"][0], aliases="云主机")]),
        dict(RAW, products=[dict(RAW["products"][0],
                                 related=[{"product": "X"}])]),  # 缺 kind
        dict(RAW, apis=[{"n": "A", "m": "get", "s": "", "t": ""}]),  # 缺 p
        dict(RAW, apis=[dict(RAW["apis"][0], k="重启")]),            # k 非列表
        dict(RAW, apis=[dict(RAW["apis"][0], k=[1])]),               # k 项非字符串
        dict(RAW, tag_products={"标签": "3"}),
    ]
    for raw in bad:
        with pytest.raises(ValueError):
            parse_entity_index(raw)


# ---------- 检索评分矩阵 ----------

def test_search_alias_exact():
    out = _graph().search_apis("云主机")
    assert out["ok"] is True
    assert out["total"] == 1
    row = out["products"][0]
    assert row["product"] == "ECS"
    assert row["score"] == 12
    assert row["matched_via"] == ["alias:云主机"]
    assert out["truncated"] is False


def test_search_keyword_exact():
    out = _graph().search_apis("重启服务器")
    assert out["total"] == 1
    row = out["products"][0]
    assert row["product"] == "ECS"
    assert row["score"] == 6
    assert row["matched_via"] == ["kw:重启服务器→NovaRebootServers"]
    assert [a["name"] for a in row["apis"]] == ["NovaRebootServers"]


def test_search_name_contains_ranks_tms_over_ecs():
    out = _graph().search_apis("标签")
    assert [r["product"] for r in out["products"]] == ["TMS", "ECS"]
    # TMS: name 5 + agg 2 + 判别 tag 2（标签管理 coverage 3 ≤3）= 9
    assert out["products"][0]["score"] == 9
    assert out["products"][0]["matched_via"] == [
        "tag:标签管理(3产品)", "name:标签"]
    # ECS: agg 2 + 判别 tag 2 = 4
    assert out["products"][1]["score"] == 4


def test_search_twin_merge():
    out = _graph().search_apis("弹性云服务器")
    assert out["total"] == 1
    row = out["products"][0]
    assert row["product"] == "ECS"           # link 非空者为主产品
    assert row["score"] == 7                 # max(ECS=7, HCSECS=5)
    assert {"product": "HCSECS", "kind": "twin"} in row["related"]
    # 同分按名字排序
    assert [a["name"] for a in row["apis"]] == [
        "BatchCreateServerTags", "NovaRebootServers"]


def test_search_multi_term_with_tag_evidence_cap():
    out = _graph().search_apis("重启 服务器")
    row = out["products"][0]
    assert row["product"] == "ECS"
    # 重启: kw-contains 2 + summary 1 = 3；服务器: name 5 + agg 5 + 判别 tag 2 = 12
    assert row["score"] == 15
    # 证据按优先级排序；kw contains 与 kw exact 同文本去重合并
    assert row["matched_via"] == [
        "tag:云服务器生命周期管理(2产品)",
        "kw:重启服务器→NovaRebootServers",
        "name:服务器"]


def test_search_discriminative_tag_bonus():
    out = _graph().search_apis("云")
    # ECS 行（twin 归并，取 max）：name 5 + alias-contains 云主机 8 + non-kw 3 + 判别 tag 2 = 18
    assert [r["product"] for r in out["products"]] == ["ECS", "EVS"]
    assert out["products"][0]["score"] == 18
    # EVS: name 5 + non-kw 2 + 判别 tag（云硬盘 coverage 1）2 = 9
    assert out["products"][1]["score"] == 9


def test_search_category_and_allowed_filters():
    out = _graph().search_apis("标签", category="存储")
    assert out["total"] == 0
    out = _graph().search_apis("标签", category="计算")
    assert [r["product"] for r in out["products"]] == ["ECS"]
    out = _graph().search_apis("标签", allowed=frozenset({"EVS", "TMS"}))
    assert [r["product"] for r in out["products"]] == ["TMS"]


def test_search_limit_and_truncated():
    out = _graph().search_apis("云", limit=1)
    assert out["total"] == 2
    assert len(out["products"]) == 1
    assert out["truncated"] is True
    assert out["limit"] == 1


def test_search_empty_and_blank_query():
    for q in ("", "   ", "、。"):
        out = _graph().search_apis(q)
        assert out == {"ok": True, "query": q, "total": 0, "limit": 8,
                       "products": [], "truncated": False}


def test_search_result_envelope_shape():
    out = _graph().search_apis("磁盘")
    assert isinstance(out, dict)
    assert SearchApisResult.__annotations__.keys() >= {
        "ok", "query", "total", "limit", "products", "truncated"}
    row = out["products"][0]
    assert row["product"] == "EVS"
    assert row["matched_via"] == ["alias:磁盘"]
    assert [a["name"] for a in row["apis"]] == []


# ---------- load_entity_index 三分支 ----------

def test_load_none_missing_default_is_silent_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(common_paths, "project_root", lambda: tmp_path)
    monkeypatch.setattr(common_paths, "config_path",
                        lambda name: tmp_path / name)
    g = load_entity_index(None)
    assert g.search_apis("云主机")["total"] == 0


def test_load_off_is_disabled(tmp_path):
    g = load_entity_index("off")
    assert g.search_apis("云主机")["total"] == 0


def test_load_explicit_path(tmp_path):
    p = tmp_path / "idx.json"
    p.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    g = load_entity_index(str(p))
    assert g.search_apis("云主机")["total"] == 1


def test_load_explicit_missing_fail_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_entity_index(str(tmp_path / "nope.json"))


def test_load_default_bundled(tmp_path, monkeypatch):
    cfg = tmp_path / "entity-index.json"
    cfg.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(common_paths, "project_root", lambda: tmp_path)
    monkeypatch.setattr(common_paths, "config_path",
                        lambda name: tmp_path / name)
    assert load_entity_index(None).search_apis("云主机")["total"] == 1


# ---------- S15c：service/server 装配 ----------

def _svc(**kw):
    from mcp_openapi.service import ServiceConfig, ToolService
    cfg = ServiceConfig(entity_graph=parse_entity_index(RAW), **kw)
    return ToolService(config=cfg)


def test_service_search_ok():
    svc = _svc()
    out = svc.search_apis("云主机")
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["products"][0]["product"] == "ECS"


def test_service_search_gate_filters_before_ranking():
    from mcp_openapi.gate import Gate
    svc = _svc(gate=Gate(allowed=frozenset({"ECS"}), restrict=True))
    out = svc.search_apis("标签")
    assert [r["product"] for r in out["products"]] == ["ECS"]


def test_service_search_gate_unrestricted_no_filter():
    svc = _svc()
    out = svc.search_apis("标签")
    assert [r["product"] for r in out["products"]] == ["TMS", "ECS"]


def test_service_search_empty_graph_refuses():
    from mcp_openapi.service import ServiceConfig, ToolService
    svc = ToolService(ServiceConfig())
    out = svc.search_apis("云主机")
    assert out["ok"] is False
    assert "实体索引" in out["reason"]


def test_service_search_audited(tmp_path):
    from common.audit import NdjsonAuditSink
    from mcp_openapi.service import ServiceConfig, ToolService
    audit = tmp_path / "audit.jsonl"
    svc = ToolService(ServiceConfig(
        entity_graph=parse_entity_index(RAW),
        audit_sink=NdjsonAuditSink(audit)))
    svc.search_apis("云主机", limit=3, category=None)
    lines = audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["tool"] == "search_apis"
    assert event["ok"] is True
    assert event["input"] == {"query": "云主机", "limit": 3, "category": None}


def _args(**kw):
    import argparse
    base = dict(mock=True, policy=None, region=None, mock_base=None,
                mock_passthrough=None, gate=None, hints=None, audit_file=None,
                spill_dir=None, deprecated_index=None, deprecated_mode=None,
                entity_index=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_build_config_entity_index_explicit(tmp_path):
    from mcp_openapi.server import build_openapi_config
    p = tmp_path / "idx.json"
    p.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    cfg = build_openapi_config(_args(entity_index=str(p)))
    assert cfg.entity_graph.search_apis("云主机")["total"] == 1


def test_build_config_entity_index_off(tmp_path, monkeypatch):
    from mcp_openapi.server import build_openapi_config
    monkeypatch.setattr(common_paths, "project_root", lambda: tmp_path)
    monkeypatch.setenv("HUAWEICLOUD_MCP_ENTITY_INDEX", "off")
    cfg = build_openapi_config(_args())
    assert cfg.entity_graph.version == 0


def test_build_config_entity_index_env(tmp_path, monkeypatch):
    from mcp_openapi.server import build_openapi_config
    p = tmp_path / "idx.json"
    p.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("HUAWEICLOUD_MCP_ENTITY_INDEX", str(p))
    cfg = build_openapi_config(_args())
    assert cfg.entity_graph.search_apis("云主机")["total"] == 1


def test_build_config_entity_index_default_missing_silent(tmp_path, monkeypatch):
    from mcp_openapi.server import build_openapi_config
    monkeypatch.setattr(common_paths, "project_root", lambda: tmp_path / "repo")
    cfg = build_openapi_config(_args())
    assert cfg.entity_graph.version == 0


def test_server_instructions_step_zero():
    from mcp_openapi.gate import Gate
    from mcp_openapi.server import build_instructions
    text = build_instructions(Gate.unrestricted())
    assert "0. `search_apis`" in text
    assert "matched_via" in text


# ---------- CJK 2-gram 弱结果回退 ----------

def test_search_fallback_continuous_cjk():
    """无分隔符口语短语：alias-only 行无 API 接战 → 2-gram 回退补召回。

    ECS 16（alias 通道一次计分：云主机 嵌入原句 → 12；标签 non-kw 2 + 判别 tag 2）
    > TMS 6（gram-name 2 + non-kw 2 + 判别 tag 2）：碎片降权后别名信号胜出。
    """
    out = _graph().search_apis("给云主机打个标签")
    assert out["products"][0]["product"] == "ECS"
    assert out["products"][0]["score"] == 16
    assert out["products"][1]["product"] == "TMS"
    assert out["products"][1]["score"] == 6


def test_search_keyword_query_no_fallback():
    """关键词精确命中（≥阈值）：不走回退，行为与既有矩阵一致。"""
    out = _graph().search_apis("重启服务器")
    assert out["total"] == 1
    row = out["products"][0]
    assert row["product"] == "ECS"
    assert row["score"] == 6
    assert row["matched_via"] == ["kw:重启服务器→NovaRebootServers"]


def test_search_weak_result_grams_add_nothing_keeps_original():
    """弱命中但 2-gram 无增量：保留原结果（不夸大）。"""
    out = _graph().search_apis("列表")
    assert out["total"] == 1
    assert out["products"][0]["product"] == "EVS"
    assert out["products"][0]["score"] == 1


def test_search_fallback_union_dedup():
    """回退并集去重：原轮与回退轮同名产品保留高分行。"""
    out = _graph().search_apis("标签 云硬盘")
    products = [r["product"] for r in out["products"]]
    assert len(products) == len(set(products))
    assert "TMS" in products and "EVS" in products


def test_search_ascii_cjk_boundary_split():
    """混写切分：k8s集群扩容 → k8s（alias exact）+ 集群扩容。fixture 无 CCE
    别名，验证不抛错且切分正确（_terms 矩阵单列）。"""
    out = _graph().search_apis("k8s集群扩容")
    assert out["ok"] is True


def test_terms_mixed_language_split():
    from mcp_openapi.entity_graph import _terms
    assert _terms("k8s集群扩容") == ["k8s", "集群扩容", "k8s集群扩容"]
    assert _terms("给云主机打个标签") == ["给云主机打个标签"]
    assert _terms("重启 服务器") == ["重启", "服务器", "重启 服务器"]
