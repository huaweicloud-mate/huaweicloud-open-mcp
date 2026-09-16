"""convert_openapi2 转换逻辑单元测试。"""

import json
import os

import pytest

from apie import convert_openapi2 as conv

# ---------- fix_schema_type ----------

def test_fix_schema_type_maps_nonstandard():
    schema = {"type": "long", "properties": {"a": {"type": "int"}, "b": {"type": "Bigint"}}}
    conv.fix_schema_type(schema)
    assert schema["type"] == "integer"
    assert schema["properties"]["a"]["type"] == "integer"
    assert schema["properties"]["b"]["type"] == "integer"


def test_fix_schema_type_keeps_standard():
    schema = {"type": "string", "properties": {"n": {"type": "number"}}}
    conv.fix_schema_type(schema)
    assert schema["type"] == "string"
    assert schema["properties"]["n"]["type"] == "number"


# ---------- convert_ref ----------

def test_convert_ref_components_to_definitions():
    obj = {"schema": {"$ref": "#/components/schemas/Req"}, "param": {"$ref": "#/components/parameters/P1"},
           "resp": {"$ref": "#/components/responses/R1"}, "head": {"$ref": "#/components/headers/H1"}}
    conv.convert_ref({}, obj)
    assert obj["schema"]["$ref"] == "#/definitions/Req"
    assert obj["param"]["$ref"] == "#/parameters/P1"
    assert obj["resp"]["$ref"] == "#/responses/R1"
    assert obj["head"]["$ref"] == "#/headers/H1"


# ---------- oas2_parameter ----------

def test_oas2_parameter_schema_to_type():
    p = conv.oas2_parameter({"name": "X", "in": "query", "schema": {"type": "integer"}})
    assert p["type"] == "integer"
    assert "schema" not in p


def test_oas2_parameter_path_required():
    p = conv.oas2_parameter({"name": "id", "in": "path", "type": "string"})
    assert p["required"] is True


def test_oas2_parameter_body_wraps_schema():
    p = conv.oas2_parameter({"name": "Body", "in": "body", "type": "string"})
    assert p["schema"] == {"type": "string"}


def test_oas2_parameter_query_object_to_string():
    p = conv.oas2_parameter({"name": "tag", "in": "query", "type": "object"})
    assert p["type"] == "string"


def test_oas2_parameter_removes_3o_fields():
    p = conv.oas2_parameter({"name": "X", "in": "query", "type": "string",
                             "style": "simple", "explode": False, "allowEmptyValue": True})
    assert "style" not in p
    assert "explode" not in p
    assert "allowEmptyValue" not in p


def test_oas2_parameter_keeps_x_extensions():
    p = conv.oas2_parameter({"name": "X", "in": "query", "type": "string", "x-constraint": "note"})
    assert p["x-constraint"] == "note"


# ---------- clean_schema ----------

def test_clean_schema_removes_3o_fields():
    s = {"type": "object", "nullable": True, "deprecated": True, "oneOf": [{"type": "string"}], "writeOnly": True,
         "properties": {"a": {"type": "string", "linkage_node_fields": "x"}}}
    conv.clean_schema(s)
    assert "nullable" not in s
    assert "deprecated" not in s
    assert "oneOf" not in s
    assert "writeOnly" not in s
    assert "linkage_node_fields" not in s["properties"]["a"]


def test_clean_schema_removes_bool_required():
    s = {"type": "object", "required": True, "properties": {"a": {"type": "string", "required": True}}}
    conv.clean_schema(s)
    assert "required" not in s
    assert "required" not in s["properties"]["a"]


def test_clean_schema_enum_dedup():
    s = {"type": "string", "enum": [0, 1, 2, 1, 0]}
    conv.clean_schema(s)
    assert s["enum"] == [0, 1, 2]


def test_clean_schema_removes_non_dict_props():
    s = {"type": "object", "properties": {"a": {"type": "string"}, "junk": ["x"]}}
    conv.clean_schema(s)
    assert "junk" not in s["properties"]


def test_clean_schema_keeps_list_required():
    s = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    conv.clean_schema(s)
    assert s["required"] == ["a"]


# ---------- clean_response / clean_header ----------

def test_clean_response_empty_description():
    r = conv.clean_response({})
    assert r["description"]


def test_clean_response_content_to_schema():
    r = conv.clean_response({"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}})
    assert r["schema"] == {"type": "object"}


def test_clean_response_content_example_to_examples():
    r = conv.clean_response({"description": "OK",
                             "content": {"application/json": {"example": {"a": 1}}}})
    assert r["examples"] == {"application/json": {"a": 1}}


def test_clean_response_header_ref_inlined():
    r = conv.clean_response({"description": "OK", "headers": {"X-Req": {"$ref": "#/headers/RequestId"}}},
                            header_defs={"RequestId": {"type": "string", "description": "id"}})
    assert r["headers"]["X-Req"]["type"] == "string"
    assert r["headers"]["X-Req"]["description"] == "id"


def test_clean_header_3o_schema_to_type():
    h = conv.clean_header({"schema": {"type": "string", "maxLength": 5}})
    assert h["type"] == "string"
    assert h["maxLength"] == 5


def test_clean_header_removes_style():
    h = conv.clean_header({"type": "string", "style": "simple", "explode": False})
    assert "style" not in h
    assert "explode" not in h


# ---------- convert_api 全量转换 ----------

def test_convert_api_3_to_2(mini_detail):
    api = mini_detail["apis"]["RabbitMQ::BatchCreateOrDeleteRabbitMqTag"]
    doc = conv.convert_api(api)
    assert doc["swagger"] == "2.0"
    assert "components" not in doc
    assert "BatchCreateOrDeleteTagReq" in doc["definitions"]
    op = list(doc["paths"].values())[0].get("post")
    body = [p for p in op.get("parameters", []) if p.get("in") == "body"]
    assert body and body[0]["schema"]["$ref"] == "#/definitions/BatchCreateOrDeleteTagReq"
    assert body[0]["required"] is True
    assert "204" in op["responses"]
    # 响应 content 已转 schema
    assert op["responses"]["400"]["schema"]["type"] == "object"


def test_convert_api_2_doc_fix(mini_detail):
    api = mini_detail["apis"]["ECS::ListServers"]
    doc = conv.convert_api(api)
    assert doc["swagger"] == "2.0"
    assert doc["info"]["title"]
    # path 参数补 required
    op = list(doc["paths"].values())[0].get("get")
    pid = [p for p in op["parameters"] if p["name"] == "project_id"][0]
    assert pid["required"] is True
    # query object → string
    limit = [p for p in op["parameters"] if p["name"] == "limit"][0]
    assert limit["type"] == "string"
    # 脏点：long/int 转标准
    assert doc["definitions"]["TaskResultVo"]["properties"]["result_code"]["type"] == "integer"
    assert doc["definitions"]["TaskResultVo"]["properties"]["create_time_timestamp"]["type"] == "integer"
    # 布尔 required 移除
    assert "required" not in doc["definitions"]["TaskResultVo"]["properties"]["name"]
    # enum 去重
    assert doc["definitions"]["EnumDup"]["properties"]["status"]["enum"] == [0, 1, 2]
    # 空响应补 description
    assert doc["paths"]["/v1/{project_id}/cloudservers"]["get"]["responses"]["400"]["description"]
    # consumes 字符串→数组
    assert isinstance(doc["consumes"], list)
    # schemes 大写→小写
    assert doc["schemes"] == ["https"]


def test_convert_api_3_response_example(mini_detail):
    api = mini_detail["apis"]["RabbitMQ::ShowRabbitMqTags"]
    doc = conv.convert_api(api)
    op = list(doc["paths"].values())[0].get("get")
    resp = op["responses"]["200"]
    assert resp["schema"]["type"] == "object"
    assert resp["examples"] == {"application/json": {"tags": [{"key": "env", "value": "prod"}]}}


def test_converted_doc_validates(mini_detail, swagger_schema):
    from jsonschema import Draft4Validator
    val = Draft4Validator(swagger_schema)
    for key in ("ECS::ListServers", "ECS::CreateServers", "ECS::ListTags", "ECS::UntaggedOp",
                "RabbitMQ::BatchCreateOrDeleteRabbitMqTag", "RabbitMQ::ShowRabbitMqTags"):
        doc = conv.convert_api(mini_detail["apis"][key])
        errs = list(val.iter_errors(doc))
        assert errs == [], f"{key} 转换后有校验错误: {errs[:3]}"


# ---------- x-xml-root 提升（OBS 根元素名保留） ----------

def test_clean_schema_hoists_xml_name():
    schema = {"xml": {"name": "CreateBucketConfiguration"},
              "properties": {"Location": {"type": "string"}}}
    conv.clean_schema(schema)
    assert "xml" not in schema
    assert schema["x-xml-root"] == "CreateBucketConfiguration"


def test_convert_api_preserves_obs_root_element():
    """OBS raw 定义含 xml.name：转换后经 x-xml-root 保留（运行时 LiveFallback 依赖）。"""
    with open(_fixture("obs_create_bucket_raw.json"), encoding="utf-8") as f:
        raw = json.load(f)
    doc = conv.convert_api(raw)
    defs = doc["definitions"]["CreateBucketRequestBody"]
    assert defs["x-xml-root"] == "CreateBucketConfiguration"
    assert "xml" not in defs


def _fixture(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", name)


def test_converted_obs_doc_validates(swagger_schema):
    from jsonschema import Draft4Validator
    with open(_fixture("obs_create_bucket_raw.json"), encoding="utf-8") as f:
        raw = json.load(f)
    doc = conv.convert_api(raw)
    errs = list(Draft4Validator(swagger_schema).iter_errors(doc))
    assert errs == [], f"OBS 转换文档校验失败: {errs[:3]}"


# ---------- 认证头 required 降级（auth demote，2026-09） ----------

def _demote_doc():
    """转换后 doc 形：doc-global（dict 形）/path 级/op 级 认证头大小写变体 + 对照参数。"""
    return {
        "swagger": "2.0",
        "host": "rds.cn-north-4.myhuaweicloud.com",
        "parameters": {
            "GlobalAuth": {"name": "x-auth-token", "in": "header",
                           "type": "string", "required": True},
            "GlobalKeep": {"name": "X-Project-Id", "in": "header",
                           "type": "string", "required": True},
        },
        "paths": {
            "/v3/{project_id}/instances/{instance_id}/volumes": {
                "parameters": [
                    {"name": "X-Auth-token", "in": "header",
                     "type": "string", "required": True},
                ],
                "get": {
                    "operationId": "ListVolumeInfo",
                    "parameters": [
                        {"name": "x-auth-token", "in": "header",
                         "type": "string", "required": True},
                        {"name": "x-security-token", "in": "header",
                         "type": "string", "required": True},
                        {"name": "Authorization", "in": "header",
                         "type": "string", "required": True},
                        {"name": "X-Auth-Token", "in": "header",
                         "type": "string", "required": True},
                        {"name": "x-Auth-Token", "in": "header",
                         "type": "string", "required": True},
                        {"name": "X-Language", "in": "header", "type": "string"},
                        {"name": "instance_id", "in": "path",
                         "type": "string", "required": True},
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            }
        },
    }


def _auth_requireds(doc):
    """收集 doc 里全部 (header 名, required)（doc-global/path 级/op 级）。"""
    out = []
    for v in doc["parameters"].values():
        if v.get("in") == "header":
            out.append((v["name"], v.get("required")))
    for item in doc["paths"].values():
        for p in item.get("parameters") or []:
            if p.get("in") == "header":
                out.append((p["name"], p.get("required")))
        for op in item.values():
            if isinstance(op, dict):
                for p in op.get("parameters") or []:
                    if p.get("in") == "header":
                        out.append((p["name"], p.get("required")))
    return out


def _auth_only_requireds(doc):
    """只看认证头（豁免/禁用断言用，排除 X-Language 等非认证对照头）。"""
    return [(n, r) for n, r in _auth_requireds(doc)
            if n.casefold() in conv.AUTH_HEADER_NAMES]


def test_demote_auth_headers_default_all_levels_and_casings():
    doc = _demote_doc()
    conv.demote_auth_headers(doc, "RDS", "ListVolumeInfo")
    assert _auth_requireds(doc) == [
        ("x-auth-token", False),    # doc-global 认证头
        ("X-Project-Id", True),     # doc-global 非认证对照头，required 不动
        ("X-Auth-token", False),    # path 级混合大小写
        ("x-auth-token", False),    # op 级小写
        ("x-security-token", False),
        ("Authorization", False),
        ("X-Auth-Token", False),
        ("x-Auth-Token", False),
        ("X-Language", None),       # 无 required 键，保持缺省
    ]
    # 对照红线：非认证头 required 不动
    assert doc["parameters"]["GlobalKeep"]["required"] is True
    op = list(doc["paths"].values())[0]["get"]
    path_param = [p for p in op["parameters"] if p["name"] == "instance_id"][0]
    assert path_param["required"] is True
    lang = [p for p in op["parameters"] if p["name"] == "X-Language"][0]
    assert "required" not in lang


def test_demote_auth_headers_exempt_exact_casefold():
    policy = conv.AuthDemotePolicy(exempt=frozenset({("rds", "listvolumeinfo")}))
    doc = _demote_doc()
    conv.demote_auth_headers(doc, "RDS", "ListVolumeInfo", policy)
    assert all(req is True for _, req in _auth_only_requireds(doc))
    # 同产品其他 API 照降
    doc2 = _demote_doc()
    conv.demote_auth_headers(doc2, "RDS", "CreateInstance", policy)
    assert all(req is False for _, req in _auth_only_requireds(doc2))


def test_demote_auth_headers_exempt_product_wide():
    policy = conv.AuthDemotePolicy(exempt=frozenset({("rds", "*")}))
    doc = _demote_doc()
    conv.demote_auth_headers(doc, "RDS", "Anything", policy)
    assert all(req is True for _, req in _auth_only_requireds(doc))
    doc2 = _demote_doc()
    conv.demote_auth_headers(doc2, "DDS", "Anything", policy)
    assert all(req is False for _, req in _auth_only_requireds(doc2))


def test_demote_auth_headers_disabled():
    policy = conv.AuthDemotePolicy(enabled=False)
    doc = _demote_doc()
    conv.demote_auth_headers(doc, "RDS", "ListVolumeInfo", policy)
    assert all(req is True for _, req in _auth_only_requireds(doc))


def test_demote_auth_headers_idempotent():
    doc = _demote_doc()
    conv.demote_auth_headers(doc, "RDS", "ListVolumeInfo")
    snapshot = json.dumps(doc, sort_keys=True)
    conv.demote_auth_headers(doc, "RDS", "ListVolumeInfo")
    assert json.dumps(doc, sort_keys=True) == snapshot


def test_demote_auth_headers_missing_payload_names_no_exempt():
    policy = conv.AuthDemotePolicy(exempt=frozenset({("rds", "listvolumeinfo")}))
    doc = _demote_doc()
    conv.demote_auth_headers(doc, "", "", policy)
    assert all(req is False for _, req in _auth_only_requireds(doc))


def _demote_raw():
    """raw 条目形（name/product_short 顶层字段，运行时详情载荷与离线管道同形）。"""
    return {
        "name": "ListVolumeInfo",
        "product_short": "RDS",
        "host": "rds.cn-north-4.myhuaweicloud.com",
        "base_path": "/",
        "consumes": ["application/json"],
        "schemes": ["HTTPS"],
        "definitions": {},
        "parameters": {
            "GlobalAuth": {"name": "x-auth-token", "in": "header",
                           "type": "string", "required": True},
            "GlobalKeep": {"name": "X-Project-Id", "in": "header",
                           "type": "string", "required": True},
        },
        "paths": {
            "/v3/{project_id}/instances/{instance_id}/volumes": {
                "get": {
                    "operationId": "ListVolumeInfo",
                    "parameters": [
                        {"name": "x-auth-token", "in": "header",
                         "type": "string", "required": True},
                        {"name": "X-Auth-Token", "in": "header",
                         "type": "string", "required": True},
                        {"name": "instance_id", "in": "path",
                         "type": "string", "required": True},
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            }
        },
    }


def test_convert_api_demotes_auth_headers_by_default():
    doc = conv.convert_api(_demote_raw())
    op = list(doc["paths"].values())[0]["get"]
    hdrs = {p["name"]: p.get("required")
            for p in op["parameters"] if p["in"] == "header"}
    assert hdrs["x-auth-token"] is False
    assert hdrs["X-Auth-Token"] is False
    assert doc["parameters"]["GlobalAuth"]["required"] is False
    assert doc["parameters"]["GlobalKeep"]["required"] is True


def test_convert_api_demote_disabled_keeps_metadata():
    doc = conv.convert_api(_demote_raw(),
                           auth_demote=conv.AuthDemotePolicy(enabled=False))
    op = list(doc["paths"].values())[0]["get"]
    hdrs = {p["name"]: p.get("required")
            for p in op["parameters"] if p["in"] == "header"}
    assert hdrs["x-auth-token"] is True
    assert hdrs["X-Auth-Token"] is True


def test_convert_api_demote_exempt_exact():
    policy = conv.AuthDemotePolicy(exempt=frozenset({("rds", "listvolumeinfo")}))
    doc = conv.convert_api(_demote_raw(), auth_demote=policy)
    op = list(doc["paths"].values())[0]["get"]
    hdrs = {p["name"]: p.get("required")
            for p in op["parameters"] if p["in"] == "header"}
    assert hdrs["x-auth-token"] is True


def test_parse_auth_demote_policy():
    p = conv.parse_auth_demote_policy(None, None)
    assert p.enabled is True and p.exempt == frozenset()
    assert conv.parse_auth_demote_policy("off", None).enabled is False
    assert conv.parse_auth_demote_policy("ON", None).enabled is True
    p = conv.parse_auth_demote_policy(None, "RDS:ListVolumeInfo, DDS:* ,Dds")
    assert p.exempt == frozenset({("rds", "listvolumeinfo"), ("dds", "*")})
    with pytest.raises(ValueError):
        conv.parse_auth_demote_policy("banana", None)
    with pytest.raises(ValueError):
        conv.parse_auth_demote_policy(None, ":ListVolumeInfo")
