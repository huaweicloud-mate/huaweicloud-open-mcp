"""apie 元数据功能接口：内存缓存优先 + 远端 API Explorer 回退。

service 与 api-docs CLI 的共用元数据入口；不依赖本地文件。
"""

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, cast

from . import http
from .live_fallback import LiveFallback
from .memory_store import MemoryStore

logger = logging.getLogger("openmcp.apie.catalog")

BASE_PRODUCTS = "https://console.huaweicloud.com/apiexplorer/new/v5/products"
BASE_APIS = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"
PAGE_SIZE = 100

_store = MemoryStore()


def _reset_store() -> None:
    """重置 MemoryStore（测试隔离用）。"""
    global _store
    _store = MemoryStore()


# ---------- 实时抓取 ----------

def _fetch_products() -> list[dict[str, Any]]:
    d = http.fetch_json(BASE_PRODUCTS, retries=4, backoff=2.0)
    return cast(list[dict[str, Any]], d.get("groups", []))


def _fetch_apis(product_short: str) -> list[dict[str, Any]]:
    apis: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = urllib.parse.urlencode(
            {"offset": offset, "limit": PAGE_SIZE, "product_short": product_short})
        d = http.fetch_json(f"{BASE_APIS}?{params}", retries=4, backoff=2.0)
        batch = cast(list[dict[str, Any]], d.get("api_basic_infos", []))
        apis.extend(batch)
        if not batch:
            break
        offset += len(batch)
        if offset >= (d.get("count") or offset + 1):
            break
    return apis


# ---------- 公共接口 ----------

@dataclass
class CatalogResult:
    """元数据查询结果：data + 数据来源（memory/remote/miss）。"""
    data: Any | None
    source: str


def get_products() -> CatalogResult:
    store = _store
    products = store.products()
    if products is not None:
        return CatalogResult(data=products, source="memory")
    try:
        live_products = _fetch_products()
        store.set_products(live_products)
        return CatalogResult(data=live_products, source="remote")
    except Exception:
        logger.warning("get_products remote fetch failed", exc_info=True)
        return CatalogResult(data=None, source="miss")


def get_apis(product: str) -> CatalogResult:
    store = _store
    cached = store.apis(product)
    if cached is not None:
        return CatalogResult(data=cached, source="memory")
    try:
        live_apis = _fetch_apis(product)
        store.set_apis(product, live_apis)
        return CatalogResult(data=live_apis, source="remote")
    except Exception:
        logger.warning("get_apis remote fetch failed for %s", product, exc_info=True)
        return CatalogResult(data=None, source="miss")


def get_api_counts() -> dict[str, int]:
    """从内存中已缓存的 API 列表计算接口计数表。"""
    store = _store
    products = store.products()
    if products is None:
        return {}
    counts: dict[str, int] = {}
    for g in products:
        for p in g.get("products", []):
            ps = p.get("productshort")
            if ps:
                counts[ps.upper()] = p.get("api_count", 0)
    return counts


def find_api_doc(product: str, api: str, region: str) -> CatalogResult:
    """查找接口 OpenAPI 文档。内存缓存命中返回 memory；
    未命中时远端拉取并缓存；失败返回 miss。
    返回 data 为 (doc, path, method, op) 或 None。
    """
    store = _store
    hit = store.find_api(product, api, region)
    if hit is not None:
        return CatalogResult(data=hit, source="memory")
    try:
        fallback = LiveFallback(store)
        result = fallback.fetch(product, api, region)
        if result is not None:
            return CatalogResult(data=result, source="remote")
        return CatalogResult(data=None, source="miss")
    except Exception:
        logger.warning("find_api_doc remote fetch failed for %s:%s region=%s",
                       product, api, region, exc_info=True)
        return CatalogResult(data=None, source="miss")
