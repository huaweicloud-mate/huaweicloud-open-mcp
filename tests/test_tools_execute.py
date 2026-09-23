"""execute 工具纯函数单元测试（stub client）。"""

import json
import os

import pytest

from apie.api_location import ApiLocation
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
    doc = conv.convert_api(mini_detail["apis"][key])
    return ApiLocation.find(doc, key.split("::")[-1])


def _policy(*lines):
    from safety import policy
    return policy.parse_policy(list(lines))


CRED = Credentials(ak="AK", sk="SK", project_id="proj123")


# ---------- build_request ----------

def test_build_request_fills_path_and_query(mini_detail):
    loc = _get_op(mini_detail)
    filled, query, body, headers, err = execute.build_request(loc.op, loc.path, {"limit": 10}, CRED)
    assert err is None
    assert filled == "/v1/proj123/cloudservers"
    assert query == {"limit": 10}
    assert body is None


def test_build_request_missing_path_param(mini_detail):
    loc = _get_op(mini_detail)
    filled, query, body, headers, err = execute.build_request(loc.op, loc.path, {}, Credentials(ak="AK", sk="SK"))
    assert err is not None
    assert "project_id" in err


def test_build_request_body(mini_detail):
    loc = _get_op(mini_detail, "RabbitMQ::BatchCreateOrDeleteRabbitMqTag")
    params = {"instance_id": "inst-1", "body": {"action": "create", "tags": []}}
    filled, query, body, headers, err = execute.build_request(loc.op, loc.path, params, CRED)
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
    loc = _get_op(mini_detail)
    big = {"servers": [{"id": "s"} for _ in range(1)], "fill": "x" * 250_000}
    client = StubClient([{"status": 200, "headers": {}, "body": big}])
    out = execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1},
                              executor=execute.RealApiExecutor(
                                  client, Credentials(ak="AK", sk="SK",
                                                      project_id="proj123")),
                              spill=SpillConfig(dir=tmp_path))
    assert out["ok"] is True
    name = os.path.basename(out["spill"]["path"])
    assert name.startswith("ECS-ListServers-")
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == big


# ---------- normalize_response：二进制 body（bytes → 占位 + spill .bin） ----------

def _png_like(n: int = 64) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes((i * 7 + 0x80) & 0xFF for i in range(n))


def test_normalize_response_binary_body_placeholder_and_spill(tmp_path):
    import hashlib

    from mcp_openapi.spill import SpillConfig
    raw = _png_like()
    out = execute.normalize_response(
        {"status": 200, "headers": {"Content-Type": "image/png"}, "body": raw},
        spill=SpillConfig(dir=tmp_path), stem="X-Dl")
    assert out["truncated"] is True
    body = out["body"]
    assert body["binary"] is True
    assert body["size"] == len(raw)
    assert body["content_type"] == "image/png"
    assert body["sha256"] == hashlib.sha256(raw).hexdigest()  # 独立真值
    assert "落盘" in body["note"]
    info = out["spill"]
    assert info["format"] == "bin"
    assert info["path"].startswith(str(tmp_path))
    with open(info["path"], "rb") as f:
        assert f.read() == raw                 # disk == wire（逐位一致）


def test_normalize_response_binary_body_small_still_spills(tmp_path):
    # bytes 恒落盘（不设体积门槛）：小二进制不落盘即整段丢失
    from mcp_openapi.spill import SpillConfig
    raw = b"\x00\x01\x02\xff"
    out = execute.normalize_response({"status": 200, "headers": {}, "body": raw},
                                     spill=SpillConfig(dir=tmp_path), stem="x")
    with open(out["spill"]["path"], "rb") as f:
        assert f.read() == raw
    assert out["body"]["size"] == 4


def test_normalize_response_binary_body_without_spill_config():
    raw = _png_like(8)
    out = execute.normalize_response({"status": 200, "headers": {}, "body": raw})
    assert out["truncated"] is True
    assert "spill" not in out
    assert out["body"]["binary"] is True
    assert "不可得" in out["body"]["note"]    # 明示数据未保留，不再输出乱码


def test_normalize_response_binary_body_content_type_case_insensitive(tmp_path):
    from mcp_openapi.spill import SpillConfig
    raw = b"\xff\xfe\xfd"
    out = execute.normalize_response(
        {"status": 200, "headers": {"content-type": "application/zip"}, "body": raw},
        spill=SpillConfig(dir=tmp_path), stem="x")
    assert out["body"]["content_type"] == "application/zip"


def test_normalize_response_binary_error_body_hex_msg(tmp_path):
    from mcp_openapi.spill import SpillConfig
    raw = b"\x89PNG\x00\xff"
    out = execute.normalize_response(
        {"status": 500, "headers": {"Content-Type": "image/png"}, "body": raw},
        spill=SpillConfig(dir=tmp_path), stem="x")
    assert out["status"] == 500
    assert out["error_msg"].startswith("binary body")
    assert raw[:16].hex() in out["error_msg"]
    assert out["body"]["binary"] is True      # 错误分支同样占位+落盘
    with open(out["spill"]["path"], "rb") as f:
        assert f.read() == raw


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
    loc = _get_op(mini_detail)
    loc.doc.pop("host", None)
    client = StubClient()
    cred = Credentials(ak="AK", sk="SK", project_id="proj123")
    out = execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1},
                              executor=execute.RealApiExecutor(client, cred))
    assert out["ok"] is False
    assert "host" in out["reason"]


def test_execute_allow_calls_client(mini_detail):
    loc = _get_op(mini_detail)
    client = StubClient([{"status": 200, "headers": {}, "body": {"count": 0}}])
    out = execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                              {"limit": 1},
                              executor=execute.RealApiExecutor(
                                  client, Credentials(ak="AK", sk="SK",
                                                      project_id="proj123")))
    assert out["ok"] is True
    assert out["status"] == 200
    assert client.calls[0][0] == "GET"


# ---------- basePath 前缀（real lane 请求路径 = doc.basePath + 填充路径） ----------

# 独立真值：/v3/apis/detail 载荷 base_path 字段——17,963 接口全量统计，
# 488 个非根（APIG/VAS/CodeArtsInspector/OrgID/CloudRTC/KooPhone/AOM/IoTEdge/CampusGo），
# path 均不含该前缀（双前缀风险 0）；转换后 doc 携带标准 Swagger basePath。


def test_execute_applies_basepath_prefix(mini_detail):
    loc = _get_op(mini_detail)
    loc.doc["basePath"] = "/v2"
    client = StubClient()
    execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                        {"limit": 1},
                        executor=execute.RealApiExecutor(
                            client, Credentials(ak="AK", sk="SK",
                                                project_id="proj123")))
    assert client.calls[0][2] == "/v2/v1/proj123/cloudservers"


def test_execute_basepath_root_unchanged(mini_detail):
    loc = _get_op(mini_detail)
    loc.doc["basePath"] = "/"
    client = StubClient()
    execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                        {"limit": 1},
                        executor=execute.RealApiExecutor(
                            client, Credentials(ak="AK", sk="SK",
                                                project_id="proj123")))
    assert client.calls[0][2] == "/v1/proj123/cloudservers"


def test_execute_basepath_missing_unchanged(mini_detail):
    loc = _get_op(mini_detail)
    loc.doc.pop("basePath", None)
    client = StubClient()
    execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                        {"limit": 1},
                        executor=execute.RealApiExecutor(
                            client, Credentials(ak="AK", sk="SK",
                                                project_id="proj123")))
    assert client.calls[0][2] == "/v1/proj123/cloudservers"


def test_execute_basepath_trailing_slash_normalized(mini_detail):
    loc = _get_op(mini_detail)
    loc.doc["basePath"] = "/v2/"
    client = StubClient()
    execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                        {"limit": 1},
                        executor=execute.RealApiExecutor(
                            client, Credentials(ak="AK", sk="SK",
                                                project_id="proj123")))
    assert client.calls[0][2] == "/v2/v1/proj123/cloudservers"


# ---------- 全局默认 Content-Type（real lane 无条件 setdefault，显式传入不覆盖） ----------

# 独立真值：官方 SDK 全局携带 CT 且从不注入 body 为既成先例；SDK-HMAC-SHA256 签名
# 排除 content-type，加头对签名输出逐字节不变。原「方言产品名单 + 写方法集」两层
# 口径因新方言产品持续出现（打地鼠）于 2026-09 收敛为全局默认，GET 红线随之撤销。

# 手写 mini op（不经 fixture）：元数据仅 header/path 参数、无 body 的写请求，
# 即 RDS StartupInstance（POST /v3/{project_id}/instances/{instance_id}/action/startup）形态。
_BODYLESS_WRITE_OP = {
    "parameters": [
        {"name": "project_id", "in": "path", "type": "string", "required": True},
    ],
}
_BODYLESS_WRITE_DOC = {"host": "ecs.cn-north-4.myhuaweicloud.com", "basePath": "/"}


def test_execute_api_bodyless_get_defaults_content_type(mini_detail):
    """任意产品无 body GET 默认携带 Content-Type（原「非方言不带」红线翻转，全局默认）。"""
    loc = _get_op(mini_detail)
    client = StubClient()
    execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                        {},
                        executor=execute.RealApiExecutor(
                            client, Credentials(ak="AK", sk="SK",
                                                project_id="proj123")))
    headers = client.calls[0][5]
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_execute_api_bodyless_defaults_content_type(method):
    """无 body 请求全方法默认携带 Content-Type（setdefault，不注入 body）。"""
    client = StubClient()
    execute.execute_api(
        ApiLocation(_BODYLESS_WRITE_DOC, "/v1/{project_id}/cloudservers/action",
                    method, _BODYLESS_WRITE_OP),
        "ECS", "StartupInstance", "cn-north-4", {},
        executor=execute.RealApiExecutor(client, CRED))
    method_, host_, path_, query, body, headers = client.calls[0]
    assert headers["Content-Type"] == "application/json"
    assert body is None  # 只补头，不注入空 JSON 体


def test_execute_api_explicit_content_type_wins(mini_detail):
    """显式传入的 Content-Type 不被默认值覆盖（setdefault 语义）。"""
    loc = _get_op(mini_detail)
    op = dict(loc.op)
    op["parameters"] = list(op.get("parameters") or []) + [
        {"name": "Content-Type", "in": "header", "type": "string"}]
    loc = ApiLocation(loc.doc, loc.path, loc.method, op)
    client = StubClient()
    execute.execute_api(loc, "ECS", "ListServers", "cn-north-4",
                        {"Content-Type": "text/plain"},
                        executor=execute.RealApiExecutor(
                            client, Credentials(ak="AK", sk="SK",
                                                project_id="proj123")))
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


def test_validate_params_auth_header_skipped_case_insensitive():
    """认证头匹配大小写不敏感：元数据 casing 混乱（x-auth-token 小写 869 处/
    X-Auth-token 等混合 19 处），漏匹配曾误拒 698 个 API op。"""
    op = _op(
        {"name": "x-auth-token", "in": "header", "type": "string", "required": True},
        {"name": "X-Auth-token", "in": "header", "type": "string", "required": True},
        {"name": "x-Auth-Token", "in": "header", "type": "string", "required": True},
        {"name": "authorization", "in": "header", "type": "string", "required": True},
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


def test_validate_params_body_allof_not_enforced():
    """allOf 组合约束不强制（Explorer 组合不可靠，2026-09 实测 35 个官方示例被
    组合约束误拒）；直接声明的约束仍生效。"""
    doc = {"definitions": {
        "Base": {"type": "object", "required": ["name"],
                 "properties": {"name": {"type": "string"}}},
        "Req": {"allOf": [{"$ref": "#/definitions/Base"},
                          {"type": "object", "properties": {"x": {"type": "string"}}}]},
    }}
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/Req"}})
    # allOf 的 required 不强制：缺 name 也放行
    assert execute.validate_params(doc, "/x", op, {"body": {}}, None) is None
    # 直接声明的约束仍生效
    op2 = _op({"name": "body", "in": "body", "required": True,
               "schema": {"type": "object", "properties": {"count": {"type": "integer"}}}})
    err = execute.validate_params({}, "/x", op2, {"body": {"count": "x"}}, None)
    assert err is not None


def _bad_pattern_doc():
    return {"definitions": {"keypair": {
        "type": "object", "required": ["key"],
        "properties": {"key": {"type": "string", "pattern": "^([\\p{L}]*)$"}}}}}


def test_validate_params_uncompilable_pattern_does_not_raise():
    """PCRE 方言 pattern（\\p{L}）Python re 编译期抛错：校验层忽略该约束，不崩。"""
    doc = _bad_pattern_doc()
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/keypair"}})
    assert execute.validate_params(doc, "/x", op, {"body": {"key": "中文"}}, None) is None


def test_validate_params_uncompilable_pattern_does_not_mask_other_errors():
    """只剥离 pattern，其余约束（type/required）照常——不能宽 catch 吞掉真实错误。"""
    doc = _bad_pattern_doc()
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/keypair"}})
    err = execute.validate_params(doc, "/x", op, {"body": {"key": 123}}, None)
    assert err is not None and "string" in err
    err2 = execute.validate_params(doc, "/x", op, {"body": {}}, None)
    assert err2 is not None and "key" in err2


def test_validate_params_compilable_pattern_still_enforced():
    doc = {"definitions": {"X": {"type": "object", "properties": {
        "k": {"type": "string", "pattern": "^[a-z]+$"}}}}}
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/X"}})
    assert execute.validate_params(doc, "/x", op, {"body": {"k": "abc"}}, None) is None
    err = execute.validate_params(doc, "/x", op, {"body": {"k": "ABC"}}, None)
    assert err is not None


def test_validate_params_does_not_mutate_doc_pattern():
    """校验视图 copy-on-write：调用后原始 doc 仍保留 pattern（元数据真值不丢）。"""
    doc = _bad_pattern_doc()
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/keypair"}})
    execute.validate_params(doc, "/x", op, {"body": {"key": "x"}}, None)
    assert doc["definitions"]["keypair"]["properties"]["key"]["pattern"] == "^([\\p{L}]*)$"


def test_validation_view_strips_bad_patterns():
    bad = "^([\\p{L}]*)$"
    bad2 = "^[\\w-.]+$"
    out = execute._validation_view({
        "type": "object", "pattern": bad,
        "properties": {"a": {"pattern": bad, "description": "d"},
                       "b": {"pattern": "^[a-z]+$"}},
        "items": {"pattern": bad2},
        "additionalProperties": {"pattern": bad},
        "allOf": [{"pattern": bad}],
    })
    assert "pattern" not in out                       # 顶层坏 pattern 删除
    assert "pattern" not in out["properties"]["a"]    # 嵌套坏 pattern 删除
    assert out["properties"]["a"]["description"] == "d"  # 兄弟键保留
    assert out["properties"]["b"]["pattern"] == "^[a-z]+$"  # 好 pattern 保留
    assert "pattern" not in out["items"]
    assert "pattern" not in out["additionalProperties"]
    assert "allOf" not in out                         # allOf 亦剥离
    # copy-on-write：入参未变
    src = {"pattern": bad}
    execute._validation_view(src)
    assert src["pattern"] == bad
    # 幂等
    assert execute._validation_view(out) == out
    # 非字符串 pattern 一并剥离（Draft4 会 TypeError）
    assert execute._validation_view({"pattern": 5}) == {}
    # 数据/扩展载荷不递归：enum 成员不得被改写（防误拒）
    data_doc = {"enum": [{"pattern": bad}], "example": {"pattern": bad},
                "x-note": {"pattern": bad}}
    assert execute._validation_view(data_doc) == data_doc


def test_validate_params_uncompilable_pattern_in_allof_member():
    inner = {"type": "object", "properties": {
        "k": {"type": "string", "pattern": "^([\\p{L}]*)$"}}}
    doc = {"definitions": {"X": {"allOf": [inner]}}}
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/X"}})
    assert execute.validate_params(doc, "/x", op, {"body": {"k": "中文"}}, None) is None


def test_validate_params_enum_of_dicts_not_mutated():
    """校验视图不进入 enum 数据：枚举成员含 pattern 键时不得被改写（防误拒）。"""
    member = {"pattern": "^([\\p{L}]*)$"}
    doc = {"definitions": {"X": {"type": "object", "properties": {
        "kind": {"enum": [member]}}}}}
    op = _op({"name": "body", "in": "body", "required": True,
              "schema": {"$ref": "#/definitions/X"}})
    assert execute.validate_params(doc, "/x", op, {"body": {"kind": member}}, None) is None


_PATTERN_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures", "pattern_dialect_raw.json")


def test_pattern_dialect_fixture_no_crash_and_display_preserved():
    """真实语料形状 fixture（CI 可达）：PCRE 方言 pattern 校验不崩，展示层保留 pattern。"""
    from apie import convert_openapi2 as conv
    from apie.api_location import ApiLocation
    from apie.metadata import format_api_detail

    with open(_PATTERN_FIXTURE, encoding="utf-8") as f:
        raw = json.load(f)
    doc = conv.convert_api(raw)
    path = "/v1/{project_id}/pools/{pool_name}/tags/create"
    op = doc["paths"][path]["post"]
    body = {"tags": [{"key": "test", "value": "service-gpu"}]}
    assert execute.validate_params(doc, path, op, {"body": body}, None) is None
    out = format_api_detail(ApiLocation(doc, path, "post", op), "ModelArts")
    defs = out["definitions"]["BatchCreatePoolTagsRequestBody"]
    assert "\\p{L}" in defs["properties"]["tags"]["items"]["properties"]["key"]["pattern"]


def test_validate_params_body_absent():
    op = _op({"name": "body", "in": "body", "schema": {"type": "object"}})
    assert execute.validate_params({}, "/x", op, {}, None) is None
    op_required = _op({"name": "body", "in": "body", "required": True,
                       "schema": {"type": "object"}})
    err = execute.validate_params({}, "/x", op_required, {}, None)
    assert err is not None and "缺少必填 body" in err


# ---------- normalize_response：_jsonpath 投影（extract 接缝） ----------

def _extract(spec):
    from mcp_openapi.extract import parse_extract
    out = parse_extract(spec)
    assert not isinstance(out, str)
    return out


def test_normalize_response_extract_single_hit_replaces_body():
    body = {"servers": [{"id": "s1"}, {"id": "s2"}], "total": 2}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": body},
                                     extract=_extract("$.servers[0].id"))
    assert out["body"] == "s1"
    assert out["truncated"] is True            # body 非完整原始响应体
    assert "未保留" in out["extract"]["note"]
    assert "spill" not in out


def test_normalize_response_extract_wildcard_list():
    body = {"servers": [{"id": "s1"}, {"id": "s2"}]}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": body},
                                     extract=_extract("$.servers[*].id"))
    assert out["body"] == ["s1", "s2"]


def test_normalize_response_extract_zero_miss_keeps_body():
    body = {"servers": [{"id": "s1"}], "total": 1}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": body},
                                     extract=_extract("$.nope.deep"))
    assert out["body"] == body                 # 未命中：body 保持现状（自纠面）
    assert "truncated" not in out
    assert out["extract"]["misses"] == ["$.nope.deep: 无命中"]
    assert "extracted" not in out["extract"]


def test_normalize_response_extract_mapping_all_hit_replaces_body():
    body = {"servers": [{"id": "s1"}], "total": 1}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": body},
                                     extract=_extract({"id": "$.servers[0].id",
                                                       "n": "$.total"}))
    assert out["body"] == {"id": "s1", "n": 1}
    assert out["truncated"] is True


def test_normalize_response_extract_mapping_partial_keeps_body():
    body = {"servers": [{"id": "s1"}], "total": 1}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": body},
                                     extract=_extract({"id": "$.servers[0].id",
                                                       "x": "$.nope"}))
    assert out["body"] == body
    assert out["extract"]["extracted"] == {"id": "s1", "x": None}
    assert out["extract"]["misses"] == ["x: 无命中（$.nope）"]


def test_normalize_response_extract_oversized_original_spills_truth(tmp_path):
    """真值双轨：原始体超限且投影不超限 → spill 落盘原始体（disk == wire）。"""
    from mcp_openapi.spill import SpillConfig
    big = {"servers": [{"id": "s1"}], "fill": "x" * 250_000}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": big},
                                     spill=SpillConfig(dir=tmp_path), stem="ECS-L",
                                     extract=_extract("$.servers[0].id"))
    assert out["body"] == "s1"
    assert out["truncated"] is True
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == big
    assert "spill" in out["extract"]["note"]


def test_normalize_response_extract_oversized_without_spill_notes_loss():
    big = {"servers": [{"id": "s1"}], "fill": "x" * 250_000}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": big},
                                     extract=_extract("$.servers[0].id"))
    assert out["body"] == "s1"
    assert "spill" not in out
    assert "未保留" in out["extract"]["note"]


def test_normalize_response_extract_oversized_projection_spills_projection(tmp_path):
    """投影自身超限 → spill 落盘投影（信封 body 的真值），原始响应未保留。"""
    from mcp_openapi.spill import SpillConfig
    big = {"rows": [{"data": "y" * 300_000}, {"data": "y" * 300_000}]}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": big},
                                     spill=SpillConfig(dir=tmp_path), stem="x",
                                     extract=_extract("$.rows[*].data"))
    assert out["truncated"] is True
    assert out["body"]["truncated"] is True
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == ["y" * 300_000, "y" * 300_000]
    assert "投影" in out["extract"]["note"]


def test_normalize_response_extract_bytes_body_noop(tmp_path):
    from mcp_openapi.spill import SpillConfig
    raw = _png_like()
    out = execute.normalize_response({"status": 200, "headers": {}, "body": raw},
                                     spill=SpillConfig(dir=tmp_path), stem="x",
                                     extract=_extract("$.a"))
    assert out["body"]["binary"] is True       # 占位现状不变
    with open(out["spill"]["path"], "rb") as f:
        assert f.read() == raw
    assert "二进制" in out["extract"]["note"]


def test_normalize_response_extract_str_body_noop():
    out = execute.normalize_response({"status": 200, "headers": {}, "body": "<xml/>"},
                                     extract=_extract("$.a"))
    assert out["body"] == "<xml/>"
    assert "文本" in out["extract"]["note"]
    assert "truncated" not in out


def test_normalize_response_extract_none_body_noop():
    out = execute.normalize_response({"status": 204, "headers": {}, "body": None},
                                     extract=_extract("$.a"))
    assert out["body"] is None
    assert "无 body" in out["extract"]["note"]


def test_normalize_response_extract_error_body_json():
    """非 2xx 抽取合法：error_code/error_msg 归一仍取自原始体。"""
    body = {"error": {"code": "E.1", "message": "boom"}}
    out = execute.normalize_response({"status": 400, "headers": {}, "body": body},
                                     extract=_extract("$.error.message"))
    assert out["body"] == "boom"
    assert out["error_code"] == "E.1"
    assert out["error_msg"] == "boom"


def test_normalize_response_without_extract_unchanged():
    """回归红线：未传 extract 的信封与既有行为逐字段一致（无 extract 字段）。"""
    body = {"servers": [{"id": "s1"}]}
    out = execute.normalize_response({"status": 200, "headers": {}, "body": body})
    assert out == {"status": 200, "body": body}
    big = {"fill": "x" * 250_000}
    out2 = execute.normalize_response({"status": 200, "headers": {}, "body": big})
    assert out2["truncated"] is True
    assert "extract" not in out2


def test_execute_api_passes_extract(mini_detail):
    loc = _get_op(mini_detail)
    client = StubClient([{"status": 200, "headers": {},
                          "body": {"servers": [{"id": "s1"}], "total": 1}}])
    out = execute.execute_api(loc, "ECS", "ListServers", "cn-north-4", {},
                              executor=execute.RealApiExecutor(client, CRED),
                              extract=_extract("$.total"))
    assert out["ok"] is True
    assert out["body"] == 1
