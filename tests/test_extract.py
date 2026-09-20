"""extract.py：execute_api body JSONPath 投影（_jsonpath 控制键）纯函数矩阵。

独立真值：手写字面量 body + 手算投影值；jsonpath-ng 不 mock（真库 in-process，
tantivy/S16a 口径），测试钉的是本模块的聚合/缺失语义而非库内幕。
"""

from mcp_openapi.extract import ExtractSpec, apply_extract, parse_extract


def _spec(spec):
    """parse 成功断言内嵌（合法表达式意外编译失败即红）。"""
    out = parse_extract(spec)
    assert not isinstance(out, str)
    assert isinstance(out, ExtractSpec)
    return out

BODY = {
    "servers": [
        {"id": "s1", "status": "running"},
        {"id": "s2", "status": "error"},
    ],
    "total": 2,
    "null_field": None,
}


# ---------- parse_extract：形态与错误 ----------

def test_parse_str_spec_compiles():
    spec = _spec("$.servers[0].id")
    assert not isinstance(spec, str)


def test_parse_empty_string_rejected():
    err = parse_extract("   ")
    assert isinstance(err, str)
    assert "_jsonpath" in err


def test_parse_invalid_syntax_rejected_with_source():
    err = parse_extract("$.servers[")
    assert isinstance(err, str)
    assert "$.servers[" in err          # 错误消息含原文（自纠）
    assert "_jsonpath" in err


def test_parse_non_str_dict_rejected():
    err = parse_extract(123)
    assert isinstance(err, str)
    assert "str 或 dict" in err


def test_parse_dict_mapping_compiles():
    spec = _spec({"id": "$.servers[0].id", "n": "$.total"})
    assert not isinstance(spec, str)


def test_parse_dict_empty_rejected():
    err = parse_extract({})
    assert isinstance(err, str)


def test_parse_dict_bad_alias_rejected():
    err = parse_extract({"": "$.total"})
    assert isinstance(err, str)


def test_parse_dict_bad_path_rejected_with_alias():
    err = parse_extract({"id": "$.servers["})
    assert isinstance(err, str)
    assert "id" in err and "$.servers[" in err


# ---------- apply_extract：单路径（str） ----------

def test_apply_single_path_hit():
    spec = _spec("$.servers[0].id")
    out = apply_extract(BODY, spec)
    assert out.extracted == "s1"
    assert out.full_hit is True
    assert out.misses == []


def test_apply_wildcard_multi_match_returns_list():
    spec = _spec("$.servers[*].id")
    out = apply_extract(BODY, spec)
    assert out.extracted == ["s1", "s2"]
    assert out.full_hit is True


def test_apply_recursive_descent():
    spec = _spec("$..id")
    out = apply_extract(BODY, spec)
    assert out.extracted == ["s1", "s2"]


def test_apply_filter_expression():
    spec = _spec("$.servers[?(@.status=='error')].id")
    out = apply_extract(BODY, spec)
    assert out.extracted == "s2"     # filter 恰命中 1 个 server → 单值（非 list）


def test_apply_bracket_quoted_key():
    body = {"x-constraint": "c1", "total": 2}
    spec = _spec('$["x-constraint"]')
    out = apply_extract(body, spec)
    assert out.extracted == "c1"


def test_apply_null_value_is_hit():
    spec = _spec("$.null_field")
    out = apply_extract(BODY, spec)
    assert out.extracted is None
    assert out.full_hit is True     # 命中 JSON null 节点 ≠ 未命中


def test_apply_zero_match_reports_miss():
    spec = _spec("$.servers[9].id")
    out = apply_extract(BODY, spec)
    assert out.extracted is None
    assert out.full_hit is False
    assert out.misses == ["$.servers[9].id: 无命中"]


def test_apply_missing_intermediate_reports_miss():
    spec = _spec("$.a.b.c")
    out = apply_extract(BODY, spec)
    assert out.full_hit is False
    assert out.misses == ["$.a.b.c: 无命中"]


def test_apply_root_returns_whole_body():
    spec = _spec("$")
    out = apply_extract(BODY, spec)
    assert out.extracted == BODY
    assert out.full_hit is True


# ---------- apply_extract：映射（dict[str, str]） ----------

def test_apply_mapping_all_hit():
    spec = _spec({"id": "$.servers[0].id", "n": "$.total"})
    out = apply_extract(BODY, spec)
    assert out.extracted == {"id": "s1", "n": 2}
    assert out.full_hit is True
    assert out.misses == []


def test_apply_mapping_partial_hit_keeps_full_keyset():
    spec = _spec({"id": "$.servers[0].id", "x": "$.nope"})
    out = apply_extract(BODY, spec)
    assert out.extracted == {"id": "s1", "x": None}   # 键集完整，未命中键 null
    assert out.full_hit is False
    assert out.misses == ["x: 无命中（$.nope）"]


def test_apply_mapping_multi_match_value_is_list():
    spec = _spec({"ids": "$.servers[*].id"})
    out = apply_extract(BODY, spec)
    assert out.extracted == {"ids": ["s1", "s2"]}
    assert out.full_hit is True


# ---------- apply_extract：载体降级（no-op + note） ----------

def test_apply_none_body_degrades_with_note():
    spec = _spec("$.a")
    out = apply_extract(None, spec)
    assert out.extracted is None
    assert out.full_hit is False
    assert out.note is not None and "不适用" in out.note


def test_apply_str_body_degrades_with_note():
    spec = _spec("$.a")
    out = apply_extract("<xml/>", spec)
    assert out.full_hit is False
    assert out.note is not None and "文本" in out.note


def test_apply_bytes_body_degrades_with_note():
    spec = _spec("$.a")
    out = apply_extract(b"\x89PNG", spec)
    assert out.full_hit is False
    assert out.note is not None and "二进制" in out.note


# ---------- apply_extract：输入不变性 ----------

def test_apply_does_not_mutate_body():
    import copy
    snapshot = copy.deepcopy(BODY)
    spec = _spec("$.servers[0].id")
    apply_extract(BODY, spec)
    assert BODY == snapshot
