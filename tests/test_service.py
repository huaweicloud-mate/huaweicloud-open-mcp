"""ToolService 单元测试（store 注入，不联网、不碰磁盘）。"""

from apie.memory_store import MemoryStore
from common.auth import Credentials
from mcp_openapi.gate import parse_gate
from mcp_openapi.service import ServiceConfig, ToolService
from safety import policy

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

FULL_DOC = {
    "swagger": "2.0",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
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
    "definitions": {},
}


def _policy(*lines):
    return policy.parse_policy(list(lines))


def _prep_store(products=True, apis=True, detail=True):
    """构建预填充的 MemoryStore，免 monkeypatch。"""
    store = MemoryStore()
    if products:
        store.set_products(FIXTURE_GROUPS)
    if apis:
        store.set_apis("ECS", FIXTURE_APIS_ECS)
    if detail:
        store.set_api_cache(
            ("ecs", "ListServersDetails", "cn-north-4"),
            (FULL_DOC, "/v1/{project_id}/cloudservers/detail", "get",
             FULL_DOC["paths"]["/v1/{project_id}/cloudservers/detail"]["get"]),
        )
    return store


# ---------- 元数据工具 ----------

def test_list_products():
    store = _prep_store(detail=False)
    out = ToolService(store=store).list_products()
    assert out["total"] == 2
    assert out["products"][0]["product"] == "ECS"


def test_get_product():
    store = _prep_store(detail=False)
    out = ToolService(store=store).get_product("ecs")
    assert out["ok"] is True
    assert out["name"] == "弹性云服务器"


def test_get_product_not_found():
    store = _prep_store(detail=False)
    out = ToolService(store=store).get_product("NOPE")
    assert out["ok"] is False


def test_list_apis():
    store = _prep_store(detail=False)
    out = ToolService(store=store).list_apis("ECS", tag="生命周期管理")
    assert out["ok"] is True
    assert out["total"] == 2


def test_get_api():
    store = _prep_store(products=False, apis=False)
    out = ToolService(store=store).get_api("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["method"] == "GET"
    assert out["path"] == "/v1/{project_id}/cloudservers/detail"


def test_get_api_examples():
    store = _prep_store(products=False, apis=False)
    out = ToolService(store=store).get_api_examples("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["examples"] == []


def test_load_api_doc_missing():
    store = MemoryStore()
    assert ToolService(store=store).load_api_doc("ECS", "X") is None


def test_metadata_tools_are_logged(caplog):
    import logging
    store = _prep_store()
    service = ToolService(store=store)
    with caplog.at_level(logging.INFO, logger="mcp_openapi.service"):
        service.list_products(keyword="云")
        service.get_product("ECS")
        service.list_apis("ECS", tag="生命周期管理", limit=5, offset=1)
        service.get_api("ECS", "ListServersDetails")
        service.get_api_examples("ECS", "ListServersDetails")
    assert "list_products category=- keyword=云" in caplog.text
    assert "get_product product=ECS" in caplog.text
    assert ("list_apis product=ECS tag=生命周期管理 search=- limit=5 offset=1"
            in caplog.text)
    assert "get_api ECS:ListServersDetails region=cn-north-4" in caplog.text
    assert "get_api_examples ECS:ListServersDetails region=cn-north-4" in caplog.text


def test_metadata_not_found_is_logged(caplog):
    import logging
    store = _prep_store()
    service = ToolService(store=store)
    with caplog.at_level(logging.WARNING, logger="mcp_openapi.service"):
        service.get_product("NOPE")
        service.get_api("ECS", "Nope")
    assert "get_product product=NOPE result=not_found" in caplog.text
    assert "get_api ECS:Nope region=cn-north-4 result=not_found" in caplog.text


# ---------- execute ----------

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


class StubObsClient:
    def __init__(self):
        self.calls = []

    def request(self, method, host, *, bucket, object_key="",
                query=None, headers=None, body=None):
        self.calls.append((method, host, bucket, object_key, query, headers, body))
        return {"status": 200, "headers": {}, "body": {"obs": True}}


OBS_DOC = {
    "swagger": "2.0",
    "host": "obs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "paths": {
        "/{object_key}": {
            "get": {
                "operationId": "GetObject",
                "parameters": [
                    {"name": "bucket_name", "in": "query", "type": "string"},
                    {"name": "object_key", "in": "path", "type": "string"},
                    {"name": "versionId", "in": "query", "type": "string"},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "definitions": {},
}


def test_execute_mock_routes_with_status_code():
    store = _prep_store(products=False, apis=False)
    mock_client = StubMockClient()
    service = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_rules=_policy("ECS:*=allow"),
        mock_client_factory=lambda: mock_client))
    out = service.execute_api("ECS", "ListServersDetails",
                              params={"_status_code": 400, "_number": 3})
    assert out["ok"] is True
    assert out["body"] == {"mock": True}
    assert mock_client.calls == [("ECS", "ListServersDetails", "cn-north-4", 400, 3)]


def test_execute_mock_deny_without_policy():
    store = _prep_store(products=False, apis=False)
    mock_client = StubMockClient()
    service = ToolService(store=store, config=ServiceConfig(
        mock=True, mock_client_factory=lambda: mock_client))
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert mock_client.calls == []


def test_execute_mock_deny_by_policy():
    store = _prep_store(products=False, apis=False)
    mock_client = StubMockClient()
    service = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_rules=_policy("ECS:*Show*=allow", "*=deny"),
        mock_client_factory=lambda: mock_client))
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert mock_client.calls == []


def test_execute_real_routes_with_signing():
    store = _prep_store(products=False, apis=False)
    http_client = StubHttpClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    service = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("ECS:*=allow"),
        credentials=cred, http_client_factory=lambda: http_client))
    out = service.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert out["ok"] is True
    assert out["body"] == {"real": True}
    method, host, path, query, body, headers = http_client.calls[0]
    assert method == "GET"
    assert "ecs" in host
    assert path == "/v1/proj123/cloudservers/detail"
    assert query == {"limit": 1}


def _prep_obs_store():
    store = MemoryStore()
    store.set_products(FIXTURE_GROUPS)
    store.set_apis("OBS", [])
    store.set_api_cache(
        ("obs", "GetObject", "cn-north-4"),
        (OBS_DOC, "/{object_key}", "get",
         OBS_DOC["paths"]["/{object_key}"]["get"]),
    )
    return store


def test_execute_obs_routes_to_obs_lane():
    store = _prep_obs_store()
    obs_client = StubObsClient()
    cred = Credentials(ak="AK", sk="SK")
    service = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("OBS:*=allow"),
        credentials=cred, obs_client_factory=lambda: obs_client))
    out = service.execute_api("OBS", "GetObject",
                              params={"bucket_name": "b", "object_key": "o.txt"})
    assert out["ok"] is True
    assert out["body"] == {"obs": True}
    method, host, bucket, object_key, query, headers, body = obs_client.calls[0]
    assert method == "GET"
    assert host == "obs.cn-north-4.myhuaweicloud.com"
    assert bucket == "b"
    assert object_key == "o.txt"


def test_execute_obs_deny_without_policy():
    store = _prep_obs_store()
    obs_client = StubObsClient()
    service = ToolService(store=store, config=ServiceConfig(
        obs_client_factory=lambda: obs_client))
    out = service.execute_api("OBS", "GetObject")
    assert out["ok"] is False
    assert obs_client.calls == []


def test_execute_real_deny_without_policy():
    store = _prep_store(products=False, apis=False)
    http_client = StubHttpClient()
    service = ToolService(store=store, config=ServiceConfig(
        http_client_factory=lambda: http_client))
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert http_client.calls == []


def test_execute_audit_logs_policy_decision(caplog):
    import logging
    store = _prep_store(products=False, apis=False)
    http_client = StubHttpClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    service = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("ECS:*=allow"),
        credentials=cred, http_client_factory=lambda: http_client))
    with caplog.at_level(logging.INFO, logger="mcp_openapi.service"):
        service.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert "ECS:ListServersDetails" in caplog.text
    assert "policy=allow" in caplog.text


def test_execute_deny_is_logged(caplog):
    import logging
    store = _prep_store(products=False, apis=False)
    service = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("ECS:*Show*=allow", "*=deny"),
        http_client_factory=lambda: StubHttpClient()))
    with caplog.at_level(logging.INFO, logger="mcp_openapi.service"):
        service.execute_api("ECS", "ListServersDetails")
    assert "policy=deny" in caplog.text


# ---------- 产品门栓（gate） ----------

def test_list_products_filters_gated():
    store = _prep_store(detail=False)
    svc = ToolService(store=store, config=ServiceConfig(gate=parse_gate(["ECS"])))
    out = svc.list_products()
    assert out["ok"] is True
    assert [p["product"] for p in out["products"]] == ["ECS"]


def test_get_product_gated_denied():
    store = _prep_store(detail=False)
    svc = ToolService(store=store, config=ServiceConfig(gate=parse_gate(["ECS"])))
    out = svc.get_product("RabbitMQ")
    assert out["ok"] is False
    assert out["reason"] == "产品 RabbitMQ 不在 openapi mcp 授权范围内"


def test_list_apis_gated_denied():
    store = _prep_store(detail=False)
    svc = ToolService(store=store, config=ServiceConfig(gate=parse_gate(["ECS"])))
    out = svc.list_apis("VPC")
    assert out["ok"] is False
    assert "不在 openapi mcp 授权范围内" in out["reason"]


def test_get_api_gated_denied():
    store = _prep_store(products=False, apis=False)
    svc = ToolService(store=store, config=ServiceConfig(gate=parse_gate(["ECS"])))
    out = svc.get_api("VPC", "ListVpcs")
    assert out["ok"] is False
    assert "不在 openapi mcp 授权范围内" in out["reason"]


def test_get_api_examples_gated_denied():
    store = _prep_store(products=False, apis=False)
    svc = ToolService(store=store, config=ServiceConfig(gate=parse_gate(["ECS"])))
    out = svc.get_api_examples("VPC", "ListVpcs")
    assert out["ok"] is False
    assert "不在 openapi mcp 授权范围内" in out["reason"]


def test_execute_gated_denied_even_when_policy_allows():
    store = _prep_store(products=False, apis=False)
    http = StubHttpClient()
    svc = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("VPC:*=allow"),
        gate=parse_gate(["ECS"]),
        http_client_factory=lambda: http))
    out = svc.execute_api("VPC", "ListVpcs")
    assert out["ok"] is False
    assert out["reason"] == "产品 VPC 不在 openapi mcp 授权范围内"
    assert http.calls == []
