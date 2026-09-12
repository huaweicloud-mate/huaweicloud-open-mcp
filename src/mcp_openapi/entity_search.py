"""openapi 实体检索引擎（S16a）：tantivy BM25 + 手动 CJK 2-gram token 化。

与 entity_graph（facade）的分工：本模块只负责「API 文本语料 → BM25 命中」，
隐藏 schema/tokenizer/字段 boost/查询构造/归一化/provenance；身份信号
（alias/product/name/category）、twin 归并、信封语义全部留在 facade。
tantivy 为 in-process 依赖：索引在 RAM 构建（规避索引格式跨版本磁盘兼容），
启动一次性建索引，查询亚毫秒。

token 化设计（doc 侧与 query 侧共用同一规则，保证 token 相等即命中）：
- name 字段：整名小写 + 驼峰切分（NovaRebootServer → nova/reboot/server）；
  ASCII 不做 2-gram（部分驼峰子串如 "boot" 不再命中，接受此边界收窄）。
- summary/tags 字段：ASCII 词 + CJK 连续段整段 + CJK 2-gram。
  2-gram OR 语义 ≈ 子串匹配的统计软化：碎片各得部分分，全中最高。
- kw_exact 字段：关键词整值（精确口语命中，高 boost）+ 关键词内 ASCII 词。
- kw_grams 字段：关键词 CJK 2-gram + 关键词内 ASCII 词（contains 级证据）。
"""

import re
from dataclasses import dataclass

import tantivy

_ASCII_RUN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z]+|[0-9]+")

_INDEX_FIELDS = ("name_tok", "summary_tok", "tags_tok", "kw_exact_tok",
                 "kw_grams_tok")


@dataclass(frozen=True)
class FieldBoosts:
    """按字段的 BM25 权重（Phase 4 对金评集调参的唯一旋钮面）。"""

    name: float = 3.0
    summary: float = 1.0
    tags: float = 1.0
    kw_exact: float = 2.5
    kw_grams: float = 1.0


@dataclass(frozen=True)
class IndexedApi:
    """引擎输入文档（facade 从解析后的 _ApiNode 映射而来）。"""

    product: str
    name: str
    method: str = ""
    summary: str = ""
    tags: str = ""
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocHit:
    """单 API 文档命中：原始字段回读 + 归一化分数 + (field, term) 溯源。"""

    product: str
    name: str
    method: str
    summary: str
    tags: str
    keywords: tuple[str, ...]
    score: float
    matched: frozenset[tuple[str, str]]


def _grams(run: str) -> list[str]:
    return [run[i:i + 2] for i in range(len(run) - 1)]


def _ascii_tokens(text: str) -> set[str]:
    return set(_ASCII_RUN.findall(text.lower()))


def _camel_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _CAMEL.finditer(text)}


def _cjk_tokens(text: str, *, with_whole: bool) -> set[str]:
    out: set[str] = set()
    for run in _CJK_RUN.findall(text.lower()):
        if with_whole:
            out.add(run)
        out.update(_grams(run))
    return out


def _kw_tokens(value: str) -> tuple[set[str], set[str]]:
    """单关键词值 → (kw_exact tokens, kw_grams tokens)。

    exact 档只收 ASCII 词 + CJK 整段（整值/整段相等才命中）；
    grams 档收 ASCII 词 + CJK 2-gram（contains 级证据）。"""
    exact = _ascii_tokens(value) | set(_CJK_RUN.findall(value.lower()))
    grams = _ascii_tokens(value)
    for run in _CJK_RUN.findall(value.lower()):
        grams.update(_grams(run))
    return exact, grams


def _doc_tokens(field: str, value: str) -> list[str]:
    """doc 侧：字段原文 → 索引 token 列表（多值，每 token 一项）。"""
    if field == "name":
        v = value.lower()
        return sorted(_ascii_tokens(v)
                      | _camel_tokens(value)  # 驼峰切分须用原始大小写
                      | {v})
    if field in ("summary", "tags"):
        return sorted(_ascii_tokens(value) | _cjk_tokens(value, with_whole=True))
    exact, grams = _kw_tokens(value)
    return sorted(exact if field == "kw_exact" else grams)


def _query_tokens(field: str, terms: list[str]) -> set[str]:
    """query 侧：术语集 → 该字段的查询 token 集（与 doc 侧同规则）。"""
    out: set[str] = set()
    for term in terms:
        if field == "name":
            out |= _ascii_tokens(term) | _camel_tokens(term)
        elif field in ("summary", "tags"):
            out |= _ascii_tokens(term) | _cjk_tokens(term, with_whole=True)
        else:
            exact, grams = _kw_tokens(term)
            out |= exact if field == "kw_exact" else grams
    return out


class TantivyEngine:
    """BM25 检索引擎：build 一次（RAM 索引），search 只读。

    Interface 事实：
    - hits 按 score 降序，tie-break (product.lower(), name.lower()) 稳定；
    - score 为查询内 max 归一化的 [0,1] 值（3 位小数），恒确定；
    - matched 为 (field, term) 溯源集（term 粒度，供 facade 重建 matched_via）；
    - terms 为空抛 ValueError；无命中返回 []；limit < 1 视为 1。

    查询结构（kw 密集文档的碎片和放大压制）：字段内按术语分组——单术语的
    token 之间 should-sum（同一术语的全 gram 命中 ≫ 单 gram，子串代理）；
    术语之间按 tie_breaker DisMax（多术语部分累加，术语数不再无界放大——
    「关键词密集产品靠碎片噪音压过精确命中」的统计版防线）；字段之间
    加权 should-sum（语义通道各自贡献，与旧分通道评分同构）。
    """

    def __init__(self, searcher: tantivy.Searcher, schema: tantivy.Schema,
                 boosts: FieldBoosts, tie_breaker: float) -> None:
        self._searcher = searcher
        self._schema = schema
        self._boosts = boosts
        self._tie = tie_breaker

    @classmethod
    def build(cls, apis: list[IndexedApi], boosts: FieldBoosts | None = None,
              tie_breaker: float | None = None) -> "TantivyEngine":
        """tie_breaker=None → 术语间 should-sum（经典 BM25）；
        0 ≤ tie ≤ 1 → DisMax（max + tie×其余和）。"""
        boosts = boosts or FieldBoosts()
        tie = 1.0 if tie_breaker is None else min(1.0, max(0.0, tie_breaker))
        sb = tantivy.SchemaBuilder()
        for f in _INDEX_FIELDS:
            sb.add_text_field(f, stored=False, tokenizer_name="raw")
        for f in ("product", "name", "method", "summary", "tags", "keywords"):
            sb.add_text_field(f, stored=True, tokenizer_name="raw")
        schema = sb.build()
        index = tantivy.Index(schema)
        writer = index.writer(heap_size=50_000_000)
        for api in apis:
            kw = " ".join(api.keywords)
            vals: dict[str, list[str]] = {
                "product": [api.product],
                "name": [api.name],
                "method": [api.method.lower()],
                "summary": [api.summary],
                "tags": [api.tags],
                "keywords": list(api.keywords),
            }
            for f in _INDEX_FIELDS:
                plain = f.removesuffix("_tok")
                src = api.name if plain == "name" \
                    else api.summary if plain == "summary" \
                    else api.tags if plain == "tags" \
                    else kw
                vals[f] = _doc_tokens(plain, src)
            writer.add_document(tantivy.Document(**vals))
        writer.commit()
        index.reload()
        return cls(index.searcher(), schema, boosts, tie)

    def search(self, terms: list[str], *, limit: int) -> list[DocHit]:
        if not terms:
            raise ValueError("terms 不能为空")
        clauses = self._field_clauses(terms)
        if not clauses:
            return []
        total = tantivy.Query.boolean_query(clauses)
        raw = self._searcher.search(total, limit=max(1, limit))
        if not raw.hits:
            return []
        top = max(score for score, _ in raw.hits)
        return self._render(raw.hits, terms, top)[:limit]

    def search_product(self, terms: list[str], product: str, *,
                       limit: int) -> list[DocHit]:
        """单产品补召回：product 字段 MUST 过滤下的 BM25（身份活跃但落出
        全局命中池的产品用）。分数仍按「主查询全局 top」归一化——内部先以
        limit=1 重放主查询取全局 top，保证与 search 的分数跨产品可比。
        无全局命中时返回 []。"""
        if not terms:
            raise ValueError("terms 不能为空")
        product = product.strip()
        if not product:
            raise ValueError("product 不能为空")
        main = tantivy.Query.boolean_query(self._field_clauses(terms))
        main_top = self._searcher.search(main, limit=1)
        if not main_top.hits:
            return []
        top = main_top.hits[0][0]
        if top <= 0:
            return []
        scoped = tantivy.Query.boolean_query([
            (tantivy.Occur.Must, tantivy.Query.term_query(
                self._schema, "product", product)),
            (tantivy.Occur.Must, main),
        ])
        raw = self._searcher.search(scoped, limit=max(1, limit))
        if not raw.hits:
            return []
        return self._render(raw.hits, terms, top)

    def _field_clauses(self, terms: list[str]) -> list:
        clauses = []
        for f in _INDEX_FIELDS:
            plain = f.removesuffix("_tok")
            term_qs: list[tantivy.Query] = []
            for term in terms:
                toks = sorted(_query_tokens(plain, [term]))
                if not toks:
                    continue
                term_qs.append(tantivy.Query.boolean_query(
                    [(tantivy.Occur.Should, tantivy.Query.term_query(
                        self._schema, f, tok)) for tok in toks]))
            if not term_qs:
                continue
            field_q = tantivy.Query.disjunction_max_query(
                term_qs, tie_breaker=self._tie)
            clauses.append((tantivy.Occur.Should,
                            tantivy.Query.boost_query(field_q,
                                                      getattr(self._boosts,
                                                              plain))))
        return clauses

    def _render(self, raw_hits: list, terms: list[str],
                top: float) -> list[DocHit]:
        hits: list[DocHit] = []
        for score, addr in raw_hits:
            d = self._searcher.doc(addr).to_dict()
            norm = round(score / top, 3) if top > 0 else 0.0
            hits.append(DocHit(
                product=d["product"][0], name=d["name"][0],
                method=(d.get("method") or [""])[0],
                summary=(d.get("summary") or [""])[0],
                tags=(d.get("tags") or [""])[0],
                keywords=tuple(d.get("keywords") or ()),
                score=norm, matched=frozenset()))
        # provenance：短名单复扫（doc 侧 token 与 query 侧 token 求交）
        proven: list[DocHit] = []
        for h in hits:
            matched: set[tuple[str, str]] = set()
            doc_side = {
                "name": _doc_tokens("name", h.name),
                "summary": _doc_tokens("summary", h.summary),
                "tags": _doc_tokens("tags", h.tags),
            }
            kw_exact_toks: set[str] = set()
            kw_gram_toks: set[str] = set()
            for kw in h.keywords:
                e, g = _kw_tokens(kw)
                kw_exact_toks |= e
                kw_gram_toks |= g
            for term in terms:
                for f in ("name", "summary", "tags"):
                    if _query_tokens(f, [term]) & set(doc_side[f]):
                        matched.add((f, term))
                q_exact, q_grams = _kw_tokens(term)
                if q_exact & kw_exact_toks:
                    matched.add(("kw_exact", term))
                if q_grams & kw_gram_toks:
                    matched.add(("kw_grams", term))
            proven.append(DocHit(
                product=h.product, name=h.name, method=h.method,
                summary=h.summary, tags=h.tags, keywords=h.keywords,
                score=h.score, matched=frozenset(matched)))
        proven.sort(key=lambda h: (-h.score, h.product.lower(), h.name.lower()))
        return proven

