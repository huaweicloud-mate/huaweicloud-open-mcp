"""五元数据工具纯函数（不碰磁盘、不耦合 MCP 协议）。

返回结构为具名信封（types.py 的 *Result TypedDict），纯函数直接产出含
ok=True 的完整结果；失败态（ToolError）由编排层（service）构造。
"""

import json
import re
from collections import Counter
from typing import Any

from common.types import (
    ApiDetailResult,
    ApiExample,
    ApiItem,
    ApiListResult,
    ProductItem,
    ProductListResult,
    ProductResult,
    TagGroup,
)

# ---------- 产品 ----------

def list_products(groups: list[dict[str, Any]], *,
                  counts: dict[str, int] | None = None,
                  category: str | None = None,
                  keyword: str | None = None) -> ProductListResult:
    """groups: huawei_products.json 的 groups；counts: {PRODUCT_UPPER: api_count}。"""
    counts = counts or {}
    kw = (keyword or "").lower()
    products: list[ProductItem] = []
    for g in groups:
        gname = g.get("name", "")
        if category and category not in gname:
            continue
        for p in g.get("products", []):
            ps = p.get("productshort") or ""
            name = p.get("name") or ""
            if kw and kw not in ps.lower() and kw not in name.lower():
                continue
            products.append({
                "product": ps,
                "name": name,
                "category": gname,
                "api_count": counts.get(ps.upper(), 0),
                "is_global": p.get("is_global"),
                "link": p.get("link") or None,
            })
    return {"ok": True, "total": len(products), "products": products}


def get_product(groups: list[dict[str, Any]], product: str, *,
                counts: dict[str, int] | None = None) -> ProductResult | None:
    counts = counts or {}
    target = (product or "").lower()
    for g in groups:
        for p in g.get("products", []):
            ps = p.get("productshort") or ""
            if ps.lower() == target or (p.get("name") or "").lower() == target:
                return {
                    "ok": True,
                    "product": ps,
                    "name": p.get("name"),
                    "category": g.get("name"),
                    "api_count": counts.get(ps.upper(), 0),
                    "is_global": p.get("is_global"),
                    "link": p.get("link") or None,
                }
    return None


# ---------- API 目录 ----------

def list_apis(apis: list[dict[str, Any]], product: str, *,
              tag: str | None = None, search: str | None = None,
              limit: int = 20, offset: int = 0) -> ApiListResult:
    p = (product or "").lower()
    matched = [a for a in apis if (a.get("product_short") or "").lower() == p]

    # tag 概览基于产品全量目录（不受 tag/search/分页过滤影响），供 LLM 收窄目录参考；
    # 按接口数降序，同数按 tag 名
    tag_counts = Counter((a.get("tags") or "").strip() or "_untagged" for a in matched)
    tag_groups: list[TagGroup] = [
        {"tag": t, "api_count": c}
        for t, c in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    if tag:
        matched = [a for a in matched if (a.get("tags") or "").strip() == tag]
    if search:
        kw = search.lower()
        matched = [a for a in matched
                   if kw in (a.get("name") or "").lower() or kw in (a.get("summary") or "").lower()]
    total = len(matched)
    page = matched[offset:offset + limit]
    items: list[ApiItem] = [{
        "name": a.get("name") or "",
        "method": a.get("method") or "",
        "summary": a.get("summary") or "",
        "tags": a.get("tags") or "",
        "info_version": a.get("info_version") or "",
    } for a in page]
    return {
        "ok": True,
        "product": product,
        "total": total,
        "offset": offset,
        "limit": limit,
        "apis": items,
        "tag_groups": tag_groups,
    }


# ---------- API 详情 ----------

def _resolve_schema(obj: Any, doc: dict[str, Any], depth: int = 0,
                    collected: set[str] | None = None) -> Any:
    if collected is None:
        collected = set()
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/") and depth < 2:
            name = ref.split("/")[-1]
            target = (doc.get("definitions") or {}).get(name)
            if target is not None:
                collected.add(name)
                resolved = _resolve_schema(target, doc, depth + 1, collected)
                merged = {k: v for k, v in obj.items() if k != "$ref"}
                merged.update(resolved)
                return merged
        return {k: _resolve_schema(v, doc, depth, collected) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_schema(x, doc, depth, collected) for x in obj]
    return obj


def format_api_detail(doc: dict[str, Any], product: str, path: str,
                      method: str, op: dict[str, Any]) -> ApiDetailResult:
    """把单个 operation 格式化为 AI 友好的结构化详情。"""
    definitions = doc.get("definitions") or {}
    collected: set[str] = set()

    parameters: list[dict[str, Any]] = []
    shared_params = doc.get("parameters") or {}
    for p in op.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        ref = p.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/parameters/"):
            target = shared_params.get(ref.split("/")[-1])
            if target is None:
                continue  # 引用缺失：跳过而非输出无 name/in 的脏参数
            out_p = dict(target)
            for extra in ("description", "required"):
                if extra in p:
                    out_p[extra] = p[extra]
        else:
            out_p = dict(p)
        if not out_p.get("name") or not out_p.get("in"):
            continue
        if out_p.get("in") == "body":
            out_p["schema"] = _resolve_schema(out_p.get("schema") or {}, doc, 0, collected)
        parameters.append(out_p)

    responses: dict[str, dict[str, Any]] = {}
    for code, resp in (op.get("responses") or {}).items():
        r = {k: v for k, v in resp.items() if k in ("description", "headers", "examples")}
        schema = _resolve_schema(resp.get("schema") or {}, doc, 0, collected)
        if schema:
            r["schema"] = schema
        responses[code] = r

    return {
        "ok": True,
        "product": product,
        "api": op.get("operationId") or "",
        "method": method.upper(),
        "path": path,
        "summary": op.get("summary"),
        "description": op.get("description"),
        "x-constraint": op.get("x-constraint"),
        "deprecated": bool(op.get("deprecated")),
        "parameters": parameters,
        "responses": responses,
        "definitions": {name: definitions[name] for name in collected if name in definitions},
    }


def extract_examples(op: dict[str, Any] | None) -> list[ApiExample]:
    """提取 x-request-examples-N 系列示例，返回 [{"description", "example"}]。"""
    if not isinstance(op, dict):
        return []
    nums = set()
    for key in op.keys():
        m = re.match(r"^x-request-examples(?:-text)?-(\d+)$", key)
        if m:
            nums.add(m.group(1))
    examples: list[ApiExample] = []
    for n in sorted(nums):
        desc = op.get(f"x-request-examples-description-{n}")
        ex = op.get(f"x-request-examples-{n}")
        if ex is None and f"x-request-examples-text-{n}" in op:
            text = op.get(f"x-request-examples-text-{n}")
            try:
                ex = json.loads(text) if isinstance(text, str) else text
            except Exception:
                ex = text
        if ex is not None:
            examples.append({"description": desc, "example": ex})
    return examples
