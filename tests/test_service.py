"""ToolService 单元测试（store 注入，不联网、不碰磁盘）。"""

import pytest

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
    """非名单 OBS 接口（ListObjects，控制面）保持 gateway 直连执行。"""
    store = _prep_obs_store()
    obs_client = StubObsClient()
    cred = Credentials(ak="AK", sk="SK")
    service = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("OBS:*=allow"),
        credentials=cred, obs_client_factory=lambda: obs_client))
    out = service.execute_api("OBS", "ListObjects",
                              params={"bucket_name": "b"})
    assert out["ok"] is True
    assert out["body"] == {"obs": True}
    method, host, bucket, object_key, query, headers, body = obs_client.calls[0]
    assert method == "GET"
    assert host == "obs.cn-north-4.myhuaweicloud.com"
    assert bucket == "b"
    assert object_key == ""


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


# ---------- manage_policy / policy 热重载（S2b 服务层集成） ----------

def _policy_file(tmp_path, entries):
    import json
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return p


def test_manage_policy_grant_takes_effect_without_restart(tmp_path):
    """拒 → add（默认会话内）→ 同一 service 实例立即放行；文件不动，重启等价不可见。"""
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["*=deny"])
    store = MemoryStore()
    mock_client = StubMockClient()
    svc = ToolService(store=store, config=ServiceConfig(
        mock=True,
        policy_store=PolicyStore(str(p)),
        mock_client_factory=lambda: mock_client))
    before = p.read_text(encoding="utf-8")

    out = svc.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in (out.get("reason") or "")

    res = svc.manage_policy("add", "ECS:*=allow")
    assert res["ok"] is True
    assert res["scope"] == "session"

    out2 = svc.execute_api("ECS", "ListServersDetails", params={"_status_code": 200})
    assert out2["ok"] is True                       # 无需重建 service / 重启 server
    assert mock_client.calls == [("ECS", "ListServersDetails", "cn-north-4", 200, 1)]

    assert p.read_text(encoding="utf-8") == before   # 会话内不落盘
    other = ToolService(config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=lambda: StubMockClient()))
    assert other.execute_api("ECS", "ListServersDetails")["ok"] is False  # 重启等价：不可见


def test_manage_policy_remove_revokes(tmp_path):
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["ECS:*=allow"])
    mock_client = StubMockClient()
    svc = ToolService(config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=lambda: mock_client))
    assert svc.execute_api("ECS", "ListServersDetails")["ok"] is True

    res = svc.manage_policy("remove", "ECS:*=allow")
    assert res["ok"] is True
    out = svc.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in (out.get("reason") or "")


def test_manage_policy_list_and_errors(tmp_path):
    from safety.policy_store import PolicyStore

    svc_none = ToolService(ServiceConfig())
    out = svc_none.manage_policy("list")
    assert out["ok"] is False and "--policy" in out["reason"]

    path = tmp_path / "p.json"
    path.write_text('["ECS:*List*=allow"]', encoding="utf-8")
    svc = ToolService(ServiceConfig(policy_store=PolicyStore(str(path))))
    listed = svc.manage_policy("list")
    assert listed["ok"] is True and "ECS:*List*=allow" in listed["policy"]
    assert listed["rules"] == [{"line": "ECS:*List*=allow",
                                "scope": "permanent", "expires_in": None}]
    assert svc.manage_policy("grant", line="ECS:*=allow")["ok"] is False   # 未知 action
    assert svc.manage_policy("add")["ok"] is False                         # 缺 line


def test_manage_policy_scope_session_default(tmp_path):
    """默认 add = 会话内：同实例立即放行，文件字节不动，新实例（重启等价）不可见。"""
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["*=deny"])
    mock_client = StubMockClient()
    svc = ToolService(config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=lambda: mock_client))
    before = p.read_text(encoding="utf-8")

    res = svc.manage_policy("add", "ECS:*=allow")
    assert res["ok"] is True and res["scope"] == "session"
    assert p.read_text(encoding="utf-8") == before          # 永不落盘
    assert svc.execute_api("ECS", "ListServersDetails",
                           params={"_status_code": 200})["ok"] is True
    other = ToolService(config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=lambda: mock_client))
    assert other.execute_api("ECS", "ListServersDetails")["ok"] is False  # 重启等价


def test_manage_policy_add_permanent_persists(tmp_path):
    """scope=permanent 落盘：文件持久化对新 store 一致可见。"""
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["*=deny"])
    svc = ToolService(config=ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))

    res = svc.manage_policy("add", "ECS:*=allow", scope="permanent")
    assert res["ok"] is True and res["scope"] == "permanent"
    import json
    assert "ECS:*=allow" in json.loads(p.read_text(encoding="utf-8"))
    other = ToolService(config=ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))
    assert policy.evaluate(other._effective_policy_rules(), "ECS", "ShowServers")


def test_manage_policy_temporary_ttl_and_list(tmp_path):
    """scope=temporary 按 ttl_seconds 过期；list 返回结构化 rules（评估序）。"""
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["*=deny"])
    svc = ToolService(config=ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))

    res = svc.manage_policy("add", "ECS:*=allow", scope="temporary", ttl_seconds=120)
    assert res["ok"] is True and res["scope"] == "temporary"
    listed = svc.manage_policy("list")
    assert listed["ok"] is True
    assert [r["scope"] for r in listed["rules"]] == ["temporary", "permanent"]
    entry = listed["rules"][0]
    assert entry["line"] == "ECS:*=allow"
    assert 0 < entry["expires_in"] <= 120


def test_manage_policy_invalid_scope_and_ttl(tmp_path):
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["*=deny"])
    svc = ToolService(config=ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))

    assert svc.manage_policy("add", "ECS:*=allow", scope="forever")["ok"] is False
    assert svc.manage_policy("add", "ECS:*=allow", ttl_seconds=60)["ok"] is False  # ttl 仅 temporary
    assert svc.manage_policy("add", "ECS:*=allow", scope="session")["ok"] is True


# ---------- _presign 预签发分支（S9f-b service 级） ----------

OBS_DOC = {
    "swagger": "2.0", "host": "obs.cn-north-4.myhuaweicloud.com", "basePath": "/",
    "paths": {
        "/{object_key}": {"get": {"operationId": "GetObject",
                                  "parameters": [
                                      {"name": "bucket_name", "in": "query",
                                       "required": True},
                                      {"name": "object_key", "in": "path",
                                       "required": True}],
                                  "responses": {"200": {"description": "OK"}}}},
        "/": {"get": {"operationId": "ListObjects",
                      "parameters": [
                          {"name": "bucket_name", "in": "query",
                           "required": True}],
                      "responses": {"200": {"description": "OK"}}}},
    },
    "definitions": {},
}


def _prep_obs_store():
    store = MemoryStore()
    store.set_products([{"name": "存储", "products": [
        {"productshort": "OBS", "name": "对象存储", "api_count": 2,
         "is_global": False, "link": None}]}])
    store.set_apis("OBS", [{"name": "GetObject", "method": "get", "summary": "下载对象",
                            "tags": "对象操作", "product_short": "OBS",
                            "info_version": "v1"},
                           {"name": "ListObjects", "method": "get", "summary": "列举对象",
                            "tags": "桶操作", "product_short": "OBS",
                            "info_version": "v1"}])
    store.set_api_cache(("obs", "GetObject", "cn-north-4"),
                        (OBS_DOC, "/{object_key}", "get",
                         OBS_DOC["paths"]["/{object_key}"]["get"]))
    store.set_api_cache(("obs", "ListObjects", "cn-north-4"),
                        (OBS_DOC, "/", "get", OBS_DOC["paths"]["/"]["get"]))
    return store


def test_presign_non_obs_product_refused():
    from common.auth.credentials import Credentials
    svc = ToolService(store=_prep_store(products=False, apis=False),
                      config=ServiceConfig(credentials=Credentials(ak="A", sk="B"),
                                           policy_rules=_policy("ECS:*=allow")))
    out = svc.execute_api("ECS", "ListServersDetails", params={"_presign": True})
    assert out["ok"] is False
    assert "_presign 仅支持 OBS" in (out.get("reason") or "")


def test_presign_flow_after_policy_grant(tmp_path):
    """拒（未授权）→ manage_policy 加规则 → 同实例立即产出预签发 URL（无网络调用）。"""
    import json as _json

    from common.auth.credentials import Credentials
    from safety.policy_store import PolicyStore

    p = tmp_path / "policy.json"
    p.write_text(_json.dumps(["*=deny"]), encoding="utf-8")
    svc = ToolService(
        store=_prep_obs_store(),
        config=ServiceConfig(policy_store=PolicyStore(str(p)),
                             credentials=Credentials(ak="CRED-AK", sk="SK-TEST")))

    out = svc.execute_api("OBS", "GetObject",
                          params={"bucket_name": "bkt", "object_key": "k.txt",
                                  "_presign": True})
    assert out["ok"] is False and "safety policy 拒绝执行" in (out.get("reason") or "")

    assert svc.manage_policy("add", "OBS:GetObject=allow")["ok"] is True

    out2 = svc.execute_api("OBS", "GetObject",
                           params={"bucket_name": "bkt", "object_key": "k.txt",
                                   "_presign": True, "_presign_expires": 300})
    assert out2["ok"] is True and out2["presign"]["method"] == "GET"
    assert out2["presign"]["expires_in"] == 300
    assert "AccessKeyId=CRED-AK&Expires=" in out2["presign"]["url"]
    assert out2["presign"]["url"].startswith("https://bkt.obs.cn-north-4.myhuaweicloud.com/k.txt?")


# ---------- 一次性授权（once，dispatch 前用后即焚） ----------

def test_execute_once_rule_allows_first_call_only(tmp_path, monkeypatch):
    """once 规则：首次 execute 放行并焚毁，二次拒绝；客户端只收到一次调用。"""
    from safety.policy_store import PolicyStore

    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)  # 封死元数据网络
    p = _policy_file(tmp_path, ["*=deny"])
    mock_client = StubMockClient()
    svc = ToolService(store=_prep_store(products=False, apis=False), config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=lambda: mock_client))
    assert svc.manage_policy("add", "ECS:*=allow", scope="once")["ok"] is True

    first = svc.execute_api("ECS", "ListServersDetails", params={"_status_code": 200})
    assert first["ok"] is True
    second = svc.execute_api("ECS", "ListServersDetails", params={"_status_code": 200})
    assert second["ok"] is False
    assert "safety policy 拒绝执行" in (second.get("reason") or "")
    assert len(mock_client.calls) == 1


def test_execute_once_rule_not_burned_by_schema_reject(tmp_path, monkeypatch):
    """参数校验失败不烧授权：自纠后重试仍放行，仅真实执行消耗 once 授权。"""
    from safety.policy_store import PolicyStore

    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)  # 封死元数据网络
    p = _policy_file(tmp_path, ["*=deny"])
    http_client = StubHttpClient()
    svc = ToolService(store=_prep_store(products=False, apis=False), config=ServiceConfig(
        policy_store=PolicyStore(str(p)),
        credentials=Credentials(ak="AK", sk="SK", project_id="proj123"),
        http_client_factory=lambda: http_client))
    assert svc.manage_policy("add", "ECS:*=allow", scope="once")["ok"] is True

    bad = svc.execute_api("ECS", "ListServersDetails", params={"limit": "abc"})
    assert bad["ok"] is False
    good = svc.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert good["ok"] is True
    exhausted = svc.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert exhausted["ok"] is False
    assert "safety policy 拒绝执行" in (exhausted.get("reason") or "")
    assert len(http_client.calls) == 1


def test_execute_once_rule_not_burned_by_missing_doc(tmp_path, monkeypatch):
    """接口未找到不烧授权（远端回退已封死：NopeApi 恒 miss）。"""
    from safety.policy_store import PolicyStore

    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)  # 封死元数据网络
    p = _policy_file(tmp_path, ["*=deny"])
    mock_client = StubMockClient()
    svc = ToolService(store=_prep_store(products=False, apis=False), config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=lambda: mock_client))
    assert svc.manage_policy("add", "ECS:*=allow", scope="once")["ok"] is True

    assert svc.execute_api("ECS", "NopeApi")["ok"] is False          # 文档未命中
    assert svc.execute_api("ECS", "ListServersDetails",
                           params={"_status_code": 200})["ok"] is True
    assert svc.execute_api("ECS", "ListServersDetails",
                           params={"_status_code": 200})["ok"] is False
    assert len(mock_client.calls) == 1


def test_execute_once_presign_branch_burns(tmp_path, monkeypatch):
    """显式 _presign 分支同样在真实预签发前消费授权；非 OBS 拒绝不焚毁。"""
    from common.auth.credentials import Credentials
    from safety.policy_store import PolicyStore

    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)  # 封死元数据网络
    p = _policy_file(tmp_path, ["*=deny"])
    svc = ToolService(
        store=_prep_obs_store(),
        config=ServiceConfig(policy_store=PolicyStore(str(p)),
                             credentials=Credentials(ak="CRED-AK", sk="SK-TEST")))
    assert svc.manage_policy("add", "OBS:GetObject=allow", scope="once")["ok"] is True

    presign = {"bucket_name": "bkt", "object_key": "k.txt", "_presign": True}
    assert svc.execute_api("OBS", "GetObject", params=presign)["ok"] is True
    out = svc.execute_api("OBS", "GetObject", params=presign)
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in (out.get("reason") or "")


def test_execute_manage_policy_misuse_guidance():
    """execute_api 误路由 manage_policy 返回引导文案，而非策略拒绝。"""
    svc = ToolService(store=MemoryStore(), config=ServiceConfig(mock=True))
    out = svc.execute_api("ECS", "manage_policy")
    assert out["ok"] is False
    assert "manage_policy" in (out.get("reason") or "")


# ---------- 对象数据面强制 presign（单口径） ----------

OBJECT_DATA_CASES = [
    ("PutObject", "put"),
    ("GetObject", "get"),
    ("AppendObject", "post"),
    ("UploadPart", "put"),
]


def _object_data_store(api: str, method: str) -> MemoryStore:
    """注册单个对象数据面接口的最小 OBS 文档。"""
    store = MemoryStore()
    store.set_products([{"name": "存储", "products": [
        {"productshort": "OBS", "name": "对象存储", "api_count": 1,
         "is_global": False, "link": None}]}])
    store.set_apis("OBS", [])
    doc = {"swagger": "2.0", "host": "obs.cn-north-4.myhuaweicloud.com",
           "basePath": "/", "definitions": {},
           "paths": {"/{object_key}": {method: {
               "operationId": api,
               "parameters": [
                   {"name": "bucket_name", "in": "query", "required": True,
                    "type": "string"},
                   {"name": "object_key", "in": "path", "required": True,
                    "type": "string"},
               ],
               "responses": {"200": {"description": "OK"}}}}}}
    store.set_api_cache(
        ("obs", api, "cn-north-4"),
        (doc, "/{object_key}", method, doc["paths"]["/{object_key}"][method]))
    return store


@pytest.mark.parametrize("api,method", OBJECT_DATA_CASES)
def test_object_data_apis_auto_presign_without_flag(api, method):
    """真实模式：名单接口不带 _presign 也自动返回预签名信封，gateway 不经手字节。"""
    obs_client = StubObsClient()
    svc = ToolService(store=_object_data_store(api, method),
                      config=ServiceConfig(
                          policy_rules=_policy("OBS:*=allow"),
                          credentials=Credentials(ak="DATA-AK", sk="SK-DATA"),
                          obs_client_factory=lambda: obs_client))
    out = svc.execute_api("OBS", api,
                          params={"bucket_name": "bkt", "object_key": "k.bin"})
    assert out["ok"] is True
    assert out["presign"]["method"] == method.upper()
    assert obs_client.calls == []
    assert out["presign"]["url"].startswith("https://bkt.obs.cn-north-4.")
    assert "AccessKeyId=DATA-AK&Expires=" in out["presign"]["url"]
    # 信封透出签名口径：缺省空 CT，headers 照抄清单为空
    assert out["presign"]["signed_content_type"] == ""
    assert out["presign"]["headers"] == {}


# ---------- schema 校验接线（mock/real 共享，OBS 跳过） ----------

def test_execute_mock_schema_reject_bad_params():
    """query 类型不符 → ok=false + 可操作 reason，mock client 未被调。"""
    store = _prep_store(products=False, apis=False)
    mock_client = StubMockClient()
    service = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_rules=_policy("ECS:*=allow"),
        mock_client_factory=lambda: mock_client))
    out = service.execute_api("ECS", "ListServersDetails", params={"limit": "100"})
    assert out["ok"] is False
    assert "limit" in (out["reason"] or "")
    assert mock_client.calls == []


def test_execute_mock_schema_reject_missing_required():
    """FULL_DOC 的路径参数 project_id 由凭证填充；构造缺 required query 的场景。"""
    store = MemoryStore()
    doc = {
        "swagger": "2.0", "host": "ecs.cn-north-4.myhuaweicloud.com", "basePath": "/",
        "definitions": {},
        "paths": {"/v1/{project_id}/servers": {"get": {
            "operationId": "ListByStatus",
            "parameters": [
                {"name": "project_id", "in": "path", "type": "string", "required": True},
                {"name": "status", "in": "query", "type": "string", "required": True,
                 "enum": ["ACTIVE", "SHUTOFF"]},
            ],
            "responses": {"200": {"description": "OK"}},
        }}},
    }
    store.set_api_cache(("ecs", "ListByStatus", "cn-north-4"),
                        (doc, "/v1/{project_id}/servers", "get", doc["paths"]["/v1/{project_id}/servers"]["get"]))
    mock_client = StubMockClient()
    service = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_rules=_policy("ECS:*=allow"),
        mock_client_factory=lambda: mock_client))
    out = service.execute_api("ECS", "ListByStatus", params={})
    assert out["ok"] is False
    assert "status" in (out["reason"] or "")
    assert mock_client.calls == []
    out = service.execute_api("ECS", "ListByStatus", params={"status": "BAD"})
    assert out["ok"] is False and "ACTIVE" in (out["reason"] or "")


def test_execute_real_schema_reject_bad_params():
    store = _prep_store(products=False, apis=False)
    http_client = StubHttpClient()
    service = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("ECS:*=allow"),
        credentials=Credentials(ak="AK", sk="SK", project_id="proj123"),
        http_client_factory=lambda: http_client))
    out = service.execute_api("ECS", "ListServersDetails", params={"limit": "many"})
    assert out["ok"] is False
    assert http_client.calls == []


def test_execute_obs_bypasses_schema_validation():
    """OBS lane（XML body/自身参数切分）不做 OpenAPI 元数据校验。"""
    store = _prep_obs_store()
    obs_client = StubObsClient()
    service = ToolService(store=store, config=ServiceConfig(
        policy_rules=_policy("OBS:*=allow"),
        credentials=Credentials(ak="AK", sk="SK", project_id="proj123"),
        obs_client_factory=lambda: obs_client))
    # versionId 声明为 query string，传 int——若误走校验将拒绝
    out = service.execute_api("OBS", "GetObject",
                              params={"bucket_name": "b", "object_key": "obj",
                                      "versionId": 3})
    assert out["ok"] is True


# ---------- policy_denial_offer（elicitation 授予提议，E1 服务层） ----------

def test_policy_denial_offer_constructed_for_policy_denial(tmp_path):
    """policy 配置且拒绝 → DenialOffer；放行 → None；拒绝 reason 与 offer 一致。"""
    from common.elicit import DenialOffer
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["*=deny"])
    svc = ToolService(config=ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))
    denial = svc.execute_api("ECS", "ListServersDetails")
    offer = svc.policy_denial_offer("ECS", "ListServersDetails",
                                    denial_reason=denial.get("reason"))
    assert isinstance(offer, DenialOffer)
    assert offer.subject == "ECS:ListServersDetails"
    assert offer.rule == "ECS:ListServersDetails=allow"
    assert offer.reason == denial["reason"]


def test_policy_denial_offer_none_when_allowed_or_unconfigured(tmp_path):
    from safety.policy import PolicyRule
    from safety.policy_store import PolicyStore

    svc = ToolService(config=ServiceConfig(mock=True,
                                           policy_rules=[PolicyRule("ECS", "*", True)]))
    assert svc.policy_denial_offer("ECS", "ListServersDetails") is None  # policy 放行

    unconfigured = ToolService(config=ServiceConfig(mock=True))  # 未配置 store（无可写文件）
    assert unconfigured.policy_denial_offer("ECS", "ListServersDetails") is None

    # 有 store 且无匹配规则（默认 deny）→ 提议存在
    p = _policy_file(tmp_path, ["ECS:*=allow"])
    stored = ToolService(config=ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))
    assert stored.policy_denial_offer("OBS", "PutObject") is not None


def test_policy_denial_offer_none_when_reason_mismatches(tmp_path):
    """门栓拒绝（reason 非 policy）→ 不提议授予。"""
    from safety.policy_store import PolicyStore

    p = _policy_file(tmp_path, ["*=deny"])
    svc = ToolService(config=ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))
    gate_denial_reason = "产品 ECS 不在 openapi mcp 授权范围内"
    assert svc.policy_denial_offer("ECS", "ListServersDetails",
                                   denial_reason=gate_denial_reason) is None
