"""LiveFallback 适配器：实时拉取 API Explorer → OpenAPI 2.0 转换 → 回写缓存。

catalog 仅在本地 store 未命中时调用此适配器；适配器拥有完整的实时路径：
HTTP 抓取 → convert_openapi2 转换 → find_api_in_doc 定位 → store 缓存。
"""

import logging
from typing import Any

from . import convert_openapi2 as conv
from . import http
from .local_store import ApiHit, LocalStore, find_api_in_doc

logger = logging.getLogger("openmcp.apie.live_fallback")

BASE_DETAIL = "https://console.huaweicloud.com/apiexplorer/new/v4/apis/detail"


def _fetch_detail(product_short: str, name: str, region: str) -> dict[str, Any]:
    params = (f"?product_short={product_short}&name={name}&region_id={region}")
    d = http.fetch_json(f"{BASE_DETAIL}{params}", retries=4, backoff=2.0)
    if isinstance(d, dict) and d.get("error_code") == "APIEXPLORER.1055":
        d = http.fetch_json(
            f"{BASE_DETAIL}?product_short={product_short}&name={name}",
            retries=4, backoff=2.0)
    return d


class LiveFallback:
    """实时回退适配器：抓取 → 转换 → 缓存。"""

    def __init__(self, store: LocalStore):
        self._store = store

    def fetch(self, product: str, api: str, region: str) -> ApiHit | None:
        raw = _fetch_detail(product, api, region)
        if not isinstance(raw, dict) or not raw.get("paths"):
            return None
        doc = conv.convert_api(raw)
        match = find_api_in_doc(doc, api)
        if match is None:
            return None
        path, method, op = match
        result: ApiHit = (doc, path, method, op)
        key = ((product or "").lower(), api, region)
        self._store.set_api_cache(key, result)
        return result
