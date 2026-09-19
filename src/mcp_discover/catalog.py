"""MCP server 目录源：本地文件起步，预留远程端点（官方目录端点上线后通过 RemoteCatalogSource 切换）。

CatalogSource 协议 + LocalCatalogSource + 过滤工具函数。

S23 起 LocalCatalogSource 具备热刷新（与 common.hotconf 同款 idiom 自持实现：
懒加载、非 fail-fast、无 schema 校验的目录语义与 HotFile 不同，不强塞同一
Module——复用 stamp_of 助手）：首次加载同步（现状语义），后续变更走后台
daemon 线程重读（stale-until-ready），文件写坏/短暂不可读沿用最近合法目录。
"""

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol

from common.deployment import (
    ENV_SERVER_CATALOG,
)
from common.hotconf import stamp_of
from common.paths import config_path

logger = logging.getLogger("mcp_discover.catalog")

DEFAULT_CATALOG = "mcp-server-catalog.example.json"


class CatalogSource(Protocol):
    """目录源协议。fetch 返回全部条目。"""

    def fetch(self) -> list[dict[str, Any]]:
        ...


class LocalCatalogSource:
    """本地 JSON 文件目录源。文件路径可通过 env 变量覆盖。

    内存缓存 + clear()：常用于测试环境中更新目录后重置。
    热刷新（S23）：fetch 每次至多一次 stat；stamp 变化触发后台重载并返回
    旧缓存（触发请求 stale-until-ready）；首次/换路径同步加载，加载失败
    返回旧缓存或空列表（不缓存失败结果，下次重试）。
    """

    def __init__(self, path: str | None = None):
        if path is None:
            path = os.environ.get(ENV_SERVER_CATALOG) or str(config_path(DEFAULT_CATALOG))
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cache: list[dict[str, Any]] | None = None
        self._loaded_path: str | None = None
        self._stamp: Any = None
        self._digest: str | None = None
        self._missing = False
        self._pending = False
        self._thread: threading.Thread | None = None

    @property
    def path(self) -> Path:
        return self._path

    def fetch(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._cache is None or str(self._path) != self._loaded_path:
                return self._load_sync()
            try:
                stamp = stamp_of(str(self._path))
            except OSError:
                if not self._missing:
                    logger.warning("catalog 文件暂时不可读 %s，沿用最近合法目录", self._path)
                    self._missing = True
                return self._cache
            self._missing = False
            if stamp == self._stamp:
                return self._cache
            if not self._pending:  # 单飞：后台重载进行中直接返回旧缓存
                self._pending = True
                self._thread = threading.Thread(
                    target=self._reload, args=(stamp,),
                    name=f"catalog-reload:{self._path}", daemon=True)
                self._thread.start()
            return self._cache

    def clear(self) -> None:
        """清空缓存，下次 fetch 重新读取文件。"""
        with self._lock:
            self._cache = None
            self._loaded_path = None
            self._stamp = None
            self._digest = None
            self._pending = False

    # ---------- 内部实现 ----------

    def _load_sync(self) -> list[dict[str, Any]]:
        """首次/换路径同步加载（现状语义：失败返回旧缓存或空列表，不缓存失败）。"""
        try:
            data, digest = _read_bytes(str(self._path))
            raw = json.loads(data)
        except Exception:
            logger.warning("catalog load failed: %s", self._path, exc_info=True)
            if self._cache is not None:
                return self._cache
            return []
        self._cache = _normalize(raw)
        self._loaded_path = str(self._path)
        self._digest = digest
        try:
            self._stamp = stamp_of(str(self._path))
        except OSError:
            self._stamp = None
        self._missing = False
        return self._cache

    def _reload(self, observed: Any) -> None:
        """后台重载主体（S23，锁外读盘，锁内落位；完成时 stamp 比对）。"""
        try:
            data, digest = _read_bytes(str(self._path))
            if digest == self._digest:
                with self._lock:
                    self._stamp = observed  # touch：内容未变仅推水位
                    self._pending = False
                return
            raw = json.loads(data)
        except Exception:
            with self._lock:
                logger.warning("catalog 变更后重载失败 %s（沿用最近合法目录）",
                               self._path, exc_info=True)
                self._stamp = observed  # 推进水位防刷屏；文件再次变更时自动重试
                self._pending = False
            return
        entries = _normalize(raw)
        with self._lock:
            self._pending = False
            try:
                current = stamp_of(str(self._path))
            except OSError:
                return
            if current != observed:
                return  # 重载期间文件又变：弃结果，下次 fetch 重触发
            self._cache = entries
            self._loaded_path = str(self._path)
            self._digest = digest
            self._stamp = current
            logger.info("catalog 热重载完成 %s：%d 条", self._path, len(entries))

    def _join_pending(self, timeout: float = 5.0) -> None:
        """internal seam（测试同步点）：等待在飞重载线程结束。"""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)


def _read_bytes(path: str) -> tuple[bytes, str]:
    with open(path, "rb") as f:
        data = f.read()
    return data, hashlib.sha256(data).hexdigest()


def _normalize(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raw = [raw]
    return [dict(r) for r in raw]


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
