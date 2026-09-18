"""S18：PolicyStore 会话键控 overlay（ADR-0003 I1–I7）。

测试方式：注入 session_fn 替身交替会话（仿 stat_fn/time_fn 先例）+ tmp 策略
文件 + 注入时钟。独立真值：手写策略字面量 + 磁盘回读 + parse_policy 交叉验证。
回归红线：session_fn=None 时全部矩阵与既有 S2b 行为逐字一致（None 桶 = 历史
命名空间，现有 tests/test_policy_store.py 全量即验收线）。
"""

import json

from safety.policy_store import (
    MAX_SESSION_BUCKETS,
    SESSION_IDLE_SECONDS,
    PolicyStore,
)

LINE_A = "ECS:List*  = allow"
LINE_B = "VPC:Show* = allow"
LINE_DENY_ALL = "*:*=deny"


def _write_policy(tmp_path, lines: list[str]) -> str:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    return str(path)


class _Clock:
    """注入时钟（S2b 同法）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Sessions:
    """可切换的会话键替身。"""

    def __init__(self) -> None:
        self.key: str | None = None

    def __call__(self) -> str | None:
        return self.key


# ---------- I1：互斥桶 + first-match ----------

def test_i1_session_bucket_exclusive(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions, clock = _Sessions(), _Clock()
    store = PolicyStore(path, session_fn=sessions, time_fn=clock)

    sessions.key = "A"
    assert store.add_rule(LINE_A, scope="session").ok
    # A 的评估：overlay[A] ++ 文件 → allow 生效
    assert store.authorize("ECS", "ListServers") is None
    # B 的评估：只见文件 → deny；且看不到 A 的规则
    sessions.key = "B"
    hit = store.rules()
    assert all(r.product != "ECS" for r in hit)


def test_i1_stdio_none_bucket_is_legacy(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    store = PolicyStore(path)
    assert store.add_rule(LINE_A, scope="session").ok
    assert store.authorize("ECS", "ListServers") is None
    assert store.add_rule(LINE_A, scope="session").ok  # 幂等
    assert len(store.list_rules()) == 2  # session 档 1 条 + 文件 1 条


# ---------- I2：strict fail-closed ----------

def test_i2_strict_rejects_scoped_write_without_identity(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions, strict_sessions=True)

    sessions.key = None
    for scope in ("session", "temporary", "once"):
        result = store.add_rule(LINE_A, scope=scope)
        assert not result.ok, scope
        assert "会话身份" in (result.reason or ""), scope
    # None 桶未被污染
    sessions.key = "A"
    assert all(r.scope == "permanent" for r in store.list_rules())


def test_i2_strict_allows_permanent_without_identity(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    store = PolicyStore(path, session_fn=_Sessions(), strict_sessions=True)
    result = store.add_rule(LINE_A, scope="permanent")
    assert result.ok
    # 插入不变量：allow 落在遮蔽它的 deny 之前
    assert json.loads(open(path, encoding="utf-8").read()) == [LINE_A, LINE_DENY_ALL]


def test_i2_non_strict_keeps_legacy_write(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions)
    sessions.key = None
    assert store.add_rule(LINE_A, scope="session").ok  # 非 strict：None 桶可写（stdio 语义）


# ---------- I3：跨会话不可见（含 list_rules） ----------

def test_i3_list_rules_session_scoped(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions, time_fn=_Clock())

    sessions.key = "A"
    store.add_rule(LINE_A, scope="session")
    sessions.key = "B"
    lines = [r.line for r in store.list_rules()]
    assert LINE_A not in lines
    sessions.key = "A"
    assert LINE_A in [r.line for r in store.list_rules()]


# ---------- I4：once 会话内恰一 ----------

def test_i4_once_burns_only_in_granting_session(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions, time_fn=_Clock())

    sessions.key = "A"
    assert store.add_rule(LINE_A, scope="once").ok
    assert store.authorize("ECS", "ListServers") is None      # A 第一次放行并焚毁
    assert store.authorize("ECS", "ListServers") is not None  # A 第二次 deny
    sessions.key = "B"
    assert store.authorize("ECS", "ListServers") is not None  # B 从未见 A 的 once


def test_i4_once_shared_none_bucket_still_burns(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    store = PolicyStore(path)
    assert store.add_rule(LINE_A, scope="once").ok
    assert store.authorize("ECS", "ListServers") is None
    assert store.authorize("ECS", "ListServers") is not None


# ---------- I5：remove 跨层序（当前桶 → 文件） ----------

def test_i5_remove_searches_current_bucket_then_file(tmp_path):
    path = _write_policy(tmp_path, [LINE_A, LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions, time_fn=_Clock())

    sessions.key = "A"
    store.add_rule(LINE_A, scope="session")   # 桶内同名条目（文件里也有同义规则）
    result = store.remove_rule(LINE_A)
    assert result.ok and result.scope == "session"   # 先桶后文件
    # 文件里的那条还在
    sessions.key = "B"
    result = store.remove_rule(LINE_A)
    assert result.ok and result.scope == "permanent"


def test_i5_remove_miss_hint_for_http_sessions(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions)

    sessions.key = "A"
    reason = store.remove_rule(LINE_A).reason or ""
    assert "先前会话" in reason
    sessions.key = None
    reason_stdio = store.remove_rule(LINE_A).reason or ""
    assert "先前会话" not in reason_stdio


def test_i5_remove_never_touches_other_buckets(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions, time_fn=_Clock())

    sessions.key = "A"
    store.add_rule(LINE_A, scope="session")
    sessions.key = "B"
    assert not store.remove_rule(LINE_A).ok
    sessions.key = "A"
    assert store.authorize("ECS", "ListServers") is None  # A 的授予未被 B 的 remove 影响


# ---------- I6：三重惰性 GC ----------

def test_i6_idle_gc(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions, clock = _Sessions(), _Clock()
    store = PolicyStore(path, session_fn=sessions, time_fn=clock)

    sessions.key = "A"
    store.add_rule(LINE_A, scope="session")
    clock.now += SESSION_IDLE_SECONDS + 1   # A 不再有心跳 → 整桶回收
    sessions.key = "B"
    store.list_rules()                       # 任意持锁操作触发 GC
    sessions.key = "A"
    assert all(r.scope == "permanent" for r in store.list_rules())


def test_i6_heartbeat_keeps_bucket_alive(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions, clock = _Sessions(), _Clock()
    store = PolicyStore(path, session_fn=sessions, time_fn=clock)

    sessions.key = "A"
    store.add_rule(LINE_A, scope="session")
    for step in range(1, 6):
        clock.now += SESSION_IDLE_SECONDS * 0.9
        sessions.key = "A"
        store.authorize("ECS", "ListServers")   # 心跳
    assert store.authorize("ECS", "ListServers") is None  # 授予仍活着


def test_i6_max_age_gc(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions, clock = _Sessions(), _Clock()
    store = PolicyStore(path, session_fn=sessions, time_fn=clock)

    sessions.key = "A"
    store.add_rule(LINE_A, scope="session")
    for _ in range(200):
        clock.now += SESSION_IDLE_SECONDS * 0.5   # 持续心跳但超过绝对年龄
        store.authorize("ECS", "ListServers")
    assert all(r.scope == "permanent" for r in store.list_rules())


def test_i6_bucket_cap_evicts_lru(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions, clock = _Sessions(), _Clock()
    store = PolicyStore(path, session_fn=sessions, time_fn=clock)

    # 塞满 cap + 1 个桶（各持一条 session 规则）
    for i in range(MAX_SESSION_BUCKETS + 1):
        sessions.key = f"s{i}"
        assert store.add_rule(LINE_A, scope="session").ok
    # 最早写入、无心跳的 s0 被 LRU 淘汰
    sessions.key = "s0"
    assert all(r.scope == "permanent" for r in store.list_rules())
    # 最近写入的仍存活
    sessions.key = f"s{MAX_SESSION_BUCKETS}"
    assert store.authorize("ECS", "ListServers") is None


def test_i6_read_only_path_allocates_no_bucket(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions)

    sessions.key = "fresh-session"
    store.authorize("ECS", "ListServers")
    store.list_rules()
    store.rules()
    sessions.key = "another-fresh"
    store.authorize("ECS", "ListServers")
    # 无写路径 → 无桶：回到 A 会话看不到任何 session 档规则
    sessions.key = "s0"
    store.add_rule(LINE_A, scope="session")
    assert len([r for r in store.list_rules() if r.scope != "permanent"]) == 1


# ---------- 空键归一 + 会话键稳定性 ----------

def test_empty_session_key_normalized_to_none(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions)

    sessions.key = ""
    store.add_rule(LINE_A, scope="session")
    sessions.key = None
    assert store.authorize("ECS", "ListServers") is None   # "" 与 None 同桶


def test_hostile_session_fn_never_raises(tmp_path):
    path = _write_policy(tmp_path, [LINE_DENY_ALL])

    def boom() -> str | None:
        raise RuntimeError("boom")

    store = PolicyStore(path, session_fn=boom)
    assert store.add_rule(LINE_A, scope="session").ok   # 视同 None（legacy 桶）


# ---------- manage_policy_ops 透传（ambient 下零改动） ----------

def test_manage_policy_ops_session_scoped(tmp_path):
    from safety.policy_store import manage_policy_ops

    path = _write_policy(tmp_path, [LINE_DENY_ALL])
    sessions = _Sessions()
    store = PolicyStore(path, session_fn=sessions)

    sessions.key = "A"
    out = manage_policy_ops(store, "add", line=LINE_A)
    assert out["ok"] and out["scope"] == "session"
    sessions.key = "B"
    out = manage_policy_ops(store, "list")
    assert all(r["scope"] == "permanent" for r in out["rules"])
    assert "ECS" not in out["policy"]   # text() 恒文件全文
