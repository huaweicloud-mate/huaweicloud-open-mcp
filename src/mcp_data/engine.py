"""DataFusion 查询引擎封装（S11）。

外部接缝：run_query(tables, sql, *, max_rows) -> QueryDataResult 信封（切片 2）。
内部接缝（纯函数，本文件直接可测）：assert_readonly_sql / json_safe / truncate_rows。

错误模式：唯一 DataError(reason)，service 是唯一翻译点（→ ToolError 信封）。
datafusion 运行时惰性 import（切片 2），纯函数只依赖 sqlparse。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import sqlparse
from sqlparse import tokens as T

# 结果行字符预算（与 execute.MAX_RESPONSE_CHARS 同口径）
MAX_RESULT_CHARS = 200_000

# 只读语句首关键字白名单（fail-closed：白名单外一律拒绝）
_READONLY_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE"})

# 返回行数：默认与硬上限
DEFAULT_MAX_ROWS = 100
MAX_ROWS_CAP = 1000

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_INSTALL_HINT = ('data 模式需要 DataFusion 引擎但未安装：'
                 'pip install "huaweicloud-open-mcp[datafusion]"'
                 '（或 uv add "huaweicloud-open-mcp[datafusion]"）')


class DataError(Exception):
    """引擎统一错误：reason 为可操作描述，service 直接透传给 ToolError。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_blank(statement: sqlparse.sql.Statement) -> bool:
    """仅由空白/分号构成的语句视为空（sqlparse 会把裸 ';' 切成独立 statement）。"""
    return all(
        tok.is_whitespace or tok.ttype in (T.Punctuation, T.Whitespace)
        for tok in statement.tokens
    )


def _first_keyword(statement: sqlparse.sql.Statement) -> str:
    """首个非注释/非空白 token 的归一化关键字（大写）。

    前导注释在 sqlparse 中可能 ttype=None（未分型），按文本形态识别跳过。
    """
    for tok in statement.tokens:
        if tok.is_whitespace or tok.ttype in T.Comment:
            continue
        text = tok.normalized
        if tok.ttype is None and text.lstrip().startswith(("--", "/*", "#")):
            continue
        return str(text).strip().upper()
    return ""


def assert_readonly_sql(sql: str) -> None:
    """只读守卫：单语句 + 首关键字白名单 + 拒 SELECT INTO（含 CTE 内嵌）。

    任何违规抛 DataError。字符串/注释内的分号不构成语句边界（sqlparse 语义）。
    """
    statements = [st for st in sqlparse.parse(sql or "") if not _is_blank(st)]
    if not statements:
        raise DataError("SQL 为空：请提供一条只读查询（SELECT/WITH/EXPLAIN/SHOW/DESCRIBE）")
    if len(statements) > 1:
        raise DataError("仅允许单条 SQL 语句（检测到多语句）")
    statement = statements[0]
    keyword = _first_keyword(statement)
    if keyword not in _READONLY_KEYWORDS:
        raise DataError(
            f"仅允许只读查询（SELECT/WITH/EXPLAIN/SHOW/DESCRIBE），语句以 {keyword!r} 开头")
    for tok in statement.flatten():
        if tok.ttype == T.Keyword and tok.normalized.upper() == "INTO":
            raise DataError("SELECT INTO / 内嵌 INTO 语句不支持（只读口径）")


def json_safe(value: Any) -> Any:
    """arrow → python 后的 JSON-safe 强制：时间→ISO、Decimal→str、bytes→占位、
    非有限浮点→null；容器递归；未知类型 str 兜底。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return str(value)


def truncate_rows(rows: list[dict[str, Any]], max_rows: int, *,
                  max_chars: int = MAX_RESULT_CHARS) -> tuple[list[dict[str, Any]], bool]:
    """双重截断：行数上限 + 序列化字符预算（行边界切分）。

    返回 (returned_rows, truncated)。单行即超预算时仍保留首行（保形可见）。
    """
    truncated = False
    kept = rows
    if len(rows) > max_rows:
        kept, truncated = rows[:max_rows], True

    out: list[dict[str, Any]] = []
    used = 0
    for row in kept:
        size = len(json.dumps(row, ensure_ascii=False, default=str))
        if out and used + size > max_chars:
            truncated = True
            break
        out.append(row)
        used += size
    if not out and kept:
        out, truncated = kept[:1], True
    elif used > max_chars:
        # 首行即超预算：保留保形，但标记截断
        truncated = True
    return out, truncated


# ---------- run_query：外部接缝（真 datafusion，惰性 import） ----------

def _register_tables(ctx: Any, tables: Mapping[str, Mapping[str, Any]] | None) -> None:
    """把 tables 映射注册进 SessionContext。

    表源两种形态：{"data": [dict, ...]}（inline，对象数组）或
    {"path": str, "format": "auto|csv|parquet|ndjson"}（文件，auto 按扩展名嗅探）。
    """
    import pyarrow as pa

    for name, source in (tables or {}).items():
        if not _TABLE_NAME_RE.match(name or ""):
            raise DataError(f"表名 {name!r} 非法（须匹配 ^[A-Za-z_][A-Za-z0-9_]*$）")
        source = dict(source or {})
        if "data" in source:
            rows = source["data"]
            if (not isinstance(rows, list) or not rows
                    or not all(isinstance(r, dict) for r in rows)):
                raise DataError(f"表 {name} 的 inline data 须为非空对象数组（list[dict]）")
            ctx.register_record_batches(name, [pa.Table.from_pylist(rows).to_batches()])
            continue
        path = source.get("path")
        if not path:
            raise DataError(f"表 {name!r} 须提供 data（inline 对象数组）或 path（本地文件）")
        if not os.path.isfile(path):
            raise DataError(f"表 {name} 的文件不存在: {path}")
        ext = os.path.splitext(path)[1].lower()
        fmt = str(source.get("format") or "auto").lower()
        if fmt == "auto":
            fmt = {".csv": "csv", ".parquet": "parquet",
                   ".jsonl": "ndjson", ".ndjson": "ndjson"}.get(ext, "")
            if not fmt:
                if ext == ".json":
                    raise DataError(
                        f"表 {name}：JSON 数组文件不受支持（{path}），"
                        "请转存为 NDJSON（.jsonl/.ndjson）")
                raise DataError(
                    f"表 {name}：无法从扩展名 {ext!r} 识别格式，"
                    "请显式指定 format=csv|parquet|ndjson")
        if fmt not in ("csv", "parquet", "ndjson"):
            raise DataError(f"表 {name}：format 仅支持 csv|parquet|ndjson，得到 {fmt!r}")
        if fmt == "csv":
            ctx.register_csv(name, path, file_extension=ext or ".csv")
        elif fmt == "parquet":
            ctx.register_parquet(name, path, file_extension=ext or ".parquet")
        else:
            ctx.register_json(name, path, file_extension=ext or ".json")


def run_query(tables: Mapping[str, Mapping[str, Any]] | None, sql: str,
              *, max_rows: int | None = None) -> dict[str, Any]:
    """外部接缝：一次性 SessionContext 上注册表 → 只读 SQL → 规范化截断信封。

    返回 {columns, rows, total_rows, returned_rows, truncated, tables}；
    任何失败抛 DataError(reason)（含 datafusion 未安装的安装指引）。
    max_rows：None→默认 100，钳位 [1, 1000]。
    """
    try:
        from datafusion import SessionContext  # noqa: F401
    except ImportError as e:
        raise DataError(_INSTALL_HINT) from e

    assert_readonly_sql(sql)
    limit = DEFAULT_MAX_ROWS if max_rows is None else int(max_rows)
    limit = max(1, min(limit, MAX_ROWS_CAP))

    ctx = SessionContext()
    _register_tables(ctx, tables)
    try:
        df = ctx.sql(sql)
        batches = df.collect()
    except Exception as e:
        raise DataError(f"SQL 执行失败: {e}") from e

    columns = [{"name": f.name, "type": str(f.type)} for f in df.schema()]
    total_rows = 0
    rows: list[dict[str, Any]] = []
    for batch in batches:
        total_rows += batch.num_rows
        if len(rows) < limit:
            take = min(limit - len(rows), batch.num_rows)
            rows.extend(batch.slice(0, take).to_pylist())
    rows = [json_safe(row) for row in rows]
    rows, truncated_by_chars = truncate_rows(rows, limit)
    return {"columns": columns,
            "rows": rows,
            "total_rows": total_rows,
            "returned_rows": len(rows),
            "truncated": bool(truncated_by_chars or total_rows > limit),
            "tables": sorted((tables or {}).keys())}
