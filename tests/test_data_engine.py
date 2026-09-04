"""S11：mcp_data.engine 内部接缝纯函数单测（SQL 只读守卫 / JSON-safe 规范化 / 截断）。

独立真值：手写字面量矩阵。不联网、不依赖 datafusion 运行时（守卫用 sqlparse）。
"""

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


def test_run_query_json_array_file_rejected(tmp_path):
    p = tmp_path / "t.json"
    p.write_text('[{"a": 1}]', encoding="utf-8")
    with pytest.raises(DataError) as exc:
        data_engine.run_query({"t": {"path": str(p)}}, "SELECT * FROM t")
    assert "jsonl" in exc.value.reason or "ndjson" in exc.value.reason


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
