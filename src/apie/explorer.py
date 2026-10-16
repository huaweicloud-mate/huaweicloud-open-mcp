"""API Explorer 元数据数据源：方言归一，运行时与构建期管道共用的深模块。

端点为 API Explorer 自身注册的开放接口（apiexplorer.cn-north-4.myhuaweicloud.com，
无需凭证）+ console 前端内部接口（索引快路径，2026-10 双源——见 fetch_apis），
URL、参数名、信封键、字段名与 1055 语义等远端方言知识全部沉入本模块，
出口恒为项目内部契约：

- fetch_products → groups 列表（/v4/products，api_count 为真实值）
- fetch_apis     → api_basic_infos 形制条目（运行时 console /new/v3/apis 优先、
                   注册域 /v2/apis 页级回落；product_short 驼峰、method 小写；
                   构建期管道经 console_first=False 钉注册域单源）
- fetch_detail   → Swagger 2.0 详情文档（/v3/apis/detail，product_short 归一；
                   1055 自动去 region 兜底，二次 1055 → ApiNotFoundError）
- counts         → raw/apis_count.json 产物契约（v4/products 派生，api_count>0 过滤）

实测口径（独立真值）：/v3/apis/detail 与 console /new/v4/apis/detail 的
paths/definitions 逐字节一致，仅 productshort 字段名差异；1055 = HTTP 400；
/v2/apis 的 productshort 大小写不敏感（AgentArts/AGENTARTS 同结果）；
console /new/v3/apis 参数 product_short（下划线）、信封 api_basic_infos、
method 大写、count 同名（2026-10 实测，analysis-report.md 第 7 章）。
"""

import logging
import time
import urllib.parse
from typing import Any, Callable, cast

from common import http

logger = logging.getLogger("apie.explorer")

BASE = "https://apiexplorer.cn-north-4.myhuaweicloud.com"
CONSOLE_APIS_BASE = "https://console.huaweicloud.com/apiexplorer/new/v3/apis"
PAGE_SIZE = 100

# console 腿单次尝试短超时（健康页 120-300 ms；回落即重试——快路径可牺牲，
# 见 analysis-report.md 第 7 章 / design.md 接缝 B 不对称预算）
CONSOLE_TIMEOUT = 5

# 注册接口「API 不存在」错误码（HTTP 400；不分区产品去 region_id 兜底后仍 1055 → 确认缺失）
ERROR_NOT_FOUND = "APIEXPLORER.1055"

# detail 用 (body, err) 元组契约（http.fetch_json_retry / fetch_json_429）；
# products 用 dict-only 契约（http.fetch_json）；apis 缺省 429 感知 adapter
# （http.fetch_json_429_raise——运行时索引腿，见 fetch_apis）。
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


def _console_apis_url(product: str, offset: int) -> str:
    params = {"offset": offset, "limit": PAGE_SIZE, "product_short": product}
    return f"{CONSOLE_APIS_BASE}?{urllib.parse.urlencode(params)}"


def _console_batch(d: dict[str, Any]) -> list[dict[str, Any]] | None:
    """console 信封宽容提取：api_basic_infos（console 方言）或 apis（注册域形）
    任一可用 → 条目列表；否则 None（调用方页级回落注册域）。"""
    batch = d.get("api_basic_infos")
    if batch is None and "apis" in d:
        batch = d["apis"]
    return batch if isinstance(batch, list) else None


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


def fetch_apis(product: str, *, page_sleep: float = 0.0, console_first: bool = True,
               fetch_json: Callable[[str], dict[str, Any]] | None = None
               ) -> list[dict[str, Any]]:
    """产品接口索引：分页沉入实现，出口 api_basic_infos 形制（跨源归一）。

    双源（console 优先 + 注册域页级回落，design.md R9）：
    - console 腿单次尝试、短超时、零重试——回落即重试（快路径可牺牲）；
    - per-call 粘性：同一次调用内 console 首败后剩余页直走注册域；
    - 出口 (name, method) 去重（跨源排序 parity 未实测验证的防御）；
    - 两源均终态失败 → 抛异常、零缓存（跨调用无 checkpoint）。

    终止：offset 达当前页源声明的 count（缺失时退化为空批 break）或空批。
    page_sleep 节流页、不节流页内回退；回退页计一页。
    console_first=False 钉注册域单源（构建期管道——产物可复现性）。
    """
    fetch = fetch_json or http.fetch_json_429_raise

    def console_leg(url: str) -> dict[str, Any]:
        # console 腿单次尝试、短超时（回落即重试——快路径可牺牲）。与注册域腿
        # 同走 adapter：429/网络/挑战页错统一抛出 → except 回落，且测试封网点
        # 单一（monkeypatch http.fetch_json_429 同时封两腿）。注入 fetch_json
        # 时注入点覆盖两腿（既有 fake fetcher 契约不变）。
        return http.fetch_json_429_raise(url, retries=1, timeout=CONSOLE_TIMEOUT)

    apis: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    use_console = console_first
    mixed_sources = False   # 源切换发生过才启用去重丢弃（单源语义不变；键恒收集）
    con_fetch = fetch_json if fetch_json is not None else console_leg
    offset = 0
    while True:
        batch: list[dict[str, Any]] | None = None
        count: int | None = None
        con_doc: dict[str, Any] | None = None
        if use_console:
            try:
                con_doc = con_fetch(_console_apis_url(product, offset))
            except Exception as e:
                con_doc = None
                logger.warning("console index page failed product=%s offset=%d "
                               "(%r), falling back to registered", product, offset, e)
        if isinstance(con_doc, dict):
            batch = _console_batch(con_doc)
            count = con_doc.get("count")
        if batch is None:
            if use_console:
                use_console = False   # per-call 粘性：本产品剩余页直走注册域
                mixed_sources = True
            d = fetch(_apis_url(product, offset))
            if not isinstance(d, dict) or "apis" not in d:
                raise ValueError(f"unexpected apis response for {product} "
                                 f"offset={offset}: {d!r}"[:200])
            batch = _normalize_index_batch(d)
            count = d.get("count")
        else:
            batch = _normalize_index_batch({"apis": batch})
        page: list[dict[str, Any]] = []
        for item in batch:
            key = (str(item.get("name") or ""), str(item.get("method") or ""))
            if key != ("", ""):
                if mixed_sources and key in seen:
                    logger.warning("duplicate api dropped (cross-source shift): "
                                   "%s %s", product, key)
                    continue
                seen.add(key)
            page.append(item)
        apis.extend(page)
        if not batch:
            break
        offset += len(batch)
        if offset >= (count if count is not None else offset + 1):
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
