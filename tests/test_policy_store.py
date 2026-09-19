"""PolicyStore 单测（S2b）：热重载 / 双向同步 / 原子写 / 回退 / 不可变。

独立真值：直接回读磁盘原始内容 + safety.policy.parse_policy 交叉验证，
不用被测代码自身的输出断言自身。stat 注入为「文件内容哈希」时间戳，
规避真实 mtime 秒级粒度导致的测试抖动。
"""

import json

import pytest

from safety import policy
from safety.policy_store import NOT_CONFIGURED_REASON, PolicyStore, manage_policy_ops


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

    res = store.add_rule("OBS:GetObject=allow", scope="permanent")
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
    store.add_rule("ECS:*List*=allow", scope="permanent")
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
    store.add_rule("VPC:*List*=allow", scope="permanent")
    disk = json.loads(read_disk(p))
    assert disk[0] == "# head"
    assert disk == ["# head", "OBS:GetObject=allow", "VPC:*List*=allow", "*=deny"]


def test_text_format_roundtrip_preserves_order(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("# c1\nOBS:GetObject=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:*=allow", scope="permanent")
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


# ---------- 三档 scope：会话内 overlay（默认档） ----------

def test_add_rule_defaults_to_session_overlay(tmp_path):
    """默认 scope=session：内存前置生效；文件字节不动；新实例（重启等价）即失。"""
    p = tmp_path / "policy.json"
    before = '["OBS:*List*=allow", "*=deny"]'
    p.write_text(before, encoding="utf-8")
    store = make_store(p)

    res = store.add_rule("OBS:GetObject=allow")
    assert res.ok is True
    assert res.scope == "session"
    assert read_disk(p) == before                                   # 文件不动
    assert policy.evaluate(store.rules(), "OBS", "GetObject") is True
    other = make_store(p)                                           # 重启等价：新实例无 overlay
    assert policy.evaluate(other.rules(), "OBS", "GetObject") is False


def test_session_overlay_precedes_file_rules(tmp_path):
    """一律前置：overlay allow 穿透文件具体 deny 与兜底 deny；overlay deny 收紧文件 allow。"""
    p = tmp_path / "policy.txt"
    p.write_text("ECS:DeleteServer=deny\nECS:*List*=allow\nECS:ShowServer=allow\n*=deny\n",
                 encoding="utf-8")
    store = make_store(p)

    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True
    store.add_rule("ECS:DeleteServer=allow")       # 穿透具体 deny
    assert policy.evaluate(store.rules(), "ECS", "DeleteServer") is True
    store.add_rule("VPC:ShowSubnet=allow")         # 穿透兜底 deny
    assert policy.evaluate(store.rules(), "VPC", "ShowSubnet") is True
    store.add_rule("ECS:*List*=deny")              # overlay deny 收紧文件 allow
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is False
    assert policy.evaluate(store.rules(), "ECS", "ListImages") is False
    assert policy.evaluate(store.rules(), "ECS", "ShowServer") is True  # 未命中的文件规则照常


def test_overlay_new_allow_inserted_before_overlay_deny(tmp_path):
    """overlay 内行序不变量：新 allow 插到 overlay 内首个遮蔽它的 deny 之前。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)

    store.add_rule("ECS:*=deny")
    store.add_rule("ECS:ListServers=allow")   # 若追加到 deny 之后则被遮蔽
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True
    assert policy.evaluate(store.rules(), "ECS", "DeleteServer") is False


def test_session_add_duplicate_idempotent(tmp_path):
    """层内语义幂等（大小写不敏感）；同文本与文件规则独立共存。"""
    p = tmp_path / "policy.txt"
    before = "ECS:List*=allow\n*=deny\n"
    p.write_text(before, encoding="utf-8")
    store = make_store(p)

    assert store.add_rule("ECS:GetServer=allow").ok is True
    assert store.add_rule("ecs:getserver=allow").ok is True   # 幂等，不重复入 overlay
    assert read_disk(p) == before
    assert len(store.rules()) == 3   # 2 文件 + 1 overlay（非 4）


# ---------- 临时（temporary，TTL 自动过期） ----------

def test_temporary_rule_expires_after_ttl(tmp_path):
    """scope=temporary：time_fn 注入时钟，到期惰性剪枝；剪枝后可重新授予。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    now = {"t": 1000.0}
    store = PolicyStore(str(p), stat_fn=make_stat_fn(), time_fn=lambda: now["t"])

    res = store.add_rule("ECS:ListServers=allow", scope="temporary", ttl_seconds=60)
    assert res.ok is True and res.scope == "temporary"
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True
    now["t"] += 59
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True   # 未到期
    now["t"] += 2
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is False  # 到期剪枝
    assert store.add_rule("ECS:ListServers=allow",
                          scope="temporary", ttl_seconds=60).ok is True   # 可重新授予
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True


def test_temporary_default_ttl_is_3600(tmp_path):
    """scope=temporary 缺省 ttl_seconds=3600。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    now = {"t": 0.0}
    store = PolicyStore(str(p), stat_fn=make_stat_fn(), time_fn=lambda: now["t"])

    store.add_rule("ECS:ListServers=allow", scope="temporary")
    now["t"] += 3599
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True
    now["t"] += 2
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is False


def test_ttl_only_valid_with_temporary(tmp_path):
    """ttl_seconds 仅与 scope=temporary 组合，且必须为正。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)

    assert store.add_rule("ECS:*=allow", scope="session", ttl_seconds=60).ok is False
    assert store.add_rule("ECS:*=allow", scope="permanent", ttl_seconds=60).ok is False
    res0 = store.add_rule("ECS:*=allow", scope="temporary", ttl_seconds=0)
    assert res0.ok is False and "ttl_seconds" in (res0.reason or "")
    resn = store.add_rule("ECS:*=allow", scope="temporary", ttl_seconds=-5)
    assert resn.ok is False


# ---------- 一次性（once，用后即焚） ----------

def test_once_rule_allows_then_burns(tmp_path):
    """scope=once：authorize 首次放行并焚毁，二次落兜底 deny；文件字节恒不动。"""
    p = tmp_path / "policy.txt"
    before = "*=deny\n"
    p.write_text(before, encoding="utf-8")
    store = make_store(p)

    res = store.add_rule("OBS:GetObject=allow", scope="once")
    assert res.ok is True and res.scope == "once"
    assert store.authorize("OBS", "GetObject") is None            # 首次放行（放行即焚）
    err = store.authorize("OBS", "GetObject")
    assert err is not None and "拒绝" in err                       # 已焚毁 → 兜底 deny
    assert read_disk(p) == before                                  # 从不落盘
    assert policy.evaluate(store.rules(), "OBS", "GetObject") is False


def test_once_rule_not_burned_by_non_matching_calls(tmp_path):
    """authorize 的 deny 路径永不焚毁：未命中调用不消耗 once 授权。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("OBS:GetObject=allow", scope="once")

    assert store.authorize("OBS", "PutObject") is not None         # 未命中
    assert store.authorize("ECS", "GetObject") is not None         # 未命中
    assert store.authorize("OBS", "GetObject") is None             # 仍放行


def test_authorize_repeatable_for_non_once_rules(tmp_path):
    """文件层 / session 层 allow 经 authorize 反复放行（无焚毁、零回归）。"""
    p = tmp_path / "policy.txt"
    p.write_text("ECS:List*=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:GetServer=allow")                          # session 层

    for _ in range(2):
        assert store.authorize("ECS", "ListServers") is None
        assert store.authorize("ECS", "GetServer") is None
    assert store.authorize("ECS", "DeleteServer") is not None


def test_once_with_ttl_rejected(tmp_path):
    """once 与 ttl_seconds 互斥。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    res = store.add_rule("ECS:*=allow", scope="once", ttl_seconds=60)
    assert res.ok is False


def test_once_list_rules_reports_scope(tmp_path):
    """list_rules 回报 scope=once、expires_in=None。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:List*=allow", scope="once")
    infos = store.list_rules()
    assert infos[0].scope == "once"
    assert infos[0].expires_in is None
    assert infos[0].line == "ECS:List*=allow"


def test_remove_once_rule_reports_scope(tmp_path):
    """remove 跨层回收 once 规则并回报 scope=once。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:List*=allow", scope="once")
    res = store.remove_rule("ECS:List*=allow")
    assert res.ok is True and res.scope == "once"
    assert store.authorize("ECS", "ListServers") is not None       # 已移除 → deny


def test_authorize_server_once_burns(tmp_path):
    """discover 调用级 server 规则 once 版同样用后即焚；connect 级授权不受影响。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)

    store.add_rule("server:srv1:list*=allow", scope="once")
    assert store.authorize_server("srv1", "listTools") is None     # 首次放行
    assert store.authorize_server("srv1", "listTools") is not None  # 已焚毁
    store.add_rule("server:srv2=allow", scope="once")
    assert store.authorize_server("srv2", None) is None            # connect 级首次放行
    assert store.authorize_server("srv2", None) is not None        # 已焚毁


def test_authorize_unconfigured_still_denies():
    """未配置路径（path=None）恒 deny，红线不变。"""
    store = PolicyStore(None)
    assert store.authorize("ECS", "ListServers") is not None
    assert store.authorize_server("srv1", "listTools") is not None


def test_authorize_once_concurrent_only_one_passes(tmp_path):
    """并发授权同一 once 规则：RLock 内评估+焚毁原子，恰有一个放行。"""
    import threading

    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("OBS:GetObject=allow", scope="once")

    results: list[str | None] = []
    barrier = threading.Barrier(8)

    def probe():
        barrier.wait()
        results.append(store.authorize("OBS", "GetObject"))

    threads = [threading.Thread(target=probe) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(None) == 1                                # 恰一次放行
    assert len(results) == 8


# ---------- remove 跨层 + list_rules ----------

def test_remove_searches_overlay_before_file(tmp_path):
    """remove 先 overlay 后文件；scope 回报删除层；同语义两层共存时分两次移除。"""
    p = tmp_path / "policy.txt"
    before = "OBS:GetObject=allow\n*=deny\n"
    p.write_text(before, encoding="utf-8")
    store = make_store(p)

    store.add_rule("OBS:GetObject=allow")            # overlay（默认 session）
    res = store.remove_rule("OBS:GetObject=allow")
    assert res.ok is True and res.scope == "session"
    assert read_disk(p) == before                    # 文件同文本仍在
    res2 = store.remove_rule("OBS:GetObject=allow")
    assert res2.ok is True and res2.scope == "permanent"
    res3 = store.remove_rule("OBS:GetObject=allow")
    assert res3.ok is False and "未找到" in (res3.reason or "")


def test_remove_temporary_reports_scope(tmp_path):
    now = {"t": 0.0}
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = PolicyStore(str(p), stat_fn=make_stat_fn(), time_fn=lambda: now["t"])

    store.add_rule("ECS:*=allow", scope="temporary", ttl_seconds=100)
    res = store.remove_rule("ECS:*=allow")
    assert res.ok is True and res.scope == "temporary"
    assert read_disk(p) == "*=deny\n"


def test_list_rules_structure_and_order(tmp_path):
    """list_rules 按评估序：overlay 前置；temporary 带 expires_in 剩余秒。"""
    p = tmp_path / "policy.txt"
    p.write_text("ECS:*List*=allow\n*=deny\n", encoding="utf-8")
    now = {"t": 100.0}
    store = PolicyStore(str(p), stat_fn=make_stat_fn(), time_fn=lambda: now["t"])

    store.add_rule("ECS:GetServer=allow")                                  # session
    store.add_rule("OBS:GetObject=allow", scope="temporary", ttl_seconds=50)

    infos = store.list_rules()
    assert [i.scope for i in infos] == ["session", "temporary", "permanent", "permanent"]
    assert infos[0].line == "ECS:GetServer=allow"
    assert infos[0].expires_in is None
    assert infos[1].line == "OBS:GetObject=allow"
    assert infos[1].expires_in == 50     # 剩余秒（now=100，expire_at=150）
    assert all(i.expires_in is None for i in infos[2:])
    assert infos[2].line == "ECS:*List*=allow"

    now["t"] += 50                        # 恰到 expire_at → 剪枝
    infos = store.list_rules()
    assert [i.scope for i in infos] == ["session", "permanent", "permanent"]


# ---------- 边界 ----------

def test_unconfigured_rejects_all_scopes():
    """红线：未配置 policy 文件，三档全部拒绝，不创建文件。"""
    store = PolicyStore(None)
    for scope in ("permanent", "temporary", "session"):
        out = store.add_rule("ECS:*=allow", scope=scope)
        assert out.ok is False and "--policy" in (out.reason or "")
    assert store.rules() == ()
    assert store.list_rules() == []


def test_hot_reload_keeps_overlay(tmp_path):
    """外部改文件触发热重载，overlay 不丢；组合视图即时反映新文件规则。"""
    p = tmp_path / "policy.json"
    p.write_text('["*=deny"]', encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:ListServers=allow")            # session overlay
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True

    p.write_text('["OBS:*=allow", "*=deny"]', encoding="utf-8")   # 外部编辑
    assert policy.evaluate(store.rules(), "OBS", "GetObject") is True    # 文件新规则生效
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True  # overlay 未丢


def test_unknown_scope_rejected(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)

    res = store.add_rule("ECS:*=allow", scope="forever")
    assert res.ok is False and "scope" in (res.reason or "")
    assert store.rules() == tuple(policy.parse_policy(["*=deny"]))   # 状态未动


# ---------- 并发一致性（进程内互斥） ----------

def _disk_rule_tuples(path):
    """独立真值：直接解析磁盘规则为语义元组，不用 store 自身输出断言自身。"""
    return sorted((r.kind, r.product, r.api_pattern, r.allow)
                  for r in policy.parse_policy(semantic_lines(path)))


def test_concurrent_adds_all_persisted(tmp_path):
    import concurrent.futures
    import threading

    p = tmp_path / "policy.txt"
    p.write_text("ECS:*List*=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    lines = [f"OBS:*Action{i}*=allow" for i in range(10)]
    barrier = threading.Barrier(len(lines))

    def worker(line):
        barrier.wait()          # 最大化并发窗口
        return store.add_rule(line, scope="permanent")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(lines)) as ex:
        results = list(ex.map(worker, lines))

    assert all(r.ok for r in results)
    disk = _disk_rule_tuples(p)
    for line in lines:
        r = policy.parse_policy([line])[0]
        assert (r.kind, r.product, r.api_pattern, r.allow) in disk   # 无丢失更新
    memory = sorted((r.kind, r.product, r.api_pattern, r.allow) for r in store.rules())
    assert memory == disk            # 静止态 memory == file
    assert len(disk) == len(lines) + 2


def test_concurrent_removes_all_applied(tmp_path):
    import concurrent.futures
    import threading

    doomed = [f"OBS:*Act{i}*=allow" for i in range(8)]
    body = "\n".join(["ECS:*List*=allow", *doomed, "*=deny"]) + "\n"
    p = tmp_path / "policy.txt"
    p.write_text(body, encoding="utf-8")
    store = make_store(p)
    barrier = threading.Barrier(len(doomed))

    def worker(line):
        barrier.wait()
        return store.remove_rule(line)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(doomed)) as ex:
        results = list(ex.map(worker, doomed))

    assert all(r.ok for r in results)
    disk = _disk_rule_tuples(p)
    leftover = {t[2] for t in disk}
    for line in doomed:
        pat = line.split(":")[1].split("=")[0]
        assert pat not in leftover   # 每条目标规则都真实消失（无覆盖回滚）
    assert store.rules() and len(store.rules()) == 2   # 仅剩 ECS allow + * deny


# ---------- 批量 add（add_rules：整批 fail-fast，单次落盘） ----------

def test_add_rules_defaults_to_session_overlay(tmp_path):
    """批量缺省 scope=session：整批入 overlay，文件字节不动，重启等价即失。"""
    p = tmp_path / "policy.txt"
    before = "*=deny\n"
    p.write_text(before, encoding="utf-8")
    store = make_store(p)

    out = store.add_rules(["OBS:GetObject=allow", "ECS:*List*=allow"])
    assert out.ok is True
    assert out.scope == "session"
    assert out.reason is None
    assert [r.ok for r in out.results] == [True, True]
    assert all(r.scope == "session" for r in out.results)
    assert read_disk(p) == before
    assert policy.evaluate(store.rules(), "OBS", "GetObject") is True
    assert policy.evaluate(store.rules(), "ECS", "ListImages") is True
    other = make_store(p)                          # 重启等价：新实例无 overlay
    assert policy.evaluate(other.rules(), "OBS", "GetObject") is False


def test_add_rules_permanent_matches_sequential_singles(tmp_path):
    """独立真值：批量 permanent 的最终落盘行序 ≡ 同一文件上 N 次顺序单条 add。"""
    seed = "ECS:*List*=allow\n*=deny\n"
    p = tmp_path / "policy.txt"
    p.write_text(seed, encoding="utf-8")
    twin = tmp_path / "twin.txt"
    twin.write_text(seed, encoding="utf-8")
    batch_store = make_store(p)
    seq_store = make_store(twin)

    lines = ["OBS:PutObject=allow", "ECS:*=deny", "OBS:GetObject=allow", "VPC:*=allow"]
    out = batch_store.add_rules(lines, scope="permanent")
    assert out.ok is True
    for line in lines:
        assert seq_store.add_rule(line, scope="permanent").ok is True

    assert semantic_lines(p) == semantic_lines(twin)   # 行序逐行一致
    other = make_store(p)                              # 真值在文件
    assert policy.evaluate(other.rules(), "OBS", "GetObject") is True


def test_add_rules_fail_fast_rejects_whole_batch(tmp_path):
    """任一行非法 → 整批拒绝（reason 指明序号），磁盘与 overlay 一条不应用。"""
    p = tmp_path / "policy.txt"
    before = "*=deny\n"
    p.write_text(before, encoding="utf-8")
    store = make_store(p)

    out = store.add_rules(["ECS:GetServer=allow", "not a rule"], scope="permanent")
    assert out.ok is False
    assert "第 2 条" in (out.reason or "")
    assert out.results == ()
    assert read_disk(p) == before
    assert policy.evaluate(store.rules(), "ECS", "GetServer") is False

    out2 = store.add_rules(["ECS:GetServer=allow", "  "])   # 空白行同样整批拒绝
    assert out2.ok is False and "第 2 条" in (out2.reason or "")
    assert policy.evaluate(store.rules(), "ECS", "GetServer") is False


def test_add_rules_in_batch_duplicate_idempotent(tmp_path):
    """批内语义重复：第二条报「规则已存在」仍 ok，overlay 仅入一条。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)

    out = store.add_rules(["ECS:GetServer=allow", "ecs:getserver=allow"])
    assert out.ok is True
    assert [r.reason for r in out.results] == [None, "规则已存在"]
    assert len(store.rules()) == 2   # 文件 1 + overlay 1


def test_add_rules_overlay_insert_invariant_with_in_batch_deny(tmp_path):
    """批内 deny+allow 相互依赖：两种提交顺序下 allow 均不被 deny 遮蔽。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    assert store.add_rules(["ECS:*=deny", "ECS:ListServers=allow"]).ok is True
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is True
    assert policy.evaluate(store.rules(), "ECS", "DeleteServer") is False

    p2 = tmp_path / "p2.txt"
    p2.write_text("*=deny\n", encoding="utf-8")
    store2 = make_store(p2)
    assert store2.add_rules(["ECS:ListServers=allow", "ECS:*=deny"]).ok is True
    assert policy.evaluate(store2.rules(), "ECS", "ListServers") is True
    assert policy.evaluate(store2.rules(), "ECS", "DeleteServer") is False


def test_add_rules_permanent_in_batch_deny_invariant(tmp_path):
    """permanent 批内不变量：后提交的 allow 落在批内 deny 之前、批内 deny 在兜底之前。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = store.add_rules(["ECS:*=deny", "ECS:ListServers=allow"], scope="permanent")
    assert out.ok is True
    lines = semantic_lines(p)
    assert lines.index("ECS:ListServers=allow") < lines.index("ECS:*=deny")
    assert lines.index("ECS:*=deny") < lines.index("*=deny")


def test_add_rules_temporary_batch_shared_ttl(tmp_path):
    """temporary 批量共享 ttl：整批同时到期剪枝。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    now = {"t": 0.0}
    store = PolicyStore(str(p), stat_fn=make_stat_fn(), time_fn=lambda: now["t"])

    out = store.add_rules(["ECS:ListServers=allow", "OBS:GetObject=allow"],
                          scope="temporary", ttl_seconds=60)
    assert out.ok is True and all(r.scope == "temporary" for r in out.results)
    now["t"] += 61
    assert policy.evaluate(store.rules(), "ECS", "ListServers") is False
    assert policy.evaluate(store.rules(), "OBS", "GetObject") is False


def test_add_rules_once_batch_burns_independently(tmp_path):
    """once 批量：每条独立用后即焚。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = store.add_rules(["OBS:GetObject=allow", "ECS:ListServers=allow"], scope="once")
    assert out.ok is True and all(r.scope == "once" for r in out.results)
    assert store.authorize("OBS", "GetObject") is None
    assert store.authorize("OBS", "GetObject") is not None      # 该条已焚
    assert store.authorize("ECS", "ListServers") is None        # 另一条仍在
    assert store.authorize("ECS", "ListServers") is not None


def test_add_rules_scope_and_ttl_validation(tmp_path):
    """整批参数校验与单条同口径：未知 scope / ttl 误用 → 整批拒绝。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = store.add_rules(["ECS:*=allow"], scope="forever")
    assert out.ok is False and "scope" in (out.reason or "")
    out = store.add_rules(["ECS:*=allow"], scope="session", ttl_seconds=60)
    assert out.ok is False
    out = store.add_rules(["ECS:*=allow"], scope="temporary", ttl_seconds=0)
    assert out.ok is False


def test_add_rules_unconfigured_rejected():
    """红线：未配置 policy 文件，批量 add 整批拒绝。"""
    store = PolicyStore(None)
    out = store.add_rules(["ECS:*=allow"])
    assert out.ok is False
    assert out.reason == NOT_CONFIGURED_REASON


def test_add_rules_empty_rejected(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = store.add_rules([])
    assert out.ok is False and out.results == ()


# ---------- 批量 remove（remove_rules：逐条尽力而为，文件层单次落盘） ----------

def test_remove_rules_mixed_layers_with_miss(tmp_path):
    """逐条先会话桶后文件；未命中记失败项；文件层删除合并落盘。"""
    p = tmp_path / "policy.txt"
    p.write_text("OBS:GetObject=allow\nECS:*List*=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("ECS:GetServer=allow")           # session overlay

    out = store.remove_rules(["ECS:GetServer=allow", "OBS:GetObject=allow",
                              "VPC:ShowSubnet=allow"])
    assert out.reason is None
    assert [r.ok for r in out.results] == [True, True, False]
    assert out.results[0].scope == "session"
    assert out.results[1].scope == "permanent"
    assert "未找到" in (out.results[2].reason or "")
    assert semantic_lines(p) == ["ECS:*List*=allow", "*=deny"]
    assert policy.evaluate(store.rules(), "ECS", "GetServer") is False


def test_remove_rules_same_target_twice_second_misses(tmp_path):
    """批量内同一目标两行：首行命中移除，次行未命中（顺序语义）。"""
    p = tmp_path / "policy.txt"
    p.write_text("ECS:GetServer=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = store.remove_rules(["ECS:GetServer=allow", "ECS:GetServer=allow"])
    assert [r.ok for r in out.results] == [True, False]
    assert semantic_lines(p) == ["*=deny"]


def test_remove_rules_invalid_line_per_item_failure(tmp_path):
    """remove 逐条尽力而为：格式非法行记失败项，其余照常应用。"""
    p = tmp_path / "policy.txt"
    p.write_text("ECS:GetServer=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = store.remove_rules(["garbage", "ECS:GetServer=allow"])
    assert [r.ok for r in out.results] == [False, True]
    assert "格式" in (out.results[0].reason or "")
    assert semantic_lines(p) == ["*=deny"]


def test_remove_rules_all_miss_disk_untouched(tmp_path):
    p = tmp_path / "policy.txt"
    before = "ECS:GetServer=allow\n*=deny\n"
    p.write_text(before, encoding="utf-8")
    store = make_store(p)
    out = store.remove_rules(["VPC:A=allow", "VPC:B=allow"])
    assert out.ok is False
    assert out.reason is None
    assert [r.ok for r in out.results] == [False, False]
    assert read_disk(p) == before


def test_remove_rules_unconfigured_rejected():
    store = PolicyStore(None)
    out = store.remove_rules(["ECS:*=allow"])
    assert out.ok is False and out.reason == NOT_CONFIGURED_REASON


# ---------- manage_policy_ops：两模式 manage_policy 工具共用的运维信封 ----------

def test_manage_policy_ops_unconfigured():
    """store=None：ok=False，reason 为统一 NOT_CONFIGURED_REASON（消灭文案漂移）。"""
    out = manage_policy_ops(None, "list")
    assert out["ok"] is False
    assert out["reason"] == NOT_CONFIGURED_REASON
    assert "--policy" in out["reason"]


def test_manage_policy_ops_action_normalized(tmp_path):
    """action 大小写/空白归一后分派（'List' ≡ 'list'）。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    out = manage_policy_ops(make_store(p), "  LIST ")
    assert out["ok"] is True and out["action"] == "list"


def test_manage_policy_ops_list_envelope(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "list")
    assert out["ok"] is True and out["action"] == "list"
    assert "*=deny" in out["policy"]
    assert out["rules"] == [{"line": "*=deny", "scope": "permanent",
                             "expires_in": None}]


def test_manage_policy_ops_add_session_envelope(tmp_path):
    """缺省 add = session 档：信封携带 scope；policy 渲染文件规则（overlay 不入，
    与 PolicyStore.text() 契约一致）；文件不动。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    before = p.read_text(encoding="utf-8")
    out = manage_policy_ops(store, "add", "ECS:*=allow")
    assert out["ok"] is True and out["action"] == "add"
    assert out["scope"] == "session"
    assert out["policy"] == before   # text() 仅文件规则，session overlay 不可见
    assert p.read_text(encoding="utf-8") == before   # session 档不落盘


def test_manage_policy_ops_add_permanent_persists(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "add", "ECS:*=allow", scope="permanent")
    assert out["ok"] is True and out["scope"] == "permanent"
    assert "ECS:*=allow" in p.read_text(encoding="utf-8")


def test_manage_policy_ops_remove_rejects_scope_and_ttl(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("ECS:*=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "remove", "ECS:*=allow", scope="session")
    assert out["ok"] is False
    assert "remove 不接受 scope/ttl_seconds" in out["reason"]
    out = manage_policy_ops(store, "remove", "ECS:*=allow", ttl_seconds=60)
    assert out["ok"] is False
    assert store.rules() == tuple(policy.parse_policy(
        ["ECS:*=allow", "*=deny"]))   # 状态未动


def test_manage_policy_ops_remove_envelope_and_disk_sync(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("ECS:*=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "remove", "ECS:*=allow")
    assert out["ok"] is True and out["action"] == "remove"
    assert "ECS:*=allow" not in p.read_text(encoding="utf-8")   # 跨层移除含文件


def test_manage_policy_ops_unknown_action_and_missing_line(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "grant", line="ECS:*=allow")
    assert out["ok"] is False and "未知 action: grant" in out["reason"]
    out = manage_policy_ops(store, "add")
    assert out["ok"] is False and "需要提供 line" in out["reason"]


def test_manage_policy_ops_add_rule_error_passthrough(tmp_path):
    """store 层拒绝（未知 scope / 规则格式非法）→ 信封 ok=False + reason 透传。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "add", "ECS:*=allow", scope="forever")
    assert out["ok"] is False and "scope" in (out.get("reason") or "")
    out = manage_policy_ops(store, "add", "not a rule")
    assert out["ok"] is False and out.get("reason")


# ---------- manage_policy_ops 批量信封（list 入参） ----------

def test_manage_policy_ops_batch_add_envelope(tmp_path):
    """list 入参 → 批量信封：results 逐条 + scope + policy。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "add", ["ECS:*=allow", "OBS:GetObject=allow"])
    assert out["ok"] is True
    assert out["action"] == "add"
    assert out["scope"] == "session"
    assert out["results"] == [
        {"line": "ECS:*=allow", "ok": True, "scope": "session"},
        {"line": "OBS:GetObject=allow", "ok": True, "scope": "session"}]
    assert out["policy"] == store.text()


def test_manage_policy_ops_batch_fail_fast_envelope(tmp_path):
    """整批拒绝：ok=False + reason 指明坏行 + results=[]；磁盘未动。"""
    p = tmp_path / "policy.txt"
    before = "*=deny\n"
    p.write_text(before, encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "add", ["ECS:*=allow", "nope"], scope="permanent")
    assert out["ok"] is False
    assert "第 2 条" in (out["reason"] or "")
    assert out["results"] == []
    assert out["scope"] == "permanent"
    assert p.read_text(encoding="utf-8") == before


def test_manage_policy_ops_batch_remove_envelope(tmp_path):
    """批量 remove：混层逐条结果 + 文件层合并落盘；顶层 ok=全部成功。"""
    p = tmp_path / "policy.txt"
    p.write_text("ECS:GetServer=allow\n*=deny\n", encoding="utf-8")
    store = make_store(p)
    store.add_rule("OBS:GetObject=allow")          # session overlay
    out = manage_policy_ops(store, "remove",
                            ["OBS:GetObject=allow", "ECS:GetServer=allow", "VPC:A=allow"])
    assert out["ok"] is False                       # 有未命中项
    assert [r["ok"] for r in out["results"]] == [True, True, False]
    assert out["results"][0]["scope"] == "session"
    assert out["results"][1]["scope"] == "permanent"
    assert "未找到" in (out["results"][2]["reason"] or "")
    disk = p.read_text(encoding="utf-8")
    assert "OBS:GetObject=allow" not in disk
    assert "ECS:GetServer=allow" not in disk


def test_manage_policy_ops_batch_empty_rejected(tmp_path):
    """空数组 / 全空串按缺 line 拒绝（与单条同一文案）。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "add", [])
    assert out["ok"] is False and "需要提供 line" in (out["reason"] or "")
    out = manage_policy_ops(store, "add", ["  "])
    assert out["ok"] is False and "需要提供 line" in (out["reason"] or "")


def test_manage_policy_ops_batch_remove_rejects_scope(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "remove", ["ECS:*=allow"], scope="session")
    assert out["ok"] is False
    assert "remove 不接受 scope/ttl_seconds" in (out["reason"] or "")


def test_manage_policy_ops_single_line_envelope_unchanged(tmp_path):
    """回归红线：str 入参信封形状与批量改造前逐字段一致（无 results 键）。"""
    p = tmp_path / "policy.txt"
    p.write_text("*=deny\n", encoding="utf-8")
    store = make_store(p)
    out = manage_policy_ops(store, "add", "ECS:*=allow")
    assert set(out.keys()) == {"ok", "action", "scope", "policy"}
    assert out["scope"] == "session"
    out = manage_policy_ops(store, "add", "bad rule")
    assert set(out.keys()) == {"ok", "action", "scope", "reason", "policy"}
    out = manage_policy_ops(store, "remove", "ECS:*=allow")
    assert set(out.keys()) == {"ok", "action", "scope", "policy"}
    out = manage_policy_ops(store, "grant", line="ECS:*=allow")
    assert set(out.keys()) == {"ok", "reason"}
