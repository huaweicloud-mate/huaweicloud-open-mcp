"""execute 工具纯函数单元测试（stub client）。"""

from common.auth import Credentials
from common.types import ClientResponse
from mcp_openapi import execute


class StubClient:
    def __init__(self, responses: list[ClientResponse] | None = None):
        self.responses = responses or []
        self.calls: list[tuple] = []

    def request(self, method, host, path, query=None, body=None, headers=None) -> ClientResponse:
        self.calls.append((method, host, path, query, body, headers))
        return self.responses.pop(0) if self.responses else {"status": 200, "body": {}}


def _get_op(mini_detail, key="ECS::ListServers"):
    from apie import convert_openapi2 as conv
    from apie.live_fallback import _find_api_in_doc
    doc = conv.convert_api(mini_detail["apis"][key])
    path, method, op = _find_api_in_doc(doc, key.split("::")[-1])
    return doc, path, method, op


def _policy(*lines):
    from safety import policy
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
    out = execute.normalize_response({"status": 200, "headers": {}, "body": {"servers": []}})
    assert out["status"] == 200
    assert out["body"] == {"servers": []}


def test_normalize_response_error_json():
    out = execute.normalize_response({"status": 400, "headers": {},
                                      "body": {"error_code": "E.400", "error_msg": "bad"}})
    assert out["status"] == 400
    assert out["error_code"] == "E.400"
    assert out["error_msg"] == "bad"


def test_normalize_response_error_non_json():
    out = execute.normalize_response({"status": 502, "headers": {}, "body": "<html>bad gateway</html>"})
    assert out["status"] == 502
    assert out["error_msg"]


def test_normalize_response_truncates_oversized():
    big = {"data": "x" * 200_000}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": big})
    assert out["truncated"] is True


# ---------- execute_api ----------

def test_execute_missing_doc_host_returns_error(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    doc.pop("host", None)
    client = StubClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    out = execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1}, client=client, credentials=cred)
    assert out["ok"] is False
    assert "host" in out["reason"]


def test_execute_allow_calls_client(mini_detail):
    doc, path, method, op = _get_op(mini_detail)
    client = StubClient([{"status": 200, "headers": {}, "body": {"count": 0}}])
    out = execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1}, client=client,
                              credentials=Credentials(ak="AK", sk="SK", project_id="proj123"))
    assert out["ok"] is True
    assert out["status"] == 200
    assert client.calls[0][0] == "GET"
