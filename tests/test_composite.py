"""组合模式装配测试（--mode 逗号多值混用）：工具集并集/manage_policy 去重/
instructions 合并/共享 policy 行为/parse_modes。

openapi 拉数据→policy 拒绝→授予→放行 与 query_data 不受 policy 干扰 在同一
server 内闭环验证（真 mcp SDK client 内存回环；元数据网络 monkeypatch 封死）。
"""

import argparse
import asyncio

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from apie.api_location import ApiLocation
from apie.memory_store import MemoryStore
from huaweicloud_open_mcp.cli import parse_modes
from huaweicloud_open_mcp.deployment import build_app, merge_instructions
from mcp_openapi.service import ServiceConfig, ToolService
from safety.policy_store import PolicyStore
from tests.test_elicit_mcp import _OPENAPI_DOC, _StubMockClient, result_dict

_OPENAPI_TOOLS = {"search_apis", "list_products", "get_product", "list_apis",
                  "get_api", "get_api_examples", "execute_api", "manage_policy"}

# 文件级密封（conftest 共享 fixture）：共享 store/sink 装配不受宿主 configs 泄入影响
pytestmark = pytest.mark.usefixtures("sealed_configs")


def make_args(**overrides):
    ns = argparse.Namespace(mock=True, policy=None, region=None, mock_base=None,
                            mock_passthrough=None, hints=None,
                            audit_file=None)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def make_openapi_service(policy_file=None):
    store = MemoryStore()
    doc = _OPENAPI_DOC
    path, method, api = "/v1/{project_id}/cloudservers/detail", "get", "ListServersDetails"
    store.set_api_cache(("ecs", api, "cn-north-4"),
                        ApiLocation(doc, path, method, doc["paths"][path][method]))
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
    app = build_app(["openapi", "data"], make_args(),
                              openapi_service=make_openapi_service())
    names = list_tool_names(app)
    assert set(names) == _OPENAPI_TOOLS | {"query_data", "transform_data"}
    assert len(names) == 10  # manage_policy 去重：8 openapi + 2 data 工具
    assert names.count("manage_policy") == 1


# ---------- instructions 合并 ----------

def test_merge_instructions_sections():
    text = merge_instructions(["openapi", "data"], None)
    assert "组合模式：openapi + data" in text
    assert "## 模式：openapi（OpenAPI 直连）" in text
    assert "## 模式：data（数据分析）" in text
    assert "query_data" in text


def test_composite_app_instructions_reach_client():
    app = build_app(["openapi", "data"], make_args())
    instructions = app.instructions or ""
    assert "## 模式：openapi（OpenAPI 直连）" in instructions
    assert "## 模式：data（数据分析）" in instructions


# （deployment.build_app 取代 composite.build_composite_app；共享 policy 行为语义不变）
# ---------- 共享 policy 行为（openapi,data 同 server 闭环） ----------

def test_openapi_data_policy_roundtrip_and_data_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text('["*=deny"]', encoding="utf-8")
    app = build_app(["openapi", "data"], make_args(),
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
                confirmed = result_dict(await s.call_tool("execute_api", {
                    "product": "ECS", "api": "ListServersDetails",
                    "confirm": True}))
                mp_add = result_dict(await s.call_tool("manage_policy", {
                    "action": "add", "line": "ECS:ListServersDetails=allow"}))
                return denied, data_ok, confirmed, mp_add

    denied, data_ok, confirmed, mp_add = run(_run())
    assert denied["ok"] is False and denied.get("reason")
    # wire 扁平信封：SDK 对可选字段 null-fill（reason 在成功臂为 None），同 ExecuteResult 先例
    assert data_ok == {"ok": True, "reason": None,
                       "columns": [{"name": "one", "type": "int64"}],
                       "rows": [{"one": 1}], "total_rows": 1, "returned_rows": 1,
                       "truncated": False, "tables": []}
    # off 默认档 confirm 快路径：同次调用授予 once 并执行（混装装配全链）
    assert confirmed["ok"] is True
    # 工具面 list-only：写路径拒绝（data 工具不受 policy 干扰的口径不变）
    assert mp_add["ok"] is False and "list" in mp_add["reason"]


# ---------- parse_modes ----------

def test_parse_modes_single_and_default():
    assert parse_modes(None) == ["openapi"]
    assert parse_modes("openapi") == ["openapi"]
    assert parse_modes("DATA") == ["data"]


def test_parse_modes_combos_dedup_and_invalid():
    assert parse_modes("openapi,data") == ["openapi", "data"]
    assert parse_modes("data, openapi") == ["data", "openapi"]
    assert parse_modes("openapi,openapi") == ["openapi"]
    assert parse_modes("openapi,bogus") == ["openapi"]
    assert parse_modes("bogus") == ["openapi"]


def test_parse_modes_garbage_flag_yields_to_valid_env(caplog):
    """垃圾 flag（有 token 零有效）让位给有效 env——显式 data（本地无凭证）
    部署不因 typo flag 静默落回带 AK/SK 的 openapi；让位告警（显式 --mode
    被忽略须可见）。"""
    import logging
    with caplog.at_level(logging.WARNING):
        assert parse_modes("junk", env="data") == ["data"]
    assert "junk" in caplog.text
    assert parse_modes("bogus1,bogus2", env="openapi,data") == ["openapi", "data"]
    # 显式有效 flag 仍赢（env 仅扫已移除名，不参与选择）
    assert parse_modes("openapi", env="dat") == ["openapi"]
    # 两源皆无有效：回退 openapi（历史单值语义）
    assert parse_modes("junk", env="dat") == ["openapi"]
    # 空白 flag 在 token 层已让位（回归）
    assert parse_modes("  ", env="data") == ["data"]


def test_parse_modes_removed_discover_fails_fast():
    """已移除的模式名启动即失败——静默降级 openapi 会让固定 discover 的部署
    以 AK/SK 签名意外打云（对齐 server: 规则行 fail-fast 的口径）。"""
    with pytest.raises(ValueError, match="discover"):
        parse_modes("discover")
    with pytest.raises(ValueError, match="discover"):
        parse_modes(None, env="openapi,DISCOVER")
    # 空白/纯逗号垃圾 flag 不遮蔽环境变量（优先级在 token 层）
    with pytest.raises(ValueError, match="discover"):
        parse_modes("  ", env="discover")
    with pytest.raises(ValueError, match="discover"):
        parse_modes(",,", env="discover")
    # 垃圾 token flag 也不遮蔽——两个源都扫已移除名（stale env 是地雷）
    with pytest.raises(ValueError, match="discover"):
        parse_modes("junk", env="discover")


def test_normalize_modes_matrix():
    """共享归一单点的直接矩阵（cli 与 build_app 两入口的字符级归一契约）：
    str 包裹、strip+lower、去空、去重保序。"""
    from common.deployment import normalize_modes
    assert normalize_modes("OpenAPI") == ["openapi"]
    assert normalize_modes([" OPENAPI ", "data", "openapi"]) == ["openapi", "data"]
    assert normalize_modes(["", "  ", "DATA"]) == ["data"]
    assert normalize_modes([]) == []


def test_reject_removed_modes_bare_string():
    """裸字符串守卫直接矩阵：str 视为单模式（逐字符迭代会静默绕过）。"""
    from common.deployment import reject_removed_modes
    with pytest.raises(ValueError, match="discover"):
        reject_removed_modes("discover")
    with pytest.raises(ValueError, match="discover"):
        reject_removed_modes(" DISCOVER ")
    reject_removed_modes("openapi")          # 合法单模式串放行
    reject_removed_modes(["openapi", "data"])


def test_build_app_removed_mode_fails_fast():
    """build_app（嵌入方唯一 external seam）对已移除模式名 fail-fast——
    混装列表不得静默降级为 openapi（单 server 带已装配凭证）。"""
    with pytest.raises(ValueError, match="discover"):
        build_app(["discover", "openapi"], make_args())
    with pytest.raises(ValueError, match="discover"):
        build_app(["DISCOVER"], make_args())
    # 裸字符串视为单模式（逐字符迭代会静默绕过守卫组装零工具 server）
    with pytest.raises(ValueError, match="discover"):
        build_app("discover", make_args())


def test_build_app_normalizes_and_rejects_unknown_modes():
    """入口归一与未知 token fail-fast——大小写/空白/逗号粘连变体要么正确
    生效要么响，不得静默丢段（说谎的 instructions 头）或拼出零工具 server。"""
    # 大小写/空白归一后正确装配（与 cli.parse_modes 真同口径）
    app = build_app(["OPENAPI", " DATA "], make_args())
    assert len(app._tool_manager._tools) == 10   # 混装并集（8 openapi + 2 data）
    single = build_app(["DATA"], make_args())
    assert sorted(single._tool_manager._tools) == ["query_data", "transform_data"]
    # 逗号粘连的已移除拼写（strip 不去逗号）经未知 token 路径 fail-fast
    with pytest.raises(ValueError, match="未知模式"):
        build_app(["discover,", "openapi"], make_args())
    # 未知 token 是编程错误：响，不静默忽略
    with pytest.raises(ValueError, match="未知模式.*bogus"):
        build_app(["bogus", "data"], make_args())
    # 空列表/空串显式拒绝（不落 IndexError、不组装空 server）
    with pytest.raises(ValueError, match="modes 为空"):
        build_app([], make_args())
    with pytest.raises(ValueError, match="modes 为空"):
        build_app("  ", make_args())
