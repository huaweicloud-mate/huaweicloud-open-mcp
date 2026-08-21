"""API Explorer mock 端点客户端（开放端点、无需凭证、无签名）。

实测行为：HTTP 状态恒为 200；status_code=200 返回与真实 API 同构的 mock 成功数据，
其它 status_code 返回空 body。
"""

import logging
import urllib.parse
import urllib.request
from typing import Any

from ..types import ClientResponse
from .http import _retry, parse_body

logger = logging.getLogger("openmcp.apie.mock")

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

        def _do() -> tuple[int, dict[str, str], Any]:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), parse_body(resp.read())

        try:
            status, headers, body = _retry(_do, max_retries=self.max_retries,
                                           backoff=self.retry_backoff,
                                           logger_name="openmcp.apie.mock")
            return {"status": status, "headers": headers, "body": body}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "headers": dict(e.headers), "body": parse_body(e.read())}
