"""抓取全量接口详情 → raw/apis_detail.json（断点续传）。"""

import json
import os
import time
from typing import Any, cast

from . import http, region_paths

BASE = "https://console.huaweicloud.com/apiexplorer/new/v4/apis/detail"
REGION = region_paths.current_region()
OUT = region_paths.raw_detail_path()
CHECKPOINT = region_paths.raw_detail_partial_path()


def fetch_detail(product_short: str, name: str) -> dict[str, Any]:
    """拉取单个接口详情。

    - 正常返回详情文档
    - APIEXPLORER.1055（不分区产品）去掉 region_id 兜底重试
    - 两次 1055 返回 {"empty": True} 占位
    """
    params = {"product_short": product_short, "name": name, "region_id": REGION}
    data, err = http.fetch_json_retry(http.query_url(BASE, params))
    if err is None:
        return data
    if data.get("error_code") == "APIEXPLORER.1055":
        fallback = {"product_short": product_short, "name": name}
        data2, err2 = http.fetch_json_retry(http.query_url(BASE, fallback))
        if err2 is None:
            return data2
        if data2.get("error_code") == "APIEXPLORER.1055":
            return {"product_short": product_short, "name": name, "empty": True}
        raise err2
    raise err


def load_checkpoint() -> dict[str, Any]:
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, "r", encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))
    return {"done": {}, "failed": []}


def main() -> None:
    with open("raw/apis_docs.json", "r", encoding="utf-8") as f:
        docs: dict[str, Any] = json.load(f)

    cp = load_checkpoint()
    done: dict[str, dict[str, Any]] = cp["done"]
    failed = [f for f in cp["failed"] if f"{f['product_short']}::{f['name']}" not in done]
    api_list = docs["apis"]

    pending = []
    for a in api_list:
        key = f"{a['product_short']}::{a['name']}"
        if key not in done:
            pending.append(a)

    print(f"total={len(api_list)} done={len(done)} failed={len(failed)} pending={len(pending)}", flush=True)

    for i, a in enumerate(pending, 1):
        key = f"{a['product_short']}::{a['name']}"
        try:
            det = fetch_detail(a["product_short"], a["name"])
            done[key] = det
            if len(done) % 50 == 0:
                with open(CHECKPOINT, "w", encoding="utf-8") as f:
                    json.dump({"done": done, "failed": failed}, f, ensure_ascii=False)
                print(f"  checkpoint: {len(done)}/{len(api_list)}", flush=True)
        except Exception as e:
            print(f"  FAIL {key}: {e}", flush=True)
            failed.append({"product_short": a["product_short"], "name": a["name"], "error": str(e)})
            with open(CHECKPOINT, "w", encoding="utf-8") as f:
                json.dump({"done": done, "failed": failed}, f, ensure_ascii=False)
        time.sleep(0.2)

    result = {
        "region_id": REGION,
        "total_apis": len(done),
        "failed": failed,
        "apis": done,
        "source": BASE,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)

    print(f"\nDone. total={len(done)} failed={len(failed)}", flush=True)


if __name__ == "__main__":
    main()
