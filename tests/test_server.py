"""server 装配冒烟测试（不启动 stdio 进程）。"""

from huaweicloud_mcp.server import ServerConfig, build_app
from huaweicloud_mcp.safety import policy


def _tool_names(app):
    return {name for name in app._tool_manager._tools.keys()}


def test_all_seven_tools_registered():
    app = build_app(ServerConfig())
    names = _tool_names(app)
    assert names == {
        "list_products", "get_product", "list_apis", "get_api",
        "get_api_examples", "suggest_apis", "execute_api",
    }


def test_mock_config_no_credentials():
    app = build_app(ServerConfig(mock=True))
    assert _tool_names(app) == {
        "list_products", "get_product", "list_apis", "get_api",
        "get_api_examples", "suggest_apis", "execute_api",
    }


def test_policy_loaded(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('["ECS:*=allow", "*=deny"]', encoding="utf-8")
    rules = policy.load_policy_file(str(p))
    app = build_app(ServerConfig(policy_rules=rules))
    assert len(_tool_names(app)) == 7
