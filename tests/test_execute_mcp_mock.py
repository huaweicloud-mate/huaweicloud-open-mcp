"""S7d 集成测试：真 SDK client → 本地 MCP stub（回环 HTTP）。"""

import asyncio

import httpx2

from openmcp.mcpdiscover.sdk import SdkSessionClient
from tests.fixtures.mcp_stub import StubMcpServer


def _client() -> SdkSessionClient:
    return SdkSessionClient(http_client=httpx2.AsyncClient(trust_env=False))


async def _connect_list_call(endpoint: str) -> None:
    client = _client()
    info = await client.connect(endpoint)
    assert info["protocol_version"] == "2025-06-18"
    assert info["server_info"]["name"] == "stub-mcp"

    tools = await client.list_tools()
    assert len(tools) == 2
    names = {t["name"] for t in tools}
    assert names == {"list_servers", "get_server"}
    assert "inputSchema" in tools[0]

    result = await client.call_tool("list_servers", {"status": "ACTIVE"})
    assert "content" in result
    assert len(result["content"]) == 1
    assert "stub-ecs-1" in result["content"][0]["text"]

    await client.disconnect()


async def _reconnect(endpoint: str) -> None:
    client = _client()
    await client.connect(endpoint)
    await client.disconnect()
    info = await client.connect(endpoint)
    assert info["protocol_version"] == "2025-06-18"
    tools = await client.list_tools()
    assert len(tools) == 2
    await client.disconnect()


def test_connect_list_and_call():
    stub = StubMcpServer()
    stub.start()
    try:
        asyncio.run(_connect_list_call(stub.endpoint))
    finally:
        stub.stop()


def test_reconnect_after_disconnect():
    stub = StubMcpServer()
    stub.start()
    try:
        asyncio.run(_reconnect(stub.endpoint))
    finally:
        stub.stop()
