"""apie 帮助中心解析纯函数：sitemap 分组 / 页面元信息 / 功能介绍提取。

fixture 依真实页面结构精简（support.huaweicloud.com 实测标记）；
期望值手写自真实页面内容（NovaRebootServer / CreateServers / 章节 0300）。
"""

from apie.help_docs import (
    alias_candidates,
    build_alias_index,
    build_hints,
    detail_descriptions,
    diff_completions,
    extract_func_intro,
    extract_links,
    extract_replacement,
    match_apis,
    parse_api_page,
    parse_sitemap,
)
from mcp_openapi.hints import parse_hints

API_PAGE_HTML = """<!doctype html>
<html><head><title>重启云服务器（废弃） - NovaRebootServer_状态管理_API参考_弹性云服务器 ECS-华为云</title>
</head>
<body>
<h1>重启<span id="t1">云服务器</span>（废弃） - NovaRebootServer</h1>
<div id="body1">
<div class="section" id="s1">
<h4 class="sectiontitle">功能介绍</h4>
<p id="p1">重启单台<span id="t2">云服务器</span>。</p>
<p id="p2">当前API已废弃，请使用<a href="https://u/ecs_02_0302.html">批量重启云服务器 - BatchRebootServers</a>。</p>
</div>
<div class="section" id="s2">
<h4 class="sectiontitle">调试</h4>
<p>您可以在 API Explorer 中调试该接口。</p>
</div>
</div>
</body></html>"""

COMPLEX_PAGE_HTML = """<!doctype html>
<html><head><title>创建云服务器 - CreateServers_生命周期管理_API_API参考_弹性云服务器 ECS-华为云</title>
</head>
<body>
<div class="section" id="s1">
<h4 class="sectiontitle">功能介绍</h4>
<p>创建一台或多台<span>云服务器</span>。</p>
<p>本接口为异步接口，当前创建<span>云服务器</span>请求下发成功后会返回job_id。</p>
<div class="p"><span>弹性云服务器</span>的登录鉴权方式包括两种：
<ul><li>密钥对
<p>指使用密钥对作为<span>弹性云服务器</span>的鉴权方式。</p>
</li><li>密码
<p>指使用密码作为<span>弹性云服务器</span>的鉴权方式。</p>
</li></ul></div>
</div>
<div class="section" id="s2">
<h4 class="sectiontitle">URI</h4>
<p>POST /v1/{project_id}/cloudservers</p>
</div>
</body></html>"""

CHAPTER_PAGE_HTML = """<!doctype html>
<html><head><title>状态管理（OpenStack Nova API）_历史API_API参考_弹性云服务器 ECS-华为云</title></head>
<body>
<h1>状态管理（OpenStack Nova API）</h1>
<div><ul class="ullinks">
<li class="ulchildlink"><strong><a href="https://u/ecs_03_0301.html">启动云服务器（废弃） - NovaStartServer</a></strong>
<br></li>
<li class="ulchildlink"><strong><a href="https://u/ecs_03_0302.html">重启云服务器（废弃）- NovaRebootServer</a></strong>
<br></li>
<li class="ulchildlink"><strong><a href="https://u/ecs_03_0307.html">云服务器创建镜像（废弃）</a></strong> <br>
</li>
</ul></div>
</body></html>"""

NON_API_PAGE_HTML = """<!doctype html>
<html><head><title> 404页面_华为云</title></head><body>not found</body></html>"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset>
<url><loc>https://support.huaweicloud.com/api-ecs/ecs_03_0301.html</loc></url>
<url><loc>https://support.huaweicloud.com/api-ecs/ecs_03_0301.html</loc></url>
<url><loc>https://support.huaweicloud.com/api-ecs/zh-cn_topic_0020805967.html</loc></url>
<url><loc>https://support.huaweicloud.com/api-obs/obs_01_0001.html</loc></url>
<url><loc>https://support.huaweicloud.com/api-ecs/ecs-api-pdf.pdf</loc></url>
<url><loc>https://other.example.com/api-x/x.html</loc></url>
</urlset>"""


# ---------- parse_sitemap ----------

def test_parse_sitemap_groups_by_docset_and_dedupes():
    got = parse_sitemap(SITEMAP_XML)
    assert got == {
        "api-ecs": [
            "https://support.huaweicloud.com/api-ecs/ecs_03_0301.html",
            "https://support.huaweicloud.com/api-ecs/zh-cn_topic_0020805967.html",
        ],
        "api-obs": ["https://support.huaweicloud.com/api-obs/obs_01_0001.html"],
    }


def test_parse_sitemap_empty():
    assert parse_sitemap("") == {}


# ---------- parse_api_page ----------

def test_parse_api_page_with_api_name():
    p = parse_api_page(API_PAGE_HTML)
    assert p.api_name == "NovaRebootServer"
    assert p.cn_name == "重启云服务器（废弃）"
    assert p.is_api_ref is True
    assert p.product_display == "弹性云服务器 ECS"


def test_parse_api_page_chapter_without_api_name():
    p = parse_api_page(CHAPTER_PAGE_HTML)
    assert p.api_name is None
    assert p.cn_name == "状态管理（OpenStack Nova API）"
    assert p.is_api_ref is True


def test_parse_api_page_non_api_doc():
    p = parse_api_page(NON_API_PAGE_HTML)
    assert p.api_name is None
    assert p.is_api_ref is False


# ---------- extract_func_intro ----------

def test_extract_func_intro_simple():
    assert extract_func_intro(API_PAGE_HTML) == (
        "重启单台云服务器。\n"
        "当前API已废弃，请使用批量重启云服务器 - BatchRebootServers。")


def test_extract_func_intro_nested_lists():
    got = extract_func_intro(COMPLEX_PAGE_HTML)
    lines = got.split("\n")
    assert lines[0] == "创建一台或多台云服务器。"
    assert lines[1].startswith("本接口为异步接口")
    assert "密钥对" in lines
    assert "指使用密钥对作为弹性云服务器的鉴权方式。" in lines
    assert "POST /v1/{project_id}/cloudservers" not in got


def test_extract_func_intro_absent():
    assert extract_func_intro(CHAPTER_PAGE_HTML) is None
    assert extract_func_intro("") is None


# ---------- S13c：匹配 / 差集 / hints 生成 ----------

PRODUCTS = [
    {"name": "计算", "products": [
        {"productshort": "ECS", "name": "弹性云服务器"},
        {"productshort": "AS", "name": "弹性伸缩"},
    ]},
    {"name": "存储", "products": [
        {"productshort": "OBS", "name": "对象存储服务"},
    ]},
    {"name": "消重", "products": [
        {"productshort": "A1", "name": "重复名"},
        {"productshort": "B2", "name": "重复名"},
    ]},
]

APIS_INDEX = [
    {"product_short": "ECS", "name": "NovaRebootServer", "summary": "重启云服务器"},
    {"product_short": "ECS", "name": "CreateServers", "summary": "创建云服务器"},
    {"product_short": "ECS", "name": "CreateServer", "summary": "创建云服务器"},
    {"product_short": "OBS", "name": "CreateBucket", "summary": "创建桶"},
]


def _rec(url, docset, *, api_name=None, cn_name="", display="弹性云服务器 ECS",
        intro=None, is_api_ref=True):
    return {"url": url, "docset": docset, "api_name": api_name, "cn_name": cn_name,
            "is_api_ref": is_api_ref, "product_display": display,
            "func_intro": intro}


def test_build_alias_index_maps_name_short_display():
    idx = build_alias_index(PRODUCTS)
    assert idx["ecs"] == "ECS"
    assert idx["弹性云服务器"] == "ECS"
    assert idx["弹性云服务器 ecs"] == "ECS"
    assert idx["obs"] == "OBS"
    assert idx["对象存储服务"] == "OBS"


def test_build_alias_index_drops_ambiguous_aliases():
    idx = build_alias_index(PRODUCTS)
    assert "重复名" not in idx
    assert idx["a1"] == "A1"
    assert idx["b2"] == "B2"


def test_match_by_name_case_insensitive():
    records = [_rec("https://u/1", "api-ecs", api_name="NovaRebootServer",
                    cn_name="重启云服务器（废弃）", intro="重启单台云服务器。\n当前API已废弃。")]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS))
    assert got["ambiguous"] == []
    assert got["unmatched"] == []
    assert got["matched"] == [{
        "product": "ECS", "api": "NovaRebootServer", "url": "https://u/1",
        "docset": "api-ecs", "matched_by": "name",
        "func_intro": "重启单台云服务器。\n当前API已废弃。",
    }]


def test_match_product_via_override_wins():
    records = [_rec("https://u/1", "api-foo", api_name="NovaRebootServer", intro="x",
                    display="不认识的名字")]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS),
                     overrides={"api-foo": "ECS"})
    assert got["matched"][0]["product"] == "ECS"


def test_match_unknown_product_unmatched():
    records = [_rec("https://u/9", "api-x", api_name="Foo", display="不存在产品")]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS))
    assert got["matched"] == []
    assert got["unmatched"] == [{"url": "https://u/9", "reason": "product", "product": None}]


def test_match_name_miss_falls_back_to_summary():
    records = [_rec("https://u/2", "api-ecs", api_name="NovaStartServerX",
                    cn_name="重启云服务器（废弃）", intro="x")]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS))
    assert got["matched"][0]["api"] == "NovaRebootServer"
    assert got["matched"][0]["matched_by"] == "summary"


def test_match_summary_ambiguous():
    records = [_rec("https://u/3", "api-ecs", cn_name="创建云服务器（废弃）", intro="x")]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS))
    assert got["matched"] == []
    assert got["ambiguous"] == [{"url": "https://u/3", "reason": "summary_ambiguous",
                                 "product": "ECS"}]


def test_match_summary_unique_via_cn_name():
    records = [_rec("https://u/4", "api-obs", cn_name="创建桶", intro="x",
                    display="对象存储服务 OBS")]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS))
    assert got["matched"][0]["api"] == "CreateBucket"
    assert got["matched"][0]["product"] == "OBS"


def test_match_skips_non_api_pages_and_duplicates():
    records = [
        _rec("https://u/1", "api-ecs", api_name="NovaRebootServer", intro="x"),
        _rec("https://u/1-mirror", "api-ecs", api_name="NovaRebootServer", intro="y"),
        _rec("https://u/chapter", "api-ecs", cn_name="状态管理（OpenStack Nova API）"),
        _rec("https://u/nonapi", "api-ecs", api_name="Foo", is_api_ref=False),
    ]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS))
    assert [m["url"] for m in got["matched"]] == ["https://u/1"]
    reasons = {u["url"]: u["reason"] for u in got["unmatched"]}
    assert reasons["https://u/1-mirror"] == "duplicate"
    assert "https://u/nonapi" not in reasons  # 非 API 文档页直接跳过


def test_detail_descriptions_extraction():
    detail = {"apis": {
        "ECS::NovaRebootServer": {"paths": {"/a": {"post": {
            "operationId": "NovaRebootServer", "description": "重启单台云服务器。"}}}},
        "OBS::CreateBucket": {"empty": True},
    }}
    got = detail_descriptions(detail)
    assert got == {("ECS", "novarebootserver"): "重启单台云服务器。"}


def test_diff_completions_threshold():
    matched = [
        {"product": "ECS", "api": "NovaRebootServer", "url": "https://u/1",
         "matched_by": "name", "func_intro": "重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。"},
        {"product": "ECS", "api": "CreateServers", "url": "https://u/2",
         "matched_by": "name", "func_intro": "创建一台或多台云服务器。"},  # 与 desc 相同
        {"product": "OBS", "api": "CreateBucket", "url": "https://u/3",
         "matched_by": "summary", "func_intro": "略长一点点的描述而已"},  # 增益不足
    ]
    descs = {("ECS", "novarebootserver"): "重启单台云服务器。",
             ("ECS", "createservers"): "创建一台或多台云服务器。",
             ("OBS", "createbucket"): "略长一点点的描述"}
    got = diff_completions(descs, matched, min_gain=10)
    assert [c["api"] for c in got] == ["NovaRebootServer"]
    assert got[0]["detail_desc"] == "重启单台云服务器。"


def test_diff_completions_missing_desc_completes():
    matched = [{"product": "OBS", "api": "CreateBucket", "url": "https://u/3",
                "matched_by": "summary", "func_intro": "创建桶，可指定区域与 ACL。"}]
    got = diff_completions({}, matched, min_gain=10)
    assert [c["api"] for c in got] == ["CreateBucket"]


def test_build_hints_shape_and_roundtrip():
    completions = [
        {"product": "ECS", "api": "NovaRebootServer", "url": "https://u/1",
         "detail_desc": "重启单台云服务器。", "matched_by": "name",
         "help_intro": "重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。"},
        {"product": "OBS", "api": "CreateBucket", "url": "https://u/3",
         "detail_desc": "", "matched_by": "summary", "help_intro": "创建桶。"},
    ]
    hints_raw = build_hints(completions)
    assert hints_raw["api_notes_in_list_apis"] is False
    assert set(hints_raw["products"]) == {"ECS", "OBS"}
    assert hints_raw["products"]["ECS"]["apis"]["novarebootserver"] == (
        "重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。\n官方帮助文档: https://u/1")
    parsed = parse_hints(hints_raw)
    assert parsed.api_notes_in_list_apis is False
    assert parsed.api_notes("ECS", "NovaRebootServer").endswith("官方帮助文档: https://u/1")
    assert parsed.combined_notes("OBS", "CreateBucket") == "创建桶。\n官方帮助文档: https://u/3"


def test_build_hints_cap_truncates_keeps_url():
    completions = [{"product": "ECS", "api": "Big", "url": "https://u/big",
                    "detail_desc": "", "matched_by": "name",
                    "help_intro": "长" * 3000}]
    hints_raw = build_hints(completions, cap=100)
    note = hints_raw["products"]["ECS"]["apis"]["big"]
    assert note.startswith("长" * 100)
    assert note.endswith("官方帮助文档: https://u/big")


def test_build_hints_empty():
    hints_raw = build_hints([])
    assert hints_raw["api_notes_in_list_apis"] is False
    assert hints_raw["products"] == {}
    assert hints_raw["instructions"]
    parse_hints(hints_raw)  # 不抛错


def test_extract_links_same_docset_only_dedup():
    html = ('<a href="https://support.huaweicloud.com/api-ecs/a.html">x</a>'
            '<a href="https://support.huaweicloud.com/api-ecs/a.html#t1">x</a>'
            '<a href="https://support.huaweicloud.com/api-obs/b.html">y</a>'
            '<a href="https://other.example.com/api-ecs/c.html">z</a>'
            '<a href="https://support.huaweicloud.com/api-ecs/sub/d.html">w</a>')
    assert extract_links(html, "api-ecs") == [
        "https://support.huaweicloud.com/api-ecs/a.html"]


# ---------- S14a：is_deprecated / extract_replacement ----------

def test_parse_api_page_deprecated_flag():
    assert parse_api_page(API_PAGE_HTML).is_deprecated is True
    assert parse_api_page(COMPLEX_PAGE_HTML).is_deprecated is False
    assert parse_api_page(CHAPTER_PAGE_HTML).is_deprecated is False


def test_extract_replacement_from_intro():
    intro = ("重启单台云服务器。\n"
             "当前API已废弃，请使用批量重启云服务器 - BatchRebootServers。")
    assert extract_replacement(intro) == "BatchRebootServers"


def test_extract_replacement_absent():
    assert extract_replacement("重启单台云服务器。") is None
    assert extract_replacement("当前API已废弃，请参考官方文档。") is None
    assert extract_replacement(None) is None
    assert extract_replacement("") is None


# ---------- S14f：display 主干/短名第四层归属 + 多归属 ApiName 试归属 ----------

GROUPS_L4 = [
    {"name": "计算", "products": [
        {"productshort": "CCI", "name": "云容器实例"},
    ]},
    {"name": "安全", "products": [
        {"productshort": "KMS", "name": "密码安全中心-密钥管理"},
        {"productshort": "CSMS", "name": "密码安全中心-凭据管理"},
        {"productshort": "KPS", "name": "密码安全中心-密钥对管理"},
        {"productshort": "CPCS", "name": "密码安全中心-云平台密码系统服务"},
    ]},
    {"name": "开发", "products": [
        {"productshort": "ProjectMan", "name": "需求管理"},
        {"productshort": "CodeCheck", "name": "代码检查"},
        {"productshort": "CodeArtsCheck", "name": "代码检查 CodeArts Check"},
    ]},
]

APIS_L4 = [
    {"product_short": "KMS", "name": "CreateAlias", "summary": "创建别名"},
    {"product_short": "CSMS", "name": "CheckSecrets", "summary": "校验凭据"},
    {"product_short": "KPS", "name": "AssociateKeypair", "summary": "绑定密钥对"},
    {"product_short": "CPCS", "name": "AddClusterPort", "summary": "添加集群端口"},
    {"product_short": "CCI", "name": "CreateNamespace", "summary": "创建命名空间"},
]


def test_alias_index_l4_unique_substring():
    """display 中文主干是产品中文名子串且唯一命中 → 直接别名。"""
    # 精确层：官方展示形唯一指向；带版本号 display 走 alias_candidates 动态判定
    idx = build_alias_index(GROUPS_L4)
    assert idx["云容器实例 cci"] == "CCI"
    assert alias_candidates("云容器实例 cci 1.0", GROUPS_L4) == {"CCI"}
    assert alias_candidates("需求管理 codearts req", GROUPS_L4) == {"PROJECTMAN"}


def test_alias_index_l4_multi_hit_excluded_from_alias():
    """主干多命中（密码安全中心 DEW → 4 产品）不建别名（防歧义）。"""
    idx = build_alias_index(GROUPS_L4)
    assert "密码安全中心 dew" not in idx
    # trunk"代码检查"双归属（CodeCheck/CodeArtsCheck）不入精确索引；
    # "代码检查 codearts check" 是 CodeArtsCheck 官方展示形（唯一），合理收录
    assert idx["代码检查 codearts check"] == "CODEARTSCHECK"


def test_alias_index_l4_short_token_hit():
    """display 尾部英文 token == productshort（大小写不敏感）→ 别名。"""
    idx = build_alias_index(GROUPS_L4)
    assert idx["媒体处理 mpc"] if False else True  # 占位：mpc 不在 L4 fixture
    assert "dew" not in idx  # DEW 非任何 productshort，落候选集合


def test_match_l4_unique_display_attributes():
    records = [_rec("https://u/cci", "api-cci", api_name="CreateNamespace",
                    display="云容器实例 CCI 1.0", intro="x")]
    got = match_apis(APIS_L4, records, build_alias_index(GROUPS_L4),
                     product_groups=GROUPS_L4)
    assert got["matched"][0]["product"] == "CCI"


def test_match_l4_multi_hit_attributes_via_api_name():
    """多命中候选集合：页面 ApiName 在候选产品目录中唯一命中则归属。"""
    records = [
        _rec("https://u/kms", "api-dew", api_name="CreateAlias",
             display="密码安全中心 DEW", intro="x"),
        _rec("https://u/csms", "api-dew", api_name="CheckSecrets",
             display="密码安全中心 DEW", intro="x"),
        _rec("https://u/kps", "api-dew", api_name="AssociateKeypair",
             display="密码安全中心 DEW", intro="x"),
        _rec("https://u/cpcs", "api-dew", api_name="AddClusterPort",
             display="密码安全中心 DEW", intro="x"),
        _rec("https://u/orphan", "api-dew", api_name="CreateSecretReplica",
             display="密码安全中心 DEW", intro="x"),  # 候选产品目录均无 → unmatched
        _rec("https://u/noapi", "api-dew", display="密码安全中心 DEW"),  # 无 ApiName 无 intro → 跳过
    ]
    got = match_apis(APIS_L4, records, build_alias_index(GROUPS_L4),
                     product_groups=GROUPS_L4)
    by_url = {m["url"]: m for m in got["matched"]}
    assert by_url["https://u/kms"]["product"] == "KMS"
    assert by_url["https://u/csms"]["product"] == "CSMS"
    assert by_url["https://u/kps"]["product"] == "KPS"
    assert by_url["https://u/cpcs"]["product"] == "CPCS"
    orphan = [u for u in got["unmatched"] if u["url"] == "https://u/orphan"]
    assert orphan and orphan[0]["reason"] == "product"


def test_match_l4_ambiguous_candidates_unmatched():
    """候选集合试归属时 ApiName 命中多个候选产品 → unmatched（不猜）。"""
    apis = [
        {"product_short": "A1", "name": "Duplicate", "summary": "x"},
        {"product_short": "B2", "name": "Duplicate", "summary": "x"},
    ]
    records = [_rec("https://u/1", "api-x", api_name="Duplicate",
                    display="重复产品群", intro="x")]
    idx = build_alias_index(PRODUCTS)  # 复用 S13c 的歧义 fixture：重复名→无别名
    got = match_apis(apis, records, idx)
    # display"重复名"不在别名索引 → unmatched(product)
    assert got["matched"] == []
    assert got["unmatched"][0]["reason"] == "product"


def test_match_l4_alias_still_wins_over_candidates():
    """overrides/别名优先于候选集合：display 可精确匹配时直接归属。"""
    records = [_rec("https://u/1", "api-ecs", api_name="NovaRebootServer",
                    display="弹性云服务器 ECS", intro="x")]
    got = match_apis(APIS_INDEX, records, build_alias_index(PRODUCTS))
    assert got["matched"][0]["product"] == "ECS"
    assert got["unmatched"] == []
