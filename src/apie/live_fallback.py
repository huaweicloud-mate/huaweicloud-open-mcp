"""LiveFallback 适配器：实时拉取 API Explorer → OpenAPI 2.0 转换 → 回写内存缓存。

纠偏在生产时点落位（doc_compose，ADR-0001）：缓存写入前 in-place，「缓存
doc 恒已纠偏」由构造保证；纠偏未命中时缓存原样转换结果。
"""

import logging

from . import explorer
from .api_location import ApiLocation
from .convert_openapi2 import AuthDemotePolicy
from .doc_compose import compose_doc
from .memory_store import MemoryStore
from .metadata_corrections import MetadataCorrections

logger = logging.getLogger("apie.live_fallback")


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

    def fetch(self, product: str, api: str, region: str) -> ApiLocation | None:
        try:
            raw = explorer.fetch_detail(product, api, region)
        except explorer.ApiNotFoundError:
            return None
        if not isinstance(raw, dict) or not raw.get("paths"):
            return None
        doc = compose_doc(raw, product=product, api=api,
                          auth_demote=self._auth_demote,
                          corrections=self._corrections)
        location = ApiLocation.find(doc, api)
        if location is None:
            return None
        key = ((product or "").lower(), api, region)
        self._store.set_api_cache(key, location)
        return location
