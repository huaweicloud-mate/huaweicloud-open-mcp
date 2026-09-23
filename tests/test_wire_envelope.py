"""S-W：MCP wire 信封形状不变量（2026-09 起）。

所有工具的 structuredContent 必须是**扁平信封**（ok 必填 + 业务字段可选），
不得被 MCP SDK 依据 union 返回注解包成 {"result": ...}（union 的副作用会使
wire 与恒扁平的 content[0].text 分叉，agent 消费者翻车）。

本文件是 wire 形状的唯一守卫：未来任何重新引入 `X | ToolError` 的注册、
或新增工具忘记使用 *Wire 类型，都会在此打红。
"""

import asyncio
import json

import jsonschema
import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from mcp_data.server import build_data_app
from mcp_data.service import DataConfig
from mcp_discover.config import DiscoverConfig
from mcp_discover.server import build_discover_app
from mcp_openapi.server import build_app

pytestmark = pytest.mark.usefixtures("sealed_configs")


def _apps():
    return {
        "openapi": build_app(),
        "discover": build_discover_app(DiscoverConfig()),
        "data": build_data_app(DataConfig()),
    }


def test_all_tools_flat_wire_schema():
    """18 处注册的 outputSchema 恒为扁平信封：含 ok、非 {"result": ...} 包裹形。"""
    seen = 0
    for mode, app in _apps().items():
        for name, tool in app._tool_manager._tools.items():
            schema = tool.output_schema
            assert schema is not None, f"{mode}:{name} 无 outputSchema"
            props = schema.get("properties") or {}
            assert "ok" in props, f"{mode}:{name} 顶层缺 ok：{sorted(props)}"
            # 排除 SDK 的 {"result": ...} 包裹形（call_server_tool 的业务 result 字段
            # 与 ok 并存，故按「键集恰为 {result}」判定，而非「含 result」）。
            assert set(props) != {"result"}, f"{mode}:{name} 被包成 {{result}}"
            seen += 1
            # 失败臂 {ok: false, reason} 必须通过 schema 校验——否则 SDK 报
            # isError=True 且把 pydantic 校验错误文本外泄给客户端。成功型工具
            # （ok: const true，如 disconnect_mcp_server）无失败臂，跳过。
            if props["ok"].get("const") is True:
                continue
            jsonschema.validate({"ok": False, "reason": "policy denied"}, schema)
    assert seen == 18, f"预期 18 处注册，实际 {seen}"


def test_failure_arm_flat_end_to_end():
    """data 模式失败臂经真 SDK 回环：structuredContent 扁平、isError 保持 False、text 同形。"""
    app = build_data_app(DataConfig())

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool("query_data", {
                    "sql": "COPY (SELECT 1) TO '/tmp/x.csv'"})

    res = asyncio.run(_run())
    sc = res.structured_content
    assert isinstance(sc, dict)
    assert sc.get("ok") is False
    assert "result" not in sc, f"失败臂被包成 {{result}}：{sorted(sc)}"
    assert res.is_error is False
    # text 为原始扁平信封（无 null-fill）；structuredContent 多出的键必为 null。
    text = json.loads(res.content[0].text)
    assert set(text) <= set(sc)
    assert all(v is None for k, v in sc.items() if k not in text)
