"""API Explorer mock 端点客户端（开放端点、无需凭证、无签名）。

实测行为：HTTP 状态恒为 200；status_code=200 返回与真实 API 同构的 mock 成功数据，
其它 status_code 返回空 body。

passthrough（opt-in）：mock_request(params=...) 把 execute 业务参数转发到端点——
扁平标量进 query、params["body"] 进 POST JSON body（无 body 保持 GET）。
标量编码对齐 real 模式 HttpClient：bool → "true"/"false"，其余 str()；
扁平 dict/list → JSON 串。默认不转发，保持与 API Explorer 契约一致。
"""

import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from common.http import _retry, parse_body
from common.types import ClientResponse

logger = logging.getLogger("apie.mock")

MOCK_BASE = "https://apiexplorer.cn-north-4.myhuaweicloud.com"
MOCK_PATH = "/v1/mock"


def split_passthrough_params(params: Mapping[str, Any] | None
                             ) -> tuple[list[tuple[str, str]], dict[str, Any] | None]:
    """passthrough 编码：(query 对列表, JSON body 或 None)。

    - `_` 前缀控制键（_status_code/_number/_presign* 等）剥离；
    - params["body"] 为 mapping 时作为 JSON body，不进 query；
    - 标量编码对齐 real 模式 HttpClient（bool → str(v).lower()，其余 str()）；
      扁平 dict/list → JSON 串。
    """
    if not params:
        return [], None
    query: list[tuple[str, str]] = []
    body: dict[str, Any] | None = None
    for key, value in params.items():
        if key.startswith("_"):
            continue
        if key == "body" and isinstance(value, Mapping):
            body = dict(value)
            continue
        if isinstance(value, str):
            query.append((key, value))
        elif isinstance(value, bool):
            query.append((key, "true" if value else "false"))
        elif isinstance(value, (dict, list)):
            query.append((key, json.dumps(value, ensure_ascii=False)))
        else:
            query.append((key, str(value)))
    return query, body


class MockApiClient:
    def __init__(self, base_url: str = MOCK_BASE, *, timeout: int = 30,
                 max_retries: int = 4, retry_backoff: float = 2.0):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def mock_request(self, product: str, api_name: str, region: str,
                     status_code: int = 200, number: int = 1,
                     params: Mapping[str, Any] | None = None) -> ClientResponse:
        passthrough_query, passthrough_body = split_passthrough_params(params)
        query: list[tuple[str, str]] = [("status_code", str(status_code)),
                                        ("number", str(number)),
                                        ("region_id", region),
                                        *passthrough_query]
        url = f"{self.base_url}{MOCK_PATH}/{product}/{api_name}?{urllib.parse.urlencode(query)}"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        data: bytes | None = None
        if passthrough_body is not None:
            data = json.dumps(passthrough_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)

        def _do() -> tuple[int, dict[str, str], Any]:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), parse_body(resp.read())

        try:
            status, headers, body = _retry(_do, max_retries=self.max_retries,
                                           backoff=self.retry_backoff,
                                           logger_name="apie.mock")
            return {"status": status, "headers": headers, "body": body}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "headers": dict(e.headers), "body": parse_body(e.read())}
