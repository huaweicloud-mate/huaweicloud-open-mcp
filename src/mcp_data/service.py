"""DataService：data 模式业务编排层（S11）。

职责：audit 信封（与 openapi/discover service 同构，经 common.audit.audited）、
DataError→ToolError 翻译（唯一翻译点）、engine 注入。
query_data 不做 safety policy 检查（本地计算工具，口径见 AGENTS.md「校验规则」），
数据访问边界由 engine 只读守卫 + 截断约束。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from common.audit import AuditSink, audited
from common.types import QueryDataResult, ToolError, TransformDataResult

from . import engine

logger = logging.getLogger("mcp_data.service")


class Engine(Protocol):
    """engine 模块形接缝：读/写两 lane（模块级函数运行时 duck-typing 满足）。"""

    def run_query(self, tables: Mapping[str, Mapping[str, Any]] | None, sql: str, *,
                  max_rows: int | None = None) -> dict[str, Any]: ...

    def run_transform(self, tables: Mapping[str, Mapping[str, Any]] | None, sql: str,
                      out: Mapping[str, Any], *, overwrite: bool = False,
                      preview_rows: int | None = None) -> dict[str, Any]: ...


@dataclass
class DataConfig:
    audit_sink: AuditSink | None = None
    engine: Engine | None = None  # 默认 engine 模块


class DataService:
    def __init__(self, config: DataConfig | None = None):
        self.config = config or DataConfig()

    def _engine(self) -> Engine:
        if self.config.engine is not None:
            return self.config.engine
        return cast(Engine, engine)

    @audited
    def query_data(self, sql: str, tables: dict[str, dict[str, Any]] | None = None,
                   max_rows: int | None = None) -> QueryDataResult | ToolError:
        """只读 SQL 分析：注册临时表 → 引擎执行 → 规范化信封。

        tables 形态见 engine._register_tables；失败返回 {"ok": False, "reason"}。
        """
        logger.info("query_data tables=%s max_rows=%s sql=%s",
                    sorted((tables or {}).keys()), max_rows, (sql or "")[:120])
        try:
            out = self._engine().run_query(tables, sql, max_rows=max_rows)
        except engine.DataError as e:
            logger.warning("query_data result=error reason=%s", e.reason)
            return {"ok": False, "reason": e.reason}
        return cast(QueryDataResult, {"ok": True, **out})

    @audited
    def transform_data(self, sql: str, tables: dict[str, dict[str, Any]] | None = None,
                       out: dict[str, Any] | None = None, overwrite: bool = False,
                       preview_rows: int | None = None) -> TransformDataResult | ToolError:
        """转换落盘：只读 SQL 变换注册表 → 结果写文件（原子）→ 产物元数据+小预览。

        out = {"path", "format"?}；写路径审计可追溯（input 快照含 out/overwrite）。
        """
        out_path = out.get("path") if isinstance(out, Mapping) else None
        logger.info("transform_data tables=%s out=%s overwrite=%s sql=%s",
                    sorted((tables or {}).keys()), out_path, overwrite,
                    (sql or "")[:120])
        try:
            payload = self._engine().run_transform(tables, sql, out or {},
                                                   overwrite=overwrite,
                                                   preview_rows=preview_rows)
        except engine.DataError as e:
            logger.warning("transform_data result=error reason=%s", e.reason)
            return {"ok": False, "reason": e.reason}
        return cast(TransformDataResult, {"ok": True, **payload})
