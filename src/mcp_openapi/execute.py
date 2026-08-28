"""execute_api 业务函数：参数校验（policy）→ 请求构建（mechanism）→ 调用 → 响应规范化。
safety policy 检查已由 ToolService.execute_api 在上层完成。
"""

import json
import logging
import re
from typing import Any, Callable, Protocol

import jsonschema

from common.auth.credentials import Credentials
from common.types import ClientResponse, ExecuteResult

logger = logging.getLogger("mcp_openapi.execute")

MAX_RESPONSE_CHARS = 200_000
PATH_PARAM = re.compile(r"\{([^}]+)\}")

# 标量类型严格口径：str 不自动强转；bool 混入整型/数值显式排除
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "integer": lambda v: type(v) is int,
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
}


class ApiExecutor(Protocol):
    """执行层协议：execute_api 只依赖 request()，不耦合具体客户端实现。"""

    def request(self, method: str, host: str, path: str, *,
                query: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> ClientResponse: ...


def _refuse(reason: str) -> ExecuteResult:
    return {"ok": False, "reason": reason}


def _describe(value: Any) -> str:
    """错误消息里的实际值描述（自纠友好）。"""
    if isinstance(value, bool):
        return f"bool({str(value).lower()})"
    if isinstance(value, str):
        shown = value if len(value) <= 20 else value[:17] + "..."
        return f"str(\"{shown}\")"
    if isinstance(value, (int, float)):
        return repr(value)
    return type(value).__name__


def _path_param_values(path: str, params: dict[str, Any],
                       credentials: Credentials | None
                       ) -> tuple[dict[str, Any], str | None]:
    """路径参数知识单元（validator 与 builder 共享）：project_id 可由凭证填充。

    返回 (填充值, 缺失名或 None)。
    """
    values: dict[str, Any] = {}
    for name in PATH_PARAM.findall(path):
        value = params.get(name)
        if value is None and name == "project_id" and credentials and credentials.project_id:
            value = credentials.project_id
        if value is None:
            return {}, name
        values[name] = value
    return values, None


def validate_params(doc: dict[str, Any], path: str, op: dict[str, Any],
                    params: dict[str, Any] | None,
                    credentials: Credentials | None) -> str | None:
    """OpenAPI 2.0 元数据参数校验（policy 接缝，mock/real 分流前共享）。

    返回 None=放行；否则返回可操作错误描述（agent 可据此自纠）。
    口径：只校验文档声明了的参数（未声明宽容透传）；`_` 前缀控制键天然跳过；
    标量类型严格（integer/number/boolean 不接受字符串形式，bool 混入数值显式
    排除）；header 协议即字符串故只查必填不查类型；body 用 jsonschema
    （Draft4 + doc.definitions resolver）校验。
    路径参数不在此校验：mock URL 不含 path，路径语义仅 real lane 有意义
    （build_request 内守卫）；`path` 形参仅为签名对称保留。
    """
    params = params or {}
    declared = [p for p in (op.get("parameters") or []) if isinstance(p, dict)]

    body_schema: dict[str, Any] | None = None
    body_required = False
    for p in declared:
        pin = p.get("in")
        name = p.get("name")
        if not isinstance(name, str):
            continue
        value = params.get(name)
        if pin == "path":
            continue
        if pin == "body":
            if isinstance(p.get("schema"), dict) and body_schema is None:
                body_schema = p["schema"]
                body_required = bool(p.get("required"))
            continue
        if value is None:
            if p.get("required"):
                return f"缺少必填 {pin} 参数 {name}（get_api 可查参数定义）"
            continue
        if pin == "header":
            continue
        ptype = p.get("type")
        check = _TYPE_CHECKS.get(ptype) if isinstance(ptype, str) else None
        if check is not None and not check(value):
            return f"参数 query.{name} 类型应为 {ptype}，实际为 {_describe(value)}"
        enum = p.get("enum")
        if isinstance(enum, list) and value not in enum:
            return (f"参数 query.{name} 取值应为 {enum} 之一，"
                    f"实际为 {_describe(value)}")

    if body_schema is not None:
        body_value = params.get("body")
        if body_value is None:
            if body_required:
                return "缺少必填 body 参数（get_api 可查请求体定义）"
            return None
        resolver = jsonschema.RefResolver.from_schema(doc)
        validator = jsonschema.Draft4Validator(body_schema, resolver=resolver)
        errors = sorted(validator.iter_errors(body_value),
                        key=lambda e: list(e.absolute_path))
        if errors:
            e = errors[0]
            loc = "/".join(str(part) for part in e.absolute_path) or "(root)"
            return f"body 参数校验失败（{loc}）: {e.message}"
    return None


def build_request(op: dict[str, Any], path: str, params: dict[str, Any],
                  credentials: Credentials | None) -> tuple[str | None, dict[str, Any],
                                                            dict[str, Any] | None,
                                                            dict[str, str], str | None]:
    """把用户参数映射到 path/query/body（mechanism：假定参数已过 validate_params）。

    返回 (filled_path, query, body, headers, error)。error 为描述字符串或 None。
    路径参数语义（含 project_id 填充）由 _path_param_values 提供，此处仅守卫。
    """
    params = dict(params or {})
    path_values, missing = _path_param_values(path, params, credentials)
    if missing is not None:
        return None, {}, None, {}, \
            f"缺少必填路径参数 {missing}（可用凭证 project_id 自动填充 project_id）"
    for name in path_values:
        params.pop(name, None)  # 路径参数不重复进 query/body

    query: dict[str, Any] = {}
    body: dict[str, Any] | None = None
    headers: dict[str, str] = {}

    declared = {p.get("name"): p for p in (op.get("parameters") or [])}

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
    for name, value in path_values.items():
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
                *, client: ApiExecutor,
                credentials: Credentials | None = None) -> ExecuteResult:
    """执行真实 API：请求构建 → 调用 → 规范化（safety 已由 ToolService 完成）。"""
    logger.info("execute %s:%s region=%s mode=real",
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
