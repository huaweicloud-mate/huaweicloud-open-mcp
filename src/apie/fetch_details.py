"""抓取全量接口详情 → raw/apis_detail.json（断点续传）。

详情拉取委托 apie.explorer（注册域 /v3/apis/detail，方言归一；
1055 去 region 兜底在 explorer 内部）。
"""

import json
import logging
import os
import time
from typing import Any, cast

from . import explorer, region_paths

logger = logging.getLogger("apie.fetch_details")

OUT = region_paths.raw_detail_path()
CHECKPOINT = region_paths.raw_detail_partial_path()


def fetch_detail(product_short: str, name: str) -> dict[str, Any]:
    """拉取单个接口详情。

    - 正常返回详情文档（explorer 归一 product_short）
    - 二次 1055（接口不存在）返回 {"empty": True} 占位
    - 其它 HTTP/网络错误原样抛出（进 failed 台账）
    """
    try:
        return explorer.fetch_detail(product_short, name, region_paths.current_region())
    except explorer.ApiNotFoundError:
        return {"product_short": product_short, "name": name, "empty": True}


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

    logger.info("total=%d done=%d failed=%d pending=%d",
                len(api_list), len(done), len(failed), len(pending))

    for i, a in enumerate(pending, 1):
        key = f"{a['product_short']}::{a['name']}"
        try:
            det = fetch_detail(a["product_short"], a["name"])
            done[key] = det
            if len(done) % 50 == 0:
                with open(CHECKPOINT, "w", encoding="utf-8") as f:
                    json.dump({"done": done, "failed": failed}, f, ensure_ascii=False)
                logger.info("checkpoint: %d/%d", len(done), len(api_list))
        except Exception as e:
            logger.warning("FAIL %s: %s", key, e)
            failed.append({"product_short": a["product_short"], "name": a["name"], "error": str(e)})
            with open(CHECKPOINT, "w", encoding="utf-8") as f:
                json.dump({"done": done, "failed": failed}, f, ensure_ascii=False)
        time.sleep(0.2)

    result = {
        "region_id": region_paths.current_region(),
        "total_apis": len(done),
        "failed": failed,
        "apis": done,
        "source": explorer.BASE,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)

    logger.info("Done. total=%d failed=%d", len(done), len(failed))
    if failed:
        logger.error("Failed items: %s", [(f["product_short"], f["name"]) for f in failed])


if __name__ == "__main__":
    main()
