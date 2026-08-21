"""华为云 Open MCP server：stdio 装配（mcp SDK）。

只做 MCP 协议装配，全部业务逻辑委托 ToolService / discover helpers。
支持两种运行模式（二选一，工具集互斥）：
- openapi（默认）：6 工具直连华为云 OpenAPI
- discover: 7 工具发现连接云端 MCP server
"""

import argparse
import logging
import os
from typing import Any, Literal, cast

from mcp.server.mcpserver import MCPServer

from .apie import mock as apie_mock
from .auth import credentials as cred_mod
from .logconf import configure_logging
from .mcpdiscover.config import DiscoverConfig
from .safety import policy as safety
from .tools.discover import DiscoverService
from .tools.service import ServiceConfig, ToolService
from .types import (
    ApiDetailResult,
    ApiListResult,
    ExamplesResult,
    ExecuteResult,
    McpCallResult,
    McpConnectResult,
    McpDisconnectResult,
    McpServerListResult,
    McpServerResult,
    ProductListResult,
    ProductResult,
    ServerToolResult,
    ServerToolsResult,
    ToolError,
)

logger = logging.getLogger("openmcp.server")

# ---------- 指令 ----------

INSTRUCTIONS_OPENAPI = """# 华为云 Open MCP 使用指引（OpenAPI 直连模式）

## 推荐工作流（渐进收窄，LLM 决策）

1. `list_products`：获取产品列表（含中文名/分类/接口数），基于用户任务语义确定产品范围；
2. `list_apis`：获取选定产品的 API 目录；返回结果含 `tag_groups` 全量 tag 概览，
   先用 `tag` 参数收窄目录，接口较多时配合 `search`/`limit`/`offset` 分页浏览；
3. `get_api`：确定候选接口后，调用前**必读**接口文档（必填参数、类型、枚举、x-constraint 约束）；
4. `get_api_examples`：官方请求示例，用于指导参数填写（可选）；
5. `execute_api`：执行接口。参数约定：路径/query 参数平铺在 params，请求体放 params["body"]。

## 执行安全

- `execute_api` 执行前强制过 safety policy（allowlist/denylist 白名单）；
- 未配置 policy 时所有执行被拒绝；
- 拒绝结果形如 {"ok": false, "reason": ...}，不要绕过，应改用被允许的接口或询问用户。

## 其它

- region 默认 cn-north-4；非默认 region 需显式传 region 参数；
- 产品 `is_global` 为 true 的全局级服务（如 IAM）与地域级服务认证模型不同。
"""

INSTRUCTIONS_DISCOVER = """# 华为云 Open MCP 使用指引（MCP Server 发现连接模式）

## 推荐工作流（渐进收窄，LLM 决策）

1. `list_mcp_servers`：列出华为云 MCP server 目录（含中文名/分类/认证模型），
   根据用户任务语义用 keyword 搜索匹配候选 server；
2. `get_mcp_server`：确认 server 详情（endpoint/传输层/认证方式/描述）；
3. `connect_mcp_server`：建立连接（过 safety policy；真实模式 endpoint 严格取自目录）；
4. `list_server_tools`：已连接 server 的工具摘要（工具名+首行描述+必填参数名），
   用 `search`/`limit`/`offset` 收窄，**禁止无过滤全量拉取**；
5. `get_server_tool`：选定候选后读**单个工具完整 schema**（参数类型/枚举/约束），
   仅取调用目标一个，防止上下文暴涨；
6. `call_server_tool`：代发调用（过 safety policy）；
7. `disconnect_mcp_server`：释放连接（空闲 5 分钟自动回收，可选显式断开）。

## 执行安全

- `connect_mcp_server` 和 `call_server_tool` 执行前强制过 safety policy；
- 未配置 policy 时所有连接与调用被拒绝；
- 拒绝结果形如 {"ok": false, "reason": ...}，不要绕过，应改用被允许的 server/tool 或询问用户。

## 其它

- endpoint 在真实模式下严格取自目录，不可覆盖（防目录外投毒）；
- 目标 server 工具清单为实时拉取，不落目录以免过期；
- 若目标 server 不存在或连接失败，检查 server id 是否匹配目录条目。
"""


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]



# ============================================================
# OpenAPI 模式
# ============================================================

def build_openapi_config(args: argparse.Namespace) -> ServiceConfig:
    mock = (args.mock if args.mock is not None
            else os.environ.get("HUAWEICLOUD_MCP_MOCK", "") in ("1", "true", "yes"))
    policy_file = args.policy or os.environ.get("HUAWEICLOUD_MCP_POLICY_FILE")
    region = args.region or os.environ.get("HUAWEICLOUD_MCP_REGION") or None
    mock_base = args.mock_base or os.environ.get("HUAWEICLOUD_MCP_MOCK_BASE") or None
    return ServiceConfig(
        region=region or "cn-north-4",
        mock=mock,
        policy_rules=safety.load_policy_file(policy_file) if policy_file else None,
        credentials=None if mock else cred_mod.get_credentials(),
        mock_base=mock_base or apie_mock.MOCK_BASE,
    )


def build_config(args: argparse.Namespace) -> ServiceConfig:
    """向后兼容别名。"""
    return build_openapi_config(args)


def _build_openapi_tools(server: MCPServer, service: ToolService) -> None:
    @server.tool()
    def list_products(category: str | None = None, keyword: str | None = None) -> ProductListResult | ToolError:
        """第一步：列出华为云产品（分类、中文名、接口数、是否全局级服务）。

        基于用户任务语义选择目标产品；不确定时用 keyword 按产品名/中文名搜索。
        选定产品后用 list_apis 浏览其 API 目录。
        """
        return service.list_products(category=category, keyword=keyword)

    @server.tool()
    def get_product(product: str) -> ProductResult | ToolError:
        """确认单个产品详情（分类/接口数/是否全局级服务）。全局级服务（is_global=true）认证模型不同。"""
        return service.get_product(product)

    @server.tool()
    def list_apis(product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        """第二步：列出产品的 API 目录。

        结果含 tag_groups（产品全量 tag 概览，不受过滤影响）：先用 tag 收窄目录，
        接口较多时用 search/limit/offset 分页浏览。选定候选接口后用 get_api 读文档。
        """
        return service.list_apis(product, tag=tag, search=search, limit=limit, offset=offset)

    @server.tool()
    def get_api(product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        """第三步：获取接口完整文档（方法/路径/参数必填性/类型/枚举/x-constraint 约束/响应结构）。

        执行前必读；x-constraint 描述调用前置条件与限制。
        """
        return service.get_api(product, api, region=region)

    @server.tool()
    def get_api_examples(product: str, api: str, region: str | None = None) -> ExamplesResult | ToolError:
        """获取接口的官方请求示例（x-request-examples），用于指导参数填写。"""
        return service.get_api_examples(product, api, region=region)

    @server.tool()
    def execute_api(product: str, api: str, region: str | None = None,
                    params: dict | None = None) -> ExecuteResult:
        """第四步：执行华为云 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。

        params 约定：路径参数/query 参数直接平铺，请求体放 params["body"]。
        mock 模式下 params["_status_code"]/params["_number"] 控制 mock 数据。
        """
        return service.execute_api(product, api, region=region, params=params)


def build_openapi_app(service: ToolService | None = None, *,
                      log_level: LogLevel = "INFO") -> MCPServer:
    service = service or ToolService()
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=INSTRUCTIONS_OPENAPI, log_level=log_level)
    _build_openapi_tools(server, service)
    return server


def build_app(service: ToolService | None = None, *,
              log_level: LogLevel = "INFO") -> MCPServer:
    """向后兼容：openapi 模式。"""
    return build_openapi_app(service, log_level=log_level)


# ============================================================
# Discover 模式
# ============================================================

def build_discover_config(args: argparse.Namespace) -> DiscoverConfig:
    mock = (args.mock if args.mock is not None
            else os.environ.get("HUAWEICLOUD_MCP_MOCK", "") in ("1", "true", "yes"))
    policy_file = args.policy or os.environ.get("HUAWEICLOUD_MCP_POLICY_FILE")
    catalog_path = os.environ.get("HUAWEICLOUD_MCP_SERVER_CATALOG") or DiscoverConfig.catalog_path
    mock_base = args.mock_base or os.environ.get("HUAWEICLOUD_MCP_MOCK_BASE") or None
    idle_timeout = int(os.environ.get("HUAWEICLOUD_MCP_SESSION_IDLE_TIMEOUT",
                                      str(DiscoverConfig.session_idle_timeout)))
    max_sessions = int(os.environ.get("HUAWEICLOUD_MCP_MAX_SESSIONS",
                                      str(DiscoverConfig.max_sessions)))
    return DiscoverConfig(
        catalog_path=catalog_path,
        mock=mock,
        mock_base=mock_base,
        policy_rules=safety.load_policy_file(policy_file) if policy_file else None,
        session_idle_timeout=idle_timeout,
        max_sessions=max_sessions,
    )


def build_discover_app(config: DiscoverConfig, *,
                       log_level: LogLevel = "INFO") -> MCPServer:
    ds = DiscoverService(config)

    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=INSTRUCTIONS_DISCOVER, log_level=log_level)

    @server.tool()
    def list_mcp_servers(category: str | None = None,
                         keyword: str | None = None) -> McpServerListResult | ToolError:
        """第一步：列出华为云 MCP server 目录（含中文名/分类/认证模型）。

        根据用户任务语义用 keyword 按 server 名/中文名/描述搜索；
        不确定时先不加过滤全量浏览。
        """
        logger.info("list_mcp_servers category=%s keyword=%s", category or "-", keyword or "-")
        return ds.list_servers(category=category, keyword=keyword)

    @server.tool()
    def get_mcp_server(server: str) -> McpServerResult | ToolError:
        """第二步：确认单个 MCP server 详情（endpoint/传输层/认证方式/描述）。

        选定后调用 connect_mcp_server 建立连接。
        """
        logger.info("get_mcp_server server=%s", server)
        return ds.get_server(server)

    @server.tool()
    async def connect_mcp_server(server: str) -> McpConnectResult | ToolError:
        """第三步：连接指定 MCP server（过 safety policy）。

        policy 匹配 server 连接规则：server:serverId=allow|deny；
        真实模式 endpoint 严格取自目录；mock 模式指向 --mock-base。
        """
        logger.info("connect_mcp_server server=%s", server)
        return await ds.connect(server)

    @server.tool()
    async def list_server_tools(server: str, search: str | None = None,
                                limit: int = 20, offset: int = 0
                                ) -> ServerToolsResult | ToolError:
        """第四步：已连接 MCP server 的工具摘要列表（两级读取第一步）。

        返回工具名+首行描述+必填参数名；用 search/limit/offset 收窄；
        禁止无过滤全量拉取。选定后用 get_server_tool 读完整 schema。
        """
        logger.info("list_server_tools server=%s search=%s limit=%d offset=%d",
                    server, search or "-", limit, offset)
        return await ds.list_tools(server, search=search, limit=limit, offset=offset)

    @server.tool()
    async def get_server_tool(server: str, tool: str) -> ServerToolResult | ToolError:
        """第五步：获取单个工具的完整 schema（两级读取第二步）。

        仅取调用目标一个工具，防上下文暴涨；超 16KB 自动截断。
        """
        logger.info("get_server_tool server=%s tool=%s", server, tool)
        return await ds.get_tool(server, tool)

    @server.tool()
    async def call_server_tool(server: str, tool: str,
                               arguments: dict[str, Any] | None = None
                               ) -> McpCallResult:
        """第六步：调用已连接 server 的指定工具（过 safety policy）。

        arguments 为工具参数 dict；policy 匹配 server:serverId:toolPattern=allow|deny。
        """
        logger.info("call_server_tool server=%s tool=%s", server, tool)
        return await ds.call_tool(server, tool, arguments=arguments)

    @server.tool()
    async def disconnect_mcp_server(server: str) -> McpDisconnectResult:
        """第七步：断开指定 MCP server 连接（空闲超时自动回收）。

        建议任务完成后显式调用释放资源。
        """
        logger.info("disconnect_mcp_server server=%s", server)
        return await ds.disconnect(server)

    return server


# ============================================================
# 主入口
# ============================================================

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
    parser.add_argument("--policy", default=None, help="safety policy 文件路径")
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

    if mode == "discover":
        discover_config = build_discover_config(args)
        logger.info("server start: mode=discover mock=%s policy=%s catalog=%s",
                     discover_config.mock,
                     "configured" if discover_config.policy_rules else "MISSING",
                     discover_config.catalog_path)
        if discover_config.policy_rules is None:
            logger.warning("未配置 safety policy，discover 连接与调用将全部拒绝（--policy 指定策略文件）")
        app = build_discover_app(discover_config, log_level=cast(LogLevel, level_name))
    else:
        openapi_config = build_openapi_config(args)
        logger.info("server start: mode=openapi region=%s mock=%s policy=%s credentials=%s",
                     openapi_config.region, openapi_config.mock,
                     "configured" if openapi_config.policy_rules else "MISSING",
                     "configured" if openapi_config.credentials else "none")
        if openapi_config.policy_rules is None:
            logger.warning("未配置 safety policy，execute_api 将拒绝所有执行（--policy 指定策略文件）")
        app = build_openapi_app(ToolService(openapi_config), log_level=cast(LogLevel, level_name))

    app.run("stdio")


if __name__ == "__main__":
    main()
