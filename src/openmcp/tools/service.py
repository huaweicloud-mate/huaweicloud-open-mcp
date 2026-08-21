"""ToolService：六工具的业务编排层。

职责：配置（region/mock/policy/凭证）、调用纯函数层（tools.metadata / tools.execute）
与执行客户端；元数据加载委托 apie.catalog 功能接口（纯内存远端）。
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..apie import catalog
from ..apie import mock as apie_mock
from ..apie.memory_store import ApiHit
from ..auth.credentials import Credentials
from ..safety import policy as safety_policy
from ..signer.client import HttpClient
from ..types import (
    ApiDetailResult,
    ApiListResult,
    ExamplesResult,
    ExecuteResult,
    ProductListResult,
    ProductResult,
    ToolError,
)
from . import execute, metadata

logger = logging.getLogger("openmcp.tools.service")

DEFAULT_REGION = "cn-north-4"


@dataclass
class ServiceConfig:
    region: str = DEFAULT_REGION
    mock: bool = False
    policy_rules: Sequence[safety_policy.PolicyRule] | None = None
    credentials: Credentials | None = None
    mock_base: str = apie_mock.MOCK_BASE
    http_client_factory: Callable[[], execute.ApiExecutor] | None = None
    mock_client_factory: Callable[[], apie_mock.MockApiClient] | None = None


class ToolService:
    def __init__(self, config: ServiceConfig | None = None):
        self.config = config or ServiceConfig()
        self._mock_client: apie_mock.MockApiClient | None = None

    def _make_http_client(self) -> execute.ApiExecutor:
        if self.config.http_client_factory is not None:
            return self.config.http_client_factory()
        return HttpClient(credentials=self.config.credentials)

    def _make_mock_client(self) -> apie_mock.MockApiClient:
        if self.config.mock_client_factory is not None:
            return self.config.mock_client_factory()
        if self._mock_client is None:
            self._mock_client = apie_mock.MockApiClient(base_url=self.config.mock_base)
        return self._mock_client

    def load_api_doc(self, product: str, api_name: str, region: str | None = None
                     ) -> ApiHit | None:
        """查找接口 OpenAPI 文档（内存缓存或远端拉取），返回 (doc, path, method, op) 或 None。"""
        r = catalog.find_api_doc(product, api_name, region or self.config.region)
        return r.data

    # ---------- 元数据工具 ----------

    def list_products(self, category: str | None = None,
                      keyword: str | None = None) -> ProductListResult | ToolError:
        logger.info("list_products category=%s keyword=%s", category or "-", keyword or "-")
        r = catalog.get_products()
        groups = r.data
        if groups is None:
            logger.warning("list_products metadata=missing")
            return {"ok": False, "reason": "产品列表不可用（远端拉取失败）"}
        return metadata.list_products(groups, counts=catalog.get_api_counts(),
                                      category=category, keyword=keyword)

    def get_product(self, product: str) -> ProductResult | ToolError:
        logger.info("get_product product=%s", product)
        r = catalog.get_products()
        groups = r.data
        if groups is None:
            logger.warning("get_product product=%s metadata=missing", product)
            return {"ok": False, "reason": "产品列表不可用（远端拉取失败）"}
        out = metadata.get_product(groups, product, counts=catalog.get_api_counts())
        if out is None:
            logger.warning("get_product product=%s result=not_found", product)
            return {"ok": False, "reason": f"产品 {product} 未找到"}
        return out

    def list_apis(self, product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        logger.info("list_apis product=%s tag=%s search=%s limit=%d offset=%d",
                    product, tag or "-", search or "-", limit, offset)
        r = catalog.get_apis(product=product)
        apis = r.data
        if apis is None:
            logger.warning("list_apis product=%s metadata=missing", product)
            return {"ok": False, "reason": "接口索引不可用（远端拉取失败）"}
        return metadata.list_apis(apis, product, tag=tag, search=search, limit=limit, offset=offset)

    def get_api(self, product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        region = region or self.config.region
        logger.info("get_api %s:%s region=%s", product, api, region)
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            logger.warning("get_api %s:%s region=%s result=not_found", product, api, region)
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        doc, path, method, op = hit
        return metadata.format_api_detail(doc, product, path, method, op)

    def get_api_examples(self, product: str, api: str,
                         region: str | None = None) -> ExamplesResult | ToolError:
        region = region or self.config.region
        logger.info("get_api_examples %s:%s region=%s", product, api, region)
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            logger.warning("get_api_examples %s:%s region=%s result=not_found",
                           product, api, region)
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        _, _, _, op = hit
        return {"ok": True, "product": product, "api": api,
                "examples": metadata.extract_examples(op)}

    # ---------- 执行工具 ----------

    def execute_api(self, product: str, api: str, region: str | None = None,
                    params: dict[str, Any] | None = None) -> ExecuteResult:
        """执行 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。"""
        region = region or self.config.region
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        doc, path, method, op = hit
        params = dict(params or {})

        if self.config.mock:
            return self._execute_mock(product, api, region, params)

        client = self._make_http_client()
        return execute.execute_api(
            doc, path, method, op, product, api, region, params,
            policy_rules=self.config.policy_rules,
            client=client,
            credentials=self.config.credentials,
        )

    def _execute_mock(self, product: str, api: str, region: str,
                      params: dict[str, Any]) -> ExecuteResult:
        """mock 模式：policy 检查后路由到 API Explorer mock 端点。"""
        rules = self.config.policy_rules
        err = safety_policy.check(rules, product, api)
        if err:
            logger.warning("execute %s:%s region=%s mode=mock policy=%s",
                           product, api, region,
                           "unconfigured" if rules is None else "deny")
            return {"ok": False, "reason": err}
        status_code = params.get("_status_code", 200)
        number = params.get("_number", 1)
        resp = self._make_mock_client().mock_request(product, api, region,
                                                     status_code=status_code, number=number)
        out = execute.normalize_response(resp)
        out.update({"ok": True, "product": product, "api": api})
        return out
