"""从 opencode 输出/导出中提取会话 trace（S6 纯函数）。"""

import json
import re
from typing import Any, cast

from .scorer import ToolCall


def extract_usage(export: dict[str, Any] | str) -> dict[str, int | float] | None:
    """从 export JSON 的 info 字段提取 token / cost 汇总。

    接受完整 dict 或原始 JSON 文本（完整解析失败时 fallback 正则）。
    """
    if isinstance(export, str):
        return _extract_usage_from_raw(export)
    info = export.get("info")
    if not isinstance(info, dict):
        return None
    tokens = info.get("tokens")
    if not isinstance(tokens, dict):
        return None
    return _build_usage(tokens, info.get("cost"))


def _extract_usage_from_raw(raw: str) -> dict[str, int | float] | None:
    """从原始 JSON 文本中正则提取 tokens 段（完整 JSON 解析失败时用）。"""
    # 提取 "tokens": {...}
    m = re.search(r'"tokens"\s*:\s*\{', raw)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    end = None
    for i in range(start, len(raw)):
        if raw[i] == '{':
            depth += 1
        elif raw[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        tokens = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return None
    if not isinstance(tokens, dict):
        return None
    cost_m = re.search(r'"cost"\s*:\s*([\d.]+)', raw)
    cost = float(cost_m.group(1)) if cost_m else None
    return _build_usage(tokens, cost)


def _build_usage(tokens: dict[str, Any], cost: Any) -> dict[str, int | float]:
    cache = tokens.get("cache") or {}
    return {
        "cost": cost if isinstance(cost, (int, float)) else None,
        "input": tokens.get("input", 0),
        "output": tokens.get("output", 0),
        "reasoning": tokens.get("reasoning", 0),
        "cache_read": cache.get("read", 0) if isinstance(cache, dict) else 0,
        "cache_write": cache.get("write", 0) if isinstance(cache, dict) else 0,
    }


def _parse_output(raw_output: Any) -> dict[str, Any] | None:
    """解析工具调用的 output 字段。已是 dict 则返回，是 JSON 字符串则解析。"""
    if isinstance(raw_output, dict):
        return raw_output
    if isinstance(raw_output, str) and raw_output.strip():
        try:
            return cast(dict[str, Any], json.loads(raw_output))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def extract_trace_from_raw(raw: str) -> list[ToolCall]:
    """从原始 JSON 文本正则提取工具调用序列（output 截断时用）。

    不解析完整 JSON 文档——仅定位 "type": "tool" 区块，独立提取
    tool 名、input（括号配对）、status；跳过 output 字段。
    """
    tools: list[ToolCall] = []
    pos = 0
    while True:
        m = re.search(r'"type"\s*:\s*"tool"', raw[pos:])
        if not m:
            break
        abs_pos = pos + m.start()
        window = raw[abs_pos:abs_pos + 5000]

        tn = re.search(r'"tool"\s*:\s*"([^"]*)"', window)
        if not tn:
            pos = abs_pos + m.end()
            continue

        ts = re.search(r'"status"\s*:\s*"([^"]*)"', window)

        input_obj = {}
        im = re.search(r'"input"\s*:\s*\{', window)
        if im:
            src = window[im.end() - 1:]
            depth = 0
            input_slice = None
            for i, ch in enumerate(src):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        input_slice = src[:i + 1]
                        break
            if input_slice:
                try:
                    input_obj = json.loads(input_slice)
                except json.JSONDecodeError:
                    pass

        output_obj = None
        om = re.search(r'"output"\s*:\s*"', window)
        if om:
            out_start = om.end()
            out_raw = []
            escaped = False
            for i in range(out_start, min(out_start + 5000, len(window))):
                ch = window[i]
                if escaped:
                    out_raw.append(ch)
                    escaped = False
                elif ch == '\\':
                    out_raw.append(ch)
                    escaped = True
                elif ch == '"':
                    break
                else:
                    out_raw.append(ch)
            output_str = ''.join(out_raw)
            if output_str:
                output_obj = _parse_output(output_str)

        tools.append(ToolCall(
            tool=tn.group(1),
            input=input_obj,
            output=output_obj,
            status=ts.group(1) if ts else "",
        ))
        pos = abs_pos + m.end()
    return tools


def extract_trace(export: dict[str, Any]) -> tuple[list[ToolCall], str]:
    """opencode export JSON → (工具调用序列, assistant 文本回答)。

    工具调用仅取 assistant 消息的 tool parts；回答文本拼接 assistant 的 text parts
    （用户消息的文本不参与 answer 断言，避免与 prompt 撞词误判）。
    """
    tools: list[ToolCall] = []
    texts: list[str] = []
    for m in export.get("messages") or []:
        if (m.get("info") or {}).get("role") != "assistant":
            continue
        for p in m.get("parts") or []:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "tool":
                st = p.get("state") or {}
                tools.append(ToolCall(
                    tool=p.get("tool") or "",
                    input=st.get("input") or {},
                    output=_parse_output(st.get("output")),
                    status=st.get("status") or "",
                ))
            elif p.get("type") == "text":
                texts.append(p.get("text") or "")
    return tools, "\n".join(texts)


def parse_run_output(ndjson: str) -> dict[str, Any]:
    """`opencode run --format json` 事件流 → session_id/answer/finish 摘要。"""
    session_id: str | None = None
    answer_parts: list[str] = []
    finish_reason: str | None = None
    is_error: bool | None = None
    for line in ndjson.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = e.get("sessionID")
        if isinstance(sid, str) and session_id is None:
            session_id = sid
        part = e.get("part") or {}
        if e.get("type") == "text" and part.get("text"):
            answer_parts.append(part["text"])
        elif e.get("type") == "step_finish":
            finish_reason = part.get("reason")
            is_error = bool(part.get("isError"))
    return {
        "session_id": session_id,
        "answer": "\n".join(answer_parts),
        "finish_reason": finish_reason,
        "is_error": is_error,
    }
