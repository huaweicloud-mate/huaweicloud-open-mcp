"""OBS 渐进式工作流 E2E（MCP 全链路，real 模式真实签名）。

拉起 openapi 模式 server 子进程（不带 --mock），走完整 MCP stdio 协议链路，
验证 OBS 元数据（live 回退）+ is_obs 分派 + HMAC-SHA1 签名 + XML 响应全链。

步骤：① get_api 读文档 → ② execute_api 执行 ListBuckets（真实签名）
    → ③ policy 拒绝写接口。

依赖：.env 的 AK/SK（conftest 已注入 os.environ，子进程继承）+ 外网（apiexplorer + OBS）。
标 e2e（默认跳过），用 `uv run pytest tests/test_workflow_obs_e2e.py -m e2e` 运行。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

PROOT = Path(__file__).resolve().parent.parent


class McpSession:
    def __init__(self, args: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(PROOT), env=env,
        )
        self.schemas: dict[str, dict] = {}
        self._log: Path | None = None

    @property
    def stderr_text(self) -> str:
        if self.proc.poll() is not None and self.proc.stderr is not None:
            return self.proc.stderr.read().decode(errors="replace")
        return ""

    def call(self, method: str, params: dict, msg_id: int) -> dict:
        self.proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                detail = self.stderr_text
                raise RuntimeError(f"server closed (stderr: {detail[:500]})")
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg

    def tool(self, name: str, arguments: dict) -> dict:
        r = self.call("tools/call", {"name": name, "arguments": arguments},
                      hash((name, str(arguments))))
        text = r["result"]["content"][0]["text"]
        return json.loads(text)

    def init(self) -> None:
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "obs-e2e", "version": "0"}}, 1)
        self.call("notifications/initialized", {}, 0)
        lst = self.call("tools/list", {}, 2)
        self.schemas = {t["name"]: t.get("outputSchema") or {} for t in lst["result"]["tools"]}

    def close(self) -> None:
        self.proc.terminate()


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    policy = tmp_path_factory.mktemp("obs-e2e") / "policy.json"
    policy.write_text(json.dumps(["OBS:ListBuckets=allow", "*=deny"], ensure_ascii=False),
                      encoding="utf-8")
    log = tmp_path_factory.mktemp("obs-e2e-log") / "server.log"

    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)

    args = [sys.executable, "-m", "main", "--mode", "openapi",
            "--policy", str(policy), "--log-file", str(log)]
    s = McpSession(args, env)
    s.init()
    s._log = log
    try:
        yield s
    finally:
        s.close()
        if log.exists():
            print("\n=== OBS SERVER LOG ===", log.read_text(), sep="\n", file=sys.stderr)


def test_obs_get_api_metadata(session):
    detail = session.tool("get_api", {"product": "OBS", "api": "ListBuckets"})
    assert detail["ok"] is True
    assert detail["method"] == "GET"
    assert detail["path"] == "/"
    params = {p["name"] for p in detail["parameters"]}
    assert "bucket_name" not in params  # ListBuckets 无桶名


def test_obs_execute_list_buckets_real(session):
    result = session.tool("execute_api", {"product": "OBS", "api": "ListBuckets", "params": {}})
    assert result["ok"] is True
    assert result["status"] == 200
    assert "ListAllMyBucketsResult" in result["body"]


def test_obs_execute_write_denied_by_policy(session):
    result = session.tool("execute_api", {"product": "OBS", "api": "DeleteBucket",
                                          "params": {"bucket_name": "x"}})
    assert result["ok"] is False
    assert "policy" in result["reason"]
