"""common.optconf 单测（S0 扩展）：可选文件配置加载 discipline 的单一实现。

独立真值：tmp 仓库文件系统状态 + 手写解析函数；sealed_configs fixture 密封
仓库 configs/（缺省档解析不受宿主泄入）。
"""

import json

import pytest

from common.hotconf import HotFile
from common.optconf import is_off, load_opt_file, watch_opt_file

# ---------- is_off：off 哨兵判定单点（大小写不敏感） ----------

def test_is_off_case_insensitive_and_empty():
    assert is_off("off") is True
    assert is_off("OFF") is True
    assert is_off(" Off ") is True
    assert is_off("") is True
    assert is_off("  ") is True


def test_is_off_non_off_values():
    assert is_off("on") is False
    assert is_off("auto") is False
    assert is_off("/tmp/spill") is False


# ---------- load_opt_file：None→缺省档 / off→禁用 / 路径→严格加载 ----------

def _parse(data):
    return {"seen": data}


def test_load_opt_file_none_without_default_returns_off():
    off = {"off": True}
    assert load_opt_file(None, parse=_parse, off=off) is off


def test_load_opt_file_none_default_resolved_from_repo_configs(sealed_configs):
    (sealed_configs / "configs").mkdir()
    (sealed_configs / "configs" / "defaults.json").write_text(
        json.dumps({"v": 1}), encoding="utf-8")
    out = load_opt_file(None, parse=_parse, off=None, default_name="defaults.json")
    assert out == {"seen": {"v": 1}}


def test_load_opt_file_none_default_missing_silent_off(sealed_configs):
    off = {"off": True}
    out = load_opt_file(None, parse=_parse, off=off, default_name="nope.json")
    assert out is off   # 缺省档缺失静默（隐式缺省不 fail-fast）


def test_load_opt_file_off_sentinel_case_insensitive():
    off = {"off": True}
    for raw in ("off", "OFF", " Off ", ""):
        assert load_opt_file(raw, parse=_parse, off=off) is off


def test_load_opt_file_explicit_path_strict(sealed_configs):
    p = sealed_configs / "mine.json"
    p.write_text(json.dumps({"v": 2}), encoding="utf-8")
    out = load_opt_file(str(p), parse=_parse, off=None)
    assert out == {"seen": {"v": 2}}


def test_load_opt_file_explicit_bare_name_from_repo_configs(sealed_configs):
    (sealed_configs / "configs").mkdir()
    (sealed_configs / "configs" / "bare.json").write_text(
        json.dumps({"v": 3}), encoding="utf-8")
    out = load_opt_file("bare.json", parse=_parse, off=None)
    assert out == {"seen": {"v": 3}}


def test_load_opt_file_explicit_missing_fail_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_opt_file(str(tmp_path / "absent.json"), parse=_parse, off=None)


def test_load_opt_file_invalid_json_fails_fast(sealed_configs):
    (sealed_configs / "configs").mkdir()
    (sealed_configs / "configs" / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_opt_file("bad.json", parse=_parse, off=None)


def test_load_opt_file_parse_receives_raw_dict(sealed_configs):
    (sealed_configs / "configs").mkdir()
    (sealed_configs / "configs" / "x.json").write_text(
        json.dumps({"k": [1, 2]}), encoding="utf-8")
    seen: list = []
    load_opt_file("x.json", parse=seen.append, off=None)
    assert seen == [{"k": [1, 2]}]


# ---------- watch_opt_file：热刷新孪生（S23） ----------


def test_watch_opt_file_none_without_default_returns_off():
    off = {"off": True}
    assert watch_opt_file(None, parse=_parse, off=off) is off


def test_watch_opt_file_none_default_missing_silent_off(sealed_configs):
    off = {"off": True}
    out = watch_opt_file(None, parse=_parse, off=off, default_name="nope.json")
    assert out is off  # 启动缺失 → 静态 off，不追踪后出现的文件


def test_watch_opt_file_off_sentinel_static(sealed_configs):
    off = {"off": True}
    for raw in ("off", "OFF", ""):
        assert watch_opt_file(raw, parse=_parse, off=off) is off


def test_watch_opt_file_none_default_present_returns_hot_file(sealed_configs):
    (sealed_configs / "configs").mkdir()
    (sealed_configs / "configs" / "defaults.json").write_text(
        json.dumps({"v": 1}), encoding="utf-8")
    out = watch_opt_file(None, parse=_parse, off=None, default_name="defaults.json")
    assert isinstance(out, HotFile)
    assert out.get() == {"seen": {"v": 1}}


def test_watch_opt_file_hot_branch_follows_changes(sealed_configs):
    p = sealed_configs / "mine.json"
    p.write_text(json.dumps({"v": 1}), encoding="utf-8")
    out = watch_opt_file(str(p), parse=_parse, off=None)
    assert isinstance(out, HotFile)
    assert out.get() == {"seen": {"v": 1}}
    p.write_text(json.dumps({"v": 2}), encoding="utf-8")
    out.get()
    out._join_pending()
    assert out.get() == {"seen": {"v": 2}}  # 运行期跟随文件


def test_watch_opt_file_explicit_missing_fail_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        watch_opt_file(str(tmp_path / "absent.json"), parse=_parse, off=None)


def test_watch_opt_file_hot_branch_eager_fail_fast(sealed_configs):
    (sealed_configs / "configs").mkdir()
    (sealed_configs / "configs" / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        watch_opt_file("bad.json", parse=_parse, off=None)
