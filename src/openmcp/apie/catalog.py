"""apie 元数据功能接口：内存缓存优先 + 远端 API Explorer 回退。

service 与 api-docs CLI 的共用元数据入口；不依赖本地文件。
store 由调用方注入（ToolService 或 api-docs CLI）。
"""

import logging
import urllib.parse
from typing import Any, cast

from . import http
from .live_fallback import LiveFallback
from .memory_store import ApiHit, MemoryStore

logger = logging.getLogger("openmcp.apie.catalog")

BASE_PRODUCTS = "https://console.huaweicloud.com/apiexplorer/new/v5/products"
BASE_APIS = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"
PAGE_SIZE = 100


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

def get_products(store: MemoryStore) -> list[dict[str, Any]] | None:
    products = store.products()
    if products is not None:
        return products
    try:
        live_products = _fetch_products()
        store.set_products(live_products)
        return live_products
    except Exception:
        logger.warning("get_products remote fetch failed", exc_info=True)
        return None


def get_apis(store: MemoryStore, product: str) -> list[dict[str, Any]] | None:
    cached = store.apis(product)
    if cached is not None:
        return cached
    try:
        live_apis = _fetch_apis(product)
        store.set_apis(product, live_apis)
        return live_apis
    except Exception:
        logger.warning("get_apis remote fetch failed for %s", product, exc_info=True)
        return None


def get_api_counts(store: MemoryStore) -> dict[str, int]:
    """从已缓存的产品列表计算接口计数表。"""
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


def find_api_doc(store: MemoryStore, product: str, api: str,
                 region: str) -> ApiHit | None:
    """查找接口 OpenAPI 文档。内存缓存命中直接返回；
    未命中时远端拉取并缓存；失败返回 None。
    返回 (doc, path, method, op) 或 None。
    """
    hit = store.find_api(product, api, region)
    if hit is not None:
        return hit
    try:
        fallback = LiveFallback(store)
        result = fallback.fetch(product, api, region)
        if result is not None:
            return result
        return None
    except Exception:
        logger.warning("find_api_doc remote fetch failed for %s:%s region=%s",
                       product, api, region, exc_info=True)
        return None
