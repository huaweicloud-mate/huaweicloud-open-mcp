"""跨模块共享的类型定义。"""

from typing import Any, Literal, TypedDict


class ClientResponse(TypedDict):
    """HTTP 客户端统一响应结构。body 为解析后的 JSON 或原始文本。"""

    status: int
    headers: dict[str, str]
    body: Any


class ExecuteResult(TypedDict, total=False):
    """execute_api 的规范化输出。ok=False 时携带 reason；错误响应携带 error_code/error_msg。"""

    ok: bool
    reason: str
    status: int
    body: Any
    truncated: bool
    error_code: str | None
    error_msg: str | None
    product: str
    api: str


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
