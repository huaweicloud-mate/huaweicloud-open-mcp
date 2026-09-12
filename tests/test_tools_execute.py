"""execute 工具纯函数单元测试（stub client）。"""

import json
import os

import pytest

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


# ---------- normalize_response：超限落盘（S12 层级 1） ----------

def test_normalize_response_spills_oversized_body(tmp_path):
    from mcp_openapi.spill import SpillConfig
    big = {"data": "x" * 250_000}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": big},
                                     spill=SpillConfig(dir=tmp_path),
                                     stem="ECS-ListServers")
    assert out["truncated"] is True
    assert out["body"]["truncated"] is True   # 预览形态保持现状
    info = out["spill"]
    assert info["path"].startswith(str(tmp_path))
    with open(info["path"], encoding="utf-8") as f:
        assert json.load(f) == big            # 文件为完整原始 body（截断前真值）
    assert "query_data" not in info["note"]


def test_normalize_response_oversized_str_spills_full(tmp_path):
    from mcp_openapi.spill import SpillConfig
    raw = "y" * 250_000
    out = execute.normalize_response({"status": 200, "headers": {}, "body": raw},
                                     spill=SpillConfig(dir=tmp_path), stem="x")
    assert out["body"] == raw[:execute.MAX_RESPONSE_CHARS]
    info = out["spill"]
    with open(info["path"], encoding="utf-8") as f:
        assert f.read() == raw


def test_normalize_response_error_oversized_also_spills(tmp_path):
    from mcp_openapi.spill import SpillConfig
    big = {"error_code": "E.1", "error_msg": "m" * 250_000}
    out = execute.normalize_response({"status": 400, "headers": {}, "body": big},
                                     spill=SpillConfig(dir=tmp_path), stem="x")
    assert out["error_code"] == "E.1"
    info = out["spill"]
    with open(info["path"], encoding="utf-8") as f:
        assert json.load(f) == big


def test_normalize_response_without_spill_config_unchanged():
    """未配置落盘：与现状逐字段一致（回归红线），不出现 spill 字段。"""
    big = {"data": "x" * 250_000}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": big})
    assert out["truncated"] is True
    assert "spill" not in out


def test_execute_api_passes_spill_with_stem(mini_detail, tmp_path):
    from mcp_openapi.spill import SpillConfig
    doc, path, method, op = _get_op(mini_detail)
    big = {"servers": [{"id": "s"} for _ in range(1)], "fill": "x" * 250_000}
    client = StubClient([{"status": 200, "headers": {}, "body": big}])
    out = execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1}, client=client,
                              spill=SpillConfig(dir=tmp_path),
                              credentials=Credentials(ak="AK", sk="SK",
                                                      project_id="proj123"))
    assert out["ok"] is True
    name = os.path.basename(out["spill"]["path"])
    assert name.startswith("ECS-ListServers-")
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == big


# ---------- normalize_response：错误体形状兼容（矩阵经单一接口断言） ----------

def test_normalize_response_error_body_always_present():
    # 错误分支 body 恒透出（真值源兜底），不再丢弃
    out = execute.normalize_response({"status": 400, "headers": {},
                                      "body": {"error_code": "E.400", "error_msg": "bad"}})
    assert out["body"] == {"error_code": "E.400", "error_msg": "bad"}


def test_normalize_response_iam_nested_error():
    # IAM v3/Keystone 嵌套形态：{"error": {"code", "message", "title"}}（数字 code → str）
    body = {"error": {"code": 403,
                      "message": "You are not authorized to perform the requested action.",
                      "title": "Forbidden"}}
    out = execute.normalize_response({"status": 403, "headers": {}, "body": body})
    assert out["status"] == 403
    assert out["error_code"] == "403"
    assert out["error_msg"] == "You are not authorized to perform the requested action."
    assert out["body"] == body


def test_normalize_response_nova_single_key_wrapper():
    # OpenStack nova 系单键包装：{"badRequest": {"code", "message"}}
    out = execute.normalize_response({"status": 400, "headers": {},
                                      "body": {"badRequest": {"code": 400,
                                                              "message": "Malformed request URL"}}})
    assert out["error_code"] == "400"
    assert out["error_msg"] == "Malformed request URL"


def test_normalize_response_errors_list():
    # SWR/Docker registry v2：{"errors": [{...}]} 取首元素
    out = execute.normalize_response({"status": 404, "headers": {},
                                      "body": {"errors": [{"code": "CRT.0001",
                                                           "message": "image not found"}]}})
    assert out["error_code"] == "CRT.0001"
    assert out["error_msg"] == "image not found"


def test_normalize_response_flat_code_message():
    # 平坦 code/message 形态
    out = execute.normalize_response({"status": 400, "headers": {},
                                      "body": {"code": "LMS.0001", "message": "quota exceeded"}})
    assert out["error_code"] == "LMS.0001"
    assert out["error_msg"] == "quota exceeded"


def test_normalize_response_msg_only():
    # 仅 msg 键：code 为 null
    out = execute.normalize_response({"status": 404, "headers": {},
                                      "body": {"message": "not found"}})
    assert out["error_code"] is None
    assert out["error_msg"] == "not found"


def test_normalize_response_bare_code_only():
    # 孤键 code（无 msg）：采纳 code，msg 为 null，body 兜底
    out = execute.normalize_response({"status": 403, "headers": {}, "body": {"code": 403}})
    assert out["error_code"] == "403"
    assert out["error_msg"] is None


def test_normalize_response_error_string_direct():
    # {"error": "<str>"} 字符串直取作 msg
    out = execute.normalize_response({"status": 500, "headers": {},
                                      "body": {"error": "internal failure"}})
    assert out["error_code"] is None
    assert out["error_msg"] == "internal failure"


def test_normalize_response_unknown_shape_nulls_with_body():
    # 未知 dict 形状：双 null，body 原始体兜底
    body = {"foo": {"bar": 1}}
    out = execute.normalize_response({"status": 418, "headers": {}, "body": body})
    assert out["error_code"] is None
    assert out["error_msg"] is None
    assert out["body"] == body


def test_normalize_response_non_scalar_code_rejected():
    # code/msg 值非标量（dict/bool/空串）不采纳，落到下层形状
    out = execute.normalize_response({"status": 400, "headers": {},
                                      "body": {"code": {"nested": 1}, "message": True, "error": 0}})
    assert out["error_code"] is None
    assert out["error_msg"] is None


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


# ---------- 服务方言：无 body 请求默认 Content-Type（_DEFAULT_JSON_CT_PRODUCTS） ----------

# 独立真值：2026-09 全量探测矩阵（219 产品/397 探针），无 Content-Type 的 GET 被
# 这些服务的头校验拒绝（MRS/LTS/UGO 415、GES/AAD/MSGSMS/WAF 400），加 CT 后越过。
DIALECT_PRODUCTS = ["MRS", "LTS", "GES", "AAD", "MSGSMS", "WAF", "UGO"]


def test_dialect_roster_matches_probe_matrix():
    """roster 与探测矩阵一致，防意外漂移（新增/删除产品须同步本清单）。"""
    assert execute._DEFAULT_JSON_CT_PRODUCTS == frozenset(DIALECT_PRODUCTS)


@pytest.mark.parametrize("product", DIALECT_PRODUCTS)
def test_execute_api_dialect_product_defaults_content_type(mini_detail, product):
    """方言产品无 body GET 自动携带默认 Content-Type（网关转调缺头致 415/400 的修复）。"""
    doc, path, method, op = _get_op(mini_detail)
    client = StubClient()
    execute.execute_api(doc, path, method, op, product, "ListServers", "cn-north-4",
                        {}, client=client,
                        credentials=Credentials(ak="AK", sk="SK", project_id="proj123"))
    headers = client.calls[0][5]
    assert headers["Content-Type"] == "application/json"


def test_execute_api_non_dialect_product_no_default_content_type(mini_detail):
    """非方言产品：无 body GET 不携带 Content-Type（191 个无 CT 即 200 的产品回归红线）。"""
    doc, path, method, op = _get_op(mini_detail)
    client = StubClient()
    execute.execute_api(doc, path, method, op, "ECS", "ListServers", "cn-north-4",
                        {}, client=client,
                        credentials=Credentials(ak="AK", sk="SK", project_id="proj123"))
    headers = client.calls[0][5]
    assert "Content-Type" not in headers


def test_execute_api_dialect_product_explicit_content_type_wins(mini_detail):
    """显式传入的 Content-Type 不被默认值覆盖（setdefault 语义）。"""
    doc, path, method, op = _get_op(mini_detail)
    op = dict(op)
    op["parameters"] = list(op.get("parameters") or []) + [
        {"name": "Content-Type", "in": "header", "type": "string"}]
    client = StubClient()
    execute.execute_api(doc, path, method, op, "MRS", "ListServers", "cn-north-4",
                        {"Content-Type": "text/plain"}, client=client,
                        credentials=Credentials(ak="AK", sk="SK", project_id="proj123"))
    headers = client.calls[0][5]
    assert headers["Content-Type"] == "text/plain"


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
