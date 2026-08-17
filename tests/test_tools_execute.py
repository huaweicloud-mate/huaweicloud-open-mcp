"""execute 工具纯函数单元测试（stub client）。"""


from huaweicloud_mcp.auth.credentials import Credentials
from huaweicloud_mcp.tools import execute


class StubClient:
    def __init__(self, responses=None, mode="real"):
        self.responses = responses or []
        self.calls = []
        self.mode = mode

    def request(self, method, host, path, query=None, body=None, headers=None):
        self.calls.append((method, host, path, query, body, headers))
        return self.responses.pop(0) if self.responses else {"status": 200, "body": {}}

    def mock_request(self, product, api_name, region, status_code=200, number=1):
        self.calls.append(("mock", product, api_name, region, status_code, number))
        return {"status": status_code, "body": {"mock": True, "count": number}}


def _get_op(mini_detail, key="ECS::ListServers"):
    from huaweicloud_mcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"][key])
    from huaweicloud_mcp.tools import metadata
    path, method, op = metadata.find_api_in_doc(doc, key.split("::")[-1])
    return doc, path, method, op


def _policy(*lines):
    from huaweicloud_mcp.safety import policy
    return policy.parse_policy(list(lines))


CRED = Credentials(ak="AK", sk="SK", project_id="proj123")


# ---------- build_request ----------

def test_build_request_fills_path_and_query(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    filled, query, body, headers, err = execute.build_request(op, path, {"limit": 10}, CRED)
    assert err is None
    assert filled == "/v1/proj123/cloudservers"
    assert query == {"limit": 10}
    assert body is None


def test_build_request_missing_path_param(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    filled, query, body, headers, err = execute.build_request(op, path, {}, Credentials(ak="AK", sk="SK"))
    assert err is not None
    assert "project_id" in err


def test_build_request_body(mini_detail):
    doc, path, method, op = _get_op(mini_detail, "RabbitMQ::BatchCreateOrDeleteRabbitMqTag")
    params = {"instance_id": "inst-1", "body": {"action": "create", "tags": []}}
    filled, query, body, headers, err = execute.build_request(op, path, params, CRED)
    assert err is None
    assert filled == "/v2/proj123/rabbitmq/inst-1/tags/action"
    assert body == {"action": "create", "tags": []}
    assert headers["Content-Type"] == "application/json"
    assert query == {}


# ---------- normalize_response ----------

def test_normalize_response_ok_json():
    out = execute.normalize_response({"status": 200, "body": {"servers": []}})
    assert out["status"] == 200
    assert out["body"] == {"servers": []}


def test_normalize_response_error_json():
    out = execute.normalize_response({"status": 400, "body": {"error_code": "E.400", "error_msg": "bad"}})
    assert out["status"] == 400
    assert out["error_code"] == "E.400"
    assert out["error_msg"] == "bad"


def test_normalize_response_error_non_json():
    out = execute.normalize_response({"status": 502, "body": "<html>bad gateway</html>"})
    assert out["status"] == 502
    assert out["error_msg"]


def test_normalize_response_truncates_oversized():
    big = {"data": "x" * 200_000}
    out = execute.normalize_response({"status": 200, "body": big})
    assert out["truncated"] is True


# ---------- execute_api ----------

def test_execute_refuses_without_policy(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    client = StubClient()
    out = execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1}, policy_rules=None, client=client, credentials=CRED)
    assert out["ok"] is False
    assert client.calls == []


def test_execute_deny_by_policy(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    client = StubClient()
    rules = _policy("ECS:*Show*=allow", "*=deny")
    out = execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1}, policy_rules=rules, client=client, credentials=CRED)
    assert out["ok"] is False
    assert client.calls == []


def test_execute_allow_calls_client(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    client = StubClient([{"status": 200, "body": {"count": 0}}])
    rules = _policy("ECS:*=allow")
    out = execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1}, policy_rules=rules, client=client, credentials=CRED)
    assert out["ok"] is True
    assert out["status"] == 200
    assert client.calls[0][0] == "GET"


def test_execute_mock_mode_routes(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    client = StubClient(mode="mock")
    rules = _policy("ECS:*=allow")
    out = execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1}, policy_rules=rules, client=client, credentials=CRED, mock=True)
    assert out["ok"] is True
    call = client.calls[0]
    assert call[0] == "mock"
    assert call[1] == "ECS"
    assert call[2] == "ListServers"
