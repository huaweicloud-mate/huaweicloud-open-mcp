"""S6：benchmark trace 提取（export JSON/NDJSON）单测。"""

import json as _json

from benchmarks.trace import (
    extract_trace,
    extract_trace_from_raw,
    extract_usage,
    parse_export,
    parse_run_output,
)

EXPORT = {
    "info": {
        "id": "ses_abc",
        "title": "t",
        "cost": 0.0123,
        "tokens": {
            "input": 100,
            "output": 20,
            "reasoning": 5,
            "cache": {"read": 50, "write": 0},
        },
    },
    "messages": [
        {
            "info": {"role": "user", "model": {}},
            "parts": [{"type": "text", "text": "查询云服务器", "id": "p1", "sessionID": "ses_abc"}],
        },
        {
            "info": {"role": "assistant", "model": {}},
            "parts": [
                {"type": "tool", "tool": "huaweicloud-open-mcp_list_products", "callID": "c1",
                 "id": "p2", "sessionID": "ses_abc", "messageID": "m1",
                 "state": {"status": "completed", "input": {"keyword": "云"},
                           "output": '{"ok": true}', "metadata": {}, "title": "", "time": {}}},
                {"type": "text", "text": "找到了产品", "id": "p3", "sessionID": "ses_abc"},
            ],
        },
        {
            "info": {"role": "assistant", "model": {}},
            "parts": [
                {"type": "tool", "tool": "huaweicloud-open-mcp_execute_api", "callID": "c2",
                 "id": "p4", "sessionID": "ses_abc", "messageID": "m2",
                 "state": {"status": "completed",
                           "input": {"product": "ECS", "api": "ListServersDetails",
                                     "params": {"limit": 1}},
                           "output": '{"ok": true}', "metadata": {}, "title": "", "time": {}}},
                {"type": "text", "text": "共 1 台 bench-server", "id": "p5", "sessionID": "ses_abc"},
            ],
        },
    ],
}


def test_extract_trace_tools_and_assistant_text():
    tools = extract_trace(EXPORT)
    assert [(t.tool, t.status) for t in tools] == [
        ("huaweicloud-open-mcp_list_products", "completed"),
        ("huaweicloud-open-mcp_execute_api", "completed"),
    ]
    assert tools[1].input == {"product": "ECS", "api": "ListServersDetails", "params": {"limit": 1}}


def test_extract_trace_tolerates_missing_state():
    export = {"info": {}, "messages": [
        {"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "tool": "huaweicloud-open-mcp_get_api"},
            {"type": "text", "text": "x"},
        ]},
    ]}
    tools = extract_trace(export)
    assert tools[0].input == {}
    assert tools[0].status == ""


def test_extract_usage():
    u = extract_usage(EXPORT)
    assert u == {"cost": 0.0123, "input": 100, "output": 20,
                 "reasoning": 5, "cache_read": 50, "cache_write": 0}


def test_extract_usage_no_info():
    assert extract_usage({"messages": []}) is None


def test_extract_usage_no_tokens():
    assert extract_usage({"info": {"id": "x"}}) is None


def test_extract_usage_from_raw_truncated_json():
    """完整 JSON 解析失败时，从 raw 文本正则提取 tokens。"""
    raw = ('{"info":{"id":"s","cost":0.0234,'
           '"tokens":{"input":200,"output":50,"reasoning":10,'
           '"cache":{"read":100,"write":5}}},'
           '"messages":{"info":{"role":"assistant"},'
           '"parts":[{"type":"tool","state":{"output":"{\\"broken"}]}}')
    u = extract_usage(raw)
    assert u == {"cost": 0.0234, "input": 200, "output": 50,
                 "reasoning": 10, "cache_read": 100, "cache_write": 5}


def test_extract_usage_from_raw_no_tokens_block():
    assert extract_usage('{"info":{"id":"x"},"messages":[]}') is None


def test_parse_run_output():
    ndjson = "\n".join([
        '{"type": "step_start", "sessionID": "ses_1", "part": {}}',
        '{"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "查到了"}}',
        '{"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "stop", "isError": null}}',
    ])
    out = parse_run_output(ndjson)
    assert out["session_id"] == "ses_1"
    assert out["answer"] == "查到了"
    assert out["finish_reason"] == "stop"
    assert out["is_error"] is False


def test_parse_run_output_multiple_steps_keeps_last_finish():
    ndjson = "\n".join([
        '{"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "tool-calls", "isError": null}}',
        '{"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "error", "isError": true}}',
    ])
    out = parse_run_output(ndjson)
    assert out["finish_reason"] == "error"
    assert out["is_error"] is True


def test_parse_run_output_empty():
    out = parse_run_output("")
    assert out == {"session_id": None, "answer": "", "finish_reason": None, "is_error": None}


def test_extract_trace_from_raw_truncated_json():
    """output 截断时从 raw 文本正则提取 tool name / input / status。"""
    raw = ('{"messages":[{"info":{"role":"assistant"},"parts":['
           '{"type":"tool","tool":"huaweicloud-open-mcp_list_products",'
           '"callID":"c1","id":"p1","state":{'
           '"status":"completed",'
           '"input":{"keyword":"云"},'
           '"output":"{\\"ok\\":true,\\"total\\":309,\\"products\\":[...TRUNCATED]'
           '}]}')
    tools = extract_trace_from_raw(raw)
    assert len(tools) == 1
    assert tools[0].tool == "huaweicloud-open-mcp_list_products"
    assert tools[0].input == {"keyword": "云"}
    assert tools[0].status == "completed"


def test_extract_trace_from_raw_multiple_tools():
    raw = ('{"messages":[{"info":{"role":"assistant"},"parts":['
           '{"type":"tool","tool":"huaweicloud-open-mcp_get_api","callID":"c1","state":{'
           '"status":"completed","input":{"product":"ECS","api":"ListServers"},"output":"ok",'
           '"metadata":{}}},'
           '{"type":"tool","tool":"huaweicloud-open-mcp_execute_api","callID":"c2","state":{'
           '"status":"completed","input":{"product":"ECS","api":"ListServers","params":{"limit":1}},"output":"{}"}}]}]}')
    tools = extract_trace_from_raw(raw)
    assert len(tools) == 2
    assert [t.tool for t in tools] == [
        "huaweicloud-open-mcp_get_api",
        "huaweicloud-open-mcp_execute_api",
    ]
    assert tools[1].input == {"product": "ECS", "api": "ListServers", "params": {"limit": 1}}


# ---------- parse_export：单一类型化产物（CONTEXT.md B，哨兵键退役） ----------




def test_parse_export_full_json():
    out = parse_export(_json.dumps(EXPORT))
    assert out is not None and out.recovered is False
    assert out.usage == extract_usage(EXPORT)
    assert [t.tool for t in out.trace] == [
        "huaweicloud-open-mcp_list_products", "huaweicloud-open-mcp_execute_api"]


def test_parse_export_truncated_recovers():
    """截断 JSON：regex 恢复 usage + trace，recovered=True（恢复机制不外泄）。"""
    raw = _json.dumps(EXPORT) + '{"info": {"tokens"'   # 尾部截断
    out = parse_export(raw, recover=True)
    assert out is not None and out.recovered is True
    assert out.usage is not None and out.usage["input"] == 100
    assert len(out.trace) == 2


def test_parse_export_truncated_no_recovery_is_none():
    raw = _json.dumps(EXPORT) + '{"info": {"tokens"'
    assert parse_export(raw, recover=False) is None


def test_parse_export_truncated_without_tokens_returns_none():
    raw = '{"info":{"id":"x"},"messages":[]'
    assert parse_export(raw, recover=True) is None


def test_parse_export_invalid_shapes_none():
    assert parse_export("") is None
    assert parse_export("[]") is None                        # 非 dict
    assert parse_export('{"no_messages": 1}') is None        # 缺 messages
    assert parse_export("not json at all", recover=True) is None  # 无 tokens 可恢复


def test_parse_export_trace_only_recovered():
    """无 usage 可恢复但有工具调用：usage=None + trace 恢复。"""
    raw = 'xx"messages": ["type": "tool", "tool": "t1"'

    out = parse_export(raw, recover=True)
    assert out is not None and out.usage is None and out.recovered is True


# ---------- extract_trace 新契约：仅工具序列（answer 归 live 流） ----------

def test_extract_trace_returns_tools_only():
    tools = extract_trace(EXPORT)
    assert [t.tool for t in tools] == [
        "huaweicloud-open-mcp_list_products", "huaweicloud-open-mcp_execute_api"]
