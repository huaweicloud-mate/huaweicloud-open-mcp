"""LLM 构建期语义抽取：指纹增量 + 分批 + 严格校验 + 合并策略。

编排纯函数 refresh_knowledge（transport/sleep_fn 注入，不读盘不写盘）产出
新 knowledge 全文；stage 层原子落盘。合并策略（curated 永不覆盖 / llm 整体
替换 / api_keywords 按产品替换 / fingerprints overlay）集中在 internal seam
merge_knowledge。真实 transport 适配器 make_openai_transport 走
OpenAI-compatible /chat/completions（urllib，429 退避）。

防幻觉三道闸：严格 JSON 解析 → 交叉验证（产品/API 存在性、长度、去重、
封顶、去自环）→ 无效条目丢弃记 WARNING 台账。LLM 输出只存在于构建期。
"""

import hashlib
import json
import logging
import time
from typing import Any, Callable

from common import http

logger = logging.getLogger("apie.entity_extract")

KNOWLEDGE_VERSION = 1
PRODUCTS_FP = "__products__"
ALIAS_MAX = 8
KEYWORD_MAX = 12
KEYWORDS_PER_API = 8
NOTE_MAX = 60
BATCH_SLEEP = 0.5

Transport = Callable[[list[dict[str, str]]], str]

_SYSTEM_PROMPT = ("你是华为云产品目录的实体关系抽取助手。"
                  "只输出一个 JSON 对象，不要解释、不要输出 markdown 之外的任何内容。")


# ---------- 配置与真实 transport ----------

def load_llm_env() -> dict[str, str] | None:
    """读取构建期 LLM 环境变量三元组；任一缺失返回 None（stage 跳过抽取）。"""
    import os
    base = os.environ.get("ENTITY_LLM_BASE_URL")
    key = os.environ.get("ENTITY_LLM_API_KEY")
    model = os.environ.get("ENTITY_LLM_MODEL")
    if not base or not key or not model:
        return None
    return {"base_url": base, "api_key": key, "model": model}


def make_openai_transport(base_url: str, api_key: str, model: str, *,
                          timeout: int = 120, retries: int = 4,
                          backoff: float = 4.0) -> Transport:
    """真实 transport 适配器：OpenAI-compatible chat/completions → 文本 content。"""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}

    def _call(messages: list[dict[str, str]]) -> str:
        resp = http.post_json_retry(
            url, body={"model": model, "messages": messages, "temperature": 0},
            headers=headers, retries=retries, backoff=backoff, timeout=timeout)
        choices = resp.get("choices") or []
        if not choices:
            raise http.ApieHttpError("llm response missing choices")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise http.ApieHttpError("llm response missing content")
        return content

    return _call


# ---------- 指纹与目录视图 ----------

def _product_info(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    for g in groups or []:
        category = g.get("name") or ""
        for p in g.get("products") or []:
            ps = (p.get("productshort") or "").strip()
            if ps:
                info[ps] = {"name": p.get("name") or "",
                            "category": category}
    return info


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _product_fingerprint(product_apis: list[dict[str, Any]]) -> str:
    lines = sorted(
        f"{a.get('name') or ''}\t{a.get('summary') or ''}"
        for a in product_apis)
    return _fingerprint("\n".join(lines))


def _discriminative_tags(apis_by_product: dict[str, list[dict[str, Any]]],
                         tag_coverage: dict[str, int]) -> dict[str, list[str]]:
    """每产品 top-5 判别性 tag（覆盖 ≤3 产品），供 prompt grounding。"""
    tags_by_product: dict[str, list[str]] = {}
    for ps, group in apis_by_product.items():
        seen: set[str] = set()
        for a in group:
            t = (a.get("tags") or "").strip()
            if t and tag_coverage.get(t, 0) <= 3:
                seen.add(t)
        tags_by_product[ps] = sorted(seen)[:5]
    return tags_by_product


def _roster_lines(node_products: list[str], info: dict[str, Any],
                   tags_by_product: dict[str, list[str]]) -> list[str]:
    lines = []
    for ps in node_products:
        meta = info[ps]
        tags = ",".join(tags_by_product.get(ps) or [])
        lines.append(f"{ps} | {meta['name']} | {meta['category']}"
                     + (f" | {tags}" if tags else ""))
    return lines


# ---------- LLM 输出严格解析与校验 ----------

def _parse_llm_json(text: str) -> dict[str, Any]:
    """宽松定位 JSON 对象（容忍 markdown 围栏与前后缀文本），严格结构校验。"""
    if not isinstance(text, str):
        raise ValueError("llm content not a string")
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("llm content has no JSON object")
    data = json.loads(stripped[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("llm JSON is not an object")
    return data


def _valid_alias_entries(data: dict[str, Any], node_set: set[str],
                         name_set: set[str]) -> list[dict[str, Any]]:
    """别名合法性：非空 1-8 字、目标在图内；等于产品中文名或 product_short
    一律丢弃（子串匹配已覆盖官方名，别名只收口语增量）。"""
    entries = []
    for item in data.get("aliases") or []:
        if not isinstance(item, dict):
            continue
        alias = item.get("alias")
        targets = item.get("targets")
        if not isinstance(alias, str):
            continue
        alias = alias.strip()
        if not 1 <= len(alias) <= ALIAS_MAX or not isinstance(targets, list):
            continue
        folded = alias.casefold()
        if folded in name_set or folded in node_set:
            continue
        valid = [t.strip() for t in targets
                 if isinstance(t, str) and t.strip() in node_set]
        if not valid:
            continue
        entries.append({"alias": alias, "targets": valid, "source": "llm"})
    return entries


def _valid_relation_entries(data: dict[str, Any],
                            node_set: set[str]) -> list[dict[str, Any]]:
    entries = []
    for item in data.get("relations") or []:
        if not isinstance(item, dict):
            continue
        src = item.get("from")
        dst = item.get("to")
        note = item.get("note")
        if not isinstance(src, str) or not isinstance(dst, str) \
                or not isinstance(note, str) or not note.strip():
            continue
        src, dst = src.strip(), dst.strip()
        if src not in node_set or dst not in node_set or src == dst:
            continue
        entries.append({"from": src, "to": dst, "kind": "semantic",
                        "note": note.strip()[:NOTE_MAX], "source": "llm"})
    return entries


def _valid_keyword_entries(data: dict[str, Any],
                           menu_map: dict[str, str]) -> dict[str, list[str]]:
    """合并全部条目后按 API 去重并封顶 ≤KEYWORDS_PER_API。
    键保留规范 API 名（menu_map: lower→canonical），仅匹配用小写。"""
    collected: dict[str, list[str]] = {}
    for item in data.get("keywords") or []:
        if not isinstance(item, dict):
            continue
        api = item.get("api")
        kws = item.get("keywords")
        if not isinstance(api, str) or api.strip().lower() not in menu_map \
                or not isinstance(kws, list):
            continue
        bucket = collected.setdefault(api.strip().lower(), [])
        for kw in kws:
            if not isinstance(kw, str):
                continue
            kw = kw.strip()
            if 1 <= len(kw) <= KEYWORD_MAX and kw not in bucket:
                bucket.append(kw)
    return {menu_map[api]: kws[:KEYWORDS_PER_API]
            for api, kws in collected.items()}


# ---------- 合并策略（internal seam）----------

def merge_knowledge(old: dict[str, Any] | None,
                    update: dict[str, Any]) -> dict[str, Any]:
    """curated 永不覆盖；llm 条目按世代整体替换——update 提供该 section
    （key 存在）即替换整代 llm 条目，缺席则沿用旧 llm（no-op 重跑幂等）；
    api_keywords/fingerprints 按产品 overlay。产出全新 dict，不改输入。"""
    old = old or {}

    def _gen(key: str, source: str) -> list[dict[str, Any]]:
        base = update.get(key) if key in update else old.get(key)
        return [e for e in base or []
                if isinstance(e, dict) and e.get("source") == source]

    aliases, have_alias = [], set()
    for a in [x for x in old.get("aliases") or []
              if isinstance(x, dict) and x.get("source") != "llm"] \
            + _gen("aliases", "llm"):
        if a.get("alias") not in have_alias:
            aliases.append(a)
            have_alias.add(a.get("alias"))
    relations, have_rel = [], set()
    for r in [x for x in old.get("relations") or []
              if isinstance(x, dict) and x.get("source") != "llm"] \
            + _gen("relations", "llm"):
        key = (r.get("from"), r.get("to"), r.get("kind"))
        if key not in have_rel:
            relations.append(r)
            have_rel.add(key)
    api_keywords = {k: dict(v) for k, v in (old.get("api_keywords") or {}).items()
                    if isinstance(v, dict)}
    api_keywords.update({k: dict(v)
                         for k, v in (update.get("api_keywords") or {}).items()
                         if isinstance(v, dict)})
    fingerprints = dict(old.get("fingerprints") or {})
    fingerprints.update(update.get("fingerprints") or {})
    return {"version": KNOWLEDGE_VERSION, "aliases": aliases,
            "relations": relations, "api_keywords": api_keywords,
            "fingerprints": fingerprints}


# ---------- 编排 ----------

def _emit_update(on_update: Callable[[dict[str, Any]], None] | None,
                 current: dict[str, Any] | None,
                 update: dict[str, Any]) -> None:
    """增量落盘回调（best-effort）：回调异常仅告警，不中断抽取。"""
    if on_update is None:
        return
    try:
        on_update(merge_knowledge(current, update))
    except Exception:
        logger.warning("on_update callback failed (persistence skipped)",
                       exc_info=True)


def refresh_knowledge(current: dict[str, Any] | None,
                      apis: list[dict[str, Any]],
                      groups: list[dict[str, Any]], *,
                      transport: Transport,
                      sleep_fn: Callable[[float], None] = time.sleep,
                      batch_products: int = 20,
                      batch_apis: int = 80,
                      on_update: Callable[[dict[str, Any]], None] | None = None
                      ) -> dict[str, Any]:
    """指纹增量抽取：roster 变化→产品级全量重抽（aliases+relations）；
    API 清单变化的产品→重抽该产品关键词。批失败跳过（指纹不更新，
    下次重跑自动重试）。纯数据进出，不改输入。

    on_update（增量落盘回调）：产品级完成、每产品 API 批完成时以合并后
    knowledge 全文回调一次（best-effort，回调异常仅告警不中断）——
    长任务中断后 stage 凭已落盘指纹续跑，只补缺口。
    """
    info = _product_info(groups)
    apis_by_product: dict[str, list[dict[str, Any]]] = {}
    for a in apis or []:
        ps = (a.get("product_short") or "").strip()
        if ps in info:
            apis_by_product.setdefault(ps, []).append(a)
    node_products = sorted(apis_by_product, key=lambda x: x.lower())
    node_set = set(node_products)
    name_set = {info[ps]["name"].casefold() for ps in node_products
                if info[ps]["name"]}

    tag_coverage: dict[str, int] = {}
    for group in apis_by_product.values():
        for a in group:
            t = (a.get("tags") or "").strip()
            if t:
                tag_coverage[t] = tag_coverage.get(t, 0) + 1
    tags_by_product = _discriminative_tags(apis_by_product, tag_coverage)

    current = current or {}
    fps = current.get("fingerprints") or {}
    roster_fp = _fingerprint("\n".join(
        f"{ps}\t{info[ps]['name']}\t{info[ps]['category']}"
        for ps in node_products))
    needs_product_level = fps.get(PRODUCTS_FP) != roster_fp
    changed = [ps for ps in node_products
               if fps.get(ps) != _product_fingerprint(apis_by_product[ps])]

    # 世代语义：aliases/relations 仅在产品级成功跑完后才写入 update
    #（key 缺席 = 无产品级抽取，沿用旧 llm 条目）
    update: dict[str, Any] = {"api_keywords": {}, "fingerprints": {}}
    product_fp_by_ps = {ps: _product_fingerprint(apis_by_product[ps])
                        for ps in node_products}

    if needs_product_level and node_products:
        roster = _roster_lines(node_products, info, tags_by_product)
        pl_aliases: list[dict[str, Any]] = []
        pl_relations: list[dict[str, Any]] = []
        ok = True
        for start in range(0, len(roster), max(batch_products, 1)):
            chunk = roster[start:start + max(batch_products, 1)]
            prompt = (
                "华为云产品清单（product_short | 中文名 | 分类 | 判别性标签）：\n"
                + "\n".join(chunk)
                + "\n\n任务一 aliases：为产品列出用户口语中可能的别名"
                  "（如 云主机），不含产品中文名本身，多产品共用时 targets 列全。\n"
                  "任务二 relations：提出有助于从用户意图定位产品的产品间语义关联"
                  "（如 弹性伸缩管理弹性云服务器），附简短 note（≤30字）。\n"
                '只输出 JSON：{"aliases": [{"alias": "...", "targets": ["产品"]}],'
                ' "relations": [{"from": "产品", "to": "产品", "note": "..."}]}')
            try:
                data = _parse_llm_json(transport([
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}]))
                pl_aliases.extend(
                    _valid_alias_entries(data, node_set, name_set))
                pl_relations.extend(_valid_relation_entries(data, node_set))
            except Exception:
                ok = False
                logger.warning("product batch %d failed, skipped",
                               start // max(batch_products, 1), exc_info=True)
            finally:
                sleep_fn(BATCH_SLEEP)
        if ok:
            # 世代语义：产品级成功跑完（即使全无效）→ 整代替换并更新 roster 指纹
            update["aliases"] = pl_aliases
            update["relations"] = pl_relations
            update["fingerprints"][PRODUCTS_FP] = roster_fp
            _emit_update(on_update, current, update)

    for ps in changed:
        menu = sorted(apis_by_product[ps],
                      key=lambda a: ((a.get("name") or "").lower(),
                                     a.get("name") or ""))
        menu_map = {(a.get("name") or "").strip().lower():
                    (a.get("name") or "").strip() for a in menu}
        collected: dict[str, list[str]] = {}
        ok = True
        for start in range(0, len(menu), max(batch_apis, 1)):
            api_chunk = menu[start:start + max(batch_apis, 1)]
            lines = [f"{a.get('name')} | {a.get('summary')} | {a.get('tags')}"
                     for a in api_chunk]
            prompt = (
                f"产品：{info[ps]['name']}（{ps}）。API 清单（name | summary | tag）：\n"
                + "\n".join(lines)
                + "\n\n为每个 API 抽取用户口语查询关键词（如 重启服务器/开机/扩容），"
                  f"每个 1-{KEYWORD_MAX} 字，每个 API 最多 {KEYWORDS_PER_API} 个，"
                  "不照抄整句 summary。\n"
                '只输出 JSON：{"keywords": [{"api": "name", "keywords": ["..."]}]}')
            try:
                data = _parse_llm_json(transport([
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}]))
                collected.update(_valid_keyword_entries(data, menu_map))
            except Exception:
                ok = False
                logger.warning("api keyword batch %s@%d failed, skipped",
                               ps, start, exc_info=True)
            finally:
                sleep_fn(BATCH_SLEEP)
        if ok:
            if collected:
                update["api_keywords"][ps] = collected
            update["fingerprints"][ps] = product_fp_by_ps[ps]
            _emit_update(on_update, current, update)

    return merge_knowledge(current, update)
