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


# ---------- 参数 $ref 解析（finalize 内缝：去重前的 ref 展开） ----------

def _ref_raw():
    """FunctionGraph::InvokeFunction raw 形状：op 全部用 $ref 引 doc-global 声明。

    历史缺陷语境：finalize 去重键 name|in 对 $ref 形参数恒为 None|None，
    每个 op 仅第一个 ref 幸存（function_urn/X-Auth-Token/Content-Type 被吞）。
    """
    return {
        "name": "InvokeFunction",
        "product_short": "FunctionGraph",
        "host": "functiongraph.cn-north-4.myhuaweicloud.com",
        "base_path": "/",
        "schemes": ["HTTPS"],
        "consumes": ["application/json"],
        "definitions": {},
        "parameters": {
            "project_id": {"name": "project_id", "in": "path", "required": True,
                           "type": "string", "description": "租户项目 ID",
                           "x-example": "3cdde48c846641ebacded48c846641eb"},
            "function_urn": {"name": "function_urn", "in": "path", "required": True,
                             "type": "string", "description": "函数的URN"},
            "X-Auth-Token": {"name": "X-Auth-Token", "in": "header", "required": True,
                             "type": "string"},
            "Content-Type": {"name": "Content-Type", "in": "header", "required": True,
                             "type": "string"},
            "marker": {"name": "marker", "in": "query", "required": False,
                       "type": "string"},
        },
        "paths": {
            "/v2/{project_id}/fgs/functions/{function_urn}/invocations": {
                "parameters": [{"$ref": "#/parameters/marker"}],
                "post": {
                    "operationId": "InvokeFunction",
                    "parameters": [
                        {"$ref": "#/parameters/project_id"},
                        {"$ref": "#/parameters/function_urn"},
                        {"$ref": "#/parameters/X-Auth-Token"},
                        {"$ref": "#/parameters/Content-Type"},
                        {"name": "InvokeFunctionRequestBody", "in": "body",
                         "required": True, "schema": {"type": "object"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            },
        },
    }


def _op_params(doc):
    return list(doc["paths"].values())[0]["post"]["parameters"]


def test_finalize_resolves_all_param_refs():
    """多 ref（path/header/query）逐个解析为 doc-global 声明，字段完整携带。"""
    doc = conv.finalize(conv.fix_2_doc(_ref_raw()))
    params = _op_params(doc)
    names = [p.get("name") for p in params]
    assert names == ["project_id", "function_urn", "X-Auth-Token",
                     "InvokeFunctionRequestBody"]
    urn = params[1]
    assert urn["in"] == "path" and urn["required"] is True
    assert urn["description"] == "函数的URN"
    pid = params[0]
    assert pid["x-example"] == "3cdde48c846641ebacded48c846641eb"


def test_finalize_resolves_path_level_refs():
    """path 级参数列表的 $ref 同样解析（语料实测 49 处）。"""
    doc = conv.finalize(conv.fix_2_doc(_ref_raw()))
    path_item = list(doc["paths"].values())[0]
    assert path_item["parameters"] == [
        {"name": "marker", "in": "query", "required": False, "type": "string"}]


def test_finalize_tolerates_missing_ref_target():
    """ref 指向缺失键：原样保留（validate_params 跳过无 name 参数，不炸）。"""
    raw = _ref_raw()
    raw["paths"]["/v2/{project_id}/fgs/functions/{function_urn}/invocations"][
        "post"]["parameters"].append({"$ref": "#/parameters/nope"})
    doc = conv.finalize(conv.fix_2_doc(raw))
    params = _op_params(doc)
    assert {"$ref": "#/parameters/nope"} in params


def test_finalize_dedup_after_ref_resolution():
    """解析后去重键恢复 name|in 语义：inline 与 ref 同名只留先者。"""
    raw = _ref_raw()
    op = raw["paths"]["/v2/{project_id}/fgs/functions/{function_urn}/invocations"][
        "post"]
    op["parameters"].append({"name": "marker", "in": "query", "type": "string"})
    doc = conv.finalize(conv.fix_2_doc(raw))
    markers = [p for p in _op_params(doc) if p.get("name") == "marker"]
    assert len(markers) == 1


def test_resolve_param_ref_copy_isolation():
    """解析返回深拷贝：原地改写不互染 doc-global 与其他解析结果。"""
    doc_params = {"X": {"name": "X-Auth-Token", "in": "header", "required": True,
                        "type": "string"}}
    first = conv._resolve_param_ref({"$ref": "#/parameters/X"}, doc_params)
    first["required"] = False
    second = conv._resolve_param_ref({"$ref": "#/parameters/X"}, doc_params)
    assert second["required"] is True
    assert doc_params["X"]["required"] is True


def test_resolve_param_ref_passthrough_and_gateway_drop():
    """非 ref 原样透传；网关供给头（Content-Type）ref 解析为 None 丢弃；
    大小写变体同样命中；认证头 ref 正常解析（由 demote 降级）。"""
    doc_params = {
        "CT": {"name": "Content-Type", "in": "header", "required": True,
               "type": "string"},
        "ct": {"name": "content-type", "in": "header", "required": True,
               "type": "string"},
        "Auth": {"name": "X-Auth-Token", "in": "header", "required": True,
                 "type": "string"},
    }
    inline = {"name": "q", "in": "query", "type": "string"}
    assert conv._resolve_param_ref(inline, doc_params) is inline
    assert conv._resolve_param_ref("junk", doc_params) == "junk"
    assert conv._resolve_param_ref({"$ref": "#/parameters/CT"}, doc_params) is None
    assert conv._resolve_param_ref({"$ref": "#/parameters/ct"}, doc_params) is None
    assert conv._resolve_param_ref({"$ref": "#/parameters/Auth"}, doc_params)[
        "name"] == "X-Auth-Token"


def test_convert_api_gold_functiongraph_invoke_function():
    """金标（真实 raw fixture）：InvokeFunction 转换后 function_urn 声明恢复，
    认证头 ref 解析后被 demote 降级，网关供给头 CT ref 维持不可见。"""
    with open(_fixture("functiongraph_invoke_function_raw.json"),
              encoding="utf-8") as f:
        raw = json.load(f)
    doc = conv.convert_api(raw)
    params = list(doc["paths"].values())[0]["post"]["parameters"]
    by_name = {p["name"]: p for p in params if p.get("name")}
    urn = by_name["function_urn"]
    assert urn["in"] == "path" and urn["required"] is True
    assert "Content-Type" not in by_name
    auth = by_name["X-Auth-Token"]
    assert auth["in"] == "header" and auth["required"] is False
    # 呈现层（get_api 同源）：function_urn 可见
    from apie.metadata import format_api_detail
    path = next(iter(doc["paths"]))
    op = doc["paths"][path]["post"]
    out = format_api_detail(doc, "FunctionGraph", path, "post", op)
    out_names = [p["name"] for p in out["parameters"]]
    assert "function_urn" in out_names and "Content-Type" not in out_names


# ---------- 路径占位符声明补全（_complete_path_params 内缝） ----------

def _gap_doc():
    """Config/RMS/IAM 类缺口形状：占位符在任何层级都无声明（raw 真缺）。"""
    return {
        "swagger": "2.0",
        "info": {"title": "X API", "version": "1.0"},
        "host": "x.cn-north-4.myhuaweicloud.com",
        "basePath": "/",
        "parameters": {
            "GlobalDomain": {"name": "domain_id", "in": "path", "required": True,
                             "type": "string"},
        },
        "paths": {
            "/v1/{project_id}/aggregators/{aggregator_id}": {
                "get": {
                    "parameters": [{"name": "X-Language", "in": "header",
                                    "type": "string"}],
                    "responses": {"200": {"description": "OK"}},
                },
            },
            "/v1/domains/{domain_id}": {
                "get": {"responses": {"200": {"description": "OK"}}},
            },
            "/v1/plain": {
                "get": {"responses": {"200": {"description": "OK"}}},
            },
        },
    }


def test_complete_path_params_injects_missing():
    """未声明占位符注入合成声明（形状：in=path/required/type=string+来源标注）。"""
    doc = conv._complete_path_params(_gap_doc())
    op = doc["paths"]["/v1/{project_id}/aggregators/{aggregator_id}"]["get"]
    by_name = {p["name"]: p for p in op["parameters"]}
    assert set(by_name) == {"X-Language", "project_id", "aggregator_id"}
    for name in ("project_id", "aggregator_id"):
        p = by_name[name]
        assert p["in"] == "path" and p["required"] is True
        assert p["type"] == "string"
        assert "元数据未声明" in p["description"]
    # doc-global 已声明的 domain_id 不注入，且无占位符的 path 不动
    assert doc["paths"]["/v1/domains/{domain_id}"]["get"].get("parameters") is None
    assert doc["paths"]["/v1/plain"]["get"].get("parameters") is None


def test_complete_path_params_project_id_credential_hint():
    """仅 project_id 的描述附「可由凭证自动填充」；其余不带。"""
    doc = conv._complete_path_params(_gap_doc())
    op = doc["paths"]["/v1/{project_id}/aggregators/{aggregator_id}"]["get"]
    by_name = {p["name"]: p for p in op["parameters"]}
    assert "凭证自动填充" in by_name["project_id"]["description"]
    assert "凭证自动填充" not in by_name["aggregator_id"]["description"]


def test_complete_path_params_respects_all_declaration_levels():
    """op 级 / path 级 / doc-global 任一层级已声明即不注入（幂等）。"""
    raw = _ref_raw()  # project_id/function_urn 均有声明（op 级 ref→解析后 inline）
    doc = conv._complete_path_params(conv.convert_api(raw))
    op = list(doc["paths"].values())[0]["post"]
    names = [p["name"] for p in op["parameters"]]
    assert names.count("project_id") == 1
    assert names.count("function_urn") == 1
    # path 级声明挡住注入（参数留在 path 级，不复制进 op）；未声明的仍注入
    doc2 = _gap_doc()
    doc2["paths"]["/v1/{project_id}/aggregators/{aggregator_id}"]["parameters"] = [
        {"name": "project_id", "in": "path", "required": True, "type": "string"}]
    conv._complete_path_params(doc2)
    op2 = doc2["paths"]["/v1/{project_id}/aggregators/{aggregator_id}"]["get"]
    names2 = [p["name"] for p in op2["parameters"]]
    assert "project_id" not in names2
    assert "aggregator_id" in names2


def test_complete_path_params_idempotent():
    doc = _gap_doc()
    conv._complete_path_params(doc)
    snapshot = json.dumps(doc, sort_keys=True)
    conv._complete_path_params(doc)
    assert json.dumps(doc, sort_keys=True) == snapshot


def test_convert_api_completes_undeclared_placeholders():
    """接线金标：convert_api 输出对 raw 真缺声明（非 ref 形）的占位符补全。"""
    raw = _gap_raw()
    doc = conv.convert_api(raw)
    op = list(doc["paths"].values())[0]["get"]
    by_name = {p["name"]: p for p in op["parameters"]}
    assert by_name["resource_id"]["in"] == "path"
    assert by_name["resource_id"]["required"] is True
    assert "project_id" not in by_name  # 无占位符则无注入


def _gap_raw():
    return {
        "name": "ShowResource",
        "product_short": "Config",
        "host": "rms.cn-north-4.myhuaweicloud.com",
        "base_path": "/",
        "schemes": ["HTTPS"],
        "consumes": ["application/json"],
        "definitions": {},
        "parameters": {},
        "paths": {
            "/v1/resource-manager/domains/{domain_id}/resources/{resource_id}": {
                "get": {
                    "operationId": "ShowResource",
                    "parameters": [
                        {"name": "domain_id", "in": "path", "required": True,
                         "type": "string"},
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            },
        },
    }
