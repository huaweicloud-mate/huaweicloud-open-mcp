"""S16b：实体图谱运行时检索 EntityGraph（BM25 引擎 + 身份信号）。

parse 严格校验 / empty / search_apis 行为锚定（排序、证据、机制参数；
分数为 BM25 统计量，不再手算——真值由金评集 tests/fixtures/entity_eval.json
固化）/ load_entity_index 三分支。S15c service/server 装配断言原样保留。
"""

import json
from pathlib import Path

import pytest

from common import paths as common_paths
from common.types import SearchApisResult
from mcp_openapi.entity_graph import (
    EntityGraph,
    load_entity_index,
    parse_entity_index,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_INDEX = REPO_ROOT / "configs" / "entity-index.json"
EVAL_SET = REPO_ROOT / "tests" / "fixtures" / "entity_eval.json"

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


# ---------- 检索行为锚定（身份信号 / BM25 / 证据） ----------

def test_search_alias_exact():
    out = _graph().search_apis("云主机")
    assert out["ok"] is True
    assert out["total"] == 1
    row = out["products"][0]
    assert row["product"] == "ECS"
    # 18（嵌句 alias exact）+ 0.12（广度先验：2 个 api 的平方 log）
    assert row["score"] == 18.12
    assert row["matched_via"] == ["alias:云主机"]
    assert out["truncated"] is False


def test_search_kw_hit_recalls_and_ranks_kw_doc_top():
    """kw 精确命中：目标文档排首位；碎片 gram 召回使孪生成员弱命中
    （twin 归并后并入主行，新语义——旧实现 substring 语义下零召回）。"""
    out = _graph().search_apis("重启服务器")
    assert out["total"] == 1
    row = out["products"][0]
    assert row["product"] == "ECS"
    assert row["matched_via"] == ["kw:重启服务器→NovaRebootServers"]
    assert row["apis"][0]["name"] == "NovaRebootServers"
    assert len(row["apis"]) >= 2


def test_search_name_contains_ranks_tms_over_ecs():
    out = _graph().search_apis("标签")
    assert [r["product"] for r in out["products"]] == ["TMS", "ECS"]
    assert out["products"][0]["matched_via"] == [
        "tag:标签管理(3产品)", "name:标签"]
    assert out["products"][1]["matched_via"] == ["tag:标签管理(3产品)"]
    assert out["products"][0]["score"] > out["products"][1]["score"]


def test_search_tag_bonus_requires_strict_substring():
    """term 重启服务器 的 gram 碎片（服务/务器）不得误触发 tag bonus。"""
    out = _graph().search_apis("重启服务器")
    row = out["products"][0]
    assert row["matched_via"] == ["kw:重启服务器→NovaRebootServers"]


def test_search_twin_merge():
    out = _graph().search_apis("弹性云服务器")
    assert out["total"] == 1
    row = out["products"][0]
    assert row["product"] == "ECS"           # link 非空者为主产品
    assert {"product": "HCSECS", "kind": "twin"} in row["related"]
    # 短字段（重启弹性云服务器）在 BM25 长度归一化下胜长字段
    assert row["apis"][0]["name"] == "NovaRebootServers"


def test_search_multi_term_evidence_priority():
    out = _graph().search_apis("重启 服务器")
    row = out["products"][0]
    assert row["product"] == "ECS"
    # 证据按优先级排序：tag(3) < kw contains(4)；封顶 3 条（name:服务器 让位）
    assert row["matched_via"] == [
        "tag:云服务器生命周期管理(2产品)",
        "kw:重启→NovaRebootServers",
        "kw:服务器→NovaRebootServers"]


def test_search_identity_beats_bm25_only():
    out = _graph().search_apis("云")
    # 单字 CJK 无 gram 召回：纯身份信号计分（name 5 + alias contains 8 / name 5）
    assert [r["product"] for r in out["products"]] == ["ECS", "EVS"]
    assert out["products"][0]["score"] == 13.12
    assert out["products"][0]["matched_via"] == ["name:云", "alias:云主机"]
    assert out["products"][1]["score"] == 5.048
    assert out["products"][1]["matched_via"] == ["name:云"]


def test_search_cjk_phrase_alias_wins():
    """连续口语短语：alias 嵌入原句（身份信号）+ 弱 gram 召回，胜纯 gram 行。"""
    out = _graph().search_apis("给云主机打个标签")
    assert [r["product"] for r in out["products"]] == ["ECS", "TMS"]
    assert out["products"][0]["matched_via"][0] == "alias:云主机"
    assert out["products"][0]["score"] > out["products"][1]["score"]


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


def _wide_graph(n: int) -> EntityGraph:
    """n 个产品共享同一关键词的合成图谱（验证 limit=-1 越过 20 上限）。"""
    raw = {
        "version": 1,
        "products": [
            {"product": f"P{i:02d}", "name": f"服务{i:02d}", "category": "计算",
             "is_global": False, "link": None, "aliases": [], "related": []}
            for i in range(n)
        ],
        "apis": [
            {"n": f"Action{i:02d}", "m": "post", "s": "执行操作",
             "t": "操作管理", "p": f"P{i:02d}", "k": ["重启"]}
            for i in range(n)
        ],
        "tag_products": {"操作管理": n},
    }
    return parse_entity_index(raw)


def test_search_limit_minus_one_unlimited():
    """limit=-1 哨兵：越过 20 上限返回全部命中，信封回显 -1。"""
    out = _wide_graph(25).search_apis("重启", limit=-1)
    assert out["total"] == 25
    assert out["limit"] == -1
    assert out["truncated"] is False
    assert len(out["products"]) == out["total"]


def test_search_limit_minus_one_blank_query_echo():
    out = _wide_graph(25).search_apis("  ", limit=-1)
    assert out == {"ok": True, "query": "  ", "total": 0, "limit": -1,
                   "products": [], "truncated": False}


def test_search_limit_clamp_regression():
    """默认 8 / 超 20 clamp 20 / 0 与非 -1 负数 clamp ≥1（现状回归）。"""
    g = _wide_graph(25)
    out = g.search_apis("重启")
    assert out["limit"] == 8 and len(out["products"]) == 8
    assert out["truncated"] is True
    out = g.search_apis("重启", limit=100)
    assert out["limit"] == 20 and len(out["products"]) == 20
    out = g.search_apis("重启", limit=0)
    assert out["limit"] == 1 and len(out["products"]) == 1
    out = g.search_apis("重启", limit=-2)
    assert out["limit"] == 1 and len(out["products"]) == 1


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


def test_search_weak_gram_recall_single_row():
    """弱 gram 命中（列表 ⊆ 云硬盘列表碎片）单行可达，分数为正浮点。"""
    out = _graph().search_apis("列表")
    assert out["total"] == 1
    assert out["products"][0]["product"] == "EVS"
    assert out["products"][0]["score"] > 0


def test_search_multi_term_union_dedup():
    out = _graph().search_apis("标签 云硬盘")
    products = [r["product"] for r in out["products"]]
    assert len(products) == len(set(products))
    assert "TMS" in products and "EVS" in products


def test_search_ascii_cjk_boundary_split():
    """混写切分：k8s集群扩容 → k8s（引擎 ASCII 词）+ 集群扩容（CJK run）。"""
    out = _graph().search_apis("k8s集群扩容")
    assert out["ok"] is True


def test_terms_mixed_language_split():
    from mcp_openapi.entity_graph import _terms
    assert _terms("k8s集群扩容") == ["k8s", "集群扩容", "k8s集群扩容"]
    assert _terms("给云主机打个标签") == ["给云主机打个标签"]
    assert _terms("重启 服务器") == ["重启", "服务器", "重启 服务器"]


# ---------- 金评集门禁（真数据，人工标注独立真值） ----------

@pytest.mark.skipif(not REAL_INDEX.is_file(), reason="缺 configs/entity-index.json")
def test_golden_eval_real_index():
    """金评集全绿是重构验收线：修复已知失败且不回退既有通过项。"""
    from tests.eval_runner import run_eval
    passed, failed = run_eval(load_entity_index(str(REAL_INDEX)), EVAL_SET)
    assert not failed, f"金评集 {passed}/{passed + len(failed)}，失败: {failed}"


# ---------- 废弃治理机制参数 exclude_apis ----------

def test_search_exclude_apis_drops_api_and_score():
    """hide 机制参数：被排除 api 从计分与 top-3 名单消失；产品可经
    name/alias/其余 api 的通用文本通道仍可达（按设计，不误伤产品可达性）。"""
    out = _graph().search_apis("重启服务器", exclude_apis={
        "ecs": frozenset({"novarebootservers"}),
        "hcsecs": frozenset({"rebootcloudhost"})})
    row = next(r for r in out["products"] if r["product"] == "ECS")
    names = [a["name"] for a in row["apis"]]
    assert "NovaRebootServers" not in names
    assert "RebootCloudHost" not in names
    assert names == ["BatchCreateServerTags"]   # 通用碎片（服务/务器）兜底召回
    assert row["score"] > 0


def test_search_exclude_apis_keeps_product_discoverable():
    """排除后产品经 name/非废弃 api 通道仍可达；top-3 名额让位。"""
    out = _graph().search_apis(
        "弹性云服务器", exclude_apis={"ecs": frozenset({"novarebootservers"})})
    row = next(r for r in out["products"] if r["product"] == "ECS")
    names = {a["name"] for a in row["apis"]}
    assert "NovaRebootServers" not in names
    assert {"BatchCreateServerTags", "RebootCloudHost"} <= names
    assert row["score"] > 0


def test_search_exclude_apis_twin_scoped():
    """排除集按产品精确生效：HCSECS 成员排除不影响 ECS 计分与归并。"""
    out = _graph().search_apis(
        "弹性云服务器", exclude_apis={"hcsecs": frozenset({"rebootcloudhost"})})
    row = next(r for r in out["products"] if r["product"] == "ECS")
    names = [a["name"] for a in row["apis"]]
    assert "RebootCloudHost" not in names
    assert set(names) == {"BatchCreateServerTags", "NovaRebootServers"}
    assert row["score"] > 0


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


def test_service_search_limit_minus_one_passthrough(tmp_path):
    from common.audit import NdjsonAuditSink
    from mcp_openapi.service import ServiceConfig, ToolService
    audit = tmp_path / "audit.jsonl"
    svc = ToolService(ServiceConfig(
        entity_graph=parse_entity_index(RAW),
        audit_sink=NdjsonAuditSink(audit)))
    out = svc.search_apis("云主机", limit=-1)
    assert out["total"] == 1
    assert out["limit"] == -1
    event = json.loads(audit.read_text(encoding="utf-8").strip())
    assert event["input"] == {"query": "云主机", "limit": -1}


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


# ---------- S15c：search 废弃治理三态（annotate/hide/off） ----------

def _svc_dep(mode):
    from mcp_openapi.deprecated import parse_deprecated_index
    idx = parse_deprecated_index({
        "products": {"ECS": {"NovaRebootServers": {
            "replacement": "BatchRebootServers",
            "doc_url": "https://u/reboot"}}}})
    return _svc(deprecated_index=idx, deprecated_mode=mode)


def test_service_search_deprecated_annotate():
    svc = _svc_dep("annotate")
    out = svc.search_apis("弹性云服务器")
    row = next(r for r in out["products"] if r["product"] == "ECS")
    nova = next(a for a in row["apis"] if a["name"] == "NovaRebootServers")
    assert nova["deprecated"] is True
    assert nova["replacement"] == "BatchRebootServers"
    # 未废弃 api 不加字段
    batch = next(a for a in row["apis"] if a["name"] == "BatchCreateServerTags")
    assert "deprecated" not in batch and "replacement" not in batch


def test_service_search_deprecated_hide():
    svc = _svc_dep("hide")
    out = svc.search_apis("重启服务器")
    row = next(r for r in out["products"] if r["product"] == "ECS")
    names = [a["name"] for a in row["apis"]]
    assert "NovaRebootServers" not in names
    assert "BatchCreateServerTags" in names


def test_service_search_deprecated_off_noop():
    svc = _svc_dep("off")
    out = svc.search_apis("重启服务器")
    row = next(r for r in out["products"] if r["product"] == "ECS")
    nova = next(a for a in row["apis"] if a["name"] == "NovaRebootServers")
    assert "deprecated" not in nova
    assert "replacement" not in nova


# ---------- name 嵌句档（S16b 扩展） ----------

def test_search_name_embedded_in_phrase():
    """产品中文名整体嵌入用户原句（非相等）：W_NAME 嵌句档生效。"""
    out = _graph().search_apis("给弹性云服务器扩个容")
    row = next(r for r in out["products"] if r["product"] == "ECS")
    assert "name:弹性云服务器" in row["matched_via"]
    assert row["score"] >= 5.0


def test_search_name_embed_no_double_count():
    """term == name 走常规 name 分支，嵌句档不双计（分数不翻倍）。"""
    out = _graph().search_apis("弹性云服务器")
    row = out["products"][0]
    assert row["matched_via"].count("name:弹性云服务器") == 1

