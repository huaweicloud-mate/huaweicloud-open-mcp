"""S21：build_app 会话装配（middleware + session_fn 单点配对 / strict_sessions
按传输置位 / HTTP 档 instructions 传输说明 / healthz）。

组合行为测试（复审 F4）：真 middleware + 真 contextvar + 真 PolicyStore 串全链
（零 mock），钉死结构断言与 e2e 之间的「contextvar 静默断裂」未覆盖带。
strict 接线经真 InMemoryTransport 工具面断言：http 档下无会话身份的
manage_policy add（session 档）结构化拒绝，stdio 档放行（legacy 语义）。
"""

import argparse
import asyncio
import json

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from common.sessions import SessionScopeMiddleware, current_session_key
from huaweicloud_open_mcp.deployment import (
    HTTP_TRANSPORT_NOTE,
    build_app,
)
from safety.policy_store import PolicyStore, manage_policy_ops
from tests.test_common_sessions import _Conn, _Ctx, _Req, _Session

pytestmark = pytest.mark.usefixtures("sealed_configs")


def make_args(**overrides):
    ns = argparse.Namespace(mock=True, policy=None, region=None, mock_base=None,
                            mock_passthrough=None, hints=None, audit_file=None)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _policy_file(tmp_path, lines):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _call_tool(app, name, arguments):
    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool(name, arguments)
                return json.loads(result.content[0].text)

    return asyncio.run(_run())


# ---------- 结构断言：middleware 单点注册 ----------

def test_build_app_registers_session_middleware(tmp_path):
    app = build_app(["openapi"], make_args(policy=_policy_file(tmp_path, ["*:*=deny"])))
    mws = [m for m in app.middleware if isinstance(m, SessionScopeMiddleware)]
    assert len(mws) == 1


# ---------- instructions：stdio 逐字节 / http 附加传输说明 ----------

def test_stdio_instructions_have_no_transport_note(tmp_path):
    app = build_app(["openapi"], make_args(policy=_policy_file(tmp_path, ["*:*=deny"])))
    assert "传输与会话边界" not in app.instructions


def test_http_instructions_append_transport_note(tmp_path):
    app = build_app(["openapi"], make_args(policy=_policy_file(tmp_path, ["*:*=deny"]),
                                           transport="http"))
    assert app.instructions.rstrip("\n").endswith(HTTP_TRANSPORT_NOTE.splitlines()[-1])
    assert "session 档规则仅对当前 MCP 连接会话生效" in app.instructions


# ---------- strict_sessions 按传输置位（经真工具面） ----------

def test_http_tool_face_is_list_only(tmp_path):
    """2026-10 起 manage_policy 工具面 list-only（build_app 外部缝合同契约）：
    add 结构化拒绝指向确认路径与操作员文件；store 层 strict/I2 语义由
    S18 单测与组合行为测试（F4）覆盖——协议级写面载体已按设计退役。"""
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http"))
    out = _call_tool(app, "manage_policy",
                     {"action": "add", "line": "ECS:List*=allow", "scope": "session"})
    assert out["ok"] is False
    assert "list" in out["reason"] and "confirm=true" in out["reason"]
    assert "policy 文件" in out["reason"]


def test_stdio_tool_face_is_list_only(tmp_path):
    path = _policy_file(tmp_path, ["*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path))
    out = _call_tool(app, "manage_policy",
                     {"action": "add", "line": "ECS:List*=allow", "scope": "session"})
    assert out["ok"] is False
    assert "list" in out["reason"]


def test_http_operator_file_rule_visible(tmp_path):
    """操作员文件路径（持久规则唯一写面）：预置规则经 build_app 装载、list 可见。"""
    path = _policy_file(tmp_path, ["ECS:List*=allow", "*:*=deny"])
    app = build_app(["openapi"], make_args(policy=path, transport="http"))
    out = _call_tool(app, "manage_policy", {"action": "list"})
    assert out["ok"] is True
    assert "ECS:List*=allow" in [r["line"] for r in out["rules"]]


# ---------- 组合行为测试（F4）：middleware → contextvar → store 全链 ----------

def test_composition_middleware_to_store_bucketing(tmp_path):
    """真 middleware 绑键 → 真 store 经 session_fn 读取 → 授予落对桶；
    他会话不可见。零 mock（fake ctx 仅替代 SDK ServerRequestContext 边界）。"""
    path = _policy_file(tmp_path, ["*:*=deny"])
    sessions = {"key": None}

    def session_fn():
        return sessions["key"]

    store = PolicyStore(path, session_fn=session_fn, strict_sessions=True)
    mw = SessionScopeMiddleware()

    async def add_in_session(key, line):
        sessions["key"] = key

        async def call_next(ctx):
            return manage_policy_ops(store, "add", line=line)

        return await mw(_Ctx(request=_Req({}), session=_Session(_Conn(key))), call_next)

    out_a = asyncio.run(add_in_session("sess-A", "ECS:List*=allow"))
    assert out_a["ok"] is True and out_a["scope"] == "session"

    # B 会话看不到 A 的授予（list）且执行被拒（authorize）
    sessions["key"] = "sess-B"
    assert all(r.scope == "permanent" for r in store.list_rules())
    assert store.authorize("ECS", "ListServers") is not None
    # A 会话放行
    sessions["key"] = "sess-A"
    assert store.authorize("ECS", "ListServers") is None
    assert current_session_key() is None  # middleware 退出后 contextvar 复位


def test_composition_strict_rejects_no_identity(tmp_path):
    """无会话身份（键 None）+ strict：manage_policy 结构化拒绝，None 桶不污染。"""
    path = _policy_file(tmp_path, ["*:*=deny"])
    store = PolicyStore(path, session_fn=current_session_key, strict_sessions=True)
    mw = SessionScopeMiddleware()

    async def call_next(ctx):
        return manage_policy_ops(store, "add", line="ECS:List*=allow")

    out = asyncio.run(mw(_Ctx(request=_Req({"mcp-session-id": ""}),
                              session=_Session(_Conn(None))), call_next))
    assert out["ok"] is False and "会话身份" in out["reason"]
    assert all(r.scope == "permanent" for r in store.list_rules())
