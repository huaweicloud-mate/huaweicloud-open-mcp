"""S10b：service 层提示注入（ServiceConfig.hints 接缝：6 个注入点 + 回归红线）。"""

from apie.memory_store import MemoryStore
from mcp_openapi.gate import parse_gate
from mcp_openapi.hints import Hints, parse_hints
from mcp_openapi.service import ServiceConfig, ToolService

GROUPS = [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器", "api_count": 2,
         "is_global": False, "link": None},
        {"productshort": "RabbitMQ", "name": "消息队列", "api_count": 1,
         "is_global": False, "link": None},
    ]},
]

APIS_ECS = [
    {"name": "ListServersDetails", "method": "get", "summary": "查询详情列表",
     "tags": "状态管理", "product_short": "ECS", "info_version": "v1"},
    {"name": "CreateServer", "method": "post", "summary": "创建云服务器",
     "tags": "生命周期管理", "product_short": "ECS", "info_version": "v1"},
]

APIS_RABBIT = [
    {"name": "ListQueues", "method": "get", "summary": "查询队列",
     "tags": "队列管理", "product_short": "RabbitMQ", "info_version": "v1"},
]

DOC = {
    "swagger": "2.0",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "definitions": {},
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {
                "operationId": "ListServersDetails",
                "summary": "查询云服务器详情列表",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string", "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}

DOC_RABBIT = {
    "swagger": "2.0",
    "host": "rabbitmq.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "definitions": {},
    "paths": {
        "/v2/queues": {
            "get": {
                "operationId": "ListQueues",
                "summary": "查询队列",
                "parameters": [],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}

HINTS = parse_hints({
    "instructions": "全局指引",
    "products": {
        "ECS": {"notes": "ECS 产品提示",
                "apis": {"ListServersDetails": "ListServersDetails 提示"}},
        "RabbitMQ": {"apis": {"ListQueues": "队列提示"}},
    },
})


def _prep_store(detail=True):
    store = MemoryStore()
    store.set_products(GROUPS)
    store.set_apis("ECS", APIS_ECS)
    store.set_apis("RabbitMQ", APIS_RABBIT)
    if detail:
        op = DOC["paths"]["/v1/{project_id}/cloudservers/detail"]["get"]
        store.set_api_cache(("ecs", "ListServersDetails", "cn-north-4"),
                            (DOC, "/v1/{project_id}/cloudservers/detail", "get", op))
        op_q = DOC_RABBIT["paths"]["/v2/queues"]["get"]
        store.set_api_cache(("rabbitmq", "ListQueues", "cn-north-4"),
                            (DOC_RABBIT, "/v2/queues", "get", op_q))
    return store


def _svc(store, hints=HINTS, gate=None):
    return ToolService(store=store,
                       config=ServiceConfig(hints=hints, gate=gate or _no_gate()))


def _no_gate():
    from mcp_openapi.gate import Gate
    return Gate.unrestricted()


# ---------- list_products：条目级，仅配置了 notes 的产品 ----------

def test_list_products_annotates_configured_only():
    out = _svc(_prep_store(detail=False)).list_products()
    items = {p["product"]: p for p in out["products"]}
    assert items["ECS"]["hints"] == "ECS 产品提示"
    assert "hints" not in items["RabbitMQ"]


def test_list_products_empty_hints_is_status_quo():
    out = _svc(_prep_store(detail=False), hints=Hints.empty()).list_products()
    assert all("hints" not in p for p in out["products"])


# ---------- get_product：顶层 ----------

def test_get_product_top_level_hints():
    out = _svc(_prep_store(detail=False)).get_product("ecs")
    assert out["ok"] is True
    assert out["hints"] == "ECS 产品提示"


def test_get_product_without_notes_no_field():
    out = _svc(_prep_store(detail=False)).get_product("RabbitMQ")
    assert out["ok"] is True
    assert "hints" not in out


def test_get_product_not_found_no_hints():
    out = _svc(_prep_store(detail=False)).get_product("OBS")
    assert out["ok"] is False
    assert "hints" not in out


# ---------- list_apis：顶层产品级 + 条目级 API 级 ----------

def test_list_apis_top_level_and_item_level():
    out = _svc(_prep_store(detail=False)).list_apis("ECS")
    assert out["ok"] is True
    assert out["hints"] == "ECS 产品提示"
    items = {a["name"]: a for a in out["apis"]}
    assert items["ListServersDetails"]["hints"] == "ListServersDetails 提示"
    assert "hints" not in items["CreateServer"]


def test_list_apis_item_level_without_product_notes():
    out = _svc(_prep_store(detail=False)).list_apis("RabbitMQ")
    assert out["ok"] is True
    assert "hints" not in out
    assert out["apis"][0]["hints"] == "队列提示"


def test_list_apis_item_level_annotates_current_page_only():
    out = _svc(_prep_store(detail=False)).list_apis("ECS", limit=1)
    assert len(out["apis"]) == 1
    assert out["apis"][0]["name"] == "ListServersDetails"
    assert out["apis"][0]["hints"] == "ListServersDetails 提示"


def test_list_apis_empty_hints_is_status_quo():
    out = _svc(_prep_store(detail=False), hints=Hints.empty()).list_apis("ECS")
    assert "hints" not in out
    assert all("hints" not in a for a in out["apis"])


# ---------- get_api：顶层合并（产品在前、API 在后） ----------

def test_get_api_combined_notes():
    out = _svc(_prep_store()).get_api("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert out["hints"] == "ECS 产品提示\nListServersDetails 提示"


def test_get_api_api_level_only_when_no_product_notes():
    out = _svc(_prep_store()).get_api("RabbitMQ", "ListQueues")
    assert out["ok"] is True
    assert out["hints"] == "队列提示"


def test_get_api_no_config_no_field():
    out = _svc(_prep_store(), hints=Hints.empty()).get_api("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert "hints" not in out


def test_get_api_not_found_no_hints():
    out = _svc(_prep_store()).get_api("ECS", "NopeApi")
    assert out["ok"] is False
    assert "hints" not in out


# ---------- get_api_examples：恒不注入 ----------

def test_get_api_examples_never_annotated():
    out = _svc(_prep_store()).get_api_examples("ECS", "ListServersDetails")
    assert out["ok"] is True
    assert "hints" not in out


# ---------- 拒绝路径不注入（防越权泄漏） ----------

def test_gated_denial_has_no_hints():
    out = _svc(_prep_store(detail=False), gate=parse_gate(["VPC"])).get_product("ECS")
    assert out["ok"] is False
    assert "hints" not in out
