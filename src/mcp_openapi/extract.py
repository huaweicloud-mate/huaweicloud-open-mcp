"""body JSONPath 投影抽取（execute_api `_jsonpath` 控制键）。

深模块：interface 恰两函数——`parse_extract`（service 层 dispatch 前预检，
fail-closed，不烧 once 授权）与 `apply_extract`（normalize_response 咽喉点
单委托，作用于截断前 raw body）。jsonpath-ng adapter 细节（方言、异常映射、
多命中语义）全部藏在本实现内：调用方不接触 jsonpath_ng 类型，ExtractSpec
为不透明值对象，仅经 execute 线程到咽喉点。

值形态：str 单路径（结果键=路径原文）| dict[str, str] 别名映射。
命中语义：0 命中=miss；1 命中=值本身；多命中=list（JSONPath 标准语义）。
载体降级：None/str/bytes 等非 JSON 载体 no-op + note（数据属性≠输入错误）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from jsonpath_ng.ext import parse as _jp_parse

__all__ = ["ExtractOutcome", "ExtractSpec", "apply_extract", "parse_extract"]


@dataclass(frozen=True)
class _Entry:
    """单条抽取路径：结果键 + 编译后表达式（不透明）+ 路径原文（misses/错误描述用）。"""

    key: str
    expr: Any
    source: str


class ExtractSpec:
    """已编译抽取规格（parse_extract 产物）。mapping=True 时按别名投影。"""

    __slots__ = ("entries", "mapping")

    def __init__(self, entries: list[_Entry], mapping: bool):
        self.entries = entries
        self.mapping = mapping


class ExtractOutcome(NamedTuple):
    """apply_extract 产物：投影值 + 未命中描述 + 降级说明。

    full_hit=True 表示全部请求路径命中（投影替换 body 的依据）；
    note 仅载体不适用时非空（JSON 载体的替换说明由咽喉点结合 spill 决策构建）。
    """

    extracted: Any
    misses: list[str]
    note: str | None
    full_hit: bool


def _compile(path: Any, context: str) -> _Entry | str:
    """编译单条路径 → _Entry | 错误描述。context 为错误定位前缀（含两侧空格）。"""
    if not isinstance(path, str) or not path.strip():
        return f"_jsonpath{context}路径须为非空字符串"
    try:
        expr = _jp_parse(path.strip())
    except Exception as exc:  # jsonpath_ng 词法/解析异常族不稳定，统一拦截
        return (f"_jsonpath{context}表达式非法: {path}（{exc}）；"
                "支持完整 JSONPath（$.a.b[0]、[*] 通配、$..递归、"
                "[?(@.x=='y')] 过滤、[\"key\"] 引号键）")
    return _Entry(key="", expr=expr, source=path.strip())


def parse_extract(spec: Any) -> ExtractSpec | str:
    """解析 `_jsonpath` 值 → ExtractSpec | 可操作错误描述（str）。

    str：单路径；dict[str, str]：别名映射（别名即下游消费契约）；
    其它形态（含空串/空映射）静态拒绝——语法错误 dispatch 前 fail-closed。
    """
    if isinstance(spec, str):
        compiled = _compile(spec, " ")
        if isinstance(compiled, str):
            return compiled
        entry = compiled
        single = _Entry(key=entry.source, expr=entry.expr, source=entry.source)
        return ExtractSpec([single], mapping=False)
    if isinstance(spec, dict):
        if not spec:
            return "_jsonpath 映射不能为空"
        entries: list[_Entry] = []
        for alias, path in spec.items():
            if not isinstance(alias, str) or not alias.strip():
                return "_jsonpath 映射的别名须为非空字符串"
            compiled = _compile(path, f" 别名 {alias} ")
            if isinstance(compiled, str):
                return compiled
            entries.append(_Entry(key=alias, expr=compiled.expr,
                                  source=compiled.source))
        return ExtractSpec(entries, mapping=True)
    return f"_jsonpath 须为 str 或 dict[str, str]（{{别名: 路径}} 映射），实际为 {type(spec).__name__}"


def _project(body: Any, entry: _Entry) -> tuple[Any, bool]:
    """单条路径求值 → (值|None, 命中)。0 命中 miss；1 命中值本身；多命中 list。"""
    values = [match.value for match in entry.expr.find(body)]
    if not values:
        return None, False
    return (values[0] if len(values) == 1 else values), True


def apply_extract(body: Any, spec: ExtractSpec) -> ExtractOutcome:
    """对截断前 raw body 投影（纯函数，不改写 body）。

    JSON 载体（dict/list）外一律 no-op + note；映射形未命中键恒保留
    （键集完整，null 标注），全命中 full_hit=True。
    """
    if body is None:
        return ExtractOutcome(None, [], "响应无 body，不适用 _jsonpath 投影", False)
    if isinstance(body, bytes):
        return ExtractOutcome(None, [], "二进制响应体，不适用 _jsonpath 投影（完整字节已落盘）", False)
    if isinstance(body, str):
        return ExtractOutcome(None, [], "响应体为文本（非 JSON），不适用 _jsonpath 投影", False)
    if not isinstance(body, (dict, list)):
        return ExtractOutcome(
            None, [], f"响应体类型 {type(body).__name__} 不适用 _jsonpath 投影", False)

    if spec.mapping:
        extracted: dict[str, Any] = {}
        misses: list[str] = []
        for entry in spec.entries:
            value, hit = _project(body, entry)
            extracted[entry.key] = value
            if not hit:
                misses.append(f"{entry.key}: 无命中（{entry.source}）")
        return ExtractOutcome(extracted, misses, None, not misses)

    entry = spec.entries[0]
    value, hit = _project(body, entry)
    if not hit:
        return ExtractOutcome(None, [f"{entry.source}: 无命中"], None, False)
    return ExtractOutcome(value, [], None, True)
