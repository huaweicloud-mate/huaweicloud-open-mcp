"""DiscoverService：discover 模式 7 工具的业务编排层。

接受 SessionManager / CatalogSource / safety policy 注入，
可从 server.py 的 MCP 装饰器直接调用，也可独立单测。
"""

import json
import logging
from typing import Any, Sequence, cast

from common.elicit import DenialOffer
from common.types import (
    McpCallResult,
    McpConnectResult,
    McpDisconnectResult,
    McpServerItem,
    McpServerListResult,
    McpServerResult,
    ServerToolResult,
    ServerToolsResult,
    ToolError,
)
from safety import policy as safety_policy

from . import catalog as discover_catalog
from .config import DiscoverConfig
from .manager import SessionManager
from .sdk import SdkSessionClient

logger = logging.getLogger("mcp_discover.service")

SCHEMA_TRUNCATE = 16384
RESULT_TRUNCATE = 65536


def _tool_summary(tool: dict[str, Any]) -> dict[str, Any]:
    desc = tool.get("description", "") or ""
    first_line = desc.split("\n")[0]
    schema = tool.get("inputSchema") or {}
    required: list[str] = list(schema.get("required", [])) if isinstance(schema, dict) else []
    return {"name": tool["name"], "description": first_line, "required": required}


def _search_tools(tools: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
    kw = keyword.lower()
    return [t for t in tools
            if kw in t["name"].lower() or kw in (t.get("description", "") or "").lower()]


def _safe_serialize(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return repr(obj)


def _to_mcp_server_item(entry: dict[str, Any]) -> McpServerItem:
    return McpServerItem(
        server=entry["id"],
        name=entry["name"],
        display_name=entry["display_name"],
        category=entry["category"],
        description=entry["description"],
        auth=entry["auth"],
        version=entry["version"],
        endpoint=entry["endpoint"],
    )


class DiscoverService:
    def __init__(self, config: DiscoverConfig, *,
                 catalog_source: discover_catalog.CatalogSource | None = None,
                 manager: SessionManager | None = None):
        self.config = config
        self.catalog = catalog_source or discover_catalog.LocalCatalogSource(config.catalog_path)
        self.manager = manager or SessionManager(
            client_factory=lambda: SdkSessionClient(),
            idle_timeout=config.session_idle_timeout,
            max_sessions=config.max_sessions,
        )

    def _effective_policy_rules(self) -> Sequence[safety_policy.PolicyRule] | None:
        """当前生效规则：注入 PolicyStore 时实时热加载，否则用启动快照。"""
        if self.config.policy_store is not None:
            return self.config.policy_store.rules()
        return self.config.policy_rules

    def _check_policy(self, server: str, tool: str | None = None) -> str | None:
        return safety_policy.check_server(self._effective_policy_rules(), server, tool)

    def _authorize(self, server: str, tool: str | None = None) -> str | None:
        """dispatch 前的原子授权门（once 规则首次放行即焚毁）。

        委托 PolicyStore.authorize_server；无 store（启动快照模式）时直通。
        """
        store = self.config.policy_store
        if store is None:
            return None
        return store.authorize_server(server, tool)

    def policy_denial_offer(self, server: str, tool: str | None = None,
                            denial_reason: str | None = None) -> DenialOffer | None:
        """server 规则拒绝且可授予时构造 elicitation 提议；其余返回 None。

        未配置 policy store（无可写文件）或放行 → None；
        denial_reason 非空时须与本方法复查结果一致（确保被增强的确实是
        policy 拒绝），不一致 → None。
        coarse_rule 仅 call_tool 路径提供（服务级全工具规则 server:X:*=allow，
        session 档授予选项）；connect 路径为 None——call 级规则匹配不到
        connect 检查，授予无意义。
        """
        if self.config.policy_store is None:
            return None
        err = self._check_policy(server, tool)
        if err is None:
            return None
        if denial_reason is not None and denial_reason != err:
            return None
        return DenialOffer(
            subject=f"{server}:{tool}" if tool else server,
            rule=safety_policy.grant_server_rule(server, tool), reason=err,
            coarse_rule=(safety_policy.grant_server_rule(server, "*")
                         if tool is not None else None))

    def manage_policy(self, action: str, line: str | None = None,
                      scope: str | None = None,
                      ttl_seconds: int | None = None) -> dict[str, Any]:
        """管理 safety policy（list/add/remove），改动即时生效无需重启。

        四档 scope：permanent（写策略文件，跨重启）/ temporary（内存 + TTL 自动
        过期，ttl_seconds 缺省 3600）/ session（内存，缺省档——本次 code agent
        会话，stdio 单进程下等价进程存活期，重启即失；非远端连接会话，断开/回收
        后授权仍在）/ once（内存，一次性——首次放行即焚毁）。
        remove 跨层先 overlay 后文件并回报 scope；不接受 scope/ttl_seconds。
        安全约定：调用方（Agent）应先经交互式问询（如 question 工具）向用户
        确认再 add/remove；审计日志强制记录。
        """
        action = (action or "").strip().lower()
        logger.info("manage_policy action=%s line=%s scope=%s ttl=%s",
                    action, line or "-", scope or "-", ttl_seconds)
        store = self.config.policy_store
        if store is None:
            return {"ok": False, "reason": (
                "未配置 safety policy 文件，manage_policy 不可用"
                "（--policy 或环境变量 HUAWEICLOUD_MCP_POLICY_FILE）")}
        if action == "list":
            return {"ok": True, "action": "list", "policy": store.text(),
                    "rules": [{"line": r.line, "scope": r.scope,
                               "expires_in": r.expires_in}
                              for r in store.list_rules()]}
        if action not in ("add", "remove"):
            return {"ok": False, "reason": f"未知 action: {action}（可选 list/add/remove）"}
        rule_text = (line or "").strip()
        if not rule_text:
            return {"ok": False, "reason": f"{action} 需要提供 line 参数（规则文本）"}
        if action == "remove":
            if scope is not None or ttl_seconds is not None:
                return {"ok": False, "reason": (
                    "remove 不接受 scope/ttl_seconds 参数"
                    "（跨层匹配：先会话/临时后文件，首个语义命中移除）")}
            result = store.remove_rule(rule_text)
        else:
            result = store.add_rule(rule_text, scope=scope, ttl_seconds=ttl_seconds)
        logger.info("manage_policy %s result=%s", action, "ok" if result.ok else "deny")
        out: dict[str, Any] = {"ok": result.ok, "action": action}
        if result.scope:
            out["scope"] = result.scope
        if result.reason:
            out["reason"] = result.reason
        out["policy"] = store.text()
        return out

    def _resolve_endpoint(self, entry: dict[str, Any]) -> str:
        if self.config.mock and self.config.mock_base:
            return self.config.mock_base
        if self.config.mock:
            return "http://127.0.0.1:8000/mcp"
        return cast(str, entry["endpoint"])

    # ---------- 1-2: server 目录 ----------

    def list_servers(self, category: str | None = None,
                     keyword: str | None = None) -> McpServerListResult:
        entries = discover_catalog.list_servers(self.catalog, category=category, keyword=keyword)
        items = [_to_mcp_server_item(e) for e in entries]
        return {"ok": True, "total": len(items), "servers": items}

    def get_server(self, server_id: str) -> McpServerResult | ToolError:
        entry = discover_catalog.get_server(self.catalog, server_id)
        if entry is None:
            return {"ok": False, "reason": f"MCP server {server_id} 未找到"}
        return {
            "ok": True,
            "server": entry["id"],
            "name": entry["name"],
            "display_name": entry["display_name"],
            "category": entry["category"],
            "description": entry["description"],
            "auth": entry["auth"],
            "version": entry["version"],
            "endpoint": entry["endpoint"],
        }

    # ---------- 3: 连接 ----------

    async def connect(self, server: str) -> McpConnectResult | ToolError:
        entry = discover_catalog.get_server(self.catalog, server)
        if entry is None:
            return {"ok": False, "reason": f"MCP server {server} 未找到"}

        err = self._check_policy(server)
        if err:
            logger.warning("connect %s policy=deny", server)
            return {"ok": False, "reason": err}

        endpoint = self._resolve_endpoint(entry)

        try:
            info = await self.manager.connect(server, endpoint)
        except Exception as exc:
            logger.warning("connect %s failed: %s", server, exc)
            return {"ok": False, "reason": f"连接失败: {exc}"}

        return {
            "ok": True,
            "server": server,
            "endpoint": endpoint,
            "protocol_version": info.protocol_version,
            "server_info": info.server_info,
        }

    # ---------- 4-5: 工具发现 ----------

    async def list_tools(self, server: str, search: str | None = None,
                         limit: int = 20, offset: int = 0) -> ServerToolsResult | ToolError:
        if not self.manager.is_connected(server):
            return {"ok": False, "reason": f"server {server} 未连接，请先调用 connect_mcp_server"}

        try:
            raw_tools = await self.manager.list_tools(server)
        except Exception as exc:
            logger.warning("list_tools %s failed: %s", server, exc)
            return {"ok": False, "reason": f"获取工具列表失败: {exc}"}

        summaries = [_tool_summary(t) for t in raw_tools]
        if search:
            filtered = _search_tools(raw_tools, search)
            summaries = [_tool_summary(t) for t in filtered]

        total = len(summaries)
        summaries = summaries[offset:offset + limit]
        return {
            "ok": True, "server": server,
            "total": total, "offset": offset, "limit": limit,
            "tools": cast(list[Any], summaries),
        }

    async def get_tool(self, server: str, tool: str) -> ServerToolResult | ToolError:
        if not self.manager.is_connected(server):
            return {"ok": False, "reason": f"server {server} 未连接，请先调用 connect_mcp_server"}

        try:
            raw_tools = await self.manager.list_tools(server)
        except Exception as exc:
            return {"ok": False, "reason": f"获取工具信息失败: {exc}"}

        hit: dict[str, Any] | None = None
        for t in raw_tools:
            if t.get("name", "").lower() == tool.lower():
                hit = t
                break
        if hit is None:
            return {"ok": False, "reason": f"工具 {tool} 未找到（server {server}）"}

        schema = hit.get("inputSchema", {})
        serialized = _safe_serialize(schema)
        truncated = len(serialized.encode("utf-8")) > SCHEMA_TRUNCATE
        if truncated:
            schema = {"_truncated": True, "_summary": hit.get("description", ""),
                      "_original_size_bytes": len(serialized.encode("utf-8"))}
        return {
            "ok": True, "server": server, "tool": hit.get("name", tool),
            "description": hit.get("description"),
            "inputSchema": schema,
            "truncated": truncated,
        }

    # ---------- 6: 调用 ----------

    async def call_tool(self, server: str, tool: str,
                        arguments: dict[str, Any] | None = None) -> McpCallResult:
        if not self.manager.is_connected(server):
            return {"ok": False, "reason": f"server {server} 未连接，请先调用 connect_mcp_server"}

        err = self._check_policy(server, tool)
        if err:
            logger.warning("call_tool %s:%s policy=deny", server, tool)
            return {"ok": False, "reason": err}

        gate_err = self._authorize(server, tool)   # 代发前消费一次性授权
        if gate_err:
            logger.warning("call_tool %s:%s policy=deny", server, tool)
            return {"ok": False, "reason": gate_err}

        try:
            result = await self.manager.call_tool(server, tool, arguments or {})
        except Exception as exc:
            logger.warning("call_tool %s:%s failed: %s", server, tool, exc)
            return {"ok": False, "reason": str(exc), "server": server, "tool": tool}

        serialized = _safe_serialize(result)
        is_truncated = len(serialized.encode("utf-8")) > RESULT_TRUNCATE
        if is_truncated:
            result = {"_truncated": True, "_text": serialized[:RESULT_TRUNCATE]}

        return {
            "ok": True, "server": server, "tool": tool,
            "result": result,
            "truncated": is_truncated,
        }

    # ---------- 7: 断开 ----------

    async def disconnect(self, server: str) -> McpDisconnectResult:
        released = await self.manager.disconnect(server)
        return {"ok": True, "server": server, "released": released}
