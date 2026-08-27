"""S6：build_benchdir_config（opencode.json 装配单一真值源）扩展参数测试。

两个适配器共用：legacy runner（不传新参，行为不变）与 harbor OpencodeAgent（传满）。
"""

import json

from benchmarks.runner import build_benchdir_config


def _command(cfg: dict) -> list[str]:
    return cfg["mcp"]["huaweicloud-open-mcp"]["command"]


def test_legacy_signature_unchanged():
    cfg = json.loads(build_benchdir_config("/p/policy.json", "http://127.0.0.1:9"))
    cmd = _command(cfg)
    assert "--mock" in cmd and "--policy" in cmd
    assert cmd[cmd.index("--policy") + 1] == "/p/policy.json"
    assert cmd[cmd.index("--mock-base") + 1] == "http://127.0.0.1:9"
    assert "--mock-passthrough" not in cmd
    assert "--audit-file" not in cmd
    assert cfg["permission"] == {"huaweicloud-open-mcp_*": "allow"}
    assert cfg["mcp"]["huaweicloud-open-mcp"]["type"] == "local"


def test_mock_base_none_omits_flag():
    cfg = json.loads(build_benchdir_config("/p/policy.json", None))
    assert "--mock-base" not in _command(cfg)


def test_passthrough_and_audit_flags_appended():
    cfg = json.loads(build_benchdir_config("/p/policy.json", None,
                                           mock_passthrough=True,
                                           audit_file="/tmp/hwc_audit.jsonl"))
    cmd = _command(cfg)
    assert "--mock-passthrough" in cmd
    assert cmd[cmd.index("--audit-file") + 1] == "/tmp/hwc_audit.jsonl"
