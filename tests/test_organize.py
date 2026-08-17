"""organize 命名与合并逻辑单元测试。"""

from openmcp.apie import organize


def test_sanitize_tag_slash():
    assert organize.sanitize_tag("管理/订单") == "管理_订单"


def test_sanitize_tag_preserves_spaces():
    assert organize.sanitize_tag("标签 管理") == "标签 管理"


def test_sanitize_tag_illegal_chars():
    assert organize.sanitize_tag("a<b>c:d") == "a_b_c_d"


def test_sanitize_tag_backslash():
    assert organize.sanitize_tag("a\\b") == "a_b"


def test_merge_multi_dedup_operations():
    docs = {
        "f1": {"swagger": "2.0", "paths": {"/a": {"get": {"operationId": "GetA"}}},
               "definitions": {"D": {"type": "object", "description": "d"}}, "parameters": {}, "responses": {}},
        "f2": {"swagger": "2.0", "paths": {"/b": {"get": {"operationId": "GetB"}}},
               "definitions": {}, "parameters": {}, "responses": {}},
    }
    base, dup = organize.merge_multi(docs)
    assert dup == 0
    assert "/a" in base["paths"] and "/b" in base["paths"]
    assert base["definitions"]["D"]["description"] == "d"


def test_merge_multi_same_operation_dedup():
    docs = {
        "f1": {"swagger": "2.0", "paths": {"/x": {"get": {"operationId": "GetX"}}},
               "definitions": {}, "parameters": {}, "responses": {}},
        "f2": {"swagger": "2.0", "paths": {"/x": {"get": {"operationId": "GetX"}}},
               "definitions": {}, "parameters": {}, "responses": {}},
    }
    base, dup = organize.merge_multi(docs)
    assert dup == 1


def test_merge_multi_definition_prefer_description():
    docs = {
        "f1": {"swagger": "2.0", "paths": {"/a": {"get": {"operationId": "GetA"}}},
               "definitions": {"D": {"type": "object"}}, "parameters": {}, "responses": {}},
        "f2": {"swagger": "2.0", "paths": {"/b": {"get": {"operationId": "GetB"}}},
               "definitions": {"D": {"type": "object", "description": "with desc"}}, "parameters": {}, "responses": {}},
    }
    base, _ = organize.merge_multi(docs)
    assert base["definitions"]["D"]["description"] == "with desc"
