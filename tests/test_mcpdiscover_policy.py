"""safety policy server 规则单测（S7b）：evaluate_server / check_server / 解析。"""

import pytest

from safety import policy

# ------------------------------------------------------------------ 解析

def test_parse_server_connect_rule():
    rules = policy.parse_policy(["server:@my/srv=allow"])
    assert len(rules) == 1
    r = rules[0]
    assert r.kind == "server"
    assert r.product == "@my/srv"
    assert r.api_pattern == "*"
    assert r.allow is True


def test_parse_server_tool_rule():
    rules = policy.parse_policy(["server:@my/srv:list*=allow"])
    r = rules[0]
    assert r.kind == "server"
    assert r.product == "@my/srv"
    assert r.api_pattern == "list*"
    assert r.allow is True


def test_parse_server_with_spaces():
    rules = policy.parse_policy([" server:@my/srv : list* = allow "])
    r = rules[0]
    assert r.kind == "server"
    assert r.product == "@my/srv"
    assert r.api_pattern == "list*"


def test_parse_server_deny():
    rules = policy.parse_policy(["server:@my/srv:write=deny",
                                 "server:@my/srv:*=allow"])
    assert rules[0].allow is False
    assert rules[1].allow is True


def test_parse_mixed_product_and_server():
    rules = policy.parse_policy([
        "ECS:*=allow",
        "server:@ecs:*=allow",
        "*=deny",
    ])
    assert len(rules) == 3
    kinds = [r.kind for r in rules]
    assert kinds == ["product", "server", "product"]


# ------------------------------------------------------------------ evaluate_server

def test_evaluate_connect_allow():
    rules = policy.parse_policy(["server:@ecs=allow"])
    assert policy.evaluate_server(rules, "@ecs") is True


def test_evaluate_connect_deny():
    rules = policy.parse_policy(["server:@ecs=deny"])
    assert policy.evaluate_server(rules, "@ecs") is False


def test_evaluate_connect_default_deny():
    """无 connect 级规则时，连接默认拒绝。"""
    rules = policy.parse_policy(["server:@ecs:*list*=allow"])
    assert policy.evaluate_server(rules, "@ecs") is False


def test_evaluate_connect_unmatched_server():
    rules = policy.parse_policy(["server:@ecs=allow"])
    assert policy.evaluate_server(rules, "@other") is False


def test_evaluate_tool_allow():
    rules = policy.parse_policy(["server:@ecs:list*=allow"])
    assert policy.evaluate_server(rules, "@ecs", "list_servers") is True
    assert policy.evaluate_server(rules, "@ecs", "list_all") is True


def test_evaluate_tool_deny():
    rules = policy.parse_policy(["server:@ecs:write*=deny",
                                 "server:@ecs:*=allow"])
    assert policy.evaluate_server(rules, "@ecs", "write_data") is False
    assert policy.evaluate_server(rules, "@ecs", "read_data") is True


def test_evaluate_first_match_wins_server():
    rules = policy.parse_policy([
        "server:@ecs:list*=allow",
        "server:@ecs:list_restricted=deny",
    ])
    assert policy.evaluate_server(rules, "@ecs", "list_restricted") is True  # first match


def test_evaluate_product_rules_ignored_in_server_context():
    rules = policy.parse_policy([
        "ECS:*=allow",
        "server:@ecs=deny",
    ])
    # evaluate_server only considers kind="server" rules
    assert policy.evaluate_server(rules, "@ecs") is False


def test_evaluate_server_wildcard():
    rules = policy.parse_policy(["server:*:list*=allow"])
    assert policy.evaluate_server(rules, "@any", "list_things") is True


# ------------------------------------------------------------------ check_server

def test_check_server_none_rules():
    assert policy.check_server(None, "@ecs") == (
        "safety policy 未配置，discover 连接与调用全部拒绝"
    )


def test_check_server_allow():
    rules = policy.parse_policy(["server:@ecs=allow"])
    assert policy.check_server(rules, "@ecs") is None


def test_check_server_deny_connect():
    rules = policy.parse_policy(["server:@ecs=deny"])
    assert policy.check_server(rules, "@ecs") == "safety policy 拒绝连接 @ecs"


def test_check_server_deny_tool():
    rules = policy.parse_policy(["server:@ecs:write=deny"])
    assert policy.check_server(rules, "@ecs", "write") == "safety policy 拒绝调用 @ecs:write"


def test_parse_invalid_server_action():
    with pytest.raises(ValueError, match="allow/deny"):
        policy.parse_policy(["server:@ecs=maybe"])
