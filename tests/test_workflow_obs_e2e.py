"""OBS 渐进式工作流 E2E（MCP 全链路，real 模式真实签名）。

拉起 openapi 模式 server 子进程（不带 --mock），走完整 MCP stdio 协议链路，
验证 OBS 元数据（live 回退）+ is_obs 分派 + HMAC-SHA1 签名 + XML/JSON/octet 全链。

链路：
① get_api 读文档 → ② execute_api(ListBuckets) 真实签名
→ ③ 临时桶自清理写链路：CreateBucket(XML body) → SetBucketTagging(嵌套 XML)
   → GetBucketTagging(子资源) → PutObject 文本(ETag 头摘取)
   → PutObject _content_b64 + GetObject 二进制占位
   → finally 删除对象/标签/桶（失败也执行）
→ ④ policy 拒绝白名单外接口。

依赖：.env 的 AK/SK（conftest 已注入 os.environ，子进程继承）+ 外网。
标 e2e（默认跳过），用 `uv run pytest tests/test_workflow_obs_e2e.py -m e2e` 运行。
"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

PROOT = Path(__file__).resolve().parent.parent

POLICY_ALLOW = [
    "OBS:ListBuckets=allow",
    "OBS:CreateBucket=allow",
    "OBS:SetBucketTagging=allow",
    "OBS:GetBucketTagging=allow",
    "OBS:DeleteBucketTagging=allow",
    "OBS:PutObject=allow",
    "OBS:GetObject=allow",
    "OBS:DeleteObject=allow",
    "OBS:DeleteBucket=allow",
]


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
    policy.write_text(json.dumps(POLICY_ALLOW + ["*=deny"], ensure_ascii=False),
                      encoding="utf-8")
    log = tmp_path_factory.mktemp("obs-e2e-log") / "server.log"

    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["HUAWEICLOUD_MCP_LOG_LEVEL"] = "DEBUG"

    args = [sys.executable, "-m", "main", "--mode", "openapi",
            "--policy", str(policy), "--log-file", str(log), "--log-level", "DEBUG"]
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


def test_obs_write_chain_with_cleanup(session):
    """临时桶自清理式写链路：XML 根元素 / 嵌套 $ref / 子资源签名 / ETag 头 / b64 上传 / 二进制占位。"""
    bucket = f"mcp-e2e-{int(time.time())}"
    created_objects: list[str] = []

    def call(api: str, params: dict) -> dict:
        return session.tool("execute_api",
                            {"product": "OBS", "api": api, "params": params})

    def ok2xx(r: dict, label: str) -> None:
        assert r.get("status") in (200, 201, 202, 204), f"{label} 失败: {r}"

    def _cleanup():
        for key in created_objects:
            call("DeleteObject", {"bucket_name": bucket, "object_key": key})
        call("DeleteBucketTagging", {"bucket_name": bucket})
        call("DeleteBucket", {"bucket_name": bucket})

    # ① 创建桶：body 走 x-xml-root 提升后的 <CreateBucketConfiguration>（P0-A 真实验证）
    ok2xx(call("CreateBucket", {"bucket_name": bucket,
                                "body": {"Location": "cn-north-4"}}), "CreateBucket")

    try:
        # ② 设置标签：schema 形状为 TagSet{Tag:[...]}（容器对象 + $ref 数组）
        ok2xx(call("SetBucketTagging", {
            "bucket_name": bucket,
            "body": {"TagSet": {"Tag": [{"Key": "owner", "Value": "mcp-e2e"}]}}}),
            "SetBucketTagging")

        # ③ 读回标签：空值子资源 ?tagging 参与签名
        r = call("GetBucketTagging", {"bucket_name": bucket})
        ok2xx(r, "GetBucketTagging")
        assert "mcp-e2e" in r["body"]

        # ④ 文本上传：ETag 进响应头（headers 白名单透出）
        r = call("PutObject", {"bucket_name": bucket, "object_key": "hello.txt",
                               "body": "hello-mcp-e2e"})
        ok2xx(r, "PutObject 文本")
        assert r.get("headers", {}).get("etag"), f"缺少 ETag 头: {r}"
        created_objects.append("hello.txt")

        # ⑤ b64 字节上传 + GetObject 二进制占位（size/content_type 准确）
        raw = b"\x00\x01obs\xffbin"
        r = call("PutObject", {"bucket_name": bucket, "object_key": "blob.bin",
                               "_content_b64": base64.b64encode(raw).decode()})
        ok2xx(r, "PutObject b64")
        created_objects.append("blob.bin")

        r = call("GetObject", {"bucket_name": bucket, "object_key": "blob.bin"})
        ok2xx(r, "GetObject 二进制")
        body = r["body"]
        assert isinstance(body, dict) and body.get("note") == "二进制响应未渲染", \
            f"二进制应返回占位而非乱码: {body}"
        assert body.get("size") == len(raw)

        # ⑥ 文本对象可正常读回
        r = call("GetObject", {"bucket_name": bucket, "object_key": "hello.txt"})
        assert r.get("ok") is True and r.get("status") == 200 and \
            r["body"] == "hello-mcp-e2e"
    finally:
        _cleanup()


def test_obs_execute_api_outside_whitelist_denied(session):
    result = session.tool("execute_api",
                          {"product": "OBS", "api": "AbortMultipartUpload",
                           "params": {"bucket_name": "x", "object_key": "o.txt",
                                      "uploadId": "u"}})
    assert result["ok"] is False
    assert "policy" in result["reason"]
