"""openapi 实体关联图谱检索（EntityGraph）：构建产物 → 跨产品 search_apis。

S16b：API 文本通道（name/summary/tags/keywords）的评分主体切换为 tantivy
BM25 引擎（entity_search.py，S16a）——IDF/长度归一化/碎片噪音由统计评分
系统性接管，取代手调权重与 2-gram 回退；身份信号（product/alias/name/
category/判别 tag）保留 Python 精确子串语义（alias「嵌入用户原句」是反向
包含，BM25 表达不了，且 alias 身份双档 12/8 为实测有效语义）。

与 Gate/Hints/DeprecatedIndex 同 idiom：--entity-index 配置产物 →
EntityGraph 值对象 → service 把 search_apis 作为渐进式工作流第 0 步暴露。
数据由 api-refresh graph 管线生成（configs/entity-index.json，wheel bundle
缺省档）；tantivy 索引在 parse 时于 RAM 构建（构建期快照，零网络）。
"""

import json
import math
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
from mcp_openapi.entity_search import DocHit, IndexedApi, TantivyEngine

DEFAULT_INDEX = "entity-index.json"

_EVIDENCE_CAP = 3
_TOP_APIS = 3
_LIMIT_MAX = 20

# 引擎命中池：单次查询从 BM25 取回的文档上限（跨产品聚合与 top-3 的召回
# 预算；身份信号可达的产品不受池约束——alias/product 命中无需引擎文档）。
_DOC_POOL = 64
# 归一化 BM25（0..1）→ 产品分数量纲。与身份权重同量纲，Phase 4 对金评集调参。
_BM25_SCALE = 12.0
# 产品广度先验：BM25 无「产品显著性」概念——通用口语查询（重启服务器）下
# 关键词密集的窄产品可压过广谱旗舰产品，且 alias 碎片等价（服务器 gram 同时
# 命中 云服务器/物理服务器）。API 数的 log 广度是唯一有数据依据的统计先验
# （产品泛用性），平方曲线让小产品近零、旗舰陡增。
_W_BREADTH = 2.5
_BREADTH_LOG = math.log10(150)

# 身份信号权重（保留手调：alias 身份语义无法由 BM25 统计表达；Phase 4
# 对金评集 45 例扫参锁定）。
W_PRODUCT_EXACT = 12
W_ALIAS_EXACT = 18
W_ALIAS_CONTAINS = 8
# alias gram 通道：连续口语短语（重启服务器）不含任何可嵌句 alias，但其
# 2-gram 碎片（服务器）可为含该词的 alias（云服务器）恢复身份召回——
# 旧 2-gram 回退的身份版语义；权重低于整词 contains（碎片证据更弱）。
W_ALIAS_GRAM = 6
W_NAME = 5
W_CATEGORY = 1
W_DISCRIMINATIVE_TAG = 2
DISCRIMINATIVE_MAX_COVERAGE = 3

# 证据优先级（越小越强）
_PRIO_PRODUCT = 0
_PRIO_ALIAS_EXACT = 1
_PRIO_KW_EXACT = 2
_PRIO_TAG = 3
_PRIO_KW_CONTAINS = 4
_PRIO_NAME = 5
_PRIO_ALIAS_CONTAINS = 6
_PRIO_ALIAS_GRAM = 7


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
    """实体关联图谱值对象：parse/load 产静态快照 + RAM 索引，search_apis
    为唯一检索入口。"""

    version: int
    products: dict[str, _ProductNode] = field(default_factory=dict)
    apis_by_product: dict[str, tuple[_ApiNode, ...]] = field(
        default_factory=dict)
    tag_products: dict[str, int] = field(default_factory=dict)
    engine: TantivyEngine | None = None

    @classmethod
    def empty(cls) -> "EntityGraph":
        return cls(version=0)

    def search_apis(self, query: str, *, limit: int = 8,
                    category: str | None = None,
                    allowed: frozenset[str] | None = None,
                    exclude_apis: dict[str, frozenset[str]] | None = None
                    ) -> SearchApisResult:
        """跨产品检索：术语切分 → BM25 命中池 + 身份信号 → 每产品聚合
        （identity + 归一化 BM25×量纲）→ 排名短名单。allowed / exclude_apis
        为机制参数（gate 过滤后的产品白名单、hide 模式的 (product_lower →
        api_lower) 排除集，均排名前过滤保证 total/truncated 语义一致）；模块
        对 Gate/废弃索引零认知。limit 缺省 8、上限 _LIMIT_MAX；limit=-1 为
        不限制哨兵（返回全部命中，信封回显 -1 且 truncated 恒 False），
        其余值 clamp 到 [1, _LIMIT_MAX]。"""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 8
        unlimited = limit == -1
        if not unlimited:
            limit = max(1, min(limit, _LIMIT_MAX))
        empty: SearchApisResult = {"ok": True, "query": query, "total": 0,
                                   "limit": limit, "products": [],
                                   "truncated": False}
        terms = _terms(query)
        if not terms:
            return empty

        by_product = self._pool_hits(terms, exclude_apis)
        scored: list[tuple[_ProductNode, float,
                           list[tuple[int, int, str]],
                           list[tuple[float, DocHit]]]] = []
        for ps, node in self.products.items():
            if allowed is not None and ps not in allowed:
                continue
            if category and category.lower() not in node.category.lower():
                continue
            cand = self._score_product(node, terms, by_product.get(ps, []))
            if cand[1] > 0:
                scored.append(cand)
        rows = _merge_rows(scored)
        rows.sort(key=lambda r: (-r["score"], r["product"].lower()))
        page = rows if unlimited else rows[:limit]
        return {"ok": True, "query": query, "total": len(rows), "limit": limit,
                "products": page, "truncated": len(rows) > len(page)}

    def _pool_hits(self, terms: list[str],
                   exclude_apis: dict[str, frozenset[str]] | None,
                   ) -> dict[str, list[DocHit]]:
        """BM25 命中池：按产品分组、hide 排除集先剥（被排除 api 不占
        top-3 名额、不贡献分数）。引擎未装配时返回空池（纯身份信号可达）。"""
        by_product: dict[str, list[DocHit]] = {}
        if self.engine is None:
            return by_product
        for hit in self.engine.search(terms, limit=_DOC_POOL):
            exclude = (exclude_apis or {}).get(hit.product.lower())
            if exclude and hit.name.lower() in exclude:
                continue
            by_product.setdefault(hit.product, []).append(hit)
        return by_product

    def _score_product(self, node: _ProductNode, terms: list[str],
                       hits: list[DocHit],
                       ) -> tuple[_ProductNode, float,
                                  list[tuple[int, int, str]],
                                  list[tuple[float, DocHit]]]:
        """单产品评分：BM25 池内最优文档 ×量纲 + 身份信号（alias 每产品
        每查询一次——嵌入用户原句 > 术语精确等于 > 术语∈别名；name/category
        精确子串；判别 tag bonus 一次）。证据 (prio, seq, text) 供 LLM 校准。"""
        product_lower = node.product.lower()
        alias_lowers = [(a, a.lower()) for a in node.aliases]
        name_lower = node.name.lower()
        category_lower = node.category.lower()
        score = 0.0
        evidence: list[tuple[int, int, str]] = []
        seq = 0

        def _ev(prio: int, text: str) -> None:
            nonlocal seq
            evidence.append((prio, seq, text))
            seq += 1

        # alias 通道（每产品一次）：嵌入用户原句 > 术语精确等于 > 术语∈别名
        # > gram 碎片∈别名。逐别名逐术语累加会被 LLM 多别名互嵌碎片打爆
        # （实测 专属云主机/独享主机），故每产品只取最强一档。
        # 纯 ASCII 术语须 ≥3 字符才可作 contains 证据（ip ⊆ 公网ip 之类
        # 短片段是弱身份，会把 防火墙封禁ip 误导到 EIP）。
        best_alias = 0
        best_alias_ev: tuple[int, str] | None = None
        for alias, al in alias_lowers:
            if any(al in t for t in terms):
                cand = (W_ALIAS_EXACT, f"alias:{alias}")
            elif any(t in al for t in terms if _alias_contains_ok(t)):
                cand = (W_ALIAS_CONTAINS, f"alias:{alias}")
            elif any(g in al for t in terms for g in _cjk_grams(t)):
                cand = (W_ALIAS_GRAM, f"alias:{alias}")
            else:
                continue
            if cand[0] > best_alias:
                best_alias = cand[0]
                best_alias_ev = cand
        if best_alias and best_alias_ev is not None:
            score += best_alias
            _ev(_PRIO_ALIAS_EXACT if best_alias == W_ALIAS_EXACT
                else _PRIO_ALIAS_CONTAINS if best_alias == W_ALIAS_CONTAINS
                else _PRIO_ALIAS_GRAM, best_alias_ev[1])

        tag_bonus = False
        for term in terms:
            if term == product_lower:
                score += W_PRODUCT_EXACT
                _ev(_PRIO_PRODUCT, f"product:{node.product}")
            if term in name_lower:
                score += W_NAME
                _ev(_PRIO_NAME, f"name:{term}")
            elif name_lower and name_lower != term and name_lower in term:
                # name 嵌句档（镜像 alias 嵌句）：产品中文名作为子串嵌入
                # 用户原句（裸金属服务器 ⊆ 裸金属服务器重装操作系统）。
                # term==name 已由上一分支覆盖，防双计。
                score += W_NAME
                _ev(_PRIO_NAME, f"name:{name_lower}")
            if category_lower and term in category_lower:
                score += W_CATEGORY
            for hit in hits:
                # 判别 tag bonus：严格子串（gram 重叠≠包含，term 重启服务器
                # 的碎片 服务/务器 会误命中 tag 云服务器生命周期管理）。
                if not tag_bonus and term in hit.tags.lower():
                    cov = self.tag_products.get(hit.tags, 0)
                    if 0 < cov <= DISCRIMINATIVE_MAX_COVERAGE:
                        tag_bonus = True
                        score += W_DISCRIMINATIVE_TAG
                        _ev(_PRIO_TAG, f"tag:{hit.tags}({cov}产品)")
                # kw 证据：严格子串（与旧词汇表同语义；gram 级弱命中只贡献
                # BM25 分数不出证据，避免 kw: 证据名不副实）。
                kw_lowers = [kw.lower() for kw in hit.keywords]
                if any(kw == term for kw in kw_lowers):
                    _ev(_PRIO_KW_EXACT, f"kw:{term}→{hit.name}")
                elif any(term in kw for kw in kw_lowers):
                    _ev(_PRIO_KW_CONTAINS, f"kw:{term}→{hit.name}")
        if hits:
            score += _BM25_SCALE * max(h.score for h in hits)
        # 广度先验仅在产品已有证据（身份信号或 BM25 命中）时计——否则
        # 零证据产品全员上榜。
        if score > 0:
            score += _breadth_bonus(len(self.apis_by_product.get(node.product,
                                                                   ())))
        apis = sorted(((h.score, h) for h in hits),
                      key=lambda x: (-x[0], x[1].name.lower()))
        return node, round(score, 3), evidence, apis


def _breadth_bonus(n_apis: int) -> float:
    if n_apis <= 0:
        return 0.0
    ratio = math.log10(1 + n_apis) / _BREADTH_LOG
    return _W_BREADTH * ratio * ratio


def _alias_contains_ok(term: str) -> bool:
    return not term.isascii() or len(term) >= 3


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _cjk_grams(term: str) -> list[str]:
    """CJK 2-gram 碎片（alias gram 通道用，≥3 字的连续段才产生碎片）。"""
    out: list[str] = []
    for run in _CJK_RUN.findall(term):
        if len(run) >= 3:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _terms(query: str) -> list[str]:
    """术语切分：空白/标点边界 + ASCII↔CJK 边界 + 多词时整句 phrase 项。

    连续 CJK 不分词（子串语义由引擎内 CJK 2-gram token 承接）；k8s集群扩容
    之类混写按语言边界切出 k8s / 集群扩容。
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


def _merge_rows(
    scored: list[tuple[_ProductNode, float, list[tuple[int, int, str]],
                       list[tuple[float, DocHit]]]],
) -> list[SearchProductHit]:
    """同名产品归并（twin）：主产品=link 非空 > API 数 > 字典序；
    分数取成员最大值，API/证据跨成员合并去重。"""
    groups: dict[str, list[tuple[_ProductNode, float,
                                 list[tuple[int, int, str]],
                                 list[tuple[float, DocHit]]]]] = {}
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
        merged: dict[str, tuple[float, DocHit]] = {}
        for _, _, _, cand_apis in members:
            for s, hit in cand_apis:
                if hit.name not in merged or s > merged[hit.name][0]:
                    merged[hit.name] = (s, hit)
        top = sorted(merged.values(),
                     key=lambda x: (-x[0], x[1].name.lower()))[:_TOP_APIS]
        apis = cast(list[SearchApiHit], [
            {"name": h.name, "method": h.method, "summary": h.summary,
             "tags": h.tags} for _, h in top])
        seen_ev: set[str] = set()
        matched_via: list[str] = []
        for _, _, ev, _ in sorted(members, key=lambda c: c[0] is not node):
            for prio, seq, text in sorted(ev, key=lambda x: (x[0], x[1])):
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
    """把 entity-index 产物解析为 EntityGraph。严格校验：非法结构抛
    ValueError；解析成功即在 RAM 构建 BM25 索引（纯内存，进程存活期）。"""
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
    engine = _build_engine(frozen_apis)
    return EntityGraph(version=version, products=products,
                       apis_by_product=frozen_apis,
                       tag_products=tag_products, engine=engine)


# 引擎装配钩子（Phase 4 调参/测试注入 tie_breaker 与 boosts；生产缺省）。
_ENGINE_KWARGS: dict[str, Any] = {}


def _build_engine(apis_by_product: dict[str, tuple[_ApiNode, ...]],
                  ) -> TantivyEngine | None:
    """RAM 索引装配：无 API 语料时跳过（纯身份信号仍可达）。"""
    docs = [IndexedApi(product=ps, name=n.name, method=n.method,
                       summary=n.summary, tags=n.tags, keywords=n.keywords)
            for ps, nodes in apis_by_product.items() for n in nodes]
    return TantivyEngine.build(docs, **_ENGINE_KWARGS) if docs else None


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
