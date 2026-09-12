"""graph 阶段：实体关联图谱确定性构建（纯函数核心 + stage 落盘壳）。

输入 raw/apis_docs.json（apis）+ raw/huawei_products.json（groups）+
configs/entity-knowledge.json（三 section：aliases/relations/api_keywords，
fingerprints 由 --llm 路径维护）→ data/graph/entity-index.json。
节点边推导（twin/attributive/tag_products）与 knowledge 并入全部在本模块实现内；
产物为运行时 mcp_openapi.entity_graph 的唯一数据契约。
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("apie.build_entity_graph")

ARTIFACT_VERSION = 1
KNOWLEDGE_FILE = "entity-knowledge.json"


def _product_info(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{productshort: {name, category, is_global, link, attributive}}。"""
    info: dict[str, dict[str, Any]] = {}
    for g in groups or []:
        category = g.get("name") or ""
        for p in g.get("products") or []:
            ps = (p.get("productshort") or "").strip()
            if not ps:
                continue
            info[ps] = {
                "name": p.get("name") or "",
                "category": category,
                "is_global": bool(p.get("is_global")),
                "link": (p.get("link") or "").strip() or None,
                "attributive": (p.get("attributive_product") or "").strip(),
            }
    return info


def _add_edge(related: dict[tuple[str, str], dict[str, Any]],
              to: str, kind: str, via: str | None) -> None:
    """(to, kind) 去重，首个优先；via 仅在有值时保留。"""
    key = (to, kind)
    if key in related:
        return
    edge: dict[str, Any] = {"product": to, "kind": kind}
    if via:
        edge["via"] = via
    related[key] = edge


def build_graph(apis: list[dict[str, Any]],
                groups: list[dict[str, Any]],
                knowledge: dict[str, Any] | None) -> dict[str, Any]:
    """构建 entity-index 产物。纯函数：不读盘、不涉时钟（generated_at 由 stage 注入）。

    - 节点口径：仅 huawei_products 收录且在 apis 中出现的产品（死端产品排除，
      apis_docs 独有产品如 Cloudtest 丢弃）；
    - twin：同名中文产品组两两互指（主产品判定在运行时按 link/API 数做）；
    - attributive：groups 的 attributive_product（child→parent）；
    - semantic：knowledge.relations（双端必须为图内产品，去自环）；
    - 防御式过滤：knowledge 引用未知产品/API 一律丢弃（构建为运行时前最后防线）。
    """
    knowledge = knowledge or {}
    info = _product_info(groups)

    apis_by_product: dict[str, list[dict[str, Any]]] = {}
    for a in apis or []:
        ps = (a.get("product_short") or "").strip()
        if ps in info:
            apis_by_product.setdefault(ps, []).append(a)

    name_by_product: dict[str, str] = {}
    for ps in apis_by_product:
        name_by_product[ps] = info[ps]["name"]

    # 别名：targets ∩ 图内产品，逐目标挂别名（文件序聚合，逐产品去重）
    aliases: dict[str, list[str]] = {ps: [] for ps in apis_by_product}
    seen_alias: dict[str, set[str]] = {ps: set() for ps in apis_by_product}
    for entry in knowledge.get("aliases") or []:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("alias")
        targets = entry.get("targets")
        if not isinstance(alias, str) or not alias.strip() \
                or not isinstance(targets, list):
            continue
        alias = alias.strip()
        for t in targets:
            if isinstance(t, str) and t in apis_by_product \
                    and alias not in seen_alias[t]:
                seen_alias[t].add(alias)
                aliases[t].append(alias)

    # 相关产品边：derived attributive → knowledge semantic → derived twin
    related: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
        ps: {} for ps in apis_by_product}
    for ps, meta in info.items():
        target = meta["attributive"]
        if ps in related and target in related and target != ps:
            _add_edge(related[ps], target, "attributive", None)
    for entry in knowledge.get("relations") or []:
        if not isinstance(entry, dict):
            continue
        src = entry.get("from")
        dst = entry.get("to")
        kind = entry.get("kind") or "semantic"
        if isinstance(src, str) and isinstance(dst, str) \
                and src in related and dst in related and src != dst:
            _add_edge(related[src], dst, kind, entry.get("note"))
    by_name: dict[str, list[str]] = {}
    for ps, name in name_by_product.items():
        if name:
            by_name.setdefault(name, []).append(ps)
    for name, members in by_name.items():
        if len(members) < 2:
            continue
        for ps in members:
            for other in members:
                if other != ps:
                    _add_edge(related[ps], other, "twin", None)

    # API 关键词：(product, api lower) 归一化匹配，防 LLM 大小写漂移
    keywords: dict[tuple[str, str], list[str]] = {}
    raw_kws = knowledge.get("api_keywords") or {}
    if isinstance(raw_kws, dict):
        for ps, per_api in raw_kws.items():
            if not isinstance(ps, str) or ps not in apis_by_product \
                    or not isinstance(per_api, dict):
                continue
            for api, kws in per_api.items():
                if not isinstance(api, str) or not isinstance(kws, list):
                    continue
                cleaned = [k.strip() for k in kws
                           if isinstance(k, str) and k.strip()]
                if cleaned:
                    keywords[(ps, api.strip().lower())] = cleaned

    product_nodes: list[dict[str, Any]] = []
    for ps in sorted(apis_by_product, key=lambda x: x.lower()):
        meta = info[ps]
        edges = sorted(related[ps].values(),
                       key=lambda e: (e["product"].lower(), e["kind"]))
        product_nodes.append({
            "product": ps,
            "name": meta["name"],
            "category": meta["category"],
            "is_global": meta["is_global"],
            "link": meta["link"],
            "aliases": aliases[ps],
            "related": edges,
        })

    api_nodes: list[dict[str, Any]] = []
    for ps in sorted(apis_by_product, key=lambda x: x.lower()):
        for a in sorted(apis_by_product[ps],
                        key=lambda x: ((x.get("name") or "").lower(),
                                       x.get("name") or "")):
            node: dict[str, Any] = {
                "n": a.get("name") or "",
                "m": a.get("method") or "",
                "s": a.get("summary") or "",
                "t": a.get("tags") or "",
                "p": ps,
            }
            kws = keywords.get((ps, (a.get("name") or "").strip().lower()))
            if kws:
                node["k"] = kws
            api_nodes.append(node)

    tag_products: dict[str, int] = {}
    for ps, group in apis_by_product.items():
        for a in group:
            tag = (a.get("tags") or "").strip()
            if tag:
                tag_products[tag] = tag_products.get(tag, 0) + 1

    return {
        "version": ARTIFACT_VERSION,
        "products": product_nodes,
        "apis": api_nodes,
        "tag_products": tag_products,
    }


# ---------- stage 落盘壳 ----------

def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _write_json_atomic(path: Path, data: Any) -> None:
    """原子落盘（tmp+os.replace），与 data engine/spill 同一不变量。"""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp-part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def refresh_knowledge_file(root: Path, *,
                           transport: Any = None,
                           batch_products: int = 20,
                           batch_apis: int = 80,
                           force: bool = False) -> dict[str, Any]:
    """--llm 路径：LLM 抽取 → 合并 → 原子写 knowledge 文件 → 返回新 knowledge。

    transport 注入点（测试用 fake；生产经 env 构造真实 OpenAI-compatible
    transport）。env 未配置且未注入 transport 时记 WARNING 跳过，返回原
    knowledge。force=True 绕过指纹全量重抽（prompt/闸门调整后用）。
    on_update 增量落盘：每产品完成即合并原子写（长任务中断后
    凭指纹台账续跑，只补缺口）。
    """
    from .entity_extract import (
        load_llm_env,
        make_openai_transport,
        refresh_knowledge,
    )

    knowledge_path = root / "configs" / KNOWLEDGE_FILE
    current = _read_json(knowledge_path) if knowledge_path.exists() else {}
    if transport is None:
        env = load_llm_env()
        if env is None:
            logger.warning("llm extraction skipped: ENTITY_LLM_* env not set")
            return current
        transport = make_openai_transport(env["base_url"], env["api_key"],
                                          env["model"])
    apis = _read_json(root / "raw" / "apis_docs.json").get("apis") or []
    groups = _read_json(root / "raw" / "huawei_products.json").get("groups") or []

    def _persist(merged: dict[str, Any]) -> None:
        _write_json_atomic(knowledge_path, merged)

    updated = refresh_knowledge(current, apis, groups, transport=transport,
                                batch_products=batch_products,
                                batch_apis=batch_apis,
                                on_update=_persist, force=force)
    _write_json_atomic(knowledge_path, updated)
    logger.info("knowledge refreshed: aliases=%d relations=%d "
                "api_keywords_products=%d",
                len(updated.get("aliases") or []),
                len(updated.get("relations") or []),
                len(updated.get("api_keywords") or {}))
    return updated


def build_artifact_files(root: Path) -> dict[str, int]:
    """graph 阶段编排：读 raw + knowledge → 落盘 data/graph/entity-index.json
    并刷新 configs/entity-index.json（wheel bundle 副本）。
    返回摘要计数。"""
    from datetime import datetime, timezone

    apis = _read_json(root / "raw" / "apis_docs.json").get("apis") or []
    groups = _read_json(root / "raw" / "huawei_products.json").get("groups") or []
    knowledge_path = root / "configs" / KNOWLEDGE_FILE
    knowledge = _read_json(knowledge_path) if knowledge_path.exists() else {}

    artifact = build_graph(apis, groups, knowledge)
    artifact["generated_at"] = datetime.now(timezone.utc).isoformat()

    data_path = root / "data" / "graph" / "entity-index.json"
    _write_json(data_path, artifact)
    _write_json(root / "configs" / "entity-index.json", artifact)
    summary = {"products": len(artifact["products"]),
               "apis": len(artifact["apis"]),
               "aliases": sum(len(p["aliases"]) for p in artifact["products"]),
               "semantic_edges": sum(
                   1 for p in artifact["products"]
                   for e in p["related"] if e["kind"] == "semantic"),
               "api_keywords": sum(len(a.get("k") or [])
                                   for a in artifact["apis"])}
    logger.info("graph artifact: %s", summary)
    return summary


def main() -> int:
    """api-refresh graph 阶段入口：--llm 时先跑构建期 LLM 抽取再组装产物。"""
    import argparse

    from common.logconf import configure_logging
    from common.paths import project_root

    p = argparse.ArgumentParser(prog="api-refresh graph")
    p.add_argument("--llm", action="store_true",
                   help="构建期 LLM 语义抽取（需 ENTITY_LLM_BASE_URL/"
                        "API_KEY/MODEL 环境变量；指纹增量+增量落盘，可断点续跑）")
    p.add_argument("--llm-force", action="store_true",
                   help="绕过指纹台账全量重抽（prompt/闸门调整后用）")
    p.add_argument("--batch-products", type=int, default=20,
                   help="产品级抽取批大小（默认 20；端点吞吐慢时调小）")
    p.add_argument("--batch-apis", type=int, default=80,
                   help="API 关键词抽取批大小（默认 80）")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-file", default=None)
    args = p.parse_args()
    configure_logging(program="api-refresh-graph",
                      level=args.log_level, log_file=args.log_file)
    root = Path(project_root())
    if args.llm:
        refresh_knowledge_file(root, batch_products=args.batch_products,
                               batch_apis=args.batch_apis,
                               force=args.llm_force)
    build_artifact_files(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
