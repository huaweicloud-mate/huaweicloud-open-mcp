"""MCP client 适配层：SessionClient 协议 + mcp SDK 实现。

定义连接/查询/调用的抽象协议，供单元测试注入 fake 实现；
生产环境用 SdkSessionClient 包装 mcp SDK 的 streamable_http_client + ClientSession。
"""

import contextlib
import logging
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("openmcp.mcpdiscover.sdk")


class SessionClient(Protocol):
    """MCP server 会话客户端协议（async）。

    每个 server 连接对应一个 SessionClient 实例；
    方法均为 async，与 mcp SDK 的异步模型保持一致。
    """

    async def connect(self, endpoint: str) -> dict[str, Any]:
        """建立连接，返回 {"protocol_version": str, "server_info": dict}。"""
        ...

    async def list_tools(self) -> list[dict[str, Any]]:
        """拉取工具清单，返回 [{"name": str, "description": str, "inputSchema": dict}, ...]。"""
        ...

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用工具，返回规范化结果 dict。"""
        ...

    async def disconnect(self) -> None:
        """断开连接，释放资源。"""
        ...


class SdkSessionClient:
    """基于 mcp SDK 的 SessionClient 实现。

    使用 AsyncExitStack 管理 streamable_http_client + ClientSession 的
    生命周期；connect 时打开栈、disconnect 时关闭栈。
    """

    def __init__(self, http_client: Any | None = None) -> None:
        self._exit_stack = contextlib.AsyncExitStack()
        self._session: ClientSession | None = None
        self._endpoint: str | None = None
        self._http_client = http_client

    async def connect(self, endpoint: str) -> dict[str, Any]:
        transport = await self._exit_stack.enter_async_context(
            streamable_http_client(endpoint, http_client=self._http_client,
                                   terminate_on_close=False)
        )
        read_stream, write_stream = transport[:2]
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        init_result = await self._session.initialize()
        self._endpoint = endpoint
        logger.info("mcp client connected: %s protocol=%s", endpoint, init_result.protocol_version)
        return {
            "protocol_version": init_result.protocol_version,
            "server_info": {
                "name": init_result.server_info.name,
                "version": init_result.server_info.version,
            } if init_result.server_info else {},
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._session is None:
            raise RuntimeError("session not connected")
        result = await self._session.list_tools()
        tools = []
        for tool in result.tools:
            d: dict[str, Any] = {"name": tool.name}
            if tool.description:
                d["description"] = tool.description
            if tool.input_schema:
                d["inputSchema"] = tool.input_schema
            tools.append(d)
        logger.debug("list_tools: %d tools from %s", len(tools), self._endpoint)
        return tools

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("session not connected")
        logger.info("call_tool: %s args=%s", tool,
                     str(arguments)[:200] if arguments else "{}")
        result = await self._session.call_tool(tool, arguments or {})
        content = getattr(result, "content", None)
        is_error = getattr(result, "isError", False)
        out: dict[str, Any] = {}
        if content is not None:
            out["content"] = [c.model_dump() if hasattr(c, "model_dump") else c for c in content]
        if is_error:
            out["isError"] = True
        return out

    async def disconnect(self) -> None:
        logger.info("mcp client disconnecting: %s", self._endpoint)
        await self._exit_stack.aclose()
        self._session = None
        self._endpoint = None
        self._exit_stack = contextlib.AsyncExitStack()
