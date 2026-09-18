"""apie.api_location 单测（S5 扩展，CONTEXT.md C）：冻结值类型 + operationId 查找单点。

独立真值：手写 mini doc 字面量（沿 live_fallback._find_api_in_doc 既有契约：
exact + 大小写不敏感，无子串兜底）。
"""

import pytest

from apie.api_location import ApiLocation


def _doc(op_id="StartupInstance"):
    return {
        "paths": {
            "/a": {"post": {"operationId": op_id, "summary": "A"}},
            "/b": {"get": {"operationId": "OtherOp", "summary": "B"}},
        }
    }


# ---------- find：operationId 精确 + 大小写不敏感（自 live_fallback 收拢） ----------

def test_find_exact():
    loc = ApiLocation.find(_doc(), "StartupInstance")
    assert loc is not None
    assert (loc.path, loc.method) == ("/a", "post")
    assert loc.op["summary"] == "A"
    assert loc.doc is not None


def test_find_case_insensitive():
    loc = ApiLocation.find(_doc(), "startupinstance")
    assert loc is not None and loc.method == "post"


def test_find_miss_returns_none():
    assert ApiLocation.find(_doc(), "Nope") is None
    assert ApiLocation.find({}, "x") is None


def test_find_is_tie_free_single_operation_id():
    """同名 operationId 跨方法：首个命中（路径序），无歧义抛错（远端按 exact 查询）。"""
    doc = {"paths": {
        "/a": {"post": {"operationId": "Dup"}},
        "/b": {"post": {"operationId": "Dup"}},
    }}
    loc = ApiLocation.find(doc, "Dup")
    assert loc.path == "/a"


# ---------- 冻结值类型：命名访问 + 不可变 ----------

def test_frozen_named_access():
    doc = _doc()
    op = doc["paths"]["/a"]["post"]
    loc = ApiLocation(doc=doc, path="/a", method="post", op=op)
    assert loc.doc is doc and loc.path == "/a" and loc.method == "post" and loc.op is op


def test_frozen_immutable():
    loc = ApiLocation(doc={}, path="/p", method="get", op={})
    with pytest.raises(Exception):
        loc.path = "/q"  # type: ignore[misc]
