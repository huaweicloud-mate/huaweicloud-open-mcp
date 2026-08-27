"""cases YAML → Harbor 任务目录（S6：render_task 纯核 + export_dataset 薄壳）。

render_task 零副作用：返回 {任务目录相对路径: 文件内容}，即测试面；
export_dataset 只做 mkdir/write。任务目录自包含（hwc/ 内嵌项目源码），
可整体拷走用 `harbor run -p <task>` 执行。
"""

import json
from pathlib import Path
from typing import Any

import yaml

from benchmarks.cases import BenchmarkCase, load_cases
from benchmarks.harbor import conventions as conv

TEMPLATES_DIR = Path(__file__).resolve().parent / "task_templates"

# 内嵌进任务 environment/hwc/ 的项目文件（自包含要求）
_HWC_FILES = [
    "pyproject.toml",
    "uv.lock",
    "configs/safety-policy.example.json",
]
_HWC_DIRS = ["src", "configs"]


def render_template(text: str, tokens: dict[str, str]) -> str:
    """显式 token 替换（__NAME__），避免 format 与 shell/JSON 花括号冲突。"""
    out = text
    for key, value in tokens.items():
        out = out.replace(f"__{key}__", value)
    return out


def derive_capability(case: BenchmarkCase) -> str:
    """labels.capability 缺省时按 expect 形状推导。"""
    if case.labels and case.labels.get("capability"):
        return case.labels["capability"]
    if case.expect.constraints.no_execute or case.expect.forbidden:
        return "safety"
    if case.expect.params is not None:
        return "execute-correctness"
    return "retrieval"


def derive_difficulty(case: BenchmarkCase) -> str:
    """labels.difficulty 缺省时按 max_calls 粗分。"""
    if case.labels and case.labels.get("difficulty"):
        return case.labels["difficulty"]
    if case.expect.constraints.max_calls >= 10:
        return "medium"
    return "easy"


def _hwc_tree(project_root: Path) -> dict[str, str]:
    """项目源码树 → {相对 hwc/ 路径: 文本内容}。"""
    out: dict[str, str] = {}
    for rel in _HWC_FILES:
        out[rel] = (project_root / rel).read_text(encoding="utf-8")
    for d in _HWC_DIRS:
        base = project_root / d
        if not base.exists():
            continue
        for fp in sorted(base.rglob("*")):
            if fp.is_file() and "__pycache__" not in fp.parts:
                out[str(fp.relative_to(project_root))] = fp.read_text(encoding="utf-8")
    return out


def render_task(case: BenchmarkCase, case_yaml: str, *, templates_dir: Path = TEMPLATES_DIR,
                project_root: Path | None = None) -> dict[str, str]:
    """渲染单个 Harbor 任务 → {相对任务目录路径: 文件内容}（零副作用）。"""
    root = project_root if project_root is not None else _default_project_root()
    source_rel = case.source or f"<memory:{case.id}>"
    tokens = {
        "ORG": conv.TASK_ORG,
        "CASE_ID": case.id,
        "PROMPT": case.prompt,
        "CAPABILITY": derive_capability(case),
        "DIFFICULTY": derive_difficulty(case),
        "SOURCE_CASE": source_rel,
        "STUB_PORT": str(conv.STUB_PORT),
        "AUDIT_FILE": conv.AUDIT_FILE,
        "STUB_LEDGER": conv.STUB_LEDGER,
        "ANSWER_FILE": conv.ANSWER_FILE,
        "MCP_COMMAND": conv.MCP_COMMAND,
    }

    def tmpl(name: str) -> str:
        return render_template((templates_dir / name).read_text(encoding="utf-8"), tokens)

    # case_yaml 为空（无源文件，如测试构造的 case）→ 重建最小 case YAML 结构
    case_text = (case_yaml if case_yaml.strip()
                 else yaml.safe_dump(_case_to_dict(case), allow_unicode=True,
                                     sort_keys=False))
    policy_text = (case.policy if case.policy is not None
                   else (root / "configs/safety-policy.example.json")
                   .read_text(encoding="utf-8"))

    files: dict[str, str] = {
        "instruction.md": tmpl("instruction.md.tmpl"),
        "task.toml": tmpl("task.toml.tmpl"),
        "environment/Dockerfile": tmpl("Dockerfile.tmpl"),
        "environment/start_services.sh": tmpl("start_services.sh.tmpl"),
        "environment/start_mcp.sh": tmpl("start_mcp.sh.tmpl"),
        "environment/stub_server.py": tmpl("stub_server.py"),
        "environment/fixtures.json": json.dumps(case.fixture or {}, ensure_ascii=False,
                                                indent=2),
        "environment/policy.json": policy_text,
        "tests/test.sh": tmpl("test.sh.tmpl"),
        "tests/test_outputs.py": tmpl("test_outputs.py.tmpl"),
        "tests/case.yaml": case_text,
        "solution/solve.sh": tmpl("solve.sh.tmpl"),
        "solution/oracle.py": tmpl("oracle.py.tmpl"),
    }
    for rel, content in _hwc_tree(root).items():
        files[f"environment/hwc/{rel}"] = content
    return files


def export_dataset(cases_dir: Path, out_dir: Path, *, case_ids: list[str] | None = None,
                   templates_dir: Path = TEMPLATES_DIR,
                   project_root: Path | None = None) -> list[Path]:
    """薄壳：加载 cases → 渲染 → 写盘。返回生成的任务目录路径（按 case id 排序）。"""
    root = project_root if project_root is not None else _default_project_root()
    cases = load_cases(cases_dir)
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c.id in wanted]
    out: list[Path] = []
    for case in sorted(cases, key=lambda c: c.id):
        case_yaml = (Path(case.source).read_text(encoding="utf-8")
                     if case.source and Path(case.source).exists() else "")
        files = render_task(case, case_yaml, templates_dir=templates_dir,
                            project_root=root)
        task_dir = out_dir / case.id
        for rel, content in files.items():
            fp = task_dir / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
        out.append(task_dir)
    return out


def _case_to_dict(case: BenchmarkCase) -> dict[str, Any]:
    """无源文件时（测试构造的 case）重建最小 case YAML 结构。"""
    data: dict[str, Any] = {"id": case.id, "prompt": case.prompt,
                            "expect": {"execute": [
                                {"product": e.product, "api": e.api}
                                for e in case.expect.executes]}}
    if case.expect.params is not None:
        data["expect"]["params"] = case.expect.params
    if case.expect.answer is not None:
        data["expect"]["answer"] = case.expect.answer
    return data


def _default_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="benchmarks.harbor.exporter",
        description="cases YAML → Harbor 任务数据集（datasets/mcp-regression）")
    parser.add_argument("cases_dir", help="case YAML 目录（benchmarks/openapi/cases）")
    parser.add_argument("out_dir", help="输出数据集目录（datasets/mcp-regression）")
    parser.add_argument("--case-id", action="append", default=None,
                        help="只导出指定 case（可重复；缺省全量）")
    args = parser.parse_args(argv)
    paths = export_dataset(Path(args.cases_dir), Path(args.out_dir),
                           case_ids=args.case_id)
    for p in paths:
        print(p)
    print(f"共导出 {len(paths)} 个任务", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
