"""MCP client 适配层：SessionClient 协议 + mcp SDK 实现（background task 模式）。

mcp SDK 的 ClientSession 使用 anyio cancel scopes，其生命周期绑定到进入 __aenter__
的 asyncio task。但 MCP server 中 connect/list/call 是不同的 tool handler 调用
（不同 task），跨 task 访问 ClientSession 会触发"attempted to exit cancel scope
in a different task"错误。

解决方案：每个 SdkSessionClient 在 connect() 时创建一个长期运行的 background
asyncio task，由其持有 ClientSession 上下文；list_tools/call_tool 通过
asyncio.Queue + Future 与 background task 通信。
"""

import asyncio
import logging
from typing import Any, Protocol, cast

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("openmcp.mcpdiscover.sdk")


class SessionClient(Protocol):
    """MCP server 会话客户端协议（async）。"""

    async def connect(self, endpoint: str) -> dict[str, Any]:
        ...

    async def list_tools(self) -> list[dict[str, Any]]:
        ...

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def disconnect(self) -> None:
        ...


class SdkSessionClient:
    """基于 mcp SDK 的 SessionClient 实现（background task 模式）。"""

    def __init__(self, http_client: Any | None = None) -> None:
        self._http_client = http_client
        self._queue: asyncio.Queue[tuple[str, int, tuple] | None] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._task: asyncio.Task[Any] | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._init_info: dict[str, Any] | None = None
        self._endpoint: str | None = None

    async def connect(self, endpoint: str) -> dict[str, Any]:
        self._queue = asyncio.Queue()
        self._pending = {}
        self._next_id = 0
        self._ready = asyncio.Event()
        self._init_info = None
        self._endpoint = endpoint

        self._task = asyncio.create_task(self._runner(endpoint))
        await self._ready.wait()

        maybe_info: Any = self._init_info
        if maybe_info is None:
            raise RuntimeError("MCP session 连接失败（background task 异常退出）")
        logger.info("mcp client connected: %s protocol=%s",
                    endpoint, maybe_info.get("protocol_version"))
        return cast(dict[str, Any], maybe_info)

    async def _runner(self, endpoint: str) -> None:
        try:
            async with streamable_http_client(
                endpoint, http_client=self._http_client, terminate_on_close=False
            ) as transport:
                read_stream, write_stream = transport[:2]
                async with ClientSession(read_stream, write_stream) as session:
                    init = await session.initialize()
                    self._init_info = {
                        "protocol_version": init.protocol_version,
                        "server_info": (
                            {"name": init.server_info.name, "version": init.server_info.version}
                            if init.server_info else {}
                        ),
                    }
                    self._ready.set()

                    while True:
                        msg = await self._queue.get()
                        if msg is None:
                            break
                        cmd, mid, args = msg
                        fut = self._pending.pop(mid, None)
                        if fut is None:
                            continue
                        try:
                            r: Any
                            if cmd == "list_tools":
                                r = await session.list_tools()
                                fut.set_result(r)
                            elif cmd == "call_tool":
                                name, arguments = args
                                r = await session.call_tool(name, arguments or {})
                                fut.set_result(r)
                            else:
                                fut.set_exception(RuntimeError(f"unknown cmd: {cmd}"))
                        except Exception as exc:
                            fut.set_exception(exc)
        except Exception:
            logger.warning("mcp client runner crashed: %s", endpoint, exc_info=True)
            if not self._ready.is_set():
                self._ready.set()
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("MCP session runner exited"))
            self._pending.clear()

    async def _send(self, cmd: str, *args: Any) -> Any:
        mid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.Future()
        self._pending[mid] = fut
        await self._queue.put((cmd, mid, args))
        return await fut

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._send("list_tools")
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
        logger.info("call_tool: %s args=%s", tool,
                     str(arguments)[:200] if arguments else "{}")
        result = await self._send("call_tool", tool, arguments)
        content = getattr(result, "content", None)
        is_error = getattr(result, "isError", False)
        out: dict[str, Any] = {}
        if content is not None:
            out["content"] = [c.model_dump() if hasattr(c, "model_dump") else c for c in content]
        if is_error:
            out["isError"] = True
        return out

    async def disconnect(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        try:
            await self._task
        except Exception:
            pass
        self._task = None
        self._endpoint = None
