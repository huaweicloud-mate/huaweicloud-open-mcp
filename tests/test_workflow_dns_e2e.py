"""DNS declared default 填充 E2E（P1，MCP 全链路，real 模式真实签名）。

拉起 openapi 模式 server 子进程（不带 --mock），走完整 MCP stdio 协议链路，
验证 P1 修复：DNS:ListPrivateZones 的 type 参数元数据 required:true +
default:"private"（官方文档标必选、取值 private），不传 type 时网关自动填充
declared default 进 query，返回内网 zone 列表（此前被必填校验硬拒绝）。

只读红线：仅 GET 查询，不创建任何资源。

依赖：.env 的 AK/SK（conftest 已注入 os.environ，子进程继承）+ 外网。
标 e2e（默认跳过），用 `uv run pytest tests/test_workflow_dns_e2e.py -m e2e` 运行。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.e2e

PROOT = Path(__file__).resolve().parent.parent

POLICY_ALLOW = [
    "DNS:ListPrivateZones=allow",
    "DNS:ListPublicZones=allow",
]


class McpSession:
    def __init__(self, args: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(PROOT), env=env,
        )
        self.schemas: dict[str, dict] = {}

    def call(self, method: str, params: dict, msg_id: int) -> dict:
        self.proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                         "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed")
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg

    def tool(self, name: str, arguments: dict) -> dict:
        r = self.call("tools/call", {"name": name, "arguments": arguments},
                      hash((name, str(arguments))))
        # 模拟客户端严格校验：structuredContent 必须符合工具 outputSchema
        schema = self.schemas.get(name)
        if schema is not None:
            jsonschema.validate(instance=r["result"].get("structuredContent") or {},
                                schema=schema)
        text = r["result"]["content"][0]["text"]
        return json.loads(text)

    def close(self) -> None:
        self.proc.terminate()


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    if not os.environ.get("HUAWEICLOUD_SDK_AK"):
        pytest.skip("缺少 HUAWEICLOUD_SDK_AK（.env 或环境变量）")
    policy = tmp_path_factory.mktemp("dns-e2e") / "policy.json"
    policy.write_text(json.dumps(POLICY_ALLOW + ["*=deny"], ensure_ascii=False),
                      encoding="utf-8")
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    args = [sys.executable, "-m", "huaweicloud_open_mcp", "--mode", "openapi",
            "--policy", str(policy)]
    s = McpSession(args, env)
    s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "dns-e2e", "version": "0"}}, 1)
    s.call("notifications/initialized", {}, 0)
    lst = s.call("tools/list", {}, 2)
    s.schemas = {t["name"]: t.get("outputSchema") or {}
                 for t in lst["result"]["tools"]}
    try:
        yield s
    finally:
        s.close()


def test_dns_metadata_declares_required_with_default(session):
    """元数据事实锚定：type required:true 且 default:"private"（填充前提）。"""
    detail = session.tool("get_api", {"product": "DNS", "api": "ListPrivateZones"})
    assert detail["ok"] is True
    type_decl = next(p for p in detail["parameters"] if p.get("name") == "type")
    assert type_decl.get("required") is True
    assert type_decl.get("default") == "private"


def test_dns_list_private_zones_fill_applied(session):
    """P1 主链：不传 type → 自动填充 private → 200 + applied_defaults 披露。"""
    result = session.tool("execute_api",
                          {"product": "DNS", "api": "ListPrivateZones"})
    assert result["ok"] is True, result.get("reason")
    assert result["status"] == 200
    assert result["applied_defaults"] == {"type": "private"}
    body = result["body"]
    assert "zones" in body and "metadata" in body
    for zone in body.get("zones") or []:
        assert zone["zone_type"] == "private"   # 填充方向正确（非公网混入）


def test_dns_list_public_zones_explicit_value_passthrough(session):
    """显式传值不被覆盖：ListPublicZones 传 type=public 原样透传。"""
    result = session.tool("execute_api",
                          {"product": "DNS", "api": "ListPublicZones",
                           "params": {"type": "public", "limit": 1}})
    assert result["ok"] is True, result.get("reason")
    assert result["status"] == 200
    assert "applied_defaults" not in result
