"""S6：benchmark trace 提取（export JSON/NDJSON）与 opencode DB token 读取单测。"""

import sqlite3

from benchmarks.opencode_db import default_db_path, get_session_usage
from benchmarks.trace import extract_trace, parse_run_output

EXPORT = {
    "info": {"id": "ses_abc", "title": "t"},
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
    # 用户消息文本不计入最终回答
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


def _make_db(tmp_path):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("""create table session (
        id text primary key,
        cost real default 0 not null,
        tokens_input integer default 0 not null,
        tokens_output integer default 0 not null,
        tokens_reasoning integer default 0 not null,
        tokens_cache_read integer default 0 not null,
        tokens_cache_write integer default 0 not null)""")
    conn.execute("insert into session values (?, 0.0123, 100, 20, 5, 50, 0)", ("ses_1",))
    conn.commit()
    conn.close()
    return db


def test_get_session_usage(tmp_path):
    db = _make_db(tmp_path)
    u = get_session_usage(db, "ses_1")
    assert u == {"cost": 0.0123, "input": 100, "output": 20,
                 "reasoning": 5, "cache_read": 50, "cache_write": 0}


def test_get_session_usage_unknown_session(tmp_path):
    db = _make_db(tmp_path)
    assert get_session_usage(db, "ses_nope") is None


def test_get_session_usage_missing_db(tmp_path):
    assert get_session_usage(tmp_path / "nope.db", "ses_1") is None


def test_default_db_path_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_db_path() == tmp_path / "opencode" / "opencode.db"


def test_default_db_path_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_db_path() == tmp_path / ".local" / "share" / "opencode" / "opencode.db"
