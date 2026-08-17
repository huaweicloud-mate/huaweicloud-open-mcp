"""execute_api 业务函数：safety 检查 → 请求构建 → 调用 → 响应规范化。"""

import json
import re

from ..safety import policy as safety_policy

MAX_RESPONSE_CHARS = 200_000
PATH_PARAM = re.compile(r"\{([^}]+)\}")


def _refuse(reason):
    return {"ok": False, "reason": reason}


def build_request(op, path, params, credentials):
    """把用户参数映射到 path/query/body。

    返回 (filled_path, query, body, headers, error)。error 为描述字符串或 None。
    """
    params = dict(params or {})
    path_params = {}
    query = {}
    body = None
    headers = {}

    declared = {p.get("name"): p for p in (op.get("parameters") or [])}

    for name in PATH_PARAM.findall(path):
        value = params.pop(name, None)
        if value is None and name == "project_id" and credentials and credentials.project_id:
            value = credentials.project_id
        if value is None:
            return None, None, None, None, f"缺少必填路径参数 {name}（可用凭证 project_id 自动填充 project_id）"
        path_params[name] = value

    for name, value in list(params.items()):
        if name in declared:
            pin = declared[name].get("in")
            if pin == "body":
                body = value
            elif pin in ("query", "header"):
                if pin == "header":
                    headers[name] = str(value)
                else:
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


def normalize_response(resp):
    """把客户端响应规范化为结构化输出。"""
    status = resp.get("status", 0)
    raw = resp.get("body")
    if 200 <= status < 300:
        out = {"status": status}
        if raw is None:
            out["body"] = None
        else:
            text = json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
            if len(text) > MAX_RESPONSE_CHARS:
                out["body"] = raw
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


def execute_api(doc, path, method, op, product, api_name, region, params,
                policy_rules, client, credentials=None, mock=False):
    """执行 API：safety policy → 请求构建 → 调用 → 规范化。

    mock=True 时路由到 API Explorer mock 端点（client.mock_request）。
    """
    if policy_rules is None:
        return _refuse("safety policy 未配置，execute_api 全部拒绝")
    if not safety_policy.evaluate(policy_rules, product, api_name):
        return _refuse(f"safety policy 拒绝执行 {product}:{api_name}")

    if mock:
        status_code = params.get("_status_code", 200) if isinstance(params, dict) else 200
        number = params.get("_number", 1) if isinstance(params, dict) else 1
        resp = client.mock_request(product, api_name, region, status_code=status_code, number=number)
        out = normalize_response(resp)
        out.update({"ok": True, "product": product, "api": api_name, "mock": True})
        return out

    filled, query, body, headers, err = build_request(op, path, params, credentials)
    if err:
        out = _refuse(err)
        return out

    if credentials and credentials.project_id and "{project_id}" not in path:
        headers.setdefault("X-Project-Id", credentials.project_id)

    resp = client.request(method.upper(), doc.get("host"), filled,
                          query=query, body=body, headers=headers)
    out = normalize_response(resp)
    out.update({"ok": True, "product": product, "api": api_name})
    return out
