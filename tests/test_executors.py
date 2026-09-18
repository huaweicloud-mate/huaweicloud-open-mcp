"""mcp_openapi.execute 执行接缝单测（S4 扩展，CONTEXT.md A/C6）：

ApiExecutor 双 adapter（RealApiExecutor/MockApiExecutor）+ RequestRefusal +
execute_api 编排。独立真值：StubClient 捕获调用字面量（沿 test_tools_execute
idiom）；RequestRefusal 消息与旧 _refuse 文案逐字节一致（回归红线）。
"""

import pytest

from apie.api_location import ApiLocation
from common.auth.credentials import Credentials
from common.types import ClientResponse
from mcp_openapi import execute
from mcp_openapi.signer.client import HttpClient


class StubClient:
    def __init__(self, responses: list[ClientResponse] | None = None):
        self.responses = responses or []
        self.calls: list[tuple] = []

    def request(self, method, host, path, query=None, body=None, headers=None):
        self.calls.append((method, host, path, query, body, headers))
        return self.responses.pop(0) if self.responses else {"status": 200, "body": {}}


class StubMock:
    def __init__(self):
        self.calls: list[tuple] = []

    def mock_request(self, product, api_name, region, status_code=200,
                     number=1, params=None):
        self.calls.append((product, api_name, region, status_code, number, params))
        return {"status": 200, "headers": {}, "body": {"mock": True}}


DOC = {
    "swagger": "2.0",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "paths": {"/v1/{project_id}/servers": {"get": {
        "operationId": "ListServers", "parameters": [],
        "responses": {"200": {"description": "OK"}}}}},
}
LOC = ApiLocation(DOC, "/v1/{project_id}/servers", "get",
                  DOC["paths"]["/v1/{project_id}/servers"]["get"])

CRED = Credentials(ak="AK", sk="SK", project_id="proj123")

# 无 {project_id} 占位的路径（X-Project-Id 头补发场景）
LOC_NOPID = ApiLocation(
    {**DOC, "paths": {"/v1/servers": DOC["paths"]["/v1/{project_id}/servers"]}},
    "/v1/servers", "get", DOC["paths"]["/v1/{project_id}/servers"]["get"])


# ---------- RealApiExecutor ----------

def test_real_executor_builds_and_sends():
    client = StubClient([{"status": 200, "body": {"x": 1}}])
    ex = execute.RealApiExecutor(client, CRED)
    resp = ex.request(LOC_NOPID, "ECS", "ListServers", "cn-north-4", {"limit": 5})
    assert resp == {"status": 200, "body": {"x": 1}}
    method, host, path, query, body, headers = client.calls[0]
    assert (method, host) == ("GET", "ecs.cn-north-4.myhuaweicloud.com")
    assert path == "/v1/servers"
    assert query == {"limit": 5}
    assert headers["Content-Type"] == "application/json"   # 全局默认 CT
    assert headers["X-Project-Id"] == "proj123"    # 无 {project_id} 占位时补头


def test_real_executor_path_fill_no_extra_header():
    """路径含 {project_id}（已由凭证填充）时不补 X-Project-Id 头。"""
    client = StubClient()
    execute.RealApiExecutor(client, CRED).request(LOC, "ECS", "ListServers",
                                                  "cn-north-4", {})
    assert "X-Project-Id" not in client.calls[0][5]


def test_real_executor_base_path_prefix_and_resign():
    doc = dict(DOC, basePath="/v2")
    loc = ApiLocation(doc, "/v1/{project_id}/servers", "get", LOC.op)
    client = StubClient()
    execute.RealApiExecutor(client, CRED).request(loc, "ECS", "ListServers",
                                                  "cn-north-4", {})
    assert client.calls[0][2] == "/v2/v1/proj123/servers"


def test_real_executor_missing_path_param_raises_refusal():
    client = StubClient()
    ex = execute.RealApiExecutor(client, credentials=None)   # 无凭证不可填充
    with pytest.raises(execute.RequestRefusal) as ei:
        ex.request(LOC, "ECS", "ListServers", "cn-north-4", {})
    assert "缺少必填路径参数 project_id" in str(ei.value)
    assert client.calls == []                              # 未发请求


def test_real_executor_missing_host_raises_refusal():
    loc = ApiLocation({"swagger": "2.0", "paths": {}}, "/p", "get", {})
    with pytest.raises(execute.RequestRefusal) as ei:
        execute.RealApiExecutor(StubClient(), CRED).request(
            loc, "ECS", "X", "cn-north-4", {})
    assert "缺少 host" in str(ei.value)


def test_real_executor_mode_token():
    assert execute.RealApiExecutor(StubClient(), CRED).mode == "real"


def test_real_executor_composes_with_http_client():
    """与签名客户端组合：HttpClient 满足传输端口（SignedClient 鸭子形状）。"""
    ex = execute.RealApiExecutor(HttpClient(CRED), CRED)
    assert callable(ex.client.request)


# ---------- MockApiExecutor ----------

def test_mock_executor_peels_control_keys():
    mock = StubMock()
    execute.MockApiExecutor(mock).request(LOC, "ECS", "ListServers",
                                          "cn-north-4", {"_status_code": 404,
                                                         "_number": 2})
    assert mock.calls[0][:5] == ("ECS", "ListServers", "cn-north-4", 404, 2)
    assert mock.calls[0][5] is None                        # 默认不透传


def test_mock_executor_passthrough_forwards_params():
    mock = StubMock()
    params = {"_status_code": 200, "limit": 5, "body": {"k": "v"}}
    execute.MockApiExecutor(mock, passthrough=True).request(
        LOC, "ECS", "ListServers", "cn-north-4", params)
    assert mock.calls[0][5] is params                      # 原样转发（编码在 mock 层）


def test_mock_executor_never_raises_refusal():
    mock = StubMock()
    ex = execute.MockApiExecutor(mock)
    assert ex.request(LOC, "ECS", "X", "cn-north-4", {})["status"] == 200
    assert ex.mode == "mock"


# ---------- execute_api 编排 ----------

def test_execute_api_wraps_envelope():
    client = StubClient([{"status": 200, "body": {"ok_data": 1}}])
    out = execute.execute_api(LOC, "ECS", "ListServers", "cn-north-4", {},
                              executor=execute.RealApiExecutor(client, CRED))
    assert out["ok"] is True and out["product"] == "ECS"
    assert out["api"] == "ListServers" and out["body"] == {"ok_data": 1}


def test_execute_api_catches_refusal():
    out = execute.execute_api(LOC, "ECS", "ListServers", "cn-north-4", {},
                              executor=execute.RealApiExecutor(StubClient()))
    assert out == {"ok": False,
                   "reason": "缺少必填路径参数 project_id"
                             "（可用凭证 project_id 自动填充 project_id）"}


def test_execute_api_normalize_error_envelope():
    client = StubClient([{"status": 404, "body": {"error_code": "E.404",
                                                  "error_msg": "not found"}}])
    out = execute.execute_api(LOC, "ECS", "ListServers", "cn-north-4", {},
                              executor=execute.RealApiExecutor(client, CRED))
    assert out["ok"] is True            # 信封 ok=执行成功（HTTP 404 是业务错误体）
    assert out["status"] == 404 and out["error_code"] == "E.404"
