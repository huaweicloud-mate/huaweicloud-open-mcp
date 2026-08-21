"""MCP server 目录源：本地文件起步，预留远程端点（官方目录端点上线后通过 RemoteCatalogSource 切换）。

CatalogSource 协议 + LocalCatalogSource + 过滤工具函数。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

from common.paths import project_root

logger = logging.getLogger("mcp_discover.catalog")

ENV_CATALOG = "HUAWEICLOUD_MCP_SERVER_CATALOG"
ENV_CATALOG_URL = "HUAWEICLOUD_MCP_SERVER_CATALOG_URL"
DEFAULT_CATALOG = "configs/mcp-server-catalog.example.json"


class CatalogSource(Protocol):
    """目录源协议。fetch 返回全部条目。"""

    def fetch(self) -> list[dict[str, Any]]:
        ...


class LocalCatalogSource:
    """本地 JSON 文件目录源。文件路径可通过 env 变量覆盖。

    内存缓存 + clear()：常用于测试环境中更新目录后重置。
    """

    def __init__(self, path: str | None = None):
        if path is None:
            path = os.environ.get(ENV_CATALOG) or str(project_root() / DEFAULT_CATALOG)
        self._path = Path(path)
        self._cache: list[dict[str, Any]] | None = None
        self._loaded_path: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def fetch(self) -> list[dict[str, Any]]:
        if self._cache is not None and str(self._path) == self._loaded_path:
            return self._cache
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.warning("catalog load failed: %s", self._path, exc_info=True)
            if self._cache is not None:
                return self._cache
            return []
        if not isinstance(raw, list):
            raw = [raw]
        entries = [dict(r) for r in raw]
        self._cache = entries
        self._loaded_path = str(self._path)
        return entries

    def clear(self) -> None:
        """清空缓存，下次 fetch 重新读取文件。"""
        self._cache = None
        self._loaded_path = None


def list_servers(source: CatalogSource, *, category: str | None = None,
                 keyword: str | None = None) -> list[dict[str, Any]]:
    """列出全部条目，支持按 category 过滤和 keyword 模糊搜索。"""
    servers = source.fetch()
    if category:
        servers = [s for s in servers if s.get("category", "").lower() == category.lower()]
    if keyword:
        kw = keyword.lower()
        servers = [s for s in servers
                   if kw in s.get("id", "").lower()
                   or kw in s.get("display_name", "").lower()
                   or kw in s.get("description", "").lower()]
    return servers


def get_server(source: CatalogSource, server_id: str) -> dict[str, Any] | None:
    """按 id 查找单条（大小写不敏感）。"""
    sid = server_id.lower()
    for s in source.fetch():
        if s.get("id", "").lower() == sid:
            return s
    return None
