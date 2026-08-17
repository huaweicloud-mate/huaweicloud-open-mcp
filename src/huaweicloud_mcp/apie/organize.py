"""组织最终产物：统一命名 → data/openapi/{Product}/{Tag}.json。"""

import collections
import json
import os
import re
import shutil
from typing import Any, cast

from ..paths import project_root

TRANSLATIONS_FILE = str(project_root() / "configs" / "tag_translations.json")

PRODUCT_CANONICAL: dict[str, str] = {
    "cloudtest": "CloudTest",
}


def sanitize_tag(name: str) -> str:
    s = name.replace("/", "_").replace("\\", "_").strip()
    s = re.sub(r'[<>:"|?*]', "_", s)
    return s


def load_translations() -> dict[str, str]:
    if os.path.exists(TRANSLATIONS_FILE):
        with open(TRANSLATIONS_FILE, encoding="utf-8") as f:
            return cast(dict[str, str], json.load(f))
    return {}


def merge_multi(docs: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], int]:
    base = next(iter(docs.values()))
    base = json.loads(json.dumps(base))
    seen_ops = set()
    dup = 0
    for fn, doc in docs.items():
        if doc is base:
            continue
        for path, pi in (doc.get("paths") or {}).items():
            for method, op in pi.items():
                op_id = op.get("operationId")
                if op_id in seen_ops:
                    dup += 1
                    continue
                seen_ops.add(op_id)
                if path not in base["paths"]:
                    base["paths"][path] = {}
                if method not in base["paths"][path]:
                    base["paths"][path][method] = op
        for dn, dv in (doc.get("definitions") or {}).items():
            if dn not in base["definitions"]:
                base["definitions"][dn] = dv
            elif not base["definitions"][dn].get("description") and dv.get("description"):
                base["definitions"][dn] = dv
        for pn, pv in (doc.get("parameters") or {}).items():
            if pn not in base["parameters"]:
                base["parameters"][pn] = pv
        for rn, rv in (doc.get("responses") or {}).items():
            if rn not in base["responses"]:
                base["responses"][rn] = rv
    return base, dup


def organize(src_dir: str, out_dir: str, translations: dict[str, str] | None = None) -> tuple[int, int]:
    """merged 目录 → 最终产物目录。返回 (total_files, total_ops)。"""
    if translations is None:
        translations = load_translations()
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    product_dirs: collections.defaultdict[tuple[str, str], dict[str, Any]] = collections.defaultdict(dict)
    for ps in sorted(os.listdir(src_dir)):
        pdir = os.path.join(src_dir, ps)
        if not os.path.isdir(pdir):
            continue
        canonical = PRODUCT_CANONICAL.get(ps.lower(), ps)
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                doc = json.load(f)
            key = (canonical, fn)
            product_dirs[key][fn] = doc

    total_files = 0
    total_ops = 0
    merged_tags = []

    for (canonical, fn), docs in sorted(product_dirs.items()):
        p_out = os.path.join(out_dir, canonical)
        os.makedirs(p_out, exist_ok=True)

        if len(docs) == 1:
            doc = next(iter(docs.values()))
        else:
            doc, dup = merge_multi(docs)
            merged_tags.append((canonical, fn, len(docs), dup))

        total_files += 1
        total_ops += sum(len(pi) for pi in doc["paths"].values())

        base_name = fn[:-5]
        safe_fn = sanitize_tag(fn)
        if base_name in translations:
            en = translations[base_name]
            safe_fn = sanitize_tag(en) + ".json"
            title = doc.get("info", {}).get("title")
            if title:
                doc["info"]["title"] = f"{canonical} - {en}"
        out_path = os.path.join(p_out, safe_fn)
        if os.path.exists(out_path):
            stem, ext = os.path.splitext(safe_fn)
            i = 1
            while os.path.exists(os.path.join(p_out, f"{stem}_{i}{ext}")):
                i += 1
            safe_fn = f"{stem}_{i}{ext}"
            out_path = os.path.join(p_out, safe_fn)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

    index_data = build_index(out_dir)
    with open(os.path.join(out_dir, "_index.json"), "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    return total_files, total_ops


def build_index(out_dir: str) -> dict[str, Any]:
    index: dict[str, Any] = {"products": {}, "files": []}
    for ps in sorted(os.listdir(out_dir)):
        pdir = os.path.join(out_dir, ps)
        if not os.path.isdir(pdir):
            continue
        tag_files = [f for f in os.listdir(pdir) if f.endswith(".json") and not f.startswith(".")]
        ops_total = 0
        for fn in tag_files:
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                d = json.load(f)
            n = sum(len(pi) for pi in d.get("paths", {}).values())
            ops_total += n
            index["files"].append({"product": ps, "file": fn, "operations": n})
        index["products"][ps] = {"tags": len(tag_files), "operations": ops_total}
    return index


def main() -> None:
    from . import region_paths
    total_files, total_ops = organize(region_paths.merged_dir(), region_paths.openapi_out_dir())
    print(f"files: {total_files}, operations: {total_ops}")


if __name__ == "__main__":
    main()
