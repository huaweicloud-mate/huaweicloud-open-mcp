"""opencode 会话 DB 的 token/cost 读取（S6，只读访问）。"""

import os
import sqlite3
from pathlib import Path

_COLUMNS = ("cost", "tokens_input", "tokens_output", "tokens_reasoning",
            "tokens_cache_read", "tokens_cache_write")


def default_db_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data_home) / "opencode" / "opencode.db"


def get_session_usage(db_path: Path, session_id: str) -> dict[str, int | float] | None:
    """读 session 表的 token/cost 汇总；会话或 DB 不存在返回 None。"""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            f"select {', '.join(_COLUMNS)} from session where id=?", (session_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return {"cost": row[0], "input": row[1], "output": row[2],
            "reasoning": row[3], "cache_read": row[4], "cache_write": row[5]}
