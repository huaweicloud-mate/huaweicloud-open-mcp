"""LiveFallback 适配器：实时拉取 API Explorer → OpenAPI 2.0 转换 → 回写内存缓存。

纠偏在生产时点落位（doc_compose，ADR-0001）：缓存写入前 in-place，「缓存
doc 恒已纠偏」由构造保证；纠偏未命中时缓存原样转换结果。
"""

import logging
from typing import Any

from . import explorer
from .convert_openapi2 import AuthDemotePolicy
from .doc_compose import compose_doc
from .memory_store import ApiHit, MemoryStore
from .metadata_corrections import MetadataCorrections

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
    """实时回退适配器：抓取 → 转换+纠偏（组合根） → 缓存。

    auth_demote 为认证头 required 降级策略（None=默认开启）；corrections 为
    元数据纠偏配置（None=空）。两者均为启动期常量，doc 随首次转换固化进
    缓存——同进程内策略变更不回溯已缓存 doc。
    """

    def __init__(self, store: MemoryStore,
                 auth_demote: AuthDemotePolicy | None = None,
                 corrections: MetadataCorrections | None = None):
        self._store = store
        self._auth_demote = auth_demote
        self._corrections = corrections

    def fetch(self, product: str, api: str, region: str) -> ApiHit | None:
        try:
            raw = explorer.fetch_detail(product, api, region)
        except explorer.ApiNotFoundError:
            return None
        if not isinstance(raw, dict) or not raw.get("paths"):
            return None
        doc = compose_doc(raw, product=product, api=api,
                          auth_demote=self._auth_demote,
                          corrections=self._corrections)
        match = _find_api_in_doc(doc, api)
        if match is None:
            return None
        path, method, op = match
        result: ApiHit = (doc, path, method, op)
        key = ((product or "").lower(), api, region)
        self._store.set_api_cache(key, result)
        return result
