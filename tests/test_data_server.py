"""S11：data 模式 server 装配 + 真 mcp SDK client 内存回环 e2e。

验证：单 data 模式工具集恰为 {query_data}（无 manage_policy）；inline 数据全链路；
错误信封；instructions 注入；audit 装配。
"""

import asyncio
import json

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from common.audit import NdjsonAuditSink
from mcp_data.server import build_data_app, build_data_config
from mcp_data.service import DataConfig


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


def run(coro):
    return asyncio.run(coro)


def make_app(**config_kwargs):
    return build_data_app(DataConfig(**config_kwargs))


# ---------- 装配 ----------

def test_data_app_registers_two_tools():
    app = make_app()

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                return sorted(t.name for t in tools.tools)

    assert run(_run()) == ["query_data", "transform_data"]


def test_data_app_instructions_mention_readonly_and_workflow():
    app = make_app()
    instructions = app.instructions or ""
    assert "query_data" in instructions
    assert "只读" in instructions
    assert "execute_api" in instructions  # 混装工作流指引


def test_build_data_config_audit_sink_from_env(tmp_path, monkeypatch):
    audit = tmp_path / "a.jsonl"
    monkeypatch.setenv("HUAWEICLOUD_MCP_AUDIT_FILE", str(audit))

    class Args:
        audit_file = None

    config = build_data_config(Args())
    assert isinstance(config.audit_sink, NdjsonAuditSink)


# ---------- 工具调用全链路 ----------

def test_data_app_query_inline_roundtrip():
    app = make_app()

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool("query_data", {
                    "sql": "SELECT g, COUNT(*) AS c FROM t GROUP BY g ORDER BY g",
                    "tables": {"t": {"data": [
                        {"g": "a", "v": 1}, {"g": "a", "v": 2}, {"g": "b", "v": 3}]},
                    },
                }))

    out = run(_run())
    assert out["ok"] is True
    assert out["columns"] == [{"name": "g", "type": "string"},
                              {"name": "c", "type": "int64"}]
    assert out["rows"] == [{"g": "a", "c": 2}, {"g": "b", "c": 1}]
    assert out["truncated"] is False


def test_data_app_query_error_envelope():
    app = make_app()

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool("query_data", {
                    "sql": "COPY (SELECT 1) TO '/tmp/x.csv'"}))

    out = run(_run())
    assert out["ok"] is False
    assert "只读" in out["reason"]


def test_data_app_audit_records_tool_call(tmp_path):
    audit = tmp_path / "audit.jsonl"
    app = make_app(audit_sink=NdjsonAuditSink(audit))

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool("query_data", {
                    "sql": "SELECT 1 AS one"}))

    run(_run())
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert event["tool"] == "query_data"
    assert event["input"]["sql"] == "SELECT 1 AS one"
    assert event["ok"] is True


def test_data_app_transform_roundtrip(tmp_path):
    """inline 数据 → transform_data 落盘 csv → 独立回读验证。"""
    app = make_app()
    out_path = tmp_path / "out.csv"

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool("transform_data", {
                    "sql": "SELECT g, COUNT(*) AS c FROM t GROUP BY g ORDER BY g",
                    "tables": {"t": {"data": [
                        {"g": "a", "v": 1}, {"g": "a", "v": 2}, {"g": "b", "v": 3}]}},
                    "out": {"path": str(out_path)},
                }))

    out = run(_run())
    assert out["ok"] is True
    assert out["path"] == str(out_path) and out["format"] == "csv"
    assert out["rows"] == 2
    assert out["preview"] == [{"g": "a", "c": 2}, {"g": "b", "c": 1}]
    assert out_path.read_text(encoding="utf-8").splitlines()[0] == "g,c"


def test_data_app_transform_refuses_overwrite(tmp_path):
    app = make_app()
    out_path = tmp_path / "out.csv"
    out_path.write_text("sentinel", encoding="utf-8")

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool("transform_data", {
                    "sql": "SELECT 1 AS a", "out": {"path": str(out_path)}}))

    out = run(_run())
    assert out["ok"] is False and "overwrite" in out["reason"]
    assert out_path.read_text(encoding="utf-8") == "sentinel"


def test_data_app_transform_audit_records_out(tmp_path):
    audit = tmp_path / "audit.jsonl"
    app = make_app(audit_sink=NdjsonAuditSink(audit))
    out_path = tmp_path / "o.jsonl"

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return result_dict(await s.call_tool("transform_data", {
                    "sql": "SELECT 1 AS a", "out": {"path": str(out_path)}}))

    run(_run())
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert event["tool"] == "transform_data" and event["ok"] is True
    assert event["input"]["out"] == {"path": str(out_path)}
