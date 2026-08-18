"""execute_api 业务函数：safety 检查 → 请求构建 → 调用 → 响应规范化（真实模式）。"""

import json
import logging
import re
from typing import Any, Protocol, Sequence

from ..auth.credentials import Credentials
from ..safety import policy as safety_policy
from ..types import ClientResponse, ExecuteResult

logger = logging.getLogger("openmcp.tools.execute")

MAX_RESPONSE_CHARS = 200_000
PATH_PARAM = re.compile(r"\{([^}]+)\}")


class ApiExecutor(Protocol):
    """执行层协议：execute_api 只依赖 request()，不耦合具体客户端实现。"""

    def request(self, method: str, host: str, path: str, *,
                query: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> ClientResponse: ...


def _refuse(reason: str) -> ExecuteResult:
    return {"ok": False, "reason": reason}


def build_request(op: dict[str, Any], path: str, params: dict[str, Any],
                  credentials: Credentials | None) -> tuple[str | None, dict[str, Any],
                                                           dict[str, Any] | None,
                                                           dict[str, str], str | None]:
    """把用户参数映射到 path/query/body。

    返回 (filled_path, query, body, headers, error)。error 为描述字符串或 None。
    """
    params = dict(params or {})
    path_params: dict[str, Any] = {}
    query: dict[str, Any] = {}
    body: dict[str, Any] | None = None
    headers: dict[str, str] = {}

    declared = {p.get("name"): p for p in (op.get("parameters") or [])}

    for name in PATH_PARAM.findall(path):
        value = params.pop(name, None)
        if value is None and name == "project_id" and credentials and credentials.project_id:
            value = credentials.project_id
        if value is None:
            return None, {}, None, {}, \
                f"缺少必填路径参数 {name}（可用凭证 project_id 自动填充 project_id）"
        path_params[name] = value

    for name, value in list(params.items()):
        if name in declared:
            pin = declared[name].get("in")
            if pin == "body":
                body = value
            elif pin == "header":
                headers[name] = str(value)
            elif pin == "query":
                query[name] = value
        elif name == "body":
            body = value
        else:
            query[name] = value

    if body is not None:
        headers.setdefault("Content-Type", "application/json")

    filled = path
    for name, value in path_params.items():
        filled = filled.replace("{" + name + "}", str(value))
    return filled, query, body, headers, None


def normalize_response(resp: ClientResponse) -> ExecuteResult:
    """把客户端响应规范化为结构化输出。"""
    status = resp.get("status", 0)
    raw = resp.get("body")
    if 200 <= status < 300:
        out: ExecuteResult = {"status": status}
        if raw is None:
            out["body"] = None
        else:
            text = json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
            if len(text) > MAX_RESPONSE_CHARS:
                out["truncated"] = True
                if isinstance(raw, str):
                    out["body"] = raw[:MAX_RESPONSE_CHARS]
                else:
                    out["body"] = {"truncated": True,
                                   "note": f"响应超过 {MAX_RESPONSE_CHARS} 字符，已截断",
                                   "raw_size": len(text)}
            else:
                out["body"] = raw
        return out
    out = {"status": status}
    if isinstance(raw, dict):
        out["error_code"] = raw.get("error_code")
        out["error_msg"] = raw.get("error_msg") or raw.get("message")
    else:
        out["error_msg"] = str(raw)[:1000] if raw else f"HTTP {status}"
    return out


def execute_api(doc: dict[str, Any], path: str, method: str, op: dict[str, Any],
                product: str, api_name: str, region: str, params: dict[str, Any],
                *, policy_rules: Sequence[safety_policy.PolicyRule] | None,
                client: ApiExecutor, credentials: Credentials | None = None) -> ExecuteResult:
    """执行真实 API：safety policy → 请求构建 → 调用 → 规范化。"""
    err = safety_policy.check(policy_rules, product, api_name)
    if err:
        logger.warning("execute %s:%s region=%s mode=real policy=%s",
                       product, api_name, region,
                       "unconfigured" if policy_rules is None else "deny")
        return _refuse(err)
    logger.info("execute %s:%s region=%s mode=real policy=allow",
                product, api_name, region)

    filled, query, body, headers, err = build_request(op, path, params, credentials)
    if err:
        return _refuse(err)
    assert filled is not None

    host = doc.get("host")
    if not isinstance(host, str) or not host:
        return _refuse("接口元数据缺少 host，无法执行")

    if credentials and credentials.project_id and "{project_id}" not in path:
        headers.setdefault("X-Project-Id", credentials.project_id)

    resp = client.request(method.upper(), host, filled,
                          query=query, body=body, headers=headers)
    out = normalize_response(resp)
    out.update({"ok": True, "product": product, "api": api_name})
    return out
