"""spill 模块纯函数单元测试（S12）：落盘原语 + 通用信封守卫。

独立真值：tmp_path 文件系统回读（内容与原始对象逐一比对），非同义反复。
"""

import json
import os

import pytest

from mcp_openapi.spill import MAX_RESPONSE_CHARS, SpillConfig, guard_result, spill_body


@pytest.fixture
def cfg(tmp_path):
    return SpillConfig(dir=tmp_path, data_enabled=False)


@pytest.fixture
def cfg_data(tmp_path):
    return SpillConfig(dir=tmp_path, data_enabled=True)


# ---------- spill_body：落盘原语 ----------

def test_spill_body_json_fidelity(cfg, tmp_path):
    raw = {"servers": [{"id": "s1", "name": "vm-1"}], "total": 1}
    info = spill_body(raw, cfg=cfg, stem="execute_api-ECS-ListServers")
    assert info is not None
    assert info["format"] == "json"
    assert os.path.isfile(info["path"])
    with open(info["path"], encoding="utf-8") as f:
        assert json.load(f) == raw
    assert info["bytes"] == os.path.getsize(info["path"])
    assert info["bytes"] > 0


def test_spill_body_text_fidelity(cfg):
    raw = "plain text body" * 10
    info = spill_body(raw, cfg=cfg, stem="x")
    assert info is not None
    assert info["format"] == "text"
    with open(info["path"], encoding="utf-8") as f:
        assert f.read() == raw


def test_spill_body_bytes_fidelity(cfg):
    raw = b"\x00\x01binary"
    info = spill_body(raw, cfg=cfg, stem="x")
    assert info is not None
    assert info["format"] == "bin"
    with open(info["path"], "rb") as f:
        assert f.read() == raw


def test_spill_body_unique_names(cfg):
    a = spill_body({"n": 1}, cfg=cfg, stem="op")
    b = spill_body({"n": 2}, cfg=cfg, stem="op")
    assert a is not None and b is not None
    assert a["path"] != b["path"]
    assert os.path.isfile(a["path"]) and os.path.isfile(b["path"])


def test_spill_body_no_tmp_leftovers(cfg, tmp_path):
    spill_body({"n": 1}, cfg=cfg, stem="op")
    leftovers = [n for n in os.listdir(tmp_path) if ".tmp-part" in n]
    assert leftovers == []


def test_spill_body_sanitized_stem(cfg, tmp_path):
    info = spill_body({"n": 1}, cfg=cfg, stem="ECS/Weird Name::Op")
    assert info is not None
    name = os.path.basename(info["path"])
    assert name.startswith("ECS_Weird_Name_Op-")


def test_spill_body_note_deployment_aware(cfg, cfg_data):
    no_data = spill_body({"n": 1}, cfg=cfg, stem="op")
    with_data = spill_body({"n": 1}, cfg=cfg_data, stem="op")
    assert no_data is not None and with_data is not None
    assert "query_data" not in no_data["note"]
    assert "query_data" in with_data["note"]


def test_spill_body_unwritable_dir_returns_none(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    cfg = SpillConfig(dir=blocker / "sub", data_enabled=False)
    assert spill_body({"n": 1}, cfg=cfg, stem="op") is None


def test_spill_body_creates_missing_dir(tmp_path):
    cfg = SpillConfig(dir=tmp_path / "deep" / "nest", data_enabled=False)
    info = spill_body({"n": 1}, cfg=cfg, stem="op")
    assert info is not None and os.path.isfile(info["path"])


# ---------- guard_result：通用信封守卫 ----------

def test_guard_result_small_result_identity(cfg):
    result = {"ok": True, "product": "ECS", "apis": [{"name": "a"}]}
    assert guard_result(result, cfg=cfg, stem="list_apis") is result


def test_guard_result_refusal_never_spilled(cfg, tmp_path):
    result = {"ok": False, "reason": "x" * 10_000}
    assert guard_result(result, cfg=cfg, stem="t") is result
    assert os.listdir(tmp_path) == []


def test_guard_result_already_spilled_noop(cfg):
    result = {"ok": True, "status": 200, "body": {"d": "x" * 10_000},
              "spill": {"path": "/tmp/x.json", "format": "json", "bytes": 1}}
    assert guard_result(result, cfg=cfg, stem="t") is result


def _tiny_cfg(tmp_path, budget=500):
    return SpillConfig(dir=tmp_path, data_enabled=False, budget=budget)


def test_guard_result_oversize_spills_full_envelope(tmp_path):
    cfg = _tiny_cfg(tmp_path)
    definitions = {"big": {"props": "p" * 2000}}
    result = {"ok": True, "product": "ECS", "api": "X", "definitions": definitions}
    out = guard_result(result, cfg=cfg, stem="get_api")
    assert out is not result
    # 完整原始信封落盘（收缩前）
    spill_path = out["spill"]["path"]
    with open(spill_path, encoding="utf-8") as f:
        assert json.load(f) == result
    # 重字段以类型兼容占位替换
    assert out["definitions"] == {"_truncated": True,
                                  "_original_size_bytes": len(json.dumps(definitions,
                                                                        ensure_ascii=False))}
    assert out["truncated"] is True
    assert out["ok"] is True
    assert out["product"] == "ECS"


def test_guard_result_list_field_stubbed_to_empty(tmp_path):
    cfg = _tiny_cfg(tmp_path)
    apis = [{"name": f"op{i}", "summary": "s" * 50} for i in range(50)]
    result = {"ok": True, "product": "ECS", "apis": apis, "total": 50}
    out = guard_result(result, cfg=cfg, stem="list_apis")
    assert out["apis"] == []
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == result
    assert out["total"] == 50


def test_guard_result_shrinks_until_under_budget(tmp_path):
    # 预算须容纳 spill 信封自身体积（收缩在不含 spill 的口径上度量）
    cfg = _tiny_cfg(tmp_path, budget=600)
    result = {"ok": True, "a": {"v": "x" * 800}, "b": {"v": "y" * 800},
              "c": {"v": "z" * 100}}
    out = guard_result(result, cfg=cfg, stem="t")
    assert out["a"]["_truncated"] is True
    assert out["b"]["_truncated"] is True
    assert out["c"] == {"v": "z" * 100}   # 小字段保留
    assert len(json.dumps(out, ensure_ascii=False)) <= cfg.budget


def test_guard_result_never_stubs_ok_or_scalars(tmp_path):
    cfg = _tiny_cfg(tmp_path)
    result = {"ok": True, "total": 3, "big": {"v": "x" * 2000}}
    out = guard_result(result, cfg=cfg, stem="t")
    assert out["ok"] is True
    assert out["total"] == 3


def test_guard_result_all_scalars_oversize_keeps_honest(tmp_path):
    # 无容器字段可收缩：原样保留 + spill + truncated（不再有真值丢失）
    cfg = _tiny_cfg(tmp_path, budget=100)
    result = {"ok": True, "reason": "r" * 500}
    out = guard_result(result, cfg=cfg, stem="t")
    assert out["reason"] == "r" * 500
    assert out["truncated"] is True
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == result


# ---------- 常量口径 ----------

def test_budget_constant_shared_with_execute():
    from mcp_openapi import execute
    assert MAX_RESPONSE_CHARS == 200_000
    assert execute.MAX_RESPONSE_CHARS is MAX_RESPONSE_CHARS


# ---------- parse_spill_config（装配解析，S12） ----------

def test_parse_spill_config_default_when_absent():
    from mcp_openapi.spill import parse_spill_config
    cfg = parse_spill_config(None)
    assert cfg is not None
    assert "hwc-mcp-spill" in str(cfg.dir)


def test_parse_spill_config_off_disabled():
    from mcp_openapi.spill import parse_spill_config
    assert parse_spill_config("") is None
    assert parse_spill_config("off") is None


def test_parse_spill_config_custom_path(tmp_path):
    from mcp_openapi.spill import parse_spill_config
    cfg = parse_spill_config(str(tmp_path / "spill"), data_enabled=True)
    assert cfg is not None
    assert cfg.dir == tmp_path / "spill"
    assert cfg.data_enabled is True


def test_guard_result_truncated_without_spill_noop(tmp_path):
    """lane 已作出不落盘决策的信封（_spill=false 或落盘失败）恒不收缩——回落纯截断口径。"""
    result = {"ok": True, "status": 200, "body": "y" * 10_000, "truncated": True}
    assert guard_result(result, cfg=_tiny_cfg(tmp_path), stem="t") is result
