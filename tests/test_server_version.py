"""serverInfo.version 契约：三入口（openapi/data/build_app 混装）恒等于发布版本真值源。

回归背景：MCPServer 构造点曾硬编码 version="0.1.0"，与 __version__（发布版本
单一真值源）脱钩——PyPI a11 与本地构建均自报 0.1.0。真值源 2026-11 起迁至
common（layer 1，依赖单向性要求 serverInfo 消费点不得反向 import 入口包）。
"""

import asyncio

from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from common import __version__
from huaweicloud_open_mcp.deployment import build_app


def _server_version(app) -> str:
    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                return init.server_info.version

    return asyncio.run(_run())


def _make_args():
    import argparse

    ns = argparse.Namespace(mock=True, policy=None, region=None, mock_base=None,
                            mock_passthrough=None, hints=None, audit_file=None)
    return ns


def test_openapi_app_reports_release_version():
    from mcp_openapi.server import build_openapi_app

    assert _server_version(build_openapi_app()) == __version__


def test_data_app_reports_release_version():
    from mcp_data.server import build_data_app

    assert _server_version(build_data_app()) == __version__


def test_composite_app_reports_release_version():
    app = build_app(["openapi", "data"], _make_args())
    assert _server_version(app) == __version__


def test_entry_reexport_keeps_import_compatibility():
    import huaweicloud_open_mcp as entry

    assert entry.__version__ == __version__


def test_release_workflow_version_source_present():
    """release.yml 与 scripts/publish 的 sed 解析目标、hatch version path 必须同为 common/__init__.py。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    workflow = root / ".github" / "workflows" / "release.yml"
    publish = root / "scripts" / "publish"
    pyproject = root / "pyproject.toml"
    for f in (workflow, publish):
        text = f.read_text(encoding="utf-8")
        assert "src/common/__init__.py" in text, f.name
        assert "src/huaweicloud_open_mcp/__init__.py" not in text, f.name
    assert 'path = "src/common/__init__.py"' in pyproject.read_text(encoding="utf-8")


def test_version_is_pep440_clean():
    from packaging.version import Version

    v = Version(__version__)  # 非法 PEP 440 字符串在此抛 InvalidVersion
    assert str(v) == __version__  # 规范形自反：无前导零等形态变体
    assert v.local is None  # 无本地版本段（PyPI 发布形态）
