"""S5：元数据纠偏 service 注入与装配断言（get_api 信封 + build_openapi_config）。"""

import argparse
import json

from apie.memory_store import MemoryStore
from apie.metadata_corrections import MetadataCorrections, parse_metadata_corrections
from mcp_openapi.server import build_openapi_config
from mcp_openapi.service import ServiceConfig, ToolService

STALE = "- 该接口仅支持PostgreSQL引擎。\n- 仅支持开启实例状态是「已停止」的实例。"
REMAINDER = "- 仅支持开启实例状态是「已停止」的实例。"

DOC = {
    "swagger": "2.0",
    "host": "rds.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "definitions": {},
    "paths": {
        "/v3/{project_id}/instances/{instance_id}/action/startup": {
            "post": {
                "operationId": "StartupInstance",
                "summary": "开启实例",
                "x-constraint": STALE,
                "parameters": [],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}

APIS_RDS = [
    {"name": "StartupInstance", "method": "post", "summary": "开启实例",
     "tags": "实例管理", "product_short": "RDS", "info_version": "v3"},
]

CORRECTIONS = parse_metadata_corrections({
    "RDS:StartupInstance": {
        "evidence": "官方帮助文档 rds_05_0026 接口约束无引擎限制",
        "patches": {"x-constraint": {"drop": ["该接口仅支持PostgreSQL引擎"]}},
    },
})

HINTS_RAW = {"products": {"RDS": {"apis": {"StartupInstance": "开启提示"}}}}


def _svc(corrections=MetadataCorrections.empty(), hints=None):
    store = MemoryStore()
    store.set_apis("RDS", APIS_RDS)
    op = DOC["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"]
    store.set_api_cache(("rds", "StartupInstance", "cn-north-4"),
                        (DOC, "/v3/{project_id}/instances/{instance_id}/action/startup",
                         "post", op))
    config = ServiceConfig(corrections=corrections)
    if hints is not None:
        config.hints = hints
    return ToolService(store=store, config=config)


# ---------- get_api：信封纠偏 ----------

def test_get_api_corrects_envelope():
    out = _svc(corrections=CORRECTIONS).get_api("RDS", "StartupInstance")
    assert out["ok"] is True
    assert out["x-constraint"] == REMAINDER


def test_get_api_correction_copy_on_write_cache_untouched():
    """缓存 doc 恒不改写：纠偏后用空纠偏服务读同一 store 仍得原文。"""
    svc = _svc(corrections=CORRECTIONS)
    corrected = svc.get_api("RDS", "StartupInstance")
    assert corrected["x-constraint"] == REMAINDER
    plain = _svc().get_api("RDS", "StartupInstance")
    assert plain["x-constraint"] == STALE


def test_get_api_empty_corrections_status_quo():
    out = _svc().get_api("RDS", "StartupInstance")
    assert out["ok"] is True
    assert out["x-constraint"] == STALE


def test_get_api_miss_untouched():
    corrections = parse_metadata_corrections({"ECS:Foo": {"patches": {
        "x-constraint": {"replace": "x"}}}})
    out = _svc(corrections=corrections).get_api("RDS", "StartupInstance")
    assert out["x-constraint"] == STALE


def test_get_api_correction_and_hints_coexist():
    from mcp_openapi.hints import parse_hints

    out = _svc(corrections=CORRECTIONS,
               hints=parse_hints(HINTS_RAW)).get_api("RDS", "StartupInstance")
    assert out["ok"] is True
    assert out["x-constraint"] == REMAINDER
    assert out["hints"] == "开启提示"


def test_get_api_replace_form():
    corrections = parse_metadata_corrections({"RDS:StartupInstance": {"patches": {
        "x-constraint": {"replace": "- 官方约束。"}}}})
    out = _svc(corrections=corrections).get_api("RDS", "StartupInstance")
    assert out["x-constraint"] == "- 官方约束。"


# ---------- 装配：args/env → ServiceConfig ----------

def _args(**kw):
    base = dict(mock=True, policy=None, region=None, mock_base=None,
                mock_passthrough=None, hints=None, audit_file=None,
                spill_dir=None, deprecated_index=None, deprecated_mode=None,
                auth_demote=None, auth_demote_pass=None,
                metadata_corrections=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_build_config_corrections_default_loads_shipped():
    """真实仓库布局：缺省档加载已入库 configs/metadata-corrections.json。"""
    cfg = build_openapi_config(_args())
    assert cfg.corrections.for_api("RDS", "StartupInstance") is not None


def test_build_config_corrections_off():
    cfg = build_openapi_config(_args(metadata_corrections="off"))
    assert cfg.corrections.for_api("RDS", "StartupInstance") is None


def test_build_config_corrections_env(monkeypatch):
    monkeypatch.setenv("HUAWEICLOUD_MCP_METADATA_CORRECTIONS", "off")
    cfg = build_openapi_config(_args())
    assert cfg.corrections.for_api("RDS", "StartupInstance") is None


def test_build_config_corrections_explicit_path(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"ECS:Foo": {"patches": {
        "x-constraint": {"replace": "x"}}}}), encoding="utf-8")
    cfg = build_openapi_config(_args(metadata_corrections=str(p)))
    assert cfg.corrections.for_api("ECS", "Foo") is not None
    assert cfg.corrections.for_api("RDS", "StartupInstance") is None


def test_build_config_corrections_missing_path_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        build_openapi_config(
            _args(metadata_corrections=str(tmp_path / "missing.json")))
