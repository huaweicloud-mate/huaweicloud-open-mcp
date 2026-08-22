"""从 opencode 输出/导出中提取会话 trace（S6 纯函数）。"""

import json
import re
from typing import Any

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
