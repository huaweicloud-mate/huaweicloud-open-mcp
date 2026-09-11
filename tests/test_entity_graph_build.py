"""S15a：实体图谱确定性构建 build_graph（纯函数）。

输入 apis_docs（raw/apis_docs.json["apis"] 形状）+ huawei_products groups +
knowledge（entity-knowledge.json 三 section）→ 完整 entity-index 产物 dict。
金标为手写期望字面量；generated_at 由 stage 层注入，纯函数不涉时钟。
"""

from apie.build_entity_graph import build_graph

APIS = [
    {"id": "1", "name": "NovaRebootServers", "method": "post",
     "summary": "重启弹性云服务器", "tags": "云服务器生命周期管理",
     "product_short": "ECS", "info_version": "v2"},
    {"id": "2", "name": "BatchCreateServerTags", "method": "post",
     "summary": "为弹性云服务器批量添加标签", "tags": "标签管理",
     "product_short": "ECS", "info_version": "v2"},
    {"id": "3", "name": "RebootCloudHost", "method": "post",
     "summary": "重启云服务器(HCS)", "tags": "云服务器生命周期管理",
     "product_short": "HCSECS", "info_version": "v2"},
    {"id": "4", "name": "ResizeGpuServer", "method": "post",
     "summary": "变更GPU加速云服务器规格", "tags": "规格管理",
     "product_short": "GACS", "info_version": "v2"},
]

GROUPS = [
    {"name": "计算", "products": [
        {"name": "弹性云服务器", "productshort": "ECS", "is_global": False,
         "link": "https://www.huaweicloud.com/product/ecs.html",
         "attributive_product": ""},
        {"name": "弹性云服务器", "productshort": "HCSECS", "is_global": False,
         "link": "", "attributive_product": ""},
        {"name": "GPU加速云服务器", "productshort": "GACS", "is_global": False,
         "link": "https://www.huaweicloud.com/product/facs.html",
         "attributive_product": "ECS"},
    ]},
]

KNOWLEDGE = {
    "version": 1,
    "aliases": [{"alias": "云主机", "targets": ["ECS"], "source": "curated"}],
    "relations": [{"from": "GACS", "to": "ECS", "kind": "semantic",
                   "source": "llm", "note": "GPU服务器归属弹性云服务器体系"}],
    "api_keywords": {"ECS": {"NovaRebootServers": ["重启服务器", "重启云主机"]}},
    "fingerprints": {"ECS": "sha256:x"},
}

EXPECTED = {
    "version": 1,
    "products": [
        {"product": "ECS", "name": "弹性云服务器", "category": "计算",
         "is_global": False,
         "link": "https://www.huaweicloud.com/product/ecs.html",
         "aliases": ["云主机"],
         "related": [{"product": "HCSECS", "kind": "twin"}]},
        {"product": "GACS", "name": "GPU加速云服务器", "category": "计算",
         "is_global": False,
         "link": "https://www.huaweicloud.com/product/facs.html",
         "aliases": [],
         "related": [{"product": "ECS", "kind": "attributive"},
                     {"product": "ECS", "kind": "semantic",
                      "via": "GPU服务器归属弹性云服务器体系"}]},
        {"product": "HCSECS", "name": "弹性云服务器", "category": "计算",
         "is_global": False, "link": None, "aliases": [],
         "related": [{"product": "ECS", "kind": "twin"}]},
    ],
    "apis": [
        {"n": "BatchCreateServerTags", "m": "post",
         "s": "为弹性云服务器批量添加标签", "t": "标签管理", "p": "ECS"},
        {"n": "NovaRebootServers", "m": "post", "s": "重启弹性云服务器",
         "t": "云服务器生命周期管理", "p": "ECS",
         "k": ["重启服务器", "重启云主机"]},
        {"n": "ResizeGpuServer", "m": "post", "s": "变更GPU加速云服务器规格",
         "t": "规格管理", "p": "GACS"},
        {"n": "RebootCloudHost", "m": "post", "s": "重启云服务器(HCS)",
         "t": "云服务器生命周期管理", "p": "HCSECS"},
    ],
    "tag_products": {"云服务器生命周期管理": 2, "标签管理": 1, "规格管理": 1},
}


def test_build_graph_golden():
    out = build_graph(APIS, GROUPS, KNOWLEDGE)
    assert out == EXPECTED


def test_empty_knowledge():
    out = build_graph(APIS, GROUPS, {})
    assert out["version"] == 1
    for p in out["products"]:
        assert p["aliases"] == []
        assert not any(e["kind"] == "semantic" for e in p["related"])
    assert not any("k" in a for a in out["apis"])


def test_alias_multi_target():
    k = {"aliases": [{"alias": "云服务器", "targets": ["ECS", "HCSECS"],
                      "source": "llm"}]}
    out = build_graph(APIS, GROUPS, k)
    by = {p["product"]: p for p in out["products"]}
    assert by["ECS"]["aliases"] == ["云服务器"]
    assert by["HCSECS"]["aliases"] == ["云服务器"]


def test_defensive_filtering():
    k = {
        "aliases": [{"alias": "幽灵", "targets": ["XXX"], "source": "llm"},
                    {"alias": "云主机", "targets": ["ECS", "XXX"],
                     "source": "llm"}],
        "relations": [{"from": "ECS", "to": "ECS", "kind": "semantic",
                       "note": "自环"},
                      {"from": "XXX", "to": "ECS", "kind": "semantic",
                       "note": "未知产品"},
                      {"from": "GACS", "to": "XXX", "kind": "semantic",
                       "note": "未知目标"}],
        "api_keywords": {"ECS": {"NovaRebootServers": ["重启服务器"],
                                 "NoSuchApi": ["幽灵词"]},
                         "XXX": {"NovaRebootServers": ["幽灵产品词"]}},
    }
    out = build_graph(APIS, GROUPS, k)
    by = {p["product"]: p for p in out["products"]}
    # 未知目标产品整条丢弃；多目标中合法目标保留
    assert by["ECS"]["aliases"] == ["云主机"]
    # 自环/未知端点边丢弃；合法 semantic 边保留
    assert by["GACS"]["related"] == [
        {"product": "ECS", "kind": "attributive"}]
    # 未知 API/未知产品的关键词丢弃，合法的保留
    kws = {(a["p"], a["n"]): a.get("k") for a in out["apis"]}
    assert kws[("ECS", "NovaRebootServers")] == ["重启服务器"]
    assert not any(v for (p, n), v in kws.items() if n == "NoSuchApi")


def test_products_without_apis_excluded():
    groups = [dict(GROUPS[0])]
    groups[0]["products"] = groups[0]["products"] + [
        {"name": "专属主机", "productshort": "DeH", "is_global": False,
         "link": "https://x", "attributive_product": "ECS"}]
    out = build_graph(APIS, groups, {})
    assert {p["product"] for p in out["products"]} == {"ECS", "GACS", "HCSECS"}


def test_apis_of_unknown_product_dropped():
    apis = APIS + [{"id": "9", "name": "ListFoo", "method": "get",
                    "summary": "查询", "tags": "T",
                    "product_short": "Cloudtest", "info_version": "v2"}]
    out = build_graph(apis, GROUPS, {})
    assert {a["p"] for a in out["apis"]} == {"ECS", "GACS", "HCSECS"}
    assert {p["product"] for p in out["products"]} == {"ECS", "GACS", "HCSECS"}


def test_related_dedupe_same_product_kind():
    k = {"relations": [
        {"from": "GACS", "to": "ECS", "kind": "semantic", "note": "第一条"},
        {"from": "GACS", "to": "ECS", "kind": "semantic", "note": "重复"},
    ]}
    out = build_graph(APIS, GROUPS, k)
    by = {p["product"]: p for p in out["products"]}
    sem = [e for e in by["GACS"]["related"] if e["kind"] == "semantic"]
    assert len(sem) == 1
    assert sem[0]["via"] == "第一条"


# ---------- S15d：stage 落盘编排 ----------

def _write_json(path, data):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_build_artifact_files_writes_both_locations(tmp_path):
    import json

    from apie.build_entity_graph import build_artifact_files
    _write_json(tmp_path / "raw" / "apis_docs.json", {"apis": APIS})
    _write_json(tmp_path / "raw" / "huawei_products.json", {"groups": GROUPS})
    _write_json(tmp_path / "configs" / "entity-knowledge.json", KNOWLEDGE)
    summary = build_artifact_files(tmp_path)
    assert summary["products"] == 3
    data_path = tmp_path / "data" / "graph" / "entity-index.json"
    cfg_path = tmp_path / "configs" / "entity-index.json"
    assert data_path.is_file() and cfg_path.is_file()
    artifact = json.loads(data_path.read_text(encoding="utf-8"))
    assert artifact["version"] == 1
    assert artifact["generated_at"]
    # 双落盘内容一致（wheel bundle 副本同源）
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == artifact
    # 产物可被运行时解析
    from mcp_openapi.entity_graph import parse_entity_index
    g = parse_entity_index(artifact)
    assert g.search_apis("云主机")["total"] == 1


def test_llm_refresh_without_env_skips(tmp_path, monkeypatch):
    from apie.build_entity_graph import refresh_knowledge_file
    for k in ("ENTITY_LLM_BASE_URL", "ENTITY_LLM_API_KEY", "ENTITY_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    _write_json(tmp_path / "configs" / "entity-knowledge.json",
                {"aliases": [], "relations": [], "api_keywords": {},
                 "fingerprints": {}})
    out = refresh_knowledge_file(tmp_path)
    assert out == {"aliases": [], "relations": [], "api_keywords": {},
                   "fingerprints": {}}


def test_llm_refresh_with_fake_transport(tmp_path):
    import json

    from apie.build_entity_graph import refresh_knowledge_file
    _write_json(tmp_path / "raw" / "apis_docs.json", {"apis": APIS})
    _write_json(tmp_path / "raw" / "huawei_products.json", {"groups": GROUPS})
    calls = []

    def fake_transport(messages):
        calls.append(messages)
        return ('{"aliases": [{"alias": "云主机", "targets": ["ECS"]}],'
                '"relations": []}')

    out = refresh_knowledge_file(tmp_path, transport=fake_transport)
    assert len(calls) >= 1
    assert out["aliases"][0]["alias"] == "云主机"
    persisted = json.loads(
        (tmp_path / "configs" / "entity-knowledge.json")
        .read_text(encoding="utf-8"))
    assert persisted["aliases"][0]["alias"] == "云主机"
    # 无 .tmp-part 残留（原子替换）
    assert not list((tmp_path / "configs").glob("*.tmp-part"))
