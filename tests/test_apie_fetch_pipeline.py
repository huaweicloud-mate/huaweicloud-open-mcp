"""fetch_details/retry_failed/fetch_apis 对 explorer 委托的薄适配单测。"""

import json

from apie import explorer, fetch_apis, fetch_details, retry_failed
from common import http


def test_fetch_details_placeholder_on_not_found(monkeypatch):
    def fake(product, name, region=None, *, fetch_json=None):
        raise explorer.ApiNotFoundError("x")

    monkeypatch.setattr(fetch_details.explorer, "fetch_detail", fake)
    det = fetch_details.fetch_detail("ECS", "Nope")
    assert det == {"product_short": "ECS", "name": "Nope", "empty": True}


def test_fetch_details_passes_region(monkeypatch):
    seen = {}

    def fake(product, name, region=None, *, fetch_json=None):
        seen.update(product=product, name=name, region=region)
        return {"product_short": product, "paths": {}}

    monkeypatch.setattr(fetch_details.explorer, "fetch_detail", fake)
    det = fetch_details.fetch_detail("ECS", "ListServers")
    assert det["product_short"] == "ECS"
    assert seen["region"]  # region_paths.current_region()（env 默认 cn-north-4）


def test_retry_failed_maps_all_outcomes(monkeypatch):
    seen = {}

    def fake(product, name, region=None, *, fetch_json=None):
        seen["fetch_json"] = fetch_json
        if name == "Missing":
            raise explorer.ApiNotFoundError("x")
        if name == "Broken":
            raise OSError("boom")
        return {"product_short": product, "paths": {}}

    monkeypatch.setattr(retry_failed.explorer, "fetch_detail", fake)
    det, err = retry_failed.fetch_detail("ECS", "Ok")
    assert err is None and det["paths"] == {}
    assert seen["fetch_json"] is http.fetch_json_429  # 429 大退避档
    det, err = retry_failed.fetch_detail("ECS", "Missing")
    assert det == {"product_short": "ECS", "name": "Missing", "empty": True}
    assert err is None
    det, err = retry_failed.fetch_detail("ECS", "Broken")
    assert det == {} and isinstance(err, OSError)


def test_fetch_apis_pipeline_delegates_with_page_sleep(monkeypatch):
    seen = {}

    def fake(product, *, page_sleep=0.0, fetch_json=None):
        seen.update(product=product, page_sleep=page_sleep)
        return [{"product_short": product, "name": "A"}]

    monkeypatch.setattr(fetch_apis.explorer, "fetch_apis", fake)
    apis = fetch_apis.fetch_product("ECS")
    assert len(apis) == 1
    assert seen == {"product": "ECS", "page_sleep": 0.3}

# ---------- refresh count/products 派生阶段 ----------

def test_refresh_stage_count_derives(monkeypatch, tmp_path):
    from apie import refresh

    monkeypatch.setattr(refresh, "PROOT", str(tmp_path))
    monkeypatch.setattr(refresh.explorer, "counts", lambda: {
        "total_api_count": 131, "total_products": 1,
        "groups": [{"product_short": "ECS", "api_count": 131}],
        "source": "https://apiexplorer.cn-north-4.myhuaweicloud.com/v4/products"})
    assert refresh.stage_count(False) == 0
    doc = json.loads((tmp_path / "raw" / "apis_count.json").read_text(encoding="utf-8"))
    assert doc["groups"] == [{"product_short": "ECS", "api_count": 131}]
    assert doc["source"].endswith("/v4/products")


def test_refresh_stage_count_dry_run_no_network(monkeypatch, tmp_path):
    from apie import refresh

    monkeypatch.setattr(refresh, "PROOT", str(tmp_path))

    def boom():
        raise AssertionError("dry-run 不得访问网络")

    monkeypatch.setattr(refresh.explorer, "counts", boom)
    assert refresh.stage_count(True) == 0
    assert not (tmp_path / "raw" / "apis_count.json").exists()


def test_refresh_stage_count_error_returns_1(monkeypatch, tmp_path):
    from apie import refresh

    monkeypatch.setattr(refresh, "PROOT", str(tmp_path))

    def boom():
        raise OSError("fail")

    monkeypatch.setattr(refresh.explorer, "counts", boom)
    assert refresh.stage_count(False) == 1
    assert not (tmp_path / "raw" / "apis_count.json").exists()


def test_refresh_stage_products_writes(monkeypatch, tmp_path):
    from apie import refresh

    monkeypatch.setattr(refresh, "PROOT", str(tmp_path))
    monkeypatch.setattr(refresh.explorer, "fetch_products", lambda: [
        {"name": "计算", "products": [
            {"productshort": "ECS", "name": "弹性云服务器", "api_count": 131}]}])
    assert refresh.stage_products(False) == 0
    doc = json.loads((tmp_path / "raw" / "huawei_products.json").read_text(encoding="utf-8"))
    assert doc["total_groups"] == 1 and doc["total_products"] == 1
    assert doc["source"].endswith("/v4/products")
