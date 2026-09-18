"""部署装配单一入口（external seam）：(modes, args, env) → 装配完成的 MCPServer。

此前装配知识散落 5 文件（cli 分发 + 三 mode server builder + composite 事后
覆写）：PolicyStore 构建两次再 mutate 覆写、mock/policy/audit 解析三处逐字
重复、include_manage_policy 去重决策跨 3 文件。本模块收拢为唯一 external
seam——per-mode ``build_X_config``/``build_X_app`` 降为 internal seams
（配置解析测试仍可穿越，dep=None 原口），``register_*_tools`` 保持公开
（InMemoryTransport 测试 seam）。

共享控制面（PolicyStore/AuditSink）在此构建一次、经构造注入各 mode builder
——共享不变量由构造保证而非事后覆写。mode server 模块按分支 lazy import
（单模式启动不付其它模式的 import 成本，延续 cli 旧语义）。
"""

from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from mcp.server.mcpserver import MCPServer

from common.audit import sink_from_path
from common.deployment import resolve_deployment
from common.sessions import SessionScopeMiddleware, current_session_key
from safety.policy_store import PolicyStore

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcp_data.service import DataService
    from mcp_discover.service import DiscoverService
    from mcp_openapi.hints import Hints
    from mcp_openapi.service import ToolService

logger = logging.getLogger("huaweicloud_open_mcp.deployment")

HEALTHZ_PATH = "/healthz"

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

HTTP_TRANSPORT_NOTE = """\
## 传输与会话边界（Streamable HTTP）

本部署经 HTTP 单进程服务多客户端会话，safety policy 会话语义如下：
- session 档规则仅对当前 MCP 连接会话生效——每个客户端连接持有独立的会话
  命名空间，互相不可见；连接断开重连即新会话，原会话内的 session/once 授予
  不随行（如需跨会话复用请由用户重新确认后再次授予）；
- once 档规则在同会话内的下一次执行后焚毁；
- temporary 档在 TTL 内对当前会话生效；
- permanent 档写入策略文件，跨会话、跨重启生效。
无 MCP 会话身份的请求（modern 单交换协议）无法授予 session/temporary/once
档规则（结构化拒绝）；如需持久放行请改用 permanent 档。"""


def _register_healthz(server: MCPServer) -> None:
    """HTTP 档健康检查端点（K8s liveness/readiness 直用；custom_route 随
    streamable_http_app 进路由表，零自研 HTTP 层）。"""
    from starlette.responses import JSONResponse

    @server.custom_route(HEALTHZ_PATH, methods=["GET"])
    async def healthz(request: object) -> JSONResponse:
        return JSONResponse({"ok": True, "transport": "http"})


def merge_instructions(modes: list[str], hints: "Hints | None") -> str:
    """合并各模式指引：统一 H1 头，各模式正文降级为 H2 段。

    hints 为 None 时按空 hints 处理（仅 openapi 段消费）。
    """
    from mcp_data.server import INSTRUCTIONS_DATA
    from mcp_discover.server import INSTRUCTIONS_DISCOVER
    from mcp_openapi.server import build_instructions

    openapi_hints: Hints = hints if hints is not None else _empty_hints()
    header = "# 华为云 Open MCP 使用指引（组合模式：" + " + ".join(modes) + "）"
    sections: list[tuple[str, str]] = []
    if "openapi" in modes:
        sections.append(("openapi（OpenAPI 直连）", build_instructions(openapi_hints)))
    if "discover" in modes:
        sections.append(("discover（MCP server 发现连接）", INSTRUCTIONS_DISCOVER))
    if "data" in modes:
        sections.append(("data（数据分析）", INSTRUCTIONS_DATA))
    body = "\n\n".join(
        "## 模式：" + title + "\n\n" + text.split("\n", 1)[1].lstrip()
        for title, text in sections)
    return header + "\n\n" + body + "\n"


def _empty_hints() -> "Hints":
    from mcp_openapi.hints import Hints
    return Hints.empty()


def _mode_instructions(mode: str, hints: "Hints | None") -> str:
    """单模式 instructions（与历史单模式 builder 逐字节一致）。"""
    if mode == "openapi":
        from mcp_openapi.server import build_instructions
        return build_instructions(hints)
    if mode == "discover":
        from mcp_discover.server import INSTRUCTIONS_DISCOVER
        return INSTRUCTIONS_DISCOVER
    if mode == "data":
        from mcp_data.server import INSTRUCTIONS_DATA
        return INSTRUCTIONS_DATA
    raise ValueError(f"未知模式: {mode}")


def build_app(modes: list[str], args: argparse.Namespace, *,
              env: "Mapping[str, str] | None" = None,
              log_level: str = "INFO",
              elicit_mode: str = "off",
              openapi_service: "ToolService | None" = None,
              discover_service: "DiscoverService | None" = None,
              data_service: "DataService | None" = None) -> MCPServer:
    """装配完成的 MCPServer：唯一 external seam（单模式与混装同一路径）。

    - 共享 PolicyStore/AuditSink 各构建一次，构造注入 per-mode builder
      （热更新与审计全局一致；single-adapter 测试可经 service 注入参数替换）；
    - env 可整体注入（测试），缺省 os.environ；
    - manage_policy 全局只注册一次（openapi 优先，discover 次之，data 无贡献）；
    - 单模式 instructions 与历史 builder 逐字节一致，混装经 merge_instructions。
    """
    dep = resolve_deployment(args, env)
    is_http = dep.transport == "http"
    # 会话键控装配（ADR-0003）：session_fn 注入与 middleware 注册在 build_app
    # 同一处配对——ambient 机制的知识单点；stdio 下 session_fn 恒 None =
    # 历史单桶语义（逐字节回归由结构保证）。strict_sessions 仅 HTTP 档置位
    # （I2 fail-closed：无会话身份的 session/temporary/once 写入结构化拒绝）。
    shared_store = (PolicyStore(dep.policy_file, session_fn=current_session_key,
                                strict_sessions=is_http)
                    if dep.policy_file else None)
    shared_sink = sink_from_path(dep.audit_file)

    svc: ToolService | None = openapi_service
    if svc is None and "openapi" in modes:
        from mcp_openapi.server import build_openapi_config
        from mcp_openapi.service import ToolService
        # 部署感知披露：data 工具已注册时 spill note 指引 query_data
        cfg = build_openapi_config(args, dep, data_enabled="data" in modes,
                                   policy_store=shared_store,
                                   audit_sink=shared_sink)
        svc = ToolService(cfg)

    discover_svc: DiscoverService | None = discover_service
    if discover_svc is None and "discover" in modes:
        from mcp_discover.server import build_discover_config
        from mcp_discover.service import DiscoverService
        # discover 模式不写审计（DiscoverConfig 无 audit_sink，口径与单模式一致）
        dcfg = build_discover_config(args, dep, policy_store=shared_store)
        discover_svc = DiscoverService(dcfg)

    data_svc: DataService | None = data_service
    if data_svc is None and "data" in modes:
        from mcp_data.server import build_data_config
        from mcp_data.service import DataService
        data_svc = DataService(build_data_config(args, dep,
                                                 audit_sink=shared_sink))

    hints = svc.config.hints if svc is not None else None
    instructions = (merge_instructions(modes, hints) if len(modes) > 1
                    else _mode_instructions(modes[0], hints))
    if is_http:
        # 传输会话边界说明只随 HTTP 档追加（stdio 文本逐字节不变，I7）
        instructions = (instructions.rstrip("\n") + "\n\n"
                        + HTTP_TRANSPORT_NOTE + "\n")
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=instructions,
                       log_level=log_level)  # type: ignore[arg-type]

    # 会话身份 middleware 无条件注册（stdio/InMemory 下 pass-through None；
    # 与上方 session_fn 注入构成单点配对，防装配漂移）
    server.middleware.append(SessionScopeMiddleware())
    if is_http:
        _register_healthz(server)
        logger.info("server transport: http host=%s port=%d path=%s "
                    "strict_sessions=true", dep.http_host, dep.http_port,
                    dep.http_path)
        if dep.http_host not in _LOOPBACK_HOSTS:
            logger.warning(
                "HTTP 绑定非 loopback 地址 %s：明文 HTTP 且无认证，能达端口者"
                "可以本部署 AK/SK 身份执行；建议置于反代/TLS 之后或保持 127.0.0.1",
                dep.http_host)

    if svc is not None:
        from mcp_openapi.server import register_openapi_tools
        _log_openapi_start(svc)
        register_openapi_tools(server, svc, consent_mode=elicit_mode)
    if discover_svc is not None:
        from mcp_discover.server import register_discover_tools
        _log_discover_start(discover_svc, elicit_mode,
                            include_manage_policy=svc is None)
        register_discover_tools(server, discover_svc, consent_mode=elicit_mode,
                                include_manage_policy=svc is None)
    if data_svc is not None:
        from mcp_data.server import register_data_tools
        logger.info("server start: mode=data audit=%s",
                    "configured" if data_svc.config.audit_sink else "none")
        register_data_tools(server, data_svc)
    return server


def _log_openapi_start(svc: "ToolService") -> None:
    """server start 日志（单模式/混装共用；policy 缺失警告随行）。"""
    cfg = svc.config
    logger.info("server start: mode=openapi region=%s mock=%s policy=%s credentials=%s spill=%s",
                cfg.region, cfg.mock,
                "configured" if cfg.policy_store else "MISSING",
                "configured" if cfg.credentials else "none",
                "off" if cfg.spill is None else str(cfg.spill.dir))
    if cfg.policy_store is None:
        logger.warning("未配置 safety policy，execute_api 将拒绝所有执行（--policy 指定策略文件）")


def _log_discover_start(svc: "DiscoverService", elicit_mode: str, *,
                        include_manage_policy: bool) -> None:
    cfg = svc.config
    logger.info("server start: mode=discover mock=%s policy=%s catalog=%s elicit=%s%s",
                cfg.mock,
                "configured" if cfg.policy_rules else "MISSING",
                cfg.catalog_path, elicit_mode,
                "" if include_manage_policy else " manage_policy=openapi")
    if cfg.policy_rules is None:
        logger.warning("未配置 safety policy，discover 连接与调用将全部拒绝（--policy 指定策略文件）")
