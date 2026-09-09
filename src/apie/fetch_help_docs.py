"""抓取帮助中心 API 文档页 → raw/help_docs.json（断点续传）。

与 fetch_details 同风格：断点续传 + 失败台账；网络边界经 fetch_fn 注入
（真实适配器 = common.http.open_text + 浏览器 UA），挑战页指数退避，
限速默认 0.4s/页。站点格式知识在 apie.help_docs（纯函数核心）。

阶段用法（api-refresh helpdocs）：
    sitemap → 过滤 docset（默认 api-*）→ 逐页抓取解析 → 记录 + 台账。
重跑语义：断点/已有产物中 done 的 URL 跳过，failed 自动重试。
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Sequence, cast

from common import http

from .help_docs import (
    extract_func_intro,
    extract_links,
    extract_replacement,
    is_challenge,
    parse_api_page,
    parse_sitemap,
)

logger = logging.getLogger("apie.fetch_help_docs")

SITEMAP_URL = "https://support.huaweicloud.com/sitemap.xml"
CHECKPOINT = "help_docs_partial.json"
ARTIFACT = "help_docs.json"

HTML_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def fetch_html(url: str) -> str:
    """单次抓取适配器：HTTP 错误抛异常，由 Crawler 统一重试。

    404/410 属确定性死链（sitemap 常含陈旧条目），包装为 PermanentError
    让 Crawler 快速失败，不进重试梯子。
    """
    text, err = http.open_text(url, timeout=60, headers=HTML_HEADERS)
    if err is None:
        return text
    if err.code in (404, 410):
        raise http.PermanentError(f"HTTP Error {err.code}")
    raise err


class ChallengeError(Exception):
    """人机验证挑战页退避耗尽。"""


class Crawler:
    """限速 + 挑战退避 + 瞬时重试的最小抓取器。

    fetch_fn(url) -> str：单次抓取（HTTP 错误抛异常）；
    get(url) 语义：每次请求前 sleep(rate)；网络异常按 backoff 指数退避重试
    至多 retries 次；挑战页按 challenge_backoff 序列退避重试，耗尽抛
    ChallengeError。两条重试轴独立计数。
    """

    def __init__(self, fetch_fn: Callable[[str], str], *,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 rate: float = 0.4,
                 challenge_backoff: Sequence[float] = (30.0, 60.0, 120.0),
                 retries: int = 3, backoff: float = 2.0):
        self._fetch = fetch_fn
        self._sleep = sleep_fn
        self._rate = rate
        self._challenge_backoff = list(challenge_backoff)
        self._retries = retries
        self._backoff = backoff

    def get(self, url: str) -> str:
        challenges = 0
        errors = 0
        while True:
            self._sleep(self._rate)
            try:
                text = self._fetch(url)
            except http.PermanentError:
                raise
            except Exception:
                errors += 1
                if errors > self._retries:
                    raise
                self._sleep(self._backoff * 2 ** (errors - 1))
                continue
            if is_challenge(text):
                challenges += 1
                if challenges > len(self._challenge_backoff):
                    raise ChallengeError(url)
                self._sleep(self._challenge_backoff[challenges - 1])
                continue
            return text


# ---------- helpdocs 阶段编排 ----------

def _load_state(out_raw: Path) -> dict[str, Any]:
    """断点优先（运行中断恢复），其次已有产物（failed 重试）。"""
    cp = out_raw / CHECKPOINT
    if cp.exists():
        with open(cp, encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))
    art = out_raw / ARTIFACT
    if art.exists():
        with open(art, encoding="utf-8") as f:
            data = cast(dict[str, Any], json.load(f))
        return {"done": data.get("records") or {}, "failed": data.get("failed") or []}
    return {"done": {}, "failed": []}


def crawl_docs(sitemap_xml: str, fetch_fn: Callable[[str], str], *,
               out_raw: Path,
               docsets: Sequence[str] | None = None,
               docset_prefix: str = "api-",
               limit: int | None = None,
               sleep_fn: Callable[[float], None] = time.sleep,
               rate: float = 0.4,
               challenge_backoff: Sequence[float] = (30.0, 60.0, 120.0),
               retries: int = 3, backoff: float = 2.0,
               checkpoint_every: int = 50) -> dict[str, Any]:
    """种子探测 + 同文档集 BFS 链接发现 → 抓取解析全部文档页（断点续传）。

    实测：sitemap 大量陈旧死链（404），且当前形态的 API 页往往不在 sitemap
    里——故 sitemap 仅作种子，页面经「同 docset 绝对链接」BFS 发现补齐。
    - 种子探测：PermanentError（404/410）记 dead_seeds 快速跳过；
    - BFS：done 页面用其记录的 links（重跑不重取）；
    - limit 截断抓取尝试总数（试点/冒烟）；
    - failed 台账按 url 去重，重试成功即清除。
    """
    groups = parse_sitemap(sitemap_xml)
    selected = (list(docsets) if docsets
                else sorted(ds for ds in groups if ds.startswith(docset_prefix)))
    state = _load_state(out_raw)
    done: dict[str, dict[str, Any]] = state["done"]
    failed: list[dict[str, str]] = list(state["failed"])
    # stale 记录（缺 is_deprecated 的旧 schema）不视为已访问：种子/BFS 都会重取升级
    stale = {u for u, r in done.items() if "is_deprecated" not in r}
    if stale:
        logger.info("stale records to refetch: %d", len(stale))
    crawler = Crawler(fetch_fn, sleep_fn=sleep_fn, rate=rate,
                      challenge_backoff=challenge_backoff,
                      retries=retries, backoff=backoff)

    out_raw.mkdir(parents=True, exist_ok=True)
    cp = out_raw / CHECKPOINT

    def _save_cp() -> None:
        cp.write_text(json.dumps({"done": done, "failed": failed},
                                 ensure_ascii=False), encoding="utf-8")

    def _fail(url: str, err: str) -> None:
        if not any(f["url"] == url for f in failed):
            failed.append({"url": url, "error": err})

    def _record(url: str, ds: str, html: str) -> None:
        page = parse_api_page(html)
        intro = extract_func_intro(html)
        done[url] = {
            "url": url, "docset": ds,
            "api_name": page.api_name, "cn_name": page.cn_name,
            "is_api_ref": page.is_api_ref,
            "is_deprecated": page.is_deprecated,
            "product_display": page.product_display,
            "func_intro": intro,
            "replacement": extract_replacement(intro),
            "links": extract_links(html, ds),
        }
        if any(f["url"] == url for f in failed):
            failed[:] = [f for f in failed if f["url"] != url]  # 重试成功清除台账

    total_urls = sum(len(groups.get(ds) or []) for ds in selected)
    fetched = 0
    dead_seeds = 0

    def _budget() -> bool:
        return limit is None or fetched < limit

    for ds in selected:
        queue: list[str] = []
        for url in groups.get(ds) or []:
            # done 记录须含 is_deprecated（S14 起的 schema）；缺字段视为 stale 重取
            if url in done and url not in stale:
                queue.append(url)  # 已有记录作 BFS 种子，不重取
                continue
            if not _budget():
                break
            try:
                html = crawler.get(url)
            except http.PermanentError:
                dead_seeds += 1
                continue
            except ChallengeError:
                _fail(url, "challenge")
                _save_cp()
                logger.warning("challenge exhausted: %s", url)
                continue
            except Exception as e:
                _fail(url, str(e))
                _save_cp()
                logger.warning("FAIL %s: %s", url, e)
                continue
            fetched += 1
            _record(url, ds, html)
            queue.append(url)
            if fetched % checkpoint_every == 0:
                _save_cp()
                logger.info("checkpoint: fetched=%d", fetched)
        seen = set(queue) | (set(done) - stale) | {f["url"] for f in failed}
        qi = 0
        while qi < len(queue):
            url = queue[qi]
            qi += 1
            for link in (done.get(url) or {}).get("links") or []:
                if link in seen:
                    continue
                seen.add(link)
                if not _budget():
                    break
                try:
                    html = crawler.get(link)
                except http.PermanentError:
                    dead_seeds += 1
                    continue
                except ChallengeError:
                    _fail(link, "challenge")
                    _save_cp()
                    logger.warning("challenge exhausted: %s", link)
                    continue
                except Exception as e:
                    _fail(link, str(e))
                    _save_cp()
                    logger.warning("FAIL %s: %s", link, e)
                    continue
                fetched += 1
                _record(link, ds, html)
                queue.append(link)
                if fetched % checkpoint_every == 0:
                    _save_cp()
                    logger.info("checkpoint: fetched=%d", fetched)

    artifact = {
        "source": SITEMAP_URL,
        "total_urls": total_urls,
        "records": done,
        "failed": failed,
    }
    (out_raw / ARTIFACT).write_text(
        json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    if cp.exists():
        cp.unlink()
    summary = {"done": len(done), "failed": len(failed), "total_urls": total_urls,
               "fetched": fetched, "dead_seeds": dead_seeds}
    logger.info("crawl finished. %s", summary)
    return summary


def main() -> int:
    """api-refresh helpdocs 阶段入口。"""
    import argparse

    from common.logconf import configure_logging
    from common.paths import project_root

    p = argparse.ArgumentParser(prog="api-refresh helpdocs")
    p.add_argument("--docset", action="append", default=None,
                   help="文档集白名单（可重复；缺省取 api-* 前缀全部）")
    p.add_argument("--limit", type=int, default=None, help="截断总页数（试点）")
    p.add_argument("--rate", type=float, default=0.4, help="限速秒/页（默认 0.4）")
    p.add_argument("--retries", type=int, default=3, help="网络异常重试次数")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-file", default=None)
    args = p.parse_args()
    configure_logging(program="api-refresh-helpdocs",
                      level=args.log_level, log_file=args.log_file)
    raw_dir = Path(project_root()) / "raw"
    summary = crawl_docs(fetch_html(SITEMAP_URL), fetch_html, out_raw=raw_dir,
                         docsets=args.docset, limit=args.limit, rate=args.rate,
                         retries=args.retries)
    logger.info("summary: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
