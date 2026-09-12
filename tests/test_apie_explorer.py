"""apie.explorer 单元测试：注册域方言归一（fake fetcher 注入，不联网）。

独立真值：apiexplorer.cn-north-4.myhuaweicloud.com 注册接口实测响应形制——
/v3/apis/detail 与 console /new/v4/apis/detail 的 paths/definitions 逐字节一致，
仅字段名 productshort（无下划线）差异；1055 = HTTP 400
{"error_code": "APIEXPLORER.1055", "error_msg": "api信息为空"}；
/v2/apis 信封 {"count": N, "apis": [...]}（productshort 大小写不敏感）。
"""

import urllib.error

import pytest

from apie import explorer

BASE = "https://apiexplorer.cn-north-4.myhuaweicloud.com"

# 注册域详情响应实测形制：字段名 productshort（无下划线）
DETAIL_DOC = {
    "id": "ecda6935",
    "name": "ListServersDetails",
    "summary": "查询云服务器详情列表",
    "productshort": "ECS",
    "region_id": "cn-north-4",
    "swagger_id": "8435f023",
    "online_cli": True,
    "paths": {"/v1/{project_id}/cloudservers/detail": {
        "get": {"operationId": "ListServersDetails"}}},
    "definitions": {"X": {"type": "object"}},
}

NOT_FOUND = {"error_code": "APIEXPLORER.1055", "error_msg": "api信息为空"}


def _err(code=400):
    return urllib.error.HTTPError("u", code, "oops", None, None)


# ---------- fetch_detail ----------

def test_fetch_detail_normalizes_productshort():
    calls = []

    def fake(url):
        calls.append(url)
        return dict(DETAIL_DOC), None

    doc = explorer.fetch_detail("ECS", "ListServersDetails", "cn-north-4",
                                fetch_json=fake)
    assert doc["product_short"] == "ECS"
    assert "productshort" not in doc
    assert doc["paths"] == DETAIL_DOC["paths"]
    assert doc["swagger_id"] == "8435f023"  # 注册域多出字段透传
    assert calls == [f"{BASE}/v3/apis/detail?productshort=ECS"
                     "&name=ListServersDetails&region_id=cn-north-4"]


def test_fetch_detail_without_region_single_call():
    calls = []

    def fake(url):
        calls.append(url)
        return dict(DETAIL_DOC), None

    doc = explorer.fetch_detail("ECS", "ListServersDetails", None, fetch_json=fake)
    assert doc["product_short"] == "ECS"
    assert calls == [f"{BASE}/v3/apis/detail?productshort=ECS&name=ListServersDetails"]


def test_fetch_detail_1055_falls_back_without_region():
    calls = []

    def fake(url):
        calls.append(url)
        if "region_id" in url:
            return dict(NOT_FOUND), _err()
        return dict(DETAIL_DOC), None

    doc = explorer.fetch_detail("ECS", "ListServersDetails", "cn-north-4",
                                fetch_json=fake)
    assert doc["product_short"] == "ECS"
    assert len(calls) == 2
    assert "region_id" not in calls[1]


def test_fetch_detail_1055_twice_raises_not_found():
    def fake(url):
        return dict(NOT_FOUND), _err()

    with pytest.raises(explorer.ApiNotFoundError):
        explorer.fetch_detail("ECS", "NoSuchApi", "cn-north-4", fetch_json=fake)


def test_fetch_detail_1055_without_region_single_call():
    calls = []

    def fake(url):
        calls.append(url)
        return dict(NOT_FOUND), _err()

    with pytest.raises(explorer.ApiNotFoundError):
        explorer.fetch_detail("ECS", "NopeApi", None, fetch_json=fake)
    assert len(calls) == 1


def test_fetch_detail_http_error_raises_verbatim():
    err = _err(500)

    def fake(url):
        return {}, err

    with pytest.raises(urllib.error.HTTPError) as ei:
        explorer.fetch_detail("ECS", "NopeApi", "cn-north-4", fetch_json=fake)
    assert ei.value is err


def test_normalize_detail_keeps_other_fields():
    out = explorer._normalize_detail(dict(DETAIL_DOC))
    assert out["region_id"] == "cn-north-4"
    assert out["online_cli"] is True
    assert out["definitions"] == {"X": {"type": "object"}}


def test_normalize_detail_no_productshort_noop():
    raw = {"paths": {}, "error_code": "SOME"}
    assert explorer._normalize_detail(raw) is raw  # copy-on-write：无键不改写

# ---------- fetch_products ----------

PRODUCTS_DOC = {"groups": [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器", "api_count": 131},
        {"productshort": "GACS", "name": "GPU加速云服务器", "api_count": 0},
    ]},
    {"name": "存储", "products": [
        {"productshort": "EVS", "name": "云硬盘", "api_count": 38}]},
]}


def test_fetch_products_returns_groups():
    def fake(url):
        assert url == f"{BASE}/v4/products"
        return dict(PRODUCTS_DOC)

    assert explorer.fetch_products(fetch_json=fake) == PRODUCTS_DOC["groups"]


def test_fetch_products_error_body_raises():
    with pytest.raises(ValueError):
        explorer.fetch_products(fetch_json=lambda url: {"error_code": "X"})


# ---------- fetch_apis ----------

def _entry(name, method="POST"):
    return {"id": f"i-{name}", "name": name, "method": method,
            "summary": name, "tags": "管理", "productshort": "ECS",
            "paths": None, "region_id": "ae-ad-1"}


def test_fetch_apis_paginates_until_count():
    pages = {
        0: {"count": 3, "apis": [_entry("A"), _entry("B")]},
        2: {"count": 3, "apis": [_entry("C")]},
    }
    calls = []

    def fake(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        calls.append(url)
        return pages[offset]

    apis = explorer.fetch_apis("ECS", fetch_json=fake)
    assert [a["name"] for a in apis] == ["A", "B", "C"]
    # 归一：productshort → product_short、method 小写、冗余字段透传
    assert apis[0]["product_short"] == "ECS"
    assert apis[0]["method"] == "post"
    assert apis[0]["region_id"] == "ae-ad-1"
    assert calls[0].endswith("offset=0&limit=100&productshort=ECS")
    assert calls[1].endswith("offset=2&limit=100&productshort=ECS")


def test_fetch_apis_empty_product_single_call():
    calls = []

    def fake(url):
        calls.append(url)
        return {"count": 0, "apis": []}

    assert explorer.fetch_apis("Nope", fetch_json=fake) == []
    assert len(calls) == 1


def test_fetch_apis_missing_count_falls_to_empty_break():
    pages = {0: {"apis": [_entry("A")]}, 1: {"apis": []}}

    def fake(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        return pages[offset]

    apis = explorer.fetch_apis("ECS", fetch_json=fake)
    assert len(apis) == 1


def test_fetch_apis_error_body_raises():
    with pytest.raises(ValueError):
        explorer.fetch_apis("ECS", fetch_json=lambda url: {"error_code": "X"})


def test_fetch_apis_page_sleep_between_pages(monkeypatch):
    seen = []

    def fake_sleep(seconds):
        seen.append(seconds)

    monkeypatch.setattr(explorer.time, "sleep", fake_sleep)
    pages = {0: {"count": 3, "apis": [_entry("A"), _entry("B")]},
             2: {"count": 3, "apis": [_entry("C")]}}
    explorer.fetch_apis("ECS", page_sleep=0.3,
                        fetch_json=lambda url: pages[int(url.split("offset=")[1].split("&")[0])])
    assert seen == [0.3]  # 仅页间休眠，末页后不休眠


def test_normalize_index_batch_empty_and_missing_key():
    assert explorer._normalize_index_batch({"apis": []}) == []
    assert explorer._normalize_index_batch({}) == []


# ---------- counts ----------

def test_counts_derives_from_v4_products():
    def fake(url):
        return dict(PRODUCTS_DOC)

    doc = explorer.counts(fetch_json=fake)
    assert doc == {
        "total_api_count": 169,
        "total_products": 2,
        "groups": [
            {"product_short": "ECS", "api_count": 131},
            {"product_short": "EVS", "api_count": 38},
        ],
        "source": f"{BASE}/v4/products",
    }


def test_flatten_products_matrix():
    assert explorer._flatten_products(PRODUCTS_DOC) == [
        ("ECS", 131), ("GACS", 0), ("EVS", 38)]
    assert explorer._flatten_products({}) == []
