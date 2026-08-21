"""重试 raw/apis_detail.json 中 failed 项（429 大退避）。"""

import json
import logging
import time
from typing import Any

from common import http

from . import region_paths

logger = logging.getLogger("apie.retry_failed")

BASE = "https://console.huaweicloud.com/apiexplorer/new/v4/apis/detail"
REGION = region_paths.current_region()
DETAIL_PATH = region_paths.raw_detail_path()


def fetch_detail(product_short: str, name: str) -> tuple[dict[str, Any], Any]:
    """拉取单个接口详情（429 自动大退避重试）。返回 (body, error)。"""
    params = {"product_short": product_short, "name": name, "region_id": REGION}
    data, err = http.fetch_json_429(http.query_url(BASE, params))
    if err is None:
        return data, None
    if data.get("error_code") == "APIEXPLORER.1055":
        fallback = {"product_short": product_short, "name": name}
        data2, err2 = http.fetch_json_429(http.query_url(BASE, fallback))
        if err2 is None:
            return data2, None
        if data2.get("error_code") == "APIEXPLORER.1055":
            return {"product_short": product_short, "name": name, "empty": True}, None
        return data2, err2
    return data, err


def main() -> None:
    with open(DETAIL_PATH, "r", encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)

    failed = result["failed"]
    done: dict[str, dict[str, Any]] = result["apis"]
    still_failed: list[dict[str, str]] = []

    logger.info("retrying %d failed items", len(failed))
    for f in failed:
        key = f"{f['product_short']}::{f['name']}"
        try:
            det, err = fetch_detail(f["product_short"], f["name"])
            if err is None:
                done[key] = det
                logger.debug("OK %s", key)
            else:
                still_failed.append({**f, "error": str(err)})
                logger.warning("FAIL %s: %s", key, err)
        except Exception as e:
            still_failed.append({**f, "error": str(e)})
            logger.warning("FAIL %s: %s", key, e)
        time.sleep(2)

    result["apis"] = done
    result["failed"] = still_failed
    result["total_apis"] = len(done)
    with open(DETAIL_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("Done. total=%d failed=%d", len(done), len(still_failed))


if __name__ == "__main__":
    main()
