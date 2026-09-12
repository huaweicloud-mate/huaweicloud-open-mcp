"""抓取接口索引 → raw/apis_docs.json。

索引拉取委托 apie.explorer（注册域 /v2/apis，方言归一、内部分页）。
"""

import json
import logging
import time
from typing import Any

from . import explorer

logger = logging.getLogger("apie.fetch_apis")


def fetch_product(product_short: str) -> list[dict[str, Any]]:
    """拉取产品全量接口索引（explorer 内分页，页间礼貌限速）。"""
    return explorer.fetch_apis(product_short, page_sleep=0.3)


def main() -> None:
    with open("raw/apis_count.json", "r", encoding="utf-8") as f:
        count_data: dict[str, Any] = json.load(f)

    products = count_data["groups"]
    all_apis: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    for i, p in enumerate(products, 1):
        short, n = p["product_short"], p["api_count"]
        logger.info("[%d/%d] %s (%d)", i, len(products), short, n)
        try:
            apis = fetch_product(short)
            all_apis.extend(apis)
            if len(apis) != n:
                logger.warning("%s expected %d, got %d", short, n, len(apis))
        except Exception as e:
            logger.error("%s: %s", short, e)
            failed.append({"product_short": short, "error": str(e)})
        time.sleep(0.3)

    result = {
        "total_products": len(products),
        "products_failed": len(failed),
        "total_apis": len(all_apis),
        "failed": failed,
        "apis": all_apis,
        "source": explorer.BASE,
    }
    with open("raw/apis_docs.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info("Done. total_apis=%d failed=%d", len(all_apis), len(failed))
    if failed:
        logger.error("Failed products: %s", [x["product_short"] for x in failed])


if __name__ == "__main__":
    main()
