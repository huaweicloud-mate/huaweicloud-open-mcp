"""helphints 阶段：raw 产物 → 补全列表 + 报告 + S10 hints 文件。

输入 raw/help_docs.json（crawl 产物）+ raw/apis_docs.json +
raw/huawei_products.json + raw 详情（apis_detail.json，region 感知）；
输出 data/help_completions/{help_completions,report}.json 与
data/hints/help-docs-hints.json（api_notes_in_list_apis=false）。
纯文件编排：匹配/差集/生成逻辑全部在 apie.help_docs。
"""

import json
import logging
from pathlib import Path
from typing import Any

from .help_docs import (
    build_alias_index,
    build_hints,
    detail_descriptions,
    diff_completions,
    match_apis,
)

logger = logging.getLogger("apie.build_help_hints")

DEFAULT_DETAIL_NAME = "apis_detail.json"


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def build_completions(raw_dir: Path, data_dir: Path, *,
                      detail_path: Path | None = None,
                      products: list[str] | None = None,
                      min_gain: int = 20, cap: int = 2000,
                      overrides: dict[str, str] | None = None) -> dict:
    """匹配 → 差集 → 落盘三产物，返回 report 摘要。

    - detail_path 缺省取 raw_dir/apis_detail.json（真实调用传 region 感知路径）；
    - products 过滤（PRODUCT_UPPER 白名单，试点用）；
    - 覆盖门不设：产物按时间戳重写（数据产物可重建）。
    """
    help_docs = _read_json(Path(raw_dir) / "help_docs.json")
    records = list((help_docs.get("records") or {}).values())
    apis_index = _read_json(Path(raw_dir) / "apis_docs.json").get("apis") or []
    product_groups = _read_json(Path(raw_dir) / "huawei_products.json").get("groups") or []
    detail_file = Path(detail_path) if detail_path else Path(raw_dir) / DEFAULT_DETAIL_NAME
    apis_detail = _read_json(detail_file) if detail_file.exists() else {"apis": {}}

    alias_index = build_alias_index(product_groups)
    m = match_apis(apis_index, records, alias_index, overrides=overrides)
    matched = m["matched"]
    if products:
        wanted = {p.upper() for p in products}
        matched = [x for x in matched if x.get("product") in wanted]
    descs = detail_descriptions(apis_detail)
    completions = diff_completions(descs, matched, min_gain=min_gain)

    per_product: dict[str, int] = {}
    for c in completions:
        per_product[c["product"]] = per_product.get(c["product"], 0) + 1
    # 废弃索引（S14）：与差集口径解耦——匹配成功的废弃页即入索引；
    # 替代接口名/文档 URL 供运行时 annotate 标注
    rec_by_url = {r.get("url"): r for r in records}
    deprecated: dict[str, dict[str, Any]] = {}
    for mt in matched:
        rec = rec_by_url.get(mt.get("url")) or {}
        if not rec.get("is_deprecated"):
            continue
        deprecated.setdefault(mt.get("product"), {})[(mt.get("api") or "").lower()] = {
            "replacement": rec.get("replacement"),
            "doc_url": mt.get("url"),
        }
    report = {
        "records": len(records),
        "matched": len(matched),
        "completions": len(completions),
        "deprecated": sum(len(v) for v in deprecated.values()),
        "unmatched": len(m["unmatched"]),
        "ambiguous": len(m["ambiguous"]),
        "unmatched_items": m["unmatched"],
        "ambiguous_items": m["ambiguous"],
        "per_product": dict(sorted(per_product.items())),
        "min_gain": min_gain,
        "cap": cap,
    }

    _write_json(Path(data_dir) / "help_completions" / "help_completions.json",
                completions)
    _write_json(Path(data_dir) / "help_completions" / "report.json", report)
    _write_json(Path(data_dir) / "help_completions" / "deprecated.json",
                {"products": deprecated})
    _write_json(Path(data_dir) / "hints" / "help-docs-hints.json",
                build_hints(completions, cap=cap))
    logger.info("helphints matched=%d completions=%d unmatched=%d ambiguous=%d",
                report["matched"], report["completions"],
                report["unmatched"], report["ambiguous"])
    return report


def main() -> int:
    """api-refresh helphints 阶段入口。"""
    import argparse

    from common.logconf import configure_logging
    from common.paths import project_root

    from . import region_paths

    p = argparse.ArgumentParser(prog="api-refresh helphints")
    p.add_argument("--product", action="append", default=None,
                   help="产品白名单（可重复，缺省全产品）")
    p.add_argument("--min-gain", type=int, default=20,
                   help="补全最小增益字符数（默认 20）")
    p.add_argument("--cap", type=int, default=2000,
                   help="单条 note 功能介绍截断上限（默认 2000）")
    p.add_argument("--region", default="cn-north-4",
                   help="详情产物 region（默认 cn-north-4）")
    p.add_argument("--overrides", default=None,
                   help="docset→产品覆盖映射 JSON（configs/helpdoc-overrides.example.json）")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-file", default=None)
    args = p.parse_args()
    configure_logging(program="api-refresh-helphints",
                      level=args.log_level, log_file=args.log_file)
    root = Path(project_root())
    overrides = None
    if args.overrides:
        overrides = {k: v for k, v in
                     _read_json(Path(args.overrides)).items()}
    report = build_completions(
        raw_dir=root / "raw", data_dir=root / "data",
        detail_path=root / region_paths.raw_detail_path(args.region),
        products=args.product, min_gain=args.min_gain, cap=args.cap,
        overrides=overrides)
    logger.info("summary: completions=%d matched=%d",
                report["completions"], report["matched"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
