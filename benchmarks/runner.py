"""LLM Agent 级工作流 benchmark runner（编排层，无单测，S6 只覆盖其纯函数依赖）。

每个 case × repeat 独立起一次 `opencode run`（全新会话、冷启 MCP server），
收集 wall-clock / 工具调用 trace / token / cost，评分后落盘并汇总。

用法示例：
  uv run python -m benchmarks.runner --backend stub --repeat 3
  uv run python -m benchmarks.runner --case ecs_list_servers --backend both
  uv run python -m benchmarks.runner --dry-run
"""

import argparse
import dataclasses
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .cases import BenchmarkCase, load_cases
from .openapi.stub_server import StubServer
from .report import CaseStats, RunResult, aggregate, dump_baseline, render_markdown
from .scorer import ToolCall, score
from .trace import extract_trace, extract_trace_from_raw, extract_usage, parse_run_output

_RAW_USAGE_KEY = "__raw_usage__"
_RAW_TOOLS_KEY = "__raw_tools__"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "maas/glm-5.2"
DEFAULT_POLICY = "configs/safety-policy.example.json"
DEFAULT_OUT = "benchmarks/results"


def build_benchdir_config(policy: str, mock_base: str | None) -> str:
    """生成 benchdir 的 opencode.json（隔离配置：mock + policy + 权限预批）。"""
    cmd = ["uv", "run", "huaweicloud-open-mcp", "--mock", "--policy", policy]
    if mock_base:
        cmd += ["--mock-base", mock_base]
    config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "huaweicloud-open-mcp": {
                "type": "local",
                "command": cmd,
                "cwd": str(PROJECT_ROOT),
                "enabled": True,
            },
        },
        "permission": {"huaweicloud-open-mcp_*": "allow"},
    }
    return json.dumps(config, ensure_ascii=False, indent=2)


def export_session(opencode_bin: str, session_id: str, retries: int = 3) -> dict | None:
    """opencode export 带重试：会话刚结束时导出可能读到未落盘数据而截断。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(1.5 * attempt)
        try:
            proc = subprocess.run(
                [opencode_bin, "export", session_id],
                capture_output=True, text=True, timeout=60,
            )
            if proc.stdout.strip():
                data = json.loads(proc.stdout)
                if isinstance(data, dict) and data.get("messages") is not None:
                    return data
        except json.JSONDecodeError:
            # JSON 截断/格式异常 → 重试或从 raw 文本提取
            if attempt < retries - 1:
                continue
            usage = extract_usage(proc.stdout)
            if usage:
                result: dict = {_RAW_USAGE_KEY: usage}
                tools = extract_trace_from_raw(proc.stdout)
                if tools:
                    result[_RAW_TOOLS_KEY] = tools
                return result
            return None
        except Exception as e:  # noqa: BLE001
            last_err = e
    if last_err is not None:
        raise last_err
    return None


def run_once(case: BenchmarkCase, backend: str, repeat: int, model: str,
             opencode_bin: str, policy: str, mock_base: str | None,
             timeout: int) -> RunResult:
    benchdir = tempfile.mkdtemp(prefix="bench-")
    Path(benchdir, "opencode.json").write_text(build_benchdir_config(policy, mock_base),
                                               encoding="utf-8")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [opencode_bin, "run", case.prompt, "--model", model,
             "--format", "json", "--dir", benchdir],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(case.id, backend, repeat, None, model, time.monotonic() - t0,
                         {}, None, None, error="timeout")
    elapsed = time.monotonic() - t0
    parsed = parse_run_output(proc.stdout)
    session_id = parsed["session_id"]
    trace: list[ToolCall] = []
    answer: str = parsed["answer"]
    export_raw: dict | None = None
    export_error: str | None = None
    raw_usage: dict[str, int | float] | None = None
    if session_id:
        try:
            export_raw = export_session(opencode_bin, session_id)
        except Exception as e:  # noqa: BLE001
            export_error = f"export 失败: {e}"
        if isinstance(export_raw, dict) and _RAW_USAGE_KEY in export_raw:
            raw_usage = export_raw[_RAW_USAGE_KEY]
            raw_tools = export_raw.get(_RAW_TOOLS_KEY, [])
            if raw_tools:
                trace = raw_tools
            export_raw = None
            export_error = "export JSON 解析异常，token/trace 从 raw 文本提取"
        elif export_raw is not None:
            trace, answer = extract_trace(export_raw)
        elif export_error is None:
            export_error = "export 失败: JSON 解析异常或结果为空"
    if not session_id:
        error = f"opencode run 失败 exit={proc.returncode}"
    elif export_error:
        error = export_error
    elif parsed["is_error"]:
        error = f"opencode 会话出错 finish={parsed['finish_reason']}"
    else:
        error = None
    sc = score(trace, answer, case) if session_id else None
    tokens: dict[str, int | float | None] = {"cost": None, "input": None, "output": None,
                                             "reasoning": None, "cache_read": None,
                                             "cache_write": None}
    if raw_usage:
        tokens.update(raw_usage)
    elif export_raw is not None:
        usage = extract_usage(export_raw)
        if usage:
            tokens.update(usage)
    return RunResult(
        case_id=case.id, backend=backend, repeat=repeat, session_id=session_id,
        model=model, elapsed_s=elapsed,
        tokens=tokens,
        cost=tokens.get("cost") if isinstance(tokens.get("cost"), (int, float)) else None,
        score=sc, error=error,
    )


def run_backend(cases: list[BenchmarkCase], backend: str, args: argparse.Namespace,
                stub: StubServer | None) -> list[RunResult]:
    mock_base = stub.base_url if stub else None
    results: list[RunResult] = []
    total = sum(c.repeat for c in cases)
    done = 0
    for case in cases:
        for i in range(case.repeat):
            done += 1
            print(f"[{backend}] {done}/{total} {case.id} #{i} ...", flush=True)
            r = run_once(case, backend, i, args.model, args.opencode, args.policy,
                         mock_base, case.timeout)
            suffix = "PASS" if (r.score and r.score.passed) else ("ERR" if r.error else "FAIL")
            extra = ""
            if r.score and r.score.checks:
                execs = r.score.checks.get("execute_calls", [])
                if execs:
                    extra = " params=" + json.dumps(execs, ensure_ascii=False)[:300]
            print(f"  -> {suffix} {r.elapsed_s:.1f}s "
                  f"tok_in={r.tokens.get('input')} err={r.error}{extra}", flush=True)
            results.append(r)
    return results


def save_runs(results: list[RunResult], run_dir: Path) -> None:
    runs_dir = run_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        fp = runs_dir / f"{r.case_id}__{r.backend}__{r.repeat}.json"
        fp.write_text(json.dumps(dataclasses.asdict(r), ensure_ascii=False, indent=2),
                      encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.runner",
                                     description="LLM Agent 级渐进式工作流 benchmark")
    parser.add_argument("--cases", default=str(PROJECT_ROOT / "benchmarks" / "openapi" / "cases"))
    parser.add_argument("--case", default=None, help="只跑指定 case id")
    parser.add_argument("--repeat", type=int, default=None, help="覆盖 case 默认 repeat")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--backend", choices=["stub", "real", "both"], default="stub")
    parser.add_argument("--timeout", type=int, default=None, help="覆盖 case 默认 timeout")
    parser.add_argument("--opencode", default="opencode")
    parser.add_argument("--policy", default=str(PROJECT_ROOT / DEFAULT_POLICY))
    parser.add_argument("--out", default=str(PROJECT_ROOT / DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true", help="只校验用例，不跑 LLM")
    parser.add_argument("--baseline-save", action="store_true")
    parser.add_argument("--baseline-compare", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.cases))
    if args.case:
        cases = [c for c in cases if c.id == args.case]
        if not cases:
            print(f"用例 {args.case} 不存在", file=sys.stderr)
            return 2
    if args.repeat:
        cases = [dataclasses.replace(c, repeat=args.repeat) for c in cases]
    if args.timeout:
        cases = [dataclasses.replace(c, timeout=args.timeout) for c in cases]

    if args.dry_run:
        for c in cases:
            print(f"case={c.id} repeat={c.repeat} prompt={c.prompt!r}")
        print(f"共 {len(cases)} 个用例")
        return 0

    backends = ["stub", "real"] if args.backend == "both" else [args.backend]
    run_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    for backend in backends:
        stub = StubServer() if backend == "stub" else None
        if stub:
            stub.start()
        try:
            results = run_backend(cases, backend, args, stub)
        finally:
            if stub:
                stub.stop()
        stats: list[CaseStats] = aggregate(results)
        save_runs(results, run_dir)
        baseline = None
        if args.baseline_compare:
            bpath = Path(args.out) / f"baseline-{backend}.json"
            if bpath.exists():
                baseline = json.loads(bpath.read_text(encoding="utf-8"))
            else:
                print(f"基线缺失: {bpath}", file=sys.stderr)
        md = render_markdown(stats, baseline=baseline, backend=backend, model=args.model)
        (run_dir / f"summary-{backend}.md").write_text(md, encoding="utf-8")
        summary = {
            "meta": {"backend": backend, "model": args.model,
                     "created_at": datetime.now().isoformat()},
            "runs": [dataclasses.asdict(r) for r in results],
            "stats": {s.case_id: s.to_dict() for s in stats},
        }
        (run_dir / f"summary-{backend}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(md)
        if args.baseline_save:
            bpath = Path(args.out) / f"baseline-{backend}.json"
            bpath.write_text(json.dumps(dump_baseline(stats, backend, args.model),
                                        ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"基线已保存: {bpath}")
        if args.fail_on_regression and baseline is not None:
            base_cases = baseline.get("cases") or {}
            for s in stats:
                b = base_cases.get(s.case_id)
                if b and b["pass_rate"] > 0 and s.pass_rate < b["pass_rate"]:
                    print(f"回归: {s.case_id} pass率 {b['pass_rate']:.0%} → {s.pass_rate:.0%}",
                          file=sys.stderr)
                    exit_code = 3
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
