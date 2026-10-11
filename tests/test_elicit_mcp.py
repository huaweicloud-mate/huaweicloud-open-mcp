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
    assert "confirm=true" in exec_res["reason"]
    assert "question" in exec_res["reason"]
    assert "policy 文件" in exec_res["reason"]
    assert "ECS:*=allow" in exec_res["reason"]
    assert mp_add["ok"] is False
    assert "confirm=true" in mp_add["reason"] and "policy 文件" in mp_add["reason"]
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
    assert "再次调用将重新提议" in out["reason"]
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
    assert "confirm=true" in second["reason"]      # 兜底指引提及快路径


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
    assert "confirm=true" in second["reason"]      # 兜底指引提及快路径
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
    """crafted 通配目标不构造 offer：confirm=true 与 declined elicitation 均无
    授予发生（最小规则载体不接受元字符；coarse_rule 是唯一合法通配载体）——
    一次性全局/产品级放行不可经 confirm 断言式获得。"""
    app, p = make_openapi(tmp_path, "off")
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    seen: list = []

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w, elicitation_callback=script_client([], seen)) as s:
                await s.initialize()
                off_confirm = result_dict(await s.call_tool(
                    "execute_api", {"product": "ECS", "api": "*", "confirm": True}))
                return off_confirm

    out = run(_run())
    assert out["ok"] is False
    assert "safety policy 拒绝执行" in out["reason"]
    assert seen == []                                  # 无 offer → 无问询
    assert not any(ln == "*:*=allow" or ln == "ECS:*=allow"
                   for ln in policy_lines(p))          # 无任何授予落盘
