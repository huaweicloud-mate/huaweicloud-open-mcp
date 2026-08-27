"""跨模块共享的类型定义。"""

from typing import Any, Literal, TypedDict


class ClientResponse(TypedDict):
    """HTTP 客户端统一响应结构。body 为解析后的 JSON 或原始文本。"""

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


class PresignInfo(TypedDict):
    """预签发 URL 信封：客户端直连 OBS 的全部信息，字节流不经过 gateway。"""

    url: str
    method: str
    expires_in: int


class ToolError(TypedDict):
    """元数据工具的统一失败结果。"""

    ok: Literal[False]
    reason: str


# ---------- 元数据工具：内层实体 ----------

class ProductItem(TypedDict):
    product: str
    name: str
    category: str
    api_count: int
    is_global: bool | None
    link: str | None


class ApiItem(TypedDict):
    name: str
    method: str
    summary: str
    tags: str
    info_version: str


class TagGroup(TypedDict):
    tag: str
    api_count: int


class ApiExample(TypedDict):
    description: str | None
    example: Any


# ---------- 元数据工具：结果信封 ----------

class ProductListResult(TypedDict):
    ok: Literal[True]
    total: int
    products: list[ProductItem]


class ProductResult(TypedDict):
    ok: Literal[True]
    product: str
    name: str | None
    category: str | None
    api_count: int
    is_global: bool | None
    link: str | None


class ApiListResult(TypedDict):
    ok: Literal[True]
    product: str
    total: int
    offset: int
    limit: int
    apis: list[ApiItem]
    tag_groups: list[TagGroup]


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
    },
)


class ExamplesResult(TypedDict):
    ok: Literal[True]
    product: str
    api: str
    examples: list[ApiExample]


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


class McpDisconnectResult(TypedDict):
    ok: Literal[True]
    server: str
    released: bool
