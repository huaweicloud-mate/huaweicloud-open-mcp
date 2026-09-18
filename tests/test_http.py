"""common.http.open_with_retry 单测（S4 扩展，CONTEXT.md D 接缝）：单一传输原语。

独立真值：monkeypatch urllib（mock 系统边界，沿 test_client.py 的
FakeResponse/FakeHTTPError idiom）；重试序列与 sleep 矩阵手写字面量。
"""

import urllib.error
import urllib.request

import pytest

from common.http import open_with_retry


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body=b""):
        super().__init__(url="http://fake", code=code, msg="err", hdrs={}, fp=None)
        self._body = body

    def read(self):
        return self._body


def _install(monkeypatch, responses):
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append((req, timeout))
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def test_success_returns_status_headers_bytes(monkeypatch):
    calls = _install(monkeypatch, [FakeResponse(200, b'{"ok": true}',
                                                {"X-Req": "r1"})])
    status, headers, raw = open_with_retry("http://fake/x")
    assert (status, raw) == (200, b'{"ok": true}')
    assert headers["X-Req"] == "r1"
    assert calls[0][1] == 30   # timeout 透传


def test_http_error_returned_not_raised(monkeypatch):
    """HTTP 错误不抛：返回 (code, headers, error body)——lane 据 status 分流。"""
    _install(monkeypatch, [FakeHTTPError(404, b"<Error/>")])
    status, headers, raw = open_with_retry("http://fake/x")
    assert status == 404 and raw == b"<Error/>"
    assert isinstance(headers, dict)


def test_429_retries_with_backoff_then_succeeds(monkeypatch):
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    sleeps: list[float] = []
    _install(monkeypatch, [FakeHTTPError(429), FakeResponse(200, b"ok")])
    status, _, raw = open_with_retry("http://fake/x", retries=3, backoff=2.0)
    assert (status, raw) == (200, b"ok")
    assert sleeps == [2.0]   # 一次 429 → 指数退避首档


def test_transient_exception_retries_then_raises(monkeypatch):
    monkeypatch.setattr("common.http.time.sleep", lambda s: None)
    _install(monkeypatch, [OSError("conn reset"), OSError("conn reset"),
                           OSError("conn reset")])
    with pytest.raises(OSError):
        open_with_retry("http://fake/x", retries=2, backoff=1.0)


def test_method_data_headers_pass_through(monkeypatch):
    calls = _install(monkeypatch, [FakeResponse(200, b"")])
    open_with_retry("http://fake/x", method="POST", headers={"X-A": "1"},
                    data=b"payload")
    req = calls[0][0]
    assert req.get_method() == "POST"
    assert req.data == b"payload"
    assert req.headers.get("X-a") == "1"   # urllib capitalize 键名


def test_default_method_preserves_urllib_semantics(monkeypatch):
    """method=None（缺省）：无 data → GET，有 data → urllib 自动 POST（mock 契约）。"""
    calls = _install(monkeypatch, [FakeResponse(200, b""), FakeResponse(200, b"")])
    open_with_retry("http://fake/get")
    open_with_retry("http://fake/post", data=b"x")
    assert calls[0][0].get_method() == "GET"
    assert calls[1][0].get_method() == "POST"
