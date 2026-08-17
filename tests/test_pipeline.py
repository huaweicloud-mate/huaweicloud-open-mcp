"""流水线集成测试：用迷你数据驱动 split → convert → merge → organize。"""

import json
import os

from openmcp.apie import convert_openapi2, merge_by_tag, organize, split_by_tag


def run_split(src, out_dir):
    return split_by_tag.split_by_tag(src, out_dir)


def run_convert(src_dir, out_dir):
    total = 0
    for ps in sorted(os.listdir(src_dir)):
        pdir = os.path.join(src_dir, ps)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                data = json.load(f)
            converted = {}
            for key, api in data.get("apis", {}).items():
                converted[key] = convert_openapi2.convert_api(api)
            total += len(converted)
            outdir = os.path.join(out_dir, ps)
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, fn), "w", encoding="utf-8") as f:
                json.dump({"product_short": data.get("product_short"), "tag": data.get("tag"),
                           "api_count": len(converted), "apis": converted}, f, ensure_ascii=False, indent=2)
    return total


def run_merge(src_dir, out_dir):
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    total_ops = 0
    for ps in sorted(os.listdir(src_dir)):
        pdir = os.path.join(src_dir, ps)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                data = json.load(f)
            doc, _ = merge_by_tag.merge_doc(data.get("apis", {}))
            outdir = os.path.join(out_dir, ps)
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, fn), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            total_ops += sum(len(pi) for pi in doc["paths"].values())
    return total_ops


def run_organize(src_dir, out_dir, translations=None):
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    total_files = 0
    total_ops = 0
    for ps in sorted(os.listdir(src_dir)):
        pdir = os.path.join(src_dir, ps)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                doc = json.load(f)
            base_name = fn[:-5]
            safe_fn = organize.sanitize_tag(fn)
            if translations and base_name in translations:
                safe_fn = organize.sanitize_tag(translations[base_name]) + ".json"
            outdir = os.path.join(out_dir, ps)
            os.makedirs(outdir, exist_ok=True)
            out_path = os.path.join(outdir, safe_fn)
            if os.path.exists(out_path):
                stem, ext = os.path.splitext(safe_fn)
                i = 1
                while os.path.exists(os.path.join(outdir, f"{stem}_{i}{ext}")):
                    i += 1
                out_path = os.path.join(outdir, f"{stem}_{i}{ext}")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            total_files += 1
            total_ops += sum(len(pi) for pi in doc["paths"].values())
    return total_files, total_ops


def test_pipeline_end_to_end(workdir, swagger_schema):
    wd = workdir
    raw_detail = os.path.join(wd, "raw", "apis_detail.json")

    # split
    by_tag = os.path.join(wd, "by_tag")
    total_api, summary = run_split(raw_detail, by_tag)
    assert total_api == 6
    assert set(summary.keys()) == {"ECS", "RabbitMQ"}
    assert summary["ECS"]["生命周期管理"] == 2
    assert summary["ECS"]["_untagged"] == 1
    assert os.path.exists(os.path.join(by_tag, "ECS", "_untagged.json"))

    # convert
    openapi2 = os.path.join(wd, "openapi2")
    converted = run_convert(by_tag, openapi2)
    assert converted == 6

    # 转换后 3.0 特征消失
    with open(os.path.join(openapi2, "RabbitMQ", "标签管理.json"), encoding="utf-8") as f:
        rb = json.load(f)
    for api in rb["apis"].values():
        assert "components" not in api
        assert "requestBody" not in api
        assert api["swagger"] == "2.0"

    # 转换后片段全过 schema 校验
    from jsonschema import Draft4Validator
    validator = Draft4Validator(swagger_schema)
    bad = 0
    for ps in sorted(os.listdir(openapi2)):
        pdir = os.path.join(openapi2, ps)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                data = json.load(f)
            for api in data.get("apis", {}).values():
                if list(validator.iter_errors(api)):
                    bad += 1
    assert bad == 0

    # merge
    merged = os.path.join(wd, "merged")
    ops = run_merge(openapi2, merged)
    assert ops == 6

    # organize（英文 tag 映射）
    translations = {"标签管理": "TagManagement", "生命周期管理": "LifecycleManagement", "_untagged": "Untagged"}
    final = os.path.join(wd, "openapi")
    files, final_ops = run_organize(merged, final, translations)
    # ECS: 生命周期管理/标签管理/_untagged = 3; RabbitMQ: 标签管理 = 1; 共 4
    assert files == 4
    assert final_ops == 6

    # 最终产物均为英文文件名
    assert os.path.exists(os.path.join(final, "ECS", "LifecycleManagement.json"))
    assert os.path.exists(os.path.join(final, "ECS", "TagManagement.json"))
    assert os.path.exists(os.path.join(final, "ECS", "Untagged.json"))
    assert os.path.exists(os.path.join(final, "RabbitMQ", "TagManagement.json"))
    assert not any(f for f in os.listdir(os.path.join(final, "ECS")) if f and ord(f[0]) > 127)
