"""API Explorer mock 端点客户端（开放端点、无需凭证、无签名）。

实测行为：HTTP 状态恒为 200；status_code=200 返回与真实 API 同构的 mock 成功数据，
其它 status_code 返回空 body。
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..types import ClientResponse

logger = logging.getLogger("huaweicloud_mcp.apie.mock")

MOCK_BASE = "https://apiexplorer.cn-north-4.myhuaweicloud.com"
MOCK_PATH = "/v1/mock"


class MockApiClient:
    def __init__(self, base_url: str = MOCK_BASE, *, timeout: int = 30,
                 max_retries: int = 4, retry_backoff: float = 2.0):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def mock_request(self, product: str, api_name: str, region: str,
                     status_code: int = 200, number: int = 1) -> ClientResponse:
        params = {"status_code": status_code, "number": number, "region_id": region}
        url = (f"{self.base_url}{MOCK_PATH}/{product}/{api_name}"
               f"?{urllib.parse.urlencode(params)}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Accept": "application/json"})
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return {"status": resp.status,
                            "headers": dict(resp.headers),
                            "body": self._parse_body(resp.read())}
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 and attempt < self.max_retries:
                    sleep_s = self.retry_backoff * (2 ** attempt)
                    logger.warning("mock 429 rate limited, retry %d/%d after %.1fs",
                                   attempt + 1, self.max_retries, sleep_s)
                    time.sleep(sleep_s)
                    continue
                return {"status": e.code, "headers": dict(e.headers), "body": self._parse_body(e.read())}
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2 ** attempt))
                    continue
                raise
        if last_err is None:
            raise RuntimeError("mock request failed without exception")
        raise last_err

    @staticmethod
    def _parse_body(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except Exception:
            return text
