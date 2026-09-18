"""S5：apie 元数据纠偏 MetadataCorrections 纯函数单测（parse/load/apply 矩阵）。

独立真值：RDS StartupInstance 过期文案取自 API Explorer 实测 payload，
官方约束取自帮助文档 rds_05_0026（2026-05-28 更新，接口约束无引擎限制）。
"""

import json
import tempfile

import pytest

from apie.metadata_corrections import (
    MetadataCorrections,
    correct_doc,
    load_metadata_corrections,
    parse_metadata_corrections,
)

# 实测 live payload（2026-09）：首两行为过期声明，官方文档均无此限制
# （引擎无限制 + 开机无需客服申请，MySQL 8.0 实测开机成功）
STARTUP_XCONSTRAINT = (
    "- 该接口仅支持PostgreSQL引擎。\n"
    "- 如需开通开启实例的权限,请联系客服人员申请。\n"
    "- 开启主实例时,如果存在只读实例,会同时开启只读实例。\n"
    '- 仅支持开启实例状态是"已停止"的实例。'
)
STARTUP_REMAINDER = (
    "- 开启主实例时,如果存在只读实例,会同时开启只读实例。\n"
    '- 仅支持开启实例状态是"已停止"的实例。'
)

RAW = {
    "RDS:StartupInstance": {
        "evidence": "官方帮助文档 rds_05_0026（2026-05-28 更新）接口约束无引擎限制",
        "doc_url": "https://support.huaweicloud.com/api-rds/rds_05_0026.html",
        "patches": {"x-constraint": {"drop": [
            "该接口仅支持PostgreSQL引擎",
            "如需开通开启实例的权限",
        ]}},
    },
}


def _envelope() -> dict:
    return {"ok": True, "product": "RDS", "api": "StartupInstance",
            "summary": "开启实例", "x-constraint": STARTUP_XCONSTRAINT}


def _doc() -> dict:
    return {"paths": {
        "/v3/{project_id}/instances/{instance_id}/action/startup": {
            "post": {"summary": "开启实例", "x-constraint": STARTUP_XCONSTRAINT},
        },
    }}


# ---------- parse：schema 严格校验 ----------

def test_empty_is_noop():
    c = MetadataCorrections.empty()
    assert c.for_api("RDS", "StartupInstance") is None
    doc = {"paths": {}}
    assert correct_doc(doc, "RDS", "StartupInstance", c) is doc


def test_parse_minimal_drop_entry():
    c = parse_metadata_corrections(RAW)
    e = c.for_api("RDS", "StartupInstance")
    assert e is not None
    assert e.evidence == RAW["RDS:StartupInstance"]["evidence"]
    assert e.doc_url == RAW["RDS:StartupInstance"]["doc_url"]
    patch = e.patches["x-constraint"]
    assert patch.drop == ("该接口仅支持PostgreSQL引擎", "如需开通开启实例的权限")
    assert patch.replace is None


def test_parse_key_casefold():
    c = parse_metadata_corrections({"rds:STARTUPinstance": {"patches": {
        "x-constraint": {"replace": "x"}}}})
    assert c.for_api("RDS", "startupINSTANCE") is not None
    assert c.for_api("RDS", "Other") is None


def test_parse_replace_form():
    c = parse_metadata_corrections({"ECS:Foo": {"patches": {
        "x-constraint": {"replace": "- 官方约束。"}}}})
    e = c.for_api("ECS", "Foo")
    assert e is not None
    assert e.patches["x-constraint"].replace == "- 官方约束。"


def test_parse_without_evidence_fields():
    c = parse_metadata_corrections({"RDS:A": {"patches": {
        "x-constraint": {"drop": ["x"]}}}})
    e = c.for_api("RDS", "A")
    assert e is not None
    assert e.evidence is None and e.doc_url is None


def test_parse_invalid_raises():
    bad = [
        "nope",                                        # 非 mapping
        {"RDS:A": 1},                                  # 条目非 mapping
        {"RDS:A": {}},                                 # 缺 patches
        {"RDS:A": {"patches": {}}},                    # patches 空
        {"RDSA": {"patches": {"x-constraint": {"drop": ["x"]}}}},   # 缺冒号
        {":A": {"patches": {"x-constraint": {"drop": ["x"]}}}},     # 产品空
        {"RDS:": {"patches": {"x-constraint": {"drop": ["x"]}}}},   # API 空
        {1: {"patches": {"x-constraint": {"drop": ["x"]}}}},        # 键非字符串
        {"RDS:A": {"unknown": 1, "patches": {"x-constraint": {"drop": ["x"]}}}},  # 未知条目键
        {"RDS:A": {"patches": {"description": {"drop": ["x"]}}}},   # 字段白名单外
        {"RDS:A": {"patches": {"x-constraint": {"drop": ["x"], "replace": "y"}}}},  # 二选一
        {"RDS:A": {"patches": {"x-constraint": {}}}},               # patch 空
        {"RDS:A": {"patches": {"x-constraint": {"drop": []}}}},     # drop 空列表
        {"RDS:A": {"patches": {"x-constraint": {"drop": [""]}}}},   # drop 空子串
        {"RDS:A": {"patches": {"x-constraint": {"drop": [1]}}}},    # drop 非字符串
        {"RDS:A": {"patches": {"x-constraint": {"replace": ""}}}},  # replace 空串
        {"RDS:A": {"patches": {"x-constraint": {"replace": 1}}}},   # replace 非字符串
        {"RDS:A": {"evidence": 1, "patches": {"x-constraint": {"drop": ["x"]}}}},   # evidence 非字符串
        {"RDS:A": {"doc_url": 1, "patches": {"x-constraint": {"drop": ["x"]}}}},    # doc_url 非字符串
        {"RDS:A": {"verified": 1, "patches": {"x-constraint": {"drop": ["x"]}}}},   # verified 非字符串
    ]
    for raw in bad:
        with pytest.raises(ValueError):
            parse_metadata_corrections(raw)


# ---------- FieldPatch.apply：字符串级核心 ----------

def test_patch_drop_removes_stale_line_byte_preserves_rest():
    c = parse_metadata_corrections(RAW)
    e = c.for_api("RDS", "StartupInstance")
    assert e is not None
    assert e.patches["x-constraint"].apply(STARTUP_XCONSTRAINT) == STARTUP_REMAINDER


def test_patch_drop_all_lines_returns_none():
    patch = parse_metadata_corrections({"RDS:A": {"patches": {
        "x-constraint": {"drop": ["仅支持PostgreSQL"]}}}}
    ).for_api("RDS", "A")
    assert patch is not None
    assert patch.patches["x-constraint"].apply("- 该接口仅支持PostgreSQL引擎。") is None


def test_patch_drop_no_hit_returns_value_unchanged():
    patch = parse_metadata_corrections({"RDS:A": {"patches": {
        "x-constraint": {"drop": ["不存在"]}}}}).for_api("RDS", "A")
    assert patch is not None
    assert patch.patches["x-constraint"].apply("- 合法约束。") == "- 合法约束。"


def test_patch_idempotent():
    c = parse_metadata_corrections(RAW)
    e = c.for_api("RDS", "StartupInstance")
    assert e is not None
    once = e.patches["x-constraint"].apply(STARTUP_XCONSTRAINT)
    assert once is not None
    assert e.patches["x-constraint"].apply(once) == STARTUP_REMAINDER


# ---------- correct_doc：doc 级（离线管道组合根） ----------

def test_correct_doc_drop():
    c = parse_metadata_corrections(RAW)
    doc = _doc()
    out = correct_doc(doc, "RDS", "StartupInstance", c)
    op = out["paths"]["/v3/{project_id}/instances/{instance_id}/action/startup"]["post"]
    assert op["x-constraint"] == STARTUP_REMAINDER
    assert op["summary"] == "开启实例"


def test_correct_doc_all_dropped_pops_key():
    c = parse_metadata_corrections({"RDS:StartupInstance": {"patches": {
        "x-constraint": {"drop": ["该接口仅支持PostgreSQL引擎"]}}}})
    doc = _doc()
    doc["paths"]["/v3/{p}/action/startup"] = doc["paths"].pop(
        "/v3/{project_id}/instances/{instance_id}/action/startup")
    op = doc["paths"]["/v3/{p}/action/startup"]["post"]
    op["x-constraint"] = "- 该接口仅支持PostgreSQL引擎。"
    out = correct_doc(doc, "RDS", "StartupInstance", c)
    popped = out["paths"]["/v3/{p}/action/startup"]["post"]
    assert "x-constraint" not in popped


def test_correct_doc_multi_method_and_miss():
    raw = {"RDS:StartupInstance": {"patches": {
        "x-constraint": {"replace": "- 官方约束。"}}}}
    c = parse_metadata_corrections(raw)
    doc = {"paths": {"/p": {
        "post": {"x-constraint": "- 旧"},
        "get": {"x-constraint": "- 旧"},
        "delete": {"summary": "无约束字段"},
    }}}
    out = correct_doc(doc, "rds", "startupinstance", c)
    assert out["paths"]["/p"]["post"]["x-constraint"] == "- 官方约束。"
    assert out["paths"]["/p"]["get"]["x-constraint"] == "- 官方约束。"
    assert out["paths"]["/p"]["delete"]["x-constraint"] == "- 官方约束。"  # replace 无条件落位，含缺失新建

    miss_doc = _doc()
    assert correct_doc(miss_doc, "ECS", "ListServersDetails", c) == _doc()


def test_correct_doc_replace_creates_absent_field():
    c = parse_metadata_corrections({"RDS:StartupInstance": {"patches": {
        "x-constraint": {"replace": "- 官方约束。"}}}})
    doc = {"paths": {"/p": {"post": {"summary": "s"}}}}
    out = correct_doc(doc, "RDS", "StartupInstance", c)
    assert out["paths"]["/p"]["post"]["x-constraint"] == "- 官方约束。"


# ---------- 指针 patch：doc 级 JSON Pointer（v1 前缀白名单 /definitions/） ----------

BROKEN_PATTERN = "$[a-zA-Z][a-zA-Z0-9-_]*"
DOC_PATTERN_TRUTH = "^[a-zA-Z]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$"

FG_RAW = {
    "FunctionGraph:CreateEvent": {
        "patches": {
            "/definitions/CreateEventRequestBody/properties/name/pattern": {
                "replace": DOC_PATTERN_TRUTH},
        },
    },
}


def _fg_doc() -> dict:
    return {
        "definitions": {
            "CreateEventRequestBody": {
                "required": ["content", "name"],
                "properties": {
                    "name": {"type": "string", "pattern": BROKEN_PATTERN},
                    "content": {"type": "string"},
                },
            },
            "OtherDef": {"type": "object"},
        },
        "paths": {"/v2/{p}/fgs/functions/{urn}/events": {"post": {
            "operationId": "CreateEvent", "summary": "创建测试事件",
            "parameters": [],
        }}},
    }


def test_parse_pointer_key_parses_segments():
    c = parse_metadata_corrections(FG_RAW)
    e = c.for_api("functiongraph", "createevent")
    assert e is not None
    (ptr, patch), = e.doc_patches.items()
    assert ptr == ("definitions", "CreateEventRequestBody",
                   "properties", "name", "pattern")
    assert patch.replace == DOC_PATTERN_TRUTH and patch.drop is None
    assert e.patches == {}


def test_parse_pointer_and_op_level_coexist():
    c = parse_metadata_corrections({"RDS:StartupInstance": {"patches": {
        "x-constraint": {"drop": ["过期行"]},
        "/definitions/Body/properties/name/pattern": {"replace": "^a$"},
    }}})
    e = c.for_api("RDS", "StartupInstance")
    assert e is not None
    assert set(e.patches) == {"x-constraint"}
    (ptr, patch), = e.doc_patches.items()
    assert ptr == ("definitions", "Body", "properties", "name", "pattern")
    assert patch.replace == "^a$"


def test_parse_pointer_drop_boolean_deletes_leaf():
    e = parse_metadata_corrections({"RDS:A": {"patches": {
        "/definitions/X/properties/pattern": {"drop": True}}}}).for_api("RDS", "A")
    assert e is not None
    (ptr, patch), = e.doc_patches.items()
    assert ptr == ("definitions", "X", "properties", "pattern")
    assert patch.drop == () and patch.replace is None


def test_parse_pointer_rejects_bad_shapes():
    bad = [
        "/paths/~1v2/post/x-constraint",   # 前缀白名单外
        "/components/schemas/X",           # 前缀白名单外
        "definitions/X",                   # 不以 / 开头 → op 级字段白名单外
        "/definitions/",                   # 尾空段
        "/definitions//properties/x",      # 中空段
        {"RDS:A": {"patches": {"/definitions/X/p": {"drop": ["x"]}}}},   # 指针 drop 非布尔
        {"RDS:A": {"patches": {"/definitions/X/p": {"drop": False}}}},   # 指针 drop 非布尔
        {"RDS:A": {"patches": {"/definitions/X/p": {"replace": ""}}}},   # replace 空串
        {"RDS:A": {"patches": {"/definitions/X/p": {"replace": 1}}}},    # replace 非字符串
        {"RDS:A": {"patches": {"/definitions/X/p": {}}}},                # patch 空
    ]
    for item in bad:
        raw = item if isinstance(item, dict) else {"RDS:A": {"patches": {item: {"replace": "x"}}}}
        with pytest.raises(ValueError):
            parse_metadata_corrections(raw)


def test_parse_pointer_unescape_rfc6901():
    e = parse_metadata_corrections({"RDS:A": {"patches": {
        "/definitions/A~1B~0C/properties/x": {"replace": "v"}}}}).for_api("RDS", "A")
    assert e is not None
    (ptr, _), = e.doc_patches.items()
    assert ptr == ("definitions", "A/B~C", "properties", "x")


# ---------- correct_doc：指针 patch（离线 in-place，每 doc 应用一次） ----------

def test_correct_doc_pointer_replace_inplace():
    c = parse_metadata_corrections(FG_RAW)
    doc = _fg_doc()
    out = correct_doc(doc, "FunctionGraph", "CreateEvent", c)
    assert out is doc  # 离线 in-place
    assert (out["definitions"]["CreateEventRequestBody"]["properties"]["name"]["pattern"]
            == DOC_PATTERN_TRUTH)
    assert out["definitions"]["OtherDef"] == {"type": "object"}  # 兄弟原样


def test_correct_doc_pointer_idempotent():
    c = parse_metadata_corrections(FG_RAW)
    doc = _fg_doc()
    correct_doc(doc, "FunctionGraph", "CreateEvent", c)
    snap = json.dumps(doc, sort_keys=True)
    correct_doc(doc, "FunctionGraph", "CreateEvent", c)
    assert json.dumps(doc, sort_keys=True) == snap


def test_correct_doc_pointer_drop_deletes_leaf():
    c = parse_metadata_corrections({"FunctionGraph:CreateEvent": {"patches": {
        "/definitions/CreateEventRequestBody/properties/name/pattern": {"drop": True}}}})
    doc = _fg_doc()
    correct_doc(doc, "FunctionGraph", "CreateEvent", c)
    name = doc["definitions"]["CreateEventRequestBody"]["properties"]["name"]
    assert "pattern" not in name
    assert name["type"] == "string"


def test_correct_doc_pointer_missing_parent_noop():
    c = parse_metadata_corrections({"RDS:A": {"patches": {
        "/definitions/NoSuch/properties/x": {"replace": "v"}}}})
    doc = {"definitions": {}, "paths": {}}
    out = correct_doc(doc, "RDS", "A", c)
    assert out == {"definitions": {}, "paths": {}}


def test_correct_doc_pointer_and_op_level_combined():
    doc = _fg_doc()
    doc["paths"]["/v2/{p}/fgs/functions/{urn}/events"]["post"]["x-constraint"] = "- 过期行"
    c = parse_metadata_corrections({"FunctionGraph:CreateEvent": {"patches": {
        "x-constraint": {"drop": ["过期行"]},
        "/definitions/CreateEventRequestBody/properties/name/pattern": {
            "replace": DOC_PATTERN_TRUTH},
    }}})
    out = correct_doc(doc, "FunctionGraph", "CreateEvent", c)
    op = out["paths"]["/v2/{p}/fgs/functions/{urn}/events"]["post"]
    assert "x-constraint" not in op  # 行级删光从 op 移除键（既有 op 级离线语义）
    assert (out["definitions"]["CreateEventRequestBody"]["properties"]["name"]["pattern"]
            == DOC_PATTERN_TRUTH)


def test_load_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    c = load_metadata_corrections(str(p))
    assert c.for_api("RDS", "StartupInstance") is not None


def test_load_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_metadata_corrections(str(p))


def test_load_bare_name_resolves_repo_configs(tmp_path, monkeypatch):
    from common import paths

    cfg = tmp_path / "configs" / "deploy-corrections.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert load_metadata_corrections("deploy-corrections.json"
                                     ).for_api("RDS", "StartupInstance") is not None


def test_load_missing_everywhere_raises(tmp_path, monkeypatch):
    from common import paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    with pytest.raises(FileNotFoundError):
        load_metadata_corrections("missing.json")


def test_load_none_default_loads_configs_file(tmp_path, monkeypatch):
    """None → 缺省档：configs/metadata-corrections.json。"""
    from common import paths

    cfg = tmp_path / "configs" / "metadata-corrections.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert load_metadata_corrections(None).for_api("RDS", "StartupInstance") is not None


def test_load_none_missing_silent_empty(tmp_path, monkeypatch):
    from common import paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    c = load_metadata_corrections(None)
    assert c.for_api("RDS", "StartupInstance") is None


def test_load_off_disables_even_when_file_present(tmp_path, monkeypatch):
    from common import paths

    cfg = tmp_path / "configs" / "metadata-corrections.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert load_metadata_corrections("off").for_api("RDS", "StartupInstance") is None
    assert load_metadata_corrections(" OFF ").for_api("RDS", "StartupInstance") is None
    assert load_metadata_corrections("").for_api("RDS", "StartupInstance") is None


def test_load_real_repo_shipped_file(monkeypatch):
    """真实仓库布局 + 任意 cwd：shipped configs/metadata-corrections.json 加载。"""
    monkeypatch.chdir(tempfile.mkdtemp())
    c = load_metadata_corrections(None)
    e = c.for_api("RDS", "StartupInstance")
    assert e is not None
    assert "该接口仅支持PostgreSQL引擎" in (e.patches["x-constraint"].drop or ())
    assert e.doc_url is not None
    fg = c.for_api("FunctionGraph", "CreateEvent")
    assert fg is not None
    (ptr, patch), = fg.doc_patches.items()
    assert ptr == ("definitions", "CreateEventRequestBody",
                   "properties", "name", "pattern")
    assert patch.replace == "^[a-zA-Z]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$"


# ---------- 离线管道组合根：convert main() 组合 correct_doc ----------

def test_convert_main_applies_shipped_corrections(tmp_path, monkeypatch):
    """api-refresh convert 组合根：shipped 纠偏随离线产物同源生效。"""
    from apie import convert_openapi2 as conv

    raw = {"product_short": "RDS", "tag": "实例管理", "apis": {
        "StartupInstance": {
            "product_short": "RDS", "name": "StartupInstance",
            "swagger": "2.0", "host": "rds.cn-north-4.myhuaweicloud.com",
            "paths": {"/v3/{p}/action/startup": {"post": {
                "operationId": "StartupInstance",
                "x-constraint": STARTUP_XCONSTRAINT}}},
        },
    }}
    src, out = tmp_path / "by_tag", tmp_path / "openapi2"
    (src / "RDS").mkdir(parents=True)
    (src / "RDS" / "实例管理.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    from apie import region_paths

    monkeypatch.setattr(region_paths, "by_tag_dir", lambda r=None: str(src))
    monkeypatch.setattr(region_paths, "openapi2_dir", lambda r=None: str(out))

    conv.main()

    doc = json.loads((out / "RDS" / "实例管理.json").read_text(encoding="utf-8"))
    op = doc["apis"]["StartupInstance"]["paths"]["/v3/{p}/action/startup"]["post"]
    assert op["x-constraint"] == STARTUP_REMAINDER
