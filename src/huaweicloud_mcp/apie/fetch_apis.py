"""抓取接口索引 → raw/apis_docs.json。"""

import json
import time
from typing import Any

from . import http

BASE = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"
PAGE_SIZE = 100


def fetch_product(product_short: str, api_count: int) -> list[dict[str, Any]]:
    apis: list[dict[str, Any]] = []
    offset = 0
    while offset < api_count:
        params = {"offset": offset, "limit": PAGE_SIZE, "product_short": product_short}
        data = http.fetch_json(http.query_url(BASE, params))
        if "api_basic_infos" not in data:
            raise RuntimeError(f"{product_short} offset={offset}: unexpected response: {data}")
        batch = data["api_basic_infos"]
        apis.extend(batch)
        if not batch:
            break
        offset += len(batch)
        time.sleep(0.3)
    return apis


def main() -> None:
    with open("raw/apis_count.json", "r", encoding="utf-8") as f:
        count_data: dict[str, Any] = json.load(f)

    products = count_data["groups"]
    all_apis: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    for i, p in enumerate(products, 1):
        short, n = p["product_short"], p["api_count"]
        print(f"[{i}/{len(products)}] {short} ({n})", flush=True)
        try:
            apis = fetch_product(short, n)
            all_apis.extend(apis)
            if len(apis) != n:
                print(f"  WARN: expected {n}, got {len(apis)}", flush=True)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            failed.append({"product_short": short, "error": str(e)})
        time.sleep(0.3)

    result = {
        "total_products": len(products),
        "products_failed": len(failed),
        "total_apis": len(all_apis),
        "failed": failed,
        "apis": all_apis,
        "source": BASE,
    }
    with open("raw/apis_docs.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nDone. total_apis={len(all_apis)}, failed={len(failed)}")
    if failed:
        print("Failed products:", [x["product_short"] for x in failed])


if __name__ == "__main__":
    main()
