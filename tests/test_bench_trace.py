"""S6：benchmark trace 提取（export JSON/NDJSON）单测。"""

from benchmarks.trace import extract_trace, extract_usage, parse_run_output

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
    tools, answer = extract_trace(EXPORT)
    assert [(t.tool, t.status) for t in tools] == [
        ("huaweicloud-open-mcp_list_products", "completed"),
        ("huaweicloud-open-mcp_execute_api", "completed"),
    ]
    assert tools[1].input == {"product": "ECS", "api": "ListServersDetails", "params": {"limit": 1}}
    assert answer == "找到了产品\n共 1 台 bench-server"


def test_extract_trace_tolerates_missing_state():
    export = {"info": {}, "messages": [
        {"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "tool": "huaweicloud-open-mcp_get_api"},
            {"type": "text", "text": "x"},
        ]},
    ]}
    tools, answer = extract_trace(export)
    assert tools[0].input == {}
    assert tools[0].status == ""
    assert answer == "x"


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
