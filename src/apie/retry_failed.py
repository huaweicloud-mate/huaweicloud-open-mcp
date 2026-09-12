"""重试 raw/apis_detail.json 中 failed 项（429 大退避）。

详情拉取委托 apie.explorer（注册域 /v3/apis/detail，方言归一；
1055 去 region 兜底在 explorer 内部），注入 429 大退避 fetcher。
"""

import json
import logging
import time
from typing import Any

from common import http

from . import explorer, region_paths

logger = logging.getLogger("apie.retry_failed")

DETAIL_PATH = region_paths.raw_detail_path()


def fetch_detail(product_short: str, name: str) -> tuple[dict[str, Any], Any]:
    """拉取单个接口详情（429 自动大退避重试）。返回 (body, error)。

    - 正常返回详情文档（explorer 归一 product_short）
    - 二次 1055（接口不存在）返回 {"empty": True} 占位
    - 其它 HTTP/网络错误返回 ({}, err)
    """
    try:
        return explorer.fetch_detail(
            product_short, name, region_paths.current_region(),
            fetch_json=http.fetch_json_429), None
    except explorer.ApiNotFoundError:
        return {"product_short": product_short, "name": name, "empty": True}, None
    except Exception as e:
        return {}, e


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
