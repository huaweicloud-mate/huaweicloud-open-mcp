"""apie 帮助中心爬取：Crawler 挑战退避/限速/瞬时重试 + http.open_text（monkeypatch urllib）。"""

import json
import urllib.error
import urllib.request

from apie.build_help_hints import build_completions
from apie.fetch_help_docs import ChallengeError, Crawler, crawl_docs
from apie.help_docs import is_challenge
from common import http
from tests.test_apie_help_docs import (
    API_PAGE_HTML,
    COMPLEX_PAGE_HTML,
)

CHALLENGE_HTML = (
    '<!doctype html><html lang="en"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>Security Verification</title>"
    '<style>body{margin:0;min-height:100vh}</style></html>'
)

NORMAL_HTML = "<html><head><title>重启云服务器 - NovaRebootServer</title></head><body>功能介绍</body></html>"


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code):
        super().__init__(url="http://fake", code=code, msg="err", hdrs={}, fp=None)


def _install_urlopen(monkeypatch, responses):
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, FakeHTTPError):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


# ---------- http.open_text ----------

def test_open_text_returns_body(monkeypatch):
    _install_urlopen(monkeypatch, [FakeResponse(b"<html>ok</html>")])
    text, err = http.open_text("https://support.huaweicloud.com/a.html")
    assert err is None
    assert text == "<html>ok</html>"


def test_open_text_http_error(monkeypatch):
    _install_urlopen(monkeypatch, [FakeHTTPError(404)])
    text, err = http.open_text("https://support.huaweicloud.com/missing.html")
    assert text == ""
    assert err is not None
    assert err.code == 404


def test_open_text_custom_headers_merged(monkeypatch):
    calls = _install_urlopen(monkeypatch, [FakeResponse(b"x")])
    http.open_text("https://support.huaweicloud.com/a.html",
                   headers={"User-Agent": "BrowserUA/1.0", "Accept": "text/html"})
    headers = {k.lower(): v for k, v in calls[0].headers.items()}
    assert headers["user-agent"] == "BrowserUA/1.0"
    assert headers["accept"] == "text/html"


# ---------- is_challenge ----------

def test_is_challenge_positive():
    assert is_challenge(CHALLENGE_HTML) is True


def test_is_challenge_negative():
    assert is_challenge(NORMAL_HTML) is False
    assert is_challenge("") is False


# ---------- Crawler ----------

def _crawler(responses, *, sleeps, **kw):
    """responses: 依次弹出（str 返回 / Exception 抛出）；sleeps: 记录 sleep 调用。"""
    calls = []

    def fetch_fn(url):
        calls.append(url)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    crawler = Crawler(fetch_fn, sleep_fn=sleeps.append, **kw)
    return crawler, calls


def test_crawler_returns_text_and_respects_rate():
    sleeps = []
    crawler, calls = _crawler(["good"], sleeps=sleeps, rate=0.5)
    assert crawler.get("https://x/1") == "good"
    assert calls == ["https://x/1"]
    assert sleeps == [0.5]


def test_crawler_challenge_backoff_then_success():
    sleeps = []
    crawler, calls = _crawler(
        [CHALLENGE_HTML, CHALLENGE_HTML, "good"],
        sleeps=sleeps, rate=0.4, challenge_backoff=(30.0, 60.0))
    assert crawler.get("https://x/1") == "good"
    assert len(calls) == 3
    # 每次请求前 sleep(rate)：退避序列间穿插 rate（backoff ≥ rate，限速恒满足）
    assert sleeps == [0.4, 30.0, 0.4, 60.0, 0.4]


def test_crawler_challenge_exhausted_raises():
    sleeps = []
    crawler, calls = _crawler(
        [CHALLENGE_HTML] * 4, sleeps=sleeps, rate=0.4,
        challenge_backoff=(30.0, 60.0, 120.0))
    try:
        crawler.get("https://x/1")
        raised = False
    except ChallengeError:
        raised = True
    assert raised
    assert len(calls) == 4
    assert sleeps == [0.4, 30.0, 0.4, 60.0, 0.4, 120.0, 0.4]


def test_crawler_transient_error_retries_then_success():
    sleeps = []
    crawler, calls = _crawler(
        [RuntimeError("boom"), RuntimeError("boom"), "ok"],
        sleeps=sleeps, rate=0.4, retries=3, backoff=2.0)
    assert crawler.get("https://x/1") == "ok"
    assert len(calls) == 3
    assert sleeps == [0.4, 2.0, 0.4, 4.0, 0.4]


def test_crawler_transient_error_exhausted_raises():
    sleeps = []
    crawler, calls = _crawler(
        [RuntimeError("boom")] * 3, sleeps=sleeps, rate=0.4,
        retries=2, backoff=2.0)
    try:
        crawler.get("https://x/1")
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert len(calls) == 3
    assert sleeps == [0.4, 2.0, 0.4, 4.0, 0.4]


# ---------- S13d：编排（断点续传 / 失败台账 / 产物回读） ----------


SITEMAP = """<?xml version="1.0"?>
<urlset>
<url><loc>https://support.huaweicloud.com/api-ecs/ecs_03_0302.html</loc></url>
<url><loc>https://support.huaweicloud.com/api-ecs/zh-cn_topic_0020805967.html</loc></url>
<url><loc>https://support.huaweicloud.com/api-ecs/zh-cn_topic_9999999999.html</loc></url>
<url><loc>https://support.huaweicloud.com/api-obs/obs_01_0001.html</loc></url>
</urlset>"""

API_URL = "https://support.huaweicloud.com/api-ecs/ecs_03_0302.html"
ROOT_URL = "https://support.huaweicloud.com/api-ecs/zh-cn_topic_0020805967.html"
DEAD_URL = "https://support.huaweicloud.com/api-ecs/zh-cn_topic_9999999999.html"
CHAPTER_URL = "https://support.huaweicloud.com/api-ecs/ecs_03_0300.html"
API2_URL = "https://support.huaweicloud.com/api-ecs/ecs_02_0101.html"
OBS_URL = "https://support.huaweicloud.com/api-obs/obs_01_0001.html"

ROOT_PAGE_HTML = (
    '<html><head><title>API参考_弹性云服务器 ECS-华为云</title></head>'
    f'<body><a href="{API_URL}">重启云服务器（废弃） - NovaRebootServer</a>'
    f'<a href="{CHAPTER_URL}">状态管理</a></body></html>')

CHAPTER2_HTML = (
    '<html><head><title>状态管理_API参考_弹性云服务器 ECS-华为云</title></head>'
    f'<body><a href="{API2_URL}">创建云服务器 - CreateServers</a></body></html>')


def _fetch_map(pages, **kw):
    calls = []

    def fetch_fn(url):
        calls.append(url)
        item = pages[url]
        if isinstance(item, Exception):
            raise item
        return item

    return fetch_fn, calls


def test_crawl_docs_bfs_discovers_linked_pages(tmp_path):
    pages = {
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        ROOT_URL: ROOT_PAGE_HTML,
        API_URL: API_PAGE_HTML,
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
        OBS_URL: COMPLEX_PAGE_HTML,
    }
    fetch_fn, calls = _fetch_map(pages)
    summary = crawl_docs(SITEMAP, fetch_fn, out_raw=tmp_path, sleep_fn=lambda s: None)
    assert summary["done"] == 5
    assert summary["dead_seeds"] == 1
    assert summary["failed"] == 0
    assert calls == [API_URL, ROOT_URL, DEAD_URL, CHAPTER_URL, API2_URL, OBS_URL]
    art = json.loads((tmp_path / "help_docs.json").read_text(encoding="utf-8"))
    assert not (tmp_path / "help_docs_partial.json").exists()
    # sitemap 未列出的 API2 经章节页链接发现
    assert art["records"][API2_URL]["api_name"] == "CreateServers"
    rec = art["records"][ROOT_URL]
    assert rec["links"] == [API_URL, CHAPTER_URL]
    rec = art["records"][API_URL]
    assert rec["api_name"] == "NovaRebootServer"
    assert rec["func_intro"].startswith("重启单台云服务器。")


def test_crawl_docs_resume_skips_done_and_uses_recorded_links(tmp_path):
    partial = tmp_path / "help_docs_partial.json"
    partial.write_text(json.dumps({
        "done": {API_URL: {"url": API_URL, "api_name": "NovaRebootServer",
                           "is_deprecated": True, "replacement": "BatchRebootServers",
                           "links": []}},
        "failed": [],
    }, ensure_ascii=False), encoding="utf-8")
    pages = {
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        ROOT_URL: ROOT_PAGE_HTML,
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
        OBS_URL: COMPLEX_PAGE_HTML,
    }
    fetch_fn, calls = _fetch_map(pages)
    summary = crawl_docs(SITEMAP, fetch_fn, out_raw=tmp_path, sleep_fn=lambda s: None)
    assert summary["done"] == 5
    assert calls == [ROOT_URL, DEAD_URL, CHAPTER_URL, API2_URL, OBS_URL]
    art = json.loads((tmp_path / "help_docs.json").read_text(encoding="utf-8"))
    assert art["records"][API_URL]["api_name"] == "NovaRebootServer"


def test_crawl_docs_failed_ledger_dedup(tmp_path):
    fetch_fn, _ = _fetch_map({
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        API_URL: RuntimeError("boom"),
        ROOT_URL: ROOT_PAGE_HTML,
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
        OBS_URL: COMPLEX_PAGE_HTML,
    })
    summary = crawl_docs(SITEMAP, fetch_fn, out_raw=tmp_path, sleep_fn=lambda s: None)
    assert summary["done"] == 4
    assert summary["failed"] == 1
    art = json.loads((tmp_path / "help_docs.json").read_text(encoding="utf-8"))
    assert art["failed"] == [{"url": API_URL, "error": "boom"}]


def test_crawl_docs_docset_filter_and_limit(tmp_path):
    fetch_fn, calls = _fetch_map({
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        ROOT_URL: ROOT_PAGE_HTML,
        API_URL: API_PAGE_HTML,
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
    })
    summary = crawl_docs(SITEMAP, fetch_fn, out_raw=tmp_path, sleep_fn=lambda s: None,
                         docsets=["api-ecs"], limit=2)
    assert calls == [API_URL, ROOT_URL]  # 预算按抓取尝试计，耗尽即止
    assert summary["fetched"] == 2
    assert summary["dead_seeds"] == 0


def test_crawl_docs_retry_pass_retries_failed_from_artifact(tmp_path):
    pages = {
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        API_URL: RuntimeError("boom"),
        ROOT_URL: ROOT_PAGE_HTML,
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
        OBS_URL: COMPLEX_PAGE_HTML,
    }
    fetch1, _ = _fetch_map(pages)
    crawl_docs(SITEMAP, fetch1, out_raw=tmp_path, sleep_fn=lambda s: None)
    art = json.loads((tmp_path / "help_docs.json").read_text(encoding="utf-8"))
    assert art["failed"] == [{"url": API_URL, "error": "boom"}]

    fetch2, calls2 = _fetch_map({API_URL: API_PAGE_HTML,
                                 DEAD_URL: http.PermanentError("HTTP Error 404")})
    summary = crawl_docs(SITEMAP, fetch2, out_raw=tmp_path, sleep_fn=lambda s: None)
    assert calls2 == [API_URL, DEAD_URL]
    assert summary == {"done": 5, "failed": 0, "total_urls": 4,
                       "fetched": 1, "dead_seeds": 1}
    art = json.loads((tmp_path / "help_docs.json").read_text(encoding="utf-8"))
    assert art["failed"] == []


# ---------- build_completions（helphints 阶段） ----------

def _prep_raw(raw, detail_desc="重启单台云服务器。"):
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "help_docs.json").write_text(json.dumps({
        "records": {
            "https://support.huaweicloud.com/api-ecs/ecs_03_0302.html": {
                "url": "https://support.huaweicloud.com/api-ecs/ecs_03_0302.html",
                "docset": "api-ecs", "api_name": "NovaRebootServer",
                "cn_name": "重启云服务器（废弃）", "is_api_ref": True,
                "product_display": "弹性云服务器 ECS",
                "func_intro": "重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。",
                "is_deprecated": True,
                "replacement": "BatchRebootServers",
            },
        },
        "failed": [],
    }, ensure_ascii=False), encoding="utf-8")
    (raw / "apis_docs.json").write_text(json.dumps({
        "apis": [{"product_short": "ECS", "name": "NovaRebootServer",
                  "summary": "重启云服务器"}],
    }, ensure_ascii=False), encoding="utf-8")
    (raw / "huawei_products.json").write_text(json.dumps({
        "groups": [{"name": "计算", "products": [
            {"productshort": "ECS", "name": "弹性云服务器"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    (raw / "apis_detail.json").write_text(json.dumps({
        "apis": {"ECS::NovaRebootServer": {"paths": {"/a": {"post": {
            "operationId": "NovaRebootServer", "description": detail_desc}}}}},
    }, ensure_ascii=False), encoding="utf-8")


def test_build_completions_writes_all_artifacts(tmp_path):
    raw, data = tmp_path / "raw", tmp_path / "data"
    _prep_raw(raw)
    report = build_completions(raw_dir=raw, data_dir=data,
                               detail_path=raw / "apis_detail.json")
    comp = json.loads(
        (data / "help_completions" / "help_completions.json").read_text(encoding="utf-8"))
    assert len(comp) == 1
    assert comp[0]["product"] == "ECS"
    assert comp[0]["api"] == "NovaRebootServer"
    hints = json.loads(
        (data / "hints" / "help-docs-hints.json").read_text(encoding="utf-8"))
    assert hints["api_notes_in_list_apis"] is False
    assert hints["products"]["ECS"]["apis"]["novarebootserver"].endswith(
        "官方帮助文档: https://support.huaweicloud.com/api-ecs/ecs_03_0302.html")
    rep = json.loads((data / "help_completions" / "report.json").read_text(encoding="utf-8"))
    assert rep["matched"] == 1
    assert rep["completions"] == 1
    assert report["completions"] == 1


def test_build_completions_product_filter(tmp_path):
    raw, data = tmp_path / "raw", tmp_path / "data"
    _prep_raw(raw)
    report = build_completions(raw_dir=raw, data_dir=data,
                               detail_path=raw / "apis_detail.json",
                               products=["OBS"])
    assert report["completions"] == 0
    hints = json.loads(
        (data / "hints" / "help-docs-hints.json").read_text(encoding="utf-8"))
    assert hints["products"] == {}


def test_build_completions_min_gain_threshold(tmp_path):
    raw, data = tmp_path / "raw", tmp_path / "data"
    _prep_raw(raw, detail_desc="重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。")
    report = build_completions(raw_dir=raw, data_dir=data,
                               detail_path=raw / "apis_detail.json")
    assert report["completions"] == 0



# ---------- 永久错误快速失败（404 等，不重试） ----------

def test_crawler_permanent_error_no_retry():
    sleeps = []
    crawler, calls = _crawler([http.PermanentError("404")], sleeps=sleeps,
                              rate=0.4, retries=3, backoff=2.0)
    try:
        crawler.get("https://x/1")
        raised = False
    except http.PermanentError:
        raised = True
    assert raised
    assert calls == ["https://x/1"]  # 不重试
    assert sleeps == [0.4]


def test_fetch_html_wraps_404_as_permanent(monkeypatch):
    _install_urlopen(monkeypatch, [FakeHTTPError(404)])
    from apie.fetch_help_docs import fetch_html
    try:
        fetch_html("https://support.huaweicloud.com/api-ecs/dead.html")
        raised = False
    except http.PermanentError:
        raised = True
    assert raised


def test_build_completions_writes_deprecated_index(tmp_path):
    raw, data = tmp_path / "raw", tmp_path / "data"
    _prep_raw(raw)
    build_completions(raw_dir=raw, data_dir=data,
                      detail_path=raw / "apis_detail.json")
    dep = json.loads(
        (data / "help_completions" / "deprecated.json").read_text(encoding="utf-8"))
    assert dep == {"products": {"ECS": {"novarebootserver": {
        "replacement": "BatchRebootServers",
        "doc_url": "https://support.huaweicloud.com/api-ecs/ecs_03_0302.html"}}}}


def test_build_completions_deprecated_decoupled_from_diff(tmp_path):
    """无差集补全的废弃接口仍进索引（废弃索引与差集口径解耦）。"""
    raw, data = tmp_path / "raw", tmp_path / "data"
    _prep_raw(raw, detail_desc="重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。")
    report = build_completions(raw_dir=raw, data_dir=data,
                               detail_path=raw / "apis_detail.json")
    assert report["completions"] == 0
    dep = json.loads(
        (data / "help_completions" / "deprecated.json").read_text(encoding="utf-8"))
    assert dep["products"]["ECS"]["novarebootserver"]["replacement"] == "BatchRebootServers"


def test_build_completions_deprecated_honors_product_filter(tmp_path):
    raw, data = tmp_path / "raw", tmp_path / "data"
    _prep_raw(raw)
    build_completions(raw_dir=raw, data_dir=data,
                      detail_path=raw / "apis_detail.json", products=["OBS"])
    dep = json.loads(
        (data / "help_completions" / "deprecated.json").read_text(encoding="utf-8"))
    assert dep == {"products": {}}


def test_crawl_docs_stale_record_without_deprecated_field_refetched(tmp_path):
    """done 记录缺 is_deprecated（旧 schema）视为未完成，重取。"""
    partial = tmp_path / "help_docs_partial.json"
    partial.write_text(json.dumps({
        "done": {API_URL: {"url": API_URL, "api_name": "NovaRebootServer",
                           "links": []}},  # 旧格式：无 is_deprecated
        "failed": [],
    }, ensure_ascii=False), encoding="utf-8")
    pages = {
        API_URL: API_PAGE_HTML,
        ROOT_URL: ROOT_PAGE_HTML,
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
        OBS_URL: COMPLEX_PAGE_HTML,
    }
    fetch_fn, calls = _fetch_map(pages)
    summary = crawl_docs(SITEMAP, fetch_fn, out_raw=tmp_path, sleep_fn=lambda s: None)
    assert API_URL in calls  # stale 记录被重取
    assert summary["done"] == 5
    art = json.loads((tmp_path / "help_docs.json").read_text(encoding="utf-8"))
    rec = art["records"][API_URL]
    assert rec["is_deprecated"] is True
    assert rec["replacement"] == "BatchRebootServers"


def test_crawl_docs_fresh_record_with_deprecated_field_not_refetched(tmp_path):
    partial = tmp_path / "help_docs_partial.json"
    partial.write_text(json.dumps({
        "done": {API_URL: {"url": API_URL, "api_name": "NovaRebootServer",
                           "is_deprecated": True, "replacement": "BatchRebootServers",
                           "links": []}},
        "failed": [],
    }, ensure_ascii=False), encoding="utf-8")
    fetch_fn, calls = _fetch_map({
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        ROOT_URL: ROOT_PAGE_HTML,
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
        OBS_URL: COMPLEX_PAGE_HTML,
    })
    crawl_docs(SITEMAP, fetch_fn, out_raw=tmp_path, sleep_fn=lambda s: None)
    assert API_URL not in calls  # 新 schema 记录跳过


def test_crawl_docs_stale_records_refetched_via_bfs_links(tmp_path):
    """stale 记录不能阻塞 BFS：链接指向的 stale 页也要重取升级。"""
    partial = tmp_path / "help_docs_partial.json"
    partial.write_text(json.dumps({
        "done": {
            API_URL: {"url": API_URL, "api_name": "NovaRebootServer", "links": []},
            CHAPTER_URL: {"url": CHAPTER_URL, "cn_name": "状态管理", "links": []},
        },  # 两条均为旧 schema（缺 is_deprecated）
        "failed": [],
    }, ensure_ascii=False), encoding="utf-8")
    fetch_fn, calls = _fetch_map({
        API_URL: API_PAGE_HTML,
        ROOT_URL: ROOT_PAGE_HTML,
        DEAD_URL: http.PermanentError("HTTP Error 404"),
        CHAPTER_URL: CHAPTER2_HTML,
        API2_URL: COMPLEX_PAGE_HTML,
        OBS_URL: COMPLEX_PAGE_HTML,
    })
    summary = crawl_docs(SITEMAP, fetch_fn, out_raw=tmp_path, sleep_fn=lambda s: None)
    assert summary["done"] == 5
    assert set(calls) == {API_URL, ROOT_URL, DEAD_URL, CHAPTER_URL, API2_URL, OBS_URL}
    art = json.loads((tmp_path / "help_docs.json").read_text(encoding="utf-8"))
    assert art["records"][CHAPTER_URL]["is_deprecated"] is False  # 章节页经 BFS 重取升级
