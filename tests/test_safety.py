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


def test_parse_server_prefix_rejected():
    """server: 语法已随 discover 模式移除：旧规则行启动即 fail-fast，不静默失效。"""
    with pytest.raises(ValueError, match="server:"):
        policy.parse_policy(["server:srv1=allow"])
    with pytest.raises(ValueError, match="server:"):
        policy.parse_policy(["*=deny", "server:srv1:list*=allow"])


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


# ---------- match_first（S2：first-match 命中规则对象） ----------

def test_match_first_returns_first_matching_rule():
    rules = policy.parse_policy(["ECS:*Show*=allow", "*=deny"])
    hit = policy.match_first(rules, "ECS", "ShowServers")
    assert hit is rules[0]
    miss = policy.match_first(rules, "ECS", "DeleteServers")
    assert miss is rules[1]


def test_match_first_no_match_returns_none():
    rules = policy.parse_policy(["ECS:List*=allow"])
    assert policy.match_first(rules, "ECS", "CreateServers") is None


def test_match_first_case_insensitive():
    rules = policy.parse_policy(["ecs:list*=allow"])
    hit = policy.match_first(rules, "ECS", "ListServers")
    assert hit is rules[0]
    assert hit.allow is True


# ---------- simulate_grant：readonly 档边际增益判定（修订方案 D7'） ----------

def _rules(lines):
    return policy.parse_policy(lines)


def test_simulate_grant_true_when_candidate_unlocks():
    """候选规则叠加后放行 → 有增益（overlay 前置穿透语义）。"""
    rules = _rules(["ECS:DeleteServers=deny", "*=deny"])
    assert policy.simulate_grant(
        rules, ["ECS:List*=allow", "ECS:Show*=allow"], "ECS", "ListServers") is True


def test_simulate_grant_true_when_candidate_pierces_deny():
    """候选 allow 前置可穿透文件 deny——对 readonly 档是已披露取舍
    （确认后可越过点名拒绝层，如 RDS:CheckInstanceForUpgrade）。"""
    rules = _rules(["RDS:CheckInstanceForUpgrade=deny", "*=deny"])
    assert policy.simulate_grant(
        rules, ["RDS:Check*=allow"], "RDS", "CheckInstanceForUpgrade") is True


def test_simulate_grant_false_when_no_candidate_matches():
    """普通写不匹配任何只读动词 → 零增益（确认循环陷阱的机械判据）。"""
    rules = _rules(["*=deny"])
    assert policy.simulate_grant(
        rules, policy.readonly_grant_rules("ECS"), "ECS", "DeleteServers") is False


def test_simulate_grant_fail_closed_on_bad_lines():
    """候选行解析失败按零增益处理（fail-closed）。"""
    rules = _rules(["*=deny"])
    assert policy.simulate_grant(rules, ["不是规则"], "ECS", "ListServers") is False


def test_simulate_grant_default_posture_zero_gain_for_all_denied():
    """缺省姿态（随包文件）下 readonly 档对任意被拒接口零增益：
    文件全局动词行是 readonly 档的超集（F2 根因的回归钉）。"""
    import json as _json

    from common.paths import project_root
    entries = _json.loads((project_root() / "configs" / "safety-policy.json")
                          .read_text(encoding="utf-8"))
    rules = policy.parse_policy(entries)
    assert policy.simulate_grant(
        rules, policy.readonly_grant_rules("ECS"), "ECS", "DeleteServers") is False
    assert policy.simulate_grant(
        rules, policy.readonly_grant_rules("ECS"), "ECS", "CreateServers") is False
