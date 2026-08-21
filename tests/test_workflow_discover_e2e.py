"""Discover 模式渐进式工作流 E2E：拉起 server 子进程，走完整 MCP stdio 协议链路。

步骤（对应 discover 模式 instructions 推荐工作流）：
① list_mcp_servers → ② get_mcp_server → ③ connect_mcp_server（经真 SDK Streamable HTTP 握手本地 stub）
→ ④ list_server_tools（摘要）→ ⑤ get_server_tool（完整 schema）→ ⑥ call_server_tool（代发调用）
→ ⑦ disconnect_mcp_server + ⑧ policy 拒绝路径。

依赖：本地 StubMcpServer（回环 HTTP），无需凭证/无需 data/openapi 产物。
标 e2e（默认跳过），用 `uv run pytest tests/test_workflow_discover_e2e.py -m e2e` 运行。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.mcp_stub import StubMcpServer

pytestmark = pytest.mark.e2e

PROOT = Path(__file__).resolve().parent.parent

CATALOG_ENTRIES = [
    {
        "id": "@test/ecs",
        "name": "Test ECS MCP Server",
        "display_name": "测试 ECS",
        "description": "Mock target for e2e tests",
        "category": "计算",
        "endpoint": "http://placeholder/mcp",
        "transport": "streamable-http",
        "auth": "none",
        "version": "1.0.0",
    },
    {
        "id": "@test/vpc",
        "name": "Test VPC MCP Server",
        "display_name": "测试 VPC",
        "description": "Another mock target",
        "category": "网络",
        "endpoint": "http://placeholder/mcp",
        "transport": "streamable-http",
        "auth": "none",
        "version": "1.0.0",
    },
]


class McpSession:
    """stdin/stdout JSON-RPC 子进程会话。"""

    def __init__(self, args: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(PROOT), env=env,
        )
        self.schemas: dict[str, dict] = {}

    @property
    def stderr_text(self) -> str:
        if self.proc.poll() is not None and self.proc.stderr is not None:
            remaining = self.proc.stderr.read().decode(errors="replace")
            return remaining
        return ""

    def call(self, method: str, params: dict, msg_id: int) -> dict:
        self.proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                detail = self.stderr_text
                raise RuntimeError(f"server closed (stderr: {detail[:500]})")
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        r = self.call("tools/call",
                      {"name": name, "arguments": arguments or {}},
                      hash((name, str(arguments))))
        schema = self.schemas.get(name)
        if schema is not None:
            import jsonschema
            jsonschema.validate(instance=r["result"].get("structuredContent") or {}, schema=schema)
        text = r["result"]["content"][0]["text"]
        return json.loads(text)

    def init(self) -> None:
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "discover-e2e", "version": "0"}}, 1)
        self.call("notifications/initialized", {}, 0)
        lst = self.call("tools/list", {}, 2)
        self.schemas = {t["name"]: t.get("outputSchema") or {} for t in lst["result"]["tools"]}

    def close(self) -> None:
        self.proc.terminate()


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    """模块级 fixture：stub + discovery server + MCP stdio session。"""
    stub = StubMcpServer()
    stub.start()

    tmp = tmp_path_factory.mktemp("e2e")
    cat = tmp / "catalog.json"
    cat.write_text(json.dumps(CATALOG_ENTRIES, ensure_ascii=False), encoding="utf-8")

    policy = tmp / "policy.json"
    policy.write_text(json.dumps([
        "server:@test/ecs=allow",
        "server:@test/ecs:list_servers=allow",
        "server:@test/ecs:get_server=deny",
        "*=deny",
    ], ensure_ascii=False), encoding="utf-8")

    log = tmp / "server.log"

    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["HUAWEICLOUD_MCP_SERVER_CATALOG"] = str(cat)
    env["HUAWEICLOUD_MCP_LOG_LEVEL"] = "DEBUG"
    args = [
        sys.executable, "-m", "main", "--mode", "discover", "--mock",
        "--mock-base", stub.endpoint,
        "--policy", str(policy),
        "--log-file", str(log),
    ]
    s = McpSession(args, env)
    s.init()
    s._log = log

    try:
        yield s
    finally:
        s.close()
        if log.exists():
            print("\n=== DISCOVER SERVER LOG ===", log.read_text(), sep="\n", file=sys.stderr)
        stub.stop()


# ------------------------------------------------------------------ 注册验证

def test_discover_tools_registered(session):
    """7 个 discover 工具注册，无 openapi 工具混入。"""
    names = set(session.schemas.keys())
    assert "list_mcp_servers" in names
    assert "connect_mcp_server" in names
    assert "list_server_tools" in names
    assert "call_server_tool" in names
    assert "disconnect_mcp_server" in names
    assert len(names) == 7
    assert "list_products" not in names
    assert "execute_api" not in names


def test_instructions_mention_discover_workflow(session):
    r = session.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "e2e-2", "version": "0"}}, 200)
    instructions = r["result"].get("instructions") or ""
    assert "list_mcp_servers" in instructions
    assert "渐进收窄" in instructions or "渐进" in instructions
    assert "safety policy" in instructions
    assert "两级" in instructions or "get_server_tool" in instructions


# ------------------------------------------------------------------ 完整工作流

def test_discover_full_progressive_chain(session):
    # ① 目录搜索
    servers = session.tool("list_mcp_servers", {"keyword": "ecs"})
    assert servers["ok"] is True
    assert servers["total"] >= 1
    found = {s["server"] for s in servers["servers"]}
    assert "@test/ecs" in found

    # ② 查看详情
    detail = session.tool("get_mcp_server", {"server": "@test/ecs"})
    assert detail["ok"] is True
    assert detail["display_name"] == "测试 ECS"

    # ③ 连接（经真 SDK Streamable HTTP 握手 stub）
    connected = session.tool("connect_mcp_server", {"server": "@test/ecs"})
    assert connected["ok"] is True, connected.get("reason", "")
    assert connected["protocol_version"] == "2025-06-18"
    assert connected["server_info"]["name"] == "stub-mcp"

    # ④ 工具摘要（两级读取第一步）
    tools_summary = session.tool("list_server_tools",
                                 {"server": "@test/ecs", "search": "server", "limit": 20})
    assert tools_summary["ok"] is True
    assert tools_summary["total"] >= 1
    tool_names = {t["name"] for t in tools_summary["tools"]}
    assert "list_servers" in tool_names
    assert "required" in tools_summary["tools"][0]

    # ⑤ 单个工具完整 schema（两级读取第二步）
    full = session.tool("get_server_tool", {"server": "@test/ecs", "tool": "list_servers"})
    assert full["ok"] is True
    schema = full.get("inputSchema") or {}
    assert schema.get("type") == "object" or "properties" in schema
    assert "status" in schema.get("properties", {})

    # ⑥ 代发调用（经 gateway SDK client → stub → 数据回流）
    result = session.tool("call_server_tool",
                          {"server": "@test/ecs", "tool": "list_servers",
                           "arguments": {"status": "ACTIVE"}})
    assert result["ok"] is True
    assert "result" in result
    content_text = result["result"].get("content", [{}])[0].get("text", "")
    assert "stub-ecs-1" in content_text

    # ⑦ 释放连接
    disconnected = session.tool("disconnect_mcp_server", {"server": "@test/ecs"})
    assert disconnected["ok"] is True
    assert disconnected["released"] is True


# ------------------------------------------------------------------ policy 拒绝

def test_discover_connect_denied_by_policy(session):
    """@test/vpc 不在 policy 白名单中，connect 应被拒绝。"""
    result = session.tool("connect_mcp_server", {"server": "@test/vpc"})
    assert result["ok"] is False
    assert "policy" in result.get("reason", "").lower() or "拒绝" in result.get("reason", "")


def test_discover_call_denied_by_policy(session):
    """@test/ecs 已连接，但 get_server 在 policy 中设 deny，调用应拒绝。"""
    # 确保已连接（使用 fixture 的 session，可能被 previous test 断开，先重连）
    connected = session.tool("connect_mcp_server", {"server": "@test/ecs"})
    assert connected["ok"] is True

    result = session.tool("call_server_tool",
                          {"server": "@test/ecs", "tool": "get_server",
                           "arguments": {"server_id": "x"}})
    assert result["ok"] is False
    reason_lower = result.get("reason", "").lower()
    assert "policy" in reason_lower or "拒绝" in result.get("reason", "")
