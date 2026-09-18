"""会话身份的 ambient 契约（第 1 层，零内部依赖、不 import mcp）。

HTTP 多会话部署下 PolicyStore 的会话键控与审计归因需要「当前请求属于哪个
MCP 会话」。本模块把会话身份定义为 ambient contextvar：

- **生产 setter 唯一**：SessionScopeMiddleware（SDK ServerMiddleware adapter，
  包裹每条入站消息的 dispatch——消息经内存流跨 task 派发，ASGI 层中间件设
  contextvar 传不到 handler，只有 dispatch 所在会话 task 内的 middleware 可达）。
- **interface 契约**：调用方（PolicyStore 经构造注入的 session_fn、audit）只
  读 current_session_key()；stdio / InMemoryTransport / 装配期恒 None。
- **隐式性补偿**（ADR-0003）：键绑定先于一切 store 调用的 ordering 约束由
  build_app 单点配对保证（middleware 注册与 session_fn 注入同处装配）。

会话键解析梯子单函数收拢 SDK 访问（S17 spike 实测 SDK 2.0.0）：
1. ``ctx.request.headers["mcp-session-id"]``——公开 API；stateful 下 tools/call
   恒带且经 transport 校验（header==会话 id），对触及 store 的请求恰为权威；
2. ``ctx.session._connection.session_id``——SDK 私有兜底腿（initialize 期
   header 缺失时它已有值；SDK 公开化后只需更新本腿）；
3. None——stdio / InMemoryTransport / 无会话身份（modern 单交换协议）。
任一腿异常归一为下一腿 / None，永不抛出。
"""

import contextvars
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

MCP_SESSION_ID_HEADER = "mcp-session-id"

_current_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hwc_mcp_session_key", default=None)


def current_session_key() -> str | None:
    """当前请求的 MCP 会话键；stdio / InMemoryTransport / 装配期恒 None。"""
    return _current_key.get()


@contextmanager
def _bind_session(key: str | None) -> Iterator[None]:
    """在作用域内绑定会话键（异常安全 reset；嵌套时内层胜出）。

    internal seam：生产唯一调用方是 SessionScopeMiddleware，测试经其公共面
    __call__ 进入（内部缝不因测试使用而暴露为公共 interface）。
    """
    token = _current_key.set(key)
    try:
        yield
    finally:
        _current_key.reset(token)


def _session_key_from_headers(headers: Mapping[str, str] | None) -> str | None:
    """从请求头提取 mcp-session-id；None/空映射/空值安全返回 None。

    生产类型为 Starlette Headers（大小写不敏感）；本函数按 Mapping.get 读取，
    大小写归一由生产类型承担。
    """
    if not headers:
        return None
    try:
        value = headers.get(MCP_SESSION_ID_HEADER)
    except Exception:              # 防御：非标准 Mapping 实现永不炸穿中间件
        return None
    return value or None


def _session_key_of(ctx: Any) -> str | None:
    """ServerRequestContext（duck-typed）→ 会话键解析梯子（永不抛出）。"""
    headers = getattr(getattr(ctx, "request", None), "headers", None)
    key = _session_key_from_headers(headers)
    if key:
        return key
    try:
        connection = getattr(getattr(ctx, "session", None), "_connection", None)
        return getattr(connection, "session_id", None) or None
    except Exception:              # 防御：SDK 私有腿任何形态漂移都归 None
        return None


class SessionScopeMiddleware:
    """SDK ServerMiddleware adapter：每条入站消息把会话键装入 ambient contextvar。

    会话身份 seam 的 HTTP 侧 adapter（stdio / InMemory 侧 = None：ctx.request
    缺失 → 梯子归 None，pass-through）。注册为唯一 setter（build_app 单点
    配对）；set/reset 异常安全，notification（call_next 返回 None）同样覆盖。
    """

    async def __call__(self, ctx: Any,
                       call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        with _bind_session(_session_key_of(ctx)):
            return await call_next(ctx)
