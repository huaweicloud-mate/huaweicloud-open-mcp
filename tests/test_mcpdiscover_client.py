"""S7d 单测：SessionClient 协议 + 适配层（fake + 最小 SDK 验证）。"""

import asyncio
from typing import Any

from mcp_discover.sdk import SessionClient


class FakeSessionClient:
    def __init__(self):
        self.calls: list[tuple[str, Any]] = []
        self._tools: list[dict[str, Any]] = []

    async def connect(self, endpoint: str) -> dict[str, Any]:
        self.calls.append(("connect", endpoint))
        return {"protocol_version": "1.0", "server_info": {"name": "stub"}}

    async def list_tools(self) -> list[dict[str, Any]]:
        self.calls.append(("list_tools", None))
        return self._tools

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("call_tool", tool, arguments))
        return {"result": "ok", "tool": tool}

    async def disconnect(self) -> None:
        self.calls.append(("disconnect", None))


def _use_client(client: SessionClient) -> None:
    async def _run():
        info = await client.connect("http://stub/mcp")
        assert info["protocol_version"] == "1.0"
        tools = await client.list_tools()
        assert isinstance(tools, list)
        result = await client.call_tool("greet", {"name": "world"})
        assert result["result"] == "ok"
        await client.disconnect()

    asyncio.run(_run())


def test_fake_client_matches_protocol():
    client = FakeSessionClient()
    _use_client(client)
    assert client.calls == [
        ("connect", "http://stub/mcp"),
        ("list_tools", None),
        ("call_tool", "greet", {"name": "world"}),
        ("disconnect", None),
    ]


def test_protocol_type_checking():
    """静态验证 FakeSessionClient 满足 SessionClient 协议（运行时不需显式 instance check）。"""
    client = FakeSessionClient()
    assert hasattr(client, "connect")
    assert hasattr(client, "list_tools")
    assert hasattr(client, "call_tool")
    assert hasattr(client, "disconnect")
    assert callable(client.connect)
    assert callable(client.list_tools)
    assert callable(client.call_tool)
    assert callable(client.disconnect)
