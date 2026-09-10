"""帮助中心（support.huaweicloud.com）纯函数核心：解析 / 匹配 / 差集 / hints 生成。

构建期管线（api-refresh helpdocs/helphints）共用；不碰网络、不耦合 MCP。
接口语义详见各函数 docstring；HTML/站点格式知识全部收敛在本模块。
"""

import html as html_mod
import re
from dataclasses import dataclass
from typing import Any

# ---------- 挑战页识别（fetch_help_docs 共用） ----------

_CHALLENGE_TITLE = re.compile(
    r"<title[^>]*>\s*security verification\s*</title>", re.IGNORECASE)


def is_challenge(text: str) -> bool:
    """识别人机验证挑战页（实测响应：<title>Security Verification</title>）。"""
    return bool(_CHALLENGE_TITLE.search(text or ""))


# ---------- 解析 ----------

_LOC = re.compile(r"<loc>\s*([^<\s]+?)\s*</loc>")
_SITEMAP_URL = re.compile(r"https?://support\.huaweicloud\.com/([^/]+)/")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_FUNC_HEADING = re.compile(r"<h4[^>]*>\s*功能介绍\s*</h4>")
_SECTION_BOUNDARY = re.compile(
    r'<div class="section"|<h[1-6][^>]*class="sectiontitle"')
_STRIP_BLOCKS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_BREAKS = re.compile(r"</(p|li|div|h[1-6]|tr|table)>|<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u3000]+")
_BLANK_LINES = re.compile(r"\n{2,}")


def parse_sitemap(xml_text: str) -> dict[str, list[str]]:
    """把 sitemap.xml 按文档集（docset，URL 首段路径）分组。

    返回 {docset: [url, ...]}；仅保留 support.huaweicloud.com 的 *.html
    文档页；URL 去重保序。
    """
    grouped: dict[str, list[str]] = {}
    seen: set[str] = set()
    for url in _LOC.findall(xml_text or ""):
        if url in seen or not url.endswith(".html"):
            continue
        m = _SITEMAP_URL.match(url)
        if not m:
            continue
        seen.add(url)
        grouped.setdefault(m.group(1), []).append(url)
    return grouped


@dataclass(frozen=True)
class ParsedPage:
    """页面 <title> 解析结果。

    标题形态（实测）：`{页头}_{章节路径...}_API参考_{产品名}-华为云`；
    页头为 `{中文名[（废弃）等后缀]}[ - {ApiName}]`。
    """

    title: str
    api_name: str | None
    cn_name: str
    is_api_ref: bool
    is_deprecated: bool
    product_display: str | None


_API_NAME_TAIL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_HUAWEI_SUFFIX = "-华为云"
_DEPRECATED_SUFFIX = re.compile(r"[（(]\s*已?废弃\s*[）)]\s*$")
_REPLACEMENT = re.compile(r"请使用.*?[-－—]\s*([A-Za-z][A-Za-z0-9_]*)")


def parse_api_page(html_text: str) -> ParsedPage:
    """从页面 <title> 提取 (api_name, cn_name, is_api_ref, is_deprecated, product_display)。"""
    m = _TITLE.search(html_text or "")
    title = html_mod.unescape(m.group(1)).strip() if m else ""
    segments = title.split("_")
    head = segments[0] if segments else ""
    is_api_ref = "API参考" in segments[1:]
    product_display: str | None = None
    if len(segments) >= 2 and segments[-1].endswith(_HUAWEI_SUFFIX):
        product_display = segments[-1][: -len(_HUAWEI_SUFFIX)]
    api_name: str | None = None
    if " - " in head:
        cn, _, tail = head.rpartition(" - ")
        if _API_NAME_TAIL.match(tail):
            api_name = tail
            head = cn
    is_deprecated = bool(_DEPRECATED_SUFFIX.search(head))
    return ParsedPage(title=title, api_name=api_name, cn_name=head.strip(),
                      is_api_ref=is_api_ref, is_deprecated=is_deprecated,
                      product_display=product_display)


def extract_replacement(intro_text: str | None) -> str | None:
    """从功能介绍文本提取替代接口名（「请使用 … - ApiName」），无则 None。"""
    if not intro_text:
        return None
    m = _REPLACEMENT.search(intro_text)
    return m.group(1) if m else None


def _strip_tags(fragment: str) -> str:
    fragment = _STRIP_BLOCKS.sub("", fragment)
    fragment = _BREAKS.sub("\n", fragment)
    fragment = _TAGS.sub("", fragment)
    fragment = html_mod.unescape(fragment)
    lines = (_WS.sub(" ", line).strip()
             for line in fragment.split("\n"))
    return _BLANK_LINES.sub("\n", "\n".join(li for li in lines if li))


def extract_func_intro(html_text: str) -> str | None:
    """提取「功能介绍」段文本（首个 h4 小节到下一小节边界），无则 None。

    段内 <p>/<li> 转换行，内联标签剥除，实体反转义，空白归一；
    嵌套 <ul>/<div> 保留为平铺行。
    """
    m = _FUNC_HEADING.search(html_text or "")
    if not m:
        return None
    rest = html_text[m.end():]
    nxt = _SECTION_BOUNDARY.search(rest)
    fragment = rest[: nxt.start()] if nxt else rest
    text = _strip_tags(fragment).strip()
    return text or None


def extract_links(html_text: str, docset: str) -> list[str]:
    """提取同文档集的绝对链接（去重保序）——BFS 队列的发现来源。"""
    pat = re.compile(
        rf'href="(https?://support\.huaweicloud\.com/{re.escape(docset)}'
        r"/[a-zA-Z0-9_]+\.html)")
    out: list[str] = []
    seen: set[str] = set()
    for m in pat.finditer(html_text or ""):
        url = m.group(1)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---------- 匹配 / 差集 / hints 生成 ----------

_DEFAULT_INSTRUCTIONS = (
    "本部署通过 hints 注入华为云帮助中心「功能介绍」补全与官方文档链接"
    "（仅 get_api 返回携带）；与 API Explorer 描述不一致时以帮助中心为准。")

_TRailing_BRACKETS = re.compile(r"[（(][^）)]*[）)]\s*$")
_HWS = re.compile(r"\s+")


def _norm(s: str | None) -> str:
    return _HWS.sub(" ", (s or "")).strip()


_BRAND_PREFIX = re.compile(r"^(codearts|huawei(?:\s?cloud)?)\s*")
_TRAIL_TOKEN = re.compile(r"[a-z0-9. ]+$", re.IGNORECASE)
_MIN_TRUNK = 3


def _trunk(s: str) -> str:
    """display/产品名主干：去括号段、去尾部英文 token/版本号、去品牌前缀。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = _TRAIL_TOKEN.sub("", s)
    s = _BRAND_PREFIX.sub("", s)
    return s.strip()


def _alias_group_tables(product_groups: list[dict[str, Any]]) -> tuple[
        dict[str, set[str]], dict[str, set[str]], set[str]]:
    """别名层共用表：精确别名 owners / 产品主干 trunk_owners / productshort 小写集。"""
    owners: dict[str, set[str]] = {}
    trunk_owners: dict[str, set[str]] = {}
    shorts: set[str] = set()
    for g in product_groups or []:
        for p in g.get("products") or []:
            short = (p.get("productshort") or "").strip()
            name = (p.get("name") or "").strip()
            if not short:
                continue
            upper = short.upper()
            shorts.add(short.lower())
            candidates = {short.lower(), name.lower()}
            if name:
                candidates.add(f"{name} {short}".lower())
            for c in candidates:
                if c:
                    owners.setdefault(c, set()).add(upper)
            t = _trunk(name)
            if len(t) >= _MIN_TRUNK:
                trunk_owners.setdefault(t, set()).add(upper)
    return owners, trunk_owners, shorts


def _l4_candidates(display: str, trunk_owners: dict[str, set[str]],
                   shorts: set[str]) -> set[str]:
    """L4 归属候选：中文主干互嵌（≥3 字）+ display 英文 token 恰为某 productshort。

    token 仅在命中已知 productshort（大小写不敏感）时计入——`CodeArts Check`
    这类品牌词不因拆词而混入候选。
    """
    hits: set[str] = set()
    t = _trunk(display)
    if len(t) >= _MIN_TRUNK:
        for trunk, ups in trunk_owners.items():
            if trunk and len(trunk) >= _MIN_TRUNK and (t in trunk or trunk in t):
                hits |= ups
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", display):
        up = token.upper()
        if token.lower() in shorts:
            hits.add(up)
    return hits


def build_alias_index(product_groups: list[dict[str, Any]]) -> dict[str, str]:
    """产品展示名/短名 → PRODUCT_UPPER 别名索引（帮助中心 product_display 匹配用）。

    - 精确层：productshort、中文名、`中文名 productshort` 展示形；同名/多归属
      别名丢弃（歧义不入索引，如 trunk `密码安全中心` 同时归属
      KMS/CSMS/KPS/CPCS 四产品——由 match_apis 的候选试归属消解）。
    - L4（主干/token 命中判定）不在索引内预展开——真实 display 带版本号等变体
      无法穷举，统一在 match_apis 侧经 alias_candidates 动态判定。
    """
    owners, _trunk_owners, _shorts = _alias_group_tables(product_groups)
    alias: dict[str, str] = {}
    for c, ups in owners.items():
        if len(ups) == 1:
            alias[c] = next(iter(ups))
    return alias


def alias_candidates(display: str,
                     product_groups: list[dict[str, Any]]) -> set[str]:
    """display 的 L4 归属候选集合（多命中场景，供 match_apis ApiName 试归属）。

    规则：display 中文主干与产品中文名互嵌（≥3 字），或 display 英文 token
    恰为某 productshort；返回 PRODUCT_UPPER 集合（可空、可多命中）。
    """
    _, trunk_owners, shorts = _alias_group_tables(product_groups)
    return _l4_candidates(display or "", trunk_owners, shorts)


def _cn_base(cn: str) -> str:
    """去掉标题中文名尾部的括号后缀（（废弃）等，可叠多个）。"""
    base = cn.strip()
    while True:
        stripped = _TRailing_BRACKETS.sub("", base).strip()
        if stripped == base:
            return base
        base = stripped


def match_apis(apis_index: list[dict[str, Any]],
               records: list[dict[str, Any]],
               alias_index: dict[str, str],
               *,
               overrides: dict[str, str] | None = None,
               product_groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """把爬取的页面记录匹配到 (product, api)。

    - records 仅接纳 is_api_ref=True 且（有 api_name 或有 func_intro）的条目；
    - 产品归属：overrides[docset] → alias_index[product_display] 精确层 → L4
      （display 中文主干/英文 token 产生的候选集合：ApiName 在候选产品目录中
      唯一命中则归属——唯一与多命中同一路径，不预展开、不猜）→ unmatched(product)；
    - 接口匹配：api_name（大小写不敏感）优先，未中回退 cn_name 去括号后缀 == summary；
    - (product, api) 重复命中只保留首个（unmatched reason=duplicate）。

    返回 {"matched": [{product, api, url, docset, matched_by, func_intro}],
          "unmatched": [{url, reason, product}], "ambiguous": [{url, reason, product}]}。
    """
    overrides = overrides or {}
    by_name: dict[tuple[str, str], dict[str, Any]] = {}
    by_summary: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in apis_index or []:
        product = (a.get("product_short") or "").upper()
        by_name[(product, (a.get("name") or "").lower())] = a
        by_summary.setdefault((product, _norm(a.get("summary"))), []).append(a)
    # L4 候选判定需要 display→候选 的表构建（每条记录重算太贵），有 product_groups
    # 时先取精确别名之外的形态：candidates 本身做缓存（display 相同复用）。
    cand_cache: dict[str, set[str]] = {}

    def _l4(display: str) -> set[str]:
        if not product_groups or not display:
            return set()
        if display not in cand_cache:
            cand_cache[display] = alias_candidates(display, product_groups)
        return cand_cache[display]

    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    taken: set[tuple[str, str]] = set()
    for r in records or []:
        if not r.get("is_api_ref"):
            continue
        if not r.get("api_name") and not r.get("func_intro"):
            continue
        url = r.get("url") or ""
        docset = r.get("docset") or ""
        display = (r.get("product_display") or "").strip()
        product = overrides.get(docset) or alias_index.get(display.lower())
        if not product:
            # L4：候选集合中 ApiName 唯一命中才归属（不猜；带版本号 display 主干
            # 相同，天然落进同一候选集合）
            api_name = r.get("api_name")
            if api_name:
                hits = _l4(display)
                hit_products = sorted({p for p in hits
                                       if (p, api_name.lower()) in by_name})
                if len(hit_products) == 1:
                    product = hit_products[0]
        if not product:
            unmatched.append({"url": url, "reason": "product", "product": None})
            continue
        hit: dict[str, Any] | None = None
        matched_by = ""
        api_name = r.get("api_name")
        if api_name:
            found = by_name.get((product, api_name.lower()))
            if found:
                hit, matched_by = found, "name"
        if hit is None:
            cands = by_summary.get((product, _cn_base(r.get("cn_name") or "")))
            if not cands:
                unmatched.append({"url": url, "reason": "api", "product": product})
                continue
            if len(cands) > 1:
                ambiguous.append({"url": url, "reason": "summary_ambiguous",
                                  "product": product})
                continue
            hit, matched_by = cands[0], "summary"
        key = (product, (hit.get("name") or "").lower())
        if key in taken:
            unmatched.append({"url": url, "reason": "duplicate", "product": product})
            continue
        taken.add(key)
        matched.append({"product": product, "api": hit.get("name") or "", "url": url,
                        "docset": docset, "matched_by": matched_by,
                        "func_intro": r.get("func_intro")})
    return {"matched": matched, "unmatched": unmatched, "ambiguous": ambiguous}


def detail_descriptions(apis_detail: dict[str, Any]) -> dict[tuple[str, str], str]:
    """raw/apis_detail.json → {(PRODUCT_UPPER, api_lower): op.description}。

    遍历每份详情文档的 paths/method/operationId；empty 占位文档跳过；
    description 为 None 时记 ""（视为无描述）。
    """
    out: dict[tuple[str, str], str] = {}
    for key, doc in (apis_detail.get("apis") or {}).items():
        product = key.split("::", 1)[0].upper()
        for path_item in (doc.get("paths") or {}).values():
            if not isinstance(path_item, dict):
                continue
            for op in path_item.values():
                if not isinstance(op, dict):
                    continue
                opid = op.get("operationId")
                if not opid:
                    continue
                out[(product, str(opid).lower())] = op.get("description") or ""
    return out


def diff_completions(detail_descs: dict[tuple[str, str], str],
                     matched: list[dict[str, Any]], *,
                     min_gain: int = 20) -> list[dict[str, Any]]:
    """仅差集口径：帮助中心功能介绍显著长于 detail description 才补全。

    空白归一后比较；完全一致或增益 < min_gain 跳过；detail 无描述时
    只要 help_intro 非空即补全（增益按全长计）。
    """
    completions: list[dict[str, Any]] = []
    for m in matched or []:
        intro = (m.get("func_intro") or "").strip()
        if not intro:
            continue
        desc = detail_descs.get(
            ((m.get("product") or "").upper(), (m.get("api") or "").lower()), "")
        help_n, desc_n = _norm(intro), _norm(desc)
        if help_n == desc_n or len(help_n) - len(desc_n) < min_gain:
            continue
        completions.append({"product": m.get("product"), "api": m.get("api"),
                            "url": m.get("url"), "detail_desc": desc,
                            "matched_by": m.get("matched_by"), "help_intro": intro})
    return completions


def build_hints(completions: list[dict[str, Any]], *,
                cap: int = 2000,
                instructions: str | None = None) -> dict[str, Any]:
    """差集条目 → S10 hints 配置（api_notes_in_list_apis 恒 false）。

    note = 功能介绍（超长截断加 …）+ "\n官方帮助文档: {url}"；
    产品键 upper、API 键 lower（parse_hints 归一化语义）；无条目产品不出现。
    """
    products: dict[str, dict[str, Any]] = {}
    for c in completions or []:
        intro = c.get("help_intro") or ""
        if cap and len(intro) > cap:
            intro = intro[:cap] + "…"
        note = f"{intro}\n官方帮助文档: {c.get('url')}"
        entry = products.setdefault((c.get("product") or "").upper(), {"apis": {}})
        entry["apis"][(c.get("api") or "").lower()] = note
    return {"instructions": instructions or _DEFAULT_INSTRUCTIONS,
            "api_notes_in_list_apis": False,
            "products": products}
