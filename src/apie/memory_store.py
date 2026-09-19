"""MemoryStore：纯内存缓存，不落盘。远端 API Explorer 为唯一数据源。"""

import threading
from collections import OrderedDict
from typing import Any

from .api_location import ApiLocation

ApiHit = ApiLocation  # 历史别名：API 详情缓存条目 = 一个 ApiLocation


class MemoryStore:
    """纯进程内存缓存。

    三层缓存：
      _products      — 产品列表（远端一次拉取，进程存活）
      _apis          — 按产品 API 列表（{product_lower: [api_basic_info]})
      _api_details   — API 详情 LRU（(product_lower, api_name, region) → ApiLocation）

    线程安全（S23）：MCP sync 工具经 anyio.to_thread 并发派发，且 corrections
    热刷新联动新增了运行期失效写源（clear_api_details）——每方法各自持锁
    per-op 串行化，锁绝不跨网络 I/O 持有（网络发生在 catalog 层锁外）。
    """

    def __init__(self, max_details: int = 500):
        self._products: list[dict[str, Any]] | None = None
        self._products_fetched: bool = False
        self._apis: dict[str, list[dict[str, Any]]] = {}
        self._api_details: OrderedDict[tuple[str, str, str], ApiLocation | None] = OrderedDict()
        self._max_details = max_details
        self._lock = threading.Lock()

    def products(self) -> list[dict[str, Any]] | None:
        """返回产品列表（group 数组），未拉取时返回 None。"""
        with self._lock:
            if not self._products_fetched:
                return None
            return self._products

    def set_products(self, data: list[dict[str, Any]]) -> None:
        with self._lock:
            self._products = data
            self._products_fetched = True

    def apis(self, product: str) -> list[dict[str, Any]] | None:
        """返回指定产品的 API 列表，未拉取时返回 None。"""
        with self._lock:
            return self._apis.get(product.lower())

    def set_apis(self, product: str, data: list[dict[str, Any]]) -> None:
        with self._lock:
            self._apis[product.lower()] = data

    def find_api(self, product: str, api_name: str, region: str) -> ApiLocation | None:
        """O(1) 查找 API 详情缓存（命中时刷新 LRU 位置）。"""
        key = (product.lower(), api_name, region)
        with self._lock:
            hit = self._api_details.get(key)
            if hit is not None:
                self._api_details.move_to_end(key)
            return hit

    def set_api_cache(self, key: tuple[str, str, str],
                      hit: ApiLocation | None) -> None:
        """写入 API 详情缓存（含 LRU 淘汰）。"""
        with self._lock:
            if key in self._api_details:
                self._api_details.move_to_end(key)
            else:
                self._api_details[key] = hit
                while len(self._api_details) > self._max_details:
                    self._api_details.popitem(last=False)

    def clear_api_details(self) -> None:
        """定向失效：仅清 API 详情 LRU（S23 corrections 热刷新联动口）。

        products/apis 列表与纠偏口径无关，保持缓存——避免发现链工具的可用性
        被绑到 API Explorer 在线状态。
        """
        with self._lock:
            self._api_details.clear()

    def clear(self) -> None:
        with self._lock:
            self._products = None
            self._products_fetched = False
            self._apis.clear()
            self._api_details.clear()
