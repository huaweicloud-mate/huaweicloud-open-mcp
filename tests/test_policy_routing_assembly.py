"""缺省姿态路由装配测试（修订方案 D4'/D5'，S21 装配面扩展）。

三种姿态经 build_app 单点构造（构造单点红线：server.py 不再自建 store），
全部断言走可观测面（InMemoryTransport 工具面 + caplog 启动日志三态）：
- explicit：显式 --policy 恒优先，文件缺失启动即崩（fail-fast 不削弱）；
- default：未配置时路由随包缺省姿态文件——写拒绝 + 确认授予流开箱可用；
- none：未配置且缺省文件缺失（sealed 空仓库）——今日红线逐字保留。

F4 空串档与 F8 data 模式急切构造保留一并钉住。policy 拒绝先于元数据拉取
（廉价拒绝），被拒路径无需网络打桩；放行路径经 fetch_json=None 桩推进到
元数据层，以「非 policy 拒绝形态」区分放行。
"""
import argparse
import json
import logging

import pytest

from common.sessions import SessionScopeMiddleware
from huaweicloud_open_mcp.deployment import build_app


def make_args(**overrides):
    ns = argparse.Namespace(mock=True, policy=None, region=None, mock_base=None,
                            mock_passthrough=None, hints=None, audit_file=None)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _explicit_policy(tmp_path, lines=("ECS:List*=allow", "*=deny")):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(list(lines)), encoding="utf-8")
    return str(path)


def _call_tool(app, name, arguments):
    import asyncio

    from mcp import ClientSession
    from mcp.client._memory import InMemoryTransport

    async def _run():
        async with InMemoryTransport(app) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool(name, arguments)
                return json.loads(result.content[0].text)

    return asyncio.run(_run())


DEPLOY_LOGGER = "huaweicloud_open_mcp.deployment"


# ---------- explicit：恒优先 + fail-fast 不削弱 ----------

def test_explicit_policy_wins_over_default(tmp_path, caplog):
    """显式文件恒优先：list 可见显式规则，启动日志 policy=configured。"""
    path = _explicit_policy(tmp_path)
    with caplog.at_level(logging.INFO, logger=DEPLOY_LOGGER):
        app = build_app(["openapi"], make_args(policy=path), env={})
    out = _call_tool(app, "manage_policy", {"action": "list"})
    assert out["ok"] is True
    assert "ECS:List*=allow" in [r["line"] for r in out["rules"]]
    assert any("policy=configured" in r.message for r in caplog.records)


def test_explicit_missing_file_fails_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_app(["openapi"], make_args(policy=str(tmp_path / "nope.json")), env={})


def test_data_only_explicit_missing_file_still_fails_fast(tmp_path):
    """F8 撤回钉：explicit 分支保持今日急切构造——纯 data 模式传坏
    --policy 依然启动即崩（配置校验不因缺省姿态引入而削弱）。"""
    with pytest.raises(FileNotFoundError):
        build_app(["data"], make_args(policy=str(tmp_path / "nope.json")), env={})


# ---------- default：随包缺省姿态路由（真仓库，未密封） ----------

def test_default_posture_routes_shipped_file(caplog, monkeypatch):
    """未配置 --policy 时路由随包缺省姿态文件：只读接口 policy 放行
    （推进到元数据层，非 policy 拒绝形态），写接口被 policy 拒绝。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    monkeypatch.setattr("common.http.fetch_json_retry", lambda *a, **k: None)
    monkeypatch.setattr("common.http.fetch_json_429", lambda *a, **k: None)
    with caplog.at_level(logging.INFO, logger=DEPLOY_LOGGER):
        app = build_app(["openapi"], make_args(), env={})
    out = _call_tool(app, "manage_policy", {"action": "list"})
    assert out["ok"] is True
    lines = [r["line"] for r in out["rules"]]
    assert "*:List*=allow" in lines and "*=deny" in lines   # 随包姿态在位
    assert "AgentIdentity:*=deny" in lines                   # 敏感脊柱在位

    denied = _call_tool(app, "execute_api",
                        {"product": "ECS", "api": "DeleteServers"})
    assert denied["ok"] is False
    assert "safety policy 拒绝执行 ECS:DeleteServers" in denied["reason"]

    allowed = _call_tool(app, "execute_api",
                         {"product": "ECS", "api": "ListServersDetails",
                          "params": {}})
    assert allowed["ok"] is False                            # 元数据桩错误
    assert "safety policy 拒绝" not in (allowed["reason"] or "")  # policy 放行

    assert any("policy=default" in r.message for r in caplog.records)
    assert any("缺省姿态文件" in r.message and "permanent" in r.message
               for r in caplog.records)                       # D5/D6' 披露


def test_default_posture_grant_channel_out_of_box(monkeypatch):
    """缺省姿态下确认授予流开箱可用：off 档 grant=api_session 断言授予
    （会话档），同次调用内放行并推进到元数据层（非 policy 拒绝形态）。"""
    monkeypatch.setattr("common.http.fetch_json", lambda *a, **k: None)
    monkeypatch.setattr("common.http.fetch_json_retry", lambda *a, **k: None)
    monkeypatch.setattr("common.http.fetch_json_429", lambda *a, **k: None)
    app = build_app(["openapi"], make_args(), env={})
    out = _call_tool(app, "execute_api",
                     {"product": "ECS", "api": "DeleteServers",
                      "params": {}, "grant": "api_session"})
    assert out["ok"] is False                                # 元数据桩错误
    assert "safety policy 拒绝" not in (out["reason"] or "")  # 授予后放行


# ---------- none：sealed 空仓库，今日红线逐字保留 ----------

def test_none_posture_in_sealed_repo(sealed_configs, caplog):
    with caplog.at_level(logging.INFO, logger=DEPLOY_LOGGER):
        app = build_app(["openapi"], make_args(), env={})
    out = _call_tool(app, "execute_api",
                     {"product": "ECS", "api": "ListServersDetails"})
    assert out["ok"] is False
    assert "未配置" in out["reason"]                          # 读也拒（今日红线）
    listed = _call_tool(app, "manage_policy", {"action": "list"})
    assert listed["ok"] is False and "未配置" in listed["reason"]
    assert any("policy=MISSING" in r.message for r in caplog.records)
    assert any("缺省姿态文件缺失" in r.message for r in caplog.records)


def test_data_only_without_policy_builds_without_store(sealed_configs):
    """data 模式无 policy 语义：缺省路由不消费（今日行为），装配成功。"""
    app = build_app(["data"], make_args(), env={})
    assert app is not None


# ---------- F4：空串披露 ----------

def test_empty_string_policy_warns_and_routes_default(caplog):
    with caplog.at_level(logging.WARNING, logger=DEPLOY_LOGGER):
        app = build_app(["openapi"], make_args(policy=""), env={})
    assert any("空串" in r.message for r in caplog.records)
    out = _call_tool(app, "manage_policy", {"action": "list"})
    assert out["ok"] is True                                   # 缺省姿态路由


def test_empty_string_policy_warns_and_none_in_sealed_repo(sealed_configs, caplog):
    with caplog.at_level(logging.WARNING, logger=DEPLOY_LOGGER):
        app = build_app(["openapi"], make_args(policy=" "), env={})
    assert any("空串" in r.message for r in caplog.records)
    out = _call_tool(app, "manage_policy", {"action": "list"})
    assert out["ok"] is False and "未配置" in out["reason"]     # 红线角落


def test_env_empty_string_disclosed_via_build_app(sealed_configs, caplog):
    with caplog.at_level(logging.WARNING, logger=DEPLOY_LOGGER):
        build_app(["openapi"], make_args(),
                  env={"HUAWEICLOUD_MCP_POLICY_FILE": ""})
    assert any("空串" in r.message for r in caplog.records)


# ---------- 结构回归：middleware 单点配对不因路由重构漂移 ----------

def test_middleware_registered_in_all_postures(tmp_path):
    app = build_app(["openapi"], make_args(policy=_explicit_policy(tmp_path)), env={})
    mws = [m for m in app.middleware if isinstance(m, SessionScopeMiddleware)]
    assert len(mws) == 1
    app2 = build_app(["openapi"], make_args(), env={})
    mws2 = [m for m in app2.middleware if isinstance(m, SessionScopeMiddleware)]
    assert len(mws2) == 1
