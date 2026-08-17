"""元数据工具纯函数单元测试（mini fixture）。"""

from huaweicloud_mcp.tools import metadata


def _count_map():
    return {"ECS": 4, "RABBITMQ": 2}


def test_list_products(mini_products):
    out = metadata.list_products(mini_products["groups"], counts=_count_map())
    assert out["total"] == 2
    by_name = {p["product"]: p for p in out["products"]}
    assert by_name["ECS"]["name"] == "弹性云服务器"
    assert by_name["ECS"]["api_count"] == 4
    assert by_name["ECS"]["is_global"] is False


def test_list_products_category_filter(mini_products):
    out = metadata.list_products(mini_products["groups"], counts=_count_map(), category="计算")
    assert out["total"] == 1
    assert out["products"][0]["product"] == "ECS"


def test_list_products_keyword_filter(mini_products):
    out = metadata.list_products(mini_products["groups"], counts=_count_map(), keyword="rabbit")
    assert out["total"] == 1
    assert out["products"][0]["product"] == "RabbitMQ"


def test_get_product(mini_products):
    p = metadata.get_product(mini_products["groups"], "ECS", counts=_count_map())
    assert p["product"] == "ECS"
    assert p["name"] == "弹性云服务器"
    assert p["category"] == "计算"
    assert p["api_count"] == 4


def test_get_product_case_insensitive(mini_products):
    p = metadata.get_product(mini_products["groups"], "ecs")
    assert p["product"] == "ECS"


def test_get_product_not_found(mini_products):
    assert metadata.get_product(mini_products["groups"], "NOPE") is None


def test_list_apis(mini_docs):
    out = metadata.list_apis(mini_docs["apis"], "ECS")
    assert out["total"] == 4
    assert {a["name"] for a in out["apis"]} == {"ListServers", "CreateServers", "ListTags", "UntaggedOp"}
    assert out["tag_groups"] == [
        {"tag": "生命周期管理", "api_count": 2},
        {"tag": "_untagged", "api_count": 1},
        {"tag": "标签管理", "api_count": 1},
    ]


def test_list_apis_tag_groups_full_scope_regardless_of_filters(mini_docs):
    out = metadata.list_apis(mini_docs["apis"], "ECS", tag="生命周期管理")
    assert out["total"] == 2
    assert out["tag_groups"] == [
        {"tag": "生命周期管理", "api_count": 2},
        {"tag": "_untagged", "api_count": 1},
        {"tag": "标签管理", "api_count": 1},
    ]


def test_list_apis_tag_filter(mini_docs):
    out = metadata.list_apis(mini_docs["apis"], "ECS", tag="生命周期管理")
    assert out["total"] == 2
    assert {a["name"] for a in out["apis"]} == {"ListServers", "CreateServers"}


def test_list_apis_search_filter(mini_docs):
    out = metadata.list_apis(mini_docs["apis"], "ECS", search="tag")
    assert out["total"] == 2
    assert {a["name"] for a in out["apis"]} == {"ListTags", "UntaggedOp"}


def test_list_apis_pagination(mini_docs):
    out = metadata.list_apis(mini_docs["apis"], "ECS", limit=2, offset=1)
    assert out["total"] == 4
    assert len(out["apis"]) == 2


def test_list_apis_case_insensitive_product(mini_docs):
    out = metadata.list_apis(mini_docs["apis"], "ecs")
    assert out["total"] == 4


def test_find_api_in_doc_exact(mini_detail):
    from huaweicloud_mcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    path, method, op = metadata.find_api_in_doc(doc, "ListServers")
    assert method == "get"
    assert path == "/v1/{project_id}/cloudservers"
    assert op["operationId"] == "ListServers"


def test_find_api_in_doc_case_insensitive(mini_detail):
    from huaweicloud_mcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    path, method, op = metadata.find_api_in_doc(doc, "listservers")
    assert op["operationId"] == "ListServers"


def test_find_api_in_doc_substring(mini_detail):
    from huaweicloud_mcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    path, method, op = metadata.find_api_in_doc(doc, "ListServer")
    assert op["operationId"] == "ListServers"


def test_find_api_in_doc_not_found(mini_detail):
    from huaweicloud_mcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    assert metadata.find_api_in_doc(doc, "NopeApi") is None


def test_format_api_detail(mini_detail):
    from huaweicloud_mcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["RabbitMQ::BatchCreateOrDeleteRabbitMqTag"])
    path, method, op = metadata.find_api_in_doc(doc, "BatchCreateOrDeleteRabbitMqTag")
    out = metadata.format_api_detail(doc, "RabbitMQ", path, method, op)
    assert out["product"] == "RabbitMQ"
    assert out["api"] == "BatchCreateOrDeleteRabbitMqTag"
    assert out["method"] == "POST"
    assert out["path"] == path
    names = {p["name"] for p in out["parameters"]}
    assert "instance_id" in names
    body = [p for p in out["parameters"] if p["in"] == "body"]
    assert body and body[0]["required"] is True
    assert "BatchCreateOrDeleteTagReq" in out["definitions"]
    assert "200" not in out["responses"]  # 该 API 只有 204/400
    assert "204" in out["responses"]


def test_format_api_detail_path_required_flag(mini_detail):
    from huaweicloud_mcp.apie import convert_openapi2 as conv
    doc = conv.convert_api(mini_detail["apis"]["ECS::ListServers"])
    path, method, op = metadata.find_api_in_doc(doc, "ListServers")
    out = metadata.format_api_detail(doc, "ECS", path, method, op)
    pid = [p for p in out["parameters"] if p["name"] == "project_id"][0]
    assert pid["required"] is True


def test_extract_examples():
    op = {"x-request-examples-description-1": "示例说明", "x-request-examples-1": {"action": "create"}}
    assert metadata.extract_examples(op) == [{"description": "示例说明", "example": {"action": "create"}}]


def test_format_api_detail_resolves_ref_parameter():
    doc = {
        "swagger": "2.0",
        "paths": {
            "/v1/{project_id}/cloudservers": {
                "get": {
                    "operationId": "ListServers",
                    "parameters": [
                        {"$ref": "#/parameters/project_id"},
                        {"name": "limit", "in": "query", "type": "integer"},
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
        "parameters": {
            "project_id": {"name": "project_id", "in": "path", "type": "string",
                           "required": True, "description": "项目ID"},
        },
        "definitions": {},
    }
    out = metadata.format_api_detail(doc, "ECS", "/v1/{project_id}/cloudservers", "get",
                                     doc["paths"]["/v1/{project_id}/cloudservers"]["get"])
    params = {p["name"]: p for p in out["parameters"]}
    assert params["project_id"]["in"] == "path"
    assert params["project_id"]["required"] is True
    assert params["project_id"]["description"] == "项目ID"
    assert "$ref" not in params["project_id"]
    assert "limit" in params


def test_format_api_detail_drops_unresolved_ref_parameter():
    doc = {
        "swagger": "2.0",
        "paths": {
            "/p": {"get": {"operationId": "Op",
                           "parameters": [{"$ref": "#/parameters/missing"}],
                           "responses": {"200": {"description": "OK"}}}}
        },
        "parameters": {},
        "definitions": {},
    }
    out = metadata.format_api_detail(doc, "ECS", "/p", "get",
                                     doc["paths"]["/p"]["get"])
    assert out["parameters"] == []


def test_extract_examples_text_fallback():
    op = {"x-request-examples-text-1": '{"action": "list"}'}
    examples = metadata.extract_examples(op)
    assert examples == [{"description": None, "example": {"action": "list"}}]


def test_extract_examples_none():
    assert metadata.extract_examples({}) == []
    assert metadata.extract_examples(None) == []
