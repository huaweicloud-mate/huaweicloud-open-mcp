"""S11：mcp_data.engine 内部接缝纯函数单测（SQL 只读守卫 / JSON-safe 规范化 / 截断）。

独立真值：手写字面量矩阵。不联网、不依赖 datafusion 运行时（守卫用 sqlparse）。
"""

import json
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from mcp_data import engine as data_engine
from mcp_data.engine import DataError, assert_readonly_sql, json_safe, truncate_rows

# ---------- assert_readonly_sql ----------

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from t",
    "  SELECT 1;  ",
    "WITH t AS (SELECT 1 AS v) SELECT * FROM t",
    "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a JOIN b ON a.v = b.v",
    "EXPLAIN SELECT 1",
    "EXPLAIN ANALYZE SELECT 1",
    "SHOW TABLES",
    "DESCRIBE t",
    "describe t",
    "SELECT 'a;b' AS s",
    "SELECT ';' AS s",
    "-- 注释\nSELECT 1",
    "/* block */ SELECT 1",
    "SELECT * FROM t WHERE s = ';' -- trailing; not a statement",
])
def test_readonly_sql_allows(sql):
    assert_readonly_sql(sql)


@pytest.mark.parametrize("sql", [
    "",
    "   ",
    ";",
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE t",
    "INSERT INTO t VALUES (1)",
    "insert into t select * from u",
    "UPDATE t SET v = 1",
    "DELETE FROM t",
    "CREATE TABLE t (v INT)",
    "CREATE EXTERNAL TABLE t STORED AS CSV LOCATION '/tmp/x.csv' AS SELECT 1",
    "COPY (SELECT 1) TO '/tmp/x.csv'",
    "copy t to 'out.parquet'",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN v INT",
    "SET datafusion.execution.target_partitions = '4'",
    "TRUNCATE TABLE t",
    "MERGE INTO t USING u ON t.v = u.v WHEN MATCHED THEN DELETE",
    "SELECT 1 INTO out_table",
    "PREPARE s AS SELECT 1",
])
def test_readonly_sql_rejects(sql):
    with pytest.raises(DataError):
        assert_readonly_sql(sql)


def test_readonly_sql_error_carries_reason():
    with pytest.raises(DataError) as exc:
        assert_readonly_sql("INSERT INTO t VALUES (1)")
    assert exc.value.reason


# ---------- json_safe ----------

def test_json_safe_passes_primitives():
    assert json_safe(None) is None
    assert json_safe(True) is True
    assert json_safe(1) == 1
    assert json_safe(3.14) == 3.14
    assert json_safe("s") == "s"


def test_json_safe_datetime_to_iso():
    assert json_safe(datetime(2026, 9, 5, 12, 0, 0)) == "2026-09-05T12:00:00"


def test_json_safe_date_and_time_to_iso():
    assert json_safe(date(2026, 9, 5)) == "2026-09-05"
    assert json_safe(time(12, 30, 0)) == "12:30:00"


def test_json_safe_decimal_to_str():
    assert json_safe(Decimal("1.50")) == "1.50"


def test_json_safe_bytes_placeholder():
    assert json_safe(b"abc") == "<binary 3 bytes>"


def test_json_safe_nonfinite_float_to_none():
    assert json_safe(float("nan")) is None
    assert json_safe(float("inf")) is None
    assert json_safe(float("-inf")) is None


def test_json_safe_recursive():
    out = json_safe({"a": [1, datetime(2026, 9, 5)], "b": Decimal("2")})
    assert out == {"a": [1, "2026-09-05T00:00:00"], "b": "2"}


# ---------- truncate_rows ----------

def _rows(n: int, fill: str = "x") -> list[dict]:
    return [{"v": fill} for _ in range(n)]


def test_truncate_rows_within_limits():
    kept, truncated = truncate_rows(_rows(10), max_rows=100)
    assert len(kept) == 10
    assert truncated is False


def test_truncate_rows_by_max_rows():
    kept, truncated = truncate_rows(_rows(150), max_rows=100)
    assert len(kept) == 100
    assert truncated is True


def test_truncate_rows_empty():
    kept, truncated = truncate_rows([], max_rows=100)
    assert kept == []
    assert truncated is False


def test_truncate_rows_by_char_budget_cut_at_row_boundary():
    rows = [{"v": "y" * 1000} for _ in range(50)]
    kept, truncated = truncate_rows(rows, max_rows=100, max_chars=10_000)
    assert 0 < len(kept) < 50
    assert truncated is True
    assert sum(len(r["v"]) for r in kept) + 10_000 > 10_000 - 2000


def test_truncate_rows_keeps_first_row_when_single_row_exceeds_budget():
    rows = [{"v": "z" * 50_000}]
    kept, truncated = truncate_rows(rows, max_rows=100, max_chars=1000)
    assert kept == rows
    assert truncated is True


def test_truncate_rows_json_shape_counts_chars():
    rows = [{"中文": "长" * 300}]
    kept, truncated = truncate_rows(rows, max_rows=10, max_chars=100)
    assert truncated is True

# ---------- run_query：真 datafusion 集成（切片 2） ----------

def _inline(n: int = 4) -> dict:
    return {"t": {"data": [{"g": i % 2, "v": i} for i in range(n)]}}


def test_run_query_inline_select():
    out = data_engine.run_query(
        {"t": {"data": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]}},
        "SELECT a, b FROM t ORDER BY a")
    assert out["columns"] == [{"name": "a", "type": "int64"},
                              {"name": "b", "type": "string"}]
    assert out["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert out["total_rows"] == 2 and out["returned_rows"] == 2
    assert out["truncated"] is False and out["tables"] == ["t"]


def test_run_query_aggregate_independent_truth():
    rows = [{"g": i % 4, "v": i} for i in range(100)]
    out = data_engine.run_query({"t": {"data": rows}},
                                "SELECT g, COUNT(*) AS c, SUM(v) AS s FROM t GROUP BY g ORDER BY g")
    expect = [{"g": g, "c": sum(1 for i in range(100) if i % 4 == g),
               "s": sum(i for i in range(100) if i % 4 == g)} for g in range(4)]
    assert out["rows"] == expect


def test_run_query_join_two_tables():
    out = data_engine.run_query(
        {"u": {"data": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]},
         "v": {"data": [{"uid": 1, "val": 10}, {"uid": 2, "val": 20}]}},
        "SELECT u.name, v.val FROM u JOIN v ON u.id = v.uid ORDER BY u.id")
    assert out["rows"] == [{"name": "a", "val": 10}, {"name": "b", "val": 20}]
    assert sorted(out["tables"]) == ["u", "v"]


def test_run_query_datetime_json_safe():
    out = data_engine.run_query(
        {"t": {"data": [{"ts": datetime(2026, 9, 5, 12, 0)}]}},
        "SELECT ts FROM t")
    assert out["rows"] == [{"ts": "2026-09-05T12:00:00"}]


def test_run_query_default_max_rows_and_truncation():
    out = data_engine.run_query(_inline(150), "SELECT g, v FROM t")
    assert out["total_rows"] == 150
    assert out["returned_rows"] == 100
    assert out["truncated"] is True


def test_run_query_max_rows_clamped_to_cap():
    out = data_engine.run_query(_inline(1500), "SELECT g, v FROM t", max_rows=5000)
    assert out["returned_rows"] == 1000
    assert out["truncated"] is True


def test_run_query_explicit_max_rows_no_truncation():
    out = data_engine.run_query(_inline(150), "SELECT g, v FROM t", max_rows=10)
    assert out["returned_rows"] == 10 and out["truncated"] is True


def test_run_query_csv_file(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
    out = data_engine.run_query({"t": {"path": str(p)}}, "SELECT a FROM t ORDER BY a")
    assert out["rows"] == [{"a": 1}, {"a": 2}]


def test_run_query_ndjson_file(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    out = data_engine.run_query({"t": {"path": str(p)}}, "SELECT SUM(a) AS s FROM t")
    assert out["rows"] == [{"s": 3}]


def test_run_query_parquet_file(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    p = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2]}), p)
    out = data_engine.run_query({"t": {"path": str(p)}}, "SELECT MAX(a) AS m FROM t")
    assert out["rows"] == [{"m": 2}]


def test_run_query_json_array_file(tmp_path):
    """JSON 数组文件直接作为表源（API 结果落盘 → 分析闭环）。"""
    p = tmp_path / "t.json"
    p.write_text('[{"a": 1}, {"a": 2}]', encoding="utf-8")
    out = data_engine.run_query({"t": {"path": str(p)}}, "SELECT SUM(a) AS s FROM t")
    assert out["rows"] == [{"s": 3}]


def test_run_query_json_array_empty_rejected(tmp_path):
    p = tmp_path / "t.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(DataError):
        data_engine.run_query({"t": {"path": str(p)}}, "SELECT * FROM t")


def test_run_query_json_array_non_dicts_rejected(tmp_path):
    p = tmp_path / "t.json"
    p.write_text('[1, 2, 3]', encoding="utf-8")
    with pytest.raises(DataError):
        data_engine.run_query({"t": {"path": str(p)}}, "SELECT * FROM t")


def test_run_query_json_ndjson_content_rejected_with_hint(tmp_path):
    """扩展名 .json 但内容是 NDJSON：报错并指引改扩展名/显式 format。"""
    p = tmp_path / "t.json"
    p.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    with pytest.raises(DataError) as exc:
        data_engine.run_query({"t": {"path": str(p)}}, "SELECT * FROM t")
    assert "jsonl" in exc.value.reason


def test_run_query_missing_file():
    with pytest.raises(DataError):
        data_engine.run_query({"t": {"path": "/nonexistent/t.csv"}}, "SELECT * FROM t")


def test_run_query_guard_rejects_before_execution():
    with pytest.raises(DataError):
        data_engine.run_query(_inline(), "COPY (SELECT 1) TO '/tmp/x.csv'")


def test_run_query_unknown_table():
    with pytest.raises(DataError):
        data_engine.run_query(None, "SELECT * FROM missing_t")


def test_run_query_bad_table_name():
    with pytest.raises(DataError):
        data_engine.run_query({"1t": {"data": [{"a": 1}]}}, "SELECT 1")


def test_run_query_empty_inline_data():
    with pytest.raises(DataError):
        data_engine.run_query({"t": {"data": []}}, "SELECT * FROM t")


def test_run_query_inline_rows_not_dicts():
    with pytest.raises(DataError):
        data_engine.run_query({"t": {"data": [1, 2]}}, "SELECT * FROM t")


# ---------- run_transform：转换落盘（切片 1） ----------

_TRANSFORM_ROWS = [{"g": "a", "v": 1}, {"g": "b", "v": 2}, {"g": "a", "v": 3}]
_TRANSFORM_TABLES = {"t": {"data": _TRANSFORM_ROWS}}


def test_run_transform_csv_roundtrip_independent_truth(tmp_path):
    out_path = tmp_path / "out.csv"
    out = data_engine.run_transform(_TRANSFORM_TABLES,
                                    "SELECT g, v FROM t WHERE v >= 2 ORDER BY v",
                                    {"path": str(out_path)})
    assert out["path"] == str(out_path) and out["format"] == "csv"
    assert out["rows"] == 2
    assert out["columns"] == [{"name": "g", "type": "string"}, {"name": "v", "type": "int64"}]
    assert out["preview"] == [{"g": "b", "v": 2}, {"g": "a", "v": 3}]
    text = out_path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "g,v"          # 独立真值：表头
    assert sorted(text.splitlines()[1:]) == ["a,3", "b,2"]
    assert out["bytes"] == out_path.stat().st_size
    assert not (tmp_path / "out.csv.tmp-part").exists()


def test_run_transform_parquet_roundtrip(tmp_path):
    import pyarrow.parquet as pq
    out_path = tmp_path / "out.parquet"
    out = data_engine.run_transform(_TRANSFORM_TABLES,
                                    "SELECT g, SUM(v) AS s FROM t GROUP BY g ORDER BY g",
                                    {"path": str(out_path)})
    assert out["rows"] == 2
    table = pq.read_table(out_path)
    assert table.to_pylist() == [{"g": "a", "s": 4}, {"g": "b", "s": 2}]


def test_run_transform_jsonl_roundtrip(tmp_path):
    out_path = tmp_path / "out.jsonl"
    data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t", {"path": str(out_path)})
    lines = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert sorted(lines, key=lambda r: r["v"]) == _TRANSFORM_ROWS


def test_run_transform_explicit_format_override(tmp_path):
    out_path = tmp_path / "out.dat"
    out = data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t",
                                    {"path": str(out_path), "format": "csv"})
    assert out["format"] == "csv"
    assert out_path.read_text(encoding="utf-8").splitlines()[0] == "g,v"


def test_run_transform_unknown_extension_rejected(tmp_path):
    with pytest.raises(DataError):
        data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t",
                                  {"path": str(tmp_path / "out.xyz")})


def test_run_transform_default_refuses_overwrite(tmp_path):
    out_path = tmp_path / "out.csv"
    out_path.write_text("sentinel", encoding="utf-8")
    with pytest.raises(DataError) as exc:
        data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t",
                                  {"path": str(out_path)})
    assert "overwrite" in exc.value.reason
    assert out_path.read_text(encoding="utf-8") == "sentinel"   # 原文件未动


def test_run_transform_explicit_overwrite(tmp_path):
    out_path = tmp_path / "out.csv"
    out_path.write_text("sentinel", encoding="utf-8")
    out = data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t",
                                    {"path": str(out_path)}, overwrite=True)
    assert out["rows"] == 3
    assert "sentinel" not in out_path.read_text(encoding="utf-8")


def test_run_transform_empty_result(tmp_path):
    out_path = tmp_path / "out.csv"
    out = data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t WHERE v > 100",
                                    {"path": str(out_path)})
    assert out["rows"] == 0 and out["preview"] == []
    assert out_path.read_text(encoding="utf-8").splitlines() == ["g,v"]


def test_run_transform_preview_capped(tmp_path):
    out = data_engine.run_transform({"t": {"data": [{"a": i} for i in range(50)]}},
                                    "SELECT * FROM t", {"path": str(tmp_path / "o.parquet")},
                                    preview_rows=5)
    assert len(out["preview"]) == 5


def test_run_transform_rejects_non_select(tmp_path):
    with pytest.raises(DataError):
        data_engine.run_transform(_TRANSFORM_TABLES, "COPY (SELECT 1) TO '/tmp/x.csv'",
                                  {"path": str(tmp_path / "o.csv")})


def test_run_transform_failure_leaves_no_tmp_part(tmp_path):
    with pytest.raises(DataError):
        data_engine.run_transform({"t": {"data": [{"a": "x"}]}},
                                  "SELECT 1/0 FROM t", {"path": str(tmp_path / "o.csv")})
    assert list(tmp_path.iterdir()) == []   # 无半截产物


def test_run_transform_out_requires_path():
    with pytest.raises(DataError):
        data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t", {})
    with pytest.raises(DataError):
        data_engine.run_transform(_TRANSFORM_TABLES, "SELECT * FROM t", None)


def test_run_transform_json_array_source_to_parquet(tmp_path):
    """API 结果 .json 落地 → 一步转 parquet 闭环。"""
    src = tmp_path / "api_result.json"
    src.write_text(json.dumps([{"id": i, "name": f"n{i}"} for i in range(4)]), encoding="utf-8")
    out_path = tmp_path / "api_result.parquet"
    out = data_engine.run_transform({"t": {"path": str(src)}},
                                    "SELECT id, name FROM t ORDER BY id",
                                    {"path": str(out_path)})
    assert out["rows"] == 4
    import pyarrow.parquet as pq
    assert pq.read_table(out_path).num_rows == 4
