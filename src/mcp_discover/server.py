"""discover 模式 server 装配（MCP 协议层）。"""

import argparse
import functools
import logging
import os
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from common.elicit import PolicyConsent, ctx_elicit_fn
from common.types import (
    McpCallResult,
    McpConnectResult,
    McpDisconnectResult,
    McpServerListResult,
    McpServerResult,
    ServerToolResult,
    ServerToolsResult,
    ToolError,
)
from safety.policy_store import PolicyStore

from .config import DiscoverConfig
from .service import DiscoverService

logger = logging.getLogger("mcp_discover.server")

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
- 拒绝结果形如 {"ok": false, "reason": ...}，不要绕过，应改用被允许的 server/tool；
- 被拒连接/调用确属任务必需时：重新调用同一工具，server 会经 MCP elicitation
  向用户弹窗提议授予最小规则（如 "server:@huaweicloud/ecs=allow"）；用户确认后
  规则热生效，结果携带 `granted_rule` 字段，再次重试即可通过；
- 也可直接调用 `manage_policy(action="add", line=...)`：add/remove 前服务端先经
  elicitation 向用户确认，**改动热生效、无需重启 server**；客户端不支持 elicitation
  时（--elicitation auto 降级 / off 模式）退回约定：先向用户确认再调用；
  临时授权建议在任务完成后用 `manage_policy(action="remove", ...)` 回收。

## 其它

- endpoint 在真实模式下严格取自目录，不可覆盖（防目录外投毒）；
- 目标 server 工具清单为实时拉取，不落目录以免过期；
- 若目标 server 不存在或连接失败，检查 server id 是否匹配目录条目。
"""


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
    policy_store = PolicyStore(policy_file) if policy_file else None
    return DiscoverConfig(
        catalog_path=catalog_path,
        mock=mock,
        mock_base=mock_base,
        policy_store=policy_store,
        policy_rules=policy_store.rules() if policy_store else None,
        session_idle_timeout=idle_timeout,
        max_sessions=max_sessions,
    )


def build_discover_app(config: DiscoverConfig, *,
                       log_level: str = "INFO",
                       elicit_mode: str = "auto") -> MCPServer:
    ds = DiscoverService(config)
    consent_mode = elicit_mode

    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=INSTRUCTIONS_DISCOVER,
                       log_level=log_level)  # type: ignore[arg-type]

    def _consent(ctx: Context | None) -> PolicyConsent:
        assert ctx is not None, "Context injected by MCP framework"
        grant = functools.partial(ds.manage_policy, "add")
        return PolicyConsent(consent_mode, ctx_elicit_fn(ctx), grant)

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
    async def connect_mcp_server(server: str, ctx: Context | None = None
                                 ) -> McpConnectResult | ToolError:
        """第三步：连接指定 MCP server（过 safety policy）。

        policy 匹配 server 连接规则：server:serverId=allow|deny；
        真实模式 endpoint 严格取自目录；mock 模式指向 --mock-base。
        被 policy 拒绝时不要绕过：直接重试本工具，server 将经 elicitation
        向用户提议授予最小连接规则（用户确认后热生效并携带 granted_rule）。
        """
        logger.info("connect_mcp_server server=%s", server)
        result = await ds.connect(server)
        if isinstance(result, dict) and result.get("ok") is False:
            offer = ds.policy_denial_offer(server, denial_reason=result.get("reason"))
            if offer is not None:
                result = cast(McpConnectResult,
                              await _consent(ctx).offer_grant(offer, result))
        return result

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
                               arguments: dict[str, Any] | None = None,
                               ctx: Context | None = None) -> McpCallResult:
        """第六步：调用已连接 server 的指定工具（过 safety policy）。

        arguments 为工具参数 dict；policy 匹配 server:serverId:toolPattern=allow|deny。
        被 policy 拒绝时不要绕过：直接重试本工具，server 将经 elicitation
        向用户提议授予最小工具规则（用户确认后热生效并携带 granted_rule）。
        """
        logger.info("call_server_tool server=%s tool=%s", server, tool)
        result = await ds.call_tool(server, tool, arguments=arguments)
        if isinstance(result, dict) and result.get("ok") is False:
            offer = ds.policy_denial_offer(server, tool,
                                           denial_reason=result.get("reason"))
            if offer is not None:
                result = cast(McpCallResult,
                              await _consent(ctx).offer_grant(offer, result))
        return result

    @server.tool()
    async def disconnect_mcp_server(server: str) -> McpDisconnectResult:
        """第七步：断开指定 MCP server 连接（空闲超时自动回收）。

        建议任务完成后显式调用释放资源。
        """
        logger.info("disconnect_mcp_server server=%s", server)
        return await ds.disconnect(server)

    @server.tool()
    async def manage_policy(action: str, line: str | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        """管理 safety policy（list/add/remove），改动热生效并写回策略文件，无需重启 server。

        action=list 查看当前全部规则；action=add 新增规则（自动插到会遮蔽它的 deny
        规则之前，如 "server:@huaweicloud/ecs=allow"）；action=remove 按语义删除首个
        匹配规则。安全约定：add/remove 由服务端先经 elicitation 向用户确认，授予最小
        规则；客户端不支持 elicitation 时退回约定由调用方先行确认。临时授权用完即回收。
        未配置 policy 文件时本工具拒绝执行（不创建文件）。
        """
        if ((action or "").strip().lower() in ("add", "remove")
                and (line or "").strip()):
            blocked = await _consent(ctx).gate_change(action, line or "")
            if blocked:
                return {"ok": False, "action": action, "reason": blocked}
        return ds.manage_policy(action, line=line)

    return server
