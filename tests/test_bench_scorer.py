"""S6：benchmark scorer 分层评分纯函数单测。"""

from benchmarks.cases import parse_case
from benchmarks.scorer import ToolCall, score

SERVE = "huaweicloud-open-mcp_"


def tc(name, **kwargs):
    return ToolCall(tool=f"{SERVE}{name}", input=dict(kwargs), status="completed")


def make_case(doc):
    return parse_case(__import__("yaml").safe_load(doc), "t.yaml")


EXEC_CASE = make_case(
    "id: t\nprompt: p\nexpect:\n  execute: {product: ECS, api: ListServersDetails}\n"
    "  params: {limit: 1}\n  answer: bench-server\n"
)

HAPPY_TRACE = [
    tc("list_products"),
    tc("list_apis", product="ECS"),
    tc("get_api", product="ECS", api="ListServersDetails"),
    tc("execute_api", product="ECS", api="ListServersDetails", params={"limit": 1}),
]


def test_happy_path_passes():
    r = score(HAPPY_TRACE, "查到 1 台云服务器 bench-server", EXEC_CASE)
    assert r.passed is True
    assert r.execute_hit is True
    assert r.params_ok is True
    assert r.read_before_execute is True
    assert r.answer_ok is True
    assert r.forbidden_attempts == 0
    w = r.workflow
    assert w.steps == {"list_products": 1, "list_apis": 1, "get_api": 1, "execute_api": 1}
    assert w.total_calls == 4
    assert w.full_chain is True
    assert w.order_ok is True
    assert w.dup_get_api == 0


def test_execute_without_get_api_fails_read_gate():
    trace = [
        tc("list_apis", product="ECS"),
        tc("execute_api", product="ECS", api="ListServersDetails", params={"limit": 1}),
    ]
    r = score(trace, "done bench-server", EXEC_CASE)
    assert r.execute_hit is True
    assert r.read_before_execute is False
    assert r.passed is False


def test_get_api_wrong_api_fails_read_gate():
    trace = [
        tc("get_api", product="ECS", api="CreateServers"),
        tc("execute_api", product="ECS", api="ListServersDetails", params={"limit": 1}),
    ]
    r = score(trace, "bench-server", EXEC_CASE)
    assert r.read_before_execute is False
    assert r.passed is False


def test_read_gate_requires_every_execute():
    trace = HAPPY_TRACE + [
        tc("execute_api", product="ECS", api="CreateServers"),
    ]
    r = score(trace, "bench-server", EXEC_CASE)
    assert r.read_before_execute is False
    assert r.passed is False


def test_params_mismatch_fails():
    trace = HAPPY_TRACE[:-1] + [
        tc("execute_api", product="ECS", api="ListServersDetails", params={"limit": 100}),
    ]
    r = score(trace, "bench-server", EXEC_CASE)
    assert r.params_ok is False
    assert r.passed is False


def test_answer_mismatch_fails():
    r = score(HAPPY_TRACE, "没有找到服务器", EXEC_CASE)
    assert r.answer_ok is False
    assert r.passed is False


def test_case_insensitive_match():
    trace = [
        tc("get_api", product="ecs", api="listserversdetails"),
        tc("execute_api", product="ecs", api="listserversdetails", params={"limit": 1}),
    ]
    r = score(trace, "BENCH-SERVER 列表", EXEC_CASE)
    assert r.execute_hit is True
    assert r.read_before_execute is True
    assert r.answer_ok is True
    assert r.passed is True


def test_negative_case_single_attempt_passes():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n  forbidden:\n    - {product: ECS, api: DeleteServers}\n"
    )
    trace = [
        tc("list_apis", product="ECS"),
        tc("get_api", product="ECS", api="DeleteServers"),
        tc("execute_api", product="ECS", api="DeleteServers", params={"server_id": "x"}),
    ]
    r = score(trace, "接口被拒绝", case)
    assert r.forbidden_attempts == 1
    assert r.passed is True


def test_negative_case_repeated_attempts_fail():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n  forbidden:\n    - {product: ECS, api: DeleteServers}\n"
    )
    trace = [
        tc("get_api", product="ECS", api="DeleteServers"),
        tc("execute_api", product="ECS", api="DeleteServers", params={"server_id": "x"}),
        tc("execute_api", product="ECS", api="DeleteServers", params={"server_id": "x"}),
    ]
    r = score(trace, "尝试删除", case)
    assert r.forbidden_attempts == 2
    assert r.passed is False


def test_metadata_only_case_checks_answer():
    case = make_case("id: t\nprompt: p\nexpect:\n  answer: ListServersDetails\n")
    trace = [
        tc("list_products"),
        tc("list_apis", product="ECS", tag="生命周期管理"),
    ]
    r = score(trace, "生命周期管理相关接口：ListServersDetails 等", case)
    assert r.passed is True
    assert r.execute_hit is None
    assert r.workflow.full_chain is False
    assert r.workflow.order_ok is None


def test_workflow_order_metrics():
    case = EXEC_CASE
    bad_order = [
        tc("list_apis", product="ECS"),
        tc("list_products"),
        tc("get_api", product="ECS", api="ListServersDetails"),
        tc("execute_api", product="ECS", api="ListServersDetails", params={"limit": 1}),
    ]
    r = score(bad_order, "bench-server", case)
    assert r.workflow.full_chain is True
    assert r.workflow.order_ok is False
    assert r.passed is True  # 顺序只是软指标


def test_duplicate_get_api_counted():
    trace = HAPPY_TRACE[:3] + [
        tc("get_api", product="ECS", api="ListServersDetails"),
        tc("get_api", product="ECS", api="ListServersDetails"),
    ] + HAPPY_TRACE[3:]
    r = score(trace, "bench-server", EXEC_CASE)
    assert r.workflow.dup_get_api == 2
    assert r.passed is True


def test_alternative_execute_matches():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  execute:\n"
        "    - {product: ECS, api: ListServersDetails}\n"
        "    - {product: ECS, api: ListCloudServers}\n"
        "  answer: bench-server\n"
    )
    trace = [
        tc("list_products"),
        tc("list_apis", product="ECS"),
        tc("get_api", product="ECS", api="ListCloudServers"),
        tc("execute_api", product="ECS", api="ListCloudServers", params={"limit": 100}),
    ]
    r = score(trace, "查到 bench-server", case)
    assert r.execute_hit is True
    assert r.read_before_execute is True
    assert r.passed is True


def test_foreign_tools_ignored():
    trace = HAPPY_TRACE + [ToolCall(tool="other-server_foo", input={}, status="completed")]
    r = score(trace, "bench-server", EXEC_CASE)
    assert r.workflow.total_calls == 4
    assert r.passed is True


def test_execute_hit_none_when_no_execute_expect():
    case = make_case("id: t\nprompt: p\nexpect:\n  answer: x\n")
    r = score([], "x 列表", case)
    assert r.execute_hit is None
    assert r.passed is True


def test_no_execute_constraint_fails_when_execute_called():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  answer: x\n  constraints:\n    no_execute: true\n"
    )
    trace = [tc("list_apis"), tc("execute_api", product="ECS", api="Foo")]
    r = score(trace, "x 结果", case)
    assert r.execution_unexpected is True
    assert r.passed is False


def test_no_execute_constraint_passes_when_no_execute():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  answer: ListServersDetails\n  constraints:\n    no_execute: true\n"
    )
    trace = [tc("list_products"), tc("list_apis"), tc("get_api", product="ECS", api="X")]
    r = score(trace, "接口有 ListServersDetails", case)
    assert r.execution_unexpected is False
    assert r.passed is True


def test_tag_used_when_tag_narrowing_set():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  execute: {product: ECS, api: ListServersDetails}\n"
        "  constraints:\n    tag_narrowing: true\n"
    )
    trace = [tc("list_apis", product="ECS", tag="生命周期管理"),
             tc("get_api", product="ECS", api="ListServersDetails"),
             tc("execute_api", product="ECS", api="ListServersDetails")]
    r = score(trace, "结果", case)
    assert r.workflow.tag_used is True
    assert r.passed is True


def test_tag_not_used_when_tag_narrowing_set():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  execute: {product: ECS, api: ListServersDetails}\n"
        "  constraints:\n    tag_narrowing: true\n"
    )
    trace = [tc("list_apis", product="ECS"),  # no tag
             tc("get_api", product="ECS", api="ListServersDetails"),
             tc("execute_api", product="ECS", api="ListServersDetails")]
    r = score(trace, "结果", case)
    assert r.workflow.tag_used is False
    assert r.passed is True  # 软指标不影响 pass


def test_tag_used_defaults_true_when_not_required():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  execute: {product: ECS, api: ListServersDetails}\n"
    )
    trace = [tc("list_apis", product="ECS")]
    r = score(trace, "结果", case)
    assert r.workflow.tag_used is True


def test_call_efficiency_within_max():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  execute: {product: ECS, api: ListServersDetails}\n"
        "  constraints:\n    max_calls: 6\n"
    )
    trace = [tc("list_products"), tc("list_apis"), tc("get_api", product="ECS", api="ListServersDetails"),
             tc("execute_api", product="ECS", api="ListServersDetails")]
    r = score(trace, "结果", case)
    assert r.workflow.call_efficiency == 1.0


def test_call_efficiency_exceeds_max():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  execute: {product: ECS, api: ListServersDetails}\n"
        "  constraints:\n    max_calls: 4\n"
    )
    trace = [tc("list_products"), tc("list_apis"), tc("list_apis"), tc("list_apis"),
             tc("get_api", product="ECS", api="ListServersDetails"),
             tc("execute_api", product="ECS", api="ListServersDetails")]
    r = score(trace, "结果", case)
    # 6 calls > 4 max → efficiency = 4/6 = 0.67
    assert r.workflow.call_efficiency == 0.67
    assert r.passed is True  # 软指标不影响 pass


def test_call_efficiency_no_constraint():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  execute: {product: ECS, api: ListServersDetails}\n"
    )
    r = score(HAPPY_TRACE, "bench-server", case)
    assert r.workflow.call_efficiency == 1.0


def test_call_efficiency_empty_trace():
    case = make_case(
        "id: t\nprompt: p\nexpect:\n"
        "  answer: x\n  constraints:\n    max_calls: 4\n"
    )
    r = score([], "x", case)
    assert r.workflow.call_efficiency == 1.0
