"""LiveFallback 适配器：实时拉取 API Explorer → OpenAPI 2.0 转换 → 回写内存缓存。"""

import logging
from typing import Any

from . import convert_openapi2 as conv
from . import explorer
from .memory_store import ApiHit, MemoryStore

logger = logging.getLogger("apie.live_fallback")


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


class LiveFallback:
    """实时回退适配器：抓取 → 转换 → 缓存。"""

    def __init__(self, store: MemoryStore):
        self._store = store

    def fetch(self, product: str, api: str, region: str) -> ApiHit | None:
        try:
            raw = explorer.fetch_detail(product, api, region)
        except explorer.ApiNotFoundError:
            return None
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
