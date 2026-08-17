"""OpenAPI 2.0 校验工具。"""

import collections
import json
import os

from jsonschema import Draft4Validator

DEFAULT_SCHEMA = "/tmp/swagger2_schema.json"


def load_validator(schema_path=DEFAULT_SCHEMA):
    with open(schema_path, encoding="utf-8") as f:
        return Draft4Validator(json.load(f))


def validate_doc(validator, doc):
    """校验单个 OpenAPI 文档，返回错误列表（每条为 (absolute_path, message)）。"""
    errors = []
    for e in validator.iter_errors(doc):
        path = list(e.absolute_path)
        label = "/".join(str(p) for p in path) if path else "ROOT"
        errors.append((label, e.message[:150]))
    return errors


def validate_dir(validator, src_dir):
    """校验目录下所有文件的 apis 字段，返回统计。"""
    res = collections.Counter()
    issues = collections.Counter()
    examples = {}
    by_product = collections.Counter()
    total = 0

    for ps_dir in sorted(os.listdir(src_dir)):
        pdir = os.path.join(src_dir, ps_dir)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                data = json.load(f)
            for api in data.get("apis", {}).values():
                total += 1
                errs = list(validator.iter_errors(api))
                if errs:
                    res["invalid"] += 1
                    by_product[ps_dir] += 1
                    seen = set()
                    for e in errs:
                        path = list(e.absolute_path)
                        pk = json.dumps(path)
                        if pk in seen:
                            continue
                        seen.add(pk)
                        label = "/".join(str(p) for p in path) if path else "ROOT"
                        issues[label] += 1
                        examples.setdefault(label, e.message[:150])

    return {
        "total": total,
        "valid": total - res["invalid"],
        "invalid": res["invalid"],
        "by_product": dict(by_product),
        "issues": {k: {"count": v, "example": examples[k]} for k, v in issues.most_common()},
    }


def validate_final_dir(validator, root):
    """校验最终产物目录（每文件一份完整 OpenAPI 文档）。返回 (checked, invalid)。"""
    checked = 0
    invalid = 0
    for ps in sorted(os.listdir(root)):
        pdir = os.path.join(root, ps)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                d = json.load(f)
            checked += 1
            errs = list(validator.iter_errors(d))
            if errs:
                invalid += 1
                print(f"  INVALID {os.path.join(ps, fn)}: {errs[0].message[:100]}")
    print(f"checked={checked} invalid={invalid}")
    return checked, invalid


def main():
    from . import region_paths
    validator = load_validator()
    src_dir = region_paths.openapi2_dir()
    stats = validate_dir(validator, src_dir)
    print("total:", stats["total"], "| valid:", stats["valid"], "| invalid:", stats["invalid"])
    with open("validation_issues.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print("saved validation_issues.json")


if __name__ == "__main__":
    main()
