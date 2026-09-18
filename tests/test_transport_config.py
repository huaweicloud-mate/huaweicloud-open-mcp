"""S19：transport 归一与 run 分派（ADR-0003 决策 4/5）。

纯函数矩阵：argv>env>默认回退链、streamable-http 别名归一、非法值 fail-fast
（有意区别于 parse_modes 的宽容回退——传输拓扑错误必须响）、裸 Namespace
getattr 安全。run_transport 用记录型 app 替身断言 stdio/http 两分派参数；
stdio 缺省路径与现状逐字节一致。
"""

import argparse
import json

import pytest

from common.deployment import (
    ENV_HTTP_HOST,
    ENV_HTTP_PATH,
    ENV_HTTP_PORT,
    ENV_TRANSPORT,
    Deployment,
    resolve_deployment,
)
from huaweicloud_open_mcp.cli import run_transport


def _args(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _bare() -> argparse.Namespace:
    """裸 Namespace（test_composite 同法：不含任何新字段）。"""
    return argparse.Namespace(mode=None, mock=None, policy=None)


# ---------- parse_transport / 归一矩阵 ----------

def test_transport_defaults_stdio():
    dep = resolve_deployment(_bare())
    assert dep.transport == "stdio"
    assert dep.http_host == "127.0.0.1"
    assert dep.http_port == 8000
    assert dep.http_path == "/mcp"


def test_transport_aliases_normalize_to_http():
    for raw in ("http", "streamable-http", "streamable_http", "HTTP", " Streamable-HTTP "):
        assert resolve_deployment(_args(transport=raw)).transport == "http"
    assert resolve_deployment(_args(transport="stdio")).transport == "stdio"


def test_transport_env_fallback_and_precedence():
    env = {ENV_TRANSPORT: "http"}
    assert resolve_deployment(_bare(), env).transport == "http"
    # argv 优先于 env
    assert resolve_deployment(_args(transport="stdio"), env).transport == "stdio"


def test_transport_invalid_fails_fast():
    with pytest.raises(ValueError, match="transport"):
        resolve_deployment(_args(transport="grpc"))
    with pytest.raises(ValueError, match="transport"):
        resolve_deployment(_bare(), {ENV_TRANSPORT: "sse"})


# ---------- endpoint 三参数 ----------

def test_http_endpoint_argv_over_env():
    dep = resolve_deployment(
        _args(http_host="0.0.0.0", http_port=9000, http_path="/gw/mcp"),
        {ENV_HTTP_HOST: "127.0.0.1", ENV_HTTP_PORT: "1", ENV_HTTP_PATH: "/x"})
    assert (dep.http_host, dep.http_port, dep.http_path) == ("0.0.0.0", 9000, "/gw/mcp")


def test_http_port_env_string_and_bounds():
    assert resolve_deployment(_bare(), {ENV_HTTP_PORT: "9911"}).http_port == 9911
    for bad in ("0", "65536", "abc", ""):
        env = {ENV_HTTP_PORT: bad} if bad else {}
        if bad:
            with pytest.raises(ValueError, match="端口"):
                resolve_deployment(_bare(), env)
    with pytest.raises(ValueError, match="端口"):
        resolve_deployment(_args(http_port=70000))
    with pytest.raises(ValueError, match="端口"):
        resolve_deployment(_args(http_port=0))


def test_http_path_must_start_with_slash():
    with pytest.raises(ValueError, match="http-path"):
        resolve_deployment(_args(http_path="mcp"))
    assert resolve_deployment(_bare(), {ENV_HTTP_PATH: "/gateway/mcp"}).http_path == "/gateway/mcp"


def test_transport_fields_on_deployment_value_object():
    dep = Deployment()
    assert dep.transport == "stdio" and dep.http_port == 8000  # 缺省不破坏既有消费方


# ---------- run_transport 分派 ----------

class _RecordingApp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(self, transport: str, **kwargs: object) -> None:
        self.calls.append((transport, kwargs))


def test_run_transport_stdio_default_byte_path():
    app = _RecordingApp()
    run_transport(app, _bare())  # type: ignore[arg-type]
    assert app.calls == [("stdio", {})]


def test_run_transport_http_dispatch_params():
    app = _RecordingApp()
    run_transport(app, _args(transport="http", http_host="0.0.0.0",
                             http_port=9000, http_path="/gw/mcp"))  # type: ignore[arg-type]
    assert app.calls == [("streamable-http",
                          {"host": "0.0.0.0", "port": 9000,
                           "streamable_http_path": "/gw/mcp"})]


def test_run_transport_env_only_http():
    app = _RecordingApp()
    run_transport(app, _bare(), {ENV_TRANSPORT: "streamable-http"})  # type: ignore[arg-type]
    assert app.calls[0][0] == "streamable-http"
    assert app.calls[0][1] == {"host": "127.0.0.1", "port": 8000,
                               "streamable_http_path": "/mcp"}


def test_resolution_is_pure_and_repeatable():
    """同一 args 两次解析结果一致（cli 与 build_app 各自 resolve 的幂等前提）。"""
    args = _args(transport="http", http_port=9911)
    a, b = resolve_deployment(args, {ENV_TRANSPORT: "stdio"}), resolve_deployment(args, {ENV_TRANSPORT: "stdio"})
    assert json.dumps(a.transport) == json.dumps(b.transport)
    assert a.http_port == b.http_port == 9911
