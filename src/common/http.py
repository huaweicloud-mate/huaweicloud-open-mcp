"""APIE 抓取公共 HTTP 助手：统一重试、超时与 429 退避。"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, TypeVar, cast

logger = logging.getLogger("common.http")

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

HttpError = urllib.error.HTTPError

T = TypeVar("T")


class ApieHttpError(Exception):
    """APIE 请求最终失败（重试耗尽后的非 HTTP 错误）。"""


class PermanentError(Exception):
    """确定性 HTTP 错误（404/410 等）：重试无意义，调用方应快速失败。"""


def _retry(fn: Callable[[], T], *, max_retries: int, backoff: float,
           logger_name: str = "common.http") -> T:
    """通用 HTTP 重试：429 指数退避，其他网络异常线性退避。"""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 and attempt < max_retries:
                sleep_s = backoff * (2 ** attempt)
                logging.getLogger(logger_name).warning(
                    "429 rate limited, retry %d/%d after %.1fs",
                    attempt + 1, max_retries, sleep_s)
                time.sleep(sleep_s)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                sleep_s = backoff * (2 ** attempt)
                logging.getLogger(logger_name).warning(
                    "http error: %s, retry %d/%d after %.1fs",
                    e, attempt + 1, max_retries, sleep_s)
                time.sleep(sleep_s)
                continue
            raise
    raise last_err  # type: ignore[misc]


def open_url(url: str, *, timeout: int = 30) -> tuple[dict[str, Any], HttpError | None]:
    """打开 URL 一次。返回 (解析后 JSON, HTTPError|None)；网络异常直接抛出。"""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return cast(dict[str, Any], json.loads(resp.read().decode("utf-8"))), None
    except urllib.error.HTTPError as e:
        try:
            body: dict[str, Any] = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"error_msg": str(e)}
        return body, e


def open_text(url: str, *, timeout: int = 30,
              headers: dict[str, str] | None = None) -> tuple[str, HttpError | None]:
    """打开 URL 一次，返回原始响应文本（HTML 抓取用）。HTTP 错误返回 ("", err)。"""
    merged = dict(HEADERS)
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        return "", e


def fetch_json(url: str, *, retries: int = 5, backoff: float = 2.0,
               timeout: int = 30) -> dict[str, Any]:
    """网络异常重试的 JSON 拉取。重试耗尽后抛 ApieHttpError。"""

    def _do() -> dict[str, Any]:
        return open_url(url, timeout=timeout)[0]

    try:
        return _retry(_do, max_retries=retries - 1, backoff=backoff)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        raise ApieHttpError(f"fetch failed after {retries} retries: {e}") from e


def fetch_json_retry(url: str, *, retries: int = 6, backoff: float = 2.0,
                     timeout: int = 30) -> tuple[dict[str, Any], HttpError | None]:
    """网络异常重试；HTTP 错误（含 429）原样返回。返回 (body, err)。"""

    def _do() -> tuple[dict[str, Any], HttpError | None]:
        return open_url(url, timeout=timeout)

    try:
        return _retry(_do, max_retries=retries - 1, backoff=backoff)
    except urllib.error.HTTPError as e:
        return {}, e
    except Exception as e:
        raise ApieHttpError(f"fetch failed after {retries} retries: {e}") from e


def fetch_json_429(url: str, *, retries: int = 10, backoff_429: float = 20.0,
                   backoff: float = 5.0, timeout: int = 30) -> tuple[dict[str, Any], HttpError | None]:
    """429 大退避重试；网络异常常规退避；其它 HTTP 错误原样返回。返回 (body, err)。"""
    for attempt in range(retries):
        try:
            body, err = open_url(url, timeout=timeout)
        except Exception as e:
            if attempt < retries - 1:
                logger.debug("fetch retry %d/%d after error: %s", attempt + 1, retries - 1, e)
                time.sleep(backoff)
                continue
            raise ApieHttpError(f"fetch failed after {retries} retries: {e}") from e
        if err is None:
            return body, None
        if err.code != 429:
            return body, err
        if attempt < retries - 1:
            logger.warning("429 rate limited, sleep %.0fs (attempt %d/%d)",
                           backoff_429, attempt + 1, retries - 1)
            time.sleep(backoff_429)
            continue
        return body, err
    raise ApieHttpError(f"429 rate-limited after {retries} retries: {url}")


def post_json(url: str, *, body: Any, headers: dict[str, str] | None = None,
              timeout: int = 120) -> Any:
    """单次 POST JSON 并解析响应（OpenAI-compatible API 调用用）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json", "Accept": "application/json"}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=merged, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json_retry(url: str, *, body: Any, headers: dict[str, str] | None = None,
                    retries: int = 4, backoff: float = 4.0,
                    timeout: int = 120) -> Any:
    """带重试的 POST JSON：429 指数退避，其它瞬时异常常规退避。"""

    def _do() -> Any:
        return post_json(url, body=body, headers=headers, timeout=timeout)

    try:
        return _retry(_do, max_retries=retries - 1, backoff=backoff)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        raise ApieHttpError(f"post failed after {retries} retries: {e}") from e


def query_url(base: str, params: dict[str, Any]) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def parse_body(raw: bytes) -> Any:
    """解析 HTTP 响应体为 JSON 或原始文本。"""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except Exception:
        return text
