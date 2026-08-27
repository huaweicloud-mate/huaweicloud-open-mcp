"""Harbor 数据集生成约定：路径与常量的单一真值源。

exporter（渲染模板）、OpencodeAgent（生成 opencode.json）、verifier/oracle 模板
共同引用这些约定；修改任何值必须全链路一致。
"""

STUB_PORT = 8010
STUB_BASE_URL = "http://127.0.0.1:8010"

AUDIT_FILE = "/tmp/hwc_audit.jsonl"        # 网关审计 NDJSON（verifier 主输入）
STUB_LEDGER = "/tmp/hwc_stub_ledger.jsonl"  # stub 请求台账（wire 级断言）
ANSWER_FILE = "/tmp/answer.txt"             # agent 最终回答（instruction 约定写入）

HWC_DIR = "/opt/hwc"                        # 容器内项目根
MCP_COMMAND = "/opt/hwc/start_mcp.sh"       # MCP server 启动脚本（stdio）
CASE_FILE = "/tests/case.yaml"              # verifier/oracle 读取的 case 原文

TASK_ORG = "mcp"                            # task.name = "<org>/<case_id>"

AGENT_WORKDIR = "/opt/agent"                # 容器内 opencode 工作目录


def build_agent_opencode_config() -> dict:
    """容器内 opencode.json（零依赖纯函数，宿主侧可测）。

    与 legacy runner 的 build_benchdir_config 同构；差异仅在 MCP 命令形态：
    容器内指向约定的 start_mcp.sh（stub 端口 / policy / audit 均已固化其中）。
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "tool_output": {"max_lines": 50000, "max_bytes": 20971520},
        "mcp": {
            "huaweicloud-open-mcp": {
                "type": "local",
                "command": [MCP_COMMAND],
                "cwd": HWC_DIR,
                "enabled": True,
            },
        },
        "permission": {"huaweicloud-open-mcp_*": "allow"},
    }
