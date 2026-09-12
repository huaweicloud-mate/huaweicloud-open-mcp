"""apie 元数据功能接口：内存缓存优先 + 远端 API Explorer 回退。

service 与 api-docs CLI 的共用元数据入口；不依赖本地文件。
store 由调用方注入（ToolService 或 api-docs CLI）。
远端拉取委托 apie.explorer（注册域数据源，方言归一）。
"""

import logging
from typing import Any

from . import explorer
from .live_fallback import LiveFallback
from .memory_store import ApiHit, MemoryStore

logger = logging.getLogger("apie.catalog")


# ---------- 实时抓取（委托 explorer：注册域方言归一） ----------

def get_products(store: MemoryStore) -> list[dict[str, Any]] | None:
    products = store.products()
    if products is not None:
        return products
    try:
        live_products = explorer.fetch_products()
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
        live_apis = explorer.fetch_apis(product)
        store.set_apis(product, live_apis)
        return live_apis
    except Exception:
        logger.warning("get_apis remote fetch failed for %s", product, exc_info=True)
        return None


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
