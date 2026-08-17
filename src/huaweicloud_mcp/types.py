"""跨模块共享的类型定义。"""

from typing import Any, TypedDict


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
