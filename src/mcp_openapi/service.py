"""ToolService：六工具的业务编排层。

职责：配置（region/mock/policy/凭证）、拥有 MemoryStore 并注入 catalog、
调用纯函数层（tools.metadata / tools.execute）与执行客户端。
"""

import functools
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TypeVar

from apie import catalog, metadata
from apie import mock as apie_mock
from apie.memory_store import ApiHit, MemoryStore
from common.audit import AuditSink
from common.auth.credentials import Credentials
from common.types import (
    ApiDetailResult,
    ApiListResult,
    ExamplesResult,
    ExecuteResult,
    ProductListResult,
    ProductResult,
    ToolError,
)
from safety import policy as safety_policy
from safety.policy_store import PolicyStore

from . import execute, execute_obs
from .execute_obs import ObsHttpClient
from .gate import Gate
from .signer.client import HttpClient

logger = logging.getLogger("mcp_openapi.service")

DEFAULT_REGION = "cn-north-4"

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass
class ServiceConfig:
    region: str = DEFAULT_REGION
    mock: bool = False
    policy_rules: Sequence[safety_policy.PolicyRule] | None = None
    policy_store: PolicyStore | None = None
    credentials: Credentials | None = None
    mock_base: str = apie_mock.MOCK_BASE
    mock_passthrough: bool = False
    http_client_factory: Callable[[], execute.ApiExecutor] | None = None
    mock_client_factory: Callable[[], apie_mock.MockApiClient] | None = None
    obs_client_factory: Callable[[], execute_obs.ObsClient] | None = None
    gate: Gate = Gate.unrestricted()
    audit_sink: AuditSink | None = None


def build_audit_event(tool: str, input_args: Mapping[str, Any],
                      result: Any) -> dict[str, Any]:
    """审计事件 payload（对 verifier 的已发布契约）：tool/input/ok；ts 由 sink 注入。

    ok 取 result 的 ok 字段（缺失视为成功）；input 为调用方显式入参快照
    （不含默认值，对齐 agent 侧 trace 口径）。
    """
    ok = bool(result.get("ok", True)) if isinstance(result, Mapping) else True
    return {"tool": tool, "input": dict(input_args), "ok": ok}


def _audited(fn: _F) -> _F:
    """工具方法审计装饰器：每次调用经 audit sink 记一条事件（未配置 sink 零开销跳过）。

    input 为绑定后的显式入参（不含 self 与默认值）；方法抛异常时记 ok=False 并原样抛出。
    签名保持：装饰不改变方法对调用方的可见类型。
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(self: "ToolService", *args: Any, **kwargs: Any) -> Any:
        try:
            bound = sig.bind(self, *args, **kwargs)
            input_args = {k: v for k, v in bound.arguments.items() if k != "self"}
        except TypeError:
            input_args = {}
        try:
            result = fn(self, *args, **kwargs)
        except Exception:
            self._audit(fn.__name__, input_args, {"ok": False})
            raise
        self._audit(fn.__name__, input_args, result)
        return result

    return wrapper  # type: ignore[return-value]


class ToolService:
    def __init__(self, config: ServiceConfig | None = None, *,
                 store: MemoryStore | None = None):
        self.config = config or ServiceConfig()
        self.store = store or MemoryStore()
        self._mock_client: apie_mock.MockApiClient | None = None

    def _make_http_client(self) -> execute.ApiExecutor:
        if self.config.http_client_factory is not None:
            return self.config.http_client_factory()
        return HttpClient(credentials=self.config.credentials)

    def _make_obs_client(self) -> execute_obs.ObsClient:
        if self.config.obs_client_factory is not None:
            return self.config.obs_client_factory()
        return ObsHttpClient(credentials=self.config.credentials)

    def _make_mock_client(self) -> apie_mock.MockApiClient:
        if self.config.mock_client_factory is not None:
            return self.config.mock_client_factory()
        if self._mock_client is None:
            self._mock_client = apie_mock.MockApiClient(base_url=self.config.mock_base)
        return self._mock_client

    def load_api_doc(self, product: str, api_name: str, region: str | None = None
                     ) -> ApiHit | None:
        """查找接口 OpenAPI 文档（内存缓存或远端拉取），返回 (doc, path, method, op) 或 None。"""
        return catalog.find_api_doc(self.store, product, api_name,
                                    region or self.config.region)

    def _effective_policy_rules(self) -> Sequence[safety_policy.PolicyRule] | None:
        """当前生效规则：注入 PolicyStore 时实时热加载，否则用启动快照。"""
        if self.config.policy_store is not None:
            return self.config.policy_store.rules()
        return self.config.policy_rules

    def _check_policy(self, product: str, api: str) -> str | None:
        """检查 safety policy，返回错误描述或 None（放行）。"""
        return safety_policy.check(self._effective_policy_rules(), product, api)

    @_audited
    def manage_policy(self, action: str,
                      line: str | None = None) -> dict[str, Any]:
        """管理 safety policy（list/add/remove），改动即时生效并写回策略文件。

        安全约定：调用方（Agent）应先向用户确认再 add/remove；审计日志强制记录。
        """
        action = (action or "").strip().lower()
        logger.info("manage_policy action=%s line=%s", action, line or "-")
        store = self.config.policy_store
        if store is None:
            return {"ok": False, "reason": (
                "未配置 safety policy 文件，manage_policy 不可用"
                "（--policy 或环境变量 HUAWEICLOUD_MCP_POLICY_FILE）")}
        if action == "list":
            return {"ok": True, "action": "list", "policy": store.text()}
        if action not in ("add", "remove"):
            return {"ok": False, "reason": f"未知 action: {action}（可选 list/add/remove）"}
        rule_text = (line or "").strip()
        if not rule_text:
            return {"ok": False, "reason": f"{action} 需要提供 line 参数（规则文本）"}
        result = (store.add_rule(rule_text) if action == "add"
                  else store.remove_rule(rule_text))
        logger.info("manage_policy %s result=%s", action, "ok" if result.ok else "deny")
        out: dict[str, Any] = {"ok": result.ok, "action": action}
        if result.reason:
            out["reason"] = result.reason
        out["policy"] = store.text()
        return out

    def _check_gate(self, product: str) -> str | None:
        """检查产品门栓，返回错误描述或 None（放行）。"""
        if self.config.gate.allows(product):
            return None
        return f"产品 {product} 不在 openapi mcp 授权范围内"

    def _audit(self, tool: str, input_args: Mapping[str, Any], result: Any) -> None:
        """经 audit sink 记录一条工具调用事件（best-effort，未配置 sink 跳过）。"""
        sink = self.config.audit_sink
        if sink is None:
            return
        sink.record(build_audit_event(tool, input_args, result))

    # ---------- 元数据工具 ----------

    @_audited
    def list_products(self, category: str | None = None,
                      keyword: str | None = None) -> ProductListResult | ToolError:
        logger.info("list_products category=%s keyword=%s", category or "-", keyword or "-")
        groups = catalog.get_products(self.store)
        if groups is None:
            logger.warning("list_products metadata=missing")
            return {"ok": False, "reason": "产品列表不可用（远端拉取失败）"}
        groups = self.config.gate.filter_products(groups)
        return metadata.list_products(groups, counts=catalog.get_api_counts(self.store),
                                      category=category, keyword=keyword)

    @_audited
    def get_product(self, product: str) -> ProductResult | ToolError:
        logger.info("get_product product=%s", product)
        gated = self._check_gate(product)
        if gated:
            logger.warning("get_product product=%s result=gated", product)
            return {"ok": False, "reason": gated}
        groups = catalog.get_products(self.store)
        if groups is None:
            logger.warning("get_product product=%s metadata=missing", product)
            return {"ok": False, "reason": "产品列表不可用（远端拉取失败）"}
        out = metadata.get_product(groups, product, counts=catalog.get_api_counts(self.store))
        if out is None:
            logger.warning("get_product product=%s result=not_found", product)
            return {"ok": False, "reason": f"产品 {product} 未找到"}
        return out

    @_audited
    def list_apis(self, product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        logger.info("list_apis product=%s tag=%s search=%s limit=%d offset=%d",
                    product, tag or "-", search or "-", limit, offset)
        gated = self._check_gate(product)
        if gated:
            logger.warning("list_apis product=%s result=gated", product)
            return {"ok": False, "reason": gated}
        apis = catalog.get_apis(self.store, product)
        if apis is None:
            logger.warning("list_apis product=%s metadata=missing", product)
            return {"ok": False, "reason": "接口索引不可用（远端拉取失败）"}
        return metadata.list_apis(apis, product, tag=tag, search=search, limit=limit, offset=offset)

    @_audited
    def get_api(self, product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        region = region or self.config.region
        logger.info("get_api %s:%s region=%s", product, api, region)
        gated = self._check_gate(product)
        if gated:
            logger.warning("get_api %s:%s region=%s result=gated", product, api, region)
            return {"ok": False, "reason": gated}
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            logger.warning("get_api %s:%s region=%s result=not_found", product, api, region)
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        doc, path, method, op = hit
        return metadata.format_api_detail(doc, product, path, method, op)

    @_audited
    def get_api_examples(self, product: str, api: str,
                         region: str | None = None) -> ExamplesResult | ToolError:
        region = region or self.config.region
        logger.info("get_api_examples %s:%s region=%s", product, api, region)
        gated = self._check_gate(product)
        if gated:
            logger.warning("get_api_examples %s:%s region=%s result=gated", product, api, region)
            return {"ok": False, "reason": gated}
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            logger.warning("get_api_examples %s:%s region=%s result=not_found",
                           product, api, region)
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        _, _, _, op = hit
        return {"ok": True, "product": product, "api": api,
                "examples": metadata.extract_examples(op)}

    # ---------- 执行工具 ----------

    @_audited
    def execute_api(self, product: str, api: str, region: str | None = None,
                    params: dict[str, Any] | None = None) -> ExecuteResult:
        """执行 API。产品门栓先粗滤，safety policy 再细检，mock/real 分支共享。"""
        region = region or self.config.region
        gated = self._check_gate(product)
        if gated:
            logger.warning("execute %s:%s region=%s mode=%s policy=gated",
                           product, api, region,
                           "mock" if self.config.mock else "real")
            return {"ok": False, "reason": gated}
        policy_err = self._check_policy(product, api)
        if policy_err:
            logger.warning("execute %s:%s region=%s mode=%s policy=%s",
                           product, api, region,
                           "mock" if self.config.mock else "real",
                           "unconfigured" if self._effective_policy_rules() is None else "deny")
            return {"ok": False, "reason": policy_err}

        hit = self.load_api_doc(product, api, region)
        if hit is None:
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        doc, path, method, op = hit
        params = dict(params or {})

        # 预签发分支：OBS 专用，gateway 只签名不搬运字节；先于 mock/real 分流
        if params.get("_presign"):
            if not execute_obs.is_obs(product, doc):
                return {"ok": False,
                        "reason": "_presign 仅支持 OBS 产品（其余服务无预签发语义）"}
            return execute_obs.execute_presign_api(
                doc, path, method, op, product, api, region, params,
                credentials=self.config.credentials)

        logger.info("execute %s:%s region=%s mode=%s policy=allow",
                    product, api, region,
                    "mock" if self.config.mock else "real")

        if self.config.mock:
            return self._execute_mock(product, api, region, params)

        if execute_obs.is_obs(product, doc):
            if execute_obs.is_object_data_api(api, op):
                # 对象数据面单口径：恒返回预签名 URL，gateway 不搬运对象字节
                return execute_obs.execute_presign_api(
                    doc, path, method, op, product, api, region, params,
                    credentials=self.config.credentials,
                )
            logger.info("execute %s:%s region=%s mode=obs policy=allow",
                        product, api, region)
            return execute_obs.execute_obs_api(
                doc, path, method, op, product, api, region, params,
                client=self._make_obs_client(),
                credentials=self.config.credentials,
            )

        return execute.execute_api(
            doc, path, method, op, product, api, region, params,
            client=self._make_http_client(),
            credentials=self.config.credentials,
        )

    def _execute_mock(self, product: str, api: str, region: str,
                      params: dict[str, Any]) -> ExecuteResult:
        """mock 模式：直接路由到 API Explorer mock 端点（policy 已在上层检查）。

        mock_passthrough 开启时把业务参数转发到端点（标量→query、body→POST JSON，
        控制键剥离）；默认关，保持 API Explorer mock 契约。
        """
        status_code = params.get("_status_code", 200)
        number = params.get("_number", 1)
        client = self._make_mock_client()
        if self.config.mock_passthrough:
            resp = client.mock_request(product, api, region,
                                       status_code=status_code, number=number,
                                       params=params)
        else:
            resp = client.mock_request(product, api, region,
                                       status_code=status_code, number=number)
        out = execute.normalize_response(resp)
        out.update({"ok": True, "product": product, "api": api})
        return out
