"""server 装配冒烟测试（不启动 stdio 进程）。"""

import argparse

from apie import mock as apie_mock
from mcp_openapi.gate import Gate, parse_gate
from mcp_openapi.server import build_app, build_config, build_instructions
from mcp_openapi.service import ServiceConfig, ToolService
from safety import policy

EXPECTED_TOOLS = {
    "list_products", "get_product", "list_apis", "get_api",
    "get_api_examples", "execute_api", "manage_policy",
}


def _tool_names(app):
    return set(app._tool_manager._tools.keys())


def _args(mock_base=None, policy_file=None):
    return argparse.Namespace(mock=True, policy=policy_file, region=None, mock_base=mock_base)


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


def test_build_config_mock_base_arg():
    cfg = build_config(_args(mock_base="http://127.0.0.1:9"))
    assert cfg.mock_base == "http://127.0.0.1:9"


def test_build_config_mock_base_env(monkeypatch):
    monkeypatch.setenv("HUAWEICLOUD_MCP_MOCK_BASE", "http://127.0.0.1:10")
    cfg = build_config(_args(mock_base=None))
    assert cfg.mock_base == "http://127.0.0.1:10"


def test_build_config_mock_base_default(monkeypatch):
    monkeypatch.delenv("HUAWEICLOUD_MCP_MOCK_BASE", raising=False)
    cfg = build_config(_args(mock_base=None))
    assert cfg.mock_base == apie_mock.MOCK_BASE


def test_execute_api_output_schema_optional_fields_nullable():
    # 回归：ExecuteResult 可选字段（拒绝路径不填充）经 SDK 序列化为 null，
    # outputSchema 必须允许 null，否则客户端严格校验报 -32602。
    app = build_app()
    schema = app._tool_manager._tools["execute_api"].output_schema
    for field in ("reason", "status", "truncated", "error_code", "error_msg", "product", "api"):
        prop = schema["properties"][field]
        assert any(opt.get("type") == "null" for opt in prop.get("anyOf", []))


# ---------- discover 模式隔离 ----------

EXPECTED_DISCOVER_TOOLS = {
    "list_mcp_servers", "get_mcp_server",
    "connect_mcp_server", "disconnect_mcp_server",
    "list_server_tools", "get_server_tool", "call_server_tool",
    "manage_policy",
}


def test_discover_mode_tools_registered():
    from mcp_discover.config import DiscoverConfig
    from mcp_discover.server import build_discover_app
    app = build_discover_app(DiscoverConfig())
    assert _tool_names(app) == EXPECTED_DISCOVER_TOOLS


def test_openapi_mode_tools_exclude_discover():
    """openapi 模式下不应注册 discover 工具。"""
    app = build_app()
    assert _tool_names(app) == EXPECTED_TOOLS
    assert "list_mcp_servers" not in _tool_names(app)


def test_discover_mode_tools_exclude_openapi():
    """discover 模式下不应注册 openapi 专属工具（manage_policy 两模式共有）。"""
    from mcp_discover.config import DiscoverConfig
    from mcp_discover.server import build_discover_app
    app = build_discover_app(DiscoverConfig())
    for tool in EXPECTED_TOOLS - {"manage_policy"}:
        assert tool not in _tool_names(app)


# ---------- 产品门栓（gate） ----------

def test_build_instructions_lists_scope():
    s = build_instructions(parse_gate(["ECS", "VPC"]))
    assert "产品授权范围" in s
    assert "ECS" in s
    assert "VPC" in s


def test_build_instructions_unrestricted():
    assert "不限制" in build_instructions(Gate.unrestricted())


def test_build_instructions_restricted_deny_hint():
    s = build_instructions(parse_gate(["ECS"]))
    assert "不要调用任何工具" in s
    assert "直接回复" in s


def test_build_instructions_unrestricted_no_deny_hint():
    assert "不要调用任何工具" not in build_instructions(Gate.unrestricted())


def test_list_products_description_deny_hint():
    app = build_app()
    desc = app._tool_manager._tools["list_products"].description
    assert "直接回复拒绝" in desc


def test_build_config_gate_arg(tmp_path):
    p = tmp_path / "g.json"
    p.write_text('{"products": ["ECS"]}', encoding="utf-8")
    cfg = build_config(argparse.Namespace(
        mock=True, policy=None, region=None, mock_base=None, gate=str(p)))
    assert cfg.gate.allows("ECS")
    assert not cfg.gate.allows("VPC")


def test_build_config_gate_env(monkeypatch, tmp_path):
    p = tmp_path / "g.json"
    p.write_text('{"products": ["VPC"]}', encoding="utf-8")
    monkeypatch.setenv("HUAWEICLOUD_MCP_OPENAPI_GATE", str(p))
    cfg = build_config(_args())
    assert cfg.gate.allows("VPC")
    assert not cfg.gate.allows("ECS")


def test_build_config_gate_default_unrestricted(monkeypatch):
    monkeypatch.delenv("HUAWEICLOUD_MCP_OPENAPI_GATE", raising=False)
    cfg = build_config(_args())
    assert cfg.gate.restrict is False
    assert cfg.gate.allows("ECS")


def test_tool_descriptions_note_gate():
    app = build_app()
    for name in EXPECTED_TOOLS:
        if name == "manage_policy":     # policy 自身不属产品门栓范围
            continue
        desc = app._tool_manager._tools[name].description
        assert "授权范围见 instructions" in desc


def test_manage_policy_description_notes_hot_reload_and_confirm():
    app = build_app()
    desc = app._tool_manager._tools["manage_policy"].description
    assert "无需重启" in desc
    assert "向用户确认" in desc


def test_discover_manage_policy_description_notes_confirm():
    from mcp_discover.config import DiscoverConfig
    from mcp_discover.server import build_discover_app
    app = build_discover_app(DiscoverConfig())
    desc = app._tool_manager._tools["manage_policy"].description
    assert "向用户确认" in desc
