"""common.http.open_with_retry 单测（S4 扩展，CONTEXT.md D 接缝）：单一传输原语。

独立真值：monkeypatch urllib（mock 系统边界，沿 test_client.py 的
FakeResponse/FakeHTTPError idiom）；重试序列与 sleep 矩阵手写字面量。
"""

import json
import urllib.error
import urllib.request

import pytest

from common.http import ApieHttpError, fetch_json_429_raise, open_with_retry


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


# ---------- fetch_json_429_raise（dict-only 抛错契约，fetch_json_429 薄适配） ----------


def test_raise_success_returns_body(monkeypatch):
    _install(monkeypatch, [FakeResponse(200, b'{"apis": []}')])
    assert fetch_json_429_raise("http://fake/x") == {"apis": []}


def test_raise_429_once_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    _install(monkeypatch, [FakeHTTPError(429), FakeResponse(200, b'{"ok": 1}')])
    out = fetch_json_429_raise("http://fake/x", retries=2, backoff_429=20.0)
    assert out == {"ok": 1}
    assert sleeps == [20.0]   # 底座 429 退避节奏透传（transposition 守卫：错传 backoff 则为 [5.0]）


def test_raise_429_exhausted_reraises(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    calls = _install(monkeypatch, [FakeHTTPError(429), FakeHTTPError(429)])
    with pytest.raises(urllib.error.HTTPError) as ei:
        fetch_json_429_raise("http://fake/x", retries=2, backoff_429=1.0)
    assert ei.value.code == 429
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_raise_network_error_retries_then_raises_apie(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    calls = _install(monkeypatch, [OSError("reset"), OSError("reset")])
    with pytest.raises(ApieHttpError):
        fetch_json_429_raise("http://fake/x", retries=2, backoff=1.0)
    assert len(calls) == 2
    assert sleeps == [1.0]   # 底座线性退避（非 _retry 指数）


def test_raise_non_429_http_error_reraises_immediately(monkeypatch):
    calls = _install(monkeypatch, [FakeHTTPError(504)])
    with pytest.raises(urllib.error.HTTPError) as ei:
        fetch_json_429_raise("http://fake/x", retries=5)
    assert ei.value.code == 504
    assert len(calls) == 1   # 终态错不重试


def test_raise_non_json_200_treated_as_transient(monkeypatch):
    """200 + 非 JSON body（挑战页形态）：open_url json.loads 抛 → 底座按瞬时错
    退避重试耗尽 → ApieHttpError（cause 链保留）。继承底座行为，钉住防漂移。"""
    sleeps: list[float] = []
    monkeypatch.setattr("common.http.time.sleep", lambda s: sleeps.append(s))
    calls = _install(monkeypatch, [FakeResponse(200, b"<html>challenge</html>"),
                                  FakeResponse(200, b"<html>challenge</html>")])
    with pytest.raises(ApieHttpError) as ei:
        fetch_json_429_raise("http://fake/x", retries=2, backoff=1.0)
    assert len(calls) == 2
    assert sleeps == [1.0]
    assert isinstance(ei.value.__cause__, json.JSONDecodeError)


def test_raise_delegates_to_base_seal_point(monkeypatch):
    """封网点契约：monkeypatch 底座 fetch_json_429 即封住 adapter（零 urlopen、
    kwargs 逐字转发、err 通道转异常）。adapter 委托是本接缝的存在理由。"""
    forwarded = []

    def fake_base(url, *, retries, backoff_429, backoff, timeout):
        forwarded.append((url, retries, backoff_429, backoff, timeout))
        return {"sealed": True}, None

    monkeypatch.setattr("common.http.fetch_json_429", fake_base)
    calls = _install(monkeypatch, [])   # 任何 urlopen 都会失败
    out = fetch_json_429_raise("http://fake/x")
    assert out == {"sealed": True}
    assert calls == []
    assert forwarded == [("http://fake/x", 10, 20.0, 5.0, 30)]   # 缺省逐字透传

    err = FakeHTTPError(502)
    monkeypatch.setattr("common.http.fetch_json_429",
                        lambda url, **kw: ({}, err))
    with pytest.raises(urllib.error.HTTPError) as ei:
        fetch_json_429_raise("http://fake/x")
    assert ei.value is err
