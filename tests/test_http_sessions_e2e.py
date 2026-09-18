"""S22：HTTP 多会话 e2e——真 mcp SDK client × build_app 装配 × 回环 uvicorn。

全链验证（无外网、无凭证：仅 manage_policy/healthz/execute 拒绝路径）：
- 双客户端并发：session 档授予互不可见（泄权修复的行为级证明）；
- 重连即新会话：原会话授予不随行、remove 未命中附「先前会话」提示；
- permanent 档跨会话可见（落文件）；
- 审计 NDJSON 会话归因（_audit_write 单点）；
- modern 单交换协议（无会话身份）manage_policy 结构化拒绝（I2 协议可达性）；
- /healthz。
"""

import asyncio
import json
import socket
import threading
import time

import httpx2
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from huaweicloud_open_mcp.deployment import build_app
from tests.test_assembly_sessions import _policy_file, make_args


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _HttpServer:
    """回环 uvicorn：驱动 build_app 产出的 MCPServer.streamable_http_app()。"""

    def __init__(self, app):
        self._asgi = app.streamable_http_app(host="127.0.0.1")
        self.port = _free_port()
        self._uv = uvicorn.Server(uvicorn.Config(
            self._asgi, host="127.0.0.1", port=self.port, log_level="error"))
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def __enter__(self):
        self._thread = threading.Thread(target=self._uv.run, daemon=True)
        self._thread.start()
        for _ in range(100):
            if self._uv.started:
                break
            time.sleep(0.1)
        assert self._uv.started, "uvicorn did not start"
        return self

    def __exit__(self, *exc):
        self._uv.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


async def _client_ops(url: str, ops: list[tuple[str, dict]]) -> list[dict]:
    out: list[dict] = []
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments in ops:
                result = await session.call_tool(name, arguments)
                out.append(json.loads(result.content[0].text))
    return out


def _manage(action: str, line: str | None = None,
            scope: str | None = None) -> tuple[str, dict]:
    args: dict = {"action": action}
    if line is not None:
        args["line"] = line
    if scope is not None:
        args["scope"] = scope
    return "manage_policy", args


# ---------- 双会话隔离 ----------

def test_two_sessions_grants_isolated(tmp_path):
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http"))
    with _HttpServer(app) as srv:
        async def run_two():
            a = _client_ops(srv.url, [_manage("add", "ECS:List*=allow", "session"),
                                      _manage("list")])
            b = _client_ops(srv.url, [_manage("add", "VPC:Show*=allow", "session"),
                                      _manage("list")])
            return await asyncio.gather(a, b)

        (a_out, b_out) = asyncio.run(run_two())
        # 两边 add 都成功（各自的会话桶）
        assert a_out[0]["ok"] and a_out[0]["scope"] == "session"
        assert b_out[0]["ok"] and b_out[0]["scope"] == "session"
        # A 的 list 只见自己的 ECS 授予（+文件），看不到 B 的 VPC 授予
        a_lines = [r["line"] for r in a_out[1]["rules"]]
        assert "ECS:List*=allow" in a_lines
        assert "VPC:Show*=allow" not in a_lines
        b_lines = [r["line"] for r in b_out[1]["rules"]]
        assert "VPC:Show*=allow" in b_lines
        assert "ECS:List*=allow" not in b_lines


# ---------- 重连即新会话 ----------

def test_reconnect_is_new_session(tmp_path):
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http"))
    with _HttpServer(app) as srv:
        first = asyncio.run(_client_ops(
            srv.url, [_manage("add", "ECS:List*=allow", "session")]))
        assert first[0]["ok"]

        second = asyncio.run(_client_ops(
            srv.url, [_manage("list"), _manage("remove", "ECS:List*=allow")]))
        assert "ECS:List*=allow" not in [r["line"] for r in second[0]["rules"]]
        assert not second[1]["ok"]
        assert "先前会话" in (second[1]["reason"] or "")


# ---------- permanent 跨会话共享 ----------

def test_permanent_rule_cross_session_visible(tmp_path):
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http"))
    with _HttpServer(app) as srv:
        asyncio.run(_client_ops(srv.url, [_manage("add", "ECS:List*=allow", "permanent")]))
        other = asyncio.run(_client_ops(srv.url, [_manage("list")]))
        assert "ECS:List*=allow" in [r["line"] for r in other[0]["rules"]]
        assert json.loads(open(path, encoding="utf-8").read()) == [
            "ECS:List*=allow", "*:*=deny"]


# ---------- 审计会话归因 ----------

def test_audit_events_carry_session(tmp_path):
    path = _policy_file(tmp_path, ["*:*=deny"])
    audit = tmp_path / "audit.jsonl"
    app = build_app(["openapi"], make_args(policy=path, transport="http",
                                           audit_file=str(audit)))
    with _HttpServer(app) as srv:
        asyncio.run(_client_ops(srv.url, [_manage("add", "ECS:List*=allow", "session")]))
        asyncio.run(_client_ops(srv.url, [_manage("list")]))
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    manage_events = [e for e in events if e.get("tool") == "manage_policy"]
    assert len(manage_events) >= 2
    sids = {e["session"] for e in manage_events}
    assert None not in sids
    assert len(sids) >= 2          # 两次连接两个会话
    for e in manage_events:
        assert "session" not in e["input"]   # provenance 不进 input 快照


# ---------- modern 单交换（无会话身份）：I2 协议可达 ----------

def test_modern_no_identity_rejected(tmp_path):
    """modern 2026-07-28 单交换协议（无 Mcp-Session-Id）打到 manage_policy：
    strict_sessions 结构化拒绝（I2 协议可达性的行为级证明）。"""
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http"))
    with _HttpServer(app) as srv:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "manage_policy",
                           "arguments": {"action": "add",
                                         "line": "ECS:List*=allow",
                                         "scope": "session"},
                           "_meta": {
                               "io.modelcontextprotocol/protocolVersion":
                                   "2026-07-28",
                               "io.modelcontextprotocol/clientCapabilities": {}}}}
        resp = httpx2.post(
            srv.url, json=body,
            headers={"MCP-Protocol-Version": "2026-07-28",
                     "Mcp-Method": "tools/call",
                     "Mcp-Name": "manage_policy",
                     "Accept": "application/json, text/event-stream"},
            timeout=10.0)
        assert resp.status_code == 200
        payload = _jsonrpc_result(resp)
        tool_text = json.loads(payload["result"]["content"][0]["text"])
        assert tool_text["ok"] is False
        assert "会话身份" in tool_text["reason"]


def _jsonrpc_result(resp) -> dict:
    """modern 路径响应形态：json_response=False 下为 SSE 流（data: 行）。"""
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return json.loads(resp.text)
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            data = json.loads(line[len("data: "):])
            if "result" in data:
                return data
    raise AssertionError(f"no jsonrpc result in response: {resp.text[:400]}")


# ---------- healthz ----------

def test_healthz(tmp_path):
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http"))
    with _HttpServer(app) as srv:
        resp = httpx2.get(f"http://127.0.0.1:{srv.port}/healthz", timeout=10.0)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "transport": "http"}


# ---------- execute 拒绝路径经 HTTP（未授予 → deny，预网络短路） ----------

def test_execute_denied_without_grant(tmp_path):
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http", mock=True))
    with _HttpServer(app) as srv:
        out = asyncio.run(_client_ops(
            srv.url, [("execute_api",
                       {"product": "ECS", "api": "ListServersDetails"})]))
        assert out[0]["ok"] is False
        assert "policy" in (out[0]["reason"] or "").lower() or "拒绝" in (out[0]["reason"] or "")
