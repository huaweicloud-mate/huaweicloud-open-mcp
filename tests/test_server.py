"""server 装配冒烟测试（不启动 stdio 进程）。"""

from openmcp.safety import policy
from openmcp.server import build_app
from openmcp.tools.service import ServiceConfig, ToolService

EXPECTED_TOOLS = {
    "list_products", "get_product", "list_apis", "get_api",
    "get_api_examples", "execute_api",
}


def _tool_names(app):
    return set(app._tool_manager._tools.keys())


def test_all_six_tools_registered():
    app = build_app()
    assert _tool_names(app) == EXPECTED_TOOLS


def test_mock_config_no_credentials():
    app = build_app(ToolService(ServiceConfig(mock=True)))
    assert _tool_names(app) == EXPECTED_TOOLS


def test_policy_loaded(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('["ECS:*=allow", "*=deny"]', encoding="utf-8")
    rules = policy.load_policy_file(str(p))
    app = build_app(ToolService(ServiceConfig(policy_rules=rules)))
    assert len(_tool_names(app)) == 6


def test_execute_api_output_schema_optional_fields_nullable():
    # 回归：ExecuteResult 可选字段（拒绝路径不填充）经 SDK 序列化为 null，
    # outputSchema 必须允许 null，否则客户端严格校验报 -32602。
    app = build_app()
    schema = app._tool_manager._tools["execute_api"].output_schema
    for field in ("reason", "status", "truncated", "error_code", "error_msg", "product", "api"):
        prop = schema["properties"][field]
        assert any(opt.get("type") == "null" for opt in prop.get("anyOf", []))
