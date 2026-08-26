"""convert_openapi2 转换逻辑单元测试。"""

import json
import os

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
