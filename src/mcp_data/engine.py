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

def _register_inline_rows(ctx: Any, name: str, rows: Any) -> None:
    """inline/JSON 数组 → arrow 表注册（共享分型校验：非空对象数组）。"""
    import pyarrow as pa

    if (not isinstance(rows, list) or not rows
            or not all(isinstance(r, dict) for r in rows)):
        raise DataError(f"表 {name} 的数据须为非空对象数组（list[dict]）")
    ctx.register_record_batches(name, [pa.Table.from_pylist(rows).to_batches()])


def _register_tables(ctx: Any, tables: Mapping[str, Mapping[str, Any]] | None) -> None:
    """把 tables 映射注册进 SessionContext。

    表源两种形态：{"data": [dict, ...]}（inline，对象数组）或
    {"path": str, "format": "auto|csv|parquet|ndjson|json"}（文件，auto 按扩展名
    嗅探；.json 数组全量载入内存，超大文件建议转 jsonl）。
    """
    for name, source in (tables or {}).items():
        if not _TABLE_NAME_RE.match(name or ""):
            raise DataError(f"表名 {name!r} 非法（须匹配 ^[A-Za-z_][A-Za-z0-9_]*$）")
        source = dict(source or {})
        if "data" in source:
            _register_inline_rows(ctx, name, source["data"])
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
                   ".jsonl": "ndjson", ".ndjson": "ndjson", ".json": "json"}.get(ext, "")
            if not fmt:
                raise DataError(
                    f"表 {name}：无法从扩展名 {ext!r} 识别格式，"
                    "请显式指定 format=csv|parquet|ndjson|json")
        if fmt not in ("csv", "parquet", "ndjson", "json"):
            raise DataError(
                f"表 {name}：format 仅支持 csv|parquet|ndjson|json，得到 {fmt!r}")
        if fmt == "csv":
            ctx.register_csv(name, path, file_extension=ext or ".csv")
        elif fmt == "parquet":
            ctx.register_parquet(name, path, file_extension=ext or ".parquet")
        elif fmt == "json":
            try:
                with open(path, encoding="utf-8") as f:
                    rows = json.load(f)
            except json.JSONDecodeError as e:
                raise DataError(
                    f"表 {name}：JSON 数组文件解析失败（{path}）；"
                    "若内容为 NDJSON 请改扩展名 .jsonl 或显式 format=ndjson") from e
            _register_inline_rows(ctx, name, rows)
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


# ---------- run_transform：转换落盘（写 lane） ----------

# 输出格式嗅探（扩展名 → 格式）
_OUTPUT_FORMATS = {".csv": "csv", ".parquet": "parquet",
                   ".jsonl": "ndjson", ".ndjson": "ndjson"}

# 转换预览行数：默认与硬上限（预览仅为结果形态可见，不承载完整数据）
DEFAULT_PREVIEW_ROWS = 10
MAX_PREVIEW_ROWS = 100


def _sniff_output_format(out: Mapping[str, Any]) -> tuple[str, str]:
    """out 参数解析：path 必填，格式按扩展名嗅探、显式 format 覆盖。返回 (path, fmt)。"""
    if not isinstance(out, Mapping) or not out.get("path"):
        raise DataError('out 须为 {"path": 输出文件路径, "format"?: "csv|parquet|ndjson"}')
    path = str(out["path"])
    fmt = str(out.get("format") or "auto").lower()
    if fmt == "auto":
        fmt = _OUTPUT_FORMATS.get(os.path.splitext(path)[1].lower(), "")
        if not fmt:
            raise DataError(
                f"无法从扩展名识别输出格式（{path}），"
                "请显式指定 format=csv|parquet|ndjson")
    if fmt not in ("csv", "parquet", "ndjson"):
        raise DataError(f"输出 format 仅支持 csv|parquet|ndjson，得到 {fmt!r}")
    return path, fmt


def _count_written(path: str, fmt: str) -> int:
    """从落盘产物回读行数（独立于执行计划）：parquet 读元数据 / csv 减表头 / jsonl 数行。"""
    if fmt == "parquet":
        import pyarrow.parquet as pq
        rows: int = int(pq.ParquetFile(path).metadata.num_rows)
        return rows
    with open(path, "rb") as f:
        lines = sum(1 for line in f if line.strip())
    return max(lines - 1, 0) if fmt == "csv" else lines   # csv 恒带表头


def run_transform(tables: Mapping[str, Mapping[str, Any]] | None, sql: str,
                  out: Mapping[str, Any], *, overwrite: bool = False,
                  preview_rows: int | None = None) -> dict[str, Any]:
    """外部接缝（写 lane）：只读 SQL 转换注册表 → 结果落盘 → 回传产物元数据与小预览。

    Interface：
    - out = {"path", "format"?}：写目标为结构化参数（非 SQL 语句），审计可追溯；
      SQL 仍须通过只读守卫，写动作在守卫之后由引擎施加。
    - 覆盖：目标已存在默认拒绝，overwrite=true 显式放行（先移除再写）。
    - 原子落盘：先写同级 .tmp-part，成功后 os.replace；失败不留半截文件。
    - SQL 执行两遍（预览流式取前 N 行 + 写盘）：源为静态文件/inline 数组，
      SQL 不得依赖易变函数（random 等）。
    - 返回 {path, format, rows, bytes, columns, preview}；preview 默认 10 行
      （钳位 [0, 100]），大结果不进上下文。
    任何失败抛 DataError(reason)。
    """
    try:
        from datafusion import SessionContext  # noqa: F401
    except ImportError as e:
        raise DataError(_INSTALL_HINT) from e

    assert_readonly_sql(sql)
    path, fmt = _sniff_output_format(out)
    if not overwrite and os.path.exists(path):
        raise DataError(
            f"输出文件已存在（{path}）；显式传 overwrite=true 覆盖，或改用新路径")
    limit = (DEFAULT_PREVIEW_ROWS if preview_rows is None
             else max(0, min(int(preview_rows), MAX_PREVIEW_ROWS)))

    ctx = SessionContext()
    _register_tables(ctx, tables)

    # 执行一：列 schema + 有界预览（复用 run_query 的流式物化路径）
    try:
        df = ctx.sql(sql)
        batches = df.collect()
    except Exception as e:
        raise DataError(f"SQL 执行失败: {e}") from e
    columns = [{"name": f.name, "type": str(f.type)} for f in df.schema()]
    preview: list[dict[str, Any]] = []
    for batch in batches:
        if len(preview) >= limit:
            break
        take = min(limit - len(preview), batch.num_rows)
        preview.extend(batch.slice(0, take).to_pylist())
    preview = [json_safe(row) for row in preview]

    # 执行二：流式写盘 → 原子替换
    tmp = f"{path}.tmp-part"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        df2 = ctx.sql(sql)
        if fmt == "csv":
            df2.write_csv(tmp, with_header=True)
        elif fmt == "parquet":
            df2.write_parquet(tmp)
        else:
            df2.write_json(tmp)
        os.replace(tmp, path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise DataError(f"转换落盘失败: {e}") from e

    return {"path": path, "format": fmt, "rows": _count_written(path, fmt),
            "bytes": os.path.getsize(path), "columns": columns, "preview": preview}
