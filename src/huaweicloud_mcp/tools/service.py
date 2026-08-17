"""ToolService：七工具的业务编排层。

职责：数据加载（raw/ + data/openapi，路径可注入）、配置（region/mock/policy/凭证）、
调用纯函数层（tools.metadata / tools.execute）与执行客户端。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ..apie import mock as apie_mock
from ..apie import region_paths
from ..auth.credentials import Credentials
from ..paths import project_root
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

DEFAULT_REGION = "cn-north-4"

PRODUCTS_FILE = "raw/huawei_products.json"
DOCS_FILE = "raw/apis_docs.json"
COUNT_FILE = "raw/apis_count.json"


@dataclass
class ServiceConfig:
    region: str = DEFAULT_REGION
    mock: bool = False
    policy_rules: Sequence[safety_policy.PolicyRule] | None = None
    credentials: Credentials | None = None
    mock_base: str = apie_mock.MOCK_BASE
    data_root: Path | None = None
    http_client_factory: Callable[[], execute.ApiExecutor] | None = None
    mock_client_factory: Callable[[], apie_mock.MockApiClient] | None = None


class ToolService:
    def __init__(self, config: ServiceConfig | None = None):
        self.config = config or ServiceConfig()
        self._mock_client: apie_mock.MockApiClient | None = None
        self._groups_cache: list[dict[str, Any]] | None = None
        self._groups_loaded = False
        self._docs_cache: list[dict[str, Any]] | None = None
        self._docs_loaded = False
        self._counts_cache: dict[str, int] | None = None
        self._counts_loaded = False

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

    # ---------- 数据访问 ----------

    @property
    def data_root(self) -> Path:
        return self.config.data_root or project_root()

    def _load_json(self, rel_path: str) -> Any | None:
        full = self.data_root / rel_path
        if not full.exists():
            return None
        with open(full, encoding="utf-8") as f:
            return json.load(f)

    def _groups(self) -> list[dict[str, Any]] | None:
        if not self._groups_loaded:
            d = self._load_json(PRODUCTS_FILE)
            self._groups_cache = d.get("groups", []) if d else None
            self._groups_loaded = True
        return self._groups_cache

    def _docs(self) -> list[dict[str, Any]] | None:
        if not self._docs_loaded:
            d = self._load_json(DOCS_FILE)
            self._docs_cache = d.get("apis", []) if d else None
            self._docs_loaded = True
        return self._docs_cache

    def _counts(self) -> dict[str, int]:
        if not self._counts_loaded:
            d = self._load_json(COUNT_FILE)
            self._counts_cache = ({g["product_short"].upper(): g["api_count"]
                             for g in d.get("groups", [])} if d else {})
            self._counts_loaded = True
        return self._counts_cache or {}

    def load_api_doc(self, product: str, api_name: str, region: str | None = None
                     ) -> tuple[dict[str, Any], str, str, dict[str, Any]] | None:
        """在 data/openapi 中查找接口所在 tag 文档，返回 (doc, path, method, op) 或 None。"""
        root = self.data_root / region_paths.openapi_out_dir(region or self.config.region)
        if not root.is_dir():
            return None
        base = None
        target_dir = (product or "").lower()
        for d in root.iterdir():
            if d.is_dir() and d.name.lower() == target_dir:
                base = d
                break
        if base is None:
            return None
        for fn in sorted(base.iterdir()):
            if fn.suffix != ".json" or fn.name.startswith("."):
                continue
            with open(fn, encoding="utf-8") as f:
                doc = json.load(f)
            hit = metadata.find_api_in_doc(doc, api_name)
            if hit:
                path, method, op = hit
                return doc, path, method, op
        return None

    # ---------- 元数据工具 ----------

    def list_products(self, category: str | None = None,
                      keyword: str | None = None) -> ProductListResult | ToolError:
        groups = self._groups()
        if groups is None:
            return {"ok": False, "reason": "本地元数据缺失，请先运行 api-refresh products"}
        return metadata.list_products(groups, counts=self._counts(),
                                      category=category, keyword=keyword)

    def get_product(self, product: str) -> ProductResult | ToolError:
        groups = self._groups()
        if groups is None:
            return {"ok": False, "reason": "本地元数据缺失，请先运行 api-refresh products"}
        out = metadata.get_product(groups, product, counts=self._counts())
        if out is None:
            return {"ok": False, "reason": f"产品 {product} 未找到"}
        return out

    def list_apis(self, product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        docs = self._docs()
        if docs is None:
            return {"ok": False, "reason": "本地接口索引缺失，请先运行 api-refresh docs"}
        return metadata.list_apis(docs, product, tag=tag, search=search, limit=limit, offset=offset)

    def get_api(self, product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        doc, path, method, op = hit
        return metadata.format_api_detail(doc, product, path, method, op)

    def get_api_examples(self, product: str, api: str,
                         region: str | None = None) -> ExamplesResult | ToolError:
        hit = self.load_api_doc(product, api, region)
        if hit is None:
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
        if rules is None:
            return {"ok": False, "reason": "safety policy 未配置，execute_api 全部拒绝"}
        if not safety_policy.evaluate(rules, product, api):
            return {"ok": False, "reason": f"safety policy 拒绝执行 {product}:{api}"}
        status_code = params.get("_status_code", 200)
        number = params.get("_number", 1)
        resp = self._make_mock_client().mock_request(product, api, region,
                                                     status_code=status_code, number=number)
        out = execute.normalize_response(resp)
        out.update({"ok": True, "product": product, "api": api})
        return out
