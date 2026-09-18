"""S17：会话身份 ambient 契约（common/sessions.py）。

纯函数单测：解析梯子矩阵（header 优先 / 私有兜底 / 归 None）、middleware
set/reset 异常安全、嵌套内层胜出、exception 穿透后 reset。独立真值为手写
fake ctx 字面量；真 SDK 全链行为归 S22 e2e。
"""

import asyncio

import pytest

from common.sessions import (
    MCP_SESSION_ID_HEADER,
    SessionScopeMiddleware,
    _bind_session,
    _session_key_from_headers,
    _session_key_of,
    current_session_key,
)


class _Ctx:
    """duck-typed ServerRequestContext 替身：按需挂 request/session。"""

    def __init__(self, *, request=None, session=None):
        self.request = request
        self.session = session


class _Req:
    def __init__(self, headers=None):
        self.headers = headers


class _Session:
    def __init__(self, conn=None):
        self._connection = conn


class _Conn:
    def __init__(self, session_id=None):
        self.session_id = session_id


def _ctx_with_header(sid: str | None) -> _Ctx:
    headers = {MCP_SESSION_ID_HEADER: sid} if sid else {}
    return _Ctx(request=_Req(headers), session=_Session(_Conn("conn-sid")))


# ---------- current_session_key / _bind_session ----------

def test_current_key_default_none():
    assert current_session_key() is None


def test_bind_sets_and_resets():
    with _bind_session("s1"):
        assert current_session_key() == "s1"
    assert current_session_key() is None


def test_bind_nesting_inner_wins():
    with _bind_session("outer"):
        with _bind_session("inner"):
            assert current_session_key() == "inner"
        assert current_session_key() == "outer"
    assert current_session_key() is None


def test_bind_resets_on_exception():
    with pytest.raises(RuntimeError), _bind_session("s1"):
        raise RuntimeError("boom")
    assert current_session_key() is None


# ---------- _session_key_from_headers ----------

def test_headers_none_and_empty():
    assert _session_key_from_headers(None) is None
    assert _session_key_from_headers({}) is None


def test_headers_present():
    assert _session_key_from_headers({MCP_SESSION_ID_HEADER: "abc"}) == "abc"


def test_headers_empty_value_is_none():
    assert _session_key_from_headers({MCP_SESSION_ID_HEADER: ""}) is None


def test_headers_hostile_mapping_never_raises():
    class Hostile:
        def get(self, _key: str) -> str:
            raise RuntimeError("boom")

    assert _session_key_from_headers(Hostile()) is None  # type: ignore[arg-type]


# ---------- _session_key_of 梯子 ----------

def test_ladder_header_wins_over_conn():
    ctx = _ctx_with_header("header-sid")
    assert _session_key_of(ctx) == "header-sid"


def test_ladder_falls_to_conn_when_header_missing():
    ctx = _Ctx(request=_Req({}), session=_Session(_Conn("conn-sid")))
    assert _session_key_of(ctx) == "conn-sid"


def test_ladder_none_when_both_missing():
    assert _session_key_of(_Ctx()) is None
    assert _session_key_of(_Ctx(request=_Req({}), session=_Session(_Conn()))) is None


def test_ladder_bare_object_is_none():
    assert _session_key_of(object()) is None
    assert _session_key_of(None) is None


def test_ladder_hostile_session_never_raises():
    class Hostile:
        @property
        def session(self) -> object:
            raise RuntimeError("boom")

    assert _session_key_of(Hostile()) is None


def test_ladder_none_headers_attr_falls_to_conn():
    ctx = _Ctx(request=_Req(None), session=_Session(_Conn("conn-sid")))
    assert _session_key_of(ctx) == "conn-sid"


# ---------- SessionScopeMiddleware（公共面） ----------

def test_middleware_binds_key_during_call_next():
    seen: dict = {}

    async def call_next(ctx):
        seen["key"] = current_session_key()
        return "ok"

    mw = SessionScopeMiddleware()
    import asyncio

    result = asyncio.run(mw(_ctx_with_header("s1"), call_next))
    assert result == "ok"
    assert seen["key"] == "s1"
    assert current_session_key() is None


def test_middleware_notification_none_result_still_bound_and_reset():
    seen: dict = {}

    async def call_next(ctx):
        seen["key"] = current_session_key()
        return None

    mw = SessionScopeMiddleware()
    assert asyncio.run(mw(_ctx_with_header("s2"), call_next)) is None
    assert seen["key"] == "s2"
    assert current_session_key() is None


def test_middleware_resets_when_call_next_raises():
    async def call_next(ctx):
        raise RuntimeError("boom")

    mw = SessionScopeMiddleware()
    with pytest.raises(RuntimeError):
        asyncio.run(mw(_ctx_with_header("s3"), call_next))
    assert current_session_key() is None


def test_middleware_stdio_like_ctx_binds_none():
    seen: dict = {}

    async def call_next(ctx):
        seen["key"] = current_session_key()
        return "ok"

    mw = SessionScopeMiddleware()
    asyncio.run(mw(_Ctx(), call_next))
    assert seen["key"] is None
