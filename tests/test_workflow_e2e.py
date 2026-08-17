"""渐进式工作流 E2E：脚本模拟 LLM 决策，逐步走完整链路。

步骤（对应 server instructions 的推荐工作流）：
① list_products 定产品 → ② list_apis(tag_groups) 定目录 → ③ list_apis(tag) 选接口
→ ④ get_api 读文档 → ⑤ get_api_examples → ⑥ execute_api（mock）。

依赖：真实 data/openapi 产物（api-refresh 生成）与 mock 端点（无需凭证）。
标 e2e（默认跳过），用 `uv run pytest -m e2e` 运行。
"""

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.e2e

PROOT = Path(__file__).resolve().parent.parent


def _require_openapi_data():
    if not (PROOT / "data" / "openapi" / "ECS").is_dir():
        pytest.skip("data/openapi 产物缺失，请先运行 api-refresh")


class McpSession:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "openmcp.server",
             "--mock", "--policy", "configs/safety-policy.example.json"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(PROOT),
        )
        self.schemas: dict[str, dict] = {}

    def call(self, method, params, msg_id):
        self.proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed")
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg

    def tool(self, name, arguments):
        r = self.call("tools/call", {"name": name, "arguments": arguments}, hash((name, str(arguments))))
        # 模拟客户端严格校验：structuredContent 必须符合工具 outputSchema
        #（回归：ExecuteResult 可选字段序列化为 null 时曾触发 -32602）
        schema = self.schemas.get(name)
        if schema is not None:
            jsonschema.validate(instance=r["result"].get("structuredContent") or {}, schema=schema)
        text = r["result"]["content"][0]["text"]
        return json.loads(text)

    def close(self):
        self.proc.terminate()


@pytest.fixture(scope="module")
def session():
    _require_openapi_data()
    s = McpSession()
    s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "workflow-e2e", "version": "0"}}, 1)
    s.call("notifications/initialized", {}, 0)
    r = s.call("tools/list", {}, 2)
    s.schemas = {t["name"]: t.get("outputSchema") or {} for t in r["result"]["tools"]}
    yield s
    s.close()


def test_workflow_instructions_present(session):
    r = session.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "w2", "version": "0"}}, 100)
    instructions = r["result"].get("instructions") or ""
    assert "list_products" in instructions
    assert "tag_groups" in instructions
    assert "safety policy" in instructions


def test_workflow_full_progressive_chain(session):
    # ① 定产品：按任务语义搜索产品
    products = session.tool("list_products", {"keyword": "云服务器"})
    assert products["ok"] is True
    found = {p["product"] for p in products["products"]}
    assert "ECS" in found

    # ② 定目录：首次 list_apis 拿到全量 tag 概览
    catalog = session.tool("list_apis", {"product": "ECS", "limit": 0})
    assert catalog["ok"] is True
    assert catalog["total"] > 50
    tag_names = {g["tag"] for g in catalog["tag_groups"]}
    assert "生命周期管理" in tag_names
    # 概览按接口数降序
    counts = [g["api_count"] for g in catalog["tag_groups"]]
    assert counts == sorted(counts, reverse=True)

    # ③ 选接口：tag 收窄目录
    narrowed = session.tool("list_apis", {"product": "ECS", "tag": "生命周期管理"})
    names = {a["name"] for a in narrowed["apis"]}
    assert "ListServersDetails" in names
    # 概览不受过滤影响
    assert narrowed["tag_groups"] == catalog["tag_groups"]

    # ④ 读文档：调用前必读，检查参数/必填/约束
    detail = session.tool("get_api", {"product": "ECS", "api": "ListServersDetails"})
    assert detail["ok"] is True
    assert detail["method"] == "GET"
    assert detail["path"] == "/v1/{project_id}/cloudservers/detail"
    params = {p["name"]: p for p in detail["parameters"]}
    assert params["project_id"]["required"] is True
    assert "limit" in params
    assert "x-constraint" in detail

    # ⑤ 示例（可选辅助）
    examples = session.tool("get_api_examples", {"product": "ECS", "api": "CreateServers"})
    assert examples["ok"] is True
    assert isinstance(examples["examples"], list)

    # ⑥ 执行（mock）
    result = session.tool("execute_api", {"product": "ECS", "api": "ListServersDetails",
                                          "params": {"limit": 1}})
    assert result["ok"] is True
    assert result["status"] == 200
    assert "count" in result["body"]
    assert "servers" in result["body"]


def test_workflow_policy_denies_out_of_whitelist(session):
    # 目录中发现但被 policy 拒绝的写接口（DeleteServers 不在白名单）
    narrowed = session.tool("list_apis", {"product": "ECS", "tag": "生命周期管理"})
    names = {a["name"] for a in narrowed["apis"]}
    assert "DeleteServers" in names  # 目录可见（读元数据不受限）
    result = session.tool("execute_api", {"product": "ECS", "api": "DeleteServers",
                                          "params": {"server_id": "x"}})
    assert result["ok"] is False
    assert "policy" in result["reason"]
