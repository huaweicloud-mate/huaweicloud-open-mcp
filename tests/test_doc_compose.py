"""apie.doc_compose 单测（S5 扩展，ADR-0001）：转换后文档组合根。

「转换后的 doc 必已纠偏」不变量的生产者单点——live_fallback（运行时，
缓存写入前）与离线 main() 共同委托；service 不再持有纠偏入口。
独立真值：手写 mini raw/doc + parse_metadata_corrections 交叉验证。
"""

import copy

from apie.doc_compose import compose_doc
from apie.metadata_corrections import parse_metadata_corrections

STALE = "- 该接口仅支持PostgreSQL引擎。\n- 仅支持开启实例状态是「已停止」的实例。"
REMAINDER = "- 仅支持开启实例状态是「已停止」的实例。"

RAW_2 = {
    "product_short": "RDS",
    "name": "StartupInstance",
    "swagger": "2.0",
    "host": "rds.cn-north-4.myhuaweicloud.com",
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

CORRECTIONS = parse_metadata_corrections({
    "RDS:StartupInstance": {
        "patches": {"x-constraint": {"drop": ["该接口仅支持PostgreSQL引擎"]}},
    },
})


def test_compose_doc_without_corrections_equals_convert():
    from apie.convert_openapi2 import convert_api

    assert compose_doc(copy.deepcopy(RAW_2), product="RDS", api="StartupInstance") \
        == convert_api(copy.deepcopy(RAW_2))


def test_compose_doc_applies_op_level_correction():
    doc = compose_doc(copy.deepcopy(RAW_2), product="RDS", api="StartupInstance",
                      corrections=CORRECTIONS)
    op = doc["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"]
    assert op["x-constraint"] == REMAINDER   # drop 删行，其余保留


def test_compose_doc_applies_doc_level_pointer():
    raw = {
        "product_short": "FunctionGraph",
        "name": "CreateEvent",
        "swagger": "2.0",
        "host": "fgs.cn-north-4.myhuaweicloud.com",
        "definitions": {"Body": {"properties": {
            "name": {"type": "string", "pattern": "$[a-zA-Z][a-zA-Z0-9-_]*"}}}},
        "paths": {"/v2/{project_id}/fgs/functions/{f}/events": {"post": {
            "operationId": "CreateEvent", "parameters": [],
            "responses": {"200": {"description": "OK"}}}}},
    }
    corrections = parse_metadata_corrections({
        "FunctionGraph:CreateEvent": {
            "patches": {"/definitions/Body/properties/name/pattern":
                        {"replace": "^[a-zA-Z]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$"}},
        },
    })
    doc = compose_doc(raw, product="FunctionGraph", api="CreateEvent",
                      corrections=corrections)
    assert (doc["definitions"]["Body"]["properties"]["name"]["pattern"]
            == "^[a-zA-Z]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$")


def test_compose_doc_auth_demote_threads_through():
    raw = copy.deepcopy(RAW_2)
    raw["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"][
        "parameters"] = [{"name": "X-Auth-Token", "in": "header",
                          "required": True, "type": "string"}]
    doc = compose_doc(raw, product="RDS", api="StartupInstance")
    op = doc["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"]
    auth = next(p for p in op["parameters"] if p["name"] == "X-Auth-Token")
    assert auth["required"] is False   # 默认归一：认证头 required 降级


def test_compose_doc_miss_is_inert():
    doc = compose_doc(copy.deepcopy(RAW_2), product="ECS", api="Other",
                      corrections=CORRECTIONS)
    op = doc["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"]
    assert op["x-constraint"] == STALE   # 未命中条目原样


# ---------- live_fallback 生产时点：缓存即已纠偏（ADR-0001 核心不变量） ----------

def _live_store(monkeypatch):
    from apie.memory_store import MemoryStore
    monkeypatch.setattr("apie.explorer.fetch_detail",
                        lambda product, api, region: copy.deepcopy(RAW_2))
    return MemoryStore()


def test_live_fallback_caches_corrected_doc(monkeypatch):
    from apie.live_fallback import LiveFallback

    store = _live_store(monkeypatch)
    lf = LiveFallback(store, corrections=CORRECTIONS)
    hit = lf.fetch("RDS", "StartupInstance", "cn-north-4")
    assert hit is not None
    _, _, _, op = hit
    assert op["x-constraint"] == REMAINDER
    cached = store.find_api("RDS", "StartupInstance", "cn-north-4")
    assert cached is not None
    assert cached[3]["x-constraint"] == REMAINDER   # 缓存写入前已纠偏


def test_live_fallback_without_corrections_caches_raw_converted(monkeypatch):
    from apie.live_fallback import LiveFallback

    store = _live_store(monkeypatch)
    LiveFallback(store).fetch("RDS", "StartupInstance", "cn-north-4")
    cached = store.find_api("RDS", "StartupInstance", "cn-north-4")
    assert cached[3]["x-constraint"] == STALE


def test_catalog_threads_corrections(monkeypatch):
    from apie import catalog

    store = _live_store(monkeypatch)
    hit = catalog.find_api_doc(store, "RDS", "StartupInstance", "cn-north-4",
                               corrections=CORRECTIONS)
    assert hit is not None and hit[3]["x-constraint"] == REMAINDER
