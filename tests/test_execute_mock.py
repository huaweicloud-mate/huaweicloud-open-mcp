"""execute_api 集成测试：直连 API Explorer mock 端点。

mock 端点行为（实测确认）：
- 开放端点、无需凭证；
- HTTP 状态恒为 200；
- status_code=200 时返回与真实 API 同构的 mock 成功数据；
- status_code 为其它值时返回空 body。
"""

from apie.api_location import ApiLocation
from apie.memory_store import MemoryStore
from apie.mock import MockApiClient
from mcp_openapi.service import ServiceConfig, ToolService
from safety import policy

RULES_ALLOW_ECS = policy.parse_policy(["ECS:*=allow", "*=deny"])

FULL_DOC = {
    "swagger": "2.0",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {
                "operationId": "ListServersDetails",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string", "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/v1/{project_id}/cloudservers": {
            "post": {
                "operationId": "CreateServers",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string"},
                ],
                "responses": {"200": {"description": "OK"}},
            },
        },
    },
    "definitions": {},
}


def _service(rules):
    store = MemoryStore()
    store.set_api_cache(
        ("ecs", "ListServersDetails", "cn-north-4"),
        ApiLocation(FULL_DOC, "/v1/{project_id}/cloudservers/detail", "get",
                    FULL_DOC["paths"]["/v1/{project_id}/cloudservers/detail"]["get"]),
    )
    return ToolService(store=store,
                       config=ServiceConfig(mock=True, policy_rules=rules))


def test_mock_list_servers_details():
    resp = MockApiClient().mock_request("ECS", "ListServersDetails", "cn-north-4")
    assert resp["status"] == 200
    body = resp["body"]
    assert isinstance(body, dict)
    assert "count" in body
    assert "servers" in body


def test_mock_create_servers_returns_job_id():
    resp = MockApiClient().mock_request("ECS", "CreateServers", "cn-north-4")
    assert resp["status"] == 200
    assert "job_id" in resp["body"]


def test_mock_status_code_non_200_empty_body():
    resp = MockApiClient().mock_request("ECS", "ListServersDetails", "cn-north-4", status_code=400)
    assert resp["status"] == 200
    assert resp["body"] is None


def test_service_execute_mock_end_to_end():
    service = _service(RULES_ALLOW_ECS)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["status"] == 200
    assert "servers" in out["body"]


def test_service_execute_mock_denied():
    service = _service(policy.parse_policy(["ECS:*Show*=allow", "*=deny"]))
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert "policy" in out["reason"]


def test_service_execute_mock_end_to_end_jsonpath():
    """_jsonpath 全链（真 mock 端点）：全命中替换 body + extract 信封。"""
    service = _service(RULES_ALLOW_ECS)
    out = service.execute_api("ECS", "ListServersDetails",
                              params={"_jsonpath": "$.servers[*].id"})
    assert out["ok"] is True
    assert out["status"] == 200
    assert isinstance(out["body"], list) and out["body"]
    assert all(isinstance(v, str) for v in out["body"])
    assert out["truncated"] is True
    assert out["extract"]["note"]


def test_service_execute_mock_end_to_end_jsonpath_mapping():
    """映射形部分命中：body 保持原始结构（自纠面），投影值在 extract 信封。"""
    service = _service(RULES_ALLOW_ECS)
    out = service.execute_api("ECS", "ListServersDetails",
                              params={"_jsonpath": {"count": "$.count",
                                                    "nope": "$.nope"}})
    assert out["ok"] is True
    assert "count" in out["body"]                       # body 未被替换
    extracted = out["extract"]["extracted"]
    assert extracted["count"] is not None
    assert extracted["nope"] is None                    # 键集完整，未命中键 null
    assert out["extract"]["misses"] == ["nope: 无命中（$.nope）"]


def test_service_execute_mock_end_to_end_jsonpath_syntax_reject():
    """语法错误在 dispatch 前拒绝，端点零调用（信封即结构化 reason）。"""
    service = _service(RULES_ALLOW_ECS)
    out = service.execute_api("ECS", "ListServersDetails", params={"_jsonpath": "$.["})
    assert out["ok"] is False
    assert "表达式非法" in (out.get("reason") or "")
    assert "status" not in out
