"""S5：元数据纠偏 service 断言（get_api 信封 + execute 校验 + build_openapi_config）。

ADR-0001（3→1）：纠偏在生产时点落位（live_fallback/doc_compose），缓存 doc
必已纠偏——fixture 经 correct_doc 预填即模拟生产者产物；service 为纯读方。
doc 级指针纠偏段（FunctionGraph:CreateEvent 畸形 pattern）独立真值：官方帮助
文档 functiongraph_06_0133（2025-10-31 更新）regexp 与取值范围。
"""

import argparse
import json

from apie.api_location import ApiLocation
from apie.memory_store import MemoryStore
from apie.metadata_corrections import (
    MetadataCorrections,
    correct_doc,
    parse_metadata_corrections,
)
from mcp_openapi.server import build_openapi_config
from mcp_openapi.service import ServiceConfig, ToolService
from safety import policy

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
    doc = correct_doc(json.loads(json.dumps(DOC)), "RDS", "StartupInstance",
                      corrections)
    op = doc["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"]
    store.set_api_cache(("rds", "StartupInstance", "cn-north-4"),
                        ApiLocation(doc,
                                    "/v3/{project_id}/instances/{instance_id}/action/startup",
                                    "post", op))
    config = ServiceConfig(corrections=corrections)
    if hints is not None:
        config.hints = hints
    return ToolService(store=store, config=config)


# ---------- get_api：缓存 doc 已纠偏 → 信封直接反映官方口径 ----------

def test_get_api_corrects_envelope():
    out = _svc(corrections=CORRECTIONS).get_api("RDS", "StartupInstance")
    assert out["ok"] is True
    assert out["x-constraint"] == REMAINDER


def test_get_api_correction_is_producer_side():
    """纠偏不发生在 service：同一未纠偏缓存 doc，有无 corrections 配置输出一致。"""
    store = MemoryStore()
    store.set_apis("RDS", APIS_RDS)
    op = DOC["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"]
    store.set_api_cache(("rds", "StartupInstance", "cn-north-4"),
                        ApiLocation(DOC,
                                    "/v3/{project_id}/instances/{instance_id}/action/startup",
                                    "post", op))
    with_cfg = ToolService(store=store,
                           config=ServiceConfig(corrections=CORRECTIONS))
    plain = ToolService(store=store, config=ServiceConfig())
    a = with_cfg.get_api("RDS", "StartupInstance")
    b = plain.get_api("RDS", "StartupInstance")
    assert a["x-constraint"] == b["x-constraint"] == STALE


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


# ---------- doc 级指针纠偏：get_api 信封 + execute_api 校验（correct_doc_cow） ----------

BROKEN_PATTERN = "$[a-zA-Z][a-zA-Z0-9-_]*"
DOC_PATTERN_TRUTH = "^[a-zA-Z]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$"

FG_DOC = {
    "swagger": "2.0",
    "host": "fgs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "definitions": {
        "CreateEventRequestBody": {
            "required": ["content", "name"],
            "properties": {
                "name": {"type": "string", "pattern": BROKEN_PATTERN},
                "content": {"type": "string"},
            },
        },
    },
    "paths": {
        "/v2/{project_id}/fgs/functions/{function_urn}/events": {
            "post": {
                "operationId": "CreateEvent",
                "summary": "创建测试事件",
                "parameters": [
                    {"name": "function_urn", "in": "path", "required": True,
                     "type": "string"},
                    {"name": "CreateEventRequestBody", "in": "body", "required": True,
                     "schema": {"$ref": "#/definitions/CreateEventRequestBody"}},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}

APIS_FG = [{"name": "CreateEvent", "method": "post", "summary": "创建测试事件",
            "tags": "函数测试事件", "product_short": "FunctionGraph",
            "info_version": "v2"}]

FG_CORRECTIONS = parse_metadata_corrections({
    "FunctionGraph:CreateEvent": {
        "patches": {"/definitions/CreateEventRequestBody/properties/name/pattern":
                    {"replace": DOC_PATTERN_TRUTH}},
    },
})


class _StubMockClient:
    def __init__(self):
        self.calls = []

    def mock_request(self, product, api, region, status_code=None, number=None):
        self.calls.append((product, api, region, status_code, number))
        return {"status": 200, "headers": {}, "body": {"mock": True}}


def _fg_svc(corrections=MetadataCorrections.empty(), **kw):
    store = MemoryStore()
    store.set_apis("FunctionGraph", APIS_FG)
    doc = correct_doc(json.loads(json.dumps(FG_DOC)), "FunctionGraph",
                      "CreateEvent", corrections)
    op = doc["paths"]["/v2/{project_id}/fgs/functions/{function_urn}/events"]["post"]
    store.set_api_cache(
        ("functiongraph", "CreateEvent", "cn-north-4"),
        ApiLocation(doc, "/v2/{project_id}/fgs/functions/{function_urn}/events",
                    "post", op))
    return ToolService(store=store,
                       config=ServiceConfig(corrections=corrections, **kw))


def _fg_exec_svc(corrections):
    return _fg_svc(corrections=corrections, mock=True,
                   policy_rules=policy.parse_policy(["FunctionGraph:*=allow"]),
                   mock_client_factory=_StubMockClient)


def test_get_api_pointer_correction_fixes_envelope_both_spots():
    out = _fg_svc(corrections=FG_CORRECTIONS).get_api("FunctionGraph", "CreateEvent")
    assert out["ok"] is True
    body = next(p for p in out["parameters"] if p.get("in") == "body")
    assert body["schema"]["properties"]["name"]["pattern"] == DOC_PATTERN_TRUTH
    assert (out["definitions"]["CreateEventRequestBody"]["properties"]["name"]["pattern"]
            == DOC_PATTERN_TRUTH)


def test_get_api_pointer_correction_serves_corrected_doc():
    """缓存 doc 已纠偏（生产时点）：get_api 信封两处均官方口径，重复读幂等。"""
    svc = _fg_svc(corrections=FG_CORRECTIONS)
    first = svc.get_api("FunctionGraph", "CreateEvent")
    assert (first["definitions"]["CreateEventRequestBody"]["properties"]["name"]
            ["pattern"] == DOC_PATTERN_TRUTH)
    second = svc.get_api("FunctionGraph", "CreateEvent")
    assert second == first


def test_execute_api_pointer_correction_unblocks_valid_name():
    # 修复前：畸形 pattern 在 re.search 语义下永不匹配 → 一切合法 name 被本地校验拒绝
    broken = _fg_exec_svc(MetadataCorrections.empty())
    out = broken.execute_api("FunctionGraph", "CreateEvent", params={
        "body": {"name": "event-xx", "content": "eyJrIjoidiJ9"}})
    assert out["ok"] is False and "body 参数校验失败" in out["reason"]

    # 修复后：同一合法 name 通过校验到达 dispatch
    fixed = _fg_exec_svc(FG_CORRECTIONS)
    out = fixed.execute_api("FunctionGraph", "CreateEvent", params={
        "body": {"name": "event-xx", "content": "eyJrIjoidiJ9"}})
    assert out["ok"] is True and out["body"] == {"mock": True}


def test_execute_api_pointer_correction_keeps_official_constraint():
    fixed = _fg_exec_svc(FG_CORRECTIONS)
    out = fixed.execute_api("FunctionGraph", "CreateEvent", params={
        "body": {"name": "event-", "content": "eyJrIjoidiJ9"}})  # 中划线结尾：官方口径拒绝
    assert out["ok"] is False and "body 参数校验失败" in out["reason"]
    out = fixed.execute_api("FunctionGraph", "CreateEvent", params={
        "body": {"name": "-event", "content": "eyJrIjoidiJ9"}})  # 非字母开头：官方口径拒绝
    assert out["ok"] is False and "body 参数校验失败" in out["reason"]
    # 范围边界（用户确认仅修 pattern）：元数据无 maxLength，>25 字符本地不拦
    out = fixed.execute_api("FunctionGraph", "CreateEvent", params={
        "body": {"name": "x" * 26, "content": "eyJrIjoidiJ9"}})
    assert out["ok"] is True
