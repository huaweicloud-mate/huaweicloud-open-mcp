"""华为云 Open MCP server 入口：CLI 参数解析 + mode 分发。"""

import argparse
import logging
import os
from typing import Literal

from common.elicit import parse_elicit_mode
from common.logconf import configure_logging

logger = logging.getLogger("main")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="huaweicloud-open-mcp",
        description="华为云 Open MCP server（stdio）。openapi 直连华为云 API；discover 发现连接云端 MCP server。")
    parser.add_argument("--mode", choices=["openapi", "discover"], default=None,
                        help="运行模式（默认 openapi；环境变量 HUAWEICLOUD_MCP_MODE）")
    parser.add_argument("--mock", action="store_true", default=None,
                        help="mock 模式：openapi 模式指向 API Explorer mock；discover 模式指向本地 stub")
    parser.add_argument("--mock-base", default=None,
                        help="mock 端点基础地址（环境变量 HUAWEICLOUD_MCP_MOCK_BASE）")
    parser.add_argument("--mock-passthrough", action="store_true", default=None,
                        help="mock 模式转发 execute 业务参数到端点"
                             "（环境变量 HUAWEICLOUD_MCP_MOCK_PASSTHROUGH）")
    parser.add_argument("--policy", default=None, help="safety policy 文件路径")
    parser.add_argument("--elicitation", default=None,
                        choices=["auto", "required", "off"],
                        help="policy 变更的 elicitation 确认模式"
                             "（环境变量 HUAWEICLOUD_MCP_ELICIT；默认 auto；headless 建议 off）")
    parser.add_argument("--audit-file", default=None,
                        help="审计事件 NDJSON 文件路径（环境变量 HUAWEICLOUD_MCP_AUDIT_FILE）")
    parser.add_argument("--gate", default=None,
                        help="openapi 产品门栓配置文件路径（环境变量 HUAWEICLOUD_MCP_OPENAPI_GATE）")
    parser.add_argument("--region", default=None, help="默认 region（openapi 模式，默认 cn-north-4）")
    parser.add_argument("--log-level", default=None, help="日志级别（默认 INFO）")
    parser.add_argument("--log-file", default=None, help="日志文件路径（默认 logs/huaweicloud-open-mcp.log）")
    args = parser.parse_args()

    mode = (args.mode or os.environ.get("HUAWEICLOUD_MCP_MODE") or "openapi").lower()
    if mode not in ("openapi", "discover"):
        mode = "openapi"

    level_name = (args.log_level or os.environ.get("HUAWEICLOUD_MCP_LOG_LEVEL") or "INFO").upper()
    if level_name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level_name = "INFO"
    configure_logging(program="huaweicloud-open-mcp", level=level_name,
                      log_file=args.log_file)

    elicit_mode = parse_elicit_mode(args.elicitation
                                    or os.environ.get("HUAWEICLOUD_MCP_ELICIT"))

    if mode == "discover":
        from mcp_discover.server import build_discover_app, build_discover_config  # noqa: E402
        discover_config = build_discover_config(args)
        logger.info("server start: mode=discover mock=%s policy=%s catalog=%s elicit=%s",
                     discover_config.mock,
                     "configured" if discover_config.policy_rules else "MISSING",
                     discover_config.catalog_path, elicit_mode)
        if discover_config.policy_rules is None:
            logger.warning("未配置 safety policy，discover 连接与调用将全部拒绝（--policy 指定策略文件）")
        app = build_discover_app(discover_config, log_level=level_name,
                                 elicit_mode=elicit_mode)
    else:
        from mcp_openapi.server import build_openapi_app, build_openapi_config  # noqa: E402
        from mcp_openapi.service import ToolService  # noqa: E402
        openapi_config = build_openapi_config(args)
        logger.info("server start: mode=openapi region=%s mock=%s policy=%s credentials=%s elicit=%s",
                     openapi_config.region, openapi_config.mock,
                     "configured" if openapi_config.policy_rules else "MISSING",
                     "configured" if openapi_config.credentials else "none", elicit_mode)
        if openapi_config.policy_rules is None:
            logger.warning("未配置 safety policy，execute_api 将拒绝所有执行（--policy 指定策略文件）")
        app = build_openapi_app(ToolService(openapi_config), log_level=level_name,
                                elicit_mode=elicit_mode)

    app.run("stdio")


if __name__ == "__main__":
    main()
