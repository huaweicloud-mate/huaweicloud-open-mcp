"""safety policy 解析与匹配单元测试。"""

import pytest

from safety import policy


def test_parse_basic_rules():
    rules = policy.parse_policy(["ECS:*Show*=allow", "*=deny"])
    assert len(rules) == 2
    assert (rules[0].product, rules[0].api_pattern, rules[0].allow) == ("ECS", "*Show*", True)
    assert (rules[1].product, rules[1].api_pattern, rules[1].allow) == ("*", "*", False)


def test_parse_skips_comments_and_blanks():
    rules = policy.parse_policy(["# 注释", "", "  ", "ECS:*=allow"])
    assert len(rules) == 1


def test_parse_malformed_raises():
    with pytest.raises(ValueError):
        policy.parse_policy(["ECS:ListServers"])  # 缺 =
    with pytest.raises(ValueError):
        policy.parse_policy(["ECS:ListServers=maybe"])  # 非法 action
    with pytest.raises(ValueError):
        policy.parse_policy(["ListServers=allow"])  # 缺 product 前缀


def test_evaluate_allowlist():
    rules = policy.parse_policy(["ECS:*Show*=allow", "*=deny"])
    assert policy.evaluate(rules, "ECS", "ShowServerDetails") is True
    assert policy.evaluate(rules, "ECS", "DeleteServers") is False
    assert policy.evaluate(rules, "VPC", "ShowVpc") is False


def test_evaluate_first_match_wins():
    deny_first = policy.parse_policy(["ECS:*=deny", "ECS:*Show*=allow"])
    assert policy.evaluate(deny_first, "ECS", "ShowServers") is False
    allow_first = policy.parse_policy(["ECS:*Show*=allow", "ECS:*=deny"])
    assert policy.evaluate(allow_first, "ECS", "ShowServers") is True


def test_evaluate_no_match_default_deny():
    rules = policy.parse_policy(["ECS:List*=allow"])
    assert policy.evaluate(rules, "ECS", "CreateServers") is False
    assert policy.evaluate(rules, "VPC", "ListVpcs") is False


def test_evaluate_case_insensitive():
    rules = policy.parse_policy(["ecs:list*=allow"])
    assert policy.evaluate(rules, "ECS", "ListServers") is True
    assert policy.evaluate(rules, "Ecs", "listservers") is True


def test_evaluate_glob_product_and_api():
    rules = policy.parse_policy(["*:Get*Job*=allow"])
    assert policy.evaluate(rules, "ECS", "GetJobInfo") is True
    assert policy.evaluate(rules, "ECS", "ListJobs") is False


def test_load_policy_file_json_array(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('["ECS:*Show*=allow", "*=deny"]', encoding="utf-8")
    rules = policy.load_policy_file(str(p))
    assert policy.evaluate(rules, "ECS", "ShowServers") is True
    assert policy.evaluate(rules, "ECS", "DeleteServers") is False


def test_load_policy_file_text_lines(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("# 白名单\nECS:*Show*=allow\n*=deny\n", encoding="utf-8")
    rules = policy.load_policy_file(str(p))
    assert len(rules) == 2


# ---------- match_first / match_server_first（S2：first-match 命中规则对象） ----------

def test_match_first_returns_first_matching_rule():
    rules = policy.parse_policy(["ECS:*Show*=allow", "*=deny"])
    hit = policy.match_first(rules, "ECS", "ShowServers")
    assert hit is rules[0]
    miss = policy.match_first(rules, "ECS", "DeleteServers")
    assert miss is rules[1]


def test_match_first_no_match_returns_none():
    rules = policy.parse_policy(["ECS:List*=allow"])
    assert policy.match_first(rules, "ECS", "CreateServers") is None


def test_match_first_case_insensitive_and_ignores_server_rules():
    rules = policy.parse_policy(
        ["server:srv1=allow", "ecs:list*=allow"])
    hit = policy.match_first(rules, "ECS", "ListServers")
    assert hit is rules[1]
    assert hit.allow is True


def test_match_server_first_returns_matching_rule():
    rules = policy.parse_policy(
        ["server:srv1:list*=allow", "server:srv1=deny", "ECS:*=allow"])
    hit_tool = policy.match_server_first(rules, "SRV1", "listTools")
    assert hit_tool is rules[0]
    hit_connect = policy.match_server_first(rules, "srv1", None)
    assert hit_connect is rules[1]
    assert hit_connect.allow is False


def test_match_server_first_no_match_returns_none():
    rules = policy.parse_policy(["server:srv1:list*=allow"])
    assert policy.match_server_first(rules, "srv1", "callTool") is None
    assert policy.match_server_first(rules, "srv1", None) is None
    assert policy.match_server_first(rules, "other", "listTools") is None
