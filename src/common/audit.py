"""审计 NDJSON 写入器（第1层，零内部依赖）。

AuditSink 是审计持久化的接缝：生产用 NdjsonAuditSink（每事件一行 JSON、
best-effort 永不抛出），未配置审计用 NullAuditSink，测试注入内存替身。
sink 只拥有信封（ts）与持久化语义；事件 payload 的 schema 由
build_audit_event 定义（对 verifier 的已发布契约：tool/input/ok）。
audited 装饰器供各模式 service 复用（openapi/discover/data 同构），
契约：self.config.audit_sink 提供 sink（None=零开销跳过）。
"""

import functools
import inspect
import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeVar

logger = logging.getLogger("common.audit")

_F = TypeVar("_F", bound=Callable[..., Any])


def build_audit_event(tool: str, input_args: Mapping[str, Any],
                      result: Any) -> dict[str, Any]:
    """审计事件 payload（对 verifier 的已发布契约）：tool/input/ok；ts 由 sink 注入。

    ok 取 result 的 ok 字段（缺失视为成功）；input 为调用方显式入参快照
    （不含默认值，对齐 agent 侧 trace 口径）。
    """
    ok = bool(result.get("ok", True)) if isinstance(result, Mapping) else True
    return {"tool": tool, "input": dict(input_args), "ok": ok}


def audited(fn: _F) -> _F:
    """工具方法审计装饰器：每次调用经 self.config.audit_sink 记一条事件。

    input 为绑定后的显式入参（不含 self 与默认值）；方法抛异常时记 ok=False
    并原样抛出。签名保持：装饰不改变方法对调用方的可见类型。
    契约：宿主 service 须暴露 config.audit_sink（None=零开销跳过）。
    """
    sig = inspect.signature(fn)
    tool = fn.__name__

    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            bound = sig.bind(self, *args, **kwargs)
            input_args = {k: v for k, v in bound.arguments.items() if k != "self"}
        except TypeError:
            input_args = {}
        try:
            result = fn(self, *args, **kwargs)
        except Exception:
            _audit_write(self, tool, input_args, {"ok": False})
            raise
        _audit_write(self, tool, input_args, result)
        return result

    return wrapper  # type: ignore[return-value]


def _audit_write(host: Any, tool: str, input_args: Mapping[str, Any],
                 result: Any) -> None:
    """经宿主 config.audit_sink 记一条事件（best-effort，未配置 sink 零开销跳过）。"""
    sink = getattr(host.config, "audit_sink", None)
    if sink is None:
        return
    sink.record(build_audit_event(tool, input_args, result))

logger = logging.getLogger("common.audit")


class AuditSink(Protocol):
    def record(self, event: Mapping[str, Any]) -> None:
        """追加一条审计事件。实现必须永不抛出（best-effort，失败仅告警）。"""
        ...


class NdjsonAuditSink:
    """NDJSON 文件 sink：每事件一行，追加写，逐行落盘保证读方可见。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: Mapping[str, Any]) -> None:
        try:
            line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event},
                              ensure_ascii=False)
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            logger.warning("audit write failed path=%s: %s", self._path, e)


class NullAuditSink:
    """未配置审计时的空实现。"""

    def record(self, event: Mapping[str, Any]) -> None:
        return None


def sink_from_path(path: str | Path | None) -> AuditSink | None:
    """路径为空返回 None（不审计）；否则返回文件 sink。"""
    if not path:
        return None
    return NdjsonAuditSink(path)
