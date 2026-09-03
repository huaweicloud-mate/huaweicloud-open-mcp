"""Elicitation 集成测试（E2）：真 mcp SDK client ↔ 真 MCPServer 内存回环。

验证 adapter（ctx_elicit_fn 归一化）与端到端契约：
拒绝→accept→会话内规则（不落盘）+granted_rule；decline→原样；off→从不发 elicitation；
不支持 elicitation 的客户端→auto 降级 / required 拒绝；显式 scope=permanent 才落盘。
元数据网络边界以 monkeypatch apie.http.fetch_json 封死（远端回退返回 None）。
"""

import asyncio
import json

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.types import ElicitResult

from apie.memory_store import MemoryStore
from common.elicit import parse_elicit_mode
from mcp_discover.config import DiscoverConfig
from mcp_discover.server import build_discover_app
from mcp_openapi.server import build_openapi_app
from mcp_openapi.service import ServiceConfig, ToolService
from safety.policy_store import PolicyStore

ACCEPT = ElicitResult(action="accept", content={"confirm": True})
REFUSE = ElicitResult(action="accept", content={"confirm": False})
DECLINE = ElicitResult(action="decline")

_OPENAPI_DOC = {
    "swagger": "2.0",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {
                "operationId": "ListServersDetails",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string",
                     "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "definitions": {},
}


class _StubMockClient:
    """mock lane 替身：确定性响应，不触网（元数据网络另行以 fetch_json 封死）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def mock_request(self, product, api_name, region, status_code=200, number=1):
        self.calls.append((product, api_name, region, status_code, number))
        return {"status": 200, "headers": {}, "body": {"mock": True}}


def script_client(script, seen):
    """脚本化 elicitation callback：按序应答并记录收到的 message。"""

    async def callback(context, params):
        seen.append(params.message)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return callback


def result_dict(res):
    """CallToolResult → dict（优先 structured_content，回退 text JSON）。"""
    data = res.structured_content
    if isinstance(data, dict):
        if "ok" in data:
            return data
        inner = data.get("result")
        if isinstance(inner, dict) and "ok" in inner:
            return inner
    return json.loads(res.content[0].text)


def policy_lines(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else data.splitlines()
    except json.JSONDecodeError:
        return path.read_text(encoding="utf-8").splitlines()


def make_openapi(tmp_path, mode):
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    store = MemoryStore()
    op = _OPENAPI_DOC["paths"]["/v1/{project_id}/cloudservers/detail"]["get"]
    store.set_api_cache(
        ("ecs", "ListServersDetails", "cn-north-4"),
        (_OPENAPI_DOC, "/v1/{project_id}/cloudservers/detail", "get", op),
    )
    svc = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=_StubMockClient))
    return build_openapi_app(svc, elicit_mode=parse_elicit_mode(mode)), p


def run(coro):
    return asyncio.run(coro)


# ---------- openapi 模式 ----------

def test_openapi_execute_denied_accept_grants_rule(tmp_path, monkeypatch):
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT, DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                # 规则已插到遮蔽它的 deny 之前 → 重试通过 policy（once 随本次执行焚毁）
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                # 第三次：once 已焚毁 → policy 再拒并重新提议（decline 保留拒绝）
                third = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                return first, second, third

    first, second, third = run(_run())
    assert first["ok"] is False
    assert first["granted_rule"] == "ECS:ListServersDetails=allow"
    assert "请重新调用" in first["reason"]
    assert "ECS:ListServersDetails=allow" in seen[0]
    assert "一次性" in seen[0]                            # 文案声明一次性语义
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)   # 授予不落盘
    assert second["ok"] is True                                 # 本次放行并真实执行（焚毁）
    assert third["ok"] is False
    assert "safety policy 拒绝执行" in third["reason"]          # once 已焚毁 → 再拒


def test_openapi_grant_is_once_scoped_restart_equivalent(tmp_path, monkeypatch):
    """授予为一次性：同一实例重试放行一次即焚毁；新建实例（重启等价）恢复拒绝。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _granted_run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([ACCEPT, DECLINE], [])) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                third = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                return first, second, third

    first, second, third = run(_granted_run())
    assert first["granted_rule"] == "ECS:ListServersDetails=allow"
    assert second["ok"] is True                                  # 同实例本次放行（焚毁）
    assert "safety policy 拒绝执行" in third["reason"]            # 用后即焚

    fresh_svc = ToolService(ServiceConfig(mock=True, policy_store=PolicyStore(str(p))))
    fresh = build_openapi_app(fresh_svc, elicit_mode="off")
    seen: list = []

    async def _fresh_run():
        async with InMemoryTransport(fresh) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))

    out = run(_fresh_run())
    assert "safety policy 拒绝执行" in out["reason"]              # 重启等价：规则已失
    assert out.get("granted_rule") is None


def test_openapi_execute_denied_decline_keeps_denial(tmp_path, monkeypatch):
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([DECLINE], [])) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))

    out = run(_run())
    assert out["ok"] is False and out.get("granted_rule") is None
    assert "safety policy 拒绝执行" in out["reason"]
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)  # 文件未变


def test_openapi_mode_off_never_elicits(tmp_path, monkeypatch):
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT, ACCEPT]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                exec_res = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                mp_res = result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "ECS:ListServers=allow"}))
                return exec_res, mp_res

    exec_res, mp_res = run(_run())
    assert seen == []                                     # 从未发起 elicitation
    assert exec_res.get("granted_rule") is None
    assert mp_res["ok"] is True                           # off 模式直接放行
    assert mp_res["scope"] == "session"                   # 默认会话内
    assert "ECS:ListServers=allow" not in policy_lines(p)  # 不落盘


def test_openapi_manage_policy_permanent_scope_persists(tmp_path, monkeypatch):
    """显式 scope=permanent：落盘且对新实例可见（off 模式免确认）。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "ECS:ListServers=allow",
                                      "scope": "permanent"}))

    out = run(_run())
    assert out["ok"] is True and out["scope"] == "permanent"
    assert "ECS:ListServers=allow" in policy_lines(p)


def test_openapi_unsupported_client_auto_degrades(tmp_path, monkeypatch):
    """客户端未声明 elicitation → ctx.elicit 失败 → auto 降级：拒绝原样/manage_policy 放行。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:          # 无 elicitation_callback
                await s.initialize()
                exec_res = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                mp_res = result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "ECS:GetObject=allow"}))
                return exec_res, mp_res

    exec_res, mp_res = run(_run())
    assert "safety policy 拒绝执行" in exec_res["reason"]
    assert exec_res.get("granted_rule") is None
    assert mp_res["ok"] is True
    assert "ECS:GetObject=allow" not in policy_lines(p)   # 默认会话内，不落盘


def test_openapi_manage_policy_confirm_flow(tmp_path):
    app, p = make_openapi(tmp_path, "auto")

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([REFUSE], [])) as s:
                await s.initialize()
                blocked = result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "OBS:GetObject=allow"}))
                granted = result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "OBS:GetObject=allow"}))
                listed = result_dict(await s.call_tool(
                    "manage_policy", {"action": "list"}))
                return blocked, granted, listed

    async def _granted():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([ACCEPT], [])) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "OBS:GetObject=allow"}))

    blocked = run(_run())[0]
    assert blocked["ok"] is False and "未确认" in blocked["reason"]
    granted = run(_granted())
    assert granted["ok"] is True
    assert granted["scope"] == "session"
    assert "OBS:GetObject=allow" not in policy_lines(p)   # 会话内授予不落盘


def test_openapi_required_unsupported_blocks_manage_policy(tmp_path):
    app, p = make_openapi(tmp_path, "required")

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "OBS:GetObject=allow"}))

    out = run(_run())
    assert out["ok"] is False
    assert "elicitation" in out["reason"]
    assert "OBS:GetObject=allow" not in policy_lines(p)   # 未落盘


# ---------- discover 模式 ----------

CATALOG_ENTRY = {
    "id": "@huaweicloud/ecs", "name": "ECS MCP Server",
    "display_name": "弹性云服务器 MCP", "description": "查询与管理 ECS",
    "category": "计算", "endpoint": "http://127.0.0.1:8200/mcp",
    "transport": "streamable-http", "auth": "none", "version": "1.0.0",
}


def make_discover(tmp_path, mode):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([CATALOG_ENTRY], ensure_ascii=False), encoding="utf-8")
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    config = DiscoverConfig(catalog_path=str(catalog),
                            policy_store=PolicyStore(str(p)))
    return build_discover_app(config, elicit_mode=parse_elicit_mode(mode)), p


def make_discover_with_endpoint(tmp_path, mode, endpoint, policy_entries):
    catalog = tmp_path / "catalog.json"
    entry = {**CATALOG_ENTRY, "endpoint": endpoint}
    catalog.write_text(json.dumps([entry], ensure_ascii=False), encoding="utf-8")
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy_entries, ensure_ascii=False), encoding="utf-8")
    config = DiscoverConfig(catalog_path=str(catalog),
                            policy_store=PolicyStore(str(p)))
    return build_discover_app(config, elicit_mode=parse_elicit_mode(mode)), p


def test_discover_connect_denied_accept_grants_rule(tmp_path):
    app, p = make_discover(tmp_path, "auto")
    seen = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([ACCEPT], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs"}))

    out = run(_run())
    assert out["ok"] is False
    assert out["granted_rule"] == "server:@huaweicloud/ecs=allow"
    assert seen and "server:@huaweicloud/ecs=allow" in seen[0]
    assert "server:@huaweicloud/ecs=allow" not in policy_lines(p)   # 授予不落盘


def test_discover_call_tool_denied_accept_grants_once(tmp_path):
    """call_tool 授予为一次性：accept → 重试放行（焚毁）→ 第三次再拒并重新提议。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "auto", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        seen, script = [], [ACCEPT, DECLINE]

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w,
                                         elicitation_callback=script_client(script, seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    call = {"server": "@huaweicloud/ecs", "tool": "list_servers",
                            "arguments": {}}
                    first = result_dict(await s.call_tool("call_server_tool", call))
                    second = result_dict(await s.call_tool("call_server_tool", call))
                    third = result_dict(await s.call_tool("call_server_tool", call))
                    return first, second, third

        first, second, third = run(_run())
    assert first["ok"] is False
    assert first["granted_rule"] == "server:@huaweicloud/ecs:list_servers=allow"
    assert "一次性" in seen[0]
    assert second["ok"] is True                                 # once 放行本次（并焚毁）
    assert third["ok"] is False
    assert "safety policy 拒绝调用" in third["reason"]           # 已焚毁 → 再拒
    assert ("server:@huaweicloud/ecs:list_servers=allow"
            not in policy_lines(p))                             # 授予不落盘


def test_discover_manage_policy_decline_keeps_file(tmp_path):
    app, p = make_discover(tmp_path, "auto")

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([DECLINE], [])) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "server:@huaweicloud/ecs=allow"}))

    out = run(_run())
    assert out["ok"] is False and "未确认" in out["reason"]
    assert "server:@huaweicloud/ecs=allow" not in policy_lines(p)
