"""本地元数据存储：raw/ 索引与 data/openapi 文档的加载与缓存。

惰性加载 + 实例级缓存（含负缓存），data_root 可注入。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import region_paths

logger = logging.getLogger("openmcp.apie.local_store")

PRODUCTS_FILE = "raw/huawei_products.json"
DOCS_FILE = "raw/apis_docs.json"
COUNT_FILE = "raw/apis_count.json"

ApiHit = tuple[dict[str, Any], str, str, dict[str, Any]]


def find_api_in_doc(doc: dict[str, Any] | None, api_name: str) -> tuple[str, str, dict[str, Any]] | None:
    """在 OpenAPI 文档中查找接口，返回 (path, method, op) 或 None。

    先 operationId 精确，再大小写不敏感，最后子串匹配。
    """
    if not doc:
        return None
    exact: tuple[str, str, dict[str, Any]] | None = None
    fuzzy: tuple[str, str, dict[str, Any]] | None = None
    target = (api_name or "").lower()
    for path, path_item in (doc.get("paths") or {}).items():
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            opid = op.get("operationId")
            if opid == api_name:
                return (path, method, op)
            if exact is None and opid and opid.lower() == target:
                exact = (path, method, op)
            if fuzzy is None and opid and target in opid.lower():
                fuzzy = (path, method, op)
    return exact or fuzzy


class LocalStore:
    """raw/ 索引 + data/openapi 文档的惰性加载与缓存。

    缓存语义：首次访问时读盘，之后（含未命中）返回缓存结果；
    元数据重建后需调用 clear() 或重启进程。
    """

    def __init__(self, data_root: str | Path):
        self.data_root = Path(data_root)
        self._products: list[dict[str, Any]] | None = None
        self._products_loaded = False
        self._apis: list[dict[str, Any]] | None = None
        self._apis_loaded = False
        self._counts: dict[str, int] | None = None
        self._counts_loaded = False
        self._api_cache: dict[tuple[str, str, str], ApiHit | None] = {}
        self._apis_live: dict[str, list[dict[str, Any]]] = {}

    def _load_json(self, rel_path: str) -> Any | None:
        full = self.data_root / rel_path
        if not full.exists():
            return None
        with open(full, encoding="utf-8") as f:
            return json.load(f)

    def products(self) -> list[dict[str, Any]] | None:
        """huawei_products.json 的 groups；文件缺失返回 None。"""
        if not self._products_loaded:
            d = self._load_json(PRODUCTS_FILE)
            self._products = d.get("groups", []) if d else None
            self._products_loaded = True
        return self._products

    def apis(self) -> list[dict[str, Any]] | None:
        """apis_docs.json 的 apis；文件缺失返回 None。"""
        if not self._apis_loaded:
            d = self._load_json(DOCS_FILE)
            self._apis = d.get("apis", []) if d else None
            self._apis_loaded = True
        return self._apis

    def counts(self) -> dict[str, int]:
        """apis_count.json 的 {PRODUCT_UPPER: api_count}；文件缺失返回 {}。"""
        if not self._counts_loaded:
            d = self._load_json(COUNT_FILE)
            self._counts = ({g["product_short"].upper(): g["api_count"]
                             for g in d.get("groups", [])} if d else {})
            self._counts_loaded = True
        return self._counts or {}

    def find_api(self, product: str, api_name: str, region: str) -> ApiHit | None:
        """在 data/openapi/{region}/{Product}/ 中查找接口，返回 (doc, path, method, op)。

        结果按 (product, api_name, region) 缓存，未命中同样缓存（负缓存）。
        """
        key = ((product or "").lower(), api_name, region)
        if key in self._api_cache:
            return self._api_cache[key]
        hit = self._scan_api(product, api_name, region)
        self._api_cache[key] = hit
        return hit

    def _scan_api(self, product: str, api_name: str, region: str) -> ApiHit | None:
        root = self.data_root / region_paths.openapi_out_dir(region)
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
            hit = find_api_in_doc(doc, api_name)
            if hit:
                path, method, op = hit
                return doc, path, method, op
        return None

    def set_products(self, products: list[dict[str, Any]]) -> None:
        """回写 products 缓存（实时拉取后调用）。"""
        self._products = products
        self._products_loaded = True

    def get_apis_for(self, product: str) -> list[dict[str, Any]] | None:
        """获取单产品实时拉取的接口缓存。仅 apis_docs.json 缺失时使用。"""
        return self._apis_live.get((product or "").lower())

    def set_apis_for(self, product: str, apis: list[dict[str, Any]]) -> None:
        """缓存单产品实时拉取的接口列表。"""
        self._apis_live[(product or "").lower()] = apis

    def set_api_cache(self, key: tuple[str, str, str], hit: ApiHit | None) -> None:
        """直接写入 find_api 缓存（含负缓存）。"""
        self._api_cache[key] = hit

    def clear(self) -> None:
        """清空全部缓存（元数据重建后调用）。"""
        self._products = None
        self._products_loaded = False
        self._apis = None
        self._apis_loaded = False
        self._counts = None
        self._counts_loaded = False
        self._api_cache = {}
        self._apis_live = {}
