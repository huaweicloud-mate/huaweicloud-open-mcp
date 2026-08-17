"""把 OpenAPI 2.0 片段按 tag 合并为完整文档。"""

import collections
import json
from typing import Any


def merge_doc(apis: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[tuple]]:
    doc: dict[str, Any] = {
        "swagger": "2.0",
        "info": {"title": "", "version": "1.0"},
        "host": None,
        "basePath": "/",
        "schemes": ["https"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "paths": {},
        "definitions": {},
        "parameters": {},
        "responses": {},
        "tags": [],
    }
    defs: dict[str, Any] = {}
    params: dict[str, Any] = {}
    responses: dict[str, Any] = {}
    op_name_seen: set[str] = set()
    path_dup: list[tuple] = []

    api_items = list(apis.items())
    host_counts: collections.Counter[str] = collections.Counter()
    for k, a in api_items:
        if a.get("host"):
            host_counts[a["host"]] += 1
    doc["host"] = max(host_counts, key=lambda h: host_counts[h]) if host_counts else None

    for k, a in api_items:
        for path, path_item in (a.get("paths") or {}).items():
            for method, op in path_item.items():
                if not isinstance(op, dict):
                    continue
                op_id = op.get("operationId") or ""
                if op_id in op_name_seen:
                    path_dup.append((path, method, op_id))
                    continue
                op_name_seen.add(op_id)

                op = dict(op)
                if "tags" in op:
                    tags = op["tags"]
                    if isinstance(tags, str):
                        tags = [tags]
                    op_tags = [t if isinstance(t, dict) else {"name": str(t)}
                               for t in tags if isinstance(t, (str, dict))]
                    op["tags"] = [t["name"] for t in op_tags if isinstance(t, dict)]
                    for t in op_tags:
                        if isinstance(t, dict):
                            doc["tags"].append(t)

                if path not in doc["paths"]:
                    doc["paths"][path] = {}
                if method not in doc["paths"][path]:
                    doc["paths"][path][method] = op
                else:
                    path_dup.append((path, method, op_id))

        for dn, dv in (a.get("definitions") or {}).items():
            if dn in defs:
                cur = json.dumps(defs[dn], sort_keys=True)
                new = json.dumps(dv, sort_keys=True)
                if cur != new and not defs[dn].get("description") and dv.get("description"):
                    defs[dn] = dv
            else:
                defs[dn] = dv

        for pn, pv in (a.get("parameters") or {}).items():
            if pn not in params:
                params[pn] = pv
            else:
                if json.dumps(params[pn], sort_keys=True) != json.dumps(pv, sort_keys=True):
                    newname = pn
                    i = 1
                    while (newname in params
                           and json.dumps(params.get(newname), sort_keys=True) != json.dumps(pv, sort_keys=True)):
                        newname = f"{pn}_{i}"
                        i += 1
                    params[newname] = pv
                    for path, path_item in (a.get("paths") or {}).items():
                        for method, op in path_item.items():
                            if not isinstance(op, dict):
                                continue
                            for p in (op.get("parameters") or []):
                                if isinstance(p, dict) and p.get("$ref") == f"#/parameters/{pn}":
                                    p["$ref"] = f"#/parameters/{newname}"

        for rn, rv in (a.get("responses") or {}).items():
            if rn not in responses:
                responses[rn] = rv
            else:
                if json.dumps(responses[rn], sort_keys=True) != json.dumps(rv, sort_keys=True):
                    newname = rn
                    i = 1
                    while (newname in responses
                           and json.dumps(responses.get(newname), sort_keys=True) != json.dumps(rv, sort_keys=True)):
                        newname = f"{rn}_{i}"
                        i += 1
                    responses[newname] = rv
                    for path, path_item in (a.get("paths") or {}).items():
                        for method, op in path_item.items():
                            if not isinstance(op, dict):
                                continue
                            for code, resp in (op.get("responses") or {}).items():
                                if isinstance(resp, dict) and resp.get("$ref") == f"#/responses/{rn}":
                                    resp["$ref"] = f"#/responses/{newname}"

    doc["definitions"] = defs
    doc["parameters"] = params
    doc["responses"] = responses
    doc["paths"] = dict(doc["paths"])
    doc["tags"] = list({json.dumps(t, sort_keys=True): t for t in doc["tags"]}.values())

    return doc, path_dup


def main() -> None:
    import os
    import shutil

    from . import region_paths

    src = region_paths.openapi2_dir()
    out = region_paths.merged_dir()
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)

    total_docs = 0
    total_ops = 0
    stats: collections.Counter[str] = collections.Counter()
    index = {}

    for ps in sorted(os.listdir(src)):
        pdir = os.path.join(src, ps)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                data = json.load(f)
            apis = data.get("apis", {})
            tag = data.get("tag", fn[:-5])

            doc, dup = merge_doc(apis)
            doc["info"]["title"] = f"{ps} - {tag}"
            doc["info"]["version"] = "1.0"

            op_count = sum(len(pi) for pi in doc["paths"].values())
            total_docs += 1
            total_ops += op_count

            out_dir = os.path.join(out, ps)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)

            index[f"{ps}/{fn}"] = {
                "tag": tag,
                "operations": op_count,
                "path_conflicts_dropped": len(dup),
            }
            if dup:
                stats["files_with_path_dup"] += 1
                stats["path_dup_total"] += len(dup)

    with open(os.path.join(out, "_index.json"), "w", encoding="utf-8") as f:
        json.dump({
            "total_docs": total_docs,
            "total_operations": total_ops,
            "path_conflict_total": stats["path_dup_total"],
            "files": index,
        }, f, ensure_ascii=False, indent=2)

    print(f"docs: {total_docs}, operations: {total_ops}, path dup: {stats['path_dup_total']}")


if __name__ == "__main__":
    main()
