"""组合模式装配测试（--mode 逗号多值混用）：工具集并集/manage_policy 去重/
instructions 合并/共享 policy 行为/parse_modes。

openapi 拉数据→policy 拒绝→授予→放行 与 query_data 不受 policy 干扰 在同一
server 内闭环验证（真 mcp SDK client 内存回环；元数据网络 monkeypatch 封死）。
"""

import argparse
import asyncio

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from apie.memory_store import MemoryStore
from huaweicloud_open_mcp.cli import parse_modes
from huaweicloud_open_mcp.composite import build_composite_app, merge_instructions
from mcp_openapi.service import ServiceConfig, ToolService
from safety.policy_store import PolicyStore
from tests.test_elicit_mcp import _OPENAPI_DOC, _StubMockClient, result_dict

_OPENAPI_TOOLS = {"list_products", "get_product", "list_apis", "get_api",
                  "get_api_examples", "execute_api", "manage_policy"}
_DISCOVER_TOOLS = {"list_mcp_servers", "get_mcp_server", "connect_mcp_server",
                   "list_server_tools", "get_server_tool", "call_server_tool",
                   "disconnect_mcp_server", "manage_policy"}


def make_args(**overrides):
    ns = argparse.Namespace(mock=True, policy=None, region=None, mock_base=None,
                            mock_passthrough=None, gate=None, hints=None,
                            audit_file=None)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def make_openapi_service(policy_file=None):
    store = MemoryStore()
    doc = _OPENAPI_DOC
    path, method, api = "/v1/{project_id}/cloudservers/detail", "get", "ListServersDetails"
    store.set_api_cache(("ecs", api, "cn-north-4"),
                        (doc, path, method, doc["paths"][path][method]))
    return ToolService(store=store, config=ServiceConfig(
        mock=True,
        policy_store=PolicyStore(str(policy_file)) if policy_file else None,
        mock_client_factory=_StubMockClient))


def run(coro):
    return asyncio.run(coro)


def list_tool_names(app):
    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                return sorted(t.name for t in tools.tools)

    return run(_run())


# ---------- 工具集与去重 ----------

def test_openapi_data_toolset_with_dedup():
    app = build_composite_app(["openapi", "data"], make_args(),
                              openapi_service=make_openapi_service())
    names = list_tool_names(app)
    assert set(names) == _OPENAPI_TOOLS | {"query_data"}
    assert len(names) == 8  # manage_policy 去重：7 openapi + query_data
    assert names.count("manage_policy") == 1


def test_discover_data_toolset_with_dedup():
    app = build_composite_app(["discover", "data"], make_args())
    names = list_tool_names(app)
    assert set(names) == _DISCOVER_TOOLS | {"query_data"}
    assert len(names) == 9
    assert names.count("manage_policy") == 1


def test_all_three_modes_toolset():
    app = build_composite_app(["openapi", "discover", "data"], make_args())
    names = list_tool_names(app)
    assert set(names) == _OPENAPI_TOOLS | _DISCOVER_TOOLS | {"query_data"}
    assert len(names) == 15
    assert names.count("manage_policy") == 1


# ---------- instructions 合并 ----------

def test_merge_instructions_sections():
    text = merge_instructions(["openapi", "data"], None, None)
    assert "组合模式：openapi + data" in text
    assert "## 模式：openapi（OpenAPI 直连）" in text
    assert "## 模式：data（数据分析）" in text
    assert "query_data" in text


def test_composite_app_instructions_reach_client():
    app = build_composite_app(["discover", "data"], make_args())
    instructions = app.instructions or ""
    assert "## 模式：discover（MCP server 发现连接）" in instructions
    assert "## 模式：data（数据分析）" in instructions


# ---------- 共享 policy 行为（openapi,data 同 server 闭环） ----------

def test_openapi_data_policy_roundtrip_and_data_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text('["*=deny"]', encoding="utf-8")
    app = build_composite_app(["openapi", "data"], make_args(),
                              openapi_service=make_openapi_service(policy_file))

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                denied = result_dict(await s.call_tool("execute_api", {
                    "product": "ECS", "api": "ListServersDetails"}))
                # data 工具不受 policy 干扰（未配置允许集也照常执行）
                data_ok = result_dict(await s.call_tool("query_data", {
                    "sql": "SELECT 1 AS one"}))
                granted = result_dict(await s.call_tool("manage_policy", {
                    "action": "add", "line": "ECS:ListServersDetails=allow"}))
                allowed = result_dict(await s.call_tool("execute_api", {
                    "product": "ECS", "api": "ListServersDetails"}))
                return denied, data_ok, granted, allowed

    denied, data_ok, granted, allowed = run(_run())
    assert denied["ok"] is False and denied.get("reason")
    assert data_ok == {"ok": True, "columns": [{"name": "one", "type": "int64"}],
                       "rows": [{"one": 1}], "total_rows": 1, "returned_rows": 1,
                       "truncated": False, "tables": []}
    assert granted["ok"] is True
    assert allowed["ok"] is True


# ---------- parse_modes ----------

def test_parse_modes_single_and_default():
    assert parse_modes(None) == ["openapi"]
    assert parse_modes("openapi") == ["openapi"]
    assert parse_modes(None, env="discover") == ["discover"]
    assert parse_modes("DATA") == ["data"]


def test_parse_modes_combos_dedup_and_invalid():
    assert parse_modes("openapi,data") == ["openapi", "data"]
    assert parse_modes("data, openapi") == ["data", "openapi"]
    assert parse_modes("openapi,openapi") == ["openapi"]
    assert parse_modes("openapi,bogus") == ["openapi"]
    assert parse_modes("bogus") == ["openapi"]
