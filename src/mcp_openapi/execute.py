"""execute_api 业务函数：参数校验（policy）→ 请求构建（mechanism）→ 调用 → 响应规范化。
safety policy 检查已由 ToolService.execute_api 在上层完成。
"""

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Callable, Protocol

import jsonschema

from apie.api_location import ApiLocation
from common.auth.credentials import Credentials
from common.types import ClientResponse, ExecuteResult, ExtractInfo, SpillInfo

from .extract import ExtractSpec, apply_extract
from .spill import MAX_RESPONSE_CHARS, SpillConfig, spill_body

logger = logging.getLogger("mcp_openapi.execute")

PATH_PARAM = re.compile(r"\{([^}]+)\}")

# 标量类型严格口径：str 不自动强转；bool 混入整型/数值显式排除
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "integer": lambda v: type(v) is int,
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
}

# 认证相关 header——由签名层自动注入（AK/SK 签名生成 Authorization +
# X-Security-Token；token 认证生成 X-Auth-Token），不要求用户传入。
# 匹配大小写不敏感（HTTP 头语义本就如此）：元数据 casing 混乱——x-auth-token
# 小写 869 处、X-Auth-token/x-Auth-Token 混合 19 处，精确匹配曾误拒 698 个 API op。
_AUTH_HEADERS = frozenset({
    "x-auth-token",
    "x-security-token",
    "authorization",
})

# 全局默认 Content-Type（2026-09 起）：real lane 全部请求（含无 body 的 GET/DELETE）
# setdefault application/json——官方 SDK 全局携带 CT 且从不注入 body 为既成先例；
# SDK-HMAC-SHA256 签名排除 content-type，加头对签名输出逐字节不变；显式传入不覆盖，
# 恒不注入空 body。原「方言产品名单 + 写方法集」两层口径因新方言产品持续出现（打地鼠）
# 收敛于此。Graduation trigger：规则超出「通用默认头」形态（如按产品注入
# X-Environment-Id/Client-Request-Id 等用户上下文头、按端点差异化头值）时，升级为
# 独立方言模块（OBS lane 先例）。


class SignedClient(Protocol):
    """传输端口：签名客户端形状（HttpClient 满足；测试注入 stub）。

    原名 ApiExecutor——2026-09 起让位于操作上下文执行接缝（CONTEXT.md A），
    本协议降为 RealApiExecutor 内部的传输端口。
    """

    def request(self, method: str, host: str, path: str, *,
                query: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> ClientResponse: ...


class RequestRefusal(Exception):
    """真实执行 adapter 的拒绝（缺必填路径参数 / doc 缺 host）。

    execute_api 捕获后转 {ok: false, reason}；mock adapter 永不抛出
    （mock URL 不含真实 path，无路径语义）。
    """


class ApiExecutor(Protocol):
    """执行接缝（CONTEXT.md A）：ToolService 与「操作如何到达华为云」之间。

    两个 adapter：RealApiExecutor（SDK-HMAC-SHA256 签名 + 从 OpenAPI 操作
    构建请求）与 MockApiExecutor（派生 API Explorer mock URL，剥 _ 控制键）。
    execute_api 调用之，再规范化 + 包装信封。
    """

    mode: str

    def request(self, location: "ApiLocation", product: str, api_name: str,
                region: str, params: dict[str, Any]) -> ClientResponse: ...


class RealApiExecutor:
    """真实 adapter：build_request（路径填充/参数切分）→ 默认 CT → basePath
    前缀 → X-Project-Id → 签名客户端发送。"""

    mode = "real"

    def __init__(self, client: SignedClient,
                 credentials: Credentials | None = None):
        self.client = client
        self.credentials = credentials

    def request(self, location: "ApiLocation", product: str, api_name: str,
                region: str, params: dict[str, Any]) -> ClientResponse:
        filled, query, body, headers, err = build_request(
            location.op, location.path, params, self.credentials)
        if err:
            raise RequestRefusal(err)
        assert filled is not None

        headers.setdefault("Content-Type", "application/json")

        host = location.doc.get("host")
        if not isinstance(host, str) or not host:
            raise RequestRefusal("接口元数据缺少 host，无法执行")

        base = location.doc.get("basePath")
        if isinstance(base, str) and base and base != "/":
            filled = base.rstrip("/") + filled

        if (self.credentials and self.credentials.project_id
                and "{project_id}" not in location.path):
            headers.setdefault("X-Project-Id", self.credentials.project_id)

        return self.client.request(location.method.upper(), host, filled,
                                   query=query, body=body, headers=headers)


class MockClient(Protocol):
    """mock 端点客户端端口（apie.mock.MockApiClient 满足；测试注入 stub）。"""

    def mock_request(self, product: str, api_name: str, region: str,
                     status_code: int = 200, number: int = 1,
                     params: Mapping[str, Any] | None = None) -> ClientResponse: ...


class MockApiExecutor:
    """mock adapter：剥 _status_code/_number 控制键 → API Explorer mock 端点。

    passthrough 开启时把业务参数原样交 mock 层编码（标量→query、body→POST）。
    永不抛 RequestRefusal（mock URL 不含真实 path，无路径语义）。
    """

    mode = "mock"

    def __init__(self, client: MockClient, *, passthrough: bool = False):
        self.client = client
        self.passthrough = passthrough

    def request(self, location: "ApiLocation", product: str, api_name: str,
                region: str, params: dict[str, Any]) -> ClientResponse:
        status_code = params.get("_status_code", 200)
        number = params.get("_number", 1)
        if self.passthrough:
            return self.client.mock_request(
                product, api_name, region, status_code=status_code,
                number=number, params=params)
        return self.client.mock_request(product, api_name, region,
                                        status_code=status_code, number=number)


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


# 校验视图不进入的「数据/扩展载荷」键：enum/default/example 是数据（改其内容会
# 改变枚举等约束的语义、可能误拒），x-* 对 Draft4 不透明（无需处理）。仅对 schema
# 位置（properties/items/additionalProperties/allOf 成员等）应用校验松弛变换。
_VALIDATION_VIEW_DATA_KEYS = frozenset({"enum", "default", "example"})


def _pattern_compilable(value: Any) -> bool:
    """pattern 是否可被 Python re 编译（与 jsonschema 编译路径一致，flags=0）。"""
    if not isinstance(value, str):
        return False
    try:
        re.compile(value)
        return True
    except (re.error, OverflowError, RecursionError):
        return False


def _relax_validation(key: str, value: Any) -> bool:
    """校验视图的删除谓词（只放松、不新增拒绝）。

    - ``allOf``：API Explorer 组合不可靠（跨分支冲突 + 与官方 x-request-examples
      不一致，实测官方示例被组合约束误拒）→ 校验层不强制（展示层保留）。
    - ``pattern``：服务端（Java/PCRE/ECMA-262）方言（``\\p{L}``/``[\\w-.]`` 等）
      Python re 编译期抛错，jsonschema 惰性编译时异常逃逸 execute_api；非字符串
      pattern 亦会令 Draft4 TypeError → 一并剥离。仅删该键，其余 required/type/
      enum/可编译 pattern 照常强制。
    """
    if key == "allOf":
        return True
    if key == "pattern" and not _pattern_compilable(value):
        logger.debug("校验视图忽略 pattern: %r", value)
        return True
    return False


def _strip_for_validation(node: Any) -> Any:
    """递归 copy-on-write 应用 ``_relax_validation``；不进入数据/扩展载荷。

    纯 / 幂等 / 只放松不新增拒绝。schema 节点重建，不原地改写输入。
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if not isinstance(k, str):
                continue
            if k in _VALIDATION_VIEW_DATA_KEYS or k.startswith("x-"):
                out[k] = v            # 数据/扩展载荷：原样保留，不递归
            elif _relax_validation(k, v):
                continue
            else:
                out[k] = _strip_for_validation(v)
        return out
    if isinstance(node, list):
        return [_strip_for_validation(x) for x in node]
    return node


def _validation_view(node: Any) -> Any:
    """校验视图：剥离 allOf 与不可编译/非字符串 pattern 的单一入口。

    展示层（get_api）/缓存 doc/离线产物均不经此变换——元数据真值不丢。
    """
    return _strip_for_validation(node)


def validate_params(doc: dict[str, Any], path: str, op: dict[str, Any],
                    params: dict[str, Any] | None,
                    credentials: Credentials | None) -> str | None:
    """OpenAPI 2.0 元数据参数校验（policy 接缝，mock/real 分流前共享）。

    返回 None=放行；否则返回可操作错误描述（agent 可据此自纠）。
    口径：只校验文档声明了的参数（未声明宽容透传）；`_` 前缀控制键天然跳过；
    标量类型严格（integer/number/boolean 不接受字符串形式，bool 混入数值显式
    排除）；header 协议即字符串故只查必填不查类型，但认证 header
    （x-auth-token/x-security-token/authorization，大小写不敏感）由签名层自动
    注入故跳过——豁免名单（AuthDemotePolicy）只影响元数据归一，不影响本校验层；
    body 用 jsonschema（Draft4 + doc.definitions resolver）校验，但 **allOf 组合
    与不可编译 pattern 均不强制**（见 `_validation_view`：Explorer 组合不可靠会
    误拒官方示例；PCRE 方言 pattern 在 Python re 下编译期抛错）。
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
        if pin == "header" and name.casefold() in _AUTH_HEADERS:
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
        validation_doc = {
            **doc,
            "definitions": _validation_view(doc.get("definitions") or {}),
        }
        resolver = jsonschema.RefResolver.from_schema(validation_doc)
        validator = jsonschema.Draft4Validator(_validation_view(body_schema),
                                               resolver=resolver)
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


def _content_type(headers: dict[str, str] | None) -> str | None:
    """大小写不敏感提取响应 Content-Type（urllib 头键 casing 不稳定）。"""
    if not headers:
        return None
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == "content-type" and v:
            return str(v)
    return None


def _render_body(raw: Any, spill: SpillConfig | None = None,
                 stem: str = "response",
                 content_type: str | None = None) -> tuple[Any, bool, SpillInfo | None]:
    """响应体渲染（成功/错误分支共用）：超限截断；配置 spill 时先把完整原始体
    保真落盘（S12 层级 1，截断前真值）。返回 (body, truncated, spill_info)。

    bytes 分支（不透明二进制，parse_body 分类产出）：恒占位不进信封（防乱码
    污染上下文），完整字节无条件落盘 `.bin`（无体积门槛——不落盘即整段丢失），
    占位携带 sha256 供 agent 独立校验；spill 未启用时 note 明示数据不可得。
    """
    if raw is None:
        return None, False, None
    if isinstance(raw, bytes):
        info = spill_body(raw, cfg=spill, stem=stem) if spill is not None else None
        note = ("二进制响应，完整字节已落盘（见 spill，可用占位体 sha256 校验）"
                if info is not None else "二进制响应未渲染（spill 未启用，完整数据不可得）")
        return {"binary": True, "size": len(raw), "content_type": content_type,
                "sha256": hashlib.sha256(raw).hexdigest(), "note": note}, True, info
    text = json.dumps(raw, ensure_ascii=False, default=str) \
        if not isinstance(raw, str) else raw
    if len(text) > MAX_RESPONSE_CHARS:
        info = spill_body(raw, cfg=spill, stem=stem) if spill is not None else None
        if isinstance(raw, str):
            return raw[:MAX_RESPONSE_CHARS], True, info
        return {"truncated": True,
                "note": f"响应超过 {MAX_RESPONSE_CHARS} 字符，已截断",
                "raw_size": len(text)}, True, info
    return raw, False, None


def _wire_len(raw: Any) -> int:
    """_render_body 同口径的体积度量（str 原长；dict/list 走 json.dumps）。"""
    if isinstance(raw, str):
        return len(raw)
    return len(json.dumps(raw, ensure_ascii=False, default=str))


def normalize_response(resp: ClientResponse, spill: SpillConfig | None = None,
                       stem: str = "response", *,
                       extract: ExtractSpec | None = None) -> ExecuteResult:
    """把客户端响应规范化为结构化输出。

    2xx：body 恒透出（超限截断；配置 spill 时完整原始体先落盘并在结果附
    spill 信封）。bytes body（不透明二进制）：占位 + `.bin` 落盘（保真不变量
    disk == wire）。非 2xx：error_code/error_msg 为尽力规范化字段（多形态兼容
    抽取，不命中保持 null）；body 恒透出原始体（真值源兜底），bytes 走
    占位 + 短 hex 描述。
    extract（_jsonpath 投影，S24）：在截断/spill 之前的 raw body 上求值。
    全命中 → body 替换为投影值 + truncated=True（body 非完整原始响应体）；
    真值双轨——原始体超限且投影可收进预算时先按层级 1 口径落盘原始体
    （spill 信封指原始体），投影自身超限时 spill 落盘投影、原始体未保留
    （note 明示）。未命中/部分命中 → body 保持 _render_body 现状（自纠面），
    extract 信封附 misses；载体（None/str/bytes）不适用 → no-op + note。
    未传 extract 时与既有行为逐字节一致（回归红线）。
    """
    status = resp.get("status", 0)
    raw = resp.get("body")
    extract_info: ExtractInfo | None = None
    pre_spill: SpillInfo | None = None
    full_hit = False
    body: Any = raw
    if extract is not None:
        outcome = apply_extract(raw, extract)
        if outcome.note is not None:
            extract_info = {"note": outcome.note}
        elif outcome.full_hit:
            full_hit = True
            if (spill is not None
                    and _wire_len(raw) > MAX_RESPONSE_CHARS
                    and _wire_len(outcome.extracted) <= MAX_RESPONSE_CHARS):
                pre_spill = spill_body(raw, cfg=spill, stem=stem)
            body = outcome.extracted
        else:
            miss_info: ExtractInfo = {}
            if outcome.extracted is not None:
                miss_info["extracted"] = outcome.extracted
            if outcome.misses:
                miss_info["misses"] = list(outcome.misses)
            extract_info = miss_info or None
    body, truncated, info = _render_body(body, spill, stem,
                                         content_type=_content_type(resp.get("headers")))
    out: ExecuteResult = {"status": status, "body": body}
    if truncated or full_hit:
        out["truncated"] = True
    if info is not None:
        out["spill"] = info
    elif pre_spill is not None:
        out["spill"] = pre_spill
    if full_hit:
        if info is not None:
            note = ("响应 body 已按 _jsonpath 投影替换且投影结果超限，"
                    "完整投影已落盘（见 spill 信封）；原始响应未保留")
        elif pre_spill is not None:
            note = "响应 body 已按 _jsonpath 投影替换；完整原始响应已落盘（见 spill 信封）"
        else:
            note = ("响应 body 已按 _jsonpath 投影替换，未选部分未保留"
                    "（如需完整原始响应，不传 _jsonpath 重新执行）")
        extract_info = {"note": note}
    if extract_info is not None:
        out["extract"] = extract_info
    if 200 <= status < 300:
        return out
    raw = resp.get("body")
    if isinstance(raw, dict):
        out["error_code"], out["error_msg"] = _extract_error_fields(raw)
    elif isinstance(raw, bytes):
        out["error_msg"] = f"binary body ({len(raw)} bytes): {raw[:16].hex()}"
    else:
        out["error_msg"] = str(raw)[:1000] if raw else f"HTTP {status}"
    return out


# ---------- 错误体形状兼容抽取（仅非 2xx；body 原始体恒透出兜底） ----------

# 华为云各服务错误体形态不一：平坦 error_code/error_msg 为标准，另有 IAM v3/Keystone
# 嵌套 error、OpenStack nova 系单键包装、SWR/Docker registry v2 errors 列表、平坦
# code/message 变体。分层 first-hit：code 候选 error_code 优先于 code，msg 候选独立匹配。
_ERROR_CODE_KEYS = ("error_code", "code", "errorCode")
_ERROR_MSG_KEYS = ("error_msg", "message", "errorMsg", "msg", "error_description")
_MAX_ERROR_DEPTH = 3


def _scalar_text(value: Any) -> str | None:
    """错误字段标量过滤：非空 str / 非 bool int（→str）采纳，其余不采纳。"""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _first_text(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """按候选键序首个标量值（值非标量/缺省时跳过该键继续）。"""
    for key in keys:
        text = _scalar_text(d.get(key))
        if text is not None:
            return text
    return None


def _extract_error_dict(d: dict[str, Any], depth: int) -> tuple[str | None, str | None]:
    if depth > _MAX_ERROR_DEPTH:
        return None, None
    for key in _ERROR_CODE_KEYS:
        code = _scalar_text(d.get(key))
        if code is not None:
            return code, _first_text(d, _ERROR_MSG_KEYS)
    msg = _first_text(d, _ERROR_MSG_KEYS)
    if msg is not None:
        return None, msg
    error = d.get("error")
    if isinstance(error, dict):
        return _extract_error_dict(error, depth + 1)
    if isinstance(error, str) and error:
        return None, error
    errors = d.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return _extract_error_dict(first, depth + 1)
        if isinstance(first, str) and first:
            return None, first
    if len(d) == 1:
        only = next(iter(d.values()))
        if isinstance(only, dict):
            return _extract_error_dict(only, depth + 1)
    return None, None


def _extract_error_fields(raw: Any) -> tuple[str | None, str | None]:
    """错误体形状兼容抽取 → (error_code, error_msg)；不识别返回 (None, None)。"""
    if isinstance(raw, dict):
        return _extract_error_dict(raw, 0)
    return None, None


def execute_api(location: ApiLocation, product: str, api_name: str,
                region: str, params: dict[str, Any], *,
                executor: ApiExecutor,
                spill: SpillConfig | None = None,
                extract: ExtractSpec | None = None) -> ExecuteResult:
    """经执行接缝发出操作：executor adapter 请求 → 响应规范化 → 信封包装。

    safety 已由 ToolService 完成；lane 决策（mock/obs/real）在 service 单点
    完成（C6），本函数只面向 executor——审计命名的 mode 归 adapter 所有。
    RequestRefusal（真实 lane 的路径参数/host 拒绝）转 {ok: false, reason}。
    spill 配置透传响应规范化：超限 body 完整落盘（S12 层级 1）。
    extract（_jsonpath 投影）透传响应规范化：全命中替换 body，未命中保 body（自纠面）。
    """
    try:
        resp = executor.request(location, product, api_name, region, params)
    except RequestRefusal as exc:
        return {"ok": False, "reason": str(exc)}
    out = normalize_response(resp, spill, stem=f"{product}-{api_name}",
                             extract=extract)
    out.update({"ok": True, "product": product, "api": api_name})
    return out
