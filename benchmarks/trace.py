"""从 opencode 输出/导出中提取会话 trace（S6 纯函数）。"""

import json
from typing import Any

from .scorer import ToolCall


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
