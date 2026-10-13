"""审计写入器（S3）：AuditSink 外接口 + service 审计挂钩。

独立真值：回读磁盘原始 NDJSON 内容 + 手写字面量断言。
"""

import asyncio
import inspect
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from apie.api_location import ApiLocation
from apie.memory_store import MemoryStore
from common.audit import NdjsonAuditSink, NullAuditSink, audited, build_audit_event, sink_from_path
from mcp_openapi.service import ServiceConfig, ToolService
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
            ApiLocation(FULL_DOC, "/v1/{project_id}/cloudservers/detail", "get",
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


def test_service_audits_manage_policy_batch_line_snapshot():
    """批量 line 数组原样进 input 快照；payload 契约 {tool, input, ok} 不变。"""
    sink = MemorySink()
    service = ToolService(config=ServiceConfig(audit_sink=sink))
    out = service.manage_policy("add", line=["ECS:*=allow", "OBS:GetObject=allow"])
    assert out["ok"] is False       # 未配置 policy store → 拒绝
    assert sink.events == [{"tool": "manage_policy",
                            "input": {"action": "add",
                                      "line": ["ECS:*=allow", "OBS:GetObject=allow"]},
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


# ---------- 会话归因（S20：ambient session 键，stdio 恒无字段） ----------

def test_audit_session_none_omits_field():
    """stdio / 装配期：键恒 None，事件无 session 字段（NDJSON 逐字节回归红线）。"""
    sink = MemorySink()
    service = ToolService(store=_prep_store(detail=False),
                          config=ServiceConfig(audit_sink=sink))
    service.list_products()
    assert sink.events[0] == {"tool": "list_products", "input": {}, "ok": True}


def test_audit_session_attributed_when_bound():
    from common.sessions import _bind_session

    sink = MemorySink()
    service = ToolService(store=_prep_store(detail=False),
                          config=ServiceConfig(audit_sink=sink))
    with _bind_session("sess-A"):
        service.list_products()
    event = sink.events[0]
    assert event["session"] == "sess-A"
    # provenance 在事件顶层，不进 input 快照（已发布契约纯净性）
    assert "session" not in event["input"]
    assert event["input"] == {}


def test_audit_exception_path_attributed():
    from common.sessions import _bind_session

    class BoomStore(MemoryStore):
        def products(self):
            raise RuntimeError("boom")

    sink = MemorySink()
    service = ToolService(store=BoomStore(), config=ServiceConfig(audit_sink=sink))
    with pytest.raises(RuntimeError), _bind_session("sess-B"):
        service.get_product("ECS")
    event = sink.events[0]
    assert event["ok"] is False
    assert event["session"] == "sess-B"


def test_build_audit_event_contract_shape_frozen():
    """build_audit_event（verifier 已发布契约函数）不接受 session 参数——
    归因是写侧 provenance，payload schema 形状冻结。"""
    import inspect

    params = list(inspect.signature(build_audit_event).parameters)
    assert params == ["tool", "input_args", "result"]


def test_audited_ki_se_anchor_logged(caplog):
    """KI/SE 锚点钉住（round-10——第九轮 headline 此前零覆盖且格式残缺）：
    sink 抛 KI/SE → 重抛前恰一条 ERROR 锚点记录，消息含锚点短语与异常
    身份（绑定异常插值——修复前打出字面 %s），exc_info 附栈；删除锚点
    调用则此测试炸（B 验证过删锚点 1573 全绿的回归洞）。"""
    import asyncio
    import logging as stdlib_logging

    from common.audit import audited

    class _KeyboardSink:
        def record(self, event) -> None:
            raise KeyboardInterrupt("sink-ki")

    class _ExitSink:
        def record(self, event) -> None:
            raise SystemExit(3)

    class _Svc:
        def __init__(self, config):
            self.config = config

        @audited
        async def ok_tool(self, x: int) -> dict:
            return {"ok": True, "x": x}

    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        host = type("Host", (), {})()
        host.config = type("C", (), {"audit_sink": _KeyboardSink()})()
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(_Svc(host.config).ok_tool(1))
        anchors = [r for r in caplog.records
                   if "aborted by shutdown-signal" in r.getMessage()]
        assert len(anchors) == 1, "sink-KI 须恰一条锚点 ERROR"
        assert anchors[0].levelno == stdlib_logging.ERROR
        assert "sink-ki" in anchors[0].getMessage()       # 异常身份（修字面 %s）
        assert anchors[0].exc_info is not None            # 栈（ERROR+栈的栈半）

    caplog.clear()
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        host = type("Host", (), {})()
        host.config = type("C", (), {"audit_sink": _ExitSink()})()
        with pytest.raises(SystemExit):
            asyncio.run(_Svc(host.config).ok_tool(1))
        anchors = [r for r in caplog.records
                   if "aborted by shutdown-signal" in r.getMessage()]
        assert len(anchors) == 1
        # exit code 插值（KI/SE 可区分；endswith 钉位置——round-11 收紧）
        assert anchors[0].getMessage().endswith("(event lost): 3")
        assert anchors[0].exc_info is not None            # 栈半（round-11 补）


def test_audited_awaitable_exception_and_cancel():
    """audited awaitable 化（round-5）：async 方法完成/异常/取消三路径均入账。
    CancelledError（BaseException 子类，不被 except Exception 捕获）同样
    记 ok=False 后重抛——service 层取消场景的审计逃逸关闭。round-6/7 补：
    introspection 红线（iscoroutinefunction 装饰后保持 True；同步方法
    保持 False——装饰期分流钉住）、_audit_write 本体 best-effort（sink
    违约不替换原异常**且不摧毁成功结果**——async 与 sync 两分支）、
    空白 name 装饰期拒绝。round-8 补：BaseException 级违约钉住（
    RuntimeError 回退 except Exception 全绿——headline 行为的回归红线）
    与 KI/SE 重抛（解释器停机信号非 sink 违约，不吞没）。"""

    events: list[dict] = []

    class _Sink:
        def record(self, event) -> None:
            events.append(dict(event))

    class _SinkContractError(BaseException):
        """非 Exception 的 BaseException 子类——钉住 BaseException 级捕获
        （不选 KeyboardInterrupt/SystemExit：round-8 起这两类被重抛）。"""

    class _RaisingSink:
        def record(self, event) -> None:
            raise RuntimeError("sink contract violation")

    class _BaseRaisingSink:
        def record(self, event) -> None:
            raise _SinkContractError("base-level violation")

    class _KeyboardSink:
        def record(self, event) -> None:
            raise KeyboardInterrupt()

    class _ExitSink:
        def record(self, event) -> None:
            raise SystemExit(3)

    host = type("Host", (), {})()
    host.config = type("C", (), {"audit_sink": _Sink()})()

    class _Svc:
        def __init__(self, config):
            self.config = config

        @audited
        async def ok_tool(self, x: int) -> dict:
            return {"ok": True, "x": x}

        @audited
        async def fail_tool(self) -> dict:
            raise RuntimeError("boom")

        @audited
        async def cancel_tool(self) -> dict:
            raise asyncio.CancelledError()

        @audited
        def sync_tool(self, y: int) -> dict:
            return {"ok": True, "y": y}

        @audited
        def sync_fail_tool(self) -> dict:
            raise RuntimeError("sync boom")

    svc = _Svc(host.config)

    # introspection 红线：装饰期分流——async 保持协程函数、sync 保持非协程
    assert inspect.iscoroutinefunction(_Svc.ok_tool) is True
    assert inspect.iscoroutinefunction(_Svc.fail_tool) is True
    assert inspect.iscoroutinefunction(_Svc.cancel_tool) is True
    assert inspect.iscoroutinefunction(_Svc.sync_tool) is False

    out = asyncio.run(svc.ok_tool(3))
    assert out == {"ok": True, "x": 3}
    assert events[0]["tool"] == "ok_tool"
    assert events[0]["ok"] is True and events[0]["input"] == {"x": 3}

    with pytest.raises(RuntimeError):
        asyncio.run(svc.fail_tool())
    assert events[1]["tool"] == "fail_tool" and events[1]["ok"] is False

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.cancel_tool())
    assert events[2]["tool"] == "cancel_tool" and events[2]["ok"] is False

    # 同步分支：正常路径入账
    sync_out = svc.sync_tool(7)
    assert sync_out == {"ok": True, "y": 7}
    assert events[3]["tool"] == "sync_tool" and events[3]["input"] == {"y": 7}

    # 本体 best-effort：sink 违约（Exception 级）——异常路径原异常照常传播
    bad_host = type("Host", (), {})()
    bad_host.config = type("C", (), {"audit_sink": _RaisingSink()})()
    bad_svc = _Svc(bad_host.config)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(bad_svc.fail_tool())
    with pytest.raises(RuntimeError, match="sync boom"):
        bad_svc.sync_fail_tool()

    # 本体 best-effort：成功路径结果不被摧毁（副作用已发生，审计失败
    # 不可把成功操作变成工具错误——round-7 三方实证修复）
    assert asyncio.run(bad_svc.ok_tool(1)) == {"ok": True, "x": 1}
    assert bad_svc.sync_tool(2) == {"ok": True, "y": 2}

    # BaseException 级违约钉住（round-8）：except Exception 回退时此组全炸
    base_host = type("Host", (), {})()
    base_host.config = type("C", (), {"audit_sink": _BaseRaisingSink()})()
    base_svc = _Svc(base_host.config)
    assert asyncio.run(base_svc.ok_tool(9)) == {"ok": True, "x": 9}   # 成功结果保留
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(base_svc.fail_tool())                             # 原异常不被替换

    # KI/SE 重抛（round-8）：解释器停机信号不被吞没——SIGINT 不转成成功
    ki_host = type("Host", (), {})()
    ki_host.config = type("C", (), {"audit_sink": _KeyboardSink()})()
    ki_svc = _Svc(ki_host.config)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(ki_svc.ok_tool(1))
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(ki_svc.fail_tool())          # async-exception 格（round-9 补）
    with pytest.raises(KeyboardInterrupt):
        ki_svc.sync_fail_tool()                                       # 异常路径同样重抛

    exit_host = type("Host", (), {})()
    exit_host.config = type("C", (), {"audit_sink": _ExitSink()})()
    exit_svc = _Svc(exit_host.config)
    with pytest.raises(SystemExit):
        asyncio.run(exit_svc.ok_tool(1))
    with pytest.raises(SystemExit):
        asyncio.run(exit_svc.fail_tool())        # async-exception 格（round-9 补）

    # host config 契约违约 fail-fast（round-8：getattr 在 try 外——
    # 与 sink 违约分型；宿主缺 config 属性时首次调用即 AttributeError，
    # config 存在但缺 audit_sink 键则 getattr default 静默跳过=零开销语义）
    class _NoConfigSvc:
        @audited
        async def ok_tool(self, x: int) -> dict:
            return {"ok": True, "x": x}

        @audited
        async def fail_tool(self) -> dict:
            raise RuntimeError("no-config boom")

    with pytest.raises(AttributeError):
        asyncio.run(_NoConfigSvc().ok_tool(1))
    with pytest.raises(AttributeError):          # 异常路径同样 fail-fast（round-9 补）
        asyncio.run(_NoConfigSvc().fail_tool())


def test_audited_guarded_log_and_sink_error_levels(caplog, monkeypatch, tmp_path):
    """round-9/10 钉住：① _guarded_log 守卫——logger.error 抛 Exception
    级错误被吞（成功/异常双路径的原结果/原异常不被劫持，删除守卫即回归
    round-7 洞）；② logger.error 抛 KI/SE 传播（守卫收窄 Exception——两
    窗口 KI/SE 传播策略一致）；③ sink 违约日志为 ERROR 级 + exc_info
    （caplog）；④ NdjsonAuditSink 生产失败（unwritable path）内部 ERROR
    级 + exc_info（生产通道 round-9 补齐、round-10 收编 _guarded_log）。"""
    import asyncio
    import logging as stdlib_logging

    class _RaisingSink:
        def record(self, event) -> None:
            raise RuntimeError("sink contract violation")

    host = type("Host", (), {})()
    host.config = type("C", (), {"audit_sink": _RaisingSink()})()

    class _Svc:
        def __init__(self, config):
            self.config = config

        @audited
        async def ok_tool(self, x: int) -> dict:
            return {"ok": True, "x": x}

        @audited
        async def fail_tool(self) -> dict:
            raise RuntimeError("boom")

    # ③ sink 违约 → ERROR 级（caplog 捕获）
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        svc = _Svc(host.config)
        assert asyncio.run(svc.ok_tool(1)) == {"ok": True, "x": 1}
    error_records = [r for r in caplog.records
                     if r.levelno == stdlib_logging.ERROR
                     and "audit write failed" in r.getMessage()]
    assert error_records, "sink 违约须记 ERROR（round-8 升格的回归钉）"
    assert all(r.exc_info is not None for r in error_records)   # 栈半（round-10 补）

    class _LoggerContractError(Exception):
        """logger 守卫钉住用 Exception 子类（守卫吞没集合）。"""

    # ① 守卫：logger.error 抛 Exception 级错误被吞——成功结果保留
    #    （守卫收窄 Exception 后 BaseException 级走 ② 的传播语义）
    caplog.clear()

    def _raising_error(*args, **kwargs):
        raise _LoggerContractError("logger contract violation")

    monkeypatch.setattr("common.audit.logger.error", _raising_error)
    guard_host = type("Host", (), {})()
    guard_host.config = type("C", (), {"audit_sink": _RaisingSink()})()
    guard_svc = _Svc(guard_host.config)
    assert asyncio.run(guard_svc.ok_tool(2)) == {"ok": True, "x": 2}  # 守卫吞掉
    with pytest.raises(RuntimeError, match="boom"):                   # 原异常不被劫持
        asyncio.run(guard_svc.fail_tool())

    # ② 守卫收窄 Exception：logger.error 抛 KI 传播（两窗口策略一致）
    def _ki_error(*args, **kwargs):
        raise KeyboardInterrupt("logger-ki")

    monkeypatch.setattr("common.audit.logger.error", _ki_error)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(guard_svc.ok_tool(3))

    monkeypatch.undo()
    caplog.clear()

    # ④ NdjsonAuditSink 生产失败（路径为目录 → IsADirectoryError）
    # → 内部 ERROR 级（第九轮生产通道补齐的回归钉）
    dir_path = tmp_path / "as_dir"
    dir_path.mkdir()
    sink = NdjsonAuditSink(dir_path)
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        sink.record({"tool": "t", "input": {}, "ok": True})
    prod_errors = [r for r in caplog.records
                   if r.levelno == stdlib_logging.ERROR
                   and "audit write failed path=" in r.getMessage()]
    assert prod_errors, "生产 sink 写失败须记 ERROR（round-9 生产通道钉）"
    assert all(r.exc_info is not None for r in prod_errors)     # 栈半（round-10 补）
    # 插值序钉住（round-11 收紧）：startswith 断言 path 先、异常后——
    # swapped 形态（path 位置填异常文本）含 Errno 21 亦通过旧断言
    assert prod_errors[0].getMessage().startswith(
        f"audit write failed path={dir_path}:")

    # ⑤ record 守卫钉住（round-11——第十轮标题修复此前零覆盖）：生产失败
    # + raising logger 的组合下 record 不抛（Exception 级永不抛出契约）；
    # 经 wired 路径该守卫行为不可见（_audit_write 外层网捕获逃逸、二次
    # 守卫打同一坏 logger 被吞——两变体可观测结果相同），故直调钉契约
    monkeypatch.setattr("common.audit.logger.error", _raising_error)
    sink_guarded = NdjsonAuditSink(dir_path)
    sink_guarded.record({"tool": "t", "input": {}, "ok": True})   # 不抛即通过
    monkeypatch.undo()


def test_audited_hostile_str_sink(caplog, tmp_path):
    """round-12 定向检视收尾 + round-13 闭包：病态 ``__str__`` 的 sink 违约钉住。

    f-string 预格式化把 ``str(exc)`` 求值移到 except 块内的急切插值
    （r11 延迟插值在 Handler.emit 内被 handleError 吞、不逃），未防御时
    病态 ``__str__`` 会从 ``_audit_write`` 逃出——成功路径摧毁已完成
    结果（r7 不变量 ②）、失败路径替换原异常（①）、KI/SE 不再重抛（r8）。
    ``_safe_str``（对齐 stdlib ``traceback._safe_string`` 的 bare-except
    口径）使插值点不可成为逃逸口；round-13 把 except 收到 BaseException
    ——``__str__`` 抛 KI/SE 时同样吞（fallback 为静态文本不可再抛）。"""
    import logging as stdlib_logging

    class _EvilStr(Exception):
        def __str__(self) -> str:  # pragma: no cover - 病态路径本体
            raise ValueError("str() is broken")

    class _EvilStrKI(KeyboardInterrupt):
        def __str__(self) -> str:  # pragma: no cover - 病态路径本体
            raise ValueError("ki str() is broken")

    class _EvilStrRaisesKI(Exception):
        """round-13 变体：``__str__`` 抛 BaseException（KI）——except
        Exception 形态的逃逸口（三方闭包检视执行级复现的缺口）。"""

        def __str__(self) -> str:  # pragma: no cover - 病态路径本体
            raise KeyboardInterrupt("nested ki from __str__")

    class _EvilExitRaisesKI(SystemExit):
        def __str__(self) -> str:  # pragma: no cover - 病态路径本体
            raise KeyboardInterrupt("nested ki from __str__")

    class _HostileSink:
        def record(self, event) -> None:
            raise _EvilStr()

    class _HostileKiSink:
        def record(self, event) -> None:
            raise _EvilStrKI()

    class _HostileRaisesKiSink:
        def record(self, event) -> None:
            raise _EvilStrRaisesKI()

    class _HostileExitRaisesKiSink:
        def record(self, event) -> None:
            raise _EvilExitRaisesKI()

    host = type("Host", (), {})()
    host.config = type("C", (), {"audit_sink": _HostileSink()})()

    class _Svc:
        def __init__(self, config):
            self.config = config

        @audited
        async def ok_tool(self, x: int) -> dict:
            return {"ok": True, "x": x}

        @audited
        async def fail_tool(self) -> dict:
            raise RuntimeError("boom")

    svc = _Svc(host.config)

    # ① 成功路径：已完成结果不摧毁（r7 不变量 ②——插值错误不得替换结果）
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        assert asyncio.run(svc.ok_tool(9)) == {"ok": True, "x": 9}
    suppress_errors = [r for r in caplog.records
                       if "best-effort suppressed" in r.getMessage()]
    assert suppress_errors and all(
        "<unprintable>" in r.getMessage() for r in suppress_errors)
    assert all(r.exc_info is not None for r in suppress_errors)  # 栈通道仍在

    # ② 失败路径：原异常不被替换（r7 不变量 ①——str(exc) 失败不劫持 boom）
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(svc.fail_tool())

    # ③ KI/SE 路径：停机信号仍重抛（r8——插值错误不得吞掉 raise 语义）
    ki_host = type("Host", (), {})()
    ki_host.config = type("C", (), {"audit_sink": _HostileKiSink()})()
    caplog.clear()
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(_Svc(ki_host.config).ok_tool(1))
    anchors = [r for r in caplog.records
               if "aborted by shutdown-signal" in r.getMessage()]
    assert anchors and "<unprintable>" in anchors[0].getMessage()

    # ③b round-13：__str__ 抛 KI（BaseException）——suppress 支全吞：
    # 嵌套 KI 不得逃出 _audit_write（成功结果不摧毁、原异常不替换）
    nested_host = type("Host", (), {})()
    nested_host.config = type("C", (), {"audit_sink": _HostileRaisesKiSink()})()
    nested_svc = _Svc(nested_host.config)
    caplog.clear()
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        assert asyncio.run(nested_svc.ok_tool(2)) == {"ok": True, "x": 2}
    nested_suppress = [r for r in caplog.records
                       if "best-effort suppressed" in r.getMessage()]
    assert nested_suppress and all(
        "<unprintable>" in r.getMessage() for r in nested_suppress)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(nested_svc.fail_tool())

    # ③c round-13：__str__ 抛 KI——anchor 支：原 SE 仍重抛（嵌套 KI 不得
    # 替换停机信号）+ 锚点日志在场（诊断面不丢）
    nested_exit_host = type("Host", (), {})()
    nested_exit_host.config = type(
        "C", (), {"audit_sink": _HostileExitRaisesKiSink()})()
    caplog.clear()
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        with pytest.raises(SystemExit):
            asyncio.run(_Svc(nested_exit_host.config).ok_tool(3))
    nested_anchors = [r for r in caplog.records
                      if "aborted by shutdown-signal" in r.getMessage()]
    assert nested_anchors and "<unprintable>" in nested_anchors[0].getMessage()
    assert nested_anchors[0].exc_info is not None

    # ④ record 站点：hostile 事件载荷 → 不抛（Exception 级永不抛出）+
    # 诊断面保留。实测形态：json.dumps 对不可序列化对象抛 TypeError
    # （不调 __str__，病态 str 在此构造不可达）——钉不抛+ERROR 在场即可。
    # 该站点的 `_safe_str` 为共享防御的均匀放置（纵深位）：站点自身异常
    # 源（dumps TypeError / open-write OSError）``__str__`` 恒安全，
    # fallback 在此不可达——其行为由 ①-③ 钉住
    caplog.clear()
    with caplog.at_level(stdlib_logging.ERROR, logger="common.audit"):
        NdjsonAuditSink(tmp_path / "hostile.jsonl").record(
            {"tool": "t", "input": {"evil": _EvilStr()}, "ok": True})
    prod = [r for r in caplog.records
            if "audit write failed path=" in r.getMessage()]
    assert prod and "not JSON serializable" in prod[0].getMessage()
