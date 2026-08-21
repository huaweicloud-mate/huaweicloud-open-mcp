"""benchmark 用例定义、加载与校验（S6 纯函数，不碰磁盘以外系统边界）。

用例 YAML 约定：每文件一个 case（顶层 mapping）。expect 至少含
execute / forbidden / answer 之一；constraints 可选。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_REPEAT = 3
DEFAULT_TIMEOUT = 600


@dataclass(frozen=True)
class ExecuteExpect:
    product: str
    api: str


@dataclass(frozen=True)
class Constraints:
    no_execute: bool = False
    tag_narrowing: bool = False
    max_calls: int = 0

    @classmethod
    def empty(cls) -> "Constraints":
        return cls()


@dataclass(frozen=True)
class Expect:
    executes: tuple[ExecuteExpect, ...] = field(default_factory=tuple)
    params: dict[str, Any] | None = None
    answer: str | None = None
    forbidden: tuple[ExecuteExpect, ...] = field(default_factory=tuple)
    constraints: Constraints = field(default_factory=Constraints.empty)


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    prompt: str
    expect: Expect
    repeat: int = DEFAULT_REPEAT
    timeout: int = DEFAULT_TIMEOUT
    source: str = ""


def _expect_executes(data: Any, source: str) -> tuple[ExecuteExpect, ...]:
    """execute 可为单个 mapping 或 mapping 列表（多个可接受接口）。"""
    if data is None:
        return ()
    items = data if isinstance(data, list) else [data]
    return tuple(_expect_execute(item, source) for item in items)


def _expect_execute(data: Any, source: str) -> ExecuteExpect:
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expect.execute 必须是 mapping")
    product = data.get("product")
    api = data.get("api")
    if not isinstance(product, str) or not product.strip():
        raise ValueError(f"{source}: expect.execute.product 必须是非空字符串")
    if not isinstance(api, str) or not api.strip():
        raise ValueError(f"{source}: expect.execute.api 必须是非空字符串")
    return ExecuteExpect(product=product, api=api)


def _expect_forbidden(data: Any, source: str) -> tuple[ExecuteExpect, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ValueError(f"{source}: expect.forbidden 必须是列表")
    out = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("product"), str) \
                or not isinstance(item.get("api"), str):
            raise ValueError(f"{source}: expect.forbidden 每项须含 product/api 字符串")
        out.append(ExecuteExpect(product=item["product"], api=item["api"]))
    return tuple(out)


def _parse_constraints(data: Any, source: str) -> Constraints:
    if data is None:
        return Constraints.empty()
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expect.constraints 必须是 mapping")
    no_execute = data.get("no_execute", False)
    tag_narrowing = data.get("tag_narrowing", False)
    max_calls = data.get("max_calls", 0)
    if not isinstance(no_execute, bool):
        raise ValueError(f"{source}: constraints.no_execute 必须是 bool")
    if not isinstance(tag_narrowing, bool):
        raise ValueError(f"{source}: constraints.tag_narrowing 必须是 bool")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool):
        raise ValueError(f"{source}: constraints.max_calls 必须是整数")
    return Constraints(no_execute=no_execute, tag_narrowing=tag_narrowing, max_calls=max_calls)


def parse_case(data: Any, source: str = "") -> BenchmarkCase:
    if not isinstance(data, dict):
        raise ValueError(f"{source}: 用例必须是 mapping")
    case_id = data.get("id")
    prompt = data.get("prompt")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"{source}: id 必须是非空字符串")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{source}: prompt 必须是非空字符串")

    expect_raw = data.get("expect")
    if not isinstance(expect_raw, dict):
        raise ValueError(f"{source}: expect 必须是 mapping")
    execute_raw = expect_raw.get("execute")
    params = expect_raw.get("params")
    answer = expect_raw.get("answer")
    if params is not None and not isinstance(params, dict):
        raise ValueError(f"{source}: expect.params 必须是 mapping")
    if answer is not None and not isinstance(answer, str):
        raise ValueError(f"{source}: expect.answer 必须是字符串")
    executes = _expect_executes(execute_raw, source)
    forbidden = _expect_forbidden(expect_raw.get("forbidden"), source)
    constraints = _parse_constraints(expect_raw.get("constraints"), source)
    if not executes and not forbidden and answer is None:
        raise ValueError(f"{source}: expect 至少需含 execute/forbidden/answer 之一")

    repeat = data.get("repeat", DEFAULT_REPEAT)
    timeout = data.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
        raise ValueError(f"{source}: repeat 必须是 >=1 的整数")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise ValueError(f"{source}: timeout 必须是 >=1 的整数")

    return BenchmarkCase(
        id=case_id,
        prompt=prompt,
        expect=Expect(executes=executes, params=params, answer=answer,
                      forbidden=forbidden, constraints=constraints),
        repeat=repeat,
        timeout=timeout,
        source=source,
    )


def load_cases(path: Path) -> list[BenchmarkCase]:
    """加载 path 下的用例；递归查找子目录中 *.yaml/*.yml，按文件名排序。"""
    if not path.exists():
        raise ValueError(f"用例路径不存在: {path}")
    if path.is_file():
        files = [path]
    else:
        files = sorted(set(list(path.rglob("*.yaml")) + list(path.rglob("*.yml"))))
    if not files:
        raise ValueError(f"用例路径下没有 .yaml/.yml 文件: {path}")
    cases: list[BenchmarkCase] = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        cases.append(parse_case(data, str(fp)))
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise ValueError(f"用例 id 重复: {c.id}")
        seen.add(c.id)
    return cases
