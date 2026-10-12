"""Elicitation 集成测试（E2）：真 mcp SDK client ↔ 真 MCPServer 内存回环。

验证 adapter（ctx_elicit_fn 归一化）与端到端契约：
拒绝→accept→授予→自动重放恰一次（单次工具调用直达执行结果；once 在重试的
授权门焚毁，重试先撞参数校验则不烧 once）；decline→原样；off→从不发
elicitation；不支持 elicitation 的客户端→auto 降级 / required 拒绝；显式
scope=permanent 才落盘；重试仍被拒时 reason 附已授予规则上下文。
元数据网络边界以 monkeypatch apie.http.fetch_json 封死（远端回退返回 None）。
"""

import asyncio
import json

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.types import ElicitResult

from apie.api_location import ApiLocation
from apie.memory_store import MemoryStore
from common.auth.credentials import Credentials
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
ACCEPT_READONLY = ElicitResult(action="accept", content={"choice": "readonly"})

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
        "/v1/{project_id}/cloudservers/{server_id}": {
            "delete": {
                "operationId": "DeleteServers",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string",
                     "required": True},
                    {"name": "server_id", "in": "path", "type": "string",
                     "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/v1/{project_id}/cloudservers/action": {
            "post": {
                "operationId": "BatchAction",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string",
                     "required": True},
                    {"name": "action", "in": "query", "type": "string",
                     "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
    "definitions": {},
}

_OBS_DOC = {
    "swagger": "2.0",
    "host": "obs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "paths": {
        "/{object_key}": {
            "get": {
                "operationId": "GetObject",
                "parameters": [
                    {"name": "bucket_name", "in": "query", "type": "string",
                     "required": True},
                    {"name": "object_key", "in": "path", "type": "string",
                     "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
    "definitions": {},
}


def _cache_entries(store):
    """三个 ECS 接口 + BatchAction + OBS GetObject 入缓存（产品级/只读规则集
    跨 API 放行与拒绝、重试校验失败保留 once、presign lane 重放验证用）。"""
    entries = (
        (_OPENAPI_DOC, "ecs", "/v1/{project_id}/cloudservers/detail", "get",
         "ListServersDetails"),
        (_OPENAPI_DOC, "ecs", "/v1/{project_id}/cloudservers", "get", "ListServers"),
        (_OPENAPI_DOC, "ecs", "/v1/{project_id}/cloudservers/{server_id}", "delete",
         "DeleteServers"),
        (_OPENAPI_DOC, "ecs", "/v1/{project_id}/cloudservers/action", "post",
         "BatchAction"),
        (_OBS_DOC, "obs", "/{object_key}", "get", "GetObject"),
    )
    for doc, product, path, method, api in entries:
        op = doc["paths"][path][method]
        store.set_api_cache((product, api, "cn-north-4"),
                            ApiLocation(doc, path, method, op))


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
    """CallToolResult → dict（structured_content 优先，回退 text JSON）。"""
    data = res.structured_content
    if isinstance(data, dict):
        return data
    return json.loads(res.content[0].text)


def policy_lines(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else data.splitlines()
    except json.JSONDecodeError:
        return path.read_text(encoding="utf-8").splitlines()


def make_openapi(tmp_path, mode, *, mock_client_factory=_StubMockClient,
                 audit_sink=None):
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    store = MemoryStore()
    _cache_entries(store)
    svc = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=mock_client_factory,
        audit_sink=audit_sink))
    return build_openapi_app(svc, elicit_mode=parse_elicit_mode(mode)), p


def run(coro):
    return asyncio.run(coro)


# ---------- openapi 模式 ----------

def test_openapi_execute_denied_accept_grants_rule(tmp_path, monkeypatch):
    """accept api（once）：单次工具调用内完成 拒绝→确认→授予→重放，直接返回
    执行结果；once 随重放焚毁，下一次调用再拒。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_API, DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                return first, second

    first, second = run(_run())
    assert first["ok"] is True                          # 确认后自动重放，直接返回执行结果
    assert first["body"] == {"mock": True}              # 真实执行（mock lane）
    assert "ECS:ListServersDetails=allow" in seen[0]
    assert "一次性" in seen[0]                            # 文案声明一次性语义
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)   # 授予不落盘
    assert second["ok"] is False                        # once 已焚毁 → 再拒（decline 收场）
    assert "safety policy 拒绝执行" in second["reason"]
    assert len(seen) == 2                               # 两次问询（授予 + decline），重放不弹窗


def test_openapi_grant_is_once_scoped_restart_equivalent(tmp_path, monkeypatch):
    """授予为一次性：确认后重放恰一次即焚毁（第二次调用再拒）；新建实例
    （重启等价）恢复拒绝。"""
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
                return first, second

    first, second = run(_granted_run())
    assert first["ok"] is True                                  # 重放放行（焚毁）
    assert second["ok"] is False
    assert "safety policy 拒绝执行" in second["reason"]            # 用后即焚

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
    assert out["ok"] is False
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
                mp_add = result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "ECS:ListServers=allow"}))
                mp_list = result_dict(await s.call_tool(
                    "manage_policy", {"action": "list"}))
                return exec_res, mp_add, mp_list

    exec_res, mp_add, mp_list = run(_run())
    assert seen == []
    assert "grant=" in exec_res["reason"]
    assert "question" in exec_res["reason"]
    assert "policy 文件" in exec_res["reason"]
    assert "ECS:*=allow" in exec_res["reason"]
    assert mp_add["ok"] is False
    assert "grant=" in mp_add["reason"] and "policy 文件" in mp_add["reason"]
    assert mp_list["ok"] is True
    assert "ECS:ListServers=allow" not in policy_lines(p)


def test_manage_policy_tool_face_read_only_all_modes(tmp_path, monkeypatch):
    """工具面 list-only 为 mode 无关契约：off/auto/required 下 add/remove 一律
    结构化拒绝（指向三条合法授予路径），list 照常返回规则与文件全文；
    授予经 elicitation/confirm 的内部路径不受影响（grant fn 直调 service 层）。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    for mode in ("off", "auto", "required"):
        app, p = make_openapi(tmp_path, mode)

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    add = result_dict(await s.call_tool(
                        "manage_policy", {"action": "add", "line": "OBS:GetObject=allow"}))
                    perm = result_dict(await s.call_tool(
                        "manage_policy", {"action": "add", "line": "ECS:*=allow",
                                          "scope": "permanent"}))
                    remove = result_dict(await s.call_tool(
                        "manage_policy", {"action": "remove", "line": "*=deny"}))
                    listed = result_dict(await s.call_tool(
                        "manage_policy", {"action": "list"}))
                    return add, perm, remove, listed

        add, perm, remove, listed = run(_run())
        assert add["ok"] is False and "list" in add["reason"]
        assert perm["ok"] is False                      # 红线：permanent（唯一写盘档）拒绝
        assert remove["ok"] is False and "policy 文件" in remove["reason"]
        assert listed["ok"] is True and listed.get("rules") is not None
        assert "OBS:GetObject=allow" not in policy_lines(p)
        assert "ECS:*=allow" not in policy_lines(p)     # 文件字节未变


def test_openapi_unsupported_client_auto_degrades(tmp_path, monkeypatch):
    """客户端未声明 elicitation → auto 降级：拒绝附操作员文件指引（不提被忽略
    的 confirm）；manage_policy 写面拒绝（list-only）。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                exec_res = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                mp_res = result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "ECS:GetObject=allow"}))
                return exec_res, mp_res

    exec_res, mp_res = run(_run())
    assert "safety policy 拒绝执行" in exec_res["reason"]
    assert "操作员" in exec_res["reason"] and "policy 文件" in exec_res["reason"]
    assert "confirm=true" not in exec_res["reason"]
    assert mp_res["ok"] is False
    assert "ECS:GetObject=allow" not in policy_lines(p)



def test_openapi_execute_denied_product_choice_grants_session(tmp_path, monkeypatch):
    """choice=product：授予产品级会话内规则（ECS:*=allow），确认后重放直接执行；
    同产品另一 API 直接放行且不再发起 elicitation；授予不落盘（session 档）。"""
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
    assert first["ok"] is True                           # 确认后自动重放，直接执行
    assert "ECS:*=allow" in seen[0]                       # 弹窗并列产品级选项
    assert "ECS:*=allow" not in policy_lines(p)           # session 档不落盘
    assert second["ok"] is True                           # 会话内持续放行（不焚毁）
    assert other["ok"] is True                            # 同产品另一 API 免确认放行
    assert len(seen) == 1                                 # 未再发起 elicitation


def test_openapi_execute_denied_api_session_choice_grants_minimal_session(
        tmp_path, monkeypatch):
    """choice=api_session：授予最小规则（session 档）——确认后重放直接执行，
    同 API 会话内持续放行且不再发起 elicitation；同产品另一 API 仍被拒
    （单功能粒度，与 product 区分）；授予不落盘。"""
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
    assert first["ok"] is True                            # 确认后重放直接执行
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)   # session 档不落盘
    assert second["ok"] is True                            # 同 API 会话内持续放行（不焚毁）
    assert other["ok"] is False                            # 单功能粒度：另一 API 仍被拒
    assert "safety policy 拒绝执行" in other["reason"]
    assert len(seen) == 2                                  # 授予一次 + 换 API 拒绝提议一次（decline）


def test_openapi_execute_denied_api_choice_message_lists_five_options(tmp_path, monkeypatch):
    """coarse 提议弹窗文案并列五选项与 scope 语义，会话档选项前置。

    openapi 的 offer 恒挂 readonly_rules（service 层无条件构造），弹窗实际
    五选项（api_session/readonly/product/api/none）；无 readonly 的四选项
    形状由 E1 单测（COARSE_OFFER fixture）覆盖。"""
    app, _ = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))

    out = run(_run())
    assert out["ok"] is False
    msg = seen[0]
    assert "ECS:ListServersDetails=allow" in msg and "ECS:*=allow" in msg
    assert ("- api：" in msg and "- api_session：" in msg
            and "- product：" in msg and "- none：" in msg)
    assert "- readonly：" in msg                           # openapi 恒挂只读规则集
    assert msg.index("- api_session：") < msg.index("- product：") < msg.index("- api：")
    assert "一次性" in msg and "会话" in msg


def test_openapi_accept_redispatch_audit_sequence_and_dispatch_once(
        tmp_path, monkeypatch):
    """单次工具调用的审计序列与 agent 手动重试的既有口径一致：
    execute 拒绝 → manage_policy add → execute 重放（mock dispatch 恰一次）。"""
    stub = _StubMockClient()
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            # 逐事件 JSON 序列化快照（NDJSON 等价观察，防引用别名）；
            # _spill 护栏：控制键若入快照在场，service 入口拷贝边界即成立
            events.append(json.loads(json.dumps(event)))

    app, p = make_openapi(tmp_path, "auto", mock_client_factory=lambda: stub,
                          audit_sink=_Sink())
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([ACCEPT_API], [])) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "params": {"limit": 5, "_spill": False}}))

    out = run(_run())
    assert out["ok"] is True
    assert len(stub.calls) == 1                       # 首次拒绝未 dispatch，重放恰一次
    assert [e["tool"] for e in events] == ["execute_api", "manage_policy", "execute_api"]
    assert [e["ok"] for e in events] == [False, True, True]
    assert events[1]["input"]["line"] == "ECS:ListServersDetails=allow"
    assert events[1]["input"]["scope"] == "once"
    assert events[0]["input"] == events[2]["input"]   # 重放入参快照与首次一致
    # 入口拷贝护栏：控制键原样入快照（service 的 dict(params) 拷贝若被移除，
    # _spill/_jsonpath 剥离将污染调用方 dict → 此断言失败）
    assert events[0]["input"]["params"]["_spill"] is False


def test_openapi_retry_validation_failure_preserves_once_grant(tmp_path, monkeypatch):
    """重放先撞参数校验（先于授权门返回）：once 不焚毁，为修正参数后的下一次
    调用保留；校验失败信封原样返回（前缀门控不注授予上下文）。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_API]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction"}))
                fixed = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "params": {"action": "reboot"}}))
                return first, fixed

    first, fixed = run(_run())
    assert first["ok"] is False
    assert first["reason"] == "缺少必填 query 参数 action（get_api 可查参数定义）"   # 原样返回，不注上下文
    assert fixed["ok"] is True                            # 修正后经 once 放行（此处焚毁）
    assert len(seen) == 1                                 # 全程恰一次问询


def test_openapi_execute_presign_denied_accept_grants_once_redispatch(
        tmp_path, monkeypatch):
    """presign lane（service 独立授权门）：确认后重放返回预签发信封，once 焚毁。"""
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    store = MemoryStore()
    _cache_entries(store)
    svc = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p)),
        mock_client_factory=_StubMockClient,
        credentials=Credentials(ak="AK9", sk="SK9")))
    app = build_openapi_app(svc, elicit_mode="auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_API, DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                args = {"product": "OBS", "api": "GetObject",
                        "params": {"bucket_name": "my-bucket",
                                   "object_key": "a b.zip", "_presign": True}}
                first = result_dict(await s.call_tool("execute_api", args))
                second = result_dict(await s.call_tool("execute_api", args))
                return first, second

    first, second = run(_run())
    assert first["ok"] is True                       # 确认后重放，直接返回预签发信封
    ps = first["presign"]
    assert ps["method"] == "GET" and ps["expires_in"] == 900
    assert ps["url"].startswith("https://my-bucket.obs.cn-north-4.myhuaweicloud.com/")
    assert "AccessKeyId=AK9" in ps["url"]
    assert second["ok"] is False                     # once 已在 presign 授权门焚毁
    assert "safety policy 拒绝执行" in second["reason"]
    assert len(seen) == 2


# ---------- discover 模式 ----------

CATALOG_ENTRY = {
    "id": "@huaweicloud/ecs", "name": "ECS MCP Server",
    "display_name": "弹性云服务器 MCP", "description": "查询与管理 ECS",
    "category": "计算", "endpoint": "http://127.0.0.1:8200/mcp",
    "transport": "streamable-http", "auth": "none", "version": "1.0.0",
}


def make_discover(tmp_path, mode, *, audit_sink=None):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([CATALOG_ENTRY], ensure_ascii=False), encoding="utf-8")
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    config = DiscoverConfig(catalog_path=str(catalog),
                            policy_store=PolicyStore(str(p)),
                            audit_sink=audit_sink)
    return build_discover_app(config, elicit_mode=parse_elicit_mode(mode)), p


def make_discover_with_endpoint(tmp_path, mode, endpoint, policy_entries,
                                *, audit_sink=None):
    catalog = tmp_path / "catalog.json"
    entry = {**CATALOG_ENTRY, "endpoint": endpoint}
    catalog.write_text(json.dumps([entry], ensure_ascii=False), encoding="utf-8")
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy_entries, ensure_ascii=False), encoding="utf-8")
    config = DiscoverConfig(catalog_path=str(catalog),
                            policy_store=PolicyStore(str(p)),
                            audit_sink=audit_sink)
    return build_discover_app(config, elicit_mode=parse_elicit_mode(mode)), p


def test_discover_connect_denied_accept_grants_rule(tmp_path):
    """connect accept：单次调用内完成 拒绝→确认→授予（会话内）→重放连接；
    弹窗如实陈述会话内档（不再误称一次性）。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(tmp_path, "auto", stub.endpoint, ["*=deny"])
        seen = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([ACCEPT], seen)) as s:
                    await s.initialize()
                    return result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))

        out = run(_run())
    assert out["ok"] is True                          # 确认后自动重放连接
    assert out["server"] == "@huaweicloud/ecs"
    assert "server:@huaweicloud/ecs=allow" in seen[0]
    assert "会话内" in seen[0] and "一次性" not in seen[0]   # scope 如实渲染
    assert "server:@huaweicloud/ecs=allow" not in policy_lines(p)   # 授予不落盘


def test_discover_call_tool_denied_accept_grants_once(tmp_path):
    """call_tool 授予为一次性：accept → 重放放行（焚毁）→ 下一次调用再拒并重新提议。"""
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
                    return first, second

        first, second = run(_run())
    assert first["ok"] is True                                 # 确认后重放，代发成功
    assert "一次性" in seen[0]
    assert second["ok"] is False
    assert "safety policy 拒绝调用" in second["reason"]           # 已焚毁 → 再拒（decline 收场）
    assert ("server:@huaweicloud/ecs:list_servers=allow"
            not in policy_lines(p))                             # 授予不落盘
    assert len(seen) == 2                                       # 重放不弹窗


def test_discover_manage_policy_decline_keeps_file(tmp_path):
    """discover 侧 manage_policy 工具面同契约：add 结构化拒绝（list-only），
    文件不变。"""
    app, p = make_discover(tmp_path, "auto")

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "server:@huaweicloud/ecs=allow"}))

    out = run(_run())
    assert out["ok"] is False and "list" in out["reason"]
    assert "server:@huaweicloud/ecs=allow" not in policy_lines(p)


def test_discover_call_tool_denied_product_choice_grants_session(tmp_path):
    """choice=product：授予服务级全工具规则（server:X:*=allow，session 档），
    确认后重放直接执行；换一工具直接放行且不再发起 elicitation。"""
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
    assert first["ok"] is True                               # 确认后重放直接执行
    assert "server:@huaweicloud/ecs:*=allow" in seen[0]       # 弹窗并列服务级选项
    assert ("server:@huaweicloud/ecs:*=allow" not in policy_lines(p))  # session 档不落盘
    assert second["ok"] is True                               # 换工具免确认放行
    assert len(seen) == 1                                     # 未再发起 elicitation


def test_discover_call_tool_denied_api_session_choice_grants_session(tmp_path):
    """choice=api_session：授予最小工具规则（session 档）——确认后重放直接执行，
    同工具会话内持续放行，换工具仍被拒（单功能粒度，与 product 区分）。"""
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
    assert first["ok"] is True                                 # 确认后重放直接执行
    assert ("server:@huaweicloud/ecs:list_servers=allow"
            not in policy_lines(p))                             # session 档不落盘
    assert second["ok"] is True                                 # 同工具会话内持续放行（不焚毁）
    assert other["ok"] is False                                 # 单功能粒度：换工具仍被拒
    assert len(seen) == 2                                       # 授予一次 + 换工具拒绝提议一次


def test_discover_connect_message_has_no_product_option(tmp_path):
    """connect 提议无产品级选项：文案不出现选项清单，仍是单一确认。"""
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
    assert "safety policy 拒绝连接" in out["reason"]
    assert "confirm=true" in out["reason"]                # off 档快路径指引
    assert "policy 文件" in out["reason"]
    assert "server:@huaweicloud/ecs=allow" in out["reason"]
    assert "product" not in out["reason"]                 # connect 无产品级选项


def test_openapi_required_mode_accept_redispatches(tmp_path, monkeypatch):
    """required 档 + 支持 elicitation 的客户端：拒绝提议照常弹窗，accept 后
    重放直接返回执行结果（与 auto 同构；required 仅在客户端不支持时 fail-closed，
    该路径由 manage_policy 测试覆盖）。"""
    app, p = make_openapi(tmp_path, "required")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_API]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))

    out = run(_run())
    assert out["ok"] is True                       # accept 后重放直接执行
    assert len(seen) == 1                          # 恰一次问询
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)   # once 不落盘


def test_discover_connect_grant_then_dead_endpoint_returns_connect_failure(tmp_path):
    """connect 授予成功后重放撞死端点：连接失败信封原样返回（非 policy 拒绝，
    不注授予上下文——自纠面为 endpoint 可达性）；会话规则已生效，再次调用
    直接重连（不再提议）。"""
    app, p = make_discover_with_endpoint(
        tmp_path, "auto", "http://127.0.0.1:1/mcp", ["*=deny"])
    # 端口 1（特权端口）恒无监听：真实 TCP 拒绝路径，且不与示例 catalog 的
    # 8200 端口撞车（同机跑真实 discover 部署不翻转本测试）
    seen, script = [], [ACCEPT]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                second = result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                return first, second

    first, second = run(_run())
    assert first["ok"] is False
    assert first["reason"].startswith("连接失败")               # 非 policy 拒绝原样返回
    assert "已确认授予" not in first["reason"]                   # 不注授予上下文
    assert second["ok"] is False and second["reason"].startswith("连接失败")
    assert len(seen) == 1                                       # 会话规则生效，直接重连不再提议


def test_openapi_execute_denied_readonly_choice_grants_four_session_rules(
        tmp_path, monkeypatch):
    """choice=readonly：授予产品级只读规则集（List/Show/Get/Query 四条 session 规则）。

    确认后重放（List 系命中只读集）直接执行；同产品 List 系直接放行不再问询；
    Delete 系不匹配只读集仍拒（新一次提议，经 decline 收场）；授予不落盘
    （session 档）。同次调用内 readonly 误选写接口的上下文另见 mispick 测试。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_READONLY, DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                listed = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServers"}))
                deleted = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "DeleteServers"}))
                return first, second, listed, deleted

    first, second, listed, deleted = run(_run())
    assert first["ok"] is True                             # List 命中只读集，重放直接执行
    assert "readonly" in seen[0] and "*List*" in seen[0]   # 弹窗并列只读选项
    lines = policy_lines(p)
    assert not any("*List*" in ln for ln in lines)          # session 档不落盘
    assert second["ok"] is True                             # 会话内持续放行（不焚毁）
    assert listed["ok"] is True                             # *List* 放行同产品其它只读
    assert deleted["ok"] is False                           # Delete 不匹配只读集 → 新一次提议（decline 收场）
    assert "safety policy 拒绝执行" in deleted["reason"]
    assert len(seen) == 2                                   # 授予一次 + Delete 拒绝提议一次（decline）


def test_openapi_readonly_mispick_retry_denied_appends_context(tmp_path, monkeypatch):
    """readonly 误选写接口（首次拒绝即写 API）：重放仍被拒，reason 附已授予
    只读规则上下文（指引再次调用重新提议），不二次弹窗。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_READONLY]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "DeleteServers"}))

    out = run(_run())
    assert out["ok"] is False
    assert out["reason"].startswith("safety policy 拒绝执行 ECS:DeleteServers")
    assert "ECS:*List*=allow" in out["reason"]           # 已授予规则上下文
    assert "再次调用将重新进入确认授予流程" in out["reason"]
    assert len(seen) == 1                                 # 不二次弹窗


# ---------- off 模式 confirm 快路径（单次调用直达执行结果） ----------

def test_openapi_off_confirm_single_call_executes(tmp_path, monkeypatch):
    """off 模式 confirm=true：单次工具调用内完成 拒绝→授予（once）→重放执行；
    授予经 manage_policy add 入审计（与手动授予同形——off 档信任模型不变）；
    once 焚毁后无 confirm 的再次调用被拒并附 confirm 兜底指引。"""
    stub = _StubMockClient()
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    app, p = make_openapi(tmp_path, "off", mock_client_factory=lambda: stub,
                          audit_sink=_Sink())
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "confirm": True}))
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                return first, second

    first, second = run(_run())
    assert first["ok"] is True and first["body"] == {"mock": True}
    assert len(stub.calls) == 1                    # 首次拒绝未 dispatch，重放恰一次
    assert seen == []                              # off 恒不问询（断言式确认）
    assert [e["tool"] for e in events[:3]] == ["execute_api", "manage_policy", "execute_api"]
    assert [e["ok"] for e in events] == [False, True, True, False]  # 第 4 事件为无 confirm 的再拒
    assert events[1]["input"]["line"] == "ECS:ListServersDetails=allow"
    assert events[1]["input"]["scope"] == "once"
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)   # 不落盘
    assert second["ok"] is False                   # once 已焚毁
    assert "safety policy 拒绝执行" in second["reason"]
    assert "grant=" in second["reason"]            # 兜底指引提及断言快路径


def test_openapi_auto_confirm_does_not_bypass_prompt(tmp_path, monkeypatch):
    """auto 模式 confirm 被忽略：协议问询照常发生（decline 保持拒绝）——
    断言式确认不得绕过部署开启的确认门。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "confirm": True}))

    out = run(_run())
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in out["reason"]
    assert len(seen) == 1                          # 问询确实发生，confirm 未绕过
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)


def test_discover_connect_off_confirm_single_call(tmp_path):
    """connect confirm=true：单次调用内授予（会话内档）并连接；后续连接免确认。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(tmp_path, "off", stub.endpoint,
                                             ["*=deny"])
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    first = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs",
                                               "confirm": True}))
                    second = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    return first, second

        first, second = run(_run())
    assert first["ok"] is True                     # 同次调用内授予+连接
    assert second["ok"] is True                    # 会话内规则，免确认免问询
    assert seen == []
    assert "server:@huaweicloud/ecs=allow" not in policy_lines(p)


def test_discover_call_tool_off_confirm_once(tmp_path):
    """call_tool confirm=true：单次调用内授予（once）并代发；再次调用需重新确认。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "off", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    call = {"server": "@huaweicloud/ecs", "tool": "list_servers",
                            "arguments": {}}
                    first = result_dict(await s.call_tool(
                        "call_server_tool", {**call, "confirm": True}))
                    second = result_dict(await s.call_tool("call_server_tool", call))
                    return first, second

        first, second = run(_run())
    assert first["ok"] is True                     # 同次调用内授予+代发
    assert second["ok"] is False                   # once 已焚毁
    assert "safety policy 拒绝调用" in second["reason"]
    assert "grant=" in second["reason"]            # 兜底指引提及断言快路径
    assert ("server:@huaweicloud/ecs:list_servers=allow"
            not in policy_lines(p))


def test_openapi_auto_unsupported_confirm_ignored_no_loop_guidance(
        tmp_path, monkeypatch):
    """auto + 不支持 elicitation 客户端 + confirm=true：confirm 被忽略、降级
    hint 不提 confirm（指引被静态忽略的快路径=无变化重试循环）；指引指向
    该模式下可用的 manage_policy 恢复路径。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:          # 无 elicitation_callback
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "confirm": True}))

    out = run(_run())
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in out["reason"]
    assert "confirm=true" not in out["reason"]      # 循环守卫：不指引被忽略的快路径
    assert "操作员" in out["reason"]                # 指向操作员 policy 文件恢复路径
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)


def test_openapi_off_confirm_validation_failure_preserves_once(
        tmp_path, monkeypatch):
    """off confirm 重放先撞参数校验（先于授权门返回）：once 不焚毁，为修正
    参数后的下一次调用保留；校验失败信封原样返回（不注授予上下文）。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "confirm": True}))
                fixed = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "params": {"action": "reboot"}}))
                return first, fixed

    first, fixed = run(_run())
    assert first["ok"] is False
    assert first["reason"] == "缺少必填 query 参数 action（get_api 可查参数定义）"
    assert fixed["ok"] is True                            # 修正后经 once 放行（此处焚毁）
    assert seen == []                                     # off 恒不问询


def test_discover_connect_off_confirm_dead_endpoint(tmp_path):
    """off confirm 授予后重放撞死端点：连接失败信封原样返回（非 policy 拒绝
    不注上下文）；会话规则已生效，再次调用免 confirm 直接重连（仍连失败）。"""
    app, p = make_discover_with_endpoint(
        tmp_path, "off", "http://127.0.0.1:1/mcp", ["*=deny"])
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs",
                                           "confirm": True}))
                second = result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                return first, second

    first, second = run(_run())
    assert first["ok"] is False and first["reason"].startswith("连接失败")
    assert "已确认授予" not in first["reason"]           # 非 policy 失败不注上下文
    assert second["ok"] is False and second["reason"].startswith("连接失败")
    assert seen == []                                     # off 恒不问询


# ---------- manage_policy 工具面 list-only（InMemoryTransport 往返） ----------

def test_manage_policy_batch_write_refused_at_tool_face(tmp_path, monkeypatch):
    """批量 line 数组 add 同样在工具面拒绝（list-only，mode 无关）；规则不落盘。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy",
                    {"action": "add", "line": ["ECS:*List*=allow", "ECS:*Get*=allow"]}))

    out = run(_run())
    assert out["ok"] is False and "list" in out["reason"]
    assert "policy 文件" in out["reason"]               # 批量写面仅操作员文件可达
    assert seen == []                                   # 无任何问询（写拒绝在问询之前）
    assert not any("*List*" in ln for ln in policy_lines(p))



def test_manage_policy_tool_schema_accepts_array(tmp_path):
    """工具 schema：line 为 anyOf[string, array[string], null]（union 参数可发现）。"""
    app, _ = make_openapi(tmp_path, "off")

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                return {t.name: t for t in tools.tools}

    tools = run(_run())
    schema = tools["manage_policy"].input_schema
    line_schema = schema["properties"]["line"]
    types = {opt.get("type") for opt in line_schema["anyOf"]}
    assert types == {"string", "array", "null"}


def test_openapi_refused_write_attempt_audited(tmp_path, monkeypatch):
    """被拒的 add 尝试入审计 NDJSON（ok=False，input 快照含 action/line/scope）——
    agent 自授尝试是审计要捕捉的安全事件（拒绝先于 @audited service 方法返回，
    由包装层补记）。"""
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    app, p = make_openapi(tmp_path, "off", audit_sink=_Sink())
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy", {"action": "add", "line": "OBS:GetObject=allow",
                                      "scope": "permanent"}))

    out = run(_run())
    assert out["ok"] is False
    refused = [e for e in events if e["tool"] == "manage_policy" and e["ok"] is False]
    assert len(refused) == 1                           # 拒绝事件恰一条
    assert refused[0]["input"]["action"] == "add"
    assert refused[0]["input"]["line"] == "OBS:GetObject=allow"
    assert refused[0]["input"]["scope"] == "permanent"


def test_openapi_wildcard_target_no_offer_no_grant(tmp_path, monkeypatch):
    """crafted 通配目标不构造 offer：confirm=true、grant=<档位> 与 declined
    elicitation 均无授予发生（最小规则载体不接受元字符/空白目标；coarse_rule
    是唯一合法通配载体）——一次性全局/产品级放行不可经断言式获得。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                off_confirm = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "*", "confirm": True}))
                off_grant = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "*",
                                    "grant": "product"}))
                blank_target = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "",
                                    "grant": "api_session"}))
                return off_confirm, off_grant, blank_target

    off_confirm, off_grant, blank_target = run(_run())
    for out in (off_confirm, off_grant, blank_target):
        assert out["ok"] is False
        assert "safety policy 拒绝执行" in out["reason"]
    assert seen == []                                  # 无 offer → 无问询
    assert not any(ln == "*:*=allow" or ln == "ECS:*=allow"
                   for ln in policy_lines(p))          # 无任何授予落盘


# ---------- off 模式 grant 档位参数（断言式授予与问询表单同构，2026-10） ----------

def test_openapi_off_grant_api_session_single_call_and_persistent(
        tmp_path, monkeypatch):
    """off + grant=api_session：单次调用内完成 拒绝→授予（session）→重放执行；
    第二次调用不经 grant 直接放行（会话内持续，与问询路径 api_session 同构）；
    审计 add 事件 source=assertion（断言式授予 provenance 可区分）。"""
    stub = _StubMockClient()
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    app, p = make_openapi(tmp_path, "off", mock_client_factory=lambda: stub,
                          audit_sink=_Sink())
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api_session"}))
                second = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))
                return first, second

    first, second = run(_run())
    assert first["ok"] is True and first["body"] == {"mock": True}
    assert second["ok"] is True                        # 会话内免确认持续放行
    assert seen == []                                  # off 恒不问询
    assert len(stub.calls) == 2                        # 两次均 dispatch
    adds = [e for e in events if e["tool"] == "manage_policy"
            and e["input"].get("action") == "add"]
    assert len(adds) == 1
    assert adds[0]["input"]["line"] == "ECS:ListServersDetails=allow"
    assert adds[0]["input"]["scope"] == "session"
    assert adds[0]["input"]["source"] == "assertion"   # 断言式授予溯源
    assert "ECS:ListServersDetails=allow" not in policy_lines(p)


def test_openapi_off_grant_product_covers_other_apis(tmp_path, monkeypatch):
    """off + grant=product：产品级 session 规则，同产品其它 API 免确认放行。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "product"}))
                other = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServers"}))
                return first, other

    first, other = run(_run())
    assert first["ok"] is True
    assert other["ok"] is True                         # 产品级会话放行
    assert seen == []
    assert "ECS:*=allow" not in policy_lines(p)        # session 档不落盘


def test_openapi_off_grant_readonly_write_still_denied_with_context(
        tmp_path, monkeypatch):
    """off + grant=readonly：四条只读规则 session 档授予；只读 API 放行，
    写接口（DeleteServers）仍拒且 reason 附已授予规则上下文。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                listing = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "readonly"}))
                write = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "DeleteServers",
                                    "params": {"server_id": "s-1"},
                                    "grant": "readonly"}))
                return listing, write

    listing, write = run(_run())
    assert listing["ok"] is True                       # 只读规则集放行
    assert write["ok"] is False                        # 写接口不在只读集（重试仍拒）
    assert "safety policy 拒绝执行" in write["reason"]
    assert "ECS:*List*=allow" in write["reason"]       # 已授予规则上下文
    assert "再次调用将重新进入确认授予流程" in write["reason"]
    assert seen == []


def test_openapi_off_confirm_equals_grant_api_envelope(tmp_path, monkeypatch):
    """confirm=true ≡ grant="api"：同会话先后两轮（每轮 once 焚毁后再拒）
    的执行信封逐字节一致——归一化构造保证（同规则同档位同信封路径）。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                via_confirm = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "confirm": True}))
                via_grant = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api"}))
                return via_confirm, via_grant

    via_confirm, via_grant = run(_run())
    assert via_confirm["ok"] is True and via_grant["ok"] is True
    assert via_confirm == via_grant                    # 信封同一性


def test_openapi_off_grant_wins_over_confirm(tmp_path, monkeypatch):
    """grant 与 confirm 并传：grant 优先（product 档授予，同产品其它 API
    放行——非 once）。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], [])) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "confirm": True, "grant": "product"}))
                other = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServers"}))
                return first, other

    first, other = run(_run())
    assert first["ok"] is True
    assert other["ok"] is True                         # product 档（grant 胜出）


def test_openapi_auto_grant_ignored_protocol_form_pops(tmp_path, monkeypatch):
    """auto + grant：被忽略，协议问询照常发生（decline 保持拒绝）——
    断言式档位不得绕过部署开启的确认门（与 confirm 同口径）。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [DECLINE]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "product"}))

    out = run(_run())
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in out["reason"]
    assert len(seen) == 1                              # 问询确实发生，grant 未绕过
    assert "ECS:*=allow" not in policy_lines(p)


def test_openapi_auto_elicit_grant_audited_with_source(tmp_path, monkeypatch):
    """auto 问询路径授予：审计 add 事件 source=elicit（协议确认 provenance，
    与 assertion 可区分）。"""
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    app, p = make_openapi(tmp_path, "auto", audit_sink=_Sink())
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen, script = [], [ACCEPT_API_SESSION]

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client(script, seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails"}))

    out = run(_run())
    assert out["ok"] is True
    adds = [e for e in events if e["tool"] == "manage_policy"
            and e["input"].get("action") == "add"]
    assert len(adds) == 1
    assert adds[0]["input"]["scope"] == "session"
    assert adds[0]["input"]["source"] == "elicit"      # 协议问询确认溯源


def test_openapi_auto_unsupported_grant_ignored_no_loop(tmp_path, monkeypatch):
    """auto + 不支持 elicitation 客户端 + grant=product：grant 被忽略、降级
    hint 明示参数被忽略（不兜售 grant/confirm——无变化重试循环守卫），
    仅指向操作员 policy 文件。"""
    app, p = make_openapi(tmp_path, "auto")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:          # 无 elicitation_callback
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "product"}))

    out = run(_run())
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in out["reason"]
    assert "被忽略" in out["reason"]                     # 明示参数在此部署无效
    assert "grant=" not in out["reason"]                 # 循环守卫
    assert "操作员" in out["reason"]
    assert "ECS:*=allow" not in policy_lines(p)


def test_openapi_off_grant_persists_after_non_policy_failure(
        tmp_path, monkeypatch):
    """grant=product 后重试撞参数校验（非 policy 失败，先于授权门）：
    session 产品级规则存续（有意行为——会话授权不随单次执行失败回收），
    同产品后续调用免确认。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                broken = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "grant": "product"}))
                other = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServers"}))
                return broken, other

    broken, other = run(_run())
    assert broken["ok"] is False                        # 重试撞校验，信封原样
    assert "缺少必填" in broken["reason"]
    assert "已授予" not in broken["reason"]              # 非 policy 失败不注上下文
    assert other["ok"] is True                          # session 规则存续，免确认


def test_openapi_off_grant_after_preserved_once_executes_via_once(
        tmp_path, monkeypatch):
    """残留 once（重试先撞校验保留）使同目标下一次调用直接放行（经 once
    焚毁）；该调用携带的 grant=api_session 因未发生 policy 拒绝而不触发
    授予——成功信封 reason 披露「所请求会话档未落地」（档位吞噬可见，
    2026-10 披露红线）；再次拒绝时重新问询（store 层的 once 吸收修复为
    并发守卫，见 test_policy_store）。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "confirm": True}))
                fixed = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "params": {"action": "reboot"},
                                    "grant": "api_session"}))
                third = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "params": {"action": "reboot"}}))
                return first, fixed, third

    first, fixed, third = run(_run())
    assert first["ok"] is False and "缺少必填" in first["reason"]   # once 保留
    assert fixed["ok"] is True                         # 经残留 once 放行（焚毁）
    assert "grant=api_session" in fixed["reason"]      # 档位未落地披露
    assert "会话档未落地" in fixed["reason"]
    assert third["ok"] is False                        # once 已焚毁 → 再拒
    assert "safety policy 拒绝执行" in third["reason"]


def test_openapi_off_grant_tier_present_disclosure_silent(
        tmp_path, monkeypatch):
    """会话档已落位时的防御性重传：披露静默（规则在 session/permanent 层
    存在 → unapplied_tier_note 返回 None），成功信封不带 reason 噪音。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                granted = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api_session"}))
                defensive = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api_session"}))
                return granted, defensive

    granted, defensive = run(_run())
    assert granted["ok"] is True and defensive["ok"] is True
    assert defensive.get("reason") is None               # 已落位 → 静默
    assert "grant=api_session" not in (defensive.get("reason") or "")


def _tool_schemas(app):
    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                return {t.name: t for t in tools.tools}
    return run(_run())


def _enum_of(prop: dict) -> list | None:
    """可选参数的 enum 值（SDK 把 ``Literal | None`` 包成 anyOf[enum, null]）。"""
    for variant in prop.get("anyOf", []):
        if "enum" in variant:
            return variant["enum"]
    return prop.get("enum")


def test_openapi_grant_schema_four_tiers(tmp_path, monkeypatch):
    """execute_api grant 参数 schema：四档位 enum（含 readonly）+ confirm 布尔。"""
    app, _ = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    tools = _tool_schemas(app)
    props = tools["execute_api"].input_schema["properties"]
    assert _enum_of(props["grant"]) == ["api", "api_session", "product", "readonly"]
    assert props["confirm"]["type"] == "boolean"


def test_discover_grant_schema_excludes_readonly(tmp_path):
    """call_server_tool grant 参数 schema：三档位 enum（无 readonly——
    discover 侧数据缺席，schema 层即不可达）；connect 不携带 grant
    （confirm-only，会话内档）。"""
    app, _ = make_discover_with_endpoint(tmp_path, "off", "http://127.0.0.1:1/mcp",
                                         ["*=deny"])
    tools = _tool_schemas(app)
    props = tools["call_server_tool"].input_schema["properties"]
    assert _enum_of(props["grant"]) == ["api", "api_session", "product"]
    assert "readonly" not in (_enum_of(props["grant"]) or [])
    assert "grant" not in tools["connect_mcp_server"].input_schema["properties"]


def test_discover_off_grant_api_session(tmp_path):
    """discover off + grant=api_session：单次调用内授予（session）+代发；
    第二次同工具调用免确认。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "off", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    call = {"server": "@huaweicloud/ecs", "tool": "list_servers",
                            "arguments": {}}
                    first = result_dict(await s.call_tool(
                        "call_server_tool", {**call, "grant": "api_session"}))
                    second = result_dict(await s.call_tool("call_server_tool", call))
                    return first, second

        first, second = run(_run())
    assert first["ok"] is True                         # 同次调用内授予+代发
    assert second["ok"] is True                        # 会话内免确认
    assert seen == []
    assert ("server:@huaweicloud/ecs:list_servers=allow"
            not in policy_lines(p))


def test_discover_elicit_form_excludes_readonly(tmp_path):
    """discover elicit 表单为四选一（DiscoverGrantChoiceConfirm）：requestedSchema
    enum 不含 readonly——表单层即不可选（与消息文本/工具参数三处值域一致；
    2026-10 两传输对称收尾）。decline 保持拒绝。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "auto", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        captured: list = []

        async def _capture(context, params):
            captured.append(params)
            return DECLINE

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w,
                                         elicitation_callback=_capture) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    return result_dict(await s.call_tool(
                        "call_server_tool", {"server": "@huaweicloud/ecs",
                                             "tool": "list_servers",
                                             "arguments": {}}))

        out = run(_run())
    assert out["ok"] is False                        # decline 保持拒绝
    assert "safety policy 拒绝调用" in out["reason"]
    assert len(captured) == 1
    props = captured[0].requested_schema["properties"]
    assert props["choice"]["enum"] == ["api_session", "product", "api", "none"]
    assert "readonly" not in props["choice"]["enum"]
    assert ("server:@huaweicloud/ecs:list_servers=allow"
            not in policy_lines(p))                  # 未发生授予


def test_confirm_grant_strict_sessions_failure_envelope(tmp_path):
    """strict store（HTTP 无会话身份）+ session 档断言授予：add 被拒，失败
    信封不附 hint、不再兜售机制（重传 grant 即无变化循环）；MutationResult
    reason 自带 permanent 档指引。"""
    import functools

    from common.elicit import PolicyConsent, confirmed_grant_and_retry

    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    store = PolicyStore(str(p), strict_sessions=True)
    svc = ToolService(store=MemoryStore(), config=ServiceConfig(
        mock=True, policy_store=store))
    offer = svc.policy_denial_offer("ECS", "ListServersDetails")
    assert offer is not None

    async def _no_elicit(message, schema):
        raise AssertionError("off 模式不经问询")

    consent = PolicyConsent("off", _no_elicit,
                            functools.partial(svc.manage_policy, "add",
                                              source="assertion"))
    denial = {"ok": False, "reason": offer.reason}

    def _retry():
        raise AssertionError("授予失败不应重放")

    out = run(confirmed_grant_and_retry(consent, offer, denial, _retry,
                                        choice="api_session"))
    assert out["ok"] is False
    assert "自动授予" in out["reason"] and "失败" in out["reason"]
    assert "会话身份" in out["reason"]                   # strict 拒绝原因透出
    assert "grant=" not in out["reason"] and "confirm=true" not in out["reason"]


def test_discover_off_grant_audited_with_source(tmp_path):
    """discover MCP 路径的授予审计（2026-10 补齐验证）：grant=api_session 断言
    授予 → manage_policy add 事件 source=assertion + line/scope；dispatch 审计
    同调用入账 call_server_tool（ok=True）——授予→执行链两半齐备。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "off", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"],
            audit_sink=_Sink())
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    return result_dict(await s.call_tool(
                        "call_server_tool", {"server": "@huaweicloud/ecs",
                                             "tool": "list_servers",
                                             "arguments": {},
                                             "grant": "api_session"}))

        out = run(_run())
    assert out["ok"] is True
    adds = [e for e in events if e["tool"] == "manage_policy"
            and e["input"].get("action") == "add"]
    assert len(adds) == 1                              # 断言式授予恰一条
    assert adds[0]["input"]["line"] == "server:@huaweicloud/ecs:list_servers=allow"
    assert adds[0]["input"]["scope"] == "session"
    assert adds[0]["input"]["source"] == "assertion"   # provenance 可区分
    dispatches = [e for e in events if e["tool"] == "call_server_tool"]
    assert len(dispatches) == 2                        # 按尝试入账：初始拒绝 + 重放
    assert [d["ok"] for d in dispatches] == [False, True]
    assert dispatches[1]["input"]["server"] == "@huaweicloud/ecs"
    assert dispatches[1]["input"]["tool"] == "list_servers"


def test_discover_connect_dispatch_audited(tmp_path):
    """connect dispatch 审计：连接调用（含拒绝路径）入账 connect_mcp_server。"""
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    app, p = make_discover(tmp_path, "off", audit_sink=_Sink())

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs"}))

    out = run(_run())
    assert out["ok"] is False                          # *=deny 拒绝
    assert len(events) == 1
    assert events[0]["tool"] == "connect_mcp_server"
    assert events[0]["ok"] is False
    assert events[0]["input"]["server"] == "@huaweicloud/ecs"


def test_discover_refused_write_attempt_audited(tmp_path):
    """discover 工具面写尝试拒绝入审计 NDJSON（openapi 先例的镜像——
    拒绝先于 @audited service 方法返回，包装层补记）。"""
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    app, p = make_discover(tmp_path, "off", audit_sink=_Sink())

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "manage_policy", {"action": "add",
                                      "line": "server:@huaweicloud/ecs=allow"}))

    out = run(_run())
    assert out["ok"] is False
    refused = [e for e in events if e["tool"] == "manage_policy" and e["ok"] is False]
    assert len(refused) == 1
    assert refused[0]["input"]["action"] == "add"
    assert refused[0]["input"]["line"] == "server:@huaweicloud/ecs=allow"
    assert "server:@huaweicloud/ecs=allow" not in policy_lines(p)


def test_discover_auto_grant_ignored_protocol_form_pops(tmp_path):
    """discover auto + grant=product：grant 被忽略，协议问询照常发生
    （decline 保持拒绝）——断言式档位不得绕过部署开启的确认门。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "auto", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        seen, script = [], [DECLINE]

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w,
                                         elicitation_callback=script_client(script, seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    return result_dict(await s.call_tool(
                        "call_server_tool", {"server": "@huaweicloud/ecs",
                                             "tool": "list_servers",
                                             "arguments": {},
                                             "grant": "product"}))

        out = run(_run())
    assert out["ok"] is False
    assert "safety policy 拒绝调用" in out["reason"]
    assert len(seen) == 1                              # 问询确实发生，grant 未绕过
    assert "server:@huaweicloud/ecs:*=allow" not in policy_lines(p)


def test_resolve_assertion_choice_matrix():
    """断言归一化单点：grant 优先（含与 confirm 并传）；confirm=true ≡
    grant="api"；两者皆无 → None。"""
    from common.elicit import resolve_assertion_choice

    assert resolve_assertion_choice("product", False) == "product"
    assert resolve_assertion_choice("product", True) == "product"   # grant 胜出
    assert resolve_assertion_choice("api_session", False) == "api_session"
    assert resolve_assertion_choice(None, True) == "api"            # confirm 等价
    assert resolve_assertion_choice(None, False) is None


def test_unapplied_tier_note_presence_matrix():
    """披露注语义判定：调用后策略复查（still_denied）None=持久层覆盖 →
    静默；非 None → 披露文案（尽力判定——焚毁叙事为最可能解释）。"""
    from common.elicit import unapplied_tier_note

    assert unapplied_tier_note("api_session", None) is None      # 持久层覆盖
    denial = "safety policy 拒绝执行 ECS:ListServers"
    note = unapplied_tier_note("api_session", denial)
    assert note is not None and "grant=api_session" in note and "会话档未落地" in note


def test_apply_tier_disclosure_matrix():
    """披露注统一落位 helper（两 wrapper 共享）：still_denied None →
    原内容返回；披露附 reason；既有 reason 前置拼接不覆写。"""
    from common.elicit import apply_tier_disclosure

    silent_in = {"ok": True, "body": {"mock": True}}
    out = apply_tier_disclosure(silent_in, "api_session", None)
    assert out == silent_in and out is not silent_in     # copy-on-write 等值新 dict

    denial = "safety policy 拒绝执行 ECS:ListServers"
    disclosed = apply_tier_disclosure(silent_in, "product", denial)
    assert disclosed["ok"] is True and "grant=product" in disclosed["reason"]

    prior = {"ok": True, "reason": "readonly 部分授予回执"}
    concat = apply_tier_disclosure(prior, "api_session", denial)
    assert concat["reason"].startswith("readonly 部分授予回执；")
    assert "grant=api_session" in concat["reason"]      # 多源 reason 不覆写


def test_openapi_off_grant_disclosure_snapshot_mode_silent(tmp_path, monkeypatch):
    """无 store（快照模式，嵌入方 build_app(openapi_service=…) 可达）：
    披露恒静默——快照无 overlay/once 概念，焚毁叙事不可兑现（round-4 修复
    的回归红线：此前附全假披露且指引不可交付）。"""
    from apie.memory_store import MemoryStore
    from mcp_openapi.service import ServiceConfig, ToolService
    from safety.policy import PolicyRule

    store = MemoryStore()
    _cache_entries(store)
    svc = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_rules=[PolicyRule("ECS", "*", True)]))   # 快照规则放行
    app = build_openapi_app(svc, elicit_mode=parse_elicit_mode("off"))
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api_session"}))

    out = run(_run())
    assert out["ok"] is True                            # 快照规则放行
    assert out.get("reason") is None                    # 无 store → 恒静默


def test_openapi_off_grant_disclosure_semantic_silence(tmp_path, monkeypatch):
    """静默判定为语义覆盖而非文本比对（第三轮检视修复的回归红线）：
    更宽持久规则覆盖（文件 allowlist 通配 / 此前产品级会话授予 / 大小写
    变体 / 被遮蔽 allow 行）→ 成功信封不带披露注（防御性重传零噪音）。"""
    app, p = make_openapi(tmp_path, "off")          # policy: ["*=deny"]
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                product_first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "product"}))
                covered = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api_session"}))     # 更宽会话规则覆盖
                return product_first, covered

    product_first, covered = run(_run())
    assert product_first["ok"] is True
    assert covered["ok"] is True
    assert covered.get("reason") is None              # ECS:*=allow 会话规则覆盖 → 静默


def test_openapi_off_grant_disclosure_wildcard_file_allowlist_silent(
        tmp_path, monkeypatch):
    """文件 allowlist 通配覆盖（快速开始档形）：grant=api_session 成功调用
    不带披露注——「该档已随本次执行焚毁」的假叙事不再产生。"""
    from apie.memory_store import MemoryStore
    from mcp_openapi.service import ServiceConfig, ToolService
    from safety.policy_store import PolicyStore

    p = tmp_path / "policy.json"
    p.write_text(json.dumps(["ECS:*List*=allow", "*=deny"]), encoding="utf-8")
    store = MemoryStore()
    _cache_entries(store)
    svc = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p))))
    app = build_openapi_app(svc, elicit_mode=parse_elicit_mode("off"))
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api_session"}))

    out = run(_run())
    assert out["ok"] is True                          # 文件通配放行
    assert out.get("reason") is None                  # 永久层覆盖 → 静默


def test_openapi_off_grant_disclosure_case_variant_and_shadow(
        tmp_path, monkeypatch):
    """大小写变体（evaluate 大小写不敏感）与遮蔽 allow 行（首条 deny 生效）
    两方向：前者静默、后者披露——语义判定由策略评估器裁决。"""
    from apie.memory_store import MemoryStore
    from mcp_openapi.service import ServiceConfig, ToolService
    from safety.policy_store import PolicyStore

    # 大小写变体：文件 ecs:*list*=allow 放行 ECS:ListServersDetails
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(["ecs:*list*=allow", "*=deny"]), encoding="utf-8")
    store = MemoryStore()
    _cache_entries(store)
    svc = ToolService(store=store, config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p))))
    app = build_openapi_app(svc, elicit_mode=parse_elicit_mode("off"))
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)

    async def _run_case():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], [])) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "ListServersDetails",
                                    "grant": "api_session"}))

    out = run(_run_case())
    assert out["ok"] is True
    assert out.get("reason") is None                  # 大小写不敏感覆盖 → 静默

    # 遮蔽 allow：文件里 allow 行被首条 deny 遮蔽（首匹配生效）→ 调用本应
    # 被拒，仅经残留 once 放行 → 披露
    p2 = tmp_path / "policy2.json"
    p2.write_text(json.dumps(["*=deny", "ECS:ListServersDetails=allow"]),
                 encoding="utf-8")
    store2 = MemoryStore()
    _cache_entries(store2)
    svc2 = ToolService(store=store2, config=ServiceConfig(
        mock=True, policy_store=PolicyStore(str(p2))))
    app2 = build_openapi_app(svc2, elicit_mode=parse_elicit_mode("off"))

    async def _run_shadow():
        async with InMemoryTransport(app2) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], [])) as s:
                await s.initialize()
                first = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "confirm": True}))
                fixed = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "BatchAction",
                                    "params": {"action": "reboot"},
                                    "grant": "api_session"}))
                return first, fixed

    first, fixed = run(_run_shadow())
    assert first["ok"] is False and "缺少必填" in first["reason"]   # once 保留
    assert fixed["ok"] is True                        # 经残留 once 放行（焚毁）
    assert "grant=api_session" in fixed["reason"]     # 遮蔽行不算覆盖 → 披露


def test_discover_off_grant_tier_disclosure_silent(tmp_path):
    """discover wrapper 级披露——静默路径：会话规则已落位的同档重传不带
    披露注。披露正路径（once 焚毁致档位未落地）经
    test_discover_off_grant_disclosure_positive_path 覆盖（store 预置残留
    once 构造等价状态）。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "off", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"])
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    call = {"server": "@huaweicloud/ecs", "tool": "list_servers",
                            "arguments": {}}
                    granted = result_dict(await s.call_tool(
                        "call_server_tool", {**call, "grant": "api_session"}))
                    defensive = result_dict(await s.call_tool(
                        "call_server_tool", {**call, "grant": "api_session"}))
                    return granted, defensive

        granted, defensive = run(_run())
    assert granted["ok"] is True and defensive["ok"] is True
    assert defensive.get("reason") is None            # 会话规则已落位 → 静默


def test_discover_disconnect_dispatch_audited(tmp_path):
    """disconnect dispatch 审计（round-7 直挂的回归钉）：事件 tool 字段为
    工具面名（name= 映射 load-bearing——方法名 disconnect 不等于工具面名，
    去掉映射则静默错标）；input 快照为 {server}；无 policy 门恒入账。"""
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    app, p = make_discover(tmp_path, "off", audit_sink=_Sink())
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "disconnect_mcp_server", {"server": "@huaweicloud/ecs"}))

    out = run(_run())
    assert out["ok"] is True and out["released"] is False   # 无连接（未 connect）
    disconnects = [e for e in events if e["tool"] == "disconnect_mcp_server"]
    assert len(disconnects) == 1                            # 无 policy 门恒入账
    assert disconnects[0]["input"] == {"server": "@huaweicloud/ecs"}
    assert disconnects[0]["ok"] is True
    assert not any(e["tool"] == "disconnect" for e in events)  # 方法名不泄漏


def test_discover_grant_path_three_audit_events(tmp_path):
    """grant 路径三事件审计形状（对齐 openapi confirm 路径先例）：初始拒绝
    （call_server_tool ok=False）→ manage_policy add → 重放（ok=True）。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    with StubMcpServer() as stub:
        app, p = make_discover_with_endpoint(
            tmp_path, "off", stub.endpoint,
            ["server:@huaweicloud/ecs=allow", "*=deny"],
            audit_sink=_Sink())
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    return result_dict(await s.call_tool(
                        "call_server_tool", {"server": "@huaweicloud/ecs",
                                             "tool": "list_servers",
                                             "arguments": {},
                                             "grant": "api_session"}))

        out = run(_run())
    assert out["ok"] is True
    tools_seq = [e["tool"] for e in events]
    # 初始拒绝 → 授予 → 重放（按尝试入账，拒绝动机可重建）
    assert tools_seq == ["connect_mcp_server", "call_server_tool",
                         "manage_policy", "call_server_tool"]
    dispatches = [e for e in events if e["tool"] == "call_server_tool"]
    assert [d["ok"] for d in dispatches] == [False, True]
    adds = [e for e in events if e["tool"] == "manage_policy"]
    assert adds[0]["input"]["source"] == "assertion"


def test_discover_form_schema_unit():
    """DiscoverGrantChoiceConfirm 单元 schema（对齐 GrantChoiceConfirm 先例）：
    单 primitive 枚举字段、四值、拒绝未知值。"""
    from common.elicit import DiscoverGrantChoiceConfirm

    schema = DiscoverGrantChoiceConfirm.model_json_schema()
    props = schema["properties"]
    assert set(props) == {"choice"}
    assert props["choice"]["enum"] == ["api_session", "product", "api", "none"]
    assert props["choice"]["type"] == "string"
    assert schema["required"] == ["choice"]
    assert DiscoverGrantChoiceConfirm(choice="product").choice == "product"
    with pytest.raises(ValueError):
        DiscoverGrantChoiceConfirm(choice="readonly")  # type: ignore[arg-type]


def test_is_write_refusal_predicate():
    """写尝试拒绝谓词：add/remove + ok=False（文案改写不影响触发）；
    归一化（空白/大小写）独立断言（round-4 修复复合断言自弱化）。"""
    from common.elicit import is_write_refusal

    assert is_write_refusal("add", {"ok": False, "reason": "任何文案"})
    assert is_write_refusal(" Remove ", {"ok": False}) is True   # 空白归一化
    assert is_write_refusal("ADD", {"ok": False}) is True        # 大小写归一化
    assert not is_write_refusal("list", {"ok": True})
    assert not is_write_refusal("add", {"ok": True})          # 委派成功非拒绝
    assert not is_write_refusal("list", {"ok": False})        # list 拒绝非写尝试


def test_discover_retry_failure_audited(tmp_path):
    """重试确实执行但失败（死端点）：重放结果入账——service 层 @audited
    按尝试语义（round-4 修复的回归红线：此前 _retried 前缀启发式对此类
    reason 形态误判漏记）。事件链交错序：初始拒绝 → 授予 → 重试失败，
    重试事件的 reason 为连接失败文案（与初始 policy 拒绝可区分）。"""
    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    # 死端点：授予成功后重试真实执行并失败（连接拒绝）
    app, p = make_discover_with_endpoint(
        tmp_path, "off", "http://127.0.0.1:1/mcp", ["*=deny"],
        audit_sink=_Sink())
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                return result_dict(await s.call_tool(
                    "connect_mcp_server", {"server": "@huaweicloud/ecs",
                                           "confirm": True}))

    out = run(_run())
    assert out["ok"] is False                        # 授予成功、重试失败（死端点）
    assert [e["tool"] for e in events] == [
        "connect_mcp_server", "manage_policy", "connect_mcp_server"]
    connect_events = [e for e in events if e["tool"] == "connect_mcp_server"]
    assert len(connect_events) == 2                  # 初始拒绝 + 重试失败均入账
    assert connect_events[0]["ok"] is False          # 初始拒绝（授予动机）
    assert connect_events[1]["ok"] is False          # 重试失败（重放确实执行）
    adds = [e for e in events if e["tool"] == "manage_policy"]
    assert [e["ok"] for e in adds] == [True]         # 授予成功（session 档连接规则）
    assert ("server:@huaweicloud/ecs=allow" not in policy_lines(p))


def test_discover_grant_failure_not_retried_not_dup_audited(tmp_path):
    """授予失败（strict store 拒绝 session 档）：重试未执行 → 不产生第二条
    dispatch 事件（失败经 manage_policy add ok=False 入账已充分）；reason
    文案前缀形态（denial.reason + 失败详情）不再是入账依据。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(json.loads(json.dumps(event)))

    with StubMcpServer() as stub:
        catalog = tmp_path / "catalog.json"
        entry = {**CATALOG_ENTRY, "endpoint": stub.endpoint}
        catalog.write_text(json.dumps([entry], ensure_ascii=False), encoding="utf-8")
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(["server:@huaweicloud/ecs=allow", "*=deny"],
                                ensure_ascii=False), encoding="utf-8")
        config = DiscoverConfig(catalog_path=str(catalog),
                                policy_store=PolicyStore(str(p),
                                                         strict_sessions=True),
                                audit_sink=_Sink())
        app = build_discover_app(config, elicit_mode=parse_elicit_mode("off"))
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True       # connect 有文件规则（非 strict 目标）
                    return result_dict(await s.call_tool(
                        "call_server_tool", {"server": "@huaweicloud/ecs",
                                             "tool": "list_servers",
                                             "arguments": {},
                                             "grant": "api_session"}))

        out = run(_run())
    assert out["ok"] is False                        # 授予失败保持拒绝
    assert "自动授予" in out["reason"]
    call_events = [e for e in events if e["tool"] == "call_server_tool"]
    assert len(call_events) == 1                     # 恰一条（初始拒绝；重试未执行）
    assert call_events[0]["ok"] is False
    adds = [e for e in events if e["tool"] == "manage_policy"]
    assert [e["ok"] for e in adds] == [False]        # 授予失败经 add 事件入账


def test_discover_off_grant_disclosure_positive_path(tmp_path):
    """discover 披露正路径 e2e（round-5 补齐——此前仅单元层覆盖；round-6
    纠正「仅竞态可达」误判）：store 预置残留 once（与 wrapper 路径的 once
    残留等价状态），grant=api_session 成功调用经残留 once 放行（焚毁）→
    成功信封 reason 带「会话档未落地」；再调用（无 grant）被拒——once
    已焚毁。"""
    from tests.fixtures.mcp_stub import StubMcpServer

    with StubMcpServer() as stub:
        catalog = tmp_path / "catalog.json"
        entry = {**CATALOG_ENTRY, "endpoint": stub.endpoint}
        catalog.write_text(json.dumps([entry], ensure_ascii=False), encoding="utf-8")
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(["server:@huaweicloud/ecs=allow", "*=deny"],
                                ensure_ascii=False), encoding="utf-8")
        store = PolicyStore(str(p))
        add = store.add_rule(
            "server:@huaweicloud/ecs:list_servers=allow", scope="once")
        assert add.ok is True and add.scope == "once"    # 残留 once（未消费）
        config = DiscoverConfig(catalog_path=str(catalog), policy_store=store)
        app = build_discover_app(config, elicit_mode=parse_elicit_mode("off"))
        seen: list = []

        async def _run():
            async with InMemoryTransport(app) as (r, w):
                async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                    await s.initialize()
                    conn = result_dict(await s.call_tool(
                        "connect_mcp_server", {"server": "@huaweicloud/ecs"}))
                    assert conn["ok"] is True
                    call = {"server": "@huaweicloud/ecs", "tool": "list_servers",
                            "arguments": {}}
                    via_once = result_dict(await s.call_tool(
                        "call_server_tool", {**call, "grant": "api_session"}))
                    third = result_dict(await s.call_tool(
                        "call_server_tool", call))
                    return via_once, third

        via_once, third = run(_run())
    assert via_once["ok"] is True                    # 经残留 once 放行（焚毁）
    assert "grant=api_session" in via_once["reason"] # 披露注落 wire
    assert "会话档未落地" in via_once["reason"]
    assert third["ok"] is False                      # once 已焚毁 → 再拒
    assert "safety policy 拒绝调用" in third["reason"]

