"""apie.explorer 单元测试：注册域方言归一（fake fetcher 注入，不联网）。

独立真值：apiexplorer.cn-north-4.myhuaweicloud.com 注册接口实测响应形制——
/v3/apis/detail 与 console /new/v4/apis/detail 的 paths/definitions 逐字节一致，
仅字段名 productshort（无下划线）差异；1055 = HTTP 400
{"error_code": "APIEXPLORER.1055", "error_msg": "api信息为空"}；
/v2/apis 信封 {"count": N, "apis": [...]}（productshort 大小写不敏感）。
console /new/v3/apis 索引双源（2026-10 实测，第 7 章附录 C）：参数
product_short（下划线）、信封 api_basic_infos、method 大写；索引腿封网点 =
monkeypatch http.fetch_json_429（fetch_json_429_raise adapter 的底座）。
"""

import logging
import urllib.error
import urllib.request

import pytest

from apie import explorer

BASE = "https://apiexplorer.cn-north-4.myhuaweicloud.com"
CONSOLE_BASE = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"

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
    # console 优先：注入 fake 下宽容信封在 console URL 直接可用（注册域形
    # 镜像），分页 URL 序列为 console 形（product_short 下划线）
    assert calls[0].endswith("offset=0&limit=100&product_short=ECS")
    assert calls[1].endswith("offset=2&limit=100&product_short=ECS")


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


# ---------- fetch_apis 双源（console 优先；第 7 章实测形制独立真值） ----------

def _con_entry(name, method="POST"):
    """console v3 索引条目实测形制：8 键、method 大写、product_short 下划线。"""
    return {"id": f"c-{name}", "name": name, "method": method,
            "summary": name, "tags": "管理", "product_short": "ECS",
            "info_version": "v1", "alias_name": ""}


def test_fetch_apis_console_first_success():
    pages = {
        0: {"count": 3, "api_basic_infos": [_con_entry("A"), _con_entry("B", "GET")]},
        2: {"count": 3, "api_basic_infos": [_con_entry("C")]},
    }
    calls = []

    def fake(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        calls.append(url)
        return pages[offset]

    apis = explorer.fetch_apis("ECS", fetch_json=fake)
    # 出口归一：api_basic_infos 信封 + method 大写 → 与注册域形制一致
    assert [a["name"] for a in apis] == ["A", "B", "C"]
    assert apis[0]["product_short"] == "ECS"
    assert apis[1]["method"] == "get"
    assert calls == [
        f"{CONSOLE_BASE}?offset=0&limit=100&product_short=ECS",
        f"{CONSOLE_BASE}?offset=2&limit=100&product_short=ECS",
    ]


def test_fetch_apis_console_first_false_pins_registered():
    """管道单源：console_first=False 恒注册域（构建产物可复现性）。"""
    calls = []

    def fake(url):
        calls.append(url)
        return {"count": 1, "apis": [_entry("A")]}

    apis = explorer.fetch_apis("ECS", console_first=False, fetch_json=fake)
    assert [a["name"] for a in apis] == ["A"]
    assert calls == [f"{BASE}/v2/apis?offset=0&limit=100&productshort=ECS"]


def test_fetch_apis_console_tolerant_envelope():
    """宽容信封：console URL 上注册域信封（apis 键）同样可用——
    console 镜像注册域形制时无需回退。"""
    calls = []

    def fake(url):
        calls.append(url)
        return {"count": 1, "apis": [_entry("A")]}

    apis = explorer.fetch_apis("ECS", fetch_json=fake)
    assert [a["name"] for a in apis] == ["A"]
    assert len(calls) == 1


def test_fetch_apis_console_page_failure_falls_back_registered():
    """console 页失败（网络错）→ 该页注册域回落 → 粘性：后续页直走注册域。"""
    seq = [
        {"count": 4, "api_basic_infos": [_con_entry("A"), _con_entry("B")]},
        OSError("console hang"),                      # page1 console 失败
        {"count": 4, "apis": [_entry("C")]},          # page1 注册域回落
        {"count": 4, "apis": [_entry("D")]},          # page2 粘性：仅注册域
    ]
    calls = []

    def fake(url):
        calls.append(url)
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    apis = explorer.fetch_apis("ECS", fetch_json=fake)
    assert [a["name"] for a in apis] == ["A", "B", "C", "D"]
    hosts = ["console" if "console.huaweicloud.com" in u else "registered" for u in calls]
    assert hosts == ["console", "console", "registered", "registered"]


def test_fetch_apis_console_error_envelope_falls_back():
    """console 返回错误体/空信封 → 页级回落注册域（不抛）。"""
    seq = [
        {"error_code": "APIGW.0106"},                 # console 错误体
        {"count": 1, "apis": [_entry("A")]},          # 注册域回落成功
    ]
    calls = []

    def fake(url):
        calls.append(url)
        item = seq.pop(0)
        return item

    apis = explorer.fetch_apis("ECS", fetch_json=fake)
    assert [a["name"] for a in apis] == ["A"]
    assert "console.huaweicloud.com" in calls[0]
    assert calls[1] == f"{BASE}/v2/apis?offset=0&limit=100&productshort=ECS"


def test_fetch_apis_both_sources_fail_raises():
    """两源均终态失败 → 抛异常、零缓存（今日语义不变）。"""
    seq = [
        OSError("console down"),
        {"error_code": "X"},   # 注册域也错误体
    ]

    def fake(url):
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    with pytest.raises(ValueError):
        explorer.fetch_apis("ECS", fetch_json=fake)


def test_fetch_apis_dedupes_cross_source_duplicates(caplog):
    """跨源重复防御（ordering parity 未实测验证）：console 页失败回落注册域、
    两源窗口重叠（A 位移重发）→ (name, method) 去重 + WARNING。单源不启用。"""
    seq = [
        {"count": 3, "api_basic_infos": [_con_entry("A"), _con_entry("B")]},
        OSError("console page1 down"),                 # console 页失败 → 源切换
        {"count": 3, "apis": [_entry("A"), _entry("C")]},   # 注册域位移重发 A
    ]
    calls = []

    def fake(url):
        calls.append(url)
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    with caplog.at_level(logging.WARNING, logger="apie.explorer"):
        apis = explorer.fetch_apis("ECS", fetch_json=fake)
    assert [a["name"] for a in apis] == ["A", "B", "C"]
    hosts = ["console" if "console.huaweicloud.com" in u else "registered" for u in calls]
    assert hosts == ["console", "console", "registered"]
    assert any("duplicate" in r.message for r in caplog.records)


def test_fetch_apis_single_source_keeps_same_key_entries():
    """单源（console 全程成功）同 (name, method) 重复不去重——管道语义不变。"""
    pages = {
        0: {"count": 2, "api_basic_infos": [_con_entry("A"), dict(_con_entry("A"), id="c-A2")]},
    }
    apis = explorer.fetch_apis("ECS", fetch_json=lambda url: pages[0])
    assert [a["name"] for a in apis] == ["A", "A"]


def test_fetch_apis_console_leg_budget_and_immediate_fallback(monkeypatch):
    """不对称预算红线（Round 0 act #1）：console 腿单次尝试（retries=1、
    timeout=5）、零退避；429/504/挑战页统一立即回落注册域。经真实 adapter
    →底座→urlopen 链（monkeypatch 传输层，不打真网）。"""

    sleeps: list[float] = []
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    forwarded: list[tuple] = []


    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=30):
        url = req.full_url
        if "console.huaweicloud.com" in url:
            forwarded.append(("console", timeout))
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        forwarded.append(("registered", timeout))
        return _FakeResp(b'{"count": 1, "apis": [{"id": "i-A", "name": "A", '
                         b'"method": "POST", "summary": "A", "tags": "t", '
                         b'"productshort": "ECS"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    apis = explorer.fetch_apis("ECS")
    assert [a["name"] for a in apis] == ["A"]
    # console 腿短超时（5s）vs 注册域腿缺省（30s）；console 单次零退避
    assert forwarded == [("console", 5), ("registered", 30)]
    assert sleeps == []


def test_fetch_apis_console_challenge_page_immediate_fallback(monkeypatch):
    """console 200+HTML（挑战页形态）：JSONDecodeError → 立即回落（无退避）。"""

    sleeps: list[float] = []
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    calls: list[str] = []

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=30):
        calls.append(req.full_url)
        if "console.huaweicloud.com" in req.full_url:
            return _FakeResp(b"<html>Security Verification</html>")
        return _FakeResp(b'{"count": 1, "apis": [{"id": "i-A", "name": "A", '
                         b'"method": "POST", "summary": "A", "tags": "t", '
                         b'"productshort": "ECS"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    apis = explorer.fetch_apis("ECS")
    assert [a["name"] for a in apis] == ["A"]
    assert len(calls) == 2   # console 一次 + 注册域回落一次
    assert sleeps == []      # 挑战页不进退避梯


def test_fetch_apis_registered_429_default_fetcher(monkeypatch):
    """缺省 fetcher = fetch_json_429_raise（429 感知）：注册域腿 429 → 底座
    退避 → 重试成功。不打真网：monkeypatch urllib（系统边界），真 adapter、
    真底座、真 429 退避链全程在场——封死的是传输层而非自有模块。"""

    sleeps: list[float] = []
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    seq = [
        urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None),
        None,
    ]
    calls = []

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=30):
        calls.append(req.full_url)
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _FakeResp(b'{"count": 1, "apis": [{"id": "i-A", "name": "A", '
                         b'"method": "POST", "summary": "A", "tags": "t", '
                         b'"productshort": "ECS"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    apis = explorer.fetch_apis("ECS", console_first=False)
    assert [a["name"] for a in apis] == ["A"]
    assert len(calls) == 2
    assert sleeps == [20.0]   # 底座 429 退避首档经缺省 adapter 生效
