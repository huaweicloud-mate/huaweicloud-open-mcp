"""S15e：LLM 构建期抽取层 entity_extract。

refresh_knowledge（编排纯函数：指纹增量 + 分批 + 严格校验 + 合并）+
merge_knowledge（internal seam：curated 永不覆盖/llm 替换/按产品替换）+
load_llm_env（env 三元组缺失返回 None，stage 跳过）。
transport/sleep_fn 注入：fake transport 罐头 + 记录器。
"""

import json

from apie.entity_extract import (
    load_llm_env,
    merge_knowledge,
    refresh_knowledge,
)

APIS = [
    {"name": "NovaRebootServers", "method": "post", "summary": "重启弹性云服务器",
     "tags": "云服务器生命周期管理", "product_short": "ECS", "info_version": "v2"},
    {"name": "BatchCreateServerTags", "method": "post",
     "summary": "为弹性云服务器批量添加标签", "tags": "标签管理",
     "product_short": "ECS", "info_version": "v2"},
    {"name": "ResizeGpuServer", "method": "post", "summary": "变更GPU加速云服务器规格",
     "tags": "规格管理", "product_short": "GACS", "info_version": "v2"},
]

GROUPS = [{"name": "计算", "products": [
    {"name": "弹性云服务器", "productshort": "ECS", "is_global": False, "link": "x"},
    {"name": "GPU加速云服务器", "productshort": "GACS", "is_global": False, "link": "y"},
]}]


class FakeTransport:
    """按序消费罐头响应；记录 messages。响应为 LLM 文本 content。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok_product_batch():
    return ('{"aliases": [{"alias": "云主机", "targets": ["ECS"]}],'
            ' "relations": [{"from": "GACS", "to": "ECS",'
            ' "note": "GPU服务器归属弹性云服务器体系"}]}')


def _kw_response(mapping):
    return json.dumps({"keywords": [
        {"api": k, "keywords": v} for k, v in mapping.items()]},
        ensure_ascii=False)


def test_load_llm_env_missing_returns_none(monkeypatch):
    for k in ("ENTITY_LLM_BASE_URL", "ENTITY_LLM_API_KEY", "ENTITY_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert load_llm_env() is None


def test_load_llm_env_present(monkeypatch):
    monkeypatch.setenv("ENTITY_LLM_BASE_URL", "https://llm/v1")
    monkeypatch.setenv("ENTITY_LLM_API_KEY", "sk-x")
    monkeypatch.setenv("ENTITY_LLM_MODEL", "m1")
    cfg = load_llm_env()
    assert cfg == {"base_url": "https://llm/v1", "api_key": "sk-x",
                   "model": "m1"}


def test_full_run_all_products_extracted():
    t = FakeTransport(_ok_product_batch(),
                      _kw_response({"NovaRebootServers": ["重启服务器"]}),
                      _kw_response({"ResizeGpuServer": ["变更大卡机"]}))
    sleeps = []
    out = refresh_knowledge({}, APIS, GROUPS, transport=t,
                            sleep_fn=lambda s: sleeps.append(s))
    # 3 次调用：1 产品批 + 每 API 批
    assert len(t.calls) == 3
    assert len(sleeps) == 3
    assert out["aliases"] == [{"alias": "云主机", "targets": ["ECS"],
                               "source": "llm"}]
    assert out["relations"] == [{"from": "GACS", "to": "ECS",
                                 "kind": "semantic",
                                 "note": "GPU服务器归属弹性云服务器体系",
                                 "source": "llm"}]
    assert out["api_keywords"] == {
        "ECS": {"NovaRebootServers": ["重启服务器"]},
        "GACS": {"ResizeGpuServer": ["变更大卡机"]}}
    fps = out["fingerprints"]
    assert set(fps) == {"__products__", "ECS", "GACS"}


def test_no_changes_skips_llm():
    first = refresh_knowledge({}, APIS, GROUPS, transport=FakeTransport(
        *([_ok_product_batch(),
           _kw_response({}),
           _kw_response({})])))
    t = FakeTransport()
    again = refresh_knowledge(first, APIS, GROUPS, transport=t)
    assert t.calls == []
    assert again == first


def test_incremental_only_changed_products():
    first = refresh_knowledge({}, APIS, GROUPS, transport=FakeTransport(
        _ok_product_batch(),
        _kw_response({"NovaRebootServers": ["旧重启"]}),
        _kw_response({"ResizeGpuServer": ["旧词"]})))
    # GACS 的 API summary 变化 → 指纹变化
    changed = [dict(a) for a in APIS]
    changed[2] = dict(changed[2], summary="变更GPU加速云服务器规格(新)")
    t = FakeTransport(_kw_response({"ResizeGpuServer": ["新词"]}))
    out = refresh_knowledge(first, changed, GROUPS, transport=t)
    # 仅 1 次 LLM 调用（GACS API 批；roster 未变 → 产品级跳过）
    assert len(t.calls) == 1
    assert out["api_keywords"]["GACS"] == {"ResizeGpuServer": ["新词"]}
    # 未变化产品的关键词与产品级知识原样保留
    assert out["api_keywords"]["ECS"] == {"NovaRebootServers": ["旧重启"]}
    assert out["aliases"] == first["aliases"]


def test_roster_change_reextracts_product_level():
    first = refresh_knowledge({}, APIS, GROUPS, transport=FakeTransport(
        _ok_product_batch(), _kw_response({}), _kw_response({})))
    apis2 = APIS + [{"name": "ListNew", "method": "get",
                     "summary": "查询新分类资源", "tags": "新管理",
                     "product_short": "NEW", "info_version": "v2"}]
    groups2 = [{"name": "计算", "products": GROUPS[0]["products"] + [
        {"name": "新分类产品", "productshort": "NEW", "is_global": False,
         "link": "z"}]}]
    t = FakeTransport(_ok_product_batch(), _kw_response({}))
    out = refresh_knowledge(first, apis2, groups2, transport=t)
    # roster 变化 → 1 次产品批；NEW 有 API → 1 次 API 批
    assert len(t.calls) == 2
    assert out["aliases"] == [{"alias": "云主机", "targets": ["ECS"],
                               "source": "llm"}]


def test_merge_knowledge_curated_priority():
    old = {
        "aliases": [{"alias": "云主机", "targets": ["ECS"], "source": "curated"}],
        "relations": [{"from": "A", "to": "B", "kind": "semantic",
                       "note": "手工", "source": "curated"}],
        "api_keywords": {"ECS": {"X": ["旧"]}},
        "fingerprints": {"__products__": "p0", "ECS": "e0"},
    }
    update = {
        "aliases": [{"alias": "云主机", "targets": ["GACS"], "source": "llm"},
                    {"alias": "云盘", "targets": ["ECS"], "source": "llm"}],
        "relations": [{"from": "A", "to": "B", "kind": "semantic",
                       "note": "新", "source": "llm"}],
        "api_keywords": {"GACS": {"Y": ["新词"]}},
        "fingerprints": {"__products__": "p1", "ECS": "e1", "GACS": "g1"},
    }
    out = merge_knowledge(old, update)
    assert out["aliases"] == [
        {"alias": "云主机", "targets": ["ECS"], "source": "curated"},
        {"alias": "云盘", "targets": ["ECS"], "source": "llm"}]
    assert out["relations"] == [{"from": "A", "to": "B", "kind": "semantic",
                                 "note": "手工", "source": "curated"}]
    assert out["api_keywords"] == {"ECS": {"X": ["旧"]},
                                   "GACS": {"Y": ["新词"]}}
    assert out["fingerprints"] == {"__products__": "p1", "ECS": "e1",
                                   "GACS": "g1"}


def test_merge_knowledge_rerun_replaces_llm_entries():
    old = {
        "aliases": [{"alias": "云主机", "targets": ["ECS"], "source": "curated"},
                    {"alias": "旧词", "targets": ["GACS"], "source": "llm"}],
        "relations": [{"from": "G", "to": "E", "kind": "semantic",
                       "note": "旧", "source": "llm"}],
        "api_keywords": {}, "fingerprints": {},
    }
    update = {
        "aliases": [{"alias": "新词", "targets": ["ECS"], "source": "llm"}],
        "relations": [{"from": "G", "to": "E", "kind": "semantic",
                       "note": "新", "source": "llm"}],
        "api_keywords": {}, "fingerprints": {},
    }
    out = merge_knowledge(old, update)
    assert out["aliases"] == [
        {"alias": "云主机", "targets": ["ECS"], "source": "curated"},
        {"alias": "新词", "targets": ["ECS"], "source": "llm"}]
    assert out["relations"] == [{"from": "G", "to": "E", "kind": "semantic",
                                 "note": "新", "source": "llm"}]


def test_invalid_entries_dropped():
    long_note = "这条备注特别长" * 20
    t = FakeTransport(
        '{"aliases": ['
        '{"alias": "幽灵", "targets": ["XXX"]},'
        '{"alias": "弹性云服务器", "targets": ["ECS"]},'
        '{"alias": "", "targets": ["ECS"]},'
        '{"alias": "云主机", "targets": ["ECS"]},'
        '{"alias": "超长别名超过了八个字的限制", "targets": ["ECS"]}],'
        ' "relations": ['
        '{"from": "ECS", "to": "ECS", "note": "自环"},'
        '{"from": "GACS", "to": "XXX", "note": "未知"},'
        '{"from": "GACS", "to": "ECS", "note": "' + long_note + '"}]}')
    out = refresh_knowledge({}, APIS, GROUPS, transport=t)
    assert out["aliases"] == [{"alias": "云主机", "targets": ["ECS"],
                               "source": "llm"}]
    assert len(out["relations"]) == 1
    assert len(out["relations"][0]["note"]) <= 60


def test_api_keywords_validation():
    t = FakeTransport(_ok_product_batch(),
                      '{"keywords": ['
                      '{"api": "NovaRebootServers", "keywords": ['
                      '"重启服务器", "重启服务器", "这个词实在是太长了超过十二", "", '
                      '"开机", "重启", "关机", "重启机器", "再来一次", "第九条"]},'
                      '{"api": "NoSuchApi", "keywords": ["幽灵词"]},'
                      '{"api": "novarebootservers", "keywords": ["小写命中"]}]}',
                      _kw_response({"ResizeGpuServer": ["变更大卡机"]}))
    out = refresh_knowledge({}, APIS, GROUPS, transport=t)
    # 合并后按 API 封顶 ≤5（S15e 收紧：密集关键词拉长 BM25 字段恶化长度
    # 归一化）：7 条合法取前 5
    kws = out["api_keywords"]["ECS"]["NovaRebootServers"]
    assert kws == ["重启服务器", "开机", "重启", "关机", "重启机器"]
    assert "NoSuchApi" not in out["api_keywords"]["ECS"]


def test_batch_failure_skipped_and_resumable():
    t = FakeTransport(_ok_product_batch(),
                      RuntimeError("boom"),
                      _kw_response({"ResizeGpuServer": ["变更大卡机"]}))
    out = refresh_knowledge({}, APIS, GROUPS, transport=t)
    # ECS API 批失败：该产品关键词与指纹不更新（下次重跑重试）
    assert "ECS" not in out["api_keywords"]
    assert "ECS" not in out["fingerprints"]
    assert out["api_keywords"]["GACS"] == {"ResizeGpuServer": ["变更大卡机"]}


def test_product_batch_failure_keeps_old_roster_fingerprint():
    t = FakeTransport(RuntimeError("boom"),
                      _kw_response({}), _kw_response({}))
    out = refresh_knowledge({"aliases": [{"alias": "旧", "targets": ["ECS"],
                                          "source": "curated"}]},
                            APIS, GROUPS, transport=t)
    assert out["aliases"] == [{"alias": "旧", "targets": ["ECS"],
                               "source": "curated"}]
    assert "__products__" not in out["fingerprints"]
    # API 批成功的产品照常入账
    assert "ECS" in out["fingerprints"]


def test_markdown_fenced_json_parsed():
    t = FakeTransport(
        "```json\n" + _ok_product_batch() + "\n```",
        _kw_response({}), _kw_response({}))
    out = refresh_knowledge({}, APIS, GROUPS, transport=t)
    assert out["aliases"] == [{"alias": "云主机", "targets": ["ECS"],
                               "source": "llm"}]


def test_inputs_not_mutated():
    current = {"aliases": [], "relations": [], "api_keywords": {},
               "fingerprints": {}}
    apis = [dict(a) for a in APIS]
    refresh_knowledge(current, apis, GROUPS, transport=FakeTransport(
        _ok_product_batch(), _kw_response({}), _kw_response({})))
    assert current == {"aliases": [], "relations": [], "api_keywords": {},
                       "fingerprints": {}}
    assert apis == APIS


def test_progressive_persistence_callback():
    """on_update：每产品 API 批完成即回调合并态（中断可按指纹续跑）。"""
    snapshots = []
    out = refresh_knowledge(
        {}, APIS, GROUPS,
        transport=FakeTransport(
            _ok_product_batch(),
            _kw_response({"NovaRebootServers": ["重启服务器"]}),
            _kw_response({"ResizeGpuServer": ["变更大卡机"]})),
        on_update=lambda k: snapshots.append(dict(k["fingerprints"])))
    # 产品级完成 1 次 + 每 API 产品完成各 1 次 = 3 次快照，指纹单调增长
    assert len(snapshots) == 3
    assert snapshots[0] == {"__products__": snapshots[0]["__products__"]}
    assert set(snapshots[1]) >= {"__products__", "ECS"}
    assert set(snapshots[2]) == {"__products__", "ECS", "GACS"}
    assert out == refresh_knowledge({}, APIS, GROUPS, transport=FakeTransport(
        _ok_product_batch(),
        _kw_response({"NovaRebootServers": ["重启服务器"]}),
        _kw_response({"ResizeGpuServer": ["变更大卡机"]})))


# ---------- S15e 扩展：域约束闸门（判别性 ASCII token） ----------

def _current_with_domain():
    """curated 别名注入域 token：hadoop 仅 ECS 认领（判别），gpu 双产品
    认领（非判别）。"""
    return {"aliases": [
        {"alias": "Hadoop服务", "targets": ["ECS"], "source": "curated"},
        {"alias": "GPU加速", "targets": ["ECS", "GACS"], "source": "curated"},
    ]}


def test_keyword_foreign_domain_token_dropped(caplog):
    """关键词含他产品判别 token → 丢弃；自有 token 与共享 token 放行。"""
    import logging
    t = FakeTransport(
        _ok_product_batch(),
        _kw_response({"NovaRebootServers": ["重启服务器", "hadoop迁移"]}),
        _kw_response({"ResizeGpuServer": ["变更大卡机", "hadoop集群",
                                          "gpu规格变更"]}))
    with caplog.at_level(logging.WARNING, logger="apie.entity_extract"):
        out = refresh_knowledge(_current_with_domain(), APIS, GROUPS,
                                transport=t, sleep_fn=lambda _s: None)
    # ECS：自有 token（hadoop 属 ECS）放行
    assert out["api_keywords"]["ECS"]["NovaRebootServers"] == \
        ["重启服务器", "hadoop迁移"]
    # GACS：hadoop（属 ECS）丢弃；gpu（双认领非判别）放行
    assert out["api_keywords"]["GACS"]["ResizeGpuServer"] == \
        ["变更大卡机", "gpu规格变更"]
    assert "hadoop集群" in caplog.text


def test_alias_foreign_domain_token_dropped(caplog):
    """产品级别名提议含他产品判别 token → 丢弃。"""
    import logging
    canned = ('{"aliases": [{"alias": "Hadoop分析", "targets": ["GACS"]},'
              ' {"alias": "GPU盒子", "targets": ["GACS"]}],'
              ' "relations": []}')
    t = FakeTransport(canned, _kw_response({}), _kw_response({}))
    with caplog.at_level(logging.WARNING, logger="apie.entity_extract"):
        out = refresh_knowledge(_current_with_domain(), APIS, GROUPS,
                                transport=t, sleep_fn=lambda _s: None)
    assert out["aliases"] == [
        {"alias": "Hadoop服务", "targets": ["ECS"], "source": "curated"},
        {"alias": "GPU加速", "targets": ["ECS", "GACS"], "source": "curated"},
        {"alias": "GPU盒子", "targets": ["GACS"], "source": "llm"}]
    assert "Hadoop分析" in caplog.text


def test_force_ignores_fingerprints():
    canned = [_ok_product_batch(),
              _kw_response({"NovaRebootServers": ["重启服务器"]}),
              _kw_response({"ResizeGpuServer": ["变更大卡机"]})]
    first = refresh_knowledge({}, APIS, GROUPS, transport=FakeTransport(*canned),
                              sleep_fn=lambda _s: None)
    # 无 force：指纹命中 → 零调用
    t_noop = FakeTransport()
    refresh_knowledge(first, APIS, GROUPS, transport=t_noop,
                      sleep_fn=lambda _s: None)
    assert t_noop.calls == []
    # force：绕过指纹全量重抽，结果与全新跑等价
    t_force = FakeTransport(*canned)
    out = refresh_knowledge(first, APIS, GROUPS, transport=t_force,
                            force=True, sleep_fn=lambda _s: None)
    assert len(t_force.calls) == 3
    fresh = refresh_knowledge({}, APIS, GROUPS,
                              transport=FakeTransport(*canned),
                              sleep_fn=lambda _s: None)
    assert out == fresh


def test_alias_subsumed_by_other_product_alias_dropped(caplog):
    """支配性闸门（覆盖度）：alias 被 >2 个非 target 产品的别名包含 → 泛词
    丢弃；targets 成员不计入（共享别名合法）。"""
    import logging
    groups4 = [{"name": "计算", "products": GROUPS[0]["products"] + [
        {"name": "裸金属服务器", "productshort": "BMS", "is_global": False,
         "link": "b"},
        {"name": "智能边缘云", "productshort": "IEC", "is_global": False,
         "link": "i"}]}]
    current = {"aliases": [
        {"alias": "云服务器", "targets": ["ECS"], "source": "curated"},
        {"alias": "物理服务器", "targets": ["BMS"], "source": "curated"},
        {"alias": "边缘服务器", "targets": ["IEC"], "source": "curated"},
        {"alias": "云主机", "targets": ["ECS"], "source": "curated"}]}
    canned = ('{"aliases": [{"alias": "服务器", "targets": ["GACS"]},'
              ' {"alias": "服务器", "targets": ["ECS", "GACS"]}],'
              ' "relations": []}')
    t = FakeTransport(canned, _kw_response({}), _kw_response({}))
    with caplog.at_level(logging.WARNING, logger="apie.entity_extract"):
        out = refresh_knowledge(current, APIS, groups4, transport=t,
                                sleep_fn=lambda _s: None)
    # APIS 无 BMS/IEC 的 API → node_products 只有 ECS/GACS；GACS 版被
    # ECS/BMS/IEC 三个别名包含 → 丢弃；共享版非 target 包含者仅 BMS/IEC
    # ≤2 → 合法
    kept = {(x["alias"], tuple(x["targets"])) for x in out["aliases"]
            if x["source"] == "llm"}
    assert kept == {("服务器", ("ECS", "GACS"))}
    assert "支配性" in caplog.text
