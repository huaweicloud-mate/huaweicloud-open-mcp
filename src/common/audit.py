"""审计 NDJSON 写入器（第1层，零内部依赖）。

AuditSink 是审计持久化的接缝：生产用 NdjsonAuditSink（每事件一行 JSON、
best-effort 永不抛出），未配置审计用 NullAuditSink，测试注入内存替身。
sink 只拥有信封（ts）与持久化语义；事件 payload 的 schema 由各模式
service 层的 build_audit_event 定义（对 verifier 的已发布契约）。
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

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
