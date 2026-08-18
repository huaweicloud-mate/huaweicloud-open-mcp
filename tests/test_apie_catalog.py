"""apie.catalog 单元测试：本地存储命中/实时回退决策（不联网）。"""

import json
from pathlib import Path

from openmcp.apie import catalog

MINI_OPENAPI_DOC = {
    "swagger": "2.0",
    "info": {"title": "ECS", "version": "1.0"},
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
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

RAW_DETAIL = {
    "api_name": "ListServersDetails",
    "api_product_short": "ECS",
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

RAW_DETAIL_INVALID = {"error_code": "SOME_ERROR"}

FIXTURE_GROUPS = [
    {"name": "计算", "products": [{"productshort": "ECS", "name": "弹性云服务器"}]},
]

FIXTURE_APIS = [
    {"id": "a1", "name": "ListServers", "method": "get", "summary": "查询", "tags": "管理",
     "product_short": "ECS"},
    {"id": "a2", "name": "CreateServers", "method": "post", "summary": "创建", "tags": "管理",
     "product_short": "ECS"},
]


def _set_root(tmp_path, monkeypatch):
    monkeypatch.setenv(catalog.ENV_DATA_ROOT, str(tmp_path))
    catalog._stores.clear()


def _stub_fetch(monkeypatch, response):
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: response)


def _install_openapi(workdir, product="ECS", fn="LifecycleManagement.json"):
    out = Path(workdir) / "data" / "openapi" / product
    out.mkdir(parents=True, exist_ok=True)
    (out / fn).write_text(json.dumps(MINI_OPENAPI_DOC, ensure_ascii=False), encoding="utf-8")


# ---------- get_products ----------

def test_get_products_local_hit(workdir, monkeypatch):
    _set_root(workdir, monkeypatch)
    r = catalog.get_products()
    assert r.source == "local"
    assert r.data is not None
    assert r.data[0]["name"] == "计算"


def test_get_products_local_miss_no_live(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    r = catalog.get_products(allow_live=False)
    assert r.source == "miss"
    assert r.data is None


def test_get_products_live_fetch(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    call_count = [0]

    def _fetcher(url, **kw):
        call_count[0] += 1
        return {"groups": FIXTURE_GROUPS}

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.get_products(allow_live=True)
    assert r.source == "live"
    assert r.data == FIXTURE_GROUPS
    assert call_count[0] == 1
    r2 = catalog.get_products(allow_live=True)  # 回写缓存命中 → local
    assert r2.source == "local"
    assert r2.data == FIXTURE_GROUPS
    assert call_count[0] == 1  # 未二次 fetch


def test_get_products_live_error(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    r = catalog.get_products(allow_live=True)
    assert r.source == "miss"
    assert r.data is None


# ---------- get_apis ----------

def test_get_apis_local_hit_all(workdir, monkeypatch):
    _set_root(workdir, monkeypatch)
    r = catalog.get_apis(product=None)
    assert r.source == "local"
    assert len(r.data) == 6  # fixture has 6 apis


def test_get_apis_local_hit_product_filter(workdir, monkeypatch):
    _set_root(workdir, monkeypatch)
    r = catalog.get_apis(product="ECS")
    assert r.source == "local"
    assert len(r.data) == 4


def test_get_apis_local_miss_no_live(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    r = catalog.get_apis(product="ECS", allow_live=False)
    assert r.source == "miss"
    assert r.data is None


def test_get_apis_live_fetch_per_product(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    call_count = [0]

    def _fetcher(url, **kw):
        call_count[0] += 1
        return {"api_basic_infos": FIXTURE_APIS, "count": len(FIXTURE_APIS)}

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.get_apis(product="ECS", allow_live=True)
    assert r.source == "live"
    assert r.data == FIXTURE_APIS
    assert call_count[0] == 1
    catalog.get_apis(product="ECS", allow_live=True)
    assert call_count[0] == 1  # cached


def test_get_apis_live_full_list_not_supported(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    r = catalog.get_apis(product=None, allow_live=True)
    assert r.source == "miss"
    assert r.data is None


def test_get_apis_live_error(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    r = catalog.get_apis(product="ECS", allow_live=True)
    assert r.source == "miss"


# ---------- find_api_doc ----------

def test_find_api_doc_local_hit(workdir, monkeypatch):
    _set_root(workdir, monkeypatch)
    _install_openapi(workdir)
    r = catalog.find_api_doc("ECS", "ListServersDetails", "cn-north-4")
    assert r.source == "local"
    assert r.data is not None
    doc, path, method, op = r.data
    assert method == "get"
    assert path == "/v1/{project_id}/cloudservers/detail"


def test_find_api_doc_miss_no_live(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    r = catalog.find_api_doc("ECS", "NoApi", "cn-north-4", allow_live=False)
    assert r.source == "miss"
    assert r.data is None


def test_find_api_doc_live_fetch_and_cache(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    call_count = [0]

    def _fetcher(url, **kw):
        call_count[0] += 1
        return dict(RAW_DETAIL)

    monkeypatch.setattr(catalog.http, "fetch_json", _fetcher)
    r = catalog.find_api_doc("ECS", "ListServersDetails", "cn-north-4", allow_live=True)
    assert r.source == "live"
    assert r.data is not None
    assert call_count[0] == 1
    catalog.find_api_doc("ECS", "ListServersDetails", "cn-north-4", allow_live=True)
    assert call_count[0] == 1  # 缓存命中


def test_find_api_doc_live_invalid_raw(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: dict(RAW_DETAIL_INVALID))
    r = catalog.find_api_doc("ECS", "ListServersDetails", "cn-north-4", allow_live=True)
    assert r.source == "miss"


def test_find_api_doc_live_error(tmp_path, monkeypatch):
    _set_root(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog.http, "fetch_json", lambda url, **kw: (_ for _ in ()).throw(OSError("fail")))
    r = catalog.find_api_doc("ECS", "NoApi", "cn-north-4", allow_live=True)
    assert r.source == "miss"


# ---------- 存储隔离 ----------

def test_store_isolation_by_root(workdir, tmp_path, monkeypatch):
    _set_root(workdir, monkeypatch)
    r1 = catalog.get_products()
    assert r1.source == "local"
    _set_root(tmp_path, monkeypatch)
    r2 = catalog.get_products(allow_live=False)
    assert r2.source == "miss"
