"""OBS 执行 lane：请求构建（桶寻址/XML body）、OBS 签名发送、XML 错误解析。

OBS 与其它华为云服务在签名（HMAC-SHA1）、寻址（桶在 path）、请求体（XML /
octet-stream）、错误响应（XML <Error>）上均不同，全部内聚在本模块，
generic 路径（execute.py + signer/sign.py）零改动。

路由谓词 is_obs 由 service 层在 load_api_doc 后分派。
"""

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from common import http as common_http
from common.auth.credentials import Credentials
from common.types import ClientResponse, ExecuteResult, PresignInfo

from . import execute
from .signer import obs as obs_sign

logger = logging.getLogger("mcp_openapi.execute_obs")

PATH_PARAM = re.compile(r"\{([^}]+)\}")

# 「开关型」子资源：API Explorer 元数据把它们声明为 required 空值 query，
# 语义上等同路由标识（如 ?tagging ?acl）。调用方无需关心，缺失时自动补 ""
AUTO_SUBRESOURCES = frozenset({
    "acl", "backtosource", "cors", "customdomain", "delete", "directcoldaccess",
    "encryption", "inventory", "lifecycle", "location", "logging", "metadata",
    "mirrorbacktosource", "notification", "object-lock", "obscompresspolicy",
    "policy", "policystatus", "publicaccessblock", "quota", "replication",
    "retention", "storageinfo", "storageclass", "storagepolicy", "tagging",
    "torrent", "uploads", "versioning", "versions", "website",
})


def is_obs(product: str, doc: dict[str, Any]) -> bool:
    """判定接口是否走 OBS 执行 lane：按产品名，host 以 obs. 开头兜底。"""
    if (product or "").upper() == "OBS":
        return True
    host = doc.get("host")
    return isinstance(host, str) and host.startswith("obs.")


# 对象数据面接口：真实模式下恒走预签发 URL 单口径（gateway 不搬运对象字节）。
# CopyObject 为服务端复制（字节不过 gateway，且需签名 x-obs-copy-source 头），不在此列。
OBJECT_DATA_APIS = frozenset({"PutObject", "GetObject", "AppendObject", "UploadPart"})


def is_object_data_api(api_name: str, op: dict[str, Any] | None = None) -> bool:
    """判定是否对象字节面接口：按 api 名或 operationId 精确命中名单。"""
    candidates = {(api_name or "").strip()}
    oid = (op or {}).get("operationId")
    if isinstance(oid, str):
        candidates.add(oid.strip())
    return bool(candidates & OBJECT_DATA_APIS)


# ---------- XML body 序列化（S9b） ----------

def _resolve(schema: Any, doc: dict[str, Any]) -> Any:
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref.startswith("#/definitions/"):
            target = (doc.get("definitions") or {}).get(ref.split("/")[-1])
            if isinstance(target, dict):
                merged = dict(target)
                for k, v in schema.items():
                    if k != "$ref":
                        merged[k] = v
                return merged
    return schema


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


OBS_XMLNS_FMT = "http://obs.{region}.myhuaweicloud.com/doc/2015-06-30/"


def _obs_xmlns(doc: dict[str, Any]) -> str:
    """从 doc.host 抽取 region 拼 OBS 文档命名空间；无法识别时不加 xmlns。"""
    host = doc.get("host") if isinstance(doc, dict) else None
    if isinstance(host, str) and host.startswith("obs.") and "." in host[4:]:
        return OBS_XMLNS_FMT.format(region=host.split(".")[1])
    return ""


def serialize_body_xml(op: dict[str, Any], doc: dict[str, Any],
                       body: Any) -> str | None:
    """把 body dict 按 op 的 body 参数 schema 序列化为 OBS XML 字符串。

    根元素名取 xml.name/x-xml-root，并按官方示例注入默认命名空间
    （服务端 schema 校验要求，缺省报 MalformedXML）。
    """
    if body is None:
        return None
    body_param = next((p for p in (op.get("parameters") or [])
                       if isinstance(p, dict) and p.get("in") == "body"), None)
    schema = _resolve((body_param or {}).get("schema") or {}, doc)
    root_name = _root_element_name(schema)
    if not root_name:
        root_name = (body_param or {}).get("name") or "Body"
    xmlns = _obs_xmlns(doc)
    open_tag = f"<{root_name} xmlns=\"{xmlns}\">" if xmlns else f"<{root_name}>"
    return _node_to_xml(root_name, schema, body, doc, open_tag=open_tag)


def _node_to_xml(name: str, schema: Any, value: Any, doc: dict[str, Any],
                 open_tag: str | None = None) -> str:
    schema = _resolve(schema, doc)
    if isinstance(value, list):
        items = (schema or {}).get("items") if isinstance(schema, dict) else None
        return "".join(_node_to_xml(name, items or {}, item, doc) for item in value)
    if isinstance(value, dict):
        props = (schema.get("properties") if isinstance(schema, dict) else None) or {}
        inner = ""
        if props:
            for prop_name, prop_schema in props.items():
                if prop_name in value and value[prop_name] is not None:
                    inner += _node_to_xml(prop_name, prop_schema, value[prop_name], doc)
        else:
            for k, v in value.items():
                if v is not None:
                    inner += _node_to_xml(k, None, v, doc)
        close = f"</{name}>"
        start = open_tag if open_tag else f"<{name}>"
        return f"{start}{inner}{close}"
    return f"<{name}>{_xml_escape(str(value))}</{name}>"


def _root_element_name(schema: Any) -> str | None:
    """OBS XML 根元素名：schema.xml.name（原始键）∥ x-xml-root（转换管线提升键）。"""
    if isinstance(schema, dict):
        if isinstance(schema.get("xml"), dict) and schema["xml"].get("name"):
            return str(schema["xml"]["name"])
        root = schema.get("x-xml-root")
        if isinstance(root, str):
            return root
    return None


# ---------- 请求构建（S9c） ----------

@dataclass
class ObsRequest:
    bucket: str
    object_key: str = ""
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: str | bytes | None = None
    content_type: str = "application/xml"


def _norm_bool(value: Any) -> Any:
    """query 参数布尔值归一化为 OBS 期望的小写字面量。"""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return value


def _media_type(op: dict[str, Any], doc: dict[str, Any]) -> str:
    """请求媒体类型：op 级 consumes（转换后幸存）优先，doc 级兜底。"""
    cons = op.get("consumes") or doc.get("consumes") or []
    for c in cons if isinstance(cons, list) else [cons]:
        if isinstance(c, str):
            return c.lower()
    return ""


def build_obs_request(op: dict[str, Any], path: str,
                      params: dict[str, Any], doc: dict[str, Any]) -> ObsRequest | str:
    """把用户参数映射为 OBS 请求（bucket/object/query/headers/body）。返回 ObsRequest 或错误描述。

    媒体分流：body 为 dict/list 时按 op consumes 分 JSON / XML；
    非结构化字符串按原文 octet-stream 发送。大文件上传走 _presign 预签发 URL（客户端直传）。
    """
    params = dict(params or {})
    declared = {p.get("name"): p for p in (op.get("parameters") or [])
                if isinstance(p, dict) and p.get("name")}
    media = _media_type(op, doc)

    bucket: str = ""
    object_key: str | None = None
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    body_value: Any = params.pop("body", None)

    for name, value in list(params.items()):
        if name == "bucket_name":
            bucket = str(value) if value is not None else ""
            continue
        if name == "object_key":
            object_key = value
            continue
        pin = (declared.get(name) or {}).get("in") if name in declared else None
        if pin == "query":
            query[name] = value
        elif pin == "header":
            if name.lower() not in ("authorization", "date"):
                headers[name] = str(value)
        elif pin == "body":
            body_value = value
        else:
            query[name] = value

    query = {k: _norm_bool(v) for k, v in query.items()}

    if "bucket_name" in declared and not bucket:
        return "缺少必填参数 bucket_name（桶名）"
    if "{object_key}" in path and object_key in (None, ""):
        return "缺少必填路径参数 object_key（对象名）"

    # 开关型子资源缺失自动补空值；其余 required 参数缺失统一报错
    missing: list[str] = []
    declared_items: list[tuple[Any, Any]] = [(n2, d2) for n2, d2 in declared.items()
                                             if isinstance(d2, dict) and d2.get("required")]
    for name_any, spec in declared_items:
        pname = name_any if isinstance(name_any, str) else str(name_any)
        if pname == "bucket_name":
            continue
        pin2: Any | None = spec.get("in")
        if pin2 == "query":
            if pname in AUTO_SUBRESOURCES:
                query.setdefault(pname, "")
            elif pname.lower() != "object_key" and pname not in query:
                missing.append(pname)
    if missing:
        return "缺少必填参数: " + "、".join(missing)

    body: str | bytes | None
    if isinstance(body_value, (dict, list)):
        if "application/json" in media:
            body = json.dumps(body_value, ensure_ascii=False)
            content_type = "application/json"
        else:
            body = serialize_body_xml(op, doc, body_value)
            content_type = "application/xml"
    elif body_value is not None:
        body = str(body_value)
        content_type = "application/octet-stream"
    else:
        body = None
        content_type = "application/xml"

    return ObsRequest(
        bucket=bucket,
        object_key="" if object_key is None else str(object_key),
        query=query,
        headers=headers,
        body=body,
        content_type=content_type,
    )


def is_virtual_hosted(host: str, bucket: str) -> bool:
    """带桶且端点为 obs.* 时用 virtual-hosted（线上对桶级操作强制）。"""
    return bool(bucket) and host.startswith("obs.")


def build_obs_url(host: str, bucket: str, object_key: str = "",
                  query: dict[str, Any] | None = None) -> str:
    """构造请求 URL。带桶时用 virtual-hosted（线上对桶级操作强制），无桶保留端点根；
    保证请求行始终携带根路径 `/`（urllib 对空路径会发出非法 selector）。
    签名 CanonicalizedResource 由 sign_obs 的 virtual_hosted 参数保持同形态。"""
    from urllib.parse import quote
    if is_virtual_hosted(host, bucket):
        base = f"https://{quote(bucket, safe='')}.{host}/"
        if object_key:
            base += obs_sign.escape_object_key(object_key)
    else:
        base = f"https://{host}"
        if bucket:
            base += "/" + quote(bucket, safe="")
            if object_key:
                base += "/" + obs_sign.escape_object_key(object_key)
        else:
            base += "/"
    if query:
        parts: list[str] = []
        for key in sorted(query):
            value = query[key]
            if value is None:
                continue
            value = str(value)
            parts.append(quote(key, safe="") + (f"={quote(value, safe='')}" if value else ""))
        if parts:
            base += "?" + "&".join(parts)
    return base


# ---------- 响应渲染与 XML 错误解析（S9d） ----------

_TEXTUAL_CT_MARKS = ("/json", "/xml", "text/")


def render_response_body(status: int, content_type: str | None, raw: bytes) -> Any:
    """响应体渲染：2xx 且非文本类媒体（或字节不可解码/含 NUL）时返回二进制占位，
    避免把图片等对象内容 utf-8-replace 成乱码文本。其余走通用解析。"""
    raw = raw or b""
    ct = (content_type or "").lower()
    if 200 <= status < 300 and raw and not any(m in ct for m in _TEXTUAL_CT_MARKS):
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        if decoded is None or "\x00" in decoded:
            return {"note": "二进制响应未渲染", "size": len(raw),
                    "content_type": content_type}
        return common_http.parse_body(raw)
    return common_http.parse_body(raw)


def _tag_text(xml_text: str, name: str) -> str | None:
    m = re.search(rf"<{name}>(.*?)</{name}>", xml_text, re.DOTALL)
    if not m:
        return None
    return m.group(1).strip()


def parse_obs_error(body: Any) -> tuple[str, str] | None:
    """解析 OBS XML <Error> 响应，返回 (code, message) 或 None。"""
    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text.startswith("<"):
        return None
    code = _tag_text(text, "Code")
    msg = _tag_text(text, "Message")
    if code is None and msg is None:
        return None
    return (code or "", msg or "")


# ---------- 传输客户端 ----------

class ObsClient(Protocol):
    def request(self, method: str, host: str, *, bucket: str,
                object_key: str = "",
                query: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None,
                body: str | bytes | None = None) -> ClientResponse: ...


class ObsHttpClient:
    def __init__(self, credentials: Credentials | None = None, *,
                 timeout: int = 30, max_retries: int = 4,
                 retry_backoff: float = 2.0):
        self.credentials = credentials
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def request(self, method: str, host: str, *, bucket: str,
                object_key: str = "",
                query: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None,
                body: str | bytes | None = None) -> ClientResponse:
        import urllib.error
        import urllib.request

        method = method.upper()
        headers = dict(headers or {})
        body_bytes: bytes | None = None
        if isinstance(body, bytes):
            body_bytes = body
        elif body is not None:
            body_bytes = str(body).encode("utf-8")

        creds = self.credentials
        vh = is_virtual_hosted(host, bucket)
        if body_bytes and not any(k.lower() == "content-md5" for k in headers):
            # OBS 对带 body 的写请求强制 Content-MD5（缺失报 InvalidRequest/MalformedXML）
            import hashlib
            headers["Content-MD5"] = base64.b64encode(
                hashlib.md5(body_bytes).digest()).decode("ascii")
        extra = obs_sign.sign_obs(
            method, ak=creds.ak, sk=creds.sk, bucket=bucket, object_key=object_key,
            query=query or {}, headers=headers,
            virtual_hosted=vh,
        ) if creds and creds.ak and creds.sk else {}
        headers.update(extra)

        url = build_obs_url(host, bucket, object_key, query or {})
        logger.debug("obs request %s %s ct=%s md5=%s body[:200]=%r",
                     method, url, headers.get("Content-Type"),
                     headers.get("Content-MD5"),
                     (body_bytes or b"")[:200])
        req = urllib.request.Request(url, data=body_bytes, method=method, headers=headers)

        def _do() -> tuple[int, dict[str, str], bytes]:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()

        try:
            status, resp_headers, raw = common_http._retry(
                _do, max_retries=self.max_retries, backoff=self.retry_backoff,
                logger_name="mcp_openapi.execute_obs")
        except urllib.error.HTTPError as e:
            status, resp_headers, raw = e.code, dict(e.headers), e.read()
        logger.info("%s %s -> %s", method, url, status)
        return {"status": status, "headers": resp_headers,
                "body": render_response_body(
                    status, resp_headers.get("Content-Type") or resp_headers.get("content-type"),
                    raw)}


# ---------- 编排（S9e） ----------

_OBS_RESULT_HEADERS = ("etag", "x-obs-request-id", "x-obs-id-2", "x-obs-version-id",
                       "x-obs-next-append-position", "content-range")


def _pick_headers(resp: ClientResponse) -> dict[str, str] | None:
    """按白名单摘取响应头（小写键），OBS 的成功结果常在头域（如 ETag）。"""
    out = {}
    for k, v in (resp.get("headers") or {}).items():
        if isinstance(k, str) and k.lower() in _OBS_RESULT_HEADERS and v:
            out[k.lower()] = str(v)
    return out or None


def _normalize_obs(resp: ClientResponse) -> ExecuteResult:
    status = resp.get("status", 0)
    picked = _pick_headers(resp)
    out: ExecuteResult
    if 200 <= status < 300:
        out = execute.normalize_response(resp)
    else:
        parsed = parse_obs_error(resp.get("body"))
        if parsed is not None:
            code, msg = parsed
            out = {"status": status, "error_code": code, "error_msg": msg}
        else:
            out = execute.normalize_response(resp)
    if picked is not None:
        out["headers"] = picked
    return out


def execute_obs_api(doc: dict[str, Any], path: str, method: str, op: dict[str, Any],
                    product: str, api_name: str, region: str, params: dict[str, Any],
                    *, client: ObsClient,
                    credentials: Credentials | None = None) -> ExecuteResult:
    """执行 OBS API：请求构建 → OBS 签名发送 → 响应规范化（safety 已由上层完成）。"""
    logger.info("execute %s:%s region=%s mode=obs", product, api_name, region)

    built = build_obs_request(op, path, params, doc)
    if isinstance(built, str):
        return {"ok": False, "reason": built}

    host = doc.get("host")
    if not isinstance(host, str) or not host:
        return {"ok": False, "reason": "接口元数据缺少 host，无法执行"}

    headers = dict(built.headers)
    if built.body is not None:
        headers.setdefault("Content-Type", built.content_type)

    resp = client.request(method.upper(), host, bucket=built.bucket,
                          object_key=built.object_key, query=built.query,
                          headers=headers, body=built.body)
    out = _normalize_obs(resp)
    out.update({"ok": True, "product": product, "api": api_name})
    return out


# ---------- 预签发 URL 编排（S9f-b） ----------

PRESIGN_DEFAULT_EXPIRES = 900


def execute_presign_api(doc: dict[str, Any], path: str, method: str, op: dict[str, Any],
                        product: str, api_name: str, region: str,
                        params: dict[str, Any], *,
                        credentials: Credentials | None) -> ExecuteResult:
    """预签发 OBS 访问 URL（gateway 只签名，字节流由客户端直连 OBS 完成）。

    `_presign_expires` 有效期秒数（默认 900）；`_presign_content_type` 可锁定 PUT 的
    Content-Type 参与签名；其余参数按常规 lane 切分桶/对象/白名单 query。
    本路径零网络请求、零落盘，部署拓扑无关。
    """
    logger.info("execute %s:%s region=%s mode=presign", product, api_name, region)

    control = dict(params or {})
    expires_raw: Any = control.pop("_presign_expires", None)
    content_type = str(control.pop("_presign_content_type", "") or "")
    try:
        expires = int(expires_raw) if expires_raw is not None else PRESIGN_DEFAULT_EXPIRES
    except (TypeError, ValueError):
        return {"ok": False, "reason": "_presign_expires 必须为正整数秒"}
    if expires <= 0:
        return {"ok": False, "reason": "_presign_expires 必须为正整数秒"}

    if not (credentials and credentials.ak and credentials.sk):
        return {"ok": False, "reason": "_presign 需要可用 AK/SK 凭证（当前环境未配置）"}

    built = build_obs_request(op, path, control, doc)
    if isinstance(built, str):
        return {"ok": False, "reason": built}

    host = doc.get("host")
    if not isinstance(host, str) or not host:
        return {"ok": False, "reason": "接口元数据缺少 host，无法执行"}

    import time
    url = obs_sign.sign_obs_url(
        method.upper(), ak=credentials.ak, sk=credentials.sk, host=host,
        bucket=built.bucket, object_key=built.object_key, query=built.query,
        expires=int(time.time()) + expires, virtual_hosted=bool(built.bucket),
        content_type=content_type,
    )
    presign = PresignInfo(url=url, method=method.upper(), expires_in=expires,
                          signed_content_type=content_type,
                          headers=({"Content-Type": content_type}
                                   if content_type else {}))
    if not content_type and method.upper() in ("PUT", "POST"):
        presign["note"] = (
            "签名按空 Content-Type 计算：直连请求请勿携带该头"
            "（curl 示例：-H 'Content-Type:' 移除默认头），"
            "否则 SignatureDoesNotMatch；如需锁定类型，重走 execute_api "
            "并传 _presign_content_type，随后携带一致的头域")
    return {"ok": True, "product": product, "api": api_name,
            "presign": presign}
