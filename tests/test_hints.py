"""S10a：openapi 自定义提示注入 Hints 纯函数单测（parse/load/lookup/合并策略）。"""

import pytest

from mcp_openapi.hints import Hints, load_hints_file, parse_hints

RAW = {
    "instructions": "全局指引",
    "products": {
        "ECS": "产品级提示",
        "VPC": {
            "notes": "VPC 提示",
            "apis": {
                "ListServersDetails": "API 级提示",
                "CreateVpc": "创建提示",
            },
        },
    },
}


def test_empty_is_noop():
    h = Hints.empty()
    assert h.instructions is None
    assert h.product_notes("ECS") is None
    assert h.api_notes("ECS", "ListServersDetails") is None
    assert h.combined_notes("ECS", "ListServersDetails") is None


def test_parse_global_instructions():
    h = parse_hints({"instructions": "全局指引"})
    assert h.instructions == "全局指引"


def test_parse_instructions_empty_is_absent():
    h = parse_hints({"instructions": ""})
    assert h.instructions is None


def test_parse_product_string_shorthand():
    h = parse_hints({"products": {"ECS": "产品级提示"}})
    assert h.product_notes("ecs") == "产品级提示"
    assert h.product_notes("VPC") is None


def test_parse_product_object_form():
    h = parse_hints(RAW)
    assert h.product_notes("ECS") == "产品级提示"
    assert h.product_notes("vpc") == "VPC 提示"
    assert h.product_notes("OBS") is None


def test_product_notes_empty_string_is_absent():
    h = parse_hints({"products": {"ECS": ""}})
    assert h.product_notes("ECS") is None


def test_api_notes_api_level_only_and_case_insensitive():
    h = parse_hints(RAW)
    assert h.api_notes("VPC", "listserversdetails") == "API 级提示"
    assert h.api_notes("vpc", "ListServersDetails") == "API 级提示"
    assert h.api_notes("VPC", "DeleteVpc") is None
    assert h.api_notes("ECS", "ListServersDetails") is None


def test_combined_notes_merge_policy():
    h = parse_hints(RAW)
    assert h.combined_notes("vpc", "CreateVpc") == "VPC 提示\n创建提示"
    assert h.combined_notes("ECS", "Any") == "产品级提示"
    assert h.combined_notes("VPC", "Unknown") == "VPC 提示"
    assert h.combined_notes("OBS", "Any") is None


def test_combined_notes_product_without_notes_but_apis():
    h = parse_hints({"products": {"VPC": {"apis": {"A": "x"}}}})
    assert h.product_notes("VPC") is None
    assert h.combined_notes("VPC", "a") == "x"


def test_parse_minimal_form_is_noop():
    h = parse_hints({})
    assert h.instructions is None
    assert h.product_notes("ECS") is None


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_hints("nope")
    with pytest.raises(ValueError):
        parse_hints({"unknown": 1})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": 1}})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": {"unknown": 1}}})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": {"notes": 1}}})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": {"apis": {"A": 1}}}})


def test_load_hints_file(tmp_path):
    p = tmp_path / "h.json"
    p.write_text('{"instructions": "g", "products": {"ECS": "n"}}', encoding="utf-8")
    h = load_hints_file(str(p))
    assert h.instructions == "g"
    assert h.product_notes("ECS") == "n"


def test_load_hints_file_none_is_empty():
    assert load_hints_file(None).instructions is None


def test_load_hints_file_empty_path_is_empty():
    assert load_hints_file("").instructions is None
