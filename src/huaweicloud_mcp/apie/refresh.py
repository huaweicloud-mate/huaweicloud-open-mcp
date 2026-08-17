"""api-refresh：API Explorer 元数据刷新流水线 CLI。

阶段：count → products → docs → details → retry → split → convert → merge → organize → validate。
"""

import argparse
import json
import os
import subprocess
import sys
import time

from ..paths import project_root
from . import region_paths

PROOT = str(project_root())

STAGES = ["count", "products", "docs", "details", "retry", "split",
          "convert", "merge", "organize", "validate"]

MODULE = {
    "docs": "fetch_apis",
    "details": "fetch_details",
    "retry": "retry_failed",
    "split": "split_by_tag",
    "convert": "convert_openapi2",
    "merge": "merge_by_tag",
    "organize": "organize",
}

CURL = {
    "count": ("https://console.huaweicloud.com/apiexplorer/new/v1/products/apis/count", "raw/apis_count.json"),
    "products": ("https://console.huaweicloud.com/apiexplorer/new/v5/products", "raw/huawei_products.json"),
}

_REGION = {"region": "cn-north-4"}


def current_region() -> str:
    return _REGION["region"]


def artifact_of(stage: str, region: str | None = None) -> str | None:
    r = region or current_region()
    if stage in ("count", "products", "docs"):
        return {"count": "raw/apis_count.json",
                "products": "raw/huawei_products.json",
                "docs": "raw/apis_docs.json"}[stage]
    if stage in ("details", "retry"):
        return region_paths.raw_detail_path(r)
    if stage == "split":
        return region_paths.by_tag_dir(r)
    if stage == "convert":
        return region_paths.openapi2_dir(r)
    if stage == "merge":
        return region_paths.merged_dir(r)
    if stage == "organize":
        return region_paths.openapi_out_dir(r)
    return None


def stage_index(stage: str) -> int:
    return STAGES.index(stage)


def run_cmd(args: list[str], dry_run: bool = False, env: dict[str, str] | None = None) -> int:
    print("+ " + " ".join(args), flush=True)
    if dry_run:
        return 0
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.call(args, cwd=PROOT, env=full_env)


def stage_count(dry_run: bool) -> int:
    url, out = CURL["count"]
    tmp = out + ".tmp"
    os.makedirs(os.path.join(PROOT, "raw"), exist_ok=True)
    rc = run_cmd(["curl", "-s", url, "-o", tmp], dry_run)
    if dry_run or rc != 0:
        return rc
    with open(os.path.join(PROOT, tmp), encoding="utf-8") as f:
        raw = json.load(f)
    groups = raw.get("groups", [])
    result = {
        "total_api_count": sum(g["api_count"] for g in groups),
        "total_products": len(groups),
        "groups": groups,
        "source": url,
    }
    with open(os.path.join(PROOT, out), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.remove(os.path.join(PROOT, tmp))
    return 0


def stage_products(dry_run: bool) -> int:
    url, out = CURL["products"]
    tmp = out + ".tmp"
    rc = run_cmd(["curl", "-s", url, "-o", tmp], dry_run)
    if dry_run or rc != 0:
        return rc
    with open(os.path.join(PROOT, tmp), encoding="utf-8") as f:
        raw = json.load(f)
    groups = raw.get("groups", [])
    result = {
        "total_groups": len(groups),
        "total_products": sum(len(g["products"]) for g in groups),
        "groups": groups,
        "source": url,
    }
    with open(os.path.join(PROOT, out), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.remove(os.path.join(PROOT, tmp))
    return 0


def stage_script(name: str, dry_run: bool, full: bool = False) -> int:
    env = None
    if name in ("details", "retry"):
        env = {"API_EXPLORER_REGION": current_region()}
        if dry_run:
            print(f"+ env API_EXPLORER_REGION={current_region()}", flush=True)
    args = [sys.executable, "-m", f"huaweicloud_mcp.apie.{MODULE[name]}"]
    return run_cmd(args, dry_run, env=env)


def stage_validate(dry_run: bool, full: bool = False) -> int:
    if full:
        code = run_cmd([sys.executable, "-m", "huaweicloud_mcp.apie.validate_openapi2"], dry_run)
        if dry_run or code != 0:
            return code
    from .validate_openapi2 import load_validator, validate_final_dir
    print("+ validate data/openapi/ (jsonschema)", flush=True)
    if dry_run:
        return 0
    schema_path = "/tmp/swagger2_schema.json"
    if not os.path.exists(schema_path):
        print(f"schema 缺失: {schema_path}，请先下载", file=sys.stderr)
        return 2
    validator = load_validator(schema_path)
    target = region_paths.openapi_out_dir()
    if not os.path.isdir(target):
        print(f"产物缺失: {target}", file=sys.stderr)
        return 2
    _, invalid = validate_final_dir(validator, target)
    return 1 if invalid else 0


def describe_file(path: str | None) -> str:
    if path is None:
        return "—"
    if os.path.isdir(path):
        n = sum(1 for _, _, fs in os.walk(path) for f in fs if f.endswith(".json") and not f.startswith("."))
        return f"dir({n} files)"
    if os.path.isfile(path):
        size = os.path.getsize(path)
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        return f"{size / 1024:.0f}KB  {mtime}"
    return "缺失"


def cmd_status(args: argparse.Namespace) -> None:
    region = getattr(args, "region", "cn-north-4") or "cn-north-4"
    print(f"region: {region}")
    print(f"{'stage':<10} {'artifact':<32} {'status'}")
    print("-" * 60)
    for s in STAGES:
        art = artifact_of(s, region)
        exists = os.path.exists(os.path.join(PROOT, art)) if art else True
        mark = "OK " if exists else "MISS"
        print(f"{s:<10} {art or '—':<32} {mark} {describe_file(art)}")


def cmd_single(args: argparse.Namespace, name: str) -> int:
    handlers = {
        "count": lambda: stage_count(args.dry_run),
        "products": lambda: stage_products(args.dry_run),
        "validate": lambda: stage_validate(args.dry_run, args.full),
    }
    fn = handlers.get(name)
    if fn is None:
        if name in MODULE:
            return stage_script(name, args.dry_run)
        print(f"未知阶段: {name}", file=sys.stderr)
        return 2
    return fn()


def detail_region_matches(region: str) -> bool:
    """检查 raw 详情产物的 region_id 是否与目标 region 一致（隐式识别）。"""
    path = region_paths.raw_detail_path(region)
    if not os.path.exists(os.path.join(PROOT, path)):
        return False
    try:
        with open(os.path.join(PROOT, path), encoding="utf-8") as f:
            d = json.load(f)
        return bool(d.get("region_id") == region)
    except Exception:
        return False


def cmd_refresh(args: argparse.Namespace) -> int:
    start = stage_index(args.start) if args.start else 0
    end = stage_index(args.end) if args.end else len(STAGES) - 1
    if start > end:
        print(f"无效范围: --from {args.start} 在 --to {args.end} 之后", file=sys.stderr)
        return 2
    selected = STAGES[start:end + 1]
    print(f"将执行 {len(selected)} 个阶段: {' → '.join(selected)}", flush=True)

    for s in selected:
        if not args.force and not args.dry_run:
            if s in ("details", "retry"):
                if detail_region_matches(current_region()):
                    print(f"跳过 {s}: 产物 region 与当前 region 一致（--force 强制刷新）", flush=True)
                    continue
            art = artifact_of(s)
            if art and os.path.exists(os.path.join(PROOT, art)):
                print(f"跳过 {s}: 产物 {art} 已存在（--force 强制刷新）", flush=True)
                continue
        print(f"\n===== {s} =====", flush=True)
        t0 = time.time()
        rc = cmd_single(args, s)
        if rc != 0:
            print(f"阶段 {s} 失败 (rc={rc})", file=sys.stderr)
            return rc
        print(f"阶段 {s} 完成，耗时 {time.time() - t0:.0f}s", flush=True)
    print("\n全部完成。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="api-refresh", description="华为云 API Explorer 接口文档刷新工具")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    common.add_argument("--force", action="store_true", help="忽略产物存在检查，强制刷新")
    common.add_argument("--full", action="store_true", help="validate 阶段同时校验 apis_detail_by_tag_openapi2/ 片段")
    common.add_argument("--region", default="cn-north-4", help="详情接口 region_id（默认 cn-north-4）")

    sub = p.add_subparsers(dest="command")

    sub.add_parser("status", help="查看各阶段产物状态", parents=[common])

    for s in STAGES:
        sp = sub.add_parser(s, help=f"单步执行 {s} 阶段", parents=[common])
        sp.set_defaults(single=s)

    rp = sub.add_parser("refresh", help="执行整条流水线（或 --from/--to 范围）", parents=[common])
    rp.add_argument("--from", dest="start", choices=STAGES, help="起始阶段")
    rp.add_argument("--to", dest="end", choices=STAGES, help="结束阶段")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
        return 0
    _REGION["region"] = getattr(args, "region", "cn-north-4")
    if args.command == "refresh":
        return cmd_refresh(args)
    if getattr(args, "single", None):
        return cmd_single(args, args.single)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
