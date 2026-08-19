"""MCP server session 注册表：连接/断开/空闲回收/LRU 上限。

线程安全假设：仅在 asyncio 服务线程中调用，无并发写。
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .sdk import SessionClient

logger = logging.getLogger("openmcp.mcpdiscover.manager")

DEFAULT_IDLE_TIMEOUT = 300
DEFAULT_MAX_SESSIONS = 5


@dataclass
class SessionRecord:
    server_id: str
    endpoint: str
    client: SessionClient
    connected_at: float
    last_used: float
    tools_cache: list[dict[str, Any]] | None = None


@dataclass
class ConnectInfo:
    protocol_version: str
    server_info: dict[str, Any]


class SessionManager:
    """管理到云端 MCP server 的 client session。

    空闲超时回收 + LRU 上限；惰性检查（操作时触发，无后台线程）。
    """

    def __init__(
        self,
        client_factory: Callable[[], SessionClient],
        *,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._sessions: dict[str, SessionRecord] = {}
        self._client_factory = client_factory
        self._idle_timeout = idle_timeout
        self._max_sessions = max(1, max_sessions)
        self._clock = clock

    def _now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------ 生命周期

    async def connect(self, server_id: str, endpoint: str) -> ConnectInfo:
        """连接到指定 server。若已有连接，先断开再重连。"""
        await self._disconnect_if_exists(server_id)
        await self._evict_stale()
        await self._evict_lru_if_needed()

        self._sessions.pop(server_id, None)

        client = self._client_factory()
        info = await client.connect(endpoint)
        now = self._now()
        self._sessions[server_id] = SessionRecord(
            server_id=server_id,
            endpoint=endpoint,
            client=client,
            connected_at=now,
            last_used=now,
        )
        logger.info("session connected: %s => %s", server_id, endpoint)
        return ConnectInfo(
            protocol_version=info["protocol_version"],
            server_info=info["server_info"],
        )

    async def disconnect(self, server_id: str) -> bool:
        """断开指定 server 连接。返回 True 表示存在并已断开。"""
        rec = self._sessions.pop(server_id, None)
        if rec is None:
            return False
        await rec.client.disconnect()
        logger.info("session disconnected: %s", server_id)
        return True

    async def _disconnect_if_exists(self, server_id: str) -> None:
        rec = self._sessions.pop(server_id, None)
        if rec is not None:
            await rec.client.disconnect()

    async def _evict_stale(self) -> None:
        """回收空闲超时的 session。"""
        now = self._now()
        stale = [
            sid for sid, rec in self._sessions.items()
            if now - rec.last_used > self._idle_timeout
        ]
        for sid in stale:
            rec = self._sessions.pop(sid)
            await rec.client.disconnect()
            logger.info("session evicted (idle): %s", sid)

    async def _evict_lru_if_needed(self) -> None:
        """超过上限时断开最近最少使用的 session。"""
        while len(self._sessions) >= self._max_sessions:
            lru_sid = min(self._sessions, key=lambda sid: self._sessions[sid].last_used)
            rec = self._sessions.pop(lru_sid)
            await rec.client.disconnect()
            logger.info("session evicted (LRU, cap=%d): %s", self._max_sessions, lru_sid)

    # ------------------------------------------------------------------ 查询

    def get_session(self, server_id: str) -> SessionRecord | None:
        """获取已连接的 session 记录（不更新 last_used）。"""
        return self._sessions.get(server_id)

    def is_connected(self, server_id: str) -> bool:
        rec = self._sessions.get(server_id)
        if rec is None:
            return False
        return self._now() - rec.last_used <= self._idle_timeout

    def _touch(self, server_id: str) -> SessionRecord:
        rec = self._sessions[server_id]
        rec.last_used = self._now()
        return rec

    # ------------------------------------------------------------------ 操作

    async def list_tools(self, server_id: str) -> list[dict[str, Any]]:
        """拉取工具清单（有缓存则复用，无缓存则拉取并缓存）。"""
        rec = self._touch(server_id)
        if rec.tools_cache is not None:
            return rec.tools_cache
        tools = await rec.client.list_tools()
        rec.tools_cache = tools
        return tools

    async def call_tool(self, server_id: str, tool: str,
                        arguments: dict[str, Any] | None) -> dict[str, Any]:
        """调用目标 server 的指定工具。"""
        rec = self._touch(server_id)
        return await rec.client.call_tool(tool, arguments or {})

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)
