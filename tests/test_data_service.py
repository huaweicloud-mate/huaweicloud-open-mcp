"""S11：DataService 编排单测（audit 信封 / DataError→ToolError 翻译 / engine 注入）。

不依赖 datafusion 运行时（engine 以可调用注入；安装缺失路径 monkeypatch sys.modules）。
"""

import json
import sys

from common.audit import NdjsonAuditSink
from common.types import QueryDataResult, ToolError
from mcp_data import engine as data_engine
from mcp_data.engine import DataError
from mcp_data.service import DataConfig, DataService

_PAYLOAD = {"columns": [{"name": "a", "type": "int64"}], "rows": [{"a": 1}],
            "total_rows": 1, "returned_rows": 1, "truncated": False, "tables": ["t"]}


def _fake_engine(payload=None, error=None):
    def run_query(tables, sql, *, max_rows=None):
        if error is not None:
            raise error
        return dict(payload or _PAYLOAD)
    return run_query


def test_query_data_happy_envelope():
    svc = DataService(DataConfig(engine=_fake_engine()))
    out = svc.query_data("SELECT * FROM t", {"t": {"data": [{"a": 1}]}})
    assert out == {"ok": True, **_PAYLOAD}


def test_query_data_passes_max_rows_to_engine():
    seen = {}

    def run_query(tables, sql, *, max_rows=None):
        seen["tables"] = tables
        seen["sql"] = sql
        seen["max_rows"] = max_rows
        return dict(_PAYLOAD)

    DataService(DataConfig(engine=run_query)).query_data("SELECT 1", None, max_rows=50)
    assert seen == {"tables": None, "sql": "SELECT 1", "max_rows": 50}


def test_query_data_translates_dataerror():
    svc = DataService(DataConfig(engine=_fake_engine(error=DataError("bad sql"))))
    out = svc.query_data("BAD")
    assert out == {"ok": False, "reason": "bad sql"}


def test_query_data_default_engine_is_real_module_fn():
    svc = DataService()
    assert svc._engine() is data_engine.run_query


def test_query_data_uninstalled_datafusion_friendly_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "datafusion", None)  # import 即 ImportError
    svc = DataService()
    out = svc.query_data("SELECT 1", {"t": {"data": [{"a": 1}]}})
    assert out["ok"] is False
    assert "huaweicloud-open-mcp[datafusion]" in out["reason"]


def test_query_data_audit_records_event(tmp_path):
    path = tmp_path / "audit.jsonl"
    svc = DataService(DataConfig(engine=_fake_engine(), audit_sink=NdjsonAuditSink(path)))
    svc.query_data("SELECT * FROM t", {"t": {"data": [{"a": 1}]}}, max_rows=7)
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["tool"] == "query_data"
    assert event["ok"] is True
    assert event["input"] == {"sql": "SELECT * FROM t",
                              "tables": {"t": {"data": [{"a": 1}]}}, "max_rows": 7}


def test_query_data_audit_records_denial(tmp_path):
    path = tmp_path / "audit.jsonl"
    svc = DataService(DataConfig(engine=_fake_engine(error=DataError("bad")),
                                 audit_sink=NdjsonAuditSink(path)))
    svc.query_data("BAD")
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["tool"] == "query_data" and event["ok"] is False


def test_query_data_without_sink_skips_audit():
    svc = DataService(DataConfig(engine=_fake_engine()))
    assert svc.query_data("SELECT 1", None)["ok"] is True  # 未配置 sink 不抛


def test_result_envelope_shape_contract():
    assert set(QueryDataResult.__annotations__) >= {
        "ok", "columns", "rows", "total_rows", "returned_rows", "truncated", "tables"}
    assert ToolError.__annotations__["ok"] is not None
