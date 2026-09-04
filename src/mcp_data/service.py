"""DataService：data 模式业务编排层（S11）。

职责：audit 信封（与 openapi/discover service 同构，经 common.audit.audited）、
DataError→ToolError 翻译（唯一翻译点）、engine 注入。
query_data 不做 safety policy 检查（本地计算工具，口径见 AGENTS.md「校验规则」），
数据访问边界由 engine 只读守卫 + 截断约束。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from common.audit import AuditSink, audited
from common.types import QueryDataResult, ToolError

from . import engine

logger = logging.getLogger("mcp_data.service")


@dataclass
class DataConfig:
    audit_sink: AuditSink | None = None
    engine: Callable[..., dict[str, Any]] | None = None  # 默认 engine.run_query


class DataService:
    def __init__(self, config: DataConfig | None = None):
        self.config = config or DataConfig()

    @audited
    def query_data(self, sql: str, tables: dict[str, dict[str, Any]] | None = None,
                   max_rows: int | None = None) -> QueryDataResult | ToolError:
        """只读 SQL 分析：注册临时表 → 引擎执行 → 规范化信封。

        tables 形态见 engine._register_tables；失败返回 {"ok": False, "reason"}。
        """
        logger.info("query_data tables=%s max_rows=%s sql=%s",
                    sorted((tables or {}).keys()), max_rows, (sql or "")[:120])
        try:
            out = self._engine()(tables, sql, max_rows=max_rows)
        except engine.DataError as e:
            logger.warning("query_data result=error reason=%s", e.reason)
            return {"ok": False, "reason": e.reason}
        return cast(QueryDataResult, {"ok": True, **out})

    def _engine(self) -> Callable[..., dict[str, Any]]:
        if self.config.engine is not None:
            return self.config.engine
        return engine.run_query
