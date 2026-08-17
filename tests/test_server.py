"""server 装配冒烟测试（不启动 stdio 进程）。"""

from huaweicloud_mcp.safety import policy
from huaweicloud_mcp.server import build_app
from huaweicloud_mcp.tools.service import ServiceConfig, ToolService

EXPECTED_TOOLS = {
    "list_products", "get_product", "list_apis", "get_api",
    "get_api_examples", "suggest_apis", "execute_api",
}


def _tool_names(app):
    return set(app._tool_manager._tools.keys())


def test_all_seven_tools_registered():
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
    assert len(_tool_names(app)) == 7
