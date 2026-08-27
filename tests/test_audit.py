"""审计写入器（S3）：AuditSink 外接口 + service 审计挂钩。

独立真值：回读磁盘原始 NDJSON 内容 + 手写字面量断言。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from apie.memory_store import MemoryStore
from common.audit import NdjsonAuditSink, NullAuditSink, sink_from_path
from mcp_openapi.service import ServiceConfig, ToolService, build_audit_event
from safety import policy
from tests.test_service import FIXTURE_APIS_ECS, FIXTURE_GROUPS, FULL_DOC


class MemorySink:
    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def record(self, event):
        self.events.append(dict(event))


def _policy(*lines):
    return policy.parse_policy(list(lines))


def _prep_store(products=True, apis=True, detail=True):
    store = MemoryStore()
    if products:
        store.set_products(FIXTURE_GROUPS)
    if apis:
        store.set_apis("ECS", FIXTURE_APIS_ECS)
    if detail:
        store.set_api_cache(
            ("ecs", "ListServersDetails", "cn-north-4"),
            (FULL_DOC, "/v1/{project_id}/cloudservers/detail", "get",
             FULL_DOC["paths"]["/v1/{project_id}/cloudservers/detail"]["get"]),
        )
    return store


def _read_lines(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


# ---------- AuditSink 外接口 ----------

def test_ndjson_sink_one_line_per_record_with_ts(tmp_path):
    fp = tmp_path / "audit.jsonl"
    sink = NdjsonAuditSink(fp)
    sink.record({"tool": "list_products", "input": {"keyword": "云"}, "ok": True})
    sink.record({"tool": "execute_api",
                 "input": {"product": "ECS", "api": "ListServersDetails"}, "ok": False})
    lines = _read_lines(fp)
    assert len(lines) == 2
    assert lines[0]["tool"] == "list_products"
    assert lines[0]["input"] == {"keyword": "云"}
    assert lines[0]["ok"] is True
    assert lines[1]["tool"] == "execute_api"
    assert lines[1]["ok"] is False
    # ts 信封由 sink 注入，可解析的 ISO8601
    for line in lines:
        datetime.fromisoformat(line["ts"])
    assert lines[0]["ts"] <= lines[1]["ts"]


def test_ndjson_sink_appends_across_instances(tmp_path):
    fp = tmp_path / "audit.jsonl"
    NdjsonAuditSink(fp).record({"tool": "a", "input": {}, "ok": True})
    NdjsonAuditSink(fp).record({"tool": "b", "input": {}, "ok": True})
    lines = _read_lines(fp)
    assert [line["tool"] for line in lines] == ["a", "b"]


def test_ndjson_sink_creates_parent_dirs(tmp_path):
    fp = tmp_path / "nested" / "dir" / "audit.jsonl"
    NdjsonAuditSink(fp).record({"tool": "a", "input": {}, "ok": True})
    assert len(_read_lines(fp)) == 1


def test_ndjson_sink_record_never_raises(tmp_path):
    sink = NdjsonAuditSink(tmp_path)  # 路径是目录，open 必失败
    sink.record({"tool": "a", "input": {}, "ok": True})  # 不应抛出


def test_null_sink_noop(tmp_path):
    NullAuditSink().record({"tool": "a", "input": {}, "ok": True})
    assert list(tmp_path.iterdir()) == []


def test_sink_from_path(tmp_path):
    assert sink_from_path(None) is None
    assert sink_from_path("") is None
    sink = sink_from_path(str(tmp_path / "a.jsonl"))
    assert isinstance(sink, NdjsonAuditSink)


# ---------- 事件 payload schema ----------

def test_build_audit_event_shape():
    event = build_audit_event("execute_api",
                              {"product": "ECS", "api": "X", "params": {"limit": 1}},
                              {"ok": True, "body": {"big": "payload"}})
    assert event == {"tool": "execute_api",
                     "input": {"product": "ECS", "api": "X", "params": {"limit": 1}},
                     "ok": True}


def test_build_audit_event_ok_defaults_true_and_copies_input():
    args = {"product": "ECS"}
    event = build_audit_event("list_products", args, {"ok": True, "total": 1})
    assert event["ok"] is True
    args["product"] = "MUTATED"
    assert event["input"] == {"product": "ECS"}


def test_build_audit_event_non_mapping_result_ok():
    assert build_audit_event("x", {}, None)["ok"] is True
    assert build_audit_event("x", {}, object())["ok"] is True


# ---------- service 审计挂钩 ----------

def test_service_audits_metadata_tools():
    sink = MemorySink()
    service = ToolService(store=_prep_store(detail=False),
                          config=ServiceConfig(audit_sink=sink))
    service.list_products(keyword="云")
    service.get_product("ECS")
    service.list_apis("ECS", tag="生命周期管理", limit=5, offset=1)
    service.get_api("ECS", "ListServersDetails")
    service.get_api_examples("ECS", "ListServersDetails")
    assert [(e["tool"], e["ok"]) for e in sink.events] == [
        ("list_products", True), ("get_product", True), ("list_apis", True),
        ("get_api", True), ("get_api_examples", True),
    ]
    assert sink.events[0]["input"] == {"keyword": "云"}
    # input 只含显式传入的参数（不含默认值），对齐 agent 侧 trace 口径
    assert sink.events[2]["input"] == {"product": "ECS", "tag": "生命周期管理",
                                       "limit": 5, "offset": 1}
    assert sink.events[3]["input"] == {"product": "ECS", "api": "ListServersDetails"}


def test_service_audits_execute_allowed():
    class StubMockClient:
        def mock_request(self, product, api_name, region, status_code=200, number=1):
            return {"status": 200, "headers": {}, "body": {"mock": True}}

    sink = MemorySink()
    service = ToolService(store=_prep_store(products=False, apis=False),
                          config=ServiceConfig(
                              mock=True, audit_sink=sink,
                              policy_rules=_policy("ECS:*=allow"),
                              mock_client_factory=lambda: StubMockClient()))
    out = service.execute_api("ECS", "ListServersDetails", params={"limit": 1})
    assert out["ok"] is True
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["tool"] == "execute_api"
    assert event["ok"] is True
    assert event["input"] == {"product": "ECS", "api": "ListServersDetails",
                              "params": {"limit": 1}}


def test_service_audits_execute_denied():
    sink = MemorySink()
    service = ToolService(store=_prep_store(products=False, apis=False),
                          config=ServiceConfig(mock=True, audit_sink=sink))
    out = service.execute_api("ECS", "ListServersDetails")
    assert out["ok"] is False
    assert sink.events == [{"tool": "execute_api",
                            "input": {"product": "ECS", "api": "ListServersDetails"},
                            "ok": False}]


def test_service_audits_manage_policy_without_store():
    sink = MemorySink()
    service = ToolService(config=ServiceConfig(audit_sink=sink))
    out = service.manage_policy("add", line="ECS:*=allow")
    assert out["ok"] is False
    assert sink.events == [{"tool": "manage_policy",
                            "input": {"action": "add", "line": "ECS:*=allow"},
                            "ok": False}]


def test_service_audits_exception_path():
    class BoomStore(MemoryStore):
        def products(self):
            raise RuntimeError("boom")

    sink = MemorySink()
    service = ToolService(store=BoomStore(), config=ServiceConfig(audit_sink=sink))
    try:
        service.get_product("ECS")
        raise AssertionError("应抛出")
    except RuntimeError:
        pass
    assert sink.events == [{"tool": "get_product",
                            "input": {"product": "ECS"}, "ok": False}]


def test_service_without_sink_unchanged():
    store = _prep_store(detail=False)
    out = ToolService(store=store).list_products()
    assert out["ok"] is True
