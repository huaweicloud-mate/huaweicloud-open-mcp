"""抓取接口索引 → raw/apis_docs.json。"""

import json
import time
import urllib.parse
import urllib.request

BASE = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"
PAGE_SIZE = 100


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == 4:
                raise
            print(f"  retry {attempt + 1} after error: {e}", flush=True)
            time.sleep(2 * (attempt + 1))


def fetch_product(product_short, api_count):
    apis = []
    offset = 0
    while offset < api_count:
        params = urllib.parse.urlencode({"offset": offset, "limit": PAGE_SIZE, "product_short": product_short})
        data = fetch(f"{BASE}?{params}")
        if "api_basic_infos" not in data:
            raise RuntimeError(f"{product_short} offset={offset}: unexpected response: {data}")
        batch = data["api_basic_infos"]
        apis.extend(batch)
        if not batch:
            break
        offset += len(batch)
        time.sleep(0.3)
    return apis


def main():
    with open("raw/apis_count.json", "r", encoding="utf-8") as f:
        count_data = json.load(f)

    products = count_data["groups"]
    all_apis = []
    failed = []

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
