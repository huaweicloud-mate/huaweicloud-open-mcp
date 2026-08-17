"""execute_api 集成测试：直连 API Explorer mock 端点。

mock 端点行为（实测确认）：
- 开放端点、无需凭证；
- HTTP 状态恒为 200；
- status_code=200 时返回与真实 API 同构的 mock 成功数据；
- status_code 为其它值时返回空 body。
"""

from huaweicloud_mcp.safety import policy
from huaweicloud_mcp.signer.client import HttpClient
from huaweicloud_mcp.tools import execute
from huaweicloud_mcp.auth.credentials import Credentials

CRED = Credentials(ak="AK", sk="SK", project_id="pid")
RULES_ALLOW_ECS = policy.parse_policy(["ECS:*=allow", "*=deny"])


def _run(api_name, params=None, rules=RULES_ALLOW_ECS):
    client = HttpClient(mock=True)
    return execute.execute_api(
        doc={"host": "ecs.cn-north-4.myhuaweicloud.com"},
        path="/v1/{project_id}/cloudservers",
        method="get",
        op={"operationId": api_name},
        product="ECS",
        api_name=api_name,
        region="cn-north-4",
        params=params or {},
        policy_rules=rules,
        client=client,
        credentials=CRED,
        mock=True,
    )


def test_mock_list_servers_details():
    out = _run("ListServersDetails")
    assert out["ok"] is True
    assert out["mock"] is True
    assert out["status"] == 200
    body = out["body"]
    assert isinstance(body, dict)
    assert "count" in body
    assert "servers" in body


def test_mock_create_servers_returns_job_id():
    out = _run("CreateServers", params={}, rules=policy.parse_policy(["ECS:Create*=allow", "*=deny"]))
    assert out["ok"] is True
    body = out["body"]
    assert "job_id" in body


def test_mock_status_code_non_200_empty_body():
    out = _run("ListServersDetails", params={"_status_code": 400})
    assert out["ok"] is True
    assert out["status"] == 200
    assert out["body"] is None


def test_mock_deny_by_policy_no_http():
    client = HttpClient(mock=True)
    rules = policy.parse_policy(["ECS:*Show*=allow", "*=deny"])
    out = execute.execute_api(
        doc={"host": "x"}, path="/", method="get", op={"operationId": "ListServersDetails"},
        product="ECS", api_name="ListServersDetails", region="cn-north-4",
        params={}, policy_rules=rules, client=client, credentials=CRED, mock=True)
    assert out["ok"] is False
    assert "policy" in out["reason"]
