"""execute_api 集成测试：直连 API Explorer mock 端点。

mock 端点行为（实测确认）：
- 开放端点、无需凭证；
- HTTP 状态恒为 200；
- status_code=200 时返回与真实 API 同构的 mock 成功数据；
- status_code 为其它值时返回空 body。
"""

import json

from openmcp.apie import catalog
from openmcp.apie.mock import MockApiClient
from openmcp.safety import policy
from openmcp.tools.service import ServiceConfig, ToolService

RULES_ALLOW_ECS = policy.parse_policy(["ECS:*=allow", "*=deny"])

MINI_OPENAPI_DOC = {
    "swagger": "2.0",
    "info": {"title": "ECS", "version": "1.0"},
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {"operationId": "ListServersDetails", "parameters": [],
                    "responses": {"200": {"description": "OK"}}},
        },
        "/v1/{project_id}/cloudservers": {
            "post": {"operationId": "CreateServers",
                     "parameters": [{"name": "project_id", "in": "path", "type": "string"}],
                     "responses": {"200": {"description": "OK"}}},
        },
    },
}


def _service(tmp_path, monkeypatch, rules):
    monkeypatch.setenv("HUAWEICLOUD_MCP_DATA_ROOT", str(tmp_path))
    catalog._stores.clear()
    out = tmp_path / "data" / "openapi" / "ECS"
    out.mkdir(parents=True)
    (out / "LifecycleManagement.json").write_text(
        json.dumps(MINI_OPENAPI_DOC, ensure_ascii=False), encoding="utf-8")
    return ToolService(ServiceConfig(mock=True, policy_rules=rules, allow_live=False))


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


def test_service_execute_mock_end_to_end(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, RULES_ALLOW_ECS)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["status"] == 200
    assert "servers" in out["body"]


def test_service_execute_mock_denied(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, policy.parse_policy(["ECS:*Show*=allow", "*=deny"]))
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert "policy" in out["reason"]
