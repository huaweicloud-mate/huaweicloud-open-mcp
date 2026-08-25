"""S8a：openapi 产品准入门栓 Gate 纯函数单测。"""

import pytest

from mcp_openapi.gate import Gate, load_gate_file, parse_gate

GROUPS = [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器", "api_count": 4},
        {"productshort": "RabbitMQ", "name": "消息队列", "api_count": 2},
    ]},
    {"name": "网络", "products": [
        {"productshort": "VPC", "name": "虚拟私有云", "api_count": 3},
    ]},
]


def test_unrestricted_allows_everything():
    g = Gate.unrestricted()
    assert g.allows("ECS")
    assert g.allows("anything")


def test_unrestricted_filter_is_identity():
    g = Gate.unrestricted()
    assert g.filter_products(GROUPS) is GROUPS


def test_parse_gate_normalizes_and_restricts():
    g = parse_gate(["ecs", " VPC "])
    assert g.restrict is True
    assert g.allowed == frozenset({"ECS", "VPC"})


def test_parse_gate_mapping():
    g = parse_gate({"products": ["ECS"]})
    assert g.allows("ecs")
    assert not g.allows("VPC")


def test_allows_case_insensitive():
    g = parse_gate(["ECS"])
    assert g.allows("ecs")
    assert g.allows("ECS")


def test_allows_denies_unlisted():
    g = parse_gate(["ECS"])
    assert not g.allows("VPC")


def test_filter_products_hides_and_folds_empty_group():
    g = parse_gate(["ECS"])
    out = g.filter_products(GROUPS)
    assert [x["name"] for x in out] == ["计算"]
    assert [p["productshort"] for p in out[0]["products"]] == ["ECS"]


def test_filter_products_does_not_mutate_input():
    g = parse_gate(["ECS"])
    g.filter_products(GROUPS)
    assert len(GROUPS) == 2
    assert len(GROUPS[0]["products"]) == 2


def test_describe_unrestricted():
    assert "不限制" in Gate.unrestricted().describe()


def test_describe_restricted_lists_products():
    d = parse_gate(["ECS", "VPC"]).describe()
    assert "ECS" in d
    assert "VPC" in d


def test_load_gate_file(tmp_path):
    p = tmp_path / "g.json"
    p.write_text('{"products": ["ECS"]}', encoding="utf-8")
    g = load_gate_file(str(p))
    assert g.allows("ECS")
    assert not g.allows("VPC")


def test_load_gate_file_none_is_unrestricted():
    assert load_gate_file(None).restrict is False


def test_load_gate_file_empty_path_is_unrestricted():
    assert load_gate_file("").restrict is False


def test_parse_gate_invalid_raises():
    with pytest.raises(ValueError):
        parse_gate({"products": [1]})
    with pytest.raises(ValueError):
        parse_gate("nope")
