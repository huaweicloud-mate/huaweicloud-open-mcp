"""apie.catalog 单元测试：store 注入 + 远端回退（monkeypatch HTTP，不联网）。"""

from apie import catalog, explorer
from apie.memory_store import MemoryStore

FIXTURE_GROUPS = [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器", "api_count": 2}]},
]

# 注册域索引条目实测形制：productshort（无下划线）、method 大写
FIXTURE_APIS = [
    {"id": "a1", "name": "ListServers", "method": "GET", "summary": "查询",
     "tags": "管理", "productshort": "ECS"},
    {"id": "a2", "name": "CreateServers", "method": "POST", "summary": "创建",
     "tags": "管理", "productshort": "ECS"},
]

# explorer 归一后的出口契约
FIXTURE_APIS_NORMALIZED = [
    {"id": "a1", "name": "ListServers", "method": "get", "summary": "查询",
     "tags": "管理", "product_short": "ECS"},
    {"id": "a2", "name": "CreateServers", "method": "post", "summary": "创建",
     "tags": "管理", "product_short": "ECS"},
]

RAW_DETAIL = {
    "api_name": "ListServersDetails",
    "api_product_short": "ECS",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {
                "operationId": "ListServersDetails",
                "parameters": [],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}


def _store() -> MemoryStore:
    return MemoryStore()


# ---------- get_products ----------

def test_get_products_remote_fetch(monkeypatch):
    store = _store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return {"groups": FIXTURE_GROUPS}

    monkeypatch.setattr(explorer.http, "fetch_json", _fetcher)
    r = catalog.get_products(store)
    assert r == FIXTURE_GROUPS
    assert calls[0] == 1
    r2 = catalog.get_products(store)
    assert r2 == FIXTURE_GROUPS
    assert calls[0] == 1  # cached


def test_get_products_remote_error(monkeypatch):
    store = _store()
    monkeypatch.setattr(explorer.http, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.get_products(store) is None


def test_get_products_cache_isolation(monkeypatch):
    s1 = _store()
    s2 = _store()
    s1.set_products(FIXTURE_GROUPS)
    assert catalog.get_products(s1) == FIXTURE_GROUPS
    # s2 is empty — remote must fail to return None
    monkeypatch.setattr(explorer.http, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.get_products(s2) is None


# ---------- get_apis ----------

def test_get_apis_remote_fetch(monkeypatch):
    store = _store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return {"apis": FIXTURE_APIS, "count": len(FIXTURE_APIS)}

    monkeypatch.setattr(explorer.http, "fetch_json", _fetcher)
    r = catalog.get_apis(store, "ECS")
    assert r == FIXTURE_APIS_NORMALIZED
    assert calls[0] == 1
    r2 = catalog.get_apis(store, "ECS")
    assert r2 == FIXTURE_APIS_NORMALIZED
    assert calls[0] == 1


def test_get_apis_remote_error(monkeypatch):
    store = _store()
    monkeypatch.setattr(explorer.http, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.get_apis(store, "ECS") is None


# ---------- find_api_doc ----------

def test_find_api_doc_remote_fetch(monkeypatch):
    store = _store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return dict(RAW_DETAIL), None

    monkeypatch.setattr(explorer.http, "fetch_json_retry", _fetcher)
    hit = catalog.find_api_doc(store, "ECS", "ListServersDetails", "cn-north-4")
    assert hit is not None
    doc, path, method, op = hit
    assert method == "get"
    assert path == "/v1/{project_id}/cloudservers/detail"
    assert calls[0] == 1
    hit2 = catalog.find_api_doc(store, "ECS", "ListServersDetails", "cn-north-4")
    assert hit2 is not None
    assert calls[0] == 1  # cached


def test_find_api_doc_invalid_raw(monkeypatch):
    store = _store()
    monkeypatch.setattr(explorer.http, "fetch_json_retry",
                        lambda url, **kw: ({"error_code": "SOME_ERROR"}, None))
    assert catalog.find_api_doc(store, "ECS", "ListServersDetails", "cn-north-4") is None


def test_find_api_doc_remote_error(monkeypatch):
    store = _store()
    monkeypatch.setattr(explorer.http, "fetch_json_retry",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.find_api_doc(store, "ECS", "NopeApi", "cn-north-4") is None


# ---------- find_api_doc 认证头降级线程（auth demote，2026-09） ----------

RAW_DETAIL_AUTH = {
    "name": "ListVolumeInfo",
    "product_short": "RDS",
    "host": "rds.cn-north-4.myhuaweicloud.com",
    "paths": {
        "/v3/{project_id}/instances/{instance_id}/volumes": {
            "get": {
                "operationId": "ListVolumeInfo",
                "parameters": [
                    {"name": "x-auth-token", "in": "header",
                     "type": "string", "required": True},
                    {"name": "instance_id", "in": "path",
                     "type": "string", "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}


def _find_auth(monkeypatch, auth_demote=None):
    import copy
    store = _store()
    # 深拷贝：demote 是 in-place 改写（生产路径 raw 每次新鲜解析），
    # 测试里浅拷贝会经共享嵌套结构污染模块级 RAW_DETAIL_AUTH
    monkeypatch.setattr(explorer.http, "fetch_json_retry",
                        lambda url, **kw: (copy.deepcopy(RAW_DETAIL_AUTH), None))
    kwargs = {} if auth_demote is None else {"auth_demote": auth_demote}
    return catalog.find_api_doc(store, "RDS", "ListVolumeInfo",
                                "cn-north-4", **kwargs)


def _op_auth_required(hit):
    doc, path, method, op = hit
    return [p.get("required") for p in op["parameters"]
            if p.get("in") == "header"
            and p["name"].casefold() == "x-auth-token"][0]


def test_find_api_doc_demotes_auth_headers_by_default(monkeypatch):
    hit = _find_auth(monkeypatch)
    assert _op_auth_required(hit) is False


def test_find_api_doc_auth_demote_exempt_keeps_metadata(monkeypatch):
    from apie.convert_openapi2 import AuthDemotePolicy
    policy = AuthDemotePolicy(exempt=frozenset({("rds", "listvolumeinfo")}))
    hit = _find_auth(monkeypatch, policy)
    assert _op_auth_required(hit) is True


def test_find_api_doc_auth_demote_disabled(monkeypatch):
    from apie.convert_openapi2 import AuthDemotePolicy
    hit = _find_auth(monkeypatch, AuthDemotePolicy(enabled=False))
    assert _op_auth_required(hit) is True
