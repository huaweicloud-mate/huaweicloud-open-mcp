"""HTTP 客户端单元测试（monkeypatch urllib，mock 系统边界）。"""

import urllib.error

from common.auth import Credentials
from mcp_openapi.signer.client import HttpClient

CRED = Credentials(ak="AK", sk="SK", project_id="pid")


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
    def __init__(self, code, body):
        super().__init__(url="http://fake", code=code, msg="err", hdrs={}, fp=None)
        self._body = body

    def read(self):
        return self._body


def _install(monkeypatch, responses):
    """responses: 按调用顺序弹出（response, error）之一。"""
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(req)
        if not responses:
            raise AssertionError("unexpected extra call")
        item = responses.pop(0)
        if isinstance(item, FakeHTTPError):
            raise item
        return item

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def get_header(req, name):
    """urllib Request.add_header 会 capitalize 键名，故大小写不敏感取值。"""
    target = name.lower()
    for k, v in req.headers.items():
        if k.lower() == target:
            return v
    raise KeyError(name)


def test_request_signs_headers(monkeypatch):
    calls = _install(monkeypatch, [FakeResponse(200, b'{"ok": true}')])
    client = HttpClient(credentials=CRED)
    resp = client.request("GET", "ecs.cn-north-4.myhuaweicloud.com",
                          "/v1/pid/cloudservers", query={"limit": 1})
    assert resp["status"] == 200
    assert resp["body"] == {"ok": True}
    req = calls[0]
    assert get_header(req, "X-Sdk-Date")
    assert get_header(req, "Authorization").startswith("SDK-HMAC-SHA256 Access=AK,")
    assert req.full_url == "https://ecs.cn-north-4.myhuaweicloud.com/v1/pid/cloudservers?limit=1"


def test_request_429_retry_then_ok(monkeypatch):
    calls = _install(monkeypatch, [
        FakeHTTPError(429, b"too many"),
        FakeHTTPError(429, b"too many"),
        FakeResponse(200, b"{}"),
    ])
    client = HttpClient(credentials=CRED, retry_backoff=0.001)
    resp = client.request("GET", "h.example.com", "/p", query=None)
    assert resp["status"] == 200
    assert len(calls) == 3


def test_request_429_exhausted(monkeypatch):
    _install(monkeypatch, [FakeHTTPError(429, b"x") for _ in range(5)])
    client = HttpClient(credentials=CRED, max_retries=4, retry_backoff=0.001)
    resp = client.request("GET", "h.example.com", "/p")
    assert resp["status"] == 429


def test_request_error_body_passthrough(monkeypatch):
    _install(monkeypatch, [FakeHTTPError(400, b'{"error_code":"E.400","error_msg":"bad"}')])
    client = HttpClient(credentials=CRED, retry_backoff=0.001)
    resp = client.request("POST", "h.example.com", "/p", body={"a": 1})
    assert resp["status"] == 400
    assert resp["body"] == {"error_code": "E.400", "error_msg": "bad"}


def test_request_security_token_header(monkeypatch):
    calls = _install(monkeypatch, [FakeResponse(200, b"{}")])
    cred = Credentials(ak="AK", sk="SK", security_token="ST")
    client = HttpClient(credentials=cred)
    client.request("GET", "h.example.com", "/p")
    assert get_header(calls[0], "X-Security-Token") == "ST"


def test_request_body_serialized(monkeypatch):
    calls = _install(monkeypatch, [FakeResponse(200, b"{}")])
    client = HttpClient(credentials=CRED)
    client.request("POST", "h.example.com", "/p", body={"name": "x"})
    req = calls[0]
    assert req.data == b'{"name": "x"}'
    assert get_header(req, "Content-Type") == "application/json"


def test_request_logs_never_contain_credentials(monkeypatch, caplog):
    import logging
    _install(monkeypatch, [FakeResponse(200, b"{}")])
    client = HttpClient(credentials=CRED)
    with caplog.at_level(logging.INFO, logger="mcp_openapi.signer.client"):
        client.request("GET", "h.example.com", "/p")
    text = caplog.text
    assert "Authorization" not in text
    assert "Access=" not in text
    assert CRED.sk not in text
    assert "X-Security-Token" not in text


def test_request_logs_status_and_path(monkeypatch, caplog):
    import logging
    _install(monkeypatch, [FakeResponse(200, b"{}")])
    client = HttpClient(credentials=CRED)
    with caplog.at_level(logging.INFO, logger="mcp_openapi.signer.client"):
        client.request("GET", "h.example.com", "/p", query={"limit": 1})
    assert "GET" in caplog.text
    assert "h.example.com/p" in caplog.text
    assert "200" in caplog.text


def test_mock_api_client_url(monkeypatch):
    from apie.mock import MockApiClient
    calls = _install(monkeypatch, [FakeResponse(200, b'{"servers": []}')])
    client = MockApiClient()
    resp = client.mock_request("ECS", "ListServersDetails", "cn-north-4", status_code=200, number=2)
    assert resp["status"] == 200
    assert resp["body"] == {"servers": []}
    url = calls[0].full_url
    assert url.startswith("https://apiexplorer.cn-north-4.myhuaweicloud.com/v1/mock/ECS/ListServersDetails?")
    assert "status_code=200" in url
    assert "number=2" in url
    assert "region_id=cn-north-4" in url
