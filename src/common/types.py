"""跨模块共享的类型定义。"""

from typing import Any, Literal, Mapping, TypeVar, cast

from typing_extensions import NotRequired, TypedDict


class ClientResponse(TypedDict):
    """HTTP 客户端统一响应结构。

    body 契约：None（空体）| dict/list（解析后 JSON）| str（合法 UTF-8 文本，
    re-encode 与线上字节一致）| bytes（不透明二进制——不可无损 UTF-8 解码或含
    NUL；调用方不得文本化，只能透传给 normalize_response 走占位+落盘）。
    保真不变量（disk == wire）：任何分类下 spill 落盘内容与线上字节逐位一致。
    """

    status: int
    headers: dict[str, str]
    body: Any


class ExecuteResult(TypedDict, total=False):
    """execute_api 的规范化输出。ok=False 时携带 reason；错误响应携带 error_code/error_msg。

    可选字段声明为可空：并非每条路径都会填充（如拒绝时无 status/body），
    MCP SDK 序列化缺失字段为 null，outputSchema 必须允许 null。
    """

    ok: bool
    reason: str | None
    status: int | None
    body: Any
    truncated: bool | None
    error_code: str | None
    error_msg: str | None
    product: str | None
    api: str | None
    headers: dict[str, str] | None
    presign: "PresignInfo | None"
    spill: "SpillInfo | None"  # 超限响应/信封完整落盘（S12）
    extract: "ExtractInfo | None"  # _jsonpath 投影抽取信封（命中/未命中/降级说明）
    granted_rule: str | None  # policy 拒绝经用户 elicitation 确认后授予的规则（最小或产品级）


class SpillInfo(TypedDict):
    """响应落盘信封：完整数据已原子落盘，path 为绝对路径。

    note 为部署感知消费指引（data 模式混装时指引 query_data，否则指引文件读取），
    由 spill 模块恒注入。
    """

    path: str
    format: str   # "json" | "text" | "bin"
    bytes: int
    note: str


class ExtractInfo(TypedDict, total=False):
    """_jsonpath 投影抽取信封：body 即投影值（全命中），本信封补未命中与降级说明。

    extracted 仅部分命中（映射形）时出现——键集完整、未命中键 null；
    misses 为未命中路径的自纠描述；note 为投影替换/载体不适用说明。
    """

    extracted: Any
    misses: list[str]
    note: str


class PresignInfo(TypedDict):
    """预签发 URL 信封：客户端直连 OBS 的全部信息，字节流不经过 gateway。

    signed_content_type / headers 透出签名口径：headers 为须照抄的头域清单
    （锁定类型时含 Content-Type；缺省为空，请求不得携带该头，否则签名失配）。
    note 仅在带 body 的 method（PUT/POST）未锁定 Content-Type 时提示口径。
    """

    url: str
    method: str
    expires_in: int
    signed_content_type: str
    headers: dict[str, str]
    note: NotRequired[str]   # 仅 PUT/POST 未锁定 Content-Type 时注入口径警示
    # GetObject 预签发 HEAD 预检产物（S9f-c）：签发前对象元数据快照，供下载端核对
    expected_size: NotRequired[int]
    expected_etag: NotRequired[str]


class ToolError(TypedDict):
    """元数据工具的统一失败结果。"""

    ok: Literal[False]
    reason: str


# ---------- 元数据工具：内层实体 ----------

class ProductItem(TypedDict):
    product: str
    name: str
    category: str
    is_global: bool | None
    link: str | None
    api_count: int  # v4/products 真实计数（2026-09 恢复：v5 恒 0 时代删除，v4 有真值）
    hints: NotRequired[str]  # 部署侧提示注入（Hints 配置命中产品时附加）


class ApiItem(TypedDict):
    name: str
    method: str
    summary: str
    tags: str
    info_version: str
    hints: NotRequired[str]  # 部署侧提示注入（Hints 配置命中 API 时附加）
    deprecated: NotRequired[bool]   # 废弃治理 annotate 标注（service 层附加）
    replacement: NotRequired[str]   # 废弃替代接口名（仅索引有值时附加）


class TagGroup(TypedDict):
    tag: str
    api_count: int


# ---------- 实体图谱检索：search_apis 信封 ----------

class SearchApiHit(TypedDict):
    name: str
    method: str
    summary: str
    tags: str
    deprecated: NotRequired[bool]     # 废弃治理 annotate 标注（service 层附加）
    replacement: NotRequired[str]     # 废弃替代接口名（仅索引有值时附加）


class SearchRelated(TypedDict):
    product: str
    kind: str
    via: NotRequired[str]


class SearchProductHit(TypedDict):
    product: str
    name: str
    category: str
    is_global: bool | None
    link: str | None
    score: float
    matched_via: list[str]
    apis: list[SearchApiHit]
    related: list[SearchRelated]


class SearchApisResult(TypedDict):
    ok: Literal[True]
    query: str
    total: int
    limit: int
    products: list[SearchProductHit]
    truncated: bool


class ApiExample(TypedDict):
    description: str | None
    example: Any


# ---------- 元数据工具：结果信封 ----------

class ProductListResult(TypedDict):
    ok: Literal[True]
    total: int
    products: list[ProductItem]


class SopIndexEntry(TypedDict):
    """产品级 SOP 索引条目（发现面轻量暴露；description 未配置时省略）。"""

    name: str
    description: NotRequired[str]


class ProductResult(TypedDict):
    ok: Literal[True]
    product: str
    name: str | None
    category: str | None
    is_global: bool | None
    link: str | None
    api_count: int
    hints: NotRequired[str]  # 部署侧提示注入（Hints 配置命中产品时附加）
    sops_index: NotRequired[list[SopIndexEntry]]  # 产品级 SOP 索引（name+description，恒轻量）
    sops: NotRequired[str]  # 部署侧产品级 SOP 全文（include_sops=true 且配置命中时附加，渲染文本）


class ApiListResult(TypedDict):
    ok: Literal[True]
    product: str
    total: int
    offset: int
    limit: int
    apis: list[ApiItem]
    tag_groups: list[TagGroup]
    hints: NotRequired[str]  # 部署侧提示注入（Hints 配置命中产品时附加）
    sops_index: NotRequired[list[SopIndexEntry]]  # 产品级 SOP 索引（仅顶层；条目级不注入）
    sops: NotRequired[str]  # 部署侧产品级 SOP 全文（仅顶层；include_sops opt-in）


# 函数式语法：允许非标识符键（x-constraint）
ApiDetailResult = TypedDict(
    "ApiDetailResult",
    {
        "ok": Literal[True],
        "product": str,
        "api": str,
        "method": str,
        "path": str,
        "summary": Any,
        "description": Any,
        "x-constraint": Any,
        "deprecated": bool,
        "parameters": list[dict[str, Any]],
        "responses": dict[str, dict[str, Any]],
        "definitions": dict[str, Any],
        "hints": NotRequired[str],  # 部署侧提示注入（产品级+API 级合并文案）
    },
)


class ExamplesResult(TypedDict):
    ok: Literal[True]
    product: str
    api: str
    examples: list[ApiExample]


# ---------- data 模式工具：结果信封 ----------

class QueryColumn(TypedDict):
    name: str
    type: str


class QueryDataResult(TypedDict):
    """query_data 的规范化输出：列 schema + JSON-safe 行 + 截断标记。"""

    ok: Literal[True]
    columns: list[QueryColumn]
    rows: list[dict[str, Any]]
    total_rows: int
    returned_rows: int
    truncated: bool
    tables: list[str]


class TransformDataResult(TypedDict):
    """transform_data 的规范化输出：落盘产物元数据 + 小预览（大结果不进上下文）。"""

    ok: Literal[True]
    path: str
    format: str
    rows: int
    bytes: int
    columns: list[QueryColumn]
    preview: list[dict[str, Any]]


# ---------- MCP server 发现工具：内层实体 ----------

class McpServerItem(TypedDict):
    server: str
    name: str
    display_name: str
    category: str
    description: str
    auth: str
    version: str
    endpoint: str


class ServerToolSummary(TypedDict):
    name: str
    description: str
    required: list[str]


# ---------- MCP server 发现工具：结果信封 ----------

class McpServerListResult(TypedDict):
    ok: Literal[True]
    total: int
    servers: list[McpServerItem]


class McpServerResult(TypedDict):
    ok: Literal[True]
    server: str
    name: str
    display_name: str
    category: str
    description: str
    auth: str
    version: str
    endpoint: str


class McpConnectResult(TypedDict, total=False):
    ok: bool
    reason: str | None
    server: str | None
    endpoint: str | None
    protocol_version: str | None
    server_info: dict[str, Any] | None
    granted_rule: str | None


class ServerToolsResult(TypedDict, total=False):
    ok: bool
    reason: str | None
    server: str | None
    total: int
    offset: int
    limit: int
    tools: list[ServerToolSummary] | None


class ServerToolResult(TypedDict, total=False):
    ok: bool
    reason: str | None
    server: str | None
    tool: str | None
    description: str | None
    inputSchema: Any
    truncated: bool | None


class McpCallResult(TypedDict, total=False):
    ok: bool
    reason: str | None
    server: str | None
    tool: str | None
    result: Any
    error_code: str | None
    error_msg: str | None
    truncated: bool | None
    granted_rule: str | None


class McpDisconnectResult(TypedDict):
    ok: Literal[True]
    server: str
    released: bool


# ---------- wire 信封（MCP 注册缝 adapter 类型，S-W，2026-09 起）----------
#
# MCP SDK（mcp/server/mcpserver/utilities/func_metadata.py）依据 @server.tool()
# 的返回注解决定 structuredContent 形状：Union（如 X | ToolError）会被自动包成
# {"result": ...}，而单一 TypedDict 则扁平。为让全部工具的 wire 形状统一为扁平
# 信封（与恒扁平的 content[0].text 一致），注册处改用本节 *Wire 类型（ok: bool
# 必填、业务字段可选，失败臂以值 ok=False + reason 表达）；service 层域类型保持
# ok: Literal[True] 富类型不动。不变量由 tests/test_wire_envelope.py 固化。

class ToolEnvelope(TypedDict):
    """wire 信封基座：ok 恒必填，reason 失败臂可选（值语义，非类型臂）。

    子类必须显式 `total=False`（TypedDict 继承不传播 total），且每个业务字段
    必须声明为可空（`X | None`）——MCP SDK 对单 TypedDict 走 model_dump 会把
    缺失的可选字段填成 null，outputSchema 若不允许 null 则客户端严格校验报错
    （同 ExecuteResult 先例）。失败臂 {ok: false, reason} 因此通过校验且不触发
    isError；成功臂缺失字段在 structuredContent 上为 null（text 不含，键集关系
    见 tests/test_wire_envelope.py）。
    """

    ok: bool
    reason: NotRequired[str | None]


class SearchApisWire(ToolEnvelope, total=False):
    query: str | None
    total: int | None
    limit: int | None
    products: list[SearchProductHit] | None
    truncated: bool | None


class ProductListWire(ToolEnvelope, total=False):
    total: int | None
    products: list[ProductItem] | None


class ProductWire(ToolEnvelope, total=False):
    product: str | None
    name: str | None
    category: str | None
    is_global: bool | None
    link: str | None
    api_count: int | None
    hints: str | None
    sops_index: list[SopIndexEntry] | None
    sops: str | None


class ApiListWire(ToolEnvelope, total=False):
    product: str | None
    total: int | None
    offset: int | None
    limit: int | None
    apis: list[ApiItem] | None
    tag_groups: list[TagGroup] | None
    hints: str | None
    sops_index: list[SopIndexEntry] | None
    sops: str | None


# 函数式语法：允许非标识符键（x-constraint）；ok 保持必填，业务字段 NotRequired。
ApiDetailWire = TypedDict(
    "ApiDetailWire",
    {
        "ok": bool,
        "reason": NotRequired[str | None],
        "product": NotRequired[str | None],
        "api": NotRequired[str | None],
        "method": NotRequired[str | None],
        "path": NotRequired[str | None],
        "summary": NotRequired[Any],
        "description": NotRequired[Any],
        "x-constraint": NotRequired[Any],
        "deprecated": NotRequired[bool | None],
        "parameters": NotRequired[list[dict[str, Any]] | None],
        "responses": NotRequired[dict[str, dict[str, Any]] | None],
        "definitions": NotRequired[dict[str, Any] | None],
        "hints": NotRequired[str | None],
    },
)


class ExamplesWire(ToolEnvelope, total=False):
    product: str | None
    api: str | None
    examples: list[ApiExample] | None


class McpServerListWire(ToolEnvelope, total=False):
    total: int | None
    servers: list[McpServerItem] | None


class McpServerWire(ToolEnvelope, total=False):
    server: str | None
    name: str | None
    display_name: str | None
    category: str | None
    description: str | None
    auth: str | None
    version: str | None
    endpoint: str | None


class McpConnectWire(ToolEnvelope, total=False):
    server: str | None
    endpoint: str | None
    protocol_version: str | None
    server_info: dict[str, Any] | None
    granted_rule: str | None


class ServerToolsWire(ToolEnvelope, total=False):
    server: str | None
    total: int | None
    offset: int | None
    limit: int | None
    tools: list[ServerToolSummary] | None


class ServerToolWire(ToolEnvelope, total=False):
    server: str | None
    tool: str | None
    description: str | None
    inputSchema: Any
    truncated: bool | None


class QueryDataWire(ToolEnvelope, total=False):
    columns: list[QueryColumn] | None
    rows: list[dict[str, Any]] | None
    total_rows: int | None
    returned_rows: int | None
    truncated: bool | None
    tables: list[str] | None


class TransformDataWire(ToolEnvelope, total=False):
    path: str | None
    format: str | None
    rows: int | None
    bytes: int | None
    columns: list[QueryColumn] | None
    preview: list[dict[str, Any]] | None


class ManagePolicyWire(ToolEnvelope, total=False):
    action: str | None
    policy: str | None
    rules: list[dict[str, Any]] | None
    results: list[dict[str, Any]] | None
    scope: str | None


_W = TypeVar("_W", bound=ToolEnvelope)


def as_wire(envelope: Mapping[str, Any], wire: type[_W]) -> _W:
    """注册缝上的类型级 wire 适配：service 域信封 → wire 注解视图。

    运行时二者是同一个扁平 dict（值面零转换）；仅因 TypedDict 值不变性
    （域字段 int 不可赋给 wire 字段 int | None），mypy 需要一个显式转换点。
    wire 参数即注册函数声明的返回类型，保证 outputSchema 由 *Wire 派生。
    """
    return cast(_W, envelope)
