"""apie.local_store 单元测试：本地元数据加载与缓存（mini 数据，不联网）。"""

import json
from pathlib import Path

from openmcp.apie.local_store import LocalStore, find_api_in_doc

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


def _store(workdir) -> LocalStore:
    return LocalStore(Path(workdir))


def _install_openapi_doc(workdir, product="ECS", fn="LifecycleManagement.json",
                         region=None, doc=None):
    out = Path(workdir) / "data" / "openapi"
    if region:
        out = out / region
    out = out / product
    out.mkdir(parents=True, exist_ok=True)
    (out / fn).write_text(json.dumps(doc or MINI_OPENAPI_DOC, ensure_ascii=False),
                          encoding="utf-8")
    return out


# ---------- raw/ 索引加载 ----------

def test_load_products_apis_counts(workdir):
    store = _store(workdir)
    groups = store.products()
    assert groups is not None
    assert [g["name"] for g in groups] == ["计算", "应用中间件"]
    apis = store.apis()
    assert apis is not None
    assert len(apis) == 6
    assert apis[0]["name"] == "ListServers"
    assert store.counts() == {"ECS": 4, "RABBITMQ": 2}


def test_missing_data_returns_none(tmp_path):
    store = LocalStore(tmp_path)
    assert store.products() is None
    assert store.apis() is None
    assert store.counts() == {}


def test_load_is_lazy_and_negative_cached(tmp_path, fixture_dir):
    store = LocalStore(tmp_path)
    assert store.products() is None
    (tmp_path / "raw").mkdir()
    src = Path(fixture_dir) / "huawei_products.json"
    (tmp_path / "raw" / "huawei_products.json").write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8")
    assert store.products() is None  # 负缓存：不重新读盘
    store.clear()
    assert store.products() is not None  # clear 后重新加载


# ---------- data/openapi 文档查找与缓存 ----------

def test_find_api_hit(workdir):
    _install_openapi_doc(workdir)
    store = _store(workdir)
    hit = store.find_api("ECS", "ListServersDetails", "cn-north-4")
    assert hit is not None
    doc, path, method, op = hit
    assert method == "get"
    assert path == "/v1/{project_id}/cloudservers/detail"
    assert op["operationId"] == "ListServersDetails"
    assert doc["swagger"] == "2.0"


def test_find_api_product_case_insensitive(workdir):
    _install_openapi_doc(workdir)
    store = _store(workdir)
    assert store.find_api("ecs", "ListServersDetails", "cn-north-4") is not None


def test_find_api_positive_cache(workdir):
    _install_openapi_doc(workdir)
    store = _store(workdir)
    first = store.find_api("ECS", "ListServersDetails", "cn-north-4")
    assert first is not None
    for f in (Path(workdir) / "data" / "openapi" / "ECS").iterdir():
        f.unlink()
    second = store.find_api("ECS", "ListServersDetails", "cn-north-4")
    assert second == first  # 文件删除后仍命中缓存


def test_find_api_negative_cache_and_clear(workdir):
    store = _store(workdir)
    assert store.find_api("ECS", "ListServersDetails", "cn-north-4") is None
    _install_openapi_doc(workdir)
    assert store.find_api("ECS", "ListServersDetails", "cn-north-4") is None  # 负缓存
    store.clear()
    assert store.find_api("ECS", "ListServersDetails", "cn-north-4") is not None


def test_find_api_nondefault_region(workdir):
    _install_openapi_doc(workdir, region="cn-south-1")
    store = _store(workdir)
    assert store.find_api("ECS", "ListServersDetails", "cn-south-1") is not None
    assert store.find_api("ECS", "ListServersDetails", "cn-north-4") is None


# ---------- find_api_in_doc 匹配 ----------

def test_find_api_in_doc_exact(mini_detail):
    from openmcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    path, method, op = find_api_in_doc(doc, "ListServers")
    assert method == "get"
    assert path == "/v1/{project_id}/cloudservers"
    assert op["operationId"] == "ListServers"


def test_find_api_in_doc_case_insensitive(mini_detail):
    from openmcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    path, method, op = find_api_in_doc(doc, "listservers")
    assert op["operationId"] == "ListServers"


def test_find_api_in_doc_substring(mini_detail):
    from openmcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    path, method, op = find_api_in_doc(doc, "ListServer")
    assert op["operationId"] == "ListServers"


def test_find_api_in_doc_not_found(mini_detail):
    from openmcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    assert find_api_in_doc(doc, "NopeApi") is None


# ---------- 回写缓存 ----------

def test_set_products_overrides_missing(workdir):
    store = LocalStore(Path(workdir))
    store.products()  # 加载磁盘
    store.clear()
    store.set_products([{"name": "test_group", "products": []}])
    assert store.products() is not None
    assert store.products()[0]["name"] == "test_group"


def test_set_apis_for_and_get(workdir):
    store = LocalStore(Path(workdir))
    assert store.get_apis_for("ECS") is None
    store.set_apis_for("ECS", [{"name": "TestApi", "product_short": "ECS"}])
    assert store.get_apis_for("ecs")[0]["name"] == "TestApi"


def test_set_api_cache(workdir):
    store = LocalStore(Path(workdir))
    fake_hit = ({"swagger": "2.0"}, "/p", "get", {"operationId": "test"})
    key = ("ecs", "test", "cn-north-4")
    store.set_api_cache(key, fake_hit)
    assert store.find_api("ECS", "test", "cn-north-4") == fake_hit


def test_clear_resets_apis_live(workdir):
    store = LocalStore(Path(workdir))
    store.set_apis_for("ECS", [{"name": "X"}])
    store.set_api_cache(("ecs", "a", "cn-north-4"), None)
    store.clear()
    assert store.get_apis_for("ECS") is None
    assert ("ecs", "a", "cn-north-4") not in store._api_cache
