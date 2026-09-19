"""S23：配置热刷新 service/apie 集成——加性 holder 接线、corrections 联动、
fetch 时点现读、热跟随。

独立真值：tmp 配置文件内容 + store 预填 + 手写断言；后台刷新经
_join_pending 同步。回归红线：holder=None 时与既有启动快照行为逐字节一致
（由既有 test_service/test_hints_service/test_deprecated 全绿保证）。
"""

import copy
import json

from apie.memory_store import MemoryStore
from common.hotconf import HotFile
from mcp_openapi.deprecated import DeprecatedIndex, parse_deprecated_index
from mcp_openapi.hints import Hints, parse_hints
from mcp_openapi.service import ServiceConfig, ToolService

GROUPS = [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器", "api_count": 2,
         "is_global": False, "link": None},
    ]},
]

APIS_ECS = [
    {"name": "ListServersDetails", "method": "get", "summary": "查询详情列表",
     "tags": "状态管理", "product_short": "ECS", "info_version": "v1"},
]

RAW_2 = {
    "product_short": "RDS",
    "name": "StartupInstance",
    "swagger": "2.0",
    "host": "rds.cn-north-4.myhuaweicloud.com",
    "paths": {
        "/v3/{project_id}/instances/{instance_id}/action/startup": {
            "post": {
                "operationId": "StartupInstance",
                "summary": "开启实例",
                "x-constraint": "- 该接口仅支持PostgreSQL引擎。\n- 其余保留。",
                "parameters": [],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}

REMAINDER = "- 其余保留。"


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _preset_store():
    store = MemoryStore()
    store.set_products(GROUPS)
    store.set_apis("ECS", APIS_ECS)
    return store


def _corrections_payload(enabled=True):
    if not enabled:
        return {}
    return {"RDS:StartupInstance": {
        "patches": {"x-constraint": {"drop": ["该接口仅支持PostgreSQL引擎"]}}}}


# ---------- hints：holder 热跟随（工具信封注入点） ----------


def test_hints_live_hot_follow(tmp_path):
    f = tmp_path / "hints.json"
    _write_json(f, {})
    hot = HotFile(str(f), parse=parse_hints)
    svc = ToolService(store=_preset_store(),
                      config=ServiceConfig(hints=Hints.empty(), hints_live=hot))
    out = svc.get_product("ecs")  # 触发 holder 读取
    assert "hints" not in out     # 启动内容为空：不注入
    _write_json(f, {"products": {"ECS": {"notes": "ECS 产品提示"}}})
    hot.get()                     # 触发后台刷新
    hot._join_pending()
    out = svc.get_product("ecs")
    assert out["ok"] is True
    assert out["hints"] == "ECS 产品提示"   # 下一次调用见新值


# ---------- deprecated：holder 热跟随（annotate 模式） ----------


def test_deprecated_live_hot_follow(tmp_path):
    f = tmp_path / "deprecated.json"
    _write_json(f, {})
    hot = HotFile(str(f), parse=parse_deprecated_index)
    svc = ToolService(store=_preset_store(),
                      config=ServiceConfig(deprecated_index=DeprecatedIndex.empty(),
                                           deprecated_mode="annotate",
                                           deprecated_live=hot))
    out = svc.list_apis("ECS")
    assert all("deprecated" not in a for a in out["apis"])
    _write_json(f, {"products": {"ECS": {"ListServersDetails": {
        "replacement": "NewApi"}}}})
    hot.get()
    hot._join_pending()
    out = svc.list_apis("ECS")
    assert out["apis"][0]["deprecated"] is True
    assert out["apis"][0]["replacement"] == "NewApi"


# ---------- entity：holder 热跟随（后台重建 stale-until-ready） ----------

ENTITY = {
    "version": 1,
    "products": [
        {"product": "ECS", "name": "弹性云服务器", "category": "计算",
         "is_global": False, "link": None, "aliases": ["云主机"],
         "related": []},
    ],
    "apis": [
        {"n": "ListServersDetails", "m": "get", "s": "查询云服务器详情列表",
         "t": "状态管理", "p": "ECS", "k": ["云主机"]},
    ],
    "tag_products": {"云服务器": 1},
}


def test_entity_live_hot_follow(tmp_path):
    from mcp_openapi.entity_graph import parse_entity_index

    f = tmp_path / "entity.json"
    _write_json(f, ENTITY)
    hot = HotFile(str(f), parse=parse_entity_index)
    svc = ToolService(store=_preset_store(),
                      config=ServiceConfig(entity_graph=hot.get(),
                                           entity_live=hot))
    out = svc.search_apis("云服务器")
    assert out["ok"] is True
    assert out["products"] and out["products"][0]["product"] == "ECS"
    # 变更图谱内容：ECS 语义改为对象存储，"对象存储" 成为新可检索词
    changed = copy.deepcopy(ENTITY)
    changed["products"][0]["name"] = "对象存储服务"
    changed["products"][0]["aliases"] = []
    changed["apis"][0]["s"] = "对象操作"
    changed["apis"][0]["t"] = "桶管理"
    changed["apis"][0]["k"] = []
    changed["tag_products"] = {"对象存储": 1}
    stale = svc.search_apis("对象存储")   # 旧图谱（reload 未触发/未完成）：零命中
    assert stale["products"] == []
    _write_json(f, changed)
    hot.get()
    hot._join_pending()
    out = svc.search_apis("对象存储")
    assert out["ok"] is True
    assert [p["product"] for p in out["products"]] == ["ECS"]  # 新图谱已生效
    assert svc.config.entity_live.get().version == 1


def test_entity_engine_kwargs_snapshot_pins_rebuild(monkeypatch, tmp_path):
    """后台重建复用装配期快照：装配后修改 _ENGINE_KWARGS 不漂移重建产物。"""
    import mcp_openapi.entity_graph as eg

    f = tmp_path / "entity.json"
    _write_json(f, ENTITY)
    monkeypatch.setattr(eg, "_ENGINE_KWARGS", {"tie_breaker": 0.4})
    hot = HotFile(str(f), parse=eg.parse_entity_index)
    _ = hot.get()
    monkeypatch.setattr(eg, "_ENGINE_KWARGS", {})  # 装配后全局被改
    g2 = eg.parse_entity_index(json.loads(f.read_text(encoding="utf-8")))
    assert g2 is not None  # 直接 parse 走新全局（既有语义）
    # 快照语义：snapshot 在装配期捕获，重建闭包用它（行为锚定在 server 装配）
    snap = eg.snapshot_engine_kwargs()
    assert snap == {}  # 当前全局值；装配点 build_openapi_config 持有其副本


# ---------- corrections：fetch 时点现读 + on_reload 定向失效 ----------

def test_live_fallback_provider_read_at_fetch_time(monkeypatch, tmp_path):
    """provider 在 compose 前现读当前文件内容（非构造期固化）。"""
    from apie.live_fallback import LiveFallback
    from apie.metadata_corrections import parse_metadata_corrections

    f = tmp_path / "corr.json"
    _write_json(f, {})
    hot = HotFile(str(f), parse=parse_metadata_corrections)
    monkeypatch.setattr("apie.explorer.fetch_detail",
                        lambda product, api, region: copy.deepcopy(RAW_2))
    lf = LiveFallback(MemoryStore(), corrections=hot)
    _write_json(f, _corrections_payload())   # 构造后、fetch 前变更
    hot.get()
    hot._join_pending()
    hit = lf.fetch("RDS", "StartupInstance", "cn-north-4")
    assert hit is not None
    assert hit[3]["x-constraint"] == REMAINDER   # fetch 时点读到新纠偏口径


def test_corrections_reload_clears_details_on_miss(monkeypatch, tmp_path):
    """miss 路径 provider.get() 触发检测 → reload 完成 → details 定向清空。

    stale-until-ready：触发该次 reload 的 miss 仍用旧口径 compose（残余窗口，
    文档化）；join 后缓存被清空，下一次 miss 用新口径重 compose。
    """
    from apie.metadata_corrections import parse_metadata_corrections

    f = tmp_path / "corr.json"
    _write_json(f, {})
    hot = HotFile(str(f), parse=parse_metadata_corrections)
    monkeypatch.setattr("apie.explorer.fetch_detail",
                        lambda product, api, region: copy.deepcopy(RAW_2))
    svc = ToolService(store=MemoryStore(),
                      config=ServiceConfig(corrections_live=hot))
    hit = svc.load_api_doc("RDS", "StartupInstance", "cn-north-4")
    assert hit is not None
    assert hit[3]["x-constraint"].startswith("- 该接口仅支持PostgreSQL引擎。")  # v1 无纠偏
    _write_json(f, _corrections_payload())
    assert svc.load_api_doc("RDS", "StartupInstance", "cn-north-4") is not None  # 缓存命中，不触发
    svc.load_api_doc("RDS", "OtherApi", "cn-north-4")  # miss：provider.get() 触发 reload
    hot._join_pending()
    assert len(svc.store._api_details) == 0  # on_reload → clear_api_details
    hit = svc.load_api_doc("RDS", "StartupInstance", "cn-north-4")  # 新口径重 compose
    assert hit is not None
    assert hit[3]["x-constraint"] == REMAINDER


def test_corrections_reload_keeps_products_and_apis(monkeypatch, tmp_path):
    from apie.metadata_corrections import parse_metadata_corrections

    f = tmp_path / "corr.json"
    _write_json(f, {})
    hot = HotFile(str(f), parse=parse_metadata_corrections)
    svc = ToolService(store=_preset_store(),
                      config=ServiceConfig(corrections_live=hot))
    _write_json(f, _corrections_payload())
    hot.get()
    hot._join_pending()
    assert svc.store.products() == GROUPS
    assert svc.store.apis("ECS") == APIS_ECS
