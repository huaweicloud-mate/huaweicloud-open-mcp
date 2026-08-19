"""SessionManager 纯函数单测（S7c）：session 生命周期 + idle 回收 + LRU 上限。"""

import asyncio

from openmcp.mcpdiscover.manager import SessionManager

# ------------------------------------------------------------------ fake client

class FakeClient:
    def __init__(self, tools=None):
        self.calls: list[tuple[str, tuple]] = []
        self._tools = tools or []

    async def connect(self, endpoint: str):
        self.calls.append(("connect", endpoint))
        return {"protocol_version": "1.0", "server_info": {"name": "fake", "version": "1.0"}}

    async def list_tools(self):
        self.calls.append(("list_tools", ()))
        return self._tools

    async def call_tool(self, tool: str, arguments: dict):
        self.calls.append(("call_tool", tool, arguments))
        return {"ok": True}

    async def disconnect(self):
        self.calls.append(("disconnect", ()))


def _manager(client_factory=None, **kwargs):
    def default_factory():
        return FakeClient()
    return SessionManager(client_factory=client_factory or default_factory, **kwargs)


# ------------------------------------------------------------------ connect

def test_connect_new():
    m = _manager()
    info = asyncio.run(m.connect("srv-a", "http://a/mcp"))
    assert info.protocol_version == "1.0"
    assert info.server_info["name"] == "fake"
    assert m.is_connected("srv-a")
    assert m.active_sessions == 1


def test_reconnect_disconnects_old():
    cli = FakeClient()
    m = _manager(client_factory=lambda: cli)
    asyncio.run(m.connect("srv-a", "http://a/mcp"))
    asyncio.run(m.connect("srv-a", "http://a2/mcp"))
    assert cli.calls[0] == ("connect", "http://a/mcp")
    assert ("disconnect", ()) in cli.calls


# ------------------------------------------------------------------ idle eviction

def test_idle_eviction():
    clock = [0.0]

    def tick(s):
        clock[0] += s

    m = _manager(clock=lambda: clock[0], idle_timeout=5)
    asyncio.run(m.connect("a", "http://a/mcp"))
    assert m.is_connected("a")
    tick(6)
    assert not m.is_connected("a")


def test_idle_eviction_on_next_connect():
    clock = [0.0]

    def tick(s):
        clock[0] += s

    cli = FakeClient()
    m = _manager(client_factory=lambda: cli, clock=lambda: clock[0], idle_timeout=5)
    asyncio.run(m.connect("a", "http://a/mcp"))
    tick(6)
    asyncio.run(m.connect("b", "http://b/mcp"))
    assert m.active_sessions == 1
    assert m.is_connected("b")
    disconnect_calls = [c for c in cli.calls if c[0] == "disconnect"]
    assert len(disconnect_calls) >= 1


# ------------------------------------------------------------------ LRU cap eviction

def test_lru_eviction():
    clock = [0.0]
    m = _manager(max_sessions=2, clock=lambda: clock[0], idle_timeout=999)
    asyncio.run(m.connect("a", "http://a/mcp"))
    clock[0] += 1
    asyncio.run(m.connect("b", "http://b/mcp"))
    clock[0] += 1
    asyncio.run(m.connect("c", "http://c/mcp"))
    assert not m.is_connected("a")  # LRU
    assert m.is_connected("b")
    assert m.is_connected("c")
    assert m.active_sessions == 2


# ------------------------------------------------------------------ disconnect

def test_disconnect_existing():
    m = _manager()
    asyncio.run(m.connect("srv-a", "http://a/mcp"))
    released = asyncio.run(m.disconnect("srv-a"))
    assert released is True
    assert not m.is_connected("srv-a")
    assert m.active_sessions == 0


def test_disconnect_not_found():
    m = _manager()
    released = asyncio.run(m.disconnect("nope"))
    assert released is False


# ------------------------------------------------------------------ list_tools

def test_list_tools_cached():
    cli = FakeClient(tools=[{"name": "t1", "description": "d1"}])
    m = _manager(client_factory=lambda: cli)
    asyncio.run(m.connect("a", "http://a/mcp"))
    tools1 = asyncio.run(m.list_tools("a"))
    assert len(tools1) == 1
    tools2 = asyncio.run(m.list_tools("a"))
    assert len(tools2) == 1
    assert cli.calls.count(("list_tools", ())) == 1


# ------------------------------------------------------------------ call_tool

def test_call_tool():
    cli = FakeClient()
    m = _manager(client_factory=lambda: cli)
    asyncio.run(m.connect("a", "http://a/mcp"))
    result = asyncio.run(m.call_tool("a", "my_tool", {"x": 1}))
    assert result == {"ok": True}
    assert ("call_tool", "my_tool", {"x": 1}) in cli.calls


def test_call_tool_updates_last_used():
    clock = [0.0]
    m = _manager(clock=lambda: clock[0], idle_timeout=999, max_sessions=2)
    asyncio.run(m.connect("a", "http://a/mcp"))
    clock[0] += 1
    asyncio.run(m.connect("b", "http://b/mcp"))
    clock[0] += 1
    asyncio.run(m.call_tool("a", "x", {}))
    clock[0] += 1
    asyncio.run(m.connect("c", "http://c/mcp"))
    assert m.is_connected("a")
    assert not m.is_connected("b")
    assert m.is_connected("c")
