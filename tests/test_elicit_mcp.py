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
ACCEPT_API = ElicitResult(action="accept", content={"choice": "api"})
ACCEPT_API_SESSION = ElicitResult(action="accept", content={"choice": "api_session"})
ACCEPT_PRODUCT = ElicitResult(action="accept", content={"choice": "product"})

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
        },
        "/v1/{project_id}/cloudservers": {
            "get": {
                "operationId": "ListServers",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string",
                     "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
    "definitions": {},
}


def _cache_entries(store):
    """两个 ECS 接口入缓存（产品级规则跨 API 放行验证用）。"""
    doc = _OPENAPI_DOC
    for path, method, api in (
            ("/v1/{project_id}/cloudservers/detail", "get", "ListServersDetails"),
            ("/v1/{project_id}/cloudservers", "get", "ListServers")):
        op = doc["paths"][path][method]
        store.set_api_cache(
            ("ecs", api, "cn-north-4"), (doc, path, method, op))


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
    _cache_entries(store)
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
    seen, script = [], [ACCEPT_API, DECLINE]

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
            async with ClientSession(r, w, elicitation_callback=script_client([ACCEPT_API, DECLINE], [])) as s:
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
    # 兜底指引直达 LLM：拒绝 reason 附带 question 问询 + manage_policy 授予口径
    assert "manage_policy" in exec_res["reason"]
    assert "question" in exec_res["reason"]
    assert "ECS:*=allow" in exec_res["reason"]            # 与三选一表单语义对齐
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


def test_openapi_execute_denied_product_choice_grants_session(tmp_path, monkeypatch):
    """choice=product：授予产品级会话内规则（ECS:*=allow），同产品另一 API
    直接放行且不再发起 elicitation；授予不落盘（session 档）。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_PRODUCT]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                other = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServers"}))
                return first, second, other

    first, second, other = run(_run())
    assert first["ok"] is False
    assert first["granted_rule"] == "ECS:*=allow"
    assert "会话内产品级规则" in first["reason"]
    assert "请重新调用" in first["reason"]
    assert "ECS:*=allow" in seen[0]                       # 弹窗并列产品级选项
    assert "ECS:*=allow" not in policy_lines(p)           # session 档不落盘
    assert second["ok"] is True                           # 会话内持续放行（不焚毁）
    assert other["ok"] is True                            # 同产品另一 API 免确认放行
    assert len(seen) == 1                                 # 未再发起 elicitation


def test_openapi_execute_denied_api_session_choice_grants_minimal_session(
        tmp_path, monkeypatch):
    """choice=api_session：授予最小规则（session 档）——同 API 会话内持续放行且
    不再发起 elicitation；同产品另一 API 仍被拒（单功能粒度，与 product 区分）；
    授予不落盘。换 API 的再拒经 decline 收场（脚本第二项）。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_API_SESSION, DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                other = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServers"}))
                return first, second, other

    first, second, other = run(_run())
    assert first["ok"] is False
    assert first["granted_rule"] == "ECS:ListServersDetails=allow"
    assert "会话内最小规则" in first["reason"]
    assert "请重新调用" in first["reason"]
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)   # session 档不落盘
    assert second["ok"] is True                            # 同 API 会话内持续放行（不焚毁）
    assert other["ok"] is False                            # 单功能粒度：另一 API 仍被拒
    assert "safety policy 拒绝执行" in other["reason"]
    assert len(seen) == 2                                  # 授予一次 + 换 API 拒绝提议一次（decline）


def test_openapi_execute_denied_api_choice_message_lists_four_options(tmp_path, monkeypatch):
    """coarse 提议弹窗文案并列四选项（api/api_session/product/none）与各自 scope 语义。"""
    app, _ = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_API]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))

    out = run(_run())
    assert out["granted_rule"] == "ECS:ListServersDetails=allow"
    msg = seen[0]
    assert "ECS:ListServersDetails=allow" in msg and "ECS:*=allow" in msg
    assert ("- api：" in msg and "- api_session：" in msg
            and "- product：" in msg and "- none：" in msg)
    assert "一次性" in msg and "会话" in msg


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
        seen, script = [], [ACCEPT_API, DECLINE]

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


def test_discover_call_tool_denied_product_choice_grants_session(tmp_path):
    """choice=product：授予服务级全工具规则（server:X:*=allow，session 档），
    换一工具直接放行且不再发起 elicitation；connect 路径不受影响（无 coarse 选项）。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "auto", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        seen, script = [], [ACCEPT_PRODUCT]

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w,
                                         elicitation_callback=script_client(script, seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    first = result_dict(await s.call_tool("call_server_tool", {
                        "server": "@huaweicloud/ecs", "tool": "list_servers",
                        "arguments": {}}))
                    second = result_dict(await s.call_tool("call_server_tool", {
                        "server": "@huaweicloud/ecs", "tool": "get_server",
                        "arguments": {"server_id": "srv-1"}}))
                    return first, second

        first, second = run(_run())
    assert first["ok"] is False
    assert first["granted_rule"] == "server:@huaweicloud/ecs:*=allow"
    assert "会话内产品级规则" in first["reason"]
    assert "server:@huaweicloud/ecs:*=allow" in seen[0]       # 弹窗并列服务级选项
    assert ("server:@huaweicloud/ecs:*=allow" not in policy_lines(p))  # session 档不落盘
    assert second["ok"] is True                               # 换工具免确认放行
    assert len(seen) == 1                                     # 未再发起 elicitation


def test_discover_call_tool_denied_api_session_choice_grants_session(tmp_path):
    """choice=api_session：授予最小工具规则（session 档）——同工具会话内持续放行，
    换工具仍被拒（单功能粒度，与 product 服务级全工具区分）；授予不落盘。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "auto", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        seen, script = [], [ACCEPT_API_SESSION, DECLINE]

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
                    other = result_dict(await s.call_tool("call_server_tool", {
                        "server": "@huaweicloud/ecs", "tool": "get_server",
                        "arguments": {"server_id": "srv-1"}}))
                    return first, second, other

        first, second, other = run(_run())
    assert first["ok"] is False
    assert first["granted_rule"] == "server:@huaweicloud/ecs:list_servers=allow"
    assert "会话内最小规则" in first["reason"]
    assert ("server:@huaweicloud/ecs:list_servers=allow"
            not in policy_lines(p))                             # session 档不落盘
    assert second["ok"] is True                                 # 同工具会话内持续放行（不焚毁）
    assert other["ok"] is False                                 # 单功能粒度：换工具仍被拒
    assert len(seen) == 2                                       # 授予一次 + 换工具拒绝提议一次


def test_discover_connect_message_has_no_product_option(tmp_path):
    """connect 提议无产品级选项：文案不出现三选项清单，仍是单一确认。"""
    app, _ = make_discover(tmp_path, "auto")
    seen = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([DECLINE], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs"}))

    run(_run())
    msg = seen[0]
    assert "server:@huaweicloud/ecs=allow" in msg
    assert "- product：" not in msg and "- none：" not in msg


def test_discover_off_denial_carries_fallback_hint(tmp_path):
    """off 模式 connect 拒绝：reason 附带兜底指引（单一选项口径）且从不弹窗。"""
    app, _ = make_discover(tmp_path, "off")
    seen = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs"}))

    out = run(_run())
    assert seen == []                                     # 从未发起 elicitation
    assert out.get("granted_rule") is None
    assert "safety policy 拒绝连接" in out["reason"]
    assert "manage_policy" in out["reason"] and "question" in out["reason"]
    assert "server:@huaweicloud/ecs=allow" in out["reason"]
    assert "product" not in out["reason"]                 # connect 无产品级选项
