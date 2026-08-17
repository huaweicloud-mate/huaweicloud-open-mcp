"""api-docs：API Explorer 渐进式查询 CLI。

产品 → 产品 tag → 接口列表 → 接口详情，Agent 友好。
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
from typing import Any, cast

from ..logconf import configure_logging
from ..paths import project_root
from . import http, region_paths

logger = logging.getLogger("huaweicloud_mcp.apie.api_docs")

PROOT = str(project_root())

PRODUCTS_FILE = "raw/huawei_products.json"
DOCS_FILE = "raw/apis_docs.json"

BASE_PRODUCTS = "https://console.huaweicloud.com/apiexplorer/new/v5/products"
BASE_APIS = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"
BASE_DETAIL = "https://console.huaweicloud.com/apiexplorer/new/v4/apis/detail"
PAGE_SIZE = 100


# ---------- 输出 ----------

def emit(data: dict, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif fmt == "yaml":
        import yaml
        clean = json.loads(json.dumps(data, ensure_ascii=False))
        print(yaml.safe_dump(clean, allow_unicode=True, sort_keys=False, default_flow_style=False))
    else:
        print_table(data)


def print_table(data: dict) -> None:
    t = data.get("_table")
    if not t:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    rows = t.get("rows", [])
    cols = t.get("columns", [])
    if not rows:
        print("(no results)")
        return
    import tabulate
    table_rows = [[r.get(c, "") if r.get(c, "") is not None else "" for c in cols] for r in rows]
    col_max = t.get("maxcolwidths")
    print(tabulate.tabulate(table_rows, headers=cols, tablefmt="simple",
                            maxcolwidths=col_max or 60, disable_numparse=True))
    if t.get("note"):
        print()
        print(t["note"])


# ---------- 本地数据读取 ----------

def load_json(path: str) -> dict[str, Any] | None:
    full = os.path.join(PROOT, path)
    if not os.path.exists(full):
        return None
    with open(full, encoding="utf-8") as f:
        return cast(dict[str, Any], json.load(f))


def load_products_local() -> list[dict[str, Any]] | None:
    d = load_json(PRODUCTS_FILE)
    if not d:
        return None
    return cast(list[dict[str, Any]], d.get("groups", []))


def load_docs_local(region: str) -> list[dict[str, Any]] | None:
    d = load_json(DOCS_FILE)
    if not d:
        return None
    return cast(list[dict[str, Any]], d.get("apis", []))


def find_openapi_doc(product: str, api_name: str, region: str) -> tuple[dict, str, str, dict] | None:
    """在 data/openapi 中查找接口所在 tag 文档，返回 (doc, path, method, op)。"""
    root = os.path.join(PROOT, region_paths.openapi_out_dir(region))
    if not os.path.isdir(root):
        return None
    base = None
    target_dir = product.lower()
    for d in os.listdir(root):
        if d.lower() == target_dir:
            base = os.path.join(root, d)
            break
    if base is None:
        return None
    exact = None
    fuzzy = None
    target = api_name.lower()
    for fn in os.listdir(base):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        with open(os.path.join(base, fn), encoding="utf-8") as f:
            doc = json.load(f)
        for path, pi in (doc.get("paths") or {}).items():
            for method, op in pi.items():
                if not isinstance(op, dict):
                    continue
                opid = op.get("operationId")
                if opid == api_name:
                    return (doc, path, method, op)
                if exact is None and opid and opid.lower() == target:
                    exact = (doc, path, method, op)
                if fuzzy is None and opid and target in opid.lower():
                    fuzzy = (doc, path, method, op)
    return exact or fuzzy


# ---------- APIE 实时调用 ----------

def fetch_products_live() -> list[dict[str, Any]]:
    d = http.fetch_json(BASE_PRODUCTS, retries=4, backoff=2.0)
    return cast(list[dict[str, Any]], d.get("groups", []))


def fetch_apis_live(product_short: str) -> list[dict[str, Any]]:
    apis: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({"offset": offset, "limit": PAGE_SIZE, "product_short": product_short})
        d = http.fetch_json(f"{BASE_APIS}?{params}", retries=4, backoff=2.0)
        batch = cast(list[dict[str, Any]], d.get("api_basic_infos", []))
        apis.extend(batch)
        if not batch:
            break
        offset += len(batch)
        if offset >= (d.get("count") or offset + 1):
            break
    return apis


def fetch_detail_live(product_short: str, name: str, region: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"product_short": product_short, "name": name, "region_id": region})
    d = http.fetch_json(f"{BASE_DETAIL}?{params}", retries=4, backoff=2.0)
    if isinstance(d, dict) and d.get("error_code") == "APIEXPLORER.1055":
        fallback = urllib.parse.urlencode({"product_short": product_short, "name": name})
        d = http.fetch_json(f"{BASE_DETAIL}?{fallback}", retries=4, backoff=2.0)
    return d


# ---------- 工具函数 ----------

def extract_example(op: dict | None) -> tuple[object, object] | tuple[None, None]:
    if not isinstance(op, dict):
        return None, None
    ex = op.get("x-request-examples-1")
    desc = op.get("x-request-examples-description-1")
    if ex is None and "x-request-examples-text-1" in op:
        text = op.get("x-request-examples-text-1")
        try:
            ex = json.loads(text) if isinstance(text, str) else text
        except Exception:
            ex = text
    return ex, desc


def load_count_map() -> dict[str, int]:
    d = load_json("raw/apis_count.json")
    if not d:
        return {}
    return {g["product_short"].upper(): g["api_count"] for g in d.get("groups", [])}


def filter_by_product(apis: list[dict[str, Any]], product: str) -> list[dict[str, Any]]:
    p = product.lower()
    return [a for a in apis if (a.get("product_short") or "").lower() == p]


# ---------- 命令实现 ----------

def cmd_products(args: argparse.Namespace) -> int:
    groups = load_products_local()
    live = False
    if groups is None:
        groups = fetch_products_live()
        live = True
    counts = load_count_map() if not live else {}
    out: dict[str, Any] = {"source": "live" if live else "local",
                           "total_products": sum(len(g["products"]) for g in groups), "groups": []}
    trows: list[dict[str, Any]] = []
    for g in groups:
        name = g.get("name", "")
        prods = g.get("products", [])
        if args.category and args.category not in name:
            continue
        group_out: dict[str, Any] = {"category": name, "products": []}
        for p in prods:
            ps = p.get("productshort")
            cnt = counts.get(ps.upper()) if ps else 0
            item = {"product": ps, "name": p.get("name"),
                    "api_count": cnt, "has_data": p.get("has_data"),
                    "is_global": p.get("is_global")}
            group_out["products"].append(item)
            trows.append(item)
        out["groups"].append(group_out)
    out["_table"] = {"columns": ["product", "name", "api_count", "has_data", "is_global"], "rows": trows}
    emit(out, args.fmt)
    return 0


def cmd_product(args: argparse.Namespace) -> int:
    groups = load_products_local()
    live = False
    if groups is None:
        groups = fetch_products_live()
        live = True
    target = args.product
    found = None
    for g in groups:
        for p in g.get("products", []):
            if p.get("productshort") == target or p.get("name") == target:
                found = {**p, "category": g.get("name")}
                break
        if found:
            break
    if found is None:
        logger.error("产品 %s 未找到", target)
        return 2
    if not live:
        counts = load_count_map()
        ps = found.get("productshort")
        found["api_count"] = counts.get(ps.upper()) if ps else 0
    out = {"product": found.get("productshort"),
           "name": found.get("name"),
           "api_count": found.get("api_count") or 0,
           "category": found.get("category"),
           "is_global": found.get("is_global"),
           "is_recommend": found.get("is_recommend"),
           "link": found.get("link"),
           "attributive_product": found.get("attributive_product") or None,
           "source": "live" if live else "local"}
    if found.get("description"):
        out["description"] = found["description"]
    emit(out, args.fmt)
    return 0


def cmd_tags(args: argparse.Namespace) -> int:
    docs = load_docs_local(args.region)
    live = False
    if docs is None:
        apis = fetch_apis_live(args.product)
        live = True
    else:
        apis = filter_by_product(docs, args.product)
    if not apis:
        logger.error("产品 %s 无接口或未找到", args.product)
        return 2
    from collections import Counter
    cnt = Counter((a.get("tags") or "").strip() or "_untagged" for a in apis)
    rows = [{"tag": t, "api_count": c} for t, c in sorted(cnt.items(), key=lambda x: -x[1])]
    out = {"product": args.product, "source": "live" if live else "local",
           "total_tags": len(rows), "total_apis": len(apis), "tags": rows}
    out["_table"] = {"columns": ["tag", "api_count"], "rows": rows}
    emit(out, args.fmt)
    return 0


def cmd_apis(args: argparse.Namespace) -> int:
    docs = load_docs_local(args.region)
    live = False
    if docs is None:
        apis = fetch_apis_live(args.product)
        live = True
    else:
        apis = filter_by_product(docs, args.product)
    if args.tag:
        apis = [a for a in apis if (a.get("tags") or "").strip() == args.tag]
    if args.search:
        kw = args.search.lower()
        apis = [a for a in apis if kw in (a.get("name") or "").lower()
                or kw in (a.get("summary") or "").lower()]
    total = len(apis)
    start = args.offset
    end = start + args.limit
    page = apis[start:end]
    rows = []
    for a in page:
        rows.append({"name": a.get("name"), "method": a.get("method"), "summary": a.get("summary"),
                     "tags": a.get("tags"), "info_version": a.get("info_version")})
    out = {"product": args.product, "source": "live" if live else "local",
           "total": total, "offset": start, "limit": args.limit, "apis": rows}
    note = f"共 {total} 个接口，显示 {start + 1}-{start + len(rows)}（--limit/--offset 翻页）" if total else "无接口"
    out["_table"] = {"columns": ["name", "method", "summary", "tags"], "rows": rows, "note": note}
    emit(out, args.fmt)
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    hit = find_openapi_doc(args.product, args.api, args.region)
    if hit:
        doc, path, method, op = hit
        ex, exdesc = extract_example(op)
        out = {"product": args.product, "api": args.api, "source": "local",
               "method": method.upper(), "path": path,
               "summary": op.get("summary"), "description": op.get("description"),
               "operationId": op.get("operationId"),
               "example": ex, "example_description": exdesc,
               "parameters": op.get("parameters", []),
               "responses": op.get("responses", {}),
               "openapi_document": doc}
        emit(out, args.fmt)
        return 0
    raw = fetch_detail_live(args.product, args.api, args.region)
    if not isinstance(raw, dict) or not raw.get("paths"):
        logger.error("接口 %s 实时拉取失败: %s", args.api, raw)
        return 2
    from . import convert_openapi2 as conv
    try:
        doc = conv.convert_api(raw)
    except Exception as e:
        logger.error("规范化失败: %s", e)
        return 2
    op2: dict[str, Any] | None = None
    path2: str | None = None
    method2: str | None = None
    for p2, pi2 in (doc.get("paths") or {}).items():
        for m2, o2 in pi2.items():
            op2, path2, method2 = o2, p2, m2
            break
        if op2:
            break
    ex, exdesc = extract_example(op2)
    out = {"product": args.product, "api": args.api, "source": "live",
           "method": method2.upper() if method2 else None, "path": path2,
           "summary": op2.get("summary") if op2 else None,
           "description": op2.get("description") if op2 else None,
           "operationId": op2.get("operationId") if op2 else None,
           "example": ex, "example_description": exdesc,
           "parameters": op2.get("parameters", []) if op2 else [],
           "responses": op2.get("responses", {}) if op2 else {},
           "openapi_document": doc}
    emit(out, args.fmt)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    docs = load_docs_local(args.region)
    if docs is None:
        logger.error("本地 apis_docs 缺失，search 需先运行 api-refresh docs 拉取")
        return 2
    kw = args.keyword.lower()
    apis = docs
    if args.product:
        apis = [a for a in apis if a.get("product_short") == args.product]
    hits = [a for a in apis
            if kw in (a.get("name") or "").lower() or kw in (a.get("summary") or "").lower()
            or kw in (a.get("tags") or "").lower()]
    hits = hits[:args.limit]
    rows = [{"product": a.get("product_short"), "name": a.get("name"),
             "method": a.get("method"), "summary": a.get("summary"),
             "tags": a.get("tags")} for a in hits]
    out = {"keyword": args.keyword, "source": "local",
           "total": len(rows), "results": rows}
    if args.product:
        out["product"] = args.product
    out["_table"] = {"columns": ["product", "name", "method", "summary", "tags"], "rows": rows}
    emit(out, args.fmt)
    return 0


# ---------- 解析 ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="api-docs", description="华为云 API Explorer 渐进式查询 CLI")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default="cn-north-4", help="region（默认 cn-north-4）")
    common.add_argument("--format", dest="fmt", choices=["table", "json", "yaml"], default="table",
                        help="输出格式（默认 table）")
    common.add_argument("--log-level", default=None, help="日志级别（默认 INFO）")
    common.add_argument("--log-file", default=None, help="日志文件路径（默认 logs/api-docs.log）")

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("products", parents=[common], help="产品列表（分类分组）")
    sp.add_argument("--category", help="按分类过滤（如 计算/存储/网络）")
    sp.set_defaults(func=cmd_products)

    sp = sub.add_parser("product", parents=[common], help="单产品信息")
    sp.add_argument("product", help="product_short 或产品名")
    sp.set_defaults(func=cmd_product)

    sp = sub.add_parser("tags", parents=[common], help="产品 tag 列表")
    sp.add_argument("product")
    sp.set_defaults(func=cmd_tags)

    sp = sub.add_parser("apis", parents=[common], help="产品接口列表")
    sp.add_argument("product")
    sp.add_argument("--tag", help="按 tag 过滤")
    sp.add_argument("--search", help="按名称/summary 关键词过滤")
    sp.add_argument("--limit", type=int, default=20, help="每页条数（默认 20）")
    sp.add_argument("--offset", type=int, default=0, help="偏移量")
    sp.set_defaults(func=cmd_apis)

    sp = sub.add_parser("api", parents=[common], help="接口详情（OpenAPI 2.0）")
    sp.add_argument("product")
    sp.add_argument("api", help="接口名（operationId）")
    sp.set_defaults(func=cmd_api)

    sp = sub.add_parser("search", parents=[common], help="全局搜索接口")
    sp.add_argument("keyword")
    sp.add_argument("--product", help="收窄到指定产品")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_search)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(program="api-docs", level=args.log_level, log_file=args.log_file)
    rc = args.func(args)
    return rc if rc is not None else 0


if __name__ == "__main__":
    sys.exit(main())
