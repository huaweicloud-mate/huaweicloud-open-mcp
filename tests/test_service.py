"""ToolService 单元测试（stub HTTP 注入，不联网、不碰磁盘）。"""


from openmcp.apie import catalog
from openmcp.auth.credentials import Credentials
from openmcp.safety import policy
from openmcp.tools.service import ServiceConfig, ToolService

FIXTURE_GROUPS = [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器", "api_count": 4,
         "is_global": False, "link": "https://console.huaweicloud.com/ecs"},
        {"productshort": "RabbitMQ", "name": "消息队列", "api_count": 2,
         "is_global": False, "link": None},
    ]},
]

FIXTURE_APIS_ECS = [
    {"name": "ListServers", "method": "get", "summary": "查询云服务器", "tags": "生命周期管理",
     "product_short": "ECS", "info_version": "v1"},
    {"name": "CreateServers", "method": "post", "summary": "创建云服务器", "tags": "生命周期管理",
     "product_short": "ECS", "info_version": "v1"},
    {"name": "ListTags", "method": "get", "summary": "查询标签", "tags": "标签管理",
     "product_short": "ECS", "info_version": "v1"},
    {"name": "UntaggedOp", "method": "get", "summary": "无 tag", "tags": "",
     "product_short": "ECS", "info_version": "v1"},
]

RAW_DETAIL = {
    "api_name": "ListServersDetails",
    "api_product_short": "ECS",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {
                "operationId": "ListServersDetails",
                "summary": "查询云服务器详情列表",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string", "required": True},
                    {"name": "limit", "in": "query", "type": "integer"},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "info": {"version": "1.0"},
}


def _service(**kwargs) -> ToolService:
    config = ServiceConfig(**kwargs)
    return ToolService(config)


def _policy(*lines):
    return policy.parse_policy(list(lines))


def _stub_catalog(monkeypatch, fetcher):
    """Monkeypatch catalog.http.fetch_json 并重置 store。"""
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json", fetcher)


def _install_products_and_apis(monkeypatch):
    """注入 products + ECS apis 数据，供元数据工具测试。"""
    def _fetcher(url, **kw):
        if "v5/products" in url:
            return {"groups": FIXTURE_GROUPS}
        if "v3/apis" in url:
            return {"api_basic_infos": FIXTURE_APIS_ECS, "count": len(FIXTURE_APIS_ECS)}
        if "v4/apis/detail" in url:
            return dict(RAW_DETAIL)
        raise RuntimeError(f"Unexpected URL: {url}")

    _stub_catalog(monkeypatch, _fetcher)


def _install_detail_only(monkeypatch):
    """仅注入接口详情数据。"""
    def _fetcher(url, **kw):
        if "v4/apis/detail" in url:
            return dict(RAW_DETAIL)
        raise RuntimeError(f"Unexpected URL: {url}")

    _stub_catalog(monkeypatch, _fetcher)


# ---------- 元数据工具 ----------

def test_list_products(monkeypatch):
    _install_products_and_apis(monkeypatch)
    out = _service().list_products()
    assert out["total"] == 2
    assert out["products"][0]["product"] == "ECS"


def test_get_product(monkeypatch):
    _install_products_and_apis(monkeypatch)
    out = _service().get_product("ecs")
    assert out["ok"] is True
    assert out["name"] == "弹性云服务器"


def test_get_product_not_found(monkeypatch):
    _install_products_and_apis(monkeypatch)
    out = _service().get_product("NOPE")
    assert out["ok"] is False


def test_list_apis(monkeypatch):
    _install_products_and_apis(monkeypatch)
    out = _service().list_apis("ECS", tag="生命周期管理")
    assert out["ok"] is True
    assert out["total"] == 2


def test_get_api(monkeypatch):
    _install_detail_only(monkeypatch)
    out = _service().get_api("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["method"] == "GET"
    assert out["path"] == "/v1/{project_id}/cloudservers/detail"


def test_get_api_examples(monkeypatch):
    _install_detail_only(monkeypatch)
    out = _service().get_api_examples("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["examples"] == []


def test_load_api_doc_missing(monkeypatch):
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: {"error_code": "NOT_FOUND"})
    assert _service().load_api_doc("ECS", "X") is None


def test_metadata_tools_are_logged(monkeypatch, caplog):
    import logging
    _install_products_and_apis(monkeypatch)
    service = _service()
    with caplog.at_level(logging.INFO, logger="openmcp.tools.service"):
        service.list_products(keyword="云")
        service.get_product("ECS")
        service.list_apis("ECS", tag="生命周期管理", limit=5, offset=1)
        service.get_api("ECS", "ListServersDetails")
        service.get_api_examples("ECS", "ListServersDetails")
    assert "list_products category=- keyword=云" in caplog.text
    assert "get_product product=ECS" in caplog.text
    assert "list_apis product=ECS tag=生命周期管理 search=- limit=5 offset=1" in caplog.text
    assert "get_api ECS:ListServersDetails region=cn-north-4" in caplog.text
    assert "get_api_examples ECS:ListServersDetails region=cn-north-4" in caplog.text


def test_metadata_not_found_is_logged(monkeypatch, caplog):
    import logging
    _install_products_and_apis(monkeypatch)
    service = _service()
    with caplog.at_level(logging.WARNING, logger="openmcp.tools.service"):
        service.get_product("NOPE")
        service.get_api("ECS", "Nope")
    assert "get_product product=NOPE result=not_found" in caplog.text
    assert "get_api ECS:Nope region=cn-north-4 result=not_found" in caplog.text


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


def test_execute_mock_routes_with_status_code(monkeypatch):
    _install_detail_only(monkeypatch)
    mock_client = StubMockClient()
    service = _service(mock=True, policy_rules=_policy("ECS:*=allow"),
                       mock_client_factory=lambda: mock_client)
    out = service.execute_api("ECS", "ListServersDetails", params={"_status_code": 400, "_number": 3})
    assert out["ok"] is True
    assert out["body"] == {"mock": True}
    assert mock_client.calls == [("ECS", "ListServersDetails", "cn-north-4", 400, 3)]


def test_execute_mock_deny_without_policy(monkeypatch):
    _install_detail_only(monkeypatch)
    mock_client = StubMockClient()
    service = _service(mock=True, mock_client_factory=lambda: mock_client)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert mock_client.calls == []


def test_execute_mock_deny_by_policy(monkeypatch):
    _install_detail_only(monkeypatch)
    mock_client = StubMockClient()
    service = _service(mock=True, policy_rules=_policy("ECS:*Show*=allow", "*=deny"),
                       mock_client_factory=lambda: mock_client)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert mock_client.calls == []


def test_execute_real_routes_with_signing(monkeypatch):
    _install_detail_only(monkeypatch)
    http_client = StubHttpClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    service = _service(policy_rules=_policy("ECS:*=allow"),
                       credentials=cred, http_client_factory=lambda: http_client)
    out = service.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert out["ok"] is True
    assert out["body"] == {"real": True}
    method, host, path, query, body, headers = http_client.calls[0]
    assert method == "GET"
    assert "ecs" in host
    assert path == "/v1/proj123/cloudservers/detail"
    assert query == {"limit": 1}


def test_execute_real_deny_without_policy(monkeypatch):
    _install_detail_only(monkeypatch)
    http_client = StubHttpClient()
    service = _service(http_client_factory=lambda: http_client)
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert http_client.calls == []


def test_execute_audit_logs_policy_decision(monkeypatch, caplog):
    import logging
    _install_detail_only(monkeypatch)
    http_client = StubHttpClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    service = _service(policy_rules=_policy("ECS:*=allow"),
                       credentials=cred, http_client_factory=lambda: http_client)
    with caplog.at_level(logging.INFO, logger="openmcp.tools.execute"):
        service.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert "ECS:ListServersDetails" in caplog.text
    assert "policy=allow" in caplog.text
    assert "mode=real" in caplog.text


def test_execute_deny_is_logged(monkeypatch, caplog):
    import logging
    _install_detail_only(monkeypatch)
    service = _service(policy_rules=_policy("ECS:*Show*=allow", "*=deny"),
                       http_client_factory=lambda: StubHttpClient())
    with caplog.at_level(logging.INFO, logger="openmcp.tools.execute"):
        service.execute_api("ECS", "ListServersDetails")
    assert "policy=deny" in caplog.text
