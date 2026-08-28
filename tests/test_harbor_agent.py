"""S6：Harbor agent 装配约定（conventions.build_agent_opencode_config）。

opencode_agent.py 本体由 harbor 运行时加载（本机无 harbor），其全部装配
约定经本文件锚定；MCP 命令等常量与 exporter 渲染的 start_mcp.sh 一致。
"""

from benchmarks.harbor import conventions as conv


def test_agent_opencode_config_registers_gateway_with_conventions():
    cfg = conv.build_agent_opencode_config()
    entry = cfg["mcp"]["huaweicloud-open-mcp"]
    assert entry == {"type": "local", "command": [conv.MCP_COMMAND],
                     "cwd": conv.HWC_DIR, "enabled": True}
    assert cfg["permission"] == {"*": "allow"}
    assert cfg["tool_output"]["max_lines"] == 50000


def test_conventions_paths_are_absolute_and_consistent():
    assert conv.MCP_COMMAND == f"{conv.HWC_DIR}/start_mcp.sh"
    assert conv.STUB_BASE_URL == f"http://127.0.0.1:{conv.STUB_PORT}"
    assert conv.AUDIT_FILE.startswith("/tmp/")
    assert conv.ANSWER_FILE.startswith("/tmp/")
    assert conv.STUB_LEDGER.startswith("/tmp/")
