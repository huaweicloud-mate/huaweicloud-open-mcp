"""APIE 抓取公共 HTTP 助手：统一重试、超时与 429 退避。"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

HttpError = urllib.error.HTTPError


class ApieHttpError(Exception):
    """APIE 请求最终失败（重试耗尽后的非 HTTP 错误）。"""


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


def fetch_json(url: str, *, retries: int = 5, backoff: float = 2.0, timeout: int = 30) -> dict[str, Any]:
    """网络异常重试的 JSON 拉取（HTTP 错误直接透出）。重试耗尽后抛 ApieHttpError。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return open_url(url, timeout=timeout)[0]
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise ApieHttpError(f"fetch failed after {retries} retries: {last_err}") from last_err


def fetch_json_retry(url: str, *, retries: int = 6, backoff: float = 2.0,
                   timeout: int = 30) -> tuple[dict[str, Any], HttpError | None]:
    """网络异常重试；HTTP 错误（含 429）原样返回。返回 (body, err)。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return open_url(url, timeout=timeout)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise ApieHttpError(f"fetch failed after {retries} retries: {last_err}") from last_err


def fetch_json_429(url: str, *, retries: int = 10, backoff_429: float = 20.0,
                   backoff: float = 5.0, timeout: int = 30) -> tuple[dict[str, Any], HttpError | None]:
    """429 大退避重试；网络异常常规退避；其它 HTTP 错误原样返回。返回 (body, err)。"""
    for attempt in range(retries):
        try:
            body, err = open_url(url, timeout=timeout)
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(backoff)
                continue
            raise ApieHttpError(f"fetch failed after {retries} retries: {e}") from e
        if err is None:
            return body, None
        if err.code != 429:
            return body, err
        if attempt < retries - 1:
            time.sleep(backoff_429)
            continue
        return body, err
    raise ApieHttpError(f"429 rate-limited after {retries} retries: {url}")


def query_url(base: str, params: dict[str, Any]) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"
