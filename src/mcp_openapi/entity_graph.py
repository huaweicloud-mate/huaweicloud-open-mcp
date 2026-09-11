"""openapi 实体关联图谱检索（EntityGraph）：构建产物 → 跨产品 search_apis。

与 Gate/Hints/DeprecatedIndex 同 idiom：--entity-index 配置产物 → EntityGraph
值对象 → service 把 search_apis 作为渐进式工作流第 0 步暴露。数据由
api-refresh graph 管线生成（configs/entity-index.json，wheel bundle 缺省档）。
检索为纯词典逻辑（子串匹配 + 手调权重 + twin 归并），零 LLM、零网络；
matched_via 证据供 LLM 校准信任。产物为构建期快照（与 hints 同性质）。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, cast

from common import paths as common_paths
from common.types import (
    SearchApiHit,
    SearchApisResult,
    SearchProductHit,
    SearchRelated,
)

DEFAULT_INDEX = "entity-index.json"

_EVIDENCE_CAP = 3
_TOP_APIS = 3
_API_AGG_CAP = 12
_LIMIT_MAX = 20

# 每术语每产品的评分权重（手调，测试矩阵固化为独立真值）。
# 身份信号（product/alias 精确）> 精确行为信号（kw exact）> contains 噪音：
# contains 级证据（2 分/条）跨多 API 累积时被独立封顶压制，避免
# "关键词密集的产品靠碎片噪音压过精确命中"（实测 IMS/Workspace/DeH 反例）。
W_PRODUCT_EXACT = 12
W_ALIAS_EXACT = 12
W_NAME = 5
W_ALIAS_CONTAINS = 8
W_CATEGORY = 1
W_API_NAME = 2
W_API_SUMMARY = 1
W_API_TAGS = 1
W_KEYWORD_EXACT = 5
W_KEYWORD_CONTAINS = 2
W_DISCRIMINATIVE_TAG = 2
DISCRIMINATIVE_MAX_COVERAGE = 3

# 2-gram 回退术语降权：宽泛碎片是弱证据（命中面越大越弱）
W_NAME_GRAM = 2

# 术语内分通道封顶（identity 信号不封顶，行为信号按证据强度分通道）
_KW_EXACT_CAP = 10
_KW_CONTAINS_CAP = 4
_KW_CONTAINS_CAP_GRAM = 2
_NON_KW_CAP = 6
_NON_KW_CAP_GRAM = 4

# 证据优先级（越小越强）
_PRIO_PRODUCT = 0
_PRIO_ALIAS_EXACT = 1
_PRIO_KW_EXACT = 2
_PRIO_TAG = 3
_PRIO_KW_CONTAINS = 4
_PRIO_NAME = 5
_PRIO_ALIAS_CONTAINS = 6

# CJK 2-gram 回退阈值：最高分低于该值（含零命中）时扩展 2-gram 术语重排取并集
_FALLBACK_THRESHOLD = 4


def _clean_str(val: Any, where: str) -> str:
    if not isinstance(val, str):
        raise ValueError(f"{where} 必须是字符串")
    return val


@dataclass(frozen=True)
class _ApiNode:
    name: str
    method: str
    summary: str
    tags: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class _ProductNode:
    product: str
    name: str
    category: str
    is_global: bool | None
    link: str | None
    aliases: tuple[str, ...]
    related: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class EntityGraph:
    """实体关联图谱值对象：parse/load 产静态快照，search_apis 为唯一检索入口。"""

    version: int
    products: dict[str, _ProductNode] = field(default_factory=dict)
    apis_by_product: dict[str, tuple[_ApiNode, ...]] = field(default_factory=dict)
    tag_products: dict[str, int] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "EntityGraph":
        return cls(version=0)

    def search_apis(self, query: str, *, limit: int = 8,
                    category: str | None = None,
                    allowed: frozenset[str] | None = None) -> SearchApisResult:
        """跨产品检索：术语切分 → 产品评分（别名/关键词/tag-IDF/孪生归并）
        → 排名短名单。allowed 为机制参数（gate 过滤后的产品白名单，
        排名前过滤保证 total/truncated 语义一致）。"""
        try:
            limit = max(1, min(int(limit), _LIMIT_MAX))
        except (TypeError, ValueError):
            limit = 8
        empty: SearchApisResult = {"ok": True, "query": query, "total": 0,
                                   "limit": limit, "products": [],
                                   "truncated": False}
        terms = _terms(query)
        if not terms:
            return empty

        def _rank(ranked_terms: list[str]) -> tuple[list[SearchProductHit], bool]:
            gram_terms = frozenset(ranked_terms) - frozenset(terms)
            scored: list[tuple[_ProductNode, int, list[tuple[int, int, str]],
                               dict[str, tuple[int, _ApiNode]]]] = []
            for ps, node in self.products.items():
                if allowed is not None and ps not in allowed:
                    continue
                if category and category.lower() not in node.category.lower():
                    continue
                cand = self._score_product(node, ranked_terms, gram_terms)
                if cand[1] > 0:
                    scored.append(cand)
            rows = _merge_rows(scored)
            rows.sort(key=lambda r: (-r["score"], r["product"].lower()))
            # API 级接战：原术语是否有任何 kw/name/summary/tag 命中
            # （alias-only 的行不算——连续口语短语需要 2-gram 补 API 召回）
            engaged = any(c[3] for c in scored)
            return rows, engaged

        rows, engaged = _rank(terms)
        if not rows or rows[0]["score"] < _FALLBACK_THRESHOLD or not engaged:
            ext = _extend_terms(terms)
            if ext != terms:
                ext_rows, _ = _rank(ext)
                rows = _union_rows(rows, ext_rows)
        page = rows[:limit]
        return {"ok": True, "query": query, "total": len(rows), "limit": limit,
                "products": page, "truncated": len(rows) > len(page)}

    def _score_product(
            self, node: _ProductNode, terms: list[str],
            gram_terms: frozenset[str] = frozenset()
    ) -> tuple[_ProductNode, int, list[tuple[int, int, str]],
               dict[str, tuple[int, _ApiNode]]]:
        """单产品评分：alias 通道每产品每查询一次（不随 gram 数堆叠）+
        Σ术语分（name/category/判别 tag/API 三通道封顶）。

        返回 (node, score, evidence[(prio, seq, text)], api_hits[name → (分, 节点)])。
        gram_terms 中的术语按回退降权（name/非kw封顶，且不进 kw 通道）。
        """
        apis = self.apis_by_product.get(node.product, ())
        product_lower = node.product.lower()
        alias_lowers = [(a, a.lower()) for a in node.aliases]
        name_lower = node.name.lower()
        category_lower = node.category.lower()
        score = 0
        evidence: list[tuple[int, int, str]] = []
        api_hits: dict[str, tuple[int, _ApiNode]] = {}
        seq = 0

        def _ev(prio: int, text: str) -> None:
            nonlocal seq
            evidence.append((prio, seq, text))
            seq += 1

        # alias 通道（每产品一次）：嵌入用户原句 > 术语精确等于 > 术语∈别名。
        # 逐别名逐术语累加会被 LLM 多别名互嵌碎片打爆（实测 专属云主机/独享主机）。
        best_alias = 0
        best_alias_ev: tuple[int, str] | None = None
        for alias, al in alias_lowers:
            if any(al in t for t in terms if t not in gram_terms):
                cand = (W_ALIAS_EXACT, f"alias:{alias}")
            elif any(t in al for t in terms):
                cand = (W_ALIAS_CONTAINS, f"alias:{alias}")
            else:
                continue
            if cand[0] > best_alias:
                best_alias = cand[0]
                best_alias_ev = cand
        if best_alias and best_alias_ev is not None:
            score += best_alias
            _ev(_PRIO_ALIAS_EXACT if best_alias == W_ALIAS_EXACT
                else _PRIO_ALIAS_CONTAINS, best_alias_ev[1])

        for term in terms:
            is_gram = term in gram_terms
            s = 0
            if term == product_lower:
                s += W_PRODUCT_EXACT
                _ev(_PRIO_PRODUCT, f"product:{node.product}")
            if term in name_lower:
                s += W_NAME_GRAM if is_gram else W_NAME
                _ev(_PRIO_NAME, f"name:{term}")
            if category_lower and term in category_lower:
                s += W_CATEGORY
            non_kw_agg = 0
            kw_exact_agg = 0
            kw_contains_agg = 0
            tag_bonus = False
            for api in apis:
                a = 0
                kw_exact = 0
                kw_contains = 0
                if term in api.name.lower():
                    a += W_API_NAME
                if term in api.summary.lower():
                    a += W_API_SUMMARY
                if term in api.tags.lower():
                    a += W_API_TAGS
                    if not tag_bonus:
                        cov = self.tag_products.get(api.tags, 0)
                        if 0 < cov <= DISCRIMINATIVE_MAX_COVERAGE:
                            tag_bonus = True
                            s += W_DISCRIMINATIVE_TAG
                            _ev(_PRIO_TAG,
                                f"tag:{api.tags}({cov}产品)")
                for kw in api.keywords:
                    if is_gram:
                        # 2-gram 碎片不做 kw 通道：LLM 关键词对碎片级命中是
                        # 噪音主源（实测 裸"标签"/"主机"级 kw 压过别名身份信号）
                        break
                    kl = kw.lower()
                    if term == kl:
                        kw_exact += W_KEYWORD_EXACT
                        _ev(_PRIO_KW_EXACT, f"kw:{kw}→{api.name}")
                    elif term in kl:
                        kw_contains += W_KEYWORD_CONTAINS
                        _ev(_PRIO_KW_CONTAINS, f"kw:{kw}→{api.name}")
                if kw_exact:
                    # 精确口语关键词视同 summary 级相关（中文形态差补偿：
                    # "重启云服务器" 不含 "重启服务器"，但 kw exact 已是更强证据）
                    a += W_API_SUMMARY
                if a or kw_exact or kw_contains:
                    prev = api_hits.get(api.name)
                    api_hits[api.name] = (a + kw_exact + kw_contains
                                          + (prev[0] if prev else 0), api)
                non_kw_agg += a
                kw_exact_agg += kw_exact
                kw_contains_agg += kw_contains
            s += min(non_kw_agg, _NON_KW_CAP_GRAM if is_gram else _NON_KW_CAP)
            s += min(kw_exact_agg, _KW_EXACT_CAP)
            s += min(kw_contains_agg,
                     _KW_CONTAINS_CAP_GRAM if is_gram else _KW_CONTAINS_CAP)
            score += s
        return node, score, evidence, api_hits


def _terms(query: str) -> list[str]:
    """术语切分：空白/标点边界 + ASCII↔CJK 边界 + 多词时整句 phrase 项。

    连续 CJK 不分词（子串匹配语义由 2-gram 回退兜底）；k8s集群扩容 之类
    混写按语言边界切出 k8s / 集群扩容。
    """
    text = (query or "").strip().lower()
    if not text:
        return []
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+|[^\sa-z0-9\u4e00-\u9fff]+",
                        text)
    uniq = list(dict.fromkeys(t for t in tokens if t))
    if len(uniq) > 1 and text not in uniq:
        uniq.append(text)
    return uniq


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _extend_terms(terms: list[str]) -> list[str]:
    """弱结果回退扩展：对 ≥3 字的 CJK 连续段补全相邻 2-gram 术语。

    只在弱结果时触发——干净的分词查询（LLM 提取的关键词）不受 2-gram
    噪音影响（如 企业主机安全 不因 '主机' 干扰 云主机 查询）。
    """
    out = list(terms)
    for term in terms:
        for run in _CJK_RUN.findall(term):
            if len(run) < 3:
                continue
            for i in range(len(run) - 1):
                gram = run[i:i + 2]
                if gram not in out:
                    out.append(gram)
    return out


def _union_rows(rows_a: list[SearchProductHit],
                rows_b: list[SearchProductHit]) -> list[SearchProductHit]:
    """两轮排名并集：同名产品保留高分行，重排序。"""
    merged: dict[str, SearchProductHit] = {}
    for row in rows_a + rows_b:
        cur = merged.get(row["product"])
        if cur is None or row["score"] > cur["score"]:
            merged[row["product"]] = row
    return sorted(merged.values(),
                  key=lambda r: (-r["score"], r["product"].lower()))


def _merge_rows(
    scored: list[tuple[_ProductNode, int, list[tuple[int, int, str]],
                       dict[str, tuple[int, _ApiNode]]]],
) -> list[SearchProductHit]:
    """同名产品归并（twin）：主产品=link 非空 > API 数 > 字典序；
    分数取成员最大值，API/证据跨成员合并去重。"""
    groups: dict[str, list[tuple[_ProductNode, int, list[tuple[int, int, str]],
                                 dict[str, tuple[int, _ApiNode]]]]] = {}
    for cand in scored:
        node = cand[0]
        key = node.name if node.name else f"\0{node.product}"
        groups.setdefault(key, []).append(cand)
    rows: list[SearchProductHit] = []
    for members in groups.values():
        primary = min(members, key=lambda c: (
            0 if c[0].link else 1, -len(c[3]), c[0].product.lower()))
        node = primary[0]
        score = max(c[1] for c in members)
        merged_apis: dict[str, tuple[int, _ApiNode]] = {}
        for _, _, _, api_hits in members:
            for name, (s, api) in api_hits.items():
                if name not in merged_apis or s > merged_apis[name][0]:
                    merged_apis[name] = (s, api)
        apis = cast(list[SearchApiHit], [
            {"name": a.name, "method": a.method, "summary": a.summary,
             "tags": a.tags}
            for _, a in sorted(merged_apis.values(),
                               key=lambda x: (-x[0], x[1].name.lower()))
        ][:_TOP_APIS])
        seen_ev: set[str] = set()
        matched_via: list[str] = []
        for _, _, ev, _ in sorted(members, key=lambda c: c[0] is not node):
            for _, seq, text in sorted(ev, key=lambda x: (x[0], x[1])):
                if text not in seen_ev:
                    seen_ev.add(text)
                    matched_via.append(text)
        rows.append({
            "product": node.product, "name": node.name,
            "category": node.category, "is_global": node.is_global,
            "link": node.link, "score": score,
            "matched_via": matched_via[:_EVIDENCE_CAP],
            "apis": apis,
            "related": cast(list[SearchRelated],
                            [dict(r) for r in node.related]),
        })
    return rows


# ---------- parse / load ----------

def parse_entity_index(raw: Any) -> EntityGraph:
    """把 entity-index 产物解析为 EntityGraph。严格校验：非法结构抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("entity-index 必须是 mapping")
    unknown = set(raw) - {"version", "generated_at", "products", "apis",
                          "tag_products"}
    if unknown:
        raise ValueError(f"entity-index 含未知键: {sorted(unknown)}")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("entity-index version 必须是整数")

    products: dict[str, _ProductNode] = {}
    raw_products = raw.get("products") or []
    if not isinstance(raw_products, list):
        raise ValueError("entity-index products 必须是列表")
    for p in raw_products:
        if not isinstance(p, dict):
            raise ValueError("entity-index products 条目必须是 mapping")
        unknown = set(p) - {"product", "name", "category", "is_global",
                            "link", "aliases", "related"}
        if unknown:
            raise ValueError(f"entity-index 产品条目含未知键: {sorted(unknown)}")
        ps = p.get("product")
        if not isinstance(ps, str) or not ps.strip():
            raise ValueError("entity-index 产品 product 必须是非空字符串")
        name = p.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError(f"entity-index 产品 {ps} name 必须是字符串")
        category = p.get("category")
        if category is not None and not isinstance(category, str):
            raise ValueError(f"entity-index 产品 {ps} category 必须是字符串")
        is_global = p.get("is_global")
        if is_global is not None and not isinstance(is_global, bool):
            raise ValueError(f"entity-index 产品 {ps} is_global 必须是布尔或 null")
        link = p.get("link")
        if link is not None and not isinstance(link, str):
            raise ValueError(f"entity-index 产品 {ps} link 必须是字符串或 null")
        aliases_raw = p.get("aliases") or []
        if not isinstance(aliases_raw, list) \
                or not all(isinstance(a, str) for a in aliases_raw):
            raise ValueError(f"entity-index 产品 {ps} aliases 必须是字符串列表")
        related_raw = p.get("related") or []
        if not isinstance(related_raw, list):
            raise ValueError(f"entity-index 产品 {ps} related 必须是列表")
        related: list[dict[str, Any]] = []
        for r in related_raw:
            if not isinstance(r, dict):
                raise ValueError(f"entity-index 产品 {ps} related 条目必须是 mapping")
            unknown = set(r) - {"product", "kind", "via"}
            if unknown:
                raise ValueError(
                    f"entity-index 产品 {ps} related 条目含未知键: {sorted(unknown)}")
            rp = r.get("product")
            kind = r.get("kind")
            if not isinstance(rp, str) or not rp.strip():
                raise ValueError(f"entity-index 产品 {ps} related.product 必填")
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError(f"entity-index 产品 {ps} related.kind 必填")
            via = r.get("via")
            if via is not None and not isinstance(via, str):
                raise ValueError(f"entity-index 产品 {ps} related.via 必须是字符串")
            edge = {"product": rp.strip(), "kind": kind.strip()}
            if via is not None:
                edge["via"] = via
            related.append(edge)
        products[ps.strip()] = _ProductNode(
            product=ps.strip(), name=name or "", category=category or "",
            is_global=is_global, link=link,
            aliases=tuple(aliases_raw), related=tuple(related))

    apis_by_product: dict[str, list[_ApiNode]] = {}
    raw_apis = raw.get("apis") or []
    if not isinstance(raw_apis, list):
        raise ValueError("entity-index apis 必须是列表")
    for a in raw_apis:
        if not isinstance(a, dict):
            raise ValueError("entity-index apis 条目必须是 mapping")
        unknown = set(a) - {"n", "m", "s", "t", "p", "k"}
        if unknown:
            raise ValueError(f"entity-index api 条目含未知键: {sorted(unknown)}")
        name = _clean_str(a.get("n"), "entity-index api n")
        ps = _clean_str(a.get("p"), "entity-index api p")
        if not name.strip() or not ps.strip() or ps.strip() not in products:
            raise ValueError(f"entity-index api 条目 n/p 缺失或产品未知: {name}")
        kws_raw = a.get("k") or []
        if not isinstance(kws_raw, list) \
                or not all(isinstance(k, str) for k in kws_raw):
            raise ValueError(f"entity-index api {name} k 必须是字符串列表")
        apis_by_product.setdefault(ps.strip(), []).append(_ApiNode(
            name=name, method=_clean_str(a.get("m") or "", "entity-index api m"),
            summary=_clean_str(a.get("s") or "", "entity-index api s"),
            tags=_clean_str(a.get("t") or "", "entity-index api t"),
            keywords=tuple(kws_raw)))

    tag_products: dict[str, int] = {}
    raw_tags = raw.get("tag_products") or {}
    if not isinstance(raw_tags, dict):
        raise ValueError("entity-index tag_products 必须是 mapping")
    for tag, count in raw_tags.items():
        if not isinstance(tag, str) or not tag.strip() \
                or not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("entity-index tag_products 必须是 {tag: int}")
        tag_products[tag.strip()] = count

    frozen_apis = {ps: tuple(nodes) for ps, nodes in apis_by_product.items()}
    return EntityGraph(version=version, products=products,
                       apis_by_product=frozen_apis,
                       tag_products=tag_products)


def load_entity_index(value: str | None) -> EntityGraph:
    """三分支装配：None→缺省档 configs/entity-index.json（缺失静默空，与
    hints 缺省档同 idiom）；off/空串→显式禁用；其余→显式路径（经
    resolve_config_arg 支持裸文件名，缺失/非法 fail-fast）。"""
    if value is None:
        path = common_paths.config_path(DEFAULT_INDEX)
        if not path.is_file():
            return EntityGraph.empty()
        with open(path, encoding="utf-8") as f:
            return parse_entity_index(json.load(f))
    if not value.strip() or value.strip().lower() == "off":
        return EntityGraph.empty()
    with open(common_paths.resolve_config_arg(value), encoding="utf-8") as f:
        return parse_entity_index(json.load(f))
