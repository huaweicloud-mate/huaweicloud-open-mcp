"""S10a：openapi 自定义提示注入 Hints 纯函数单测（parse/load/lookup/合并策略）。"""

import tempfile

import pytest

from mcp_openapi.hints import Hints, load_hints_file, parse_hints

RAW = {
    "instructions": "全局指引",
    "products": {
        "ECS": "产品级提示",
        "VPC": {
            "notes": "VPC 提示",
            "apis": {
                "ListServersDetails": "API 级提示",
                "CreateVpc": "创建提示",
            },
        },
    },
}


def test_empty_is_noop():
    h = Hints.empty()
    assert h.instructions is None
    assert h.product_notes("ECS") is None
    assert h.api_notes("ECS", "ListServersDetails") is None
    assert h.combined_notes("ECS", "ListServersDetails") is None


def test_parse_global_instructions():
    h = parse_hints({"instructions": "全局指引"})
    assert h.instructions == "全局指引"


def test_parse_instructions_empty_is_absent():
    h = parse_hints({"instructions": ""})
    assert h.instructions is None


def test_parse_product_string_shorthand():
    h = parse_hints({"products": {"ECS": "产品级提示"}})
    assert h.product_notes("ecs") == "产品级提示"
    assert h.product_notes("VPC") is None


def test_parse_product_object_form():
    h = parse_hints(RAW)
    assert h.product_notes("ECS") == "产品级提示"
    assert h.product_notes("vpc") == "VPC 提示"
    assert h.product_notes("OBS") is None


def test_product_notes_empty_string_is_absent():
    h = parse_hints({"products": {"ECS": ""}})
    assert h.product_notes("ECS") is None


def test_api_notes_api_level_only_and_case_insensitive():
    h = parse_hints(RAW)
    assert h.api_notes("VPC", "listserversdetails") == "API 级提示"
    assert h.api_notes("vpc", "ListServersDetails") == "API 级提示"
    assert h.api_notes("VPC", "DeleteVpc") is None
    assert h.api_notes("ECS", "ListServersDetails") is None


def test_combined_notes_merge_policy():
    h = parse_hints(RAW)
    assert h.combined_notes("vpc", "CreateVpc") == "VPC 提示\n创建提示"
    assert h.combined_notes("ECS", "Any") == "产品级提示"
    assert h.combined_notes("VPC", "Unknown") == "VPC 提示"
    assert h.combined_notes("OBS", "Any") is None


def test_combined_notes_product_without_notes_but_apis():
    h = parse_hints({"products": {"VPC": {"apis": {"A": "x"}}}})
    assert h.product_notes("VPC") is None
    assert h.combined_notes("VPC", "a") == "x"


def test_parse_minimal_form_is_noop():
    h = parse_hints({})
    assert h.instructions is None
    assert h.product_notes("ECS") is None


# ---------- S10a 扩展：产品级 SOP（sops，mapping-only，parse 期渲染） ----------

def test_parse_sops_mapping_with_string_value():
    h = parse_hints({"products": {"ECS": {"sops": {
        "变更规格": "ShowServer → ResizeServer → ConfirmResize"}}}})
    assert h.product_sops("ECS") == \
        "变更规格：\nShowServer → ResizeServer → ConfirmResize"


def test_parse_sops_mapping_with_array_value_auto_numbering():
    h = parse_hints({"products": {"ECS": {"sops": {
        "创建云服务器": ["ListFlavors 查规格", "CreateServers 创建",
                         "ShowServer 轮询 ACTIVE"]}}}})
    assert h.product_sops("ECS") == (
        "创建云服务器：\n1. ListFlavors 查规格\n2. CreateServers 创建"
        "\n3. ShowServer 轮询 ACTIVE")


def test_parse_sops_multi_task_config_order_and_blank_line():
    h = parse_hints({"products": {"ECS": {"sops": {
        "创建云服务器": "1) ListFlavors → 2) CreateServers",
        "变更规格": ["ShowServer 确认", "ResizeServer 提交"]}}}})
    assert h.product_sops("ECS") == (
        "创建云服务器：\n1) ListFlavors → 2) CreateServers\n\n"
        "变更规格：\n1. ShowServer 确认\n2. ResizeServer 提交")


def test_parse_sops_blank_steps_dropped_and_numbering_compacted():
    h = parse_hints({"products": {"ECS": {"sops": {
        "任务": ["步骤一", "  ", "步骤二"]}}}})
    assert h.product_sops("ECS") == "任务：\n1. 步骤一\n2. 步骤二"


def test_parse_sops_blank_task_dropped_all_blank_is_absent():
    h = parse_hints({"products": {"ECS": {"sops": {
        "空任务": "  ", "正常任务": "步骤"}}}})
    assert h.product_sops("ECS") == "正常任务：\n步骤"
    h2 = parse_hints({"products": {"ECS": {"sops": {"空任务": "  "}}}})
    assert h2.product_sops("ECS") is None


def test_product_sops_product_key_case_insensitive():
    h = parse_hints({"products": {"ECS": {"sops": {"任务": "步骤"}}}})
    assert h.product_sops("ecs") == "任务：\n步骤"
    assert h.product_sops("OBS") is None


def test_product_sops_empty_hints_is_noop():
    assert Hints.empty().product_sops("ECS") is None


def test_parse_sops_string_shorthand_has_no_sops():
    h = parse_hints({"products": {"OBS": "产品级提示"}})
    assert h.product_sops("OBS") is None


def test_parse_sops_and_notes_coexist():
    h = parse_hints({"products": {"ECS": {
        "notes": "产品提示", "sops": {"任务": "步骤"}}}})
    assert h.product_notes("ECS") == "产品提示"
    assert h.product_sops("ECS") == "任务：\n步骤"


def test_parse_sops_invalid_raises():
    with pytest.raises(ValueError):   # sops 非 mapping（string 简写不收录）
        parse_hints({"products": {"ECS": {"sops": "文本"}}})
    with pytest.raises(ValueError):   # sops 非 mapping（标量）
        parse_hints({"products": {"ECS": {"sops": 1}}})
    with pytest.raises(ValueError):   # 任务名空串
        parse_hints({"products": {"ECS": {"sops": {"": "x"}}}})
    with pytest.raises(ValueError):   # 任务值非法标量
        parse_hints({"products": {"ECS": {"sops": {"任务": 1}}}})
    with pytest.raises(ValueError):   # 步骤非字符串
        parse_hints({"products": {"ECS": {"sops": {"任务": ["a", 2]}}}})
    with pytest.raises(ValueError):   # 任务值嵌套 dict
        parse_hints({"products": {"ECS": {"sops": {"任务": {"嵌套": "dict"}}}}})
    with pytest.raises(ValueError):   # 未知键仍 fail-fast（单数拼写 typo）
        parse_hints({"products": {"ECS": {"sop": {"任务": "步骤"}}}})


# ---------- S13f：api_notes_in_list_apis 开关（缺省 true = 现状） ----------

def test_parse_flag_default_true():
    h = parse_hints({"products": {"ECS": {"apis": {"A": "x"}}}})
    assert h.api_notes_in_list_apis is True


def test_parse_flag_explicit_false():
    h = parse_hints({"api_notes_in_list_apis": False, "products": {}})
    assert h.api_notes_in_list_apis is False


def test_parse_flag_non_bool_raises():
    with pytest.raises(ValueError):
        parse_hints({"api_notes_in_list_apis": "no"})


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_hints("nope")
    with pytest.raises(ValueError):
        parse_hints({"unknown": 1})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": 1}})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": {"unknown": 1}}})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": {"notes": 1}}})
    with pytest.raises(ValueError):
        parse_hints({"products": {"ECS": {"apis": {"A": 1}}}})


def test_load_hints_file(tmp_path):
    p = tmp_path / "h.json"
    p.write_text('{"instructions": "g", "products": {"ECS": "n"}}', encoding="utf-8")
    h = load_hints_file(str(p))
    assert h.instructions == "g"
    assert h.product_notes("ECS") == "n"


def test_load_hints_file_bare_name_resolves_repo_configs(tmp_path, monkeypatch):
    """裸文件名经 config_path 解析：仓库根 configs/ 优先（S0 委派，经 loader 接口验证）。"""
    from common import paths

    cfg = tmp_path / "configs" / "deploy-hints.json"
    cfg.parent.mkdir()
    cfg.write_text('{"instructions": "裸名解析"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # cwd 无同名文件
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert load_hints_file("deploy-hints.json").instructions == "裸名解析"


def test_load_hints_file_bare_name_real_repo_layout(monkeypatch):
    """真实仓库布局 + 任意 cwd：包内随附示例 configs/openapi-hints.example.json 可裸名加载。"""
    monkeypatch.chdir(tempfile.mkdtemp())
    h = load_hints_file("openapi-hints.example.json")
    assert isinstance(h, Hints)


def test_load_hints_file_missing_everywhere_raises(tmp_path, monkeypatch):
    """全缺失 fail-fast：FileNotFoundError 含全部尝试路径（显式 + 两条 configs 候选）。"""
    from common import paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    with pytest.raises(FileNotFoundError) as excinfo:
        load_hints_file("missing.json")
    msg = str(excinfo.value)
    assert str(tmp_path / "repo" / "configs" / "missing.json") in msg
    assert "huaweicloud_open_mcp" in msg


def test_load_hints_file_none_default_loads_configs_file(tmp_path, monkeypatch):
    """None（--hints/env 均未配置）→ 缺省档：configs/help-docs-hints.json 加载。"""
    from common import paths

    cfg = tmp_path / "configs" / "help-docs-hints.json"
    cfg.parent.mkdir()
    cfg.write_text('{"instructions": "缺省档"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # cwd 无同名文件
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert load_hints_file(None).instructions == "缺省档"


def test_load_hints_file_none_missing_silent_empty(tmp_path, monkeypatch):
    """None + 缺省文件全缺失 → 静默 Hints.empty()（隐式缺省不 fail-fast）。"""
    from common import paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    h = load_hints_file(None)
    assert h.instructions is None
    assert h.product_notes("ECS") is None


def test_load_hints_file_off_disables_even_when_file_present(tmp_path, monkeypatch):
    """显式 "off"（strip + 大小写不敏感，对齐 spill idiom）→ 禁用，优先于缺省文件存在。"""
    from common import paths

    cfg = tmp_path / "configs" / "help-docs-hints.json"
    cfg.parent.mkdir()
    cfg.write_text('{"instructions": "不应加载"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert load_hints_file("off").instructions is None
    assert load_hints_file(" OFF ").instructions is None


def test_load_hints_file_real_repo_default_loads(monkeypatch):
    """真实仓库布局 + 任意 cwd：缺省档加载已入库 configs/help-docs-hints.json。"""
    monkeypatch.chdir(tempfile.mkdtemp())
    h = load_hints_file(None)
    assert isinstance(h, Hints)
    assert h.instructions is not None                 # 该文件 instructions 非空


def test_load_hints_file_empty_path_is_empty():
    assert load_hints_file("").instructions is None
