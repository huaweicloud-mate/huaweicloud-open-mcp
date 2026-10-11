"""S22：HTTP 多会话 e2e——真 mcp SDK client × build_app 装配 × 回环 uvicorn。

全链验证（无外网、无凭证：仅 manage_policy list-only/healthz/execute 与确认路径）：
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

from common.sessions import current_session_key
from huaweicloud_open_mcp.deployment import build_app
from safety.policy_store import PolicyStore
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


_ECS_DOC = {
    "swagger": "2.0", "host": "ecs.cn-north-4.myhuaweicloud.com", "basePath": "/",
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {"get": {
            "operationId": "ListServersDetails",
            "parameters": [{"name": "project_id", "in": "path", "type": "string",
                            "required": True}],
            "responses": {"200": {"description": "OK"}}}},
        "/v1/{project_id}/cloudservers": {"get": {
            "operationId": "ListServers",
            "parameters": [{"name": "project_id", "in": "path", "type": "string",
                            "required": True}],
            "responses": {"200": {"description": "OK"}}}},
    },
    "definitions": {},
}


class _StubMock:
    def __init__(self):
        self.calls = []

    def mock_request(self, product, api_name, region, status_code=200, number=1):
        self.calls.append((product, api_name))
        return {"status": 200, "headers": {}, "body": {"mock": True}}


def _seeded_service(policy_path, *, audit_sink=None):
    """会话键控 seeded openapi service（mock lane；store 按 build_app 同款
    session_fn/strict_sessions 构造——S22 装配语义在注入 service 下等价）。"""
    from apie.api_location import ApiLocation
    from apie.memory_store import MemoryStore
    from mcp_openapi.service import ServiceConfig, ToolService

    store = MemoryStore()
    for api, path in (("ListServersDetails", "/v1/{project_id}/cloudservers/detail"),
                      ("ListServers", "/v1/{project_id}/cloudservers")):
        op = _ECS_DOC["paths"][path]["get"]
        store.set_api_cache(("ecs", api, "cn-north-4"),
                            ApiLocation(_ECS_DOC, path, "get", op))
    return ToolService(ServiceConfig(
        mock=True,
        policy_store=PolicyStore(str(policy_path),
                                 session_fn=current_session_key,
                                 strict_sessions=True),
        mock_client_factory=_StubMock, audit_sink=audit_sink),
        store=store)


async def _client_elicit(url: str, choice: str | None, api: str = "ListServersDetails"):
    """单连接：execute（denied→elicitation 提议→按 choice 应答）+ list。

    choice=None 模拟 decline（保持拒绝）。返回 (execute 结果, list 结果, 弹窗文案)。"""
    from mcp.types import ElicitResult

    prompts: list[str] = []

    async def callback(context, params):
        prompts.append(params.message)
        if choice is None:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"choice": choice})

    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write, elicitation_callback=callback) as session:
            await session.initialize()
            exec_res = json.loads((await session.call_tool(
                "execute_api", {"product": "ECS", "api": api}
            )).content[0].text)
            listed = json.loads((await session.call_tool(
                "manage_policy", {"action": "list"})).content[0].text)
            return exec_res, listed, prompts


# ---------- 双会话隔离 ----------

def test_two_sessions_grants_isolated(tmp_path, monkeypatch):
    """双会话隔离经 elicitation 授予路径（工具面写路径 2026-10 起仅确认路径）：
    A accept api_session → 授予落 A 的会话桶并重放执行；B decline → 仍拒，
    B 的 list 看不到 A 的授予（S22 会话键控全链）。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http", mock=True),
                    elicit_mode="auto", openapi_service=_seeded_service(path))
    with _HttpServer(app) as srv:
        # 双向隔离：两连接并发各授予不同 API，断言互不可见
        async def run_two():
            a = _client_elicit(srv.url, "api_session", "ListServersDetails")
            b = _client_elicit(srv.url, "api_session", "ListServers")
            return await asyncio.gather(a, b)

        (a_exec, a_list, a_prompts), (b_exec, b_list, b_prompts) = asyncio.run(run_two())

    assert a_exec["ok"] is True and a_exec["body"] == {"mock": True}
    assert a_prompts and "ECS:ListServersDetails=allow" in a_prompts[0]
    a_lines = [r["line"] for r in a_list["rules"]]
    assert "ECS:ListServersDetails=allow" in a_lines
    assert "ECS:ListServers=allow" not in a_lines      # B 的授予对 A 不可见
    assert b_exec["ok"] is True and b_exec["body"] == {"mock": True}
    assert b_prompts and "ECS:ListServers=allow" in b_prompts[0]
    b_lines = [r["line"] for r in b_list["rules"]]
    assert "ECS:ListServers=allow" in b_lines
    assert "ECS:ListServersDetails=allow" not in b_lines  # A 的授予对 B 不可见


# ---------- 重连即新会话 ----------

def test_reconnect_is_new_session(tmp_path, monkeypatch):
    """重连即新会话：连接 1 accept 授予并执行；连接 2 同 API 仍被拒
    （会话桶随连接消亡，不继承授予）。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http", mock=True),
                    elicit_mode="auto", openapi_service=_seeded_service(path))
    with _HttpServer(app) as srv:
        first_exec, first_list, _ = asyncio.run(_client_elicit(srv.url, "api_session"))
        second_exec, second_list, _ = asyncio.run(_client_elicit(srv.url, None))

    assert first_exec["ok"] is True
    assert "ECS:ListServersDetails=allow" in [r["line"] for r in first_list["rules"]]
    assert second_exec["ok"] is False                   # 新会话：无继承
    assert "ECS:ListServersDetails=allow" not in [
        r["line"] for r in second_list["rules"]]


# ---------- permanent 跨会话共享 ----------

def test_permanent_rule_cross_session_visible(tmp_path, monkeypatch):
    """持久规则唯一写面为操作员文件（热重载）：运行期编辑 policy 文件，
    另一会话立即可见（跨会话共享 + stat 热重载全链）。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http", mock=True),
                    elicit_mode="auto", openapi_service=_seeded_service(path))
    with _HttpServer(app) as srv:
        # 操作员运行期编辑（插到 deny 前，与 store 插位语义一致）
        with open(path, "w", encoding="utf-8") as f:
            json.dump(["ECS:List*=allow", "*:*=deny"], f)
        other_exec, other_list, _ = asyncio.run(_client_elicit(srv.url, None))
        lines = [r["line"] for r in other_list["rules"]]
        assert "ECS:List*=allow" in lines               # 热重载生效
        assert other_exec["ok"] is True                 # 文件规则放行执行
        assert json.loads(open(path, encoding="utf-8").read()) == [
            "ECS:List*=allow", "*:*=deny"]


# ---------- 审计会话归因 ----------

def test_audit_events_carry_session(tmp_path, monkeypatch):
    """审计会话归因经 elicitation 授予路径：两连接各授予一次（grant fn 直调
    service 层 manage_policy add），事件按连接归因到不同会话键。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    path = _policy_file(tmp_path, ["*:*=deny"])
    audit = tmp_path / "audit.jsonl"
    from common.audit import NdjsonAuditSink

    app = build_app(["openapi"], make_args(policy=path, transport="http", mock=True),
                    elicit_mode="auto",
                    openapi_service=_seeded_service(path,
                                                    audit_sink=NdjsonAuditSink(audit)))
    with _HttpServer(app) as srv:
        asyncio.run(_client_elicit(srv.url, "api_session"))
        asyncio.run(_client_elicit(srv.url, "api_session"))
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    add_events = [e for e in events if e.get("tool") == "manage_policy"
                  and e["input"].get("action") == "add"]
    assert len(add_events) == 2
    assert all(e["input"]["line"] == "ECS:ListServersDetails=allow" for e in add_events)
    sids = {e["session"] for e in add_events}
    assert None not in sids
    assert len(sids) == 2          # 两次连接两个会话
    for e in add_events:
        assert "session" not in e["input"]   # provenance 不进 input 快照


# ---------- modern 单交换（无会话身份）：I2 协议可达 ----------

def test_modern_no_identity_write_refused_at_tool_face(tmp_path):
    """modern 2026-07-28 单交换协议（无 Mcp-Session-Id）打到 manage_policy add：
    工具面 list-only 结构化拒绝（协议级写面载体已按设计退役——该协议形态无
    elicitation 回调通道，agent 可达写面为空；store 层 strict/I2 语义由 S18
    单测与组合行为测试覆盖）。"""
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
        assert "list" in tool_text["reason"] and "policy 文件" in tool_text["reason"]


def test_modern_no_identity_confirm_grant_rejected(tmp_path, monkeypatch):
    """modern 单交换 + off 模式 confirm=true：断言式授予路径在无会话身份下
    穿透为 strict_sessions 结构化拒绝（I2 fail-closed 覆盖被拒工具的写载体），
    不会静默落入共享 None 桶。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http", mock=True),
                    openapi_service=_seeded_service(path))
    with _HttpServer(app) as srv:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "execute_api",
                           "arguments": {"product": "ECS",
                                         "api": "ListServersDetails",
                                         "confirm": True},
                           "_meta": {
                               "io.modelcontextprotocol/protocolVersion":
                                   "2026-07-28",
                               "io.modelcontextprotocol/clientCapabilities": {}}}}
        resp = httpx2.post(
            srv.url, json=body,
            headers={"MCP-Protocol-Version": "2026-07-28",
                     "Mcp-Method": "tools/call",
                     "Mcp-Name": "execute_api",
                     "Accept": "application/json, text/event-stream"},
            timeout=10.0)
        assert resp.status_code == 200
        payload = _jsonrpc_result(resp)
        tool_text = json.loads(payload["result"]["content"][0]["text"])
        assert tool_text["ok"] is False
        assert "会话身份" in tool_text["reason"]      # I2 结构化拒绝，非静默 None 桶


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
