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
- 被拒连接/调用确属任务必需时：先经对话/交互式问询（如 question 工具）向用户确认，再调用
  `manage_policy(action="add", line=...)` 授予规则（如 "server:@huaweicloud/ecs=allow"，
  或服务级全工具 "server:@huaweicloud/ecs:*=allow"），规则热生效后重试即可通过；
  call_tool 问询按四选一口径：api=最小工具规则（一次性，用后即焚）/
  api_session=最小工具规则（会话内，本次会话内持续放行该工具，重启即失）/
  product=服务级全工具规则（会话内，覆盖该 server 全部工具，重启即失）/ none=不授予；
  connect 为单一确认（会话内连接级授予）；
  部署开启 elicitation（--elicitation auto/required）时重新调用被拒工具，服务端会
  经 MCP elicitation 弹出同样的提议（结果携带 `granted_rule` 字段）；
  默认 off 或客户端不支持 elicitation 时，拒绝原因会附带同样的兜底指引，
  按指引问询确认后再授予；
- 也可直接调用 `manage_policy`：**改动热生效、无需重启 server**；默认
  （--elicitation off）无弹窗，务必先向用户确认再调用；开启 elicitation 后
  add/remove 由服务端先经 elicitation 向用户确认；
- 规则四档 scope：`once` 一次性（仅放行下一次执行，用后即焚，重启即失）/
  `session` 会话内（缺省，本次 code agent 会话，重启即失、无需回收）/
  `temporary` 临时（内存 + ttl_seconds 自动过期，缺省 3600s）/ `permanent` 永久
  （写入策略文件，跨重启）；仅 permanent 落盘，授予最小权限请优先用 once/会话内/临时；
  `remove` 跨层回收（先会话/临时后文件，首个语义命中移除）。
  注意「会话内」指 code agent 会话（AI 客户端与 gateway 的一次连接），**非**到远端
  MCP server 的连接会话——远端连接由空闲回收/LRU 管理，断开或回收后 session 档
  授权仍在，直到本次 code agent 会话结束。

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
                       elicit_mode: str = "off") -> MCPServer:
    ds = DiscoverService(config)
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=INSTRUCTIONS_DISCOVER,
                       log_level=log_level)  # type: ignore[arg-type]
    register_discover_tools(server, ds, consent_mode=elicit_mode)
    return server


def register_discover_tools(server: MCPServer, ds: DiscoverService, *,
                            consent_mode: str,
                            include_manage_policy: bool = True) -> None:
    """注册 discover 模式 8 工具（混装装配复用；instructions 由 builder 自持）。

    include_manage_policy：混装时 manage_policy 全局只注册一次（由 composite 决定归属）。
    """

    def _consent(ctx: Context | None, minimal_scope: str = "session") -> PolicyConsent:
        assert ctx is not None, "Context injected by MCP framework"
        # choice→scope 映射内聚于 PolicyConsent；minimal_scope 由调用点注入：
        # connect 授予为会话内（连接是持续态）；call_tool 传 once（一次性，
        # 一次用户确认只放行一次代发调用）。api_session 与 product 选择恒为
        # session（api_session=最小工具规则会话内；product=服务级全工具规则
        # server:X:*=allow，会话内生效）。
        grant = functools.partial(ds.manage_policy, "add")
        return PolicyConsent(consent_mode, ctx_elicit_fn(ctx), grant,
                             minimal_scope=minimal_scope)

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
        向用户提议授予最小连接规则（用户确认后热生效并携带 granted_rule）；
        未开启 elicitation 时拒绝原因附带兜底指引（先经交互式问询向用户
        确认，再经 manage_policy 授予）。
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
        向用户弹窗四选一提议授予（用户确认后热生效并携带 granted_rule）：
        api=最小工具规则（一次性，用后即焚）/ api_session=最小工具规则（会话内，
        本次会话内持续放行该工具，重启即失）/ product=服务级全工具规则如
        "server:@huaweicloud/ecs:*=allow"（会话内放行该 server 全部工具，
        重启即失）/ none=不授予；默认 off 或客户端不支持 elicitation 时，
        拒绝原因附带同样的兜底指引（先经交互式问询向用户确认，再经
        manage_policy 授予）。
        """
        logger.info("call_server_tool server=%s tool=%s", server, tool)
        result = await ds.call_tool(server, tool, arguments=arguments)
        if isinstance(result, dict) and result.get("ok") is False:
            offer = ds.policy_denial_offer(server, tool,
                                           denial_reason=result.get("reason"))
            if offer is not None:
                result = cast(McpCallResult,
                              await _consent(ctx, minimal_scope="once").offer_grant(offer, result))
        return result

    @server.tool()
    async def disconnect_mcp_server(server: str) -> McpDisconnectResult:
        """第七步：断开指定 MCP server 连接（空闲超时自动回收）。

        建议任务完成后显式调用释放资源。
        """
        logger.info("disconnect_mcp_server server=%s", server)
        return await ds.disconnect(server)

    if include_manage_policy:
        @server.tool()
        async def manage_policy(action: str, line: str | None = None,
                                scope: str | None = None,
                                ttl_seconds: int | None = None,
                                ctx: Context | None = None) -> dict[str, Any]:
            """管理 safety policy（list/add/remove），改动热生效、无需重启 server。

            四档 scope：once 一次性（仅放行下一次执行，用后即焚，重启即失）/
            session 会话内（缺省，本次 code agent 会话，重启即失、无需回收）/
            temporary 临时（内存 + ttl_seconds 自动过期，缺省 3600s）/ permanent 永久
            （写入策略文件，跨重启）。仅 permanent 落盘。「会话内」指 code agent 会话
            （AI 客户端与 gateway 的一次连接），非到远端 MCP server 的连接会话——远端
            连接断开或空闲回收后 session 档授权仍在。
            action=list 查看当前全部规则（结构化 rules 含 scope/expires_in + 文件全文）；
            action=add 新增规则（自动插到会遮蔽它的 deny 规则之前，如
            "server:@huaweicloud/ecs=allow"）；action=remove 按语义移除首个匹配规则
            （跨层：先会话/临时后文件；不接受 scope/ttl_seconds）。
            安全约定：先经交互式问询（如 question 工具）向用户确认再 add/remove；开启
            elicitation 时由服务端弹窗确认，未开启/客户端不支持时由调用方自行完成问询确认。
            未配置 policy 文件时本工具拒绝执行（不创建文件）。
            """
            if ((action or "").strip().lower() in ("add", "remove")
                    and (line or "").strip()):
                blocked = await _consent(ctx).gate_change(action, line or "")
                if blocked:
                    return {"ok": False, "action": action, "reason": blocked}
            return ds.manage_policy(action, line=line, scope=scope, ttl_seconds=ttl_seconds)
