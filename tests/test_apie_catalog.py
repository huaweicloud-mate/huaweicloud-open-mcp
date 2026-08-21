"""apie.catalog 单元测试：内存缓存 + 远端回退（monkeypatch HTTP，不联网）。"""

from openmcp.apie import catalog

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


# ---------- get_products ----------

def test_get_products_remote_fetch(monkeypatch):
    catalog._reset_store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return {"groups": FIXTURE_GROUPS}

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.get_products()
    assert r.source == "remote"
    assert r.data == FIXTURE_GROUPS
    assert calls[0] == 1
    r2 = catalog.get_products()
    assert r2.source == "memory"
    assert r2.data == FIXTURE_GROUPS
    assert calls[0] == 1


def test_get_products_remote_error(monkeypatch):
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    r = catalog.get_products()
    assert r.source == "miss"
    assert r.data is None


def test_get_products_cache_isolation(monkeypatch):
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: {"groups": FIXTURE_GROUPS})
    catalog.get_products()
    assert catalog.get_products().source == "memory"
    catalog._reset_store()
    assert catalog.get_products().source == "remote"


# ---------- get_apis ----------

def test_get_apis_remote_fetch(monkeypatch):
    catalog._reset_store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return {"api_basic_infos": FIXTURE_APIS, "count": len(FIXTURE_APIS)}

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.get_apis(product="ECS")
    assert r.source == "remote"
    assert r.data == FIXTURE_APIS
    assert calls[0] == 1
    r2 = catalog.get_apis(product="ECS")
    assert r2.source == "memory"
    assert calls[0] == 1


def test_get_apis_remote_error(monkeypatch):
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    r = catalog.get_apis(product="ECS")
    assert r.source == "miss"
    assert r.data is None


# ---------- find_api_doc ----------

def test_find_api_doc_remote_fetch(monkeypatch):
    catalog._reset_store()
    calls = [0]

    def _fetcher(url, **kw):
        calls[0] += 1
        return dict(RAW_DETAIL)

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.find_api_doc("ECS", "ListServersDetails", "cn-north-4")
    assert r.source == "remote"
    assert r.data is not None
    doc, path, method, op = r.data
    assert method == "get"
    assert path == "/v1/{project_id}/cloudservers/detail"
    assert calls[0] == 1
    r2 = catalog.find_api_doc("ECS", "ListServersDetails", "cn-north-4")
    assert r2.source == "memory"
    assert calls[0] == 1


def test_find_api_doc_invalid_raw(monkeypatch):
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: {"error_code": "SOME_ERROR"})
    r = catalog.find_api_doc("ECS", "ListServersDetails", "cn-north-4")
    assert r.source == "miss"
    assert r.data is None


def test_find_api_doc_remote_error(monkeypatch):
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    r = catalog.find_api_doc("ECS", "NopeApi", "cn-north-4")
    assert r.source == "miss"
    assert r.data is None


# ---------- get_api_counts ----------

def test_get_api_counts_from_products(monkeypatch):
    catalog._reset_store()
    monkeypatch.setattr(catalog.http, "fetch_json",
                        lambda url, **kw: {"groups": FIXTURE_GROUPS})
    catalog.get_products()  # populate
    counts = catalog.get_api_counts()
    assert counts == {"ECS": 2}


def test_get_api_counts_empty_when_no_products():
    catalog._reset_store()
    assert catalog.get_api_counts() == {}
