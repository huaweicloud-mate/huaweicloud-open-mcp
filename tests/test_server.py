"""server 装配冒烟测试（不启动 stdio 进程）。"""

import argparse

import pytest

from apie import mock as apie_mock
from common import paths
from mcp_openapi.hints import Hints, parse_hints
from mcp_openapi.server import build_app, build_config, build_instructions
from mcp_openapi.service import ServiceConfig, ToolService
from safety import policy

EXPECTED_TOOLS = {
    "search_apis", "list_products", "get_product", "list_apis", "get_api",
    "get_api_examples", "execute_api", "manage_policy",
}

# 文件级密封（conftest 共享 fixture）：缺省 hints 装配不受宿主文件系统泄入影响
pytestmark = pytest.mark.usefixtures("sealed_configs")


def _tool_names(app):
    return set(app._tool_manager._tools.keys())


def _args(mock_base=None, policy_file=None, **kw):
    ns = argparse.Namespace(mock=True, policy=policy_file, region=None, mock_base=mock_base)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_all_tools_registered():
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
    assert len(_tool_names(app)) == 8


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
    for field in ("reason", "status", "truncated", "error_code", "error_msg",
                  "product", "api", "presign"):
        prop = schema["properties"][field]
        assert any(opt.get("type") == "null" for opt in prop.get("anyOf", []))


def test_manage_policy_description_notes_list_only_and_operator_path():
    app = build_app()
    desc = app._tool_manager._tools["manage_policy"].description
    assert "action=list" in desc
    assert "操作员" in desc and "policy 文件" in desc
    assert "热生效" in desc and "无需重启" in desc
    assert "confirm=true" in desc            # 单次执行确认路径指引


# ---------- 提示注入（hints） ----------

def test_build_instructions_appends_global_hints():
    h = parse_hints({"instructions": "全局指引"})
    s = build_instructions(h)
    assert "部署自定义指引" in s
    assert "全局指引" in s


def test_build_instructions_without_hints_no_section():
    assert "部署自定义指引" not in build_instructions()
    assert "部署自定义指引" not in build_instructions(Hints.empty())


def test_build_config_hints_arg(tmp_path):
    p = tmp_path / "h.json"
    p.write_text('{"instructions": "g", "products": {"ECS": "n"}}', encoding="utf-8")
    cfg = build_config(argparse.Namespace(
        mock=True, policy=None, region=None, mock_base=None, hints=str(p)))
    assert cfg.hints.instructions == "g"
    assert cfg.hints.product_notes("ECS") == "n"


def test_build_config_hints_env(monkeypatch, tmp_path):
    p = tmp_path / "h.json"
    p.write_text('{"instructions": "g2"}', encoding="utf-8")
    monkeypatch.setenv("HUAWEICLOUD_MCP_OPENAPI_HINTS", str(p))
    cfg = build_config(_args())
    assert cfg.hints.instructions == "g2"


def test_build_config_hints_default_loads_configs_file(tmp_path, monkeypatch):
    """未配置 --hints/env → 缺省档加载仓库 configs/help-docs-hints.json。"""
    cfgdir = tmp_path / "repo" / "configs"
    cfgdir.mkdir(parents=True)
    (cfgdir / "help-docs-hints.json").write_text('{"instructions": "装配缺省"}',
                                                 encoding="utf-8")
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    cfg = build_config(_args())
    assert cfg.hints.instructions == "装配缺省"


def test_build_config_hints_default_missing_silent(tmp_path, monkeypatch):
    """未配置且缺省文件全缺失 → 静默 empty（隐式缺省不 fail-fast）。"""
    monkeypatch.delenv("HUAWEICLOUD_MCP_OPENAPI_HINTS", raising=False)
    cfg = build_config(_args())
    assert cfg.hints.instructions is None
    assert cfg.hints.product_notes("ECS") is None


def test_build_config_hints_off_disables(tmp_path, monkeypatch):
    """--hints off → 显式禁用（即使缺省文件存在）。"""
    cfgdir = tmp_path / "repo" / "configs"
    cfgdir.mkdir(parents=True)
    (cfgdir / "help-docs-hints.json").write_text('{"instructions": "不应加载"}',
                                                 encoding="utf-8")
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    cfg = build_config(_args(hints="off"))
    assert cfg.hints.instructions is None


def test_build_config_hints_env_off_disables(monkeypatch):
    """env=off → 显式禁用。"""
    monkeypatch.setenv("HUAWEICLOUD_MCP_OPENAPI_HINTS", "off")
    cfg = build_config(_args())
    assert cfg.hints.instructions is None


def test_build_config_hints_explicit_missing_fail_fast(tmp_path, monkeypatch):
    """显式路径缺失 → fail-fast（与缺省档静默相区分）。"""
    with pytest.raises(FileNotFoundError):
        build_config(_args(hints="nope.json"))


def test_server_instructions_carry_hints():
    svc = ToolService(ServiceConfig(hints=parse_hints({"instructions": "全局指引"})))
    app = build_app(svc)
    assert "全局指引" in app.instructions


# ---------- spill 装配（S12） ----------

def test_build_config_spill_default_on():
    cfg = build_config(argparse.Namespace(mock=True, policy=None, region=None,
                                          mock_base=None))
    assert cfg.spill is not None
    assert cfg.spill.data_enabled is False


def test_build_config_spill_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUAWEICLOUD_MCP_SPILL_DIR", str(tmp_path / "s"))
    cfg = build_config(argparse.Namespace(mock=True, policy=None, region=None,
                                          mock_base=None))
    assert cfg.spill is not None
    assert cfg.spill.dir == tmp_path / "s"


def test_build_config_spill_dir_arg_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUAWEICLOUD_MCP_SPILL_DIR", "/from-env")
    cfg = build_config(argparse.Namespace(mock=True, policy=None, region=None,
                                          mock_base=None,
                                          spill_dir=str(tmp_path / "arg")))
    assert cfg.spill is not None
    assert cfg.spill.dir == tmp_path / "arg"


def test_build_config_spill_off(monkeypatch):
    monkeypatch.setenv("HUAWEICLOUD_MCP_SPILL_DIR", "off")
    cfg = build_config(argparse.Namespace(mock=True, policy=None, region=None,
                                          mock_base=None))
    assert cfg.spill is None


def test_build_config_spill_data_enabled(tmp_path):
    cfg = build_config(argparse.Namespace(mock=True, policy=None, region=None,
                                          mock_base=None,
                                          spill_dir=str(tmp_path)),
                       data_enabled=True)
    assert cfg.spill is not None
    assert cfg.spill.data_enabled is True


# ---------- parse_auth_demote_policy（2026-09 起自 apie.convert_openapi2 迁入装配侧） ----------

def test_parse_auth_demote_policy():
    from mcp_openapi.server import parse_auth_demote_policy
    p = parse_auth_demote_policy(None, None)
    assert p.enabled is True and p.exempt == frozenset()
    assert parse_auth_demote_policy("off", None).enabled is False
    assert parse_auth_demote_policy("ON", None).enabled is True
    p = parse_auth_demote_policy(None, "RDS:ListVolumeInfo, DDS:* ,Dds")
    assert p.exempt == frozenset({("rds", "listvolumeinfo"), ("dds", "*")})
    with pytest.raises(ValueError):
        parse_auth_demote_policy("banana", None)
    with pytest.raises(ValueError):
        parse_auth_demote_policy(None, ":ListVolumeInfo")
