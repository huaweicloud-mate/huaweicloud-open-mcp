"""MemoryStore：纯内存缓存，不落盘。远端 API Explorer 为唯一数据源。"""

from collections import OrderedDict
from typing import Any

ApiHit = tuple[dict[str, Any], str, str, dict[str, Any]]  # (doc, path, method, op)


class MemoryStore:
    """纯进程内存缓存。

    三层缓存：
      _products      — 产品列表（远端一次拉取，进程存活）
      _apis          — 按产品 API 列表（{product_lower: [api_basic_info]})
      _api_details   — API 详情 LRU（(product_lower, api_name, region) → ApiHit）
    """

    def __init__(self, max_details: int = 500):
        self._products: list[dict[str, Any]] | None = None
        self._products_fetched: bool = False
        self._apis: dict[str, list[dict[str, Any]]] = {}
        self._api_details: OrderedDict[tuple[str, str, str], ApiHit | None] = OrderedDict()
        self._max_details = max_details

    def products(self) -> list[dict[str, Any]] | None:
        """返回产品列表（group 数组），未拉取时返回 None。"""
        if not self._products_fetched:
            return None
        return self._products

    def set_products(self, data: list[dict[str, Any]]) -> None:
        self._products = data
        self._products_fetched = True

    def apis(self, product: str) -> list[dict[str, Any]] | None:
        """返回指定产品的 API 列表，未拉取时返回 None。"""
        return self._apis.get(product.lower())

    def set_apis(self, product: str, data: list[dict[str, Any]]) -> None:
        self._apis[product.lower()] = data

    def find_api(self, product: str, api_name: str, region: str) -> ApiHit | None:
        """O(1) 查找 API 详情缓存（命中时刷新 LRU 位置）。"""
        key = (product.lower(), api_name, region)
        hit = self._api_details.get(key)
        if hit is not None:
            self._api_details.move_to_end(key)
        return hit

    def set_api_cache(self, key: tuple[str, str, str], hit: ApiHit | None) -> None:
        """写入 API 详情缓存（含 LRU 淘汰）。"""
        if key in self._api_details:
            self._api_details.move_to_end(key)
        else:
            self._api_details[key] = hit
            while len(self._api_details) > self._max_details:
                self._api_details.popitem(last=False)

    def clear(self) -> None:
        self._products = None
        self._products_fetched = False
        self._apis.clear()
        self._api_details.clear()
