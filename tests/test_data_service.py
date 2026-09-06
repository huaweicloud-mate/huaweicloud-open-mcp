"""S11：DataService 编排单测（audit 信封 / DataError→ToolError 翻译 / engine 注入）。

不依赖 datafusion 运行时（engine 以模块形替身注入；安装缺失路径 monkeypatch sys.modules）。
"""

import json
import sys
from types import SimpleNamespace

from common.audit import NdjsonAuditSink
from common.types import QueryDataResult, ToolError
from mcp_data import engine as data_engine
from mcp_data.engine import DataError
from mcp_data.service import DataConfig, DataService

_PAYLOAD = {"columns": [{"name": "a", "type": "int64"}], "rows": [{"a": 1}],
            "total_rows": 1, "returned_rows": 1, "truncated": False, "tables": ["t"]}
_TRANSFORM_PAYLOAD = {"path": "/tmp/o.csv", "format": "csv", "rows": 2, "bytes": 12,
                      "columns": [{"name": "a", "type": "int64"}],
                      "preview": [{"a": 1}, {"a": 2}]}


def _fake_engine(query_payload=None, transform_payload=None, query_error=None,
                 transform_error=None):
    def run_query(tables, sql, *, max_rows=None):
        if query_error is not None:
            raise query_error
        return dict(query_payload or _PAYLOAD)

    def run_transform(tables, sql, out, *, overwrite=False, preview_rows=None):
        if transform_error is not None:
            raise transform_error
        return dict(transform_payload or _TRANSFORM_PAYLOAD)

    return SimpleNamespace(run_query=run_query, run_transform=run_transform)


# ---------- query_data（读 lane） ----------

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

    DataService(DataConfig(engine=SimpleNamespace(run_query=run_query))).query_data(
        "SELECT 1", None, max_rows=50)
    assert seen == {"tables": None, "sql": "SELECT 1", "max_rows": 50}


def test_query_data_translates_dataerror():
    svc = DataService(DataConfig(engine=_fake_engine(query_error=DataError("bad sql"))))
    out = svc.query_data("BAD")
    assert out == {"ok": False, "reason": "bad sql"}


def test_query_data_default_engine_is_real_module():
    svc = DataService()
    assert svc._engine() is data_engine


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
    svc = DataService(DataConfig(engine=_fake_engine(query_error=DataError("bad")),
                                 audit_sink=NdjsonAuditSink(path)))
    svc.query_data("BAD")
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["tool"] == "query_data" and event["ok"] is False


def test_query_data_without_sink_skips_audit():
    svc = DataService(DataConfig(engine=_fake_engine()))
    assert svc.query_data("SELECT 1", None)["ok"] is True  # 未配置 sink 不抛


# ---------- transform_data（写 lane） ----------

def test_transform_data_happy_envelope():
    svc = DataService(DataConfig(engine=_fake_engine()))
    out = svc.transform_data("SELECT * FROM t", {"t": {"data": [{"a": 1}]}},
                             {"path": "/tmp/o.csv"})
    assert out == {"ok": True, **_TRANSFORM_PAYLOAD}


def test_transform_data_passes_args_to_engine():
    seen = {}

    def run_transform(tables, sql, out, *, overwrite=False, preview_rows=None):
        seen.update(tables=tables, sql=sql, out=out, overwrite=overwrite,
                    preview_rows=preview_rows)
        return dict(_TRANSFORM_PAYLOAD)

    DataService(DataConfig(engine=SimpleNamespace(run_transform=run_transform))).transform_data(
        "SELECT 1", {"t": {"data": []}}, {"path": "o.csv", "format": "csv"},
        overwrite=True, preview_rows=3)
    assert seen == {"tables": {"t": {"data": []}}, "sql": "SELECT 1",
                    "out": {"path": "o.csv", "format": "csv"},
                    "overwrite": True, "preview_rows": 3}


def test_transform_data_translates_dataerror():
    svc = DataService(DataConfig(engine=_fake_engine(transform_error=DataError("已存在"))))
    out = svc.transform_data("SELECT 1", None, {"path": "o.csv"})
    assert out == {"ok": False, "reason": "已存在"}


def test_transform_data_audit_records_out_snapshot(tmp_path):
    """写路径审计可追溯：input 快照含 out（path/format）与 overwrite。"""
    path = tmp_path / "audit.jsonl"
    svc = DataService(DataConfig(engine=_fake_engine(), audit_sink=NdjsonAuditSink(path)))
    svc.transform_data("SELECT * FROM t", {"t": {"data": [{"a": 1}]}},
                       {"path": "out.parquet"}, overwrite=True)
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["tool"] == "transform_data" and event["ok"] is True
    assert event["input"] == {"sql": "SELECT * FROM t",
                              "tables": {"t": {"data": [{"a": 1}]}},
                              "out": {"path": "out.parquet"}, "overwrite": True}


def test_result_envelope_shape_contract():
    assert set(QueryDataResult.__annotations__) >= {
        "ok", "columns", "rows", "total_rows", "returned_rows", "truncated", "tables"}
    assert ToolError.__annotations__["ok"] is not None
