"""OBS Header 签名（HMAC-SHA1）实现。

OBS 与其它华为云服务签名算法不同：`Authorization: OBS <AK>:<Signature>`，
Signature = Base64(HMAC-SHA1(SK, StringToSign))，其中

StringToSign = VERB + "\\n" + Content-MD5 + "\\n" + Content-Type + "\\n" + Date + "\\n"
             + CanonicalizedOBSHeaders + CanonicalizedResource

算法与官方 OBS SDK（huaweicloud-sdk-go-obs obs/authV2.go + conf.go）及
官方文档「Header中携带签名」保持一致。
"""

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("mcp_openapi.signer.obs")

OBS_PREFIX = "x-obs-"
RFC1123_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"

# 官方 SDK allowedResourceParameterNames（小写），仅这些 query 键参与 CanonicalizedResource。
SUB_RESOURCES = frozenset({
    "acl", "backtosource", "metadata", "policy", "torrent", "logging",
    "location", "storageinfo", "quota", "storageclass", "storagepolicy",
    "requestpayment", "versions", "versioning", "versionid", "uploads",
    "uploadid", "partnumber", "website", "notification", "lifecycle",
    "deletebucket", "delete", "cors", "restore", "encryption", "tagging",
    "append", "modify", "position", "replication",
    "response-content-type", "response-content-language", "response-expires",
    "response-cache-control", "response-content-disposition",
    "response-content-encoding", "x-image-process", "x-oss-process",
    "x-image-save-bucket", "x-image-save-object", "ignore-sign-in-query",
    "name", "rename", "customdomain", "mirrorbacktosource",
    "x-obs-accesslabel", "object-lock", "retention", "x-obs-security-token",
    "truncate", "length", "inventory", "directcoldaccess", "attname",
    "cdnnotifyconfiguration", "publicaccessblock", "bucketstatus",
    "policystatus", "obscompresspolicy", "dispolicy",
})


def escape_object_key(object_key: str) -> str:
    """对象名 URL 编码：保留 `/`，空格与其余保留字符按 %XX 编码（对齐官方 SDK prepareObjectKey）。"""
    return quote(object_key, safe="/")


def canonicalized_resource(bucket: str, object_key: str = "",
                           query: dict[str, Any] | None = None,
                           virtual_hosted: bool = False) -> str:
    """构造 CanonicalizedResource。

    寻址风格影响形态（对齐官方 Go SDK conf.go prepareBaseURL）：
    - path-style 或自定义域名：`/{bucket}[/{object}]`
    - virtual-hosted（桶级操作线上强制）：`/{bucket}/[{object}]`（桶名后恒有 `/`）
    - 列举账号所有桶（无桶）：`/`
    仅白名单子资源（或 x-obs- 前缀键）参与签名，按字典序；值用原始字符串（不编码）。
    """
    if not bucket:
        resource = "/"
    elif virtual_hosted:
        resource = f"/{bucket}/"
    else:
        resource = f"/{bucket}"
    if object_key:
        if not virtual_hosted:
            resource += "/"
        resource += escape_object_key(object_key)

    if query:
        sub: dict[str, str] = {}
        for key, value in query.items():
            low = (key or "").strip().lower()
            if low in SUB_RESOURCES or low.startswith(OBS_PREFIX):
                sub[key] = "" if value is None else str(value)
        if sub:
            parts: list[str] = []
            for key in sorted(sub):
                value = sub[key]
                parts.append(quote(key, safe="") + (f"={value}" if value else ""))
            resource += "?" + "&".join(parts)
    return resource


def canonicalized_headers(headers: dict[str, str]) -> str:
    """CanonicalizedOBSHeaders：`x-obs-*` 头域，名小写、值去空白、按名字典序；
    每个 `name:value\\n`；无头域返回空串（无尾随换行）。"""
    by_low: dict[str, list[str]] = {}
    for key, value in headers.items():
        low = key.lower()
        if low.startswith(OBS_PREFIX):
            by_low.setdefault(low, []).append(str(value).strip())
    if not by_low:
        return ""
    return "".join(f"{low}:{','.join(by_low[low])}\n" for low in sorted(by_low))


def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return ""


def _has(headers: dict[str, str], name: str) -> bool:
    return any(key.lower() == name for key in headers)


def obs_string_to_sign(method: str, *, bucket: str, object_key: str = "",
                       query: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None,
                       virtual_hosted: bool = False) -> str:
    """构造 StringToSign。headers 含 content-md5/content-type/date/x-obs-*；
    x-obs-date 存在时 Date 位按空处理；virtual_hosted 决定 CanonicalizedResource 形态。"""
    headers = dict(headers or {})
    content_md5 = _header(headers, "content-md5")
    content_type = _header(headers, "content-type")
    date = _header(headers, "date")
    if _has(headers, "x-obs-date"):
        date = ""
    return (f"{method.upper()}\n"
            f"{content_md5}\n"
            f"{content_type}\n"
            f"{date}\n"
            f"{canonicalized_headers(headers)}"
            f"{canonicalized_resource(bucket, object_key, query, virtual_hosted)}")


def obs_signature(string_to_sign: str, sk: str) -> str:
    """Signature = Base64(HMAC-SHA1(SK, StringToSign))。"""
    digest = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def sign_obs(method: str, *, ak: str, sk: str, bucket: str, object_key: str = "",
             query: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None,
             date: str | None = None,
             virtual_hosted: bool = False) -> dict[str, str]:
    """对 OBS 请求签名，返回需附加的请求头 dict（含 Authorization 与 Date）。"""
    if not ak or not sk:
        raise ValueError("ak and sk are required")

    headers = dict(headers or {})
    if date is not None:
        headers["Date"] = date
    elif not _has(headers, "date") and not _has(headers, "x-obs-date"):
        headers["Date"] = datetime.now(timezone.utc).strftime(RFC1123_FORMAT)

    sts = obs_string_to_sign(method, bucket=bucket, object_key=object_key,
                             query=query, headers=headers,
                             virtual_hosted=virtual_hosted)
    logger.debug("sign_obs sts=%r", sts)
    out = {"Authorization": f"OBS {ak}:{obs_signature(sts, sk)}"}
    date_value = _header(headers, "date")
    if date_value:
        out["Date"] = date_value
    for key, value in headers.items():
        if key.lower() == "x-obs-date":
            out[key] = value
    return out
