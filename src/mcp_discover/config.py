"""DiscoverConfig：MCP server 发现模式的配置。"""

import logging
from dataclasses import dataclass
from typing import Sequence

from safety.policy import PolicyRule
from safety.policy_store import PolicyStore

logger = logging.getLogger("mcp_discover.config")

ENV_CATALOG = "HUAWEICLOUD_MCP_SERVER_CATALOG"
ENV_SESSION_IDLE_TIMEOUT = "HUAWEICLOUD_MCP_SESSION_IDLE_TIMEOUT"
ENV_MAX_SESSIONS = "HUAWEICLOUD_MCP_MAX_SESSIONS"

DEFAULT_IDLE_TIMEOUT = 300
DEFAULT_MAX_SESSIONS = 5


@dataclass
class DiscoverConfig:
    catalog_path: str | None = None  # 未配置时由 LocalCatalogSource 经 config_path 延迟解析
    mock: bool = False
    mock_base: str | None = None
    policy_rules: Sequence[PolicyRule] | None = None
    policy_store: PolicyStore | None = None
    session_idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    max_sessions: int = DEFAULT_MAX_SESSIONS
