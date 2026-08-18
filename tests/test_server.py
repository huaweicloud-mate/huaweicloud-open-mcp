"""server 装配冒烟测试（不启动 stdio 进程）。"""

import argparse

from openmcp.apie import mock as apie_mock
from openmcp.safety import policy
from openmcp.server import build_app, build_config
from openmcp.tools.service import ServiceConfig, ToolService

EXPECTED_TOOLS = {
    "list_products", "get_product", "list_apis", "get_api",
    "get_api_examples", "execute_api",
}


def _tool_names(app):
    return set(app._tool_manager._tools.keys())


def _args(mock_base=None, policy_file=None):
    return argparse.Namespace(mock=True, policy=policy_file, region=None, mock_base=mock_base)


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
