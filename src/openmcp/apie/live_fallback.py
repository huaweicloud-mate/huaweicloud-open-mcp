"""LiveFallback 适配器：实时拉取 API Explorer → OpenAPI 2.0 转换 → 回写内存缓存。"""

import logging
from typing import Any

from . import convert_openapi2 as conv
from . import http
from .memory_store import ApiHit, MemoryStore

logger = logging.getLogger("openmcp.apie.live_fallback")

BASE_DETAIL = "https://console.huaweicloud.com/apiexplorer/new/v4/apis/detail"


def _find_api_in_doc(doc: dict[str, Any], api_name: str) -> tuple[str, str, dict[str, Any]] | None:
    """在远端拉取+转换后的 doc 中定位 operation，精确 + 大小写不敏感。

    远端按 exact name 查询，不存在跨文件歧义，无需子串兜底。
    """
    if not doc:
        return None
    target = (api_name or "").lower()
    for path, path_item in (doc.get("paths") or {}).items():
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            opid = op.get("operationId")
            if opid == api_name or (opid and opid.lower() == target):
                return (path, method, op)
    return None


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

    def __init__(self, store: MemoryStore):
        self._store = store

    def fetch(self, product: str, api: str, region: str) -> ApiHit | None:
        raw = _fetch_detail(product, api, region)
        if not isinstance(raw, dict) or not raw.get("paths"):
            return None
        doc = conv.convert_api(raw)
        match = _find_api_in_doc(doc, api)
        if match is None:
            return None
        path, method, op = match
        result: ApiHit = (doc, path, method, op)
        key = ((product or "").lower(), api, region)
        self._store.set_api_cache(key, result)
        return result
