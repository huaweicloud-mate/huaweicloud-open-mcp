"""OBS 执行 lane：请求构建（桶寻址/XML body）、OBS 签名发送、XML 错误解析。

OBS 与其它华为云服务在签名（HMAC-SHA1）、寻址（桶在 path）、请求体（XML /
octet-stream）、错误响应（XML <Error>）上均不同，全部内聚在本模块，
generic 路径（execute.py + signer/sign.py）零改动。

路由谓词 is_obs 由 service 层在 load_api_doc 后分派。
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from common import http as common_http
from common.auth.credentials import Credentials
from common.types import ClientResponse, ExecuteResult

from . import execute
from .signer import obs as obs_sign

logger = logging.getLogger("mcp_openapi.execute_obs")

PATH_PARAM = re.compile(r"\{([^}]+)\}")


def is_obs(product: str, doc: dict[str, Any]) -> bool:
    """判定接口是否走 OBS 执行 lane：按产品名，host 以 obs. 开头兜底。"""
    if (product or "").upper() == "OBS":
        return True
    host = doc.get("host")
    return isinstance(host, str) and host.startswith("obs.")


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


def _node_to_xml(name: str, schema: Any, value: Any, doc: dict[str, Any]) -> str:
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
        return f"<{name}>{inner}</{name}>"
    return f"<{name}>{_xml_escape(str(value))}</{name}>"


def serialize_body_xml(op: dict[str, Any], doc: dict[str, Any],
                       body: Any) -> str | None:
    """把 body dict 按 op 的 body 参数 schema 序列化为 OBS XML 字符串（根元素取 xml.name）。"""
    if body is None:
        return None
    body_param = next((p for p in (op.get("parameters") or [])
                       if isinstance(p, dict) and p.get("in") == "body"), None)
    schema = _resolve((body_param or {}).get("schema") or {}, doc)
    root_name: str | None = None
    if isinstance(schema, dict) and isinstance(schema.get("xml"), dict):
        root_name = schema["xml"].get("name")
    if not root_name:
        root_name = (body_param or {}).get("name") or "Body"
    return _node_to_xml(root_name, schema, body, doc)


# ---------- 请求构建（S9c） ----------

@dataclass
class ObsRequest:
    bucket: str
    object_key: str = ""
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    content_type: str = "application/xml"


def build_obs_request(op: dict[str, Any], path: str,
                      params: dict[str, Any], doc: dict[str, Any]) -> ObsRequest | str:
    """把用户参数映射为 OBS 请求（bucket/object/query/headers/body）。返回 ObsRequest 或错误描述。"""
    params = dict(params or {})
    declared = {p.get("name"): p for p in (op.get("parameters") or [])
                if isinstance(p, dict) and p.get("name")}

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

    if "bucket_name" in declared and not bucket:
        return "缺少必填参数 bucket_name（桶名）"
    if "{object_key}" in path and object_key in (None, ""):
        return "缺少必填路径参数 object_key（对象名）"

    if isinstance(body_value, dict):
        body_str = serialize_body_xml(op, doc, body_value)
        content_type = "application/xml"
    elif body_value is not None:
        body_str = str(body_value)
        content_type = "application/octet-stream"
    else:
        body_str = None
        content_type = "application/xml"

    return ObsRequest(
        bucket=bucket,
        object_key="" if object_key is None else str(object_key),
        query=query,
        headers=headers,
        body=body_str,
        content_type=content_type,
    )


def build_obs_url(host: str, bucket: str, object_key: str = "",
                  query: dict[str, Any] | None = None) -> str:
    """构造 path-style URL：https://{host}/{bucket}[/{object}][?query]（键排序，空值不带 =）。"""
    from urllib.parse import quote
    path = "/" + quote(bucket, safe="")
    if object_key:
        path += "/" + obs_sign.escape_object_key(object_key)
    url = f"https://{host}{path}"
    if query:
        parts: list[str] = []
        for key in sorted(query):
            value = query[key]
            if value is None:
                continue
            value = str(value)
            parts.append(quote(key, safe="") + (f"={quote(value, safe='')}" if value else ""))
        if parts:
            url += "?" + "&".join(parts)
    return url


# ---------- XML 错误解析（S9d） ----------

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
                body: str | None = None) -> ClientResponse: ...


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
                body: str | None = None) -> ClientResponse:
        import urllib.error
        import urllib.request

        method = method.upper()
        headers = dict(headers or {})
        body_bytes = None
        if body is not None:
            body_bytes = body.encode("utf-8")

        creds = self.credentials
        extra = obs_sign.sign_obs(
            method, ak=creds.ak, sk=creds.sk, bucket=bucket, object_key=object_key,
            query=query or {}, headers=headers,
        ) if creds and creds.ak and creds.sk else {}
        headers.update(extra)

        url = build_obs_url(host, bucket, object_key, query or {})
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
        logger.info("%s https://%s%s -> %s",
                    method, host, build_obs_url(host, bucket, object_key, query or {}), status)
        return {"status": status, "headers": resp_headers,
                "body": common_http.parse_body(raw)}


# ---------- 编排（S9e） ----------

def _normalize_obs(resp: ClientResponse) -> ExecuteResult:
    status = resp.get("status", 0)
    if 200 <= status < 300:
        return execute.normalize_response(resp)
    parsed = parse_obs_error(resp.get("body"))
    if parsed is not None:
        code, msg = parsed
        return {"status": status, "error_code": code, "error_msg": msg}
    return execute.normalize_response(resp)


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
