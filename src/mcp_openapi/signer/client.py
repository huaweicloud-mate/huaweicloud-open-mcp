"""HTTP 客户端：真实模式（SDK-HMAC-SHA256 签名直连华为云）。"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from common.auth.credentials import Credentials
from common.http import _retry, parse_body
from common.types import ClientResponse

from . import sign

logger = logging.getLogger("mcp_openapi.signer.client")

MAX_RETRIES = 4
RETRY_BACKOFF = 2.0


class HttpClient:
    def __init__(self, credentials: Credentials | None = None, *,
                 timeout: int = 30, max_retries: int = MAX_RETRIES,
                 retry_backoff: float = RETRY_BACKOFF):
        self.credentials = credentials
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def _open(self, url: str, method: str, headers: dict[str, str],
               body_bytes: bytes | None) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body_bytes, method=method, headers=headers)

        def _do() -> tuple[int, dict[str, str], bytes]:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()

        try:
            return _retry(_do, max_retries=self.max_retries,
                          backoff=self.retry_backoff,
                          logger_name="mcp_openapi.signer.client")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def request(self, method: str, host: str, path: str, *,
                query: dict[str, Any] | None = None, body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> ClientResponse:
        """签名并发送请求。返回 {"status", "headers", "body"}（body 为解析后 JSON 或原始文本）。"""
        headers = dict(headers or {})
        body_bytes: bytes | None = None
        if body is not None:
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        if self.credentials and self.credentials.security_token:
            headers.setdefault("X-Security-Token", self.credentials.security_token)

        extra = sign.sign_request(
            method=method,
            host=host,
            path=path,
            query=query or {},
            headers=headers,
            body=body_bytes,
            ak=self.credentials.ak if self.credentials else "",
            sk=self.credentials.sk if self.credentials else "",
        )
        headers.update(extra)

        url = f"https://{host}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(self._flatten(query), doseq=True)

        t0 = time.monotonic()
        status, resp_headers, raw = self._open(url, method, headers, body_bytes)
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("%s https://%s%s -> %s (%.0fms)",
                    method.upper(), host, path, status, elapsed_ms)
        if body is not None:
            logger.debug("request body: %s", json.dumps(body, ensure_ascii=False)[:500])
        return {"status": status, "headers": resp_headers, "body": parse_body(raw)}

    @staticmethod
    def _flatten(query: dict[str, Any]) -> dict[str, Any]:
        flat: dict[str, Any] = {}
        for k, v in query.items():
            if v is None:
                continue
            flat[k] = str(v).lower() if isinstance(v, bool) else v
        return flat
