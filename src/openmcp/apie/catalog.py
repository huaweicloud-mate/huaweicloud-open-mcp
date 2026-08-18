"""apie 元数据功能接口：本地缓存优先 + 实时回退决策。

service 与 api-docs CLI 的共用元数据入口；不暴露 store / data_root 细节，
内部通过环境变量 HUAWEICLOUD_MCP_DATA_ROOT 决定数据根（默认项目根）。
"""

import logging
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..paths import project_root
from . import http
from .live_fallback import LiveFallback
from .local_store import LocalStore

logger = logging.getLogger("openmcp.apie.catalog")

ENV_DATA_ROOT = "HUAWEICLOUD_MCP_DATA_ROOT"

BASE_PRODUCTS = "https://console.huaweicloud.com/apiexplorer/new/v5/products"
BASE_APIS = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"
PAGE_SIZE = 100

_stores: dict[str, LocalStore] = {}


def _resolve_root() -> Path:
    env = os.environ.get(ENV_DATA_ROOT)
    if env:
        return Path(env)
    return project_root()


def _get_store() -> LocalStore:
    root = str(_resolve_root())
    if root not in _stores:
        _stores[root] = LocalStore(root)
    return _stores[root]


@dataclass
class CatalogResult:
    """元数据查询结果：data + 数据来源（local/live/miss）。"""
    data: Any | None
    source: str


# ---------- 实时抓取 ----------

def _fetch_products_live() -> list[dict[str, Any]]:
    d = http.fetch_json(BASE_PRODUCTS, retries=4, backoff=2.0)
    return cast(list[dict[str, Any]], d.get("groups", []))

def _fetch_apis_live(product_short: str) -> list[dict[str, Any]]:
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

def get_products(allow_live: bool = False) -> CatalogResult:
    """产品列表。本地命中返回 local；未命中时 allow_live=True 实时拉取（回写缓存）；否则 miss。"""
    store = _get_store()
    products = store.products()
    if products is not None:
        return CatalogResult(data=products, source="local")
    if not allow_live:
        return CatalogResult(data=None, source="miss")
    try:
        live_products = _fetch_products_live()
        store.set_products(live_products)
        return CatalogResult(data=live_products, source="live")
    except Exception:
        logger.warning("get_products live fetch failed", exc_info=True)
        return CatalogResult(data=None, source="miss")


def get_apis(product: str | None = None, allow_live: bool = False) -> CatalogResult:
    """接口列表。product 非空时过滤到指定产品；本地 docs 缺失时允许按产品实时拉取（不支持全量）。"""
    store = _get_store()
    docs = store.apis()
    if docs is not None:
        apis = docs
        if product:
            p = product.lower()
            apis = [a for a in apis if (a.get("product_short") or "").lower() == p]
        return CatalogResult(data=apis, source="local")
    if product:
        live_apis = store.get_apis_for(product)
        if live_apis is not None:
            return CatalogResult(data=live_apis, source="live")
    if not allow_live or not product:
        return CatalogResult(data=None, source="miss")
    try:
        live_apis = _fetch_apis_live(product)
        store.set_apis_for(product, live_apis)
        return CatalogResult(data=live_apis, source="live")
    except Exception:
        logger.warning("get_apis live fetch failed for %s", product, exc_info=True)
        return CatalogResult(data=None, source="miss")


def get_api_counts() -> dict[str, int]:
    """接口计数表（仅磁盘）。"""
    return _get_store().counts()


def find_api_doc(product: str, api: str, region: str,
                 allow_live: bool = False) -> CatalogResult:
    """查找接口 OpenAPI 文档。本地 data/openapi 命中返回 local；
    未命中时 allow_live=True 委托 LiveFallback 适配器实时拉取并回写缓存；否则 miss。
    返回 data 为 (doc, path, method, op) 或 None。
    """
    store = _get_store()
    hit = store.find_api(product, api, region)
    if hit is not None:
        return CatalogResult(data=hit, source="local")
    if not allow_live:
        return CatalogResult(data=None, source="miss")
    try:
        fallback = LiveFallback(store)
        result = fallback.fetch(product, api, region)
        if result is not None:
            return CatalogResult(data=result, source="live")
        return CatalogResult(data=None, source="miss")
    except Exception:
        logger.warning("find_api_doc live fetch failed for %s:%s region=%s",
                       product, api, region, exc_info=True)
        return CatalogResult(data=None, source="miss")
