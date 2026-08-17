"""华为云 SDK-HMAC-SHA256 签名实现。

算法与官方 SDK 保持一致（参考 huaweicloud-sdk-go-v3 core/auth/signer）：
规范请求 → StringToSign → HMAC-SHA256(SK, StringToSign)。
"""

import hashlib
import hmac
from datetime import datetime, timezone

ALGORITHM = "SDK-HMAC-SHA256"
DATE_FORMAT = "%Y%m%dT%H%M%SZ"
UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-~.")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def escape(s: str) -> str:
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if c in UNRESERVED:
            out.append(c)
        else:
            out.append(f"%{b:02X}")
    return "".join(out)


def canonical_uri(path: str) -> str:
    segments = path.split("/")
    uri = "/".join(escape(seg) for seg in segments)
    if not uri or not uri.endswith("/"):
        uri += "/"
    return uri


def _str_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def canonical_query_string(query) -> str:
    if not query:
        return ""
    collected = {}
    for key, value in query.items():
        if isinstance(value, (list, tuple)):
            vals = [_str_value(v) for v in value]
        else:
            vals = [_str_value(value)]
        collected.setdefault(key, []).extend(vals)
    parts = []
    for key in sorted(collected):
        for v in sorted(collected[key]):
            parts.append(f"{escape(key)}={escape(v)}")
    return "&".join(parts)


def signed_headers_list(headers) -> list:
    sh = []
    for key in headers:
        low = key.lower()
        if low.startswith("content-type") or "_" in key:
            continue
        if low not in sh:
            sh.append(low)
    sh.sort()
    return sh


def canonical_headers(headers, signed_headers, host) -> str:
    by_low = {}
    for key, value in headers.items():
        by_low.setdefault(key.lower(), []).append(str(value).strip())
    lines = []
    for key in signed_headers:
        if key == "host":
            values = [host]
        else:
            values = by_low.get(key, [])
        for v in sorted(values):
            lines.append(f"{key}:{v}")
    return "\n".join(lines) + ("\n" if lines else "")


def content_hash(headers, body) -> str:
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value
            break
    if content_type and "application/json" not in content_type and "application/bson" not in content_type:
        return "UNSIGNED-PAYLOAD"
    for key, value in headers.items():
        if key.lower() == "x-sdk-content-sha256":
            return value
    if body is None:
        return sha256_hex(b"")
    if isinstance(body, str):
        body = body.encode("utf-8")
    return sha256_hex(body)


def build_canonical_request(method, host, path, query, headers, body) -> str:
    method = method.upper()
    signed_headers = signed_headers_list(headers)
    payload_hash = content_hash(headers, body)
    return (
        f"{method}\n"
        f"{canonical_uri(path)}\n"
        f"{canonical_query_string(query or {})}\n"
        f"{canonical_headers(headers, signed_headers, host)}\n"
        f"{';'.join(signed_headers)}\n"
        f"{payload_hash}"
    )


def string_to_sign(canonical_request: str, request_date: str) -> str:
    return f"{ALGORITHM}\n{request_date}\n{sha256_hex(canonical_request.encode('utf-8'))}"


def _sign(string_to_sign: str, sk: str) -> str:
    return hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_request(method, host, path, query=None, headers=None, body=None,
                 ak=None, sk=None, x_sdk_date=None):
    """对请求签名，返回需附加的请求头 dict（含 X-Sdk-Date 与 Authorization）。"""
    if not ak or not sk:
        raise ValueError("ak and sk are required")

    headers = dict(headers or {})
    if x_sdk_date:
        request_date = x_sdk_date
    else:
        for key, value in headers.items():
            if key.lower() == "x-sdk-date":
                request_date = value
                break
        else:
            request_date = datetime.now(timezone.utc).strftime(DATE_FORMAT)
    headers["X-Sdk-Date"] = request_date

    if content_hash(headers, body) == "UNSIGNED-PAYLOAD":
        headers.setdefault("X-Sdk-Content-Sha256", "UNSIGNED-PAYLOAD")

    signed_headers = signed_headers_list(headers)
    cr = build_canonical_request(method, host, path, query, headers, body)
    sts = string_to_sign(cr, request_date)
    sig = _sign(sts, sk)

    return {
        "X-Sdk-Date": request_date,
        "Authorization": (f"{ALGORITHM} Access={ak}, "
                          f"SignedHeaders={';'.join(signed_headers)}, "
                          f"Signature={sig}"),
    }
