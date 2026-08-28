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


# ---------- validate_params（OpenAPI 元数据校验，policy 接缝） ----------

def _op(*params):
    return {"operationId": "X", "parameters": list(params)}


PATH_P = {"name": "project_id", "in": "path", "type": "string", "required": True}


def test_validate_params_ok_passthrough():
    op = _op(PATH_P, {"name": "limit", "in": "query", "type": "integer"})
    assert execute.validate_params({}, "/v1/{project_id}/x", op,
                                   {"project_id": "p", "limit": 5}, None) is None


def test_validate_params_path_not_checked():
    """路径校验归 real lane（build_request）：mock URL 无 path，validate_params 不查。"""
    assert execute.validate_params({}, "/v1/{project_id}/x", _op(PATH_P), {}, None) is None


def test_validate_params_path_filled_by_credentials():
    assert execute.validate_params({}, "/v1/{project_id}/x", _op(PATH_P), {},
                                   Credentials(ak="A", sk="S", project_id="proj123")) is None


def test_validate_params_query_required_missing():
    op = _op({"name": "status", "in": "query", "type": "string", "required": True})
    err = execute.validate_params({}, "/x", op, {}, None)
    assert err is not None and "缺少必填" in err and "status" in err


def test_validate_params_query_type_strict():
    op = _op({"name": "limit", "in": "query", "type": "integer"})
    assert execute.validate_params({}, "/x", op, {"limit": 100}, None) is None
    err = execute.validate_params({}, "/x", op, {"limit": "100"}, None)
    assert err is not None and "integer" in err and "100" in err
    # bool 陷阱：True 不是合法 integer
    err = execute.validate_params({}, "/x", op, {"limit": True}, None)
    assert err is not None and "integer" in err


def test_validate_params_query_number_boolean_string():
    op = _op({"name": "w", "in": "query", "type": "number"},
             {"name": "dry", "in": "query", "type": "boolean"},
             {"name": "name", "in": "query", "type": "string"})
    assert execute.validate_params({}, "/x", op,
                                   {"w": 1.5, "dry": True, "name": "vm"}, None) is None
    assert execute.validate_params({}, "/x", op, {"w": True}, None) is not None
    assert execute.validate_params({}, "/x", op, {"dry": "true"}, None) is not None
    assert execute.validate_params({}, "/x", op, {"name": 123}, None) is not None


def test_validate_params_query_enum():
    op = _op({"name": "status", "in": "query", "type": "string",
              "enum": ["ACTIVE", "SHUTOFF"]})
    assert execute.validate_params({}, "/x", op, {"status": "ACTIVE"}, None) is None
    err = execute.validate_params({}, "/x", op, {"status": "BAD"}, None)
    assert err is not None and "ACTIVE" in err and "BAD" in err


def test_validate_params_control_keys_and_undeclared_lenient():
    op = _op({"name": "limit", "in": "query", "type": "integer"})
    assert execute.validate_params({}, "/x", op,
                                   {"_status_code": 400, "foo": "bar"}, None) is None


def test_validate_params_header_required_only():
    op = _op({"name": "X-Trace", "in": "header", "type": "string", "required": True})
    assert execute.validate_params({}, "/x", op, {"X-Trace": 123}, None) is None
    err = execute.validate_params({}, "/x", op, {}, None)
    assert err is not None and "缺少必填" in err


def test_validate_params_auth_header_skipped():
    """认证 header（X-Auth-Token/X-Security-Token/Authorization）由签名层自动注入，跳过必填检查。"""
    op = _op(
        {"name": "X-Auth-Token", "in": "header", "type": "string", "required": True},
        {"name": "X-Security-Token", "in": "header", "type": "string", "required": True},
        {"name": "Authorization", "in": "header", "type": "string", "required": True},
    )
    assert execute.validate_params({}, "/x", op, {}, None) is None


def test_validate_params_body_required_field_missing():
    doc = {"definitions": {"keypair": {
        "type": "object", "required": ["name"],
        "properties": {"name": {"type": "string"}, "key_file": {"type": "string"}}}}}
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/keypair"}})
    err = execute.validate_params(doc, "/x", op, {"body": {"key_file": "k"}}, None)
    assert err is not None and "name" in err
    assert execute.validate_params(doc, "/x", op, {"body": {"name": "my-key"}}, None) is None


def test_validate_params_body_type_error():
    op = _op({"name": "body", "in": "body",
              "schema": {"type": "object", "properties": {"count": {"type": "integer"}}}})
    err = execute.validate_params({}, "/x", op, {"body": {"count": "x"}}, None)
    assert err is not None and "body" in err


def test_validate_params_body_absent():
    op = _op({"name": "body", "in": "body", "schema": {"type": "object"}})
    assert execute.validate_params({}, "/x", op, {}, None) is None
    op_required = _op({"name": "body", "in": "body", "required": True,
                       "schema": {"type": "object"}})
    err = execute.validate_params({}, "/x", op_required, {}, None)
    assert err is not None and "缺少必填 body" in err
