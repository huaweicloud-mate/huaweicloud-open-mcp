"""部署旋钮归一（Deployment）：argv+env → 跨模式共享值对象，装配归一单点。

此前 mock 三元组/policy/audit 的「args 优先 env 兜底」解析在 openapi/discover
两个 builder 逐字重复、data 再抄一遍 audit，~12 处 getattr Namespace 兼容补丁
散布三文件，env 名以内联字符串散布 62 处。本模块收拢：

- env 名常量单点（全部 HUAWEICLOUD_MCP_* 名唯一定义处）；
- ``Deployment`` 值对象：跨模式共享旋钮 + env 映射（mode 专属旋钮经
  ``dep.env`` 读取，测试可整体注入 env 而不必 monkeypatch os.environ）；
- ``resolve_deployment(args, env)``：归一函数，getattr 安全（测试可传裸
  Namespace）；mock 布尔解析（"1"/"true"/"yes"）与 policy/audit 回退链
  只此一份。

mode 专属旋钮（hints/deprecated/entity/corrections/auth_demote/spill/catalog
等）的解析留在各 mode builder——共享的是跨模式旋钮纪律，不是 mode 语义。
"""

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# ---------- env 名常量（跨模式 + mode 专属，唯一定义处） ----------

ENV_MODE = "HUAWEICLOUD_MCP_MODE"
ENV_MOCK = "HUAWEICLOUD_MCP_MOCK"
ENV_MOCK_BASE = "HUAWEICLOUD_MCP_MOCK_BASE"
ENV_MOCK_PASSTHROUGH = "HUAWEICLOUD_MCP_MOCK_PASSTHROUGH"
ENV_POLICY_FILE = "HUAWEICLOUD_MCP_POLICY_FILE"
ENV_AUDIT_FILE = "HUAWEICLOUD_MCP_AUDIT_FILE"
ENV_REGION = "HUAWEICLOUD_MCP_REGION"
ENV_LOG_LEVEL = "HUAWEICLOUD_MCP_LOG_LEVEL"
ENV_ELICIT = "HUAWEICLOUD_MCP_ELICIT"
ENV_SPILL_DIR = "HUAWEICLOUD_MCP_SPILL_DIR"
ENV_OPENAPI_HINTS = "HUAWEICLOUD_MCP_OPENAPI_HINTS"
ENV_DEPRECATED_INDEX = "HUAWEICLOUD_MCP_DEPRECATED_INDEX"
ENV_DEPRECATED_MODE = "HUAWEICLOUD_MCP_DEPRECATED_MODE"
ENV_ENTITY_INDEX = "HUAWEICLOUD_MCP_ENTITY_INDEX"
ENV_AUTH_DEMOTE = "HUAWEICLOUD_MCP_AUTH_DEMOTE"
ENV_AUTH_DEMOTE_PASS = "HUAWEICLOUD_MCP_AUTH_DEMOTE_PASS"
ENV_METADATA_CORRECTIONS = "HUAWEICLOUD_MCP_METADATA_CORRECTIONS"
ENV_SERVER_CATALOG = "HUAWEICLOUD_MCP_SERVER_CATALOG"
ENV_SERVER_CATALOG_URL = "HUAWEICLOUD_MCP_SERVER_CATALOG_URL"
ENV_SESSION_IDLE_TIMEOUT = "HUAWEICLOUD_MCP_SESSION_IDLE_TIMEOUT"
ENV_MAX_SESSIONS = "HUAWEICLOUD_MCP_MAX_SESSIONS"


@dataclass(frozen=True)
class Deployment:
    """跨模式共享部署旋钮（argv 优先、env 兜底，归一一次全员消费）。

    env 映射随行（缺省 os.environ 快照）：mode 专属旋钮的 env 兜底经
    ``dep.env`` 读取——build_app(env=...) 可整体注入，模式 builder 不再
    直触 os.environ。
    """

    mock: bool = False
    mock_base: str | None = None
    mock_passthrough: bool = False
    policy_file: str | None = None
    audit_file: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


def _env_flag(raw: str | None) -> bool:
    return raw in ("1", "true", "yes")


def resolve_deployment(args: argparse.Namespace,
                       env: Mapping[str, str] | None = None) -> Deployment:
    """argv+env → Deployment（跨模式共享旋钮归一单点，getattr 安全）。"""
    env_map = os.environ if env is None else env
    mock = getattr(args, "mock", None)
    mock_passthrough = getattr(args, "mock_passthrough", None)
    return Deployment(
        mock=(mock if mock is not None
              else _env_flag(env_map.get(ENV_MOCK, ""))),
        mock_base=(getattr(args, "mock_base", None)
                   or env_map.get(ENV_MOCK_BASE) or None),
        mock_passthrough=(mock_passthrough if mock_passthrough is not None
                          else _env_flag(env_map.get(ENV_MOCK_PASSTHROUGH, ""))),
        policy_file=(getattr(args, "policy", None)
                     or env_map.get(ENV_POLICY_FILE) or None),
        audit_file=(getattr(args, "audit_file", None)
                    or env_map.get(ENV_AUDIT_FILE) or None),
        env=env_map,
    )
