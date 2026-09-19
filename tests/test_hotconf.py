"""HotFile 单测（S23）：配置文件统一异步热刷新内核。

独立真值：tmp 文件系统状态 + parse 调用计数；stat 注入双替身——
内容哈希 stamp（内容变 ⇒ stamp 变，规避真实 mtime 粒度抖动，
同 test_policy_store.make_stat_fn 手法）与可拨 mtime 时钟（touch 语义）。
后台刷新经 internal seam ``_join_pending`` 同步，不 sleep 轮询。
"""

import logging
import os
import threading

import pytest

from common.hotconf import HotFile


def make_hash_stat_fn():
    """内容哈希 stamp 替身：内容变 ⇒ stamp 变。"""

    def stat_fn(path):
        with open(path, "rb") as f:
            data = f.read()
        return type("StatResult", (), {
            "st_mtime_ns": hash(data) & 0xFFFFFFFFFFFF,
            "st_size": len(data),
            "st_ino": 1,
        })()

    return stat_fn


def make_clock_stat_fn(state):
    """可拨 mtime 时钟替身：mtime 来自 state dict，与内容无关（touch 语义）。"""

    def stat_fn(path):
        st = os.stat(path)  # 缺文件时 FileNotFoundError → OSError 分支
        return type("StatResult", (), {
            "st_mtime_ns": state["mtime"],
            "st_size": st.st_size,
            "st_ino": 1,
        })()

    return stat_fn


def write(path, text):
    path.write_text(text, encoding="utf-8")


# ---------- 构造期急切加载（fail-fast） ----------


def test_initial_load_eager(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn())
    assert hot.get() == "v1"


def test_missing_file_fails_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        HotFile(str(tmp_path / "nope.json"), lambda raw: raw, stat_fn=make_hash_stat_fn())


def test_invalid_json_fails_fast(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, "{broken")
    with pytest.raises(ValueError):
        HotFile(str(p), lambda raw: raw, stat_fn=make_hash_stat_fn())


def test_invalid_schema_fails_fast(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": 1}')

    def parse(raw):
        raise ValueError("bad schema")

    with pytest.raises(ValueError):
        HotFile(str(p), parse, stat_fn=make_hash_stat_fn())


# ---------- 稳态：stamp 未变零重读 ----------


def test_stable_get_no_reread(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    calls = {"n": 0}

    def parse(raw):
        calls["n"] += 1
        return raw["k"]

    hot = HotFile(str(p), parse, stat_fn=make_hash_stat_fn())
    assert calls["n"] == 1
    for _ in range(3):
        assert hot.get() == "v1"
    assert calls["n"] == 1  # stamp 未变：零文件读、零重解析


# ---------- 热重载（异步 stale-until-ready） ----------


def test_hot_reload_on_external_change(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn())
    write(p, '{"k": "v2"}')
    assert hot.get() == "v1"  # 触发请求 stale-until-ready
    hot._join_pending()
    assert hot.get() == "v2"  # 下一次调用见新值


def test_corrupt_file_keeps_last_known_good(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn())
    write(p, "{broken")
    hot.get()
    hot._join_pending()
    assert hot.get() == "v1"


def test_invalid_utf8_keeps_last_known_good(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn())
    p.write_bytes(b'{"k": "\xff\xfe"}')
    hot.get()
    hot._join_pending()
    assert hot.get() == "v1"


def test_watermark_advances_on_parse_failure(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    calls = {"n": 0}

    def parse(raw):
        calls["n"] += 1
        return raw["k"]

    hot = HotFile(str(p), parse, stat_fn=make_hash_stat_fn())
    write(p, "{broken")
    hot.get()
    hot._join_pending()
    assert calls["n"] == 1
    hot.get()  # 同 stamp：不重读坏内容（防刷屏）
    hot._join_pending()
    assert calls["n"] == 1
    write(p, '{"k": "v2"}')
    hot.get()
    hot._join_pending()
    assert calls["n"] == 2  # 文件再次变更：自动重试
    assert hot.get() == "v2"


def test_recovered_file_is_picked_up(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn())
    write(p, "{broken")
    hot.get()
    hot._join_pending()
    write(p, '{"k": "v2"}')
    hot.get()
    hot._join_pending()
    assert hot.get() == "v2"


def test_deleted_file_keeps_last_known_good(tmp_path):
    state = {"mtime": 100}
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_clock_stat_fn(state))
    p.unlink()
    assert hot.get() == "v1"
    assert hot.get() == "v1"


def test_missing_warning_logged_once(tmp_path, caplog):
    state = {"mtime": 100}
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_clock_stat_fn(state))
    p.unlink()
    with caplog.at_level(logging.WARNING, logger="common.hotconf"):
        hot.get()
        hot.get()
        hot.get()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_recreated_file_is_picked_up(tmp_path):
    state = {"mtime": 100}
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_clock_stat_fn(state))
    p.unlink()
    hot.get()
    state["mtime"] = 200
    write(p, '{"k": "v2"}')
    hot.get()
    hot._join_pending()
    assert hot.get() == "v2"


# ---------- 内容哈希防抖（touch） ----------


def test_touch_same_content_no_parse_no_hook(tmp_path):
    state = {"mtime": 100}
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    calls = {"n": 0}
    hooks = []

    def parse(raw):
        calls["n"] += 1
        return raw["k"]

    hot = HotFile(str(p), parse, stat_fn=make_clock_stat_fn(state), on_reload=hooks.append)
    state["mtime"] = 200  # touch：mtime 变内容不变
    hot.get()
    hot._join_pending()
    assert calls["n"] == 1  # 内容哈希确认：不重解析
    assert hooks == []  # 不触发 on_reload
    hot.get()
    assert calls["n"] == 1  # 水位已推进：稳态零 IO


# ---------- 单飞守卫 ----------


def test_single_flight_while_reload_in_progress(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    calls = {"n": 0}
    gate = threading.Event()

    def parse(raw):
        calls["n"] += 1
        if raw["k"] == "v2":
            gate.wait(5)
        return raw["k"]

    hot = HotFile(str(p), parse, stat_fn=make_hash_stat_fn())
    write(p, '{"k": "v2"}')
    assert hot.get() == "v1"  # 触发后台刷新
    for _ in range(5):
        assert hot.get() == "v1"  # pending：单飞，不重复触发
    gate.set()
    hot._join_pending()
    assert calls["n"] == 2
    assert hot.get() == "v2"


# ---------- 完成时 stamp 比对（后台化特有竞态） ----------


def test_completion_stamp_mismatch_discards(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    calls = {"n": 0}
    gate = threading.Event()
    started = threading.Event()

    def parse(raw):
        calls["n"] += 1
        if raw["k"] == "v2":
            started.set()
            gate.wait(5)
        return raw["k"]

    hot = HotFile(str(p), parse, stat_fn=make_hash_stat_fn())
    write(p, '{"k": "v2"}')
    hot.get()  # 触发（observed stamp = v2）
    assert started.wait(5)  # 确保线程已读取 v2 内容
    write(p, '{"k": "v3"}')  # 刷新期间文件又变
    gate.set()
    hot._join_pending()
    assert hot.get() == "v1"  # 弃结果；此 get() 已重触发 v3
    hot._join_pending()
    assert hot.get() == "v3"
    assert calls["n"] == 3


def test_stat_failure_at_completion_discards(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    base = make_hash_stat_fn()
    calls = {"n": 0}

    def stat_fn(path):
        calls["n"] += 1
        if calls["n"] == 3:  # 1=急切加载 2=触发 3=完成时比对
            raise OSError("transient stat failure")
        return base(path)

    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=stat_fn)
    write(p, '{"k": "v2"}')
    hot.get()
    hot._join_pending()
    assert hot.get() == "v1"  # 完成时 stat 失败：弃结果、水位不推进
    hot._join_pending()  # 上一个 get() 已重触发
    assert hot.get() == "v2"


# ---------- on_reload 联动钩子 ----------


def test_on_reload_called_with_new_value(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    seen = []
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn(),
                  on_reload=seen.append)
    write(p, '{"k": "v2"}')
    hot.get()
    hot._join_pending()
    assert seen == ["v2"]


def test_on_reload_exception_contained(tmp_path, caplog):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')

    def hook(_value):
        raise RuntimeError("boom")

    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn(), on_reload=hook)
    write(p, '{"k": "v2"}')
    with caplog.at_level(logging.WARNING, logger="common.hotconf"):
        hot.get()
        hot._join_pending()
    assert hot.get() == "v2"  # 换值已生效，回调异常不外抛
    assert any("on_reload" in r.message for r in caplog.records)


def test_on_reload_not_called_on_parse_failure(tmp_path):
    p = tmp_path / "cfg.json"
    write(p, '{"k": "v1"}')
    hooks = []
    hot = HotFile(str(p), lambda raw: raw["k"], stat_fn=make_hash_stat_fn(),
                  on_reload=hooks.append)
    write(p, "{broken")
    hot.get()
    hot._join_pending()
    assert hooks == []
