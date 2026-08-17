"""ToolService 单元测试（mini 数据 + 工厂注入 stub）。"""

import json
from pathlib import Path

from huaweicloud_mcp.auth.credentials import Credentials
from huaweicloud_mcp.safety import policy
from huaweicloud_mcp.tools.service import ServiceConfig, ToolService


def _service(workdir, **kwargs) -> ToolService:
    config = ServiceConfig(data_root=Path(workdir), **kwargs)
    return ToolService(config)


def _policy(*lines):
    return policy.parse_policy(list(lines))


MINI_OPENAPI_DOC = {
    "swagger": "2.0",
    "info": {"title": "ECS - 生命周期管理", "version": "1.0"},
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "schemes": ["https"],
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {
                "operationId": "ListServersDetails",
                "summary": "查询云服务器详情列表",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string"},
                    {"name": "limit", "in": "query", "type": "integer"},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}


def _install_openapi_doc(workdir, product="ECS", fn="LifecycleManagement.json", doc=None):
    out = Path(workdir) / "data" / "openapi" / product
    out.mkdir(parents=True, exist_ok=True)
    (out / fn).write_text(json.dumps(doc or MINI_OPENAPI_DOC, ensure_ascii=False), encoding="utf-8")
    return out


# ---------- 元数据工具 ----------

def test_list_products(workdir):
    out = _service(workdir).list_products()
    assert out["total"] == 2
    assert out["products"][0]["product"] == "ECS"


def test_get_product(workdir):
    out = _service(workdir).get_product("ecs")
    assert out["ok"] is True
    assert out["name"] == "弹性云服务器"


def test_get_product_not_found(workdir):
    out = _service(workdir).get_product("NOPE")
    assert out["ok"] is False


def test_list_apis(workdir):
    out = _service(workdir).list_apis("ECS", tag="生命周期管理")
    assert out["ok"] is True
    assert out["total"] == 2


def test_get_api(workdir):
    _install_openapi_doc(workdir)
    out = _service(workdir).get_api("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["method"] == "GET"
    assert out["path"] == "/v1/{project_id}/cloudservers/detail"


def test_get_api_examples(workdir):
    _install_openapi_doc(workdir)
    out = _service(workdir).get_api_examples("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["examples"] == []


def test_load_api_doc_missing(workdir):
    assert _service(workdir).load_api_doc("ECS", "X") is None


# ---------- execute：mock 路由 ----------

class StubMockClient:
    def __init__(self):
        self.calls = []

    def mock_request(self, product, api_name, region, status_code=200, number=1):
        self.calls.append((product, api_name, region, status_code, number))
        return {"status": 200, "headers": {}, "body": {"mock": True}}


class StubHttpClient:
    def __init__(self):
        self.calls = []

    def request(self, method, host, path, query=None, body=None, headers=None):
        self.calls.append((method, host, path, query, body, headers))
        return {"status": 200, "headers": {}, "body": {"real": True}}


def test_execute_mock_routes_with_status_code(workdir):
    _install_openapi_doc(workdir)
    mock_client = StubMockClient()
    service = _service(workdir, mock=True, policy_rules=_policy("ECS:*=allow"),
                       mock_client_factory=lambda: mock_client)
    out = service.execute_api("ECS", "ListServersDetails", params={"_status_code": 400, "_number": 3})
    assert out["ok"] is True
    assert out["body"] == {"mock": True}
    assert mock_client.calls == [("ECS", "ListServersDetails", "cn-north-4", 400, 3)]


def test_execute_mock_deny_without_policy(workdir):
    _install_openapi_doc(workdir)
    mock_client = StubMockClient()
    service = _service(workdir, mock=True, mock_client_factory=lambda: mock_client)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert mock_client.calls == []


def test_execute_mock_deny_by_policy(workdir):
    _install_openapi_doc(workdir)
    mock_client = StubMockClient()
    service = _service(workdir, mock=True, policy_rules=_policy("ECS:*Show*=allow", "*=deny"),
                       mock_client_factory=lambda: mock_client)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert mock_client.calls == []


def test_execute_real_routes_with_signing(workdir):
    _install_openapi_doc(workdir)
    http_client = StubHttpClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    service = _service(workdir, policy_rules=_policy("ECS:*=allow"),
                       credentials=cred, http_client_factory=lambda: http_client)
    out = service.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert out["ok"] is True
    assert out["body"] == {"real": True}
    method, host, path, query, body, headers = http_client.calls[0]
    assert method == "GET"
    assert host == "ecs.cn-north-4.myhuaweicloud.com"
    assert path == "/v1/proj123/cloudservers/detail"
    assert query == {"limit": 1}


def test_execute_real_deny_without_policy(workdir):
    _install_openapi_doc(workdir)
    http_client = StubHttpClient()
    service = _service(workdir, http_client_factory=lambda: http_client)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert http_client.calls == []


def test_execute_audit_logs_policy_decision(workdir, caplog):
    import logging
    _install_openapi_doc(workdir)
    http_client = StubHttpClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    service = _service(workdir, policy_rules=_policy("ECS:*=allow"),
                       credentials=cred, http_client_factory=lambda: http_client)
    with caplog.at_level(logging.INFO, logger="huaweicloud_mcp.tools.execute"):
        service.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert "ECS:ListServersDetails" in caplog.text
    assert "policy=allow" in caplog.text
    assert "mode=real" in caplog.text


def test_execute_deny_is_logged(workdir, caplog):
    import logging
    _install_openapi_doc(workdir)
    service = _service(workdir, policy_rules=_policy("ECS:*Show*=allow", "*=deny"),
                       http_client_factory=lambda: StubHttpClient())
    with caplog.at_level(logging.INFO, logger="huaweicloud_mcp.tools.execute"):
        service.execute_api("ECS", "ListServersDetails")
    assert "policy=deny" in caplog.text
