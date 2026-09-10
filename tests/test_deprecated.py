"""S14：openapi 废弃接口索引 DeprecatedIndex + list_apis 发现面治理。

S14b parse/load/查询/严格校验；S14c metadata.list_apis exclude_apis；
S14d service 三模式 + 装配默认档推导。
"""

import argparse

import pytest

from apie.memory_store import MemoryStore
from apie.metadata import list_apis
from common import paths as common_paths
from mcp_openapi.deprecated import (
    DeprecatedEntry,
    DeprecatedIndex,
    load_deprecated_index,
    parse_deprecated_index,
)
from mcp_openapi.server import build_openapi_config
from mcp_openapi.service import ServiceConfig, ToolService


@pytest.fixture(autouse=True)
def _seal_from_repo_configs(tmp_path, monkeypatch):
    """隔离真实仓库 configs/：缺省 hints 装配不依赖宿主文件系统（本文件只测
    deprecated 语义，hints 缺省档静默 empty 即可）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(common_paths, "project_root", lambda: tmp_path / "repo")

RAW = {
    "products": {
        "ECS": {
            "NovaRebootServer": {"replacement": "BatchRebootServers",
                                 "doc_url": "https://u/1"},
            "NovaStartServer": {},
            "NovaStopServer": {"replacement": None, "doc_url": None},
        },
    },
}


def test_empty_is_noop():
    idx = DeprecatedIndex.empty()
    assert idx.entry("ECS", "NovaRebootServer") is None
    assert idx.names("ECS") == frozenset()


def test_parse_lookup_case_insensitive():
    idx = parse_deprecated_index(RAW)
    assert idx.entry("ecs", "NovaRebootServer") == DeprecatedEntry(
        replacement="BatchRebootServers", doc_url="https://u/1")
    assert idx.entry("ECS", "novarebootserver") == DeprecatedEntry(
        replacement="BatchRebootServers", doc_url="https://u/1")
    assert idx.entry("ECS", "CreateServer") is None
    assert idx.entry("OBS", "NovaRebootServer") is None


def test_parse_names_for_hide_mode():
    idx = parse_deprecated_index(RAW)
    assert idx.names("ecs") == frozenset({"novarebootserver", "novastartserver",
                                          "novastopserver"})
    assert idx.names("OBS") == frozenset()


def test_parse_empty_form():
    idx = parse_deprecated_index({"products": {}})
    assert idx.names("ECS") == frozenset()


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_deprecated_index("nope")
    with pytest.raises(ValueError):
        parse_deprecated_index({"unknown": 1})
    with pytest.raises(ValueError):
        parse_deprecated_index({"products": {"ECS": 1}})
    with pytest.raises(ValueError):
        parse_deprecated_index({"products": {"ECS": {"A": 1}}})
    with pytest.raises(ValueError):
        parse_deprecated_index({"products": {"ECS": {"A": {"unknown": 1}}}})
    with pytest.raises(ValueError):
        parse_deprecated_index({"products": {"ECS": {"A": {"replacement": 1}}}})


def test_load_deprecated_index(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"products": {"ECS": {"A": {"replacement": "B"}}}}', encoding="utf-8")
    idx = load_deprecated_index(str(p))
    assert idx.entry("ECS", "a").replacement == "B"


def test_load_deprecated_index_bare_name_resolves_repo_configs(tmp_path, monkeypatch):
    """裸文件名经 config_path 解析：仓库根 configs/ 优先（S0 委派，经 loader 接口验证）。"""
    from common import paths

    cfg = tmp_path / "configs" / "deprecated.json"
    cfg.parent.mkdir()
    cfg.write_text('{"products": {"ECS": {"A": {"replacement": "B"}}}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # cwd 无同名文件
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert load_deprecated_index("deprecated.json").entry("ECS", "a").replacement == "B"


def test_load_deprecated_index_missing_everywhere_raises(tmp_path, monkeypatch):
    """全缺失 fail-fast：FileNotFoundError 含全部尝试路径（显式 + 两条 configs 候选）。"""
    from common import paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    with pytest.raises(FileNotFoundError) as excinfo:
        load_deprecated_index("missing.json")
    msg = str(excinfo.value)
    assert str(tmp_path / "repo" / "configs" / "missing.json") in msg
    assert "huaweicloud_open_mcp" in msg


def test_load_deprecated_index_none_is_empty():
    assert load_deprecated_index(None).names("ECS") == frozenset()
    assert load_deprecated_index("").names("ECS") == frozenset()


# ---------- S14c：metadata.list_apis exclude_apis（hide 机制参数） ----------

APIS = [
    {"name": "CreateServers", "method": "post", "summary": "创建云服务器",
     "tags": "生命周期管理", "product_short": "ECS", "info_version": "v1"},
    {"name": "NovaRebootServer", "method": "post", "summary": "重启云服务器",
     "tags": "状态管理", "product_short": "ECS", "info_version": "v2"},
    {"name": "ListServersDetails", "method": "get", "summary": "查询详情列表",
     "tags": "状态管理", "product_short": "ECS", "info_version": "v1"},
]


def test_list_apis_exclude_filters_items_and_counts():
    out = list_apis(APIS, "ECS", exclude_apis=frozenset({"novarebootserver"}))
    names = [a["name"] for a in out["apis"]]
    assert names == ["CreateServers", "ListServersDetails"]
    assert out["total"] == 2
    # tag_groups 同口径：过滤后状态管理只剩 1
    tags = {t["tag"]: t["api_count"] for t in out["tag_groups"]}
    assert tags == {"生命周期管理": 1, "状态管理": 1}


def test_list_apis_exclude_paginates_after_filter():
    out = list_apis(APIS, "ECS", limit=2,
                    exclude_apis=frozenset({"novarebootserver"}))
    assert out["total"] == 2
    assert [a["name"] for a in out["apis"]] == ["CreateServers", "ListServersDetails"]


def test_list_apis_exclude_empty_set_is_noop():
    out = list_apis(APIS, "ECS", exclude_apis=frozenset())
    assert out["total"] == 3


def test_list_apis_without_exclude_is_status_quo():
    out = list_apis(APIS, "ECS")
    assert out["total"] == 3


# ---------- S14d：service 三模式 + get_api 恒可用 ----------

GROUPS = [{"name": "计算", "products": [
    {"productshort": "ECS", "name": "弹性云服务器", "api_count": 3,
     "is_global": False, "link": None}]}]

DOC_NRB = {
    "swagger": "2.0", "host": "ecs.cn-north-4.myhuaweicloud.com", "basePath": "/",
    "definitions": {},
    "paths": {"/v2.1/{p}/servers/{id}/action": {"post": {
        "operationId": "NovaRebootServer", "summary": "重启云服务器",
        "parameters": [], "responses": {"202": {"description": "Accepted"}}}}},
}

INDEX = parse_deprecated_index({
    "products": {"ECS": {"NovaRebootServer": {
        "replacement": "BatchRebootServers", "doc_url": "https://u/1"}}}})


def _svc(mode, index=INDEX):
    store = MemoryStore()
    store.set_products(GROUPS)
    store.set_apis("ECS", APIS)
    op = DOC_NRB["paths"]["/v2.1/{p}/servers/{id}/action"]["post"]
    store.set_api_cache(("ecs", "NovaRebootServer", "cn-north-4"),
                        (DOC_NRB, "/v2.1/{p}/servers/{id}/action", "post", op))
    return ToolService(store=store,
                       config=ServiceConfig(deprecated_index=index,
                                            deprecated_mode=mode))


def test_mode_off_is_status_quo():
    out = _svc("off").list_apis("ECS")
    assert out["total"] == 3
    assert all("deprecated" not in a for a in out["apis"])


def test_service_default_config_is_off():
    out = ToolService(store=_svc("off").store).list_apis("ECS")
    assert out["total"] == 3
    assert all("deprecated" not in a for a in out["apis"])


def test_mode_annotate_structured_fields():
    out = _svc("annotate").list_apis("ECS")
    assert out["total"] == 3
    items = {a["name"]: a for a in out["apis"]}
    assert items["NovaRebootServer"]["deprecated"] is True
    assert items["NovaRebootServer"]["replacement"] == "BatchRebootServers"
    assert "deprecated" not in items["CreateServers"]
    assert "deprecated" not in items["ListServersDetails"]


def test_mode_annotate_without_replacement_only_flag():
    idx = parse_deprecated_index({"products": {"ECS": {"NovaRebootServer": {}}}})
    out = _svc("annotate", idx).list_apis("ECS")
    items = {a["name"]: a for a in out["apis"]}
    assert items["NovaRebootServer"]["deprecated"] is True
    assert "replacement" not in items["NovaRebootServer"]


def test_mode_hide_filters_and_get_api_still_works():
    svc = _svc("hide")
    out = svc.list_apis("ECS")
    assert out["total"] == 2
    assert [a["name"] for a in out["apis"]] == ["CreateServers", "ListServersDetails"]
    assert all("deprecated" not in a for a in out["apis"])
    detail = svc.get_api("ECS", "NovaRebootServer")
    assert detail["ok"] is True
    assert detail["api"] == "NovaRebootServer"


# ---------- S14d：装配默认档推导 ----------




def _args(**kw):
    base = dict(mock=True, policy=None, region=None, mock_base=None,
                mock_passthrough=None, gate=None, hints=None, audit_file=None,
                spill_dir=None, deprecated_index=None, deprecated_mode=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_build_config_deprecated_default_off(tmp_path):
    cfg = build_openapi_config(_args())
    assert cfg.deprecated_mode == "off"
    assert cfg.deprecated_index.names("ECS") == frozenset()


def test_build_config_deprecated_index_defaults_annotate(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"products": {"ECS": {"A": {}}}}', encoding="utf-8")
    cfg = build_openapi_config(_args(deprecated_index=str(p)))
    assert cfg.deprecated_mode == "annotate"
    assert cfg.deprecated_index.names("ECS") == {"a"}


def test_build_config_deprecated_mode_explicit_hide(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"products": {}}', encoding="utf-8")
    cfg = build_openapi_config(_args(deprecated_index=str(p), deprecated_mode="hide"))
    assert cfg.deprecated_mode == "hide"


def test_build_config_deprecated_mode_requires_index():
    with pytest.raises(ValueError):
        build_openapi_config(_args(deprecated_mode="hide"))


def test_build_config_deprecated_mode_invalid_raises(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"products": {}}', encoding="utf-8")
    with pytest.raises(ValueError):
        build_openapi_config(_args(deprecated_index=str(p), deprecated_mode="bogus"))


def test_build_config_deprecated_env(monkeypatch, tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"products": {"ECS": {"A": {}}}}', encoding="utf-8")
    monkeypatch.setenv("HUAWEICLOUD_MCP_DEPRECATED_INDEX", str(p))
    monkeypatch.delenv("HUAWEICLOUD_MCP_DEPRECATED_MODE", raising=False)
    cfg = build_openapi_config(_args())
    assert cfg.deprecated_mode == "annotate"
    assert cfg.deprecated_index.names("ECS") == {"a"}
