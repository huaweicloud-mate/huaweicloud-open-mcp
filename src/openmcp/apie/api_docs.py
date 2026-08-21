"""api-docs：API Explorer 渐进式查询 CLI。

产品 → 产品 tag → 接口列表 → 接口详情，Agent 友好。
复用 tools.metadata 纯函数避免重复逻辑。
"""

import argparse
import json
import logging
import sys

from ..apie import catalog
from ..apie.memory_store import MemoryStore
from ..logconf import configure_logging
from ..tools import metadata

logger = logging.getLogger("openmcp.apie.api_docs")

_store = MemoryStore()


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


# ---------- 命令实现 ----------

def cmd_products(args: argparse.Namespace) -> int:
    groups = catalog.get_products(_store) or []
    r = metadata.list_products(groups, counts=catalog.get_api_counts(_store),
                               category=args.category, keyword=args.keyword)
    products = r["products"]
    trows = [dict(p) for p in products]
    out = {"total_products": len(products), "products": products}
    out["_table"] = {"columns": ["product", "name", "api_count", "is_global",
                                  "category"], "rows": trows}
    emit(out, args.fmt)
    return 0


def cmd_product(args: argparse.Namespace) -> int:
    groups = catalog.get_products(_store) or []
    p = metadata.get_product(groups, args.product, counts=catalog.get_api_counts(_store))
    if p is None:
        logger.error("产品 %s 未找到", args.product)
        return 2
    emit(dict(p), args.fmt)
    return 0


def cmd_tags(args: argparse.Namespace) -> int:
    apis = catalog.get_apis(_store, args.product)
    if not apis:
        logger.error("产品 %s 无接口或未找到", args.product)
        return 2
    tag_groups = metadata.list_apis(apis, args.product)["tag_groups"]
    rows = [dict(t) for t in tag_groups]
    out = {"product": args.product, "total_tags": len(rows),
           "total_apis": len(apis), "tags": rows}
    out["_table"] = {"columns": ["tag", "api_count"], "rows": rows}
    emit(out, args.fmt)
    return 0


def cmd_apis(args: argparse.Namespace) -> int:
    apis = catalog.get_apis(_store, args.product)
    if not apis:
        logger.error("产品 %s 无接口或未找到", args.product)
        return 2
    r = metadata.list_apis(apis, args.product, tag=args.tag, search=args.search,
                           limit=args.limit, offset=args.offset)
    items = r["apis"]
    rows = [dict(a) for a in items]
    out = {"product": args.product, "total": r["total"],
           "offset": r["offset"], "limit": r["limit"], "apis": rows}
    note = (f"共 {r['total']} 个接口，显示 {args.offset + 1}-"
            f"{args.offset + len(rows)}（--limit/--offset 翻页）") if r["total"] else "无接口"
    out["_table"] = {"columns": ["name", "method", "summary", "tags"], "rows": rows, "note": note}
    emit(out, args.fmt)
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    hit = catalog.find_api_doc(_store, args.product, args.api, args.region)
    if not hit:
        logger.error("接口 %s 未找到（产品 %s）", args.api, args.product)
        return 2
    doc, path, method, op = hit
    details = metadata.format_api_detail(doc, args.product, path, method, op)
    examples = metadata.extract_examples(op)
    out = {**details, "examples": examples, "source": "remote"}
    emit(out, args.fmt)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not args.product:
        logger.error("search 需指定 --product")
        return 2
    apis = catalog.get_apis(_store, args.product)
    if apis is None:
        logger.error("产品 %s 接口列表不可用（远端拉取失败）", args.product)
        return 2
    r = metadata.list_apis(apis, args.product, search=args.keyword,
                           limit=args.limit, offset=0)
    rows = [{"product": args.product, **dict(a)} for a in r["apis"]]
    out = {"keyword": args.keyword, "total": r["total"], "results": rows,
           "product": args.product}
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
    sp.add_argument("--keyword", default=None, help="按产品名/中文名关键词搜索")
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

    sp = sub.add_parser("search", parents=[common], help="按产品搜索接口")
    sp.add_argument("keyword")
    sp.add_argument("--product", required=True, help="收窄到指定产品")
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
