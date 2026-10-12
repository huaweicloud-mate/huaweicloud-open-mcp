"""discover 模式 server 装配（MCP 协议层）。"""

import argparse
import functools
import logging
from typing import Any, Literal, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from common.audit import AuditSink, _audit_write, sink_from_path
from common.deployment import (
    ENV_MAX_SESSIONS,
    ENV_SERVER_CATALOG,
    ENV_SESSION_IDLE_TIMEOUT,
    Deployment,
    resolve_deployment,
)
from common.elicit import (
    PERSISTENT_GRANT_TIERS,
    DiscoverGrantArg,
    PolicyConsent,
    apply_tier_disclosure,
    confirmed_grant_and_retry,
    ctx_elicit_fn,
    is_write_refusal,
    manage_policy_list_only,
    offer_grant_and_retry,
    parse_elicit_mode,
    resolve_assertion_choice,
)
from common.types import (
    ManagePolicyWire,
    McpCallResult,
    McpConnectWire,
    McpDisconnectResult,
    McpServerListWire,
    McpServerWire,
    ServerToolsWire,
    ServerToolWire,
    as_wire,
)
from safety import policy as safety_policy
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
- 被拒连接/调用确属任务必需时：先经对话/交互式问询（如 question 工具）向
  用户确认所选档位；确认后重传本工具并传 grant=<档位>（仅 --elicitation
  off 模式生效，auto/required 下被忽略——服务端在同次调用内按所选档位
  授予并直接执行）：call_server_tool 支持 api_session=最小工具规则（会话内，
  本次会话内持续放行该工具，重启即失；单工具重复使用首选）/
  product=服务级全工具规则（会话内，覆盖该 server 全部工具，重启即失）/
  api=最小工具规则（一次性，用后即焚；仅明确单次使用时选择；confirm=true
  为其历史等价写法）；connect_mcp_server 仅 confirm=true（会话内连接级
  授予，后续连接免确认）；持久/批量/自定义
  规则由操作员编辑 policy 文件（`--policy` 指定，热生效、无需重启）；
  部署开启 elicitation（--elicitation auto/required）时直接重试被拒工具：服务端
  经 MCP elicitation 弹出同样的提议（口径与 grant 档位同构），用户确认后自动
  重放一次并直接返回连接/调用
  结果（无需再次调用）；重试仍被拒时结果附已授予规则上下文，再次调用将重新进入
  确认授予流程；
  默认 off 或客户端不支持 elicitation 时，拒绝原因会附带同样的兜底指引，
  按指引问询确认后再授予；
- manage_policy 仅保留 action=list（读）——add/remove 已从工具面收敛，防止
  agent 未经用户确认自授规则；授予路径：off 模式经被拒工具传 grant=<档位>
  或 confirm=true，auto/required 经被拒工具的 elicitation 提议，持久/批量/
  自定义规则由操作员直接编辑 policy 文件（`--policy` 指定，热生效、无需重启）；
- 规则四档 scope：`once` 一次性（仅放行下一次执行，用后即焚，重启即失）/
  `session` 会话内（缺省，本次 code agent 会话，重启即失、无需回收）/
  `temporary` 临时（内存 + ttl_seconds 自动过期，缺省 3600s）/ `permanent` 永久
  （写入策略文件，跨重启）；仅 permanent 落盘，授予最小权限请优先用 once/会话内。
  会话内规则随连接/进程结束自动失效（文件编辑不影响在途会话规则）；
  temporary/permanent 与 remove 仅为操作员侧语义（文件编辑/内部接缝）。
  注意「会话内」指 code agent 会话（AI 客户端与 gateway 的一次连接），**非**到远端
  MCP server 的连接会话——远端连接由空闲回收/LRU 管理，断开或回收后 session 档
  授权仍在，直到本次 code agent 会话结束。

## 其它

- endpoint 在真实模式下严格取自目录，不可覆盖（防目录外投毒）；
- 目标 server 工具清单为实时拉取，不落目录以免过期；
- 若目标 server 不存在或连接失败，检查 server id 是否匹配目录条目。
"""


def build_discover_config(args: argparse.Namespace, dep: Deployment | None = None, *,
                          policy_store: PolicyStore | None = None,
                          audit_sink: AuditSink | None = None) -> DiscoverConfig:
    """discover 模式 DiscoverConfig 构建（internal seam；共享旋钮经 Deployment）。"""
    dep = dep or resolve_deployment(args)
    env = dep.env
    catalog_path = env.get(ENV_SERVER_CATALOG) or DiscoverConfig.catalog_path
    idle_timeout = int(env.get(ENV_SESSION_IDLE_TIMEOUT,
                               str(DiscoverConfig.session_idle_timeout)))
    max_sessions = int(env.get(ENV_MAX_SESSIONS,
                               str(DiscoverConfig.max_sessions)))
    if policy_store is None:
        policy_store = PolicyStore(dep.policy_file) if dep.policy_file else None
    return DiscoverConfig(
        catalog_path=catalog_path,
        mock=dep.mock,
        mock_base=dep.mock_base,
        policy_store=policy_store,
        policy_rules=policy_store.rules() if policy_store else None,
        session_idle_timeout=idle_timeout,
        max_sessions=max_sessions,
        audit_sink=audit_sink if audit_sink is not None else sink_from_path(dep.audit_file),
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
    consent_mode = parse_elicit_mode(consent_mode)

    def _consent(ctx: Context | None, minimal_scope: str = "session", *,
                 source: Literal["assertion", "elicit"] = "elicit"
                 ) -> PolicyConsent:
        assert ctx is not None, "Context injected by MCP framework"
        # choice→scope 映射内聚于 PolicyConsent；minimal_scope 由调用点注入：
        # connect 授予为会话内（连接是持续态）；call_tool 传 once（一次性，
        # 一次用户确认只放行一次代发调用）。api_session 与 product 选择恒为
        # session（api_session=最小工具规则会话内；product=服务级全工具规则
        # server:X:*=allow，会话内生效）。
        # source 为审计溯源：assertion=off 档 grant/confirm 参数断言式授予 /
        # elicit=协议问询确认授予（包装层分支单点决定，NDJSON 可区分 provenance）
        grant = functools.partial(ds.manage_policy, "add", source=source)
        return PolicyConsent(consent_mode, ctx_elicit_fn(ctx), grant,
                             minimal_scope=minimal_scope)

    @server.tool()
    def list_mcp_servers(category: str | None = None,
                         keyword: str | None = None) -> McpServerListWire:
        """第一步：列出华为云 MCP server 目录（含中文名/分类/认证模型）。

        根据用户任务语义用 keyword 按 server 名/中文名/描述搜索；
        不确定时先不加过滤全量浏览。
        """
        logger.info("list_mcp_servers category=%s keyword=%s", category or "-", keyword or "-")
        return as_wire(ds.list_servers(category=category, keyword=keyword),
                       McpServerListWire)

    @server.tool()
    def get_mcp_server(server: str) -> McpServerWire:
        """第二步：确认单个 MCP server 详情（endpoint/传输层/认证方式/描述）。

        选定后调用 connect_mcp_server 建立连接。
        """
        logger.info("get_mcp_server server=%s", server)
        return as_wire(ds.get_server(server), McpServerWire)

    @server.tool()
    async def connect_mcp_server(server: str, confirm: bool = False,
                                 ctx: Context | None = None
                                 ) -> McpConnectWire:
        """第三步：连接指定 MCP server（过 safety policy）。

        policy 匹配 server 连接规则：server:serverId=allow|deny；
        真实模式 endpoint 严格取自目录；mock 模式指向 --mock-base。
        被 policy 拒绝时不要绕过：经对话/交互式问询向用户确认后重传本工具并传
        confirm=true——服务端在同次调用内授予会话内连接规则并直接连接（后续
        连接免确认）；更宽档位经提议表单（auto/required）或操作员 policy 文件。开启 elicitation 时直接
        重试本工具，server 经 elicitation 向用户
        提议授予最小连接规则（会话内档，弹窗如实陈述实际授予档位）；用户确认后
        服务端自动重放一次并直接返回连接结果，无需再次调用；重试仍被拒时结果
        附已授予规则上下文。
        """
        logger.info("connect_mcp_server server=%s", server)
        result = await ds.connect(server)
        if isinstance(result, dict) and result.get("ok") is False:
            offer = ds.policy_denial_offer(server, denial_reason=result.get("reason"))
            if offer is not None:

                def _retry() -> Any:
                    return ds.connect(server)

                # 授予成功后内联重放恰一次（拒绝点在会话副作用之前，重试不双建会话）；
                # 重放经 ds.connect 的 @audited 自动按尝试入账
                if consent_mode == "off" and confirm:
                    result = await confirmed_grant_and_retry(
                        _consent(ctx, source="assertion"), offer, result, _retry)
                else:
                    result = await offer_grant_and_retry(
                        _consent(ctx, source="elicit"), offer, result, _retry)
        # 无会话档披露分支（对比 call_tool 成功路径 elif，round-12 执行期
        # 核清）：connect 路径无 authorize 门（service 侧仅 _check_policy
        # 廉价检，once 规则在 connect 级不焚毁）——「残留 once 已随本次
        # 放行焚毁、所请求会话档未落地」叙事结构性不可达；且披露注文本
        # 为 grant 参数专属（「再次传同一 grant 档位」），对 confirm-only
        # 的 connect 不可交付（其 confirm 即直接授予会话档）。披露的适用
        # 性由授权门结构承载：有门才有披露，connect 无门故无披露。
        return as_wire(result, McpConnectWire)

    @server.tool()
    async def list_server_tools(server: str, search: str | None = None,
                                limit: int = 20, offset: int = 0
                                ) -> ServerToolsWire:
        """第四步：已连接 MCP server 的工具摘要列表（两级读取第一步）。

        返回工具名+首行描述+必填参数名；用 search/limit/offset 收窄；
        禁止无过滤全量拉取。选定后用 get_server_tool 读完整 schema。
        """
        logger.info("list_server_tools server=%s search=%s limit=%d offset=%d",
                    server, search or "-", limit, offset)
        return as_wire(await ds.list_tools(server, search=search, limit=limit,
                                           offset=offset), ServerToolsWire)

    @server.tool()
    async def get_server_tool(server: str, tool: str) -> ServerToolWire:
        """第五步：获取单个工具的完整 schema（两级读取第二步）。

        仅取调用目标一个工具，防上下文暴涨；超 16KB 自动截断。
        """
        logger.info("get_server_tool server=%s tool=%s", server, tool)
        return as_wire(await ds.get_tool(server, tool), ServerToolWire)

    @server.tool()
    async def call_server_tool(server: str, tool: str,
                               arguments: dict[str, Any] | None = None,
                               confirm: bool = False,
                               grant: DiscoverGrantArg | None = None,
                               ctx: Context | None = None) -> McpCallResult:
        """第六步：调用已连接 server 的指定工具（过 safety policy）。

        arguments 为工具参数 dict（grant/confirm 为网关确认参数，不进入
        arguments 容器，平铺在工具入参顶层）；
        policy 匹配 server:serverId:toolPattern=allow|deny。
        被 policy 拒绝时不要绕过：先经对话/交互式问询向用户确认所选档位，
        确认后重传本工具并传 grant=<档位>（仅 --elicitation off 模式生效，
        auto/required 下被忽略——服务端在同次调用内按所选档位授予并直接
        执行）：grant="api_session"=最小工具规则（会话内，本次会话内持续
        放行该工具，重启即失；单工具重复使用首选）/ grant="product"=服务级
        全工具规则如 "server:@huaweicloud/ecs:*=allow"（会话内放行该 server
        全部工具，重启即失）/ grant="api"=最小工具规则（一次性，用后即焚；
        仅明确单次使用时选择）；confirm=true 等价 grant="api"（历史兼容），
        grant 与 confirm 并传时以 grant 为准（discover 侧无 readonly 档）。
        开启 elicitation 时直接重试本工具，server 经 elicitation 向用户
        弹窗四选一提议授予（口径与 grant 档位同构，首次问询即推荐会话档；
        readonly 只读档仅 openapi 侧提供，discover 拒绝不含只读规则集）；
        用户确认后服务端自动重放一次并直接返回调用结果，无需再次调用；重试仍
        被拒时结果附已授予规则上下文，再次调用将重新进入确认授予流程；默认
        off 或客户端
        不支持 elicitation 时，拒绝原因附带同样的兜底指引（先经交互式问询向
        用户确认所选档位，持久/自定义规则由操作员编辑 policy 文件授予）。
        """
        logger.info("call_server_tool server=%s tool=%s", server, tool)
        result = await ds.call_tool(server, tool, arguments=arguments)
        # 断言归一化单点：grant 优先；confirm=true ≡ grant="api"
        choice = resolve_assertion_choice(grant, confirm)
        if isinstance(result, dict) and result.get("ok") is False:
            offer = ds.policy_denial_offer(server, tool,
                                           denial_reason=result.get("reason"))
            if offer is not None:

                def _retry() -> Any:
                    return ds.call_tool(server, tool, arguments=arguments)

                if consent_mode == "off" and choice is not None:
                    # 授予成功后内联重放恰一次（once 在重试的授权门焚毁；
                    # 重放经 ds.call_tool 的 @audited 自动按尝试入账——
                    # 重试成功/仍拒/上游失败均留痕，授予失败重试未跑无第二条）
                    result = await confirmed_grant_and_retry(
                        _consent(ctx, minimal_scope="once", source="assertion"),
                        offer, result, _retry, choice)
                else:
                    result = await offer_grant_and_retry(
                        _consent(ctx, minimal_scope="once", source="elicit"),
                        offer, result, _retry)
        elif (consent_mode == "off" and choice in PERSISTENT_GRANT_TIERS
                and isinstance(result, dict) and result.get("ok") is True):
            # 会话档断言未被消费（残留 once 已随本次执行焚毁）：调用后策略
            # 复查——持久层（session/temporary/permanent，含更宽规则覆盖/
            # 大小写变体/遮蔽语义）仍拒才披露，覆盖则静默（与 authorize 同
            # 口径）；无 store（快照模式）恒静默（无 overlay/once 概念，
            # 披露指引不可兑现）
            store = ds.config.policy_store
            still_denied = (safety_policy.check_server(store.rules(), server, tool)
                            if store is not None else None)
            result = cast(McpCallResult,
                          apply_tier_disclosure(result, choice, still_denied))
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
        async def manage_policy(action: str, line: str | list[str] | None = None,
                                scope: str | None = None,
                                ttl_seconds: int | None = None) -> ManagePolicyWire:
            """查看 safety policy（action=list，读面；add/remove 已从工具面收敛）。

            add/remove 结构化拒绝——防止 agent 未经用户确认自授规则。授予路径：
            off 模式经 call_server_tool 传 grant=<档位>（api/api_session/product）
            或 confirm=true（等价 grant=api）；connect_mcp_server 仅
            confirm=true（会话内连接级档）；auto/required 经被拒工具的
            elicitation 提议；持久/批量/自定义规则由操作员直接编辑 policy
            文件（--policy 指定，热生效、无需重启）。
            action=list 返回当前全部规则（结构化 rules 含 scope/expires_in +
            文件全文）；未配置 policy 文件时本工具拒绝执行（不创建文件）。
            """
            result = manage_policy_list_only(
                ds.manage_policy, action, line=line,
                scope=scope, ttl_seconds=ttl_seconds)
            if is_write_refusal(action, result):
                # agent 自授尝试恰是审计要捕捉的安全事件（拒绝先于 @audited
                # service 方法返回，不经此补记则 NDJSON 无痕）——openapi 先例
                _audit_write(ds, "manage_policy",
                             {"action": action, "line": line, "scope": scope,
                              "ttl_seconds": ttl_seconds}, result)
            return as_wire(result, ManagePolicyWire)
