"""apie.catalog 单元测试：store 注入 + 远端回退（monkeypatch HTTP，不联网）。"""

from openmcp.apie import catalog
from openmcp.apie.memory_store import MemoryStore

FIXTURE_GROUPS = [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器", "api_count": 2}]},
]

FIXTURE_APIS = [
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

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.get_products(store)
    assert r == FIXTURE_GROUPS
    assert calls[0] == 1
    r2 = catalog.get_products(store)
    assert r2 == FIXTURE_GROUPS
    assert calls[0] == 1  # cached


def test_get_products_remote_error(monkeypatch):
    store = _store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.get_products(store) is None


def test_get_products_cache_isolation(monkeypatch):
    s1 = _store()
    s2 = _store()
    s1.set_products(FIXTURE_GROUPS)
    assert catalog.get_products(s1) == FIXTURE_GROUPS
    # s2 is empty — remote must fail to return None
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.get_products(s2) is None


# ---------- get_apis ----------

def test_get_apis_remote_fetch(monkeypatch):
    store = _store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return {"api_basic_infos": FIXTURE_APIS, "count": len(FIXTURE_APIS)}

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.get_apis(store, "ECS")
    assert r == FIXTURE_APIS
    assert calls[0] == 1
    r2 = catalog.get_apis(store, "ECS")
    assert r2 == FIXTURE_APIS
    assert calls[0] == 1


def test_get_apis_remote_error(monkeypatch):
    store = _store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.get_apis(store, "ECS") is None


# ---------- find_api_doc ----------

def test_find_api_doc_remote_fetch(monkeypatch):
    store = _store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return dict(RAW_DETAIL)

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
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
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: {"error_code": "SOME_ERROR"})
    assert catalog.find_api_doc(store, "ECS", "ListServersDetails", "cn-north-4") is None


def test_find_api_doc_remote_error(monkeypatch):
    store = _store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    assert catalog.find_api_doc(store, "ECS", "NopeApi", "cn-north-4") is None


# ---------- get_api_counts ----------

def test_get_api_counts_from_products(monkeypatch):
    store = _store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: {"groups": FIXTURE_GROUPS})
    catalog.get_products(store)
    counts = catalog.get_api_counts(store)
    assert counts == {"ECS": 2}


def test_get_api_counts_empty_when_no_products():
    store = _store()
    assert catalog.get_api_counts(store) == {}
