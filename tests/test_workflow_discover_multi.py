"""Discover 多 server E2E：同时连接 ECS + VPC MCP server，交叉调用验证。

依赖：tests/servers/ecs_server.py + vpc_server.py（本地回环，无外网依赖）。
标 e2e（默认跳过），用 `uv run pytest tests/test_workflow_discover_multi.py -m e2e -s` 运行。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.servers.ecs_server import make_server as make_ecs
from tests.servers.vpc_server import make_server as make_vpc

pytestmark = pytest.mark.e2e

PROOT = Path(__file__).resolve().parent.parent


class McpSession:
    def __init__(self, args: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(PROOT), env=env,
        )
        self.schemas: dict[str, dict] = {}

    def call(self, method: str, params: dict, msg_id: int) -> dict:
        self.proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                detail = b""
                if self.proc.poll() is not None and self.proc.stderr:
                    detail = self.proc.stderr.read()
                raise RuntimeError(f"server closed: {detail[:300]}")
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        r = self.call("tools/call",
                      {"name": name, "arguments": arguments or {}},
                      hash((name, str(arguments))))
        text = r["result"]["content"][0]["text"]
        return json.loads(text)

    def init(self) -> None:
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "multi-e2e", "version": "0"}}, 1)
        self.call("notifications/initialized", {}, 0)
        lst = self.call("tools/list", {}, 2)
        self.schemas = {t["name"]: t.get("outputSchema") or {} for t in lst["result"]["tools"]}

    def close(self) -> None:
        self.proc.terminate()


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    """启动 ECS + VPC server，启用 gateway discover 模式连接。"""
    ecs = make_ecs()
    vpc = make_vpc()
    ecs.start()
    vpc.start()

    tmp = tmp_path_factory.mktemp("multi")
    cat = tmp / "catalog.json"
    cat.write_text(json.dumps([
        {"id": "@test/ecs", "name": "E2E ECS", "display_name": "测试 ECS",
         "category": "计算", "description": "ECS test server",
         "auth": "none", "version": "1.0",
         "endpoint": ecs.endpoint, "transport": "streamable-http"},
        {"id": "@test/vpc", "name": "E2E VPC", "display_name": "测试 VPC",
         "category": "网络", "description": "VPC test server",
         "auth": "none", "version": "1.0",
         "endpoint": vpc.endpoint, "transport": "streamable-http"},
    ], ensure_ascii=False), encoding="utf-8")

    policy = tmp / "policy.json"
    policy.write_text(json.dumps([
        "server:@test/ecs=allow",
        "server:@test/ecs:*=allow",
        "server:@test/vpc=allow",
        "server:@test/vpc:*=allow",
        "*=deny",
    ], ensure_ascii=False), encoding="utf-8")

    log = tmp / "server.log"

    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["HUAWEICLOUD_MCP_SERVER_CATALOG"] = str(cat)
    env["HUAWEICLOUD_MCP_LOG_LEVEL"] = "DEBUG"
    args = [
        sys.executable, "-m", "main", "--mode", "discover",
        "--policy", str(policy),
        "--log-file", str(log),
    ]
    s = McpSession(args, env)
    s.init()
    s._log = log
    s._ecs = ecs
    s._vpc = vpc

    try:
        yield s
    finally:
        s.close()
        ecs.stop()
        vpc.stop()


# ------------------------------------------------------------------ 多 server 发现连接

def test_discover_multi_servers(session):
    servers = session.tool("list_mcp_servers")
    assert servers["ok"] is True
    ids = {s["server"] for s in servers["servers"]}
    assert "@test/ecs" in ids
    assert "@test/vpc" in ids


# ------------------------------------------------------------------ ECS 完整链 + 状态变化

def test_ecs_full_chain_and_state_change(session):
    # connect
    r = session.tool("connect_mcp_server", {"server": "@test/ecs"})
    assert r["ok"] is True

    # list tools
    tools = session.tool("list_server_tools", {"server": "@test/ecs"})
    names = {t["name"] for t in tools["tools"]}
    assert "list_servers" in names
    assert "start_server" in names
    assert "stop_server" in names
    assert "create_server" in names

    # list servers → 3 instances, ecs-003 is SHUTOFF
    result = session.tool("call_server_tool",
                          {"server": "@test/ecs", "tool": "list_servers", "arguments": {}})
    assert result["ok"] is True
    servers_text = result["result"]["content"][0]["text"]
    data = json.loads(servers_text)
    assert data["count"] == 3
    s003 = next(s for s in data["servers"] if s["server_id"] == "ecs-003")
    assert s003["status"] == "SHUTOFF"

    # start ecs-003
    r2 = session.tool("call_server_tool",
                      {"server": "@test/ecs", "tool": "start_server",
                       "arguments": {"server_id": "ecs-003"}})
    assert r2["ok"] is True

    # verify state changed
    result2 = session.tool("call_server_tool",
                           {"server": "@test/ecs", "tool": "list_servers", "arguments": {}})
    data2 = json.loads(result2["result"]["content"][0]["text"])
    s003_after = next(s for s in data2["servers"] if s["server_id"] == "ecs-003")
    assert s003_after["status"] == "ACTIVE"

    # create new server
    r3 = session.tool("call_server_tool",
                      {"server": "@test/ecs", "tool": "create_server",
                       "arguments": {"name": "new-srv", "flavor": "s6.large.2", "image_id": "centos-7.9"}})
    assert r3["ok"] is True

    # verify 4 servers now
    result3 = session.tool("call_server_tool",
                           {"server": "@test/ecs", "tool": "list_servers", "arguments": {}})
    data3 = json.loads(result3["result"]["content"][0]["text"])
    assert data3["count"] == 4

    session.tool("disconnect_mcp_server", {"server": "@test/ecs"})


# ------------------------------------------------------------------ VPC 只读交叉

def test_vpc_readonly_cross_access(session):
    r = session.tool("connect_mcp_server", {"server": "@test/vpc"})
    assert r["ok"] is True

    tools = session.tool("list_server_tools", {"server": "@test/vpc"})
    names = {t["name"] for t in tools["tools"]}
    assert "list_vpcs" in names
    assert "get_vpc" in names
    assert "list_subnets" in names
    assert "list_security_groups" in names

    # list_vpcs
    r1 = session.tool("call_server_tool",
                      {"server": "@test/vpc", "tool": "list_vpcs", "arguments": {}})
    data1 = json.loads(r1["result"]["content"][0]["text"])
    assert data1["count"] == 2

    # get_vpc
    r2 = session.tool("call_server_tool",
                      {"server": "@test/vpc", "tool": "get_vpc",
                       "arguments": {"vpc_id": "vpc-001"}})
    data2 = json.loads(r2["result"]["content"][0]["text"])
    assert data2["name"] == "default-vpc"

    # list_subnets with vpc filter
    r3 = session.tool("call_server_tool",
                      {"server": "@test/vpc", "tool": "list_subnets",
                       "arguments": {"vpc_id": "vpc-001"}})
    data3 = json.loads(r3["result"]["content"][0]["text"])
    assert data3["count"] == 2

    # list_security_groups
    r4 = session.tool("call_server_tool",
                      {"server": "@test/vpc", "tool": "list_security_groups", "arguments": {}})
    data4 = json.loads(r4["result"]["content"][0]["text"])
    assert data4["count"] == 3

    session.tool("disconnect_mcp_server", {"server": "@test/vpc"})


# ------------------------------------------------------------------ 同时连接两个 server

def test_both_servers_connected_simultaneously(session):
    r1 = session.tool("connect_mcp_server", {"server": "@test/ecs"})
    assert r1["ok"] is True
    r2 = session.tool("connect_mcp_server", {"server": "@test/vpc"})
    assert r2["ok"] is True

    # call ECS
    e = session.tool("call_server_tool",
                     {"server": "@test/ecs", "tool": "list_servers", "arguments": {}})
    assert e["ok"] is True

    # call VPC
    v = session.tool("call_server_tool",
                     {"server": "@test/vpc", "tool": "list_vpcs", "arguments": {}})
    assert v["ok"] is True

    session.tool("disconnect_mcp_server", {"server": "@test/ecs"})
    session.tool("disconnect_mcp_server", {"server": "@test/vpc"})
