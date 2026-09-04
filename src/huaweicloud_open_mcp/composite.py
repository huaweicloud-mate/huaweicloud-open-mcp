"""组合模式装配（--mode 逗号多值混用，第4层）。

职责：共享 PolicyStore/AuditSink（一次加载、热更新一致）、合并各模式
instructions、逐模式注册工具、manage_policy 全局去重（openapi 优先，
discover 次之；data 无贡献）。单模式不经过本模块（cli 直达 build_X_app）。
"""

from __future__ import annotations

import argparse
import logging
import os

from mcp.server.mcpserver import MCPServer

from common.audit import sink_from_path
from mcp_data.server import INSTRUCTIONS_DATA, build_data_config, register_data_tools
from mcp_data.service import DataService
from mcp_discover.server import (
    INSTRUCTIONS_DISCOVER,
    build_discover_config,
    register_discover_tools,
)
from mcp_discover.service import DiscoverService
from mcp_openapi.gate import Gate
from mcp_openapi.hints import Hints
from mcp_openapi.server import (
    build_instructions,
    build_openapi_config,
    register_openapi_tools,
)
from mcp_openapi.service import ToolService
from safety.policy_store import PolicyStore

logger = logging.getLogger("huaweicloud_open_mcp.composite")


def merge_instructions(modes: list[str], gate: "Gate | None",
                       hints: "Hints | None") -> str:
    """合并各模式指引：统一 H1 头，各模式正文降级为 H2 段（openapi 段 gate-aware）。

    gate/hints 为 None 时按不限制/空 hints 处理（仅 openapi 段消费）。
    """
    openapi_gate: Gate = gate if gate is not None else Gate.unrestricted()
    openapi_hints: Hints = hints if hints is not None else Hints.empty()
    header = "# 华为云 Open MCP 使用指引（组合模式：" + " + ".join(modes) + "）"
    sections: list[tuple[str, str]] = []
    if "openapi" in modes:
        sections.append(("openapi（OpenAPI 直连）", build_instructions(openapi_gate, openapi_hints)))
    if "discover" in modes:
        sections.append(("discover（MCP server 发现连接）", INSTRUCTIONS_DISCOVER))
    if "data" in modes:
        sections.append(("data（数据分析）", INSTRUCTIONS_DATA))
    body = "\n\n".join(
        "## 模式：" + title + "\n\n" + text.split("\n", 1)[1].lstrip()
        for title, text in sections)
    return header + "\n\n" + body + "\n"


def build_composite_app(modes: list[str], args: argparse.Namespace, *,
                        log_level: str = "INFO", elicit_mode: str = "off",
                        openapi_service: ToolService | None = None,
                        discover_service: DiscoverService | None = None,
                        data_service: DataService | None = None) -> MCPServer:
    """混装装配：单 MCPServer 同时注册所选模式工具集。

    服务实例可注入（测试路径）；未注入时经 build_X_config(args) 构建并以
    共享 PolicyStore/AuditSink 覆盖（生产路径，热更新与审计全局一致）。
    """
    policy_file = (getattr(args, "policy", None)
                   or os.environ.get("HUAWEICLOUD_MCP_POLICY_FILE"))
    audit_file = (getattr(args, "audit_file", None)
                  or os.environ.get("HUAWEICLOUD_MCP_AUDIT_FILE"))
    shared_store = PolicyStore(policy_file) if policy_file else None
    shared_sink = sink_from_path(audit_file)

    svc: ToolService | None = None
    ds: DiscoverService | None = None
    das: DataService | None = None

    if "openapi" in modes:
        if openapi_service is not None:
            svc = openapi_service
        else:
            openapi_cfg = build_openapi_config(args)
            if shared_store is not None:
                openapi_cfg.policy_store = shared_store
                openapi_cfg.policy_rules = shared_store.rules()
            if shared_sink is not None:
                openapi_cfg.audit_sink = shared_sink
            svc = ToolService(openapi_cfg)
    if "discover" in modes:
        if discover_service is not None:
            ds = discover_service
        else:
            discover_cfg = build_discover_config(args)
            if shared_store is not None:
                discover_cfg.policy_store = shared_store
                discover_cfg.policy_rules = shared_store.rules()
            # discover 模式不写审计（DiscoverConfig 无 audit_sink，口径与单模式一致）
            ds = DiscoverService(discover_cfg)
    if "data" in modes:
        if data_service is not None:
            das = data_service
        else:
            data_cfg = build_data_config(args)
            if shared_sink is not None:
                data_cfg.audit_sink = shared_sink
            das = DataService(data_cfg)

    gate = svc.config.gate if svc is not None else None
    hints = svc.config.hints if svc is not None else None
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=merge_instructions(modes, gate, hints),
                       log_level=log_level)  # type: ignore[arg-type]

    if svc is not None:
        register_openapi_tools(server, svc, consent_mode=elicit_mode)
    if ds is not None:
        register_discover_tools(server, ds, consent_mode=elicit_mode,
                                include_manage_policy=svc is None)
    if das is not None:
        register_data_tools(server, das)
    return server
