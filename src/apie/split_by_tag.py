"""把 raw/apis_detail.json 按 产品/tag 切分。"""

import collections
import json
import logging
import os
import shutil

logger = logging.getLogger("apie.split_by_tag")


def split_by_tag(src: str, out_dir: str) -> tuple[int, dict[str, dict[str, int]]]:
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    with open(src, "r", encoding="utf-8") as f:
        detail = json.load(f)

    buckets_by_product: collections.defaultdict[str, dict] = collections.defaultdict(dict)
    for key, val in detail["apis"].items():
        ps, _, name = key.partition("::")
        tag = (val.get("tags") or "").strip()
        if not tag:
            tag = "_untagged"
        buckets_by_product[ps][(tag, name)] = val

    total_api = 0
    product_summary = {}

    for ps, items in sorted(buckets_by_product.items()):
        pdir = os.path.join(out_dir, ps)
        os.makedirs(pdir, exist_ok=True)
        tag_buckets: collections.defaultdict[str, dict] = collections.defaultdict(dict)
        for (tag, name), val in items.items():
            tag_buckets[tag][f"{ps}::{name}"] = val
        count = 0
        for tag, tag_items in sorted(tag_buckets.items()):
            safe_tag = tag.replace("/", "_").replace("\\", "_").strip()
            tfn = os.path.join(pdir, f"{safe_tag}.json")
            with open(tfn, "w", encoding="utf-8") as f:
                json.dump({"product_short": ps, "tag": tag, "api_count": len(tag_items), "apis": tag_items},
                          f, ensure_ascii=False, indent=2)
            count += len(tag_items)
        total_api += count
        product_summary[ps] = {tag: len(v) for tag, v in tag_buckets.items()}

    index = {
        "total_apis": total_api,
        "products": len(product_summary),
        "tags_per_product": product_summary,
    }
    with open(os.path.join(out_dir, "_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    return total_api, product_summary


def main() -> None:
    from . import region_paths
    total, summary = split_by_tag(region_paths.raw_detail_path(), region_paths.by_tag_dir())
    logger.info("total apis: %d", total)
    logger.info("products: %d", len(summary))


if __name__ == "__main__":
    main()
