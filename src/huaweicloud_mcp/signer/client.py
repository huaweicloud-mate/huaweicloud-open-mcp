"""HTTP 客户端：真实模式（签名直连）+ mock 模式（API Explorer mock 端点）。"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import sign

MOCK_BASE = "https://apiexplorer.cn-north-4.myhuaweicloud.com"
MOCK_PATH = "/v1/mock"
MAX_RETRIES = 4
RETRY_BACKOFF = 2.0


class ApiError(Exception):
    def __init__(self, status, error_code=None, error_msg=None, body=None):
        super().__init__(error_msg or f"HTTP {status}")
        self.status = status
        self.error_code = error_code
        self.error_msg = error_msg
        self.body = body


class HttpClient:
    def __init__(self, credentials=None, mock=False, mock_base=MOCK_BASE,
                 timeout=30, max_retries=MAX_RETRIES, retry_backoff=RETRY_BACKOFF):
        self.credentials = credentials
        self.mock = mock
        self.mock_base = mock_base
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def _open(self, url, method, headers, body_bytes):
        req = urllib.request.Request(url, data=body_bytes, method=method, headers=headers)
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2 ** attempt))
                    continue
                return e.code, dict(e.headers), e.read()
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2 ** attempt))
                    continue
                raise
        raise last_err

    def request(self, method, host, path, query=None, body=None, headers=None):
        """签名并发送请求。返回 {"status", "body"}（body 为解析后的 JSON 或原始文本）。"""
        headers = dict(headers or {})
        body_bytes = None
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

        scheme = "https"
        url = f"{scheme}://{host}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(self._flatten(query), doseq=True)

        status, resp_headers, raw = self._open(url, method, headers, body_bytes)
        return {"status": status, "headers": resp_headers, "body": self._parse_body(raw)}

    @staticmethod
    def _flatten(query):
        flat = {}
        for k, v in query.items():
            if v is None:
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            flat[k] = v
        return flat

    @staticmethod
    def _parse_body(raw):
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except Exception:
            return text

    def mock_request(self, product, api_name, region, status_code=200, number=1):
        """调用 API Explorer mock 端点（开放端点，无需凭证）。"""
        params = {
            "status_code": status_code,
            "number": number,
            "region_id": region,
        }
        url = f"{self.mock_base}{MOCK_PATH}/{product}/{api_name}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return {"status": resp.status, "headers": dict(resp.headers),
                            "body": self._parse_body(resp.read())}
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2 ** attempt))
                    continue
                return {"status": e.code, "headers": dict(e.headers), "body": self._parse_body(e.read())}
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2 ** attempt))
                    continue
                raise
        raise last_err
