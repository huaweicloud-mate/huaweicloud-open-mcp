"""PolicyStore 单测（S2b）：热重载 / 双向同步 / 原子写 / 回退 / 不可变。

独立真值：直接回读磁盘原始内容 + safety.policy.parse_policy 交叉验证，
不用被测代码自身的输出断言自身。stat 注入为「文件内容哈希」时间戳，
规避真实 mtime 秒级粒度导致的测试抖动。
"""

import json

import pytest

from safety import policy
from safety.policy_store import PolicyStore


def make_stat_fn():
    """以文件内容哈希作为 mtime_ns 的确定性 stat 替身：内容变 ⇒ stamp 变。"""
    def stat_fn(path):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise
        size = len(data)
        digest = hash(data) & 0xFFFFFFFFFFFF
        return type("StatResult", (), {
            "st_mtime_ns": digest, "st_size": size, "st_ino": 1})()
    return stat_fn


def make_store(path):
    return PolicyStore(str(path), stat_fn=make_stat_fn())


def read_disk(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def semantic_lines(path):
    """磁盘策略里的语义规则行（不含注释/空行），用于插入位置断言。"""
    raw = read_disk(path)
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            lines = [str(x) for x in data]
        else:
            lines = raw.splitlines()
    except json.JSONDecodeError:
        lines = raw.splitlines()
    return [ln.strip() for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]


# ---------- 初始加载 ----------

def test_initial_load_json(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('["ECS:*Show*=allow", "*=deny"]', encoding="utf-8")
    store = make_store(p)
    assert policy.evaluate(store.rules(), "ECS", "ShowServers") is True
    assert policy.evaluate(store.rules(), "ECS", "DeleteServers") is False


def test_initial_load_text_with_comments(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("# 白名单\nECS:*List*=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    assert len(store.rules()) == 2
    assert "# 白名单" in store.text()


def test_missing_file_fails_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        make_store(tmp_path / "nope.json")


def test_malformed_rule_fails_fast(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('["ECS:ListServers"]', encoding="utf-8")
    with pytest.raises(ValueError):
        make_store(p)


# ---------- 未配置（path=None）不可变 ----------

def test_unconfigured_rules_empty():
    store = PolicyStore(None)
    assert store.rules() == ()
    assert store.text() == ""


def test_unconfigured_mutations_rejected():
    store = PolicyStore(None)
    out = store.add_rule("ECS:*=allow")
    assert out.ok is False
    assert "--policy" in (out.reason or "")
    out2 = store.remove_rule("ECS:*=allow")
    assert out2.ok is False


# ---------- 热重载（file → memory） ----------

def test_hot_reload_on_external_change(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('["OBS:*List*=allow", "*=deny"]', encoding="utf-8")
    store = make_store(p)
    assert policy.evaluate(store.rules(), "OBS", "PutObject") is False

    # 外部直接改文件（模拟编辑器保存），不重建 store、不重启
    p.write_text('["OBS:*List*=allow", "OBS:PutObject=allow", "*=deny"]', encoding="utf-8")
    assert policy.evaluate(store.rules(), "OBS", "PutObject") is True


def test_no_reread_when_file_unchanged(tmp_path, monkeypatch):
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    stat_fn = make_stat_fn()
    monkeypatch.setattr("safety.policy_store.os.stat", stat_fn)
    calls = {"n": 0}
    orig_open = open

    def counting_open(*a, **kw):
        if a and str(a[0]) == str(p):
            calls["n"] += 1
        return orig_open(*a, **kw)

    store = PolicyStore(str(p), stat_fn=stat_fn)
    base = calls["n"]
    store.rules()
    store.rules()
    store.rules()
    assert calls["n"] == base  # stamp 未变则不再读盘解析


# ---------- 双向同步（memory → file） ----------

def test_add_rule_persists_to_disk_and_takes_effect(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('["OBS:*List*=allow", "*=deny"]', encoding="utf-8")
    store = make_store(p)

    res = store.add_rule("OBS:GetObject=allow")
    assert res.ok is True

    disk = json.loads(read_disk(p))
    assert "OBS:GetObject=allow" in disk          # 文件已持久化
    assert policy.evaluate(store.rules(), "OBS", "GetObject") is True  # 内存即时生效
    # 新开一个 store 读同一文件也应一致（真值在文件）
    other = make_store(p)
    assert policy.evaluate(other.rules(), "OBS", "GetObject") is True


def test_add_rule_inserted_before_catchall_deny(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(
        ["#", "*=deny"], ensure_ascii=False), encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:*List*=allow")
    lines = semantic_lines(p)
    assert lines == ["ECS:*List*=allow", "*=deny"]  # 顺序敏感：兜底行之前


def test_add_invalid_rule_rejected_disk_untouched(tmp_path):
    p = tmp_path / "policy.json"
    before = '["OBS:*List*=allow", "*=deny"]'
    p.write_text(before, encoding="utf-8")
    store = make_store(p)
    res = store.add_rule("GetObject=allow")  # 缺 product 前缀
    assert res.ok is False
    assert "格式" in (res.reason or "")
    assert read_disk(p) == before  # 字节不变


def test_add_duplicate_idempotent(tmp_path):
    p = tmp_path / "policy.json"
    before = '["OBS:GetObject=allow", "*=deny"]'
    p.write_text(before, encoding="utf-8")
    store = make_store(p)
    res = store.add_rule("obs:getobject=allow")  # 大小写不敏感的语义匹配
    assert res.ok is True
    assert read_disk(p) == before  # 幂等：文件未被重写


def test_remove_rule_updates_disk_and_memory(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(
        ["OBS:GetObject=allow", "OBS:*List*=allow", "*=deny"],
        ensure_ascii=False), encoding="utf-8")
    store = make_store(p)
    res = store.remove_rule("OBS:GetObject=allow")
    assert res.ok is True
    disk = json.loads(read_disk(p))
    assert "OBS:GetObject=allow" not in disk
    assert disk == ["OBS:*List*=allow", "*=deny"]
    assert policy.evaluate(store.rules(), "OBS", "GetObject") is False


def test_remove_not_found(tmp_path):
    p = tmp_path / "policy.json"
    before = '["*=deny"]'
    p.write_text(before, encoding="utf-8")
    store = make_store(p)
    res = store.remove_rule("OBS:GetObject=allow")
    assert res.ok is False
    assert "未找到" in (res.reason or "")
    assert read_disk(p) == before


def test_remove_keeps_comments_json_format(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(
        ["# head", "OBS:GetObject=allow", "*=deny"], ensure_ascii=False),
        encoding="utf-8")
    store = make_store(p)
    store.add_rule("VPC:*List*=allow")
    disk = json.loads(read_disk(p))
    assert disk[0] == "# head"
    assert disk == ["# head", "OBS:GetObject=allow", "VPC:*List*=allow", "*=deny"]


def test_text_format_roundtrip_preserves_order(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("# c1\nOBS:GetObject=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:*=allow")
    lines = semantic_lines(p)
    assert lines.index("ECS:*=allow") < lines.index("*=deny")


# ---------- 运行时降级 ----------

def test_corrupt_file_keeps_last_known_good(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('["OBS:*List*=allow", "*=deny"]', encoding="utf-8")
    store = make_store(p)
    good_rules = store.rules()

    p.write_text('["broken rule without equals"', encoding="utf-8")  # 非法 JSON + 非法行
    assert store.rules() == good_rules              # 沿用最近合法版本，不抛异常
    assert policy.evaluate(store.rules(), "OBS", "ListBuckets") is True


def test_recovered_file_is_picked_up(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    store = make_store(p)
    p.write_text('["ECS:*=allow"]', encoding="utf-8")
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True


def test_deleted_file_midrun_keeps_last_known_good(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('["OBS:*List*=allow"]', encoding="utf-8")
    store = make_store(p)
    p.unlink()
    assert policy.evaluate(store.rules(), "OBS", "ListBuckets") is True
