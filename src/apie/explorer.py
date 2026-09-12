"""API Explorer 注册域数据源：方言归一，运行时与构建期管道共用的深模块。

端点为 API Explorer 自身注册的开放接口（apiexplorer.cn-north-4.myhuaweicloud.com，
无需凭证），URL、参数名、信封键、字段名与 1055 语义等远端方言知识全部沉入本模块，
出口恒为项目内部契约：

- fetch_products → groups 列表（/v4/products，api_count 为真实值）
- fetch_apis     → api_basic_infos 形制条目（/v2/apis，product_short 驼峰、method 小写）
- fetch_detail   → Swagger 2.0 详情文档（/v3/apis/detail，product_short 归一；
                   1055 自动去 region 兜底，二次 1055 → ApiNotFoundError）
- counts         → raw/apis_count.json 产物契约（v4/products 派生，api_count>0 过滤）

实测口径（独立真值）：/v3/apis/detail 与 console /new/v4/apis/detail 的
paths/definitions 逐字节一致，仅 productshort 字段名差异；1055 = HTTP 400；
/v2/apis 的 productshort 大小写不敏感（AgentArts/AGENTARTS 同结果）。
"""

import logging
import time
import urllib.parse
from typing import Any, Callable, cast

from common import http

logger = logging.getLogger("apie.explorer")

BASE = "https://apiexplorer.cn-north-4.myhuaweicloud.com"
PAGE_SIZE = 100

# 注册接口「API 不存在」错误码（HTTP 400；不分区产品去 region_id 兜底后仍 1055 → 确认缺失）
ERROR_NOT_FOUND = "APIEXPLORER.1055"

# detail 用 (body, err) 元组契约（http.fetch_json_retry / fetch_json_429）；
# products/apis 用 dict-only 契约（http.fetch_json）。
DetailFetcher = Callable[[str], tuple[dict[str, Any], Any]]
JsonFetcher = Callable[[str], dict[str, Any]]


class ApiNotFoundError(Exception):
    """接口在产品目录中不存在（1055 兜底后仍缺失）。"""


# ---------- 内部接缝：方言归一（单测直测，非调用方接口） ----------

def _is_1055(body: Any) -> bool:
    return isinstance(body, dict) and body.get("error_code") == ERROR_NOT_FOUND


def _normalize_detail(data: Any) -> Any:
    """详情文档归一：productshort → product_short（copy-on-write，其余透传）。"""
    if not isinstance(data, dict) or "productshort" not in data:
        return data
    out = dict(data)
    out["product_short"] = out.pop("productshort")
    return out


def _normalize_index_batch(data: dict[str, Any]) -> list[dict[str, Any]]:
    """索引批归一：apis → 条目列表；productshort → product_short；method 小写。"""
    out: list[dict[str, Any]] = []
    for e in data.get("apis") or []:
        item = dict(e)
        if "productshort" in item:
            item["product_short"] = item.pop("productshort")
        if isinstance(item.get("method"), str):
            item["method"] = item["method"].lower()
        out.append(item)
    return out


def _flatten_products(products_doc: dict[str, Any]) -> list[tuple[str, int]]:
    """products groups 平铺：[(product_short, api_count)]（保持源顺序）。"""
    flat: list[tuple[str, int]] = []
    for g in (products_doc or {}).get("groups") or []:
        for p in g.get("products") or []:
            flat.append((p.get("productshort") or "", p.get("api_count") or 0))
    return flat


def _detail_url(product: str, name: str, region: str | None) -> str:
    params: dict[str, str] = {"productshort": product, "name": name}
    if region:
        params["region_id"] = region
    return f"{BASE}/v3/apis/detail?{urllib.parse.urlencode(params)}"


def _apis_url(product: str, offset: int) -> str:
    params = {"offset": offset, "limit": PAGE_SIZE, "productshort": product}
    return f"{BASE}/v2/apis?{urllib.parse.urlencode(params)}"


# ---------- 接口 ----------

def fetch_products(*, fetch_json: Callable[[str], dict[str, Any]] | None = None
                   ) -> list[dict[str, Any]]:
    """产品列表 groups（v4/products；api_count 为真实值、个别 is_global 更准）。"""
    fetch = fetch_json or http.fetch_json
    d = fetch(f"{BASE}/v4/products")
    groups = d.get("groups") if isinstance(d, dict) else None
    if groups is None:
        raise ValueError(f"unexpected products response: {d!r}"[:200])
    return cast(list[dict[str, Any]], groups)


def fetch_apis(product: str, *, page_sleep: float = 0.0,
               fetch_json: Callable[[str], dict[str, Any]] | None = None
               ) -> list[dict[str, Any]]:
    """产品接口索引：分页沉入实现，出口 api_basic_infos 形制。

    终止：offset 达响应 count（缺失时退化为空批 break）或空批。
    page_sleep 供批量管道页间礼貌限速（默认 0，交互路径不休眠）。
    """
    fetch = fetch_json or http.fetch_json
    apis: list[dict[str, Any]] = []
    offset = 0
    while True:
        d = fetch(_apis_url(product, offset))
        if not isinstance(d, dict) or "apis" not in d:
            raise ValueError(f"unexpected apis response for {product} "
                             f"offset={offset}: {d!r}"[:200])
        batch = _normalize_index_batch(d)
        apis.extend(batch)
        if not batch:
            break
        offset += len(batch)
        if offset >= (d.get("count") or offset + 1):
            break
        if page_sleep:
            time.sleep(page_sleep)
    return apis


def fetch_detail(product: str, name: str, region: str | None = None, *,
                 fetch_json: DetailFetcher | None = None) -> dict[str, Any]:
    """接口详情：Swagger 2.0 形制，product_short 归一。

    - 1055（带 region 查询）→ 自动去 region 兜底重试（不分区产品）
    - 二次 1055 → ApiNotFoundError（调用方自行映射占位/None 语义）
    - 其它 HTTP/网络错误 → 原样抛出 fetcher 的 err
    """
    fetch = fetch_json or http.fetch_json_retry
    data, err = fetch(_detail_url(product, name, region))
    if _is_1055(data) and region:
        data, err = fetch(_detail_url(product, name, None))
    if _is_1055(data):
        raise ApiNotFoundError(f"api not found: {product}::{name}")
    if err is not None:
        raise err
    return cast(dict[str, Any], _normalize_detail(data))


def counts(*, fetch_json: Callable[[str], dict[str, Any]] | None = None
           ) -> dict[str, Any]:
    """产品接口计数（v4/products 派生，raw/apis_count.json 产物契约）。

    仅收录 api_count>0 的产品（对齐 v1/count 口径）；键取源站驼峰 productshort。
    """
    groups = [{"product_short": ps, "api_count": n}
              for ps, n in _flatten_products({"groups": fetch_products(
                  fetch_json=fetch_json)}) if n > 0]
    return {
        "total_api_count": sum(g["api_count"] for g in groups),
        "total_products": len(groups),
        "groups": groups,
        "source": f"{BASE}/v4/products",
    }
