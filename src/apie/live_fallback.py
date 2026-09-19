"""LiveFallback 适配器：实时拉取 API Explorer → OpenAPI 2.0 转换 → 回写内存缓存。

纠偏在生产时点落位（doc_compose，ADR-0001）：缓存写入前 in-place，「缓存
doc 按写入时点的纠偏世代纠偏」由构造保证；纠偏未命中时缓存原样转换结果。
S23 起 corrections 可为热刷新 provider（fetch 时点现读），热刷新联动
MemoryStore.clear_api_details() 定向失效详情缓存。
"""

import logging

from . import explorer
from .api_location import ApiLocation
from .convert_openapi2 import AuthDemotePolicy
from .doc_compose import compose_doc
from .memory_store import MemoryStore
from .metadata_corrections import CorrectionsProvider, MetadataCorrections

logger = logging.getLogger("apie.live_fallback")


class LiveFallback:
    """实时回退适配器：抓取 → 转换+纠偏（组合根） → 缓存。

    auth_demote 为认证头 required 降级策略（None=默认开启，启动期常量，doc
    随首次转换固化进缓存）；corrections 为元数据纠偏配置（None=空）或热刷新
    provider（S23，fetch 时点现读——纠偏口径跟随当前配置文件内容）。
    """

    def __init__(self, store: MemoryStore,
                 auth_demote: AuthDemotePolicy | None = None,
                 corrections: "MetadataCorrections | CorrectionsProvider | None" = None):
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
        # S23 fetch 时点现读：值对象原样，provider（热刷新 holder）现取当前值
        corrections = self._corrections
        if corrections is not None and not isinstance(corrections, MetadataCorrections):
            corrections = corrections.get()
        doc = compose_doc(raw, product=product, api=api,
                          auth_demote=self._auth_demote,
                          corrections=corrections)
        location = ApiLocation.find(doc, api)
        if location is None:
            return None
        key = ((product or "").lower(), api, region)
        self._store.set_api_cache(key, location)
        return location
