"""六元数据工具业务函数（纯函数 + 磁盘加载器，不耦合 MCP 协议）。"""

import json
import os
import re

from ..apie import region_paths

PROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_CJK = re.compile(r"[\u4e00-\u9fff]+")


# ---------- 纯函数 ----------

def list_products(groups, counts=None, category=None, keyword=None):
    """groups: huawei_products.json 的 groups；counts: {PRODUCT_UPPER: api_count}。"""
    counts = counts or {}
    kw = (keyword or "").lower()
    products = []
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
    return {"total": len(products), "products": products}


def get_product(groups, product, counts=None):
    counts = counts or {}
    target = (product or "").lower()
    for g in groups:
        for p in g.get("products", []):
            ps = p.get("productshort") or ""
            if ps.lower() == target or (p.get("name") or "").lower() == target:
                return {
                    "product": ps,
                    "name": p.get("name"),
                    "category": g.get("name"),
                    "api_count": counts.get(ps.upper(), 0),
                    "is_global": p.get("is_global"),
                    "link": p.get("link") or None,
                }
    return None


def list_apis(apis, product, tag=None, search=None, limit=20, offset=0):
    p = (product or "").lower()
    matched = [a for a in apis if (a.get("product_short") or "").lower() == p]
    if tag:
        matched = [a for a in matched if (a.get("tags") or "").strip() == tag]
    if search:
        kw = search.lower()
        matched = [a for a in matched
                   if kw in (a.get("name") or "").lower() or kw in (a.get("summary") or "").lower()]
    total = len(matched)
    page = matched[offset:offset + limit]
    return {
        "product": product,
        "total": total,
        "offset": offset,
        "limit": limit,
        "apis": [{
            "name": a.get("name"),
            "method": a.get("method"),
            "summary": a.get("summary"),
            "tags": a.get("tags"),
            "info_version": a.get("info_version"),
        } for a in page],
    }


def find_api_in_doc(doc, api_name):
    """在 OpenAPI 文档中查找接口，返回 (path, method, op) 或 None。

    先 operationId 精确，再大小写不敏感，最后子串匹配。
    """
    if not doc:
        return None
    exact = fuzzy = None
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


def _resolve_schema(obj, doc, depth=0, collected=None):
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


def format_api_detail(doc, product, path, method, op):
    """把单个 operation 格式化为 AI 友好的结构化详情。"""
    definitions = doc.get("definitions") or {}
    collected = set()

    parameters = []
    for p in op.get("parameters") or []:
        out_p = {k: v for k, v in p.items()}
        if p.get("in") == "body":
            schema = _resolve_schema(p.get("schema") or {}, doc, 0, collected)
            out_p["schema"] = schema
        parameters.append(out_p)

    responses = {}
    for code, resp in (op.get("responses") or {}).items():
        r = {k: v for k, v in resp.items() if k in ("description", "headers", "examples")}
        schema = _resolve_schema(resp.get("schema") or {}, doc, 0, collected)
        if schema:
            r["schema"] = schema
        responses[code] = r

    return {
        "product": product,
        "api": op.get("operationId"),
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


def extract_examples(op):
    """提取 x-request-examples-N 系列示例，返回 [{"description", "example"}]。"""
    if not isinstance(op, dict):
        return []
    nums = set()
    for key in op.keys():
        m = re.match(r"^x-request-examples(?:-text)?-(\d+)$", key)
        if m:
            nums.add(m.group(1))
    examples = []
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


def _task_keywords(task):
    keywords = []
    for token in re.split(r"[^a-zA-Z0-9]+", task or ""):
        t = token.strip().lower()
        if len(t) >= 2:
            keywords.append(t)
    for cjk_run in _CJK.findall(task or ""):
        for i in range(len(cjk_run) - 1):
            keywords.append(cjk_run[i:i + 2])
    return list(dict.fromkeys(keywords))


def suggest_apis(apis, task, product=None, limit=10):
    """任务描述 → 推荐 API 列表。关键词（英文词/CJK 二元组）加权匹配。"""
    keywords = _task_keywords(task)
    if not keywords:
        return {"task": task, "total": 0, "apis": []}
    scored = []
    for a in apis:
        if product and (a.get("product_short") or "").lower() != product.lower():
            continue
        name = (a.get("name") or "").lower()
        summary = (a.get("summary") or "").lower()
        tags = (a.get("tags") or "").lower()
        score = 0
        matched = []
        for kw in keywords:
            if kw in name:
                score += 3
                matched.append(kw)
            elif kw in tags:
                score += 2
                matched.append(kw)
            elif kw in summary:
                score += 1
                matched.append(kw)
        if score > 0:
            scored.append({
                "product": a.get("product_short"),
                "name": a.get("name"),
                "method": a.get("method"),
                "summary": a.get("summary"),
                "tags": a.get("tags"),
                "score": score,
                "matched_keywords": list(dict.fromkeys(matched)),
            })
    scored.sort(key=lambda x: -x["score"])
    scored = scored[:limit]
    return {"task": task, "total": len(scored), "apis": scored}


# ---------- 磁盘加载器 ----------

def _load_json(rel_path):
    full = os.path.join(PROOT, rel_path)
    if not os.path.exists(full):
        return None
    with open(full, encoding="utf-8") as f:
        return json.load(f)


def load_products():
    d = _load_json("raw/huawei_products.json")
    return d.get("groups", []) if d else None


def load_docs():
    d = _load_json("raw/apis_docs.json")
    return d.get("apis", []) if d else None


def load_counts():
    d = _load_json("raw/apis_count.json")
    if not d:
        return {}
    return {g["product_short"].upper(): g["api_count"] for g in d.get("groups", [])}


def load_api_doc(product, api_name, region=None):
    """在 data/openapi 中查找接口所在 tag 文档，返回 (doc, path, method, op) 或 None。"""
    root = os.path.join(PROOT, region_paths.openapi_out_dir(region))
    if not os.path.isdir(root):
        return None
    base = None
    target_dir = (product or "").lower()
    for d in os.listdir(root):
        if d.lower() == target_dir:
            base = os.path.join(root, d)
            break
    if base is None:
        return None
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        with open(os.path.join(base, fn), encoding="utf-8") as f:
            doc = json.load(f)
        hit = find_api_in_doc(doc, api_name)
        if hit:
            path, method, op = hit
            return (doc, path, method, op)
    return None
