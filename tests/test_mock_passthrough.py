"""mock 模式 passthrough 参数转发（S4）：编码纯函数 + 本地回环 + service 管线。

passthrough 默认关（与 API Explorer mock 契约兼容）；开启后扁平标量 → query、
body → POST JSON，`_` 前缀控制键剥离。标量编码对齐 real 模式 HttpClient
（bool → str(v).lower()，其余 str()；扁平容器 → 紧凑 JSON 串）。
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apie.memory_store import MemoryStore
from apie.mock import MockApiClient, split_passthrough_params
from mcp_openapi.service import ServiceConfig, ToolService
from safety import policy

FULL_DOC = {
    "swagger": "2.0",
    "host": "ecs.cn-north-4.myhuaweicloud.com",
    "basePath": "/",
    "paths": {
        "/v1/{project_id}/cloudservers/detail": {
            "get": {
                "operationId": "ListServersDetails",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "string", "required": True},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
    "definitions": {},
}


# ---------- 编码纯函数 ----------

def test_split_none_returns_empty():
    assert split_passthrough_params(None) == ([], None)


def test_split_strips_control_keys():
    query, body = split_passthrough_params({
        "_status_code": 400, "_number": 2, "_presign": True,
        "_presign_expires": 300, "_presign_content_type": "text/plain",
        "limit": 5,
    })
    assert body is None
    assert query == [("limit", "5")]


def test_split_scalar_encoding_aligns_real_mode():
    query, body = split_passthrough_params(
        {"limit": 5, "name": "vm-1", "dry_run": True, "weight": 1.5})
    assert body is None
    assert dict(query) == {"limit": "5", "name": "vm-1",
                           "dry_run": "true", "weight": "1.5"}


def test_split_container_json_encoded():
    query, body = split_passthrough_params({"filter": {"status": "ACTIVE"}, "ids": [1, 2]})
    assert body is None
    assert dict(query) == {"filter": '{"status": "ACTIVE"}', "ids": "[1, 2]"}


def test_split_body_extracted():
    query, body = split_passthrough_params(
        {"region_id": "cn-north-4", "body": {"server": {"name": "vm-1"}}})
    assert body == {"server": {"name": "vm-1"}}
    assert dict(query) == {"region_id": "cn-north-4"}


# ---------- 本地回环（真 HTTP，不经网关内部桩） ----------

class _CaptureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._capture(None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self._capture(self.rfile.read(length) if length else b"")

    def _capture(self, raw_body):
        parsed = urllib.parse.urlparse(self.path)
        self.server.captures.append({  # type: ignore[attr-defined]
            "method": self.command,
            "path": parsed.path,
            "query": {k: v[0] for k, v in
                      urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()},
            "body": json.loads(raw_body) if raw_body else None,
            "content_type": self.headers.get("Content-Type"),
        })
        payload = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class CaptureServer:
    def __init__(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self._httpd.captures = []
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def captures(self):
        return self._httpd.captures

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


def _policy_allow_ecs():
    return policy.parse_policy(["ECS:*=allow", "*=deny"])


def test_mock_request_passthrough_get_query_hits_local_stub():
    with CaptureServer() as stub:
        client = MockApiClient(base_url=stub.base_url)
        resp = client.mock_request("ECS", "ListServersDetails", "cn-north-4",
                                   params={"limit": 5, "dry_run": True, "_number": 2})
    assert resp["status"] == 200
    assert len(stub.captures) == 1
    cap = stub.captures[0]
    assert cap["method"] == "GET"
    assert cap["path"] == "/v1/mock/ECS/ListServersDetails"
    # mock 契约三元组 + passthrough 标量
    assert cap["query"]["status_code"] == "200"
    assert cap["query"]["region_id"] == "cn-north-4"
    assert cap["query"]["limit"] == "5"
    assert cap["query"]["dry_run"] == "true"
    assert "_number" not in cap["query"]


def test_mock_request_passthrough_post_body_hits_local_stub():
    with CaptureServer() as stub:
        client = MockApiClient(base_url=stub.base_url)
        resp = client.mock_request("ECS", "ListServersDetails", "cn-north-4",
                                   params={"body": {"server": {"name": "vm-1"}}})
    assert resp["status"] == 200
    cap = stub.captures[0]
    assert cap["method"] == "POST"
    assert cap["body"] == {"server": {"name": "vm-1"}}
    assert cap["content_type"] == "application/json"
    assert set(cap["query"]) == {"status_code", "number", "region_id"}


def test_mock_request_default_unchanged():
    with CaptureServer() as stub:
        client = MockApiClient(base_url=stub.base_url)
        client.mock_request("ECS", "ListServersDetails", "cn-north-4")
    cap = stub.captures[0]
    assert cap["method"] == "GET"
    assert cap["body"] is None
    assert set(cap["query"]) == {"status_code", "number", "region_id"}


# ---------- service 管线 ----------

class StubMockClient:
    def __init__(self):
        self.calls = []

    def mock_request(self, product, api_name, region, status_code=200, number=1,
                     params=None):
        self.calls.append((product, api_name, region, status_code, number, params))
        return {"status": 200, "headers": {}, "body": {"mock": True}}


def _service(mock_client, *, passthrough):
    store = MemoryStore()
    store.set_api_cache(
        ("ecs", "ListServersDetails", "cn-north-4"),
        (FULL_DOC, "/v1/{project_id}/cloudservers/detail", "get",
         FULL_DOC["paths"]["/v1/{project_id}/cloudservers/detail"]["get"]),
    )
    return ToolService(store=store, config=ServiceConfig(
        mock=True, policy_rules=_policy_allow_ecs(), mock_passthrough=passthrough,
        mock_client_factory=lambda: mock_client))


def test_service_passthrough_off_by_default_keeps_legacy_signature():
    client = StubMockClient()
    service = _service(client, passthrough=False)
    out = service.execute_api("ECS", "ListServersDetails",
                              params={"limit": 1, "_status_code": 400})
    assert out["ok"] is True
    assert client.calls == [("ECS", "ListServersDetails", "cn-north-4", 400, 1, None)]


def test_service_passthrough_on_forwards_params():
    """client 边界收到原始 params（含控制键）；控制键剥离在 mock_request 编码层内完成，
    wire 侧剥离由回环用例（query 断言）覆盖。"""
    client = StubMockClient()
    service = _service(client, passthrough=True)
    out = service.execute_api("ECS", "ListServersDetails",
                              params={"limit": 1, "_status_code": 400, "_number": 3})
    assert out["ok"] is True
    assert client.calls == [("ECS", "ListServersDetails", "cn-north-4", 400, 3,
                             {"limit": 1, "_status_code": 400, "_number": 3})]


def test_service_passthrough_end_to_end_via_local_stub():
    with CaptureServer() as stub:
        store = MemoryStore()
        store.set_api_cache(
            ("ecs", "ListServersDetails", "cn-north-4"),
            (FULL_DOC, "/v1/{project_id}/cloudservers/detail", "get",
             FULL_DOC["paths"]["/v1/{project_id}/cloudservers/detail"]["get"]),
        )
        service = ToolService(store=store, config=ServiceConfig(
            mock=True, mock_base=stub.base_url, mock_passthrough=True,
            policy_rules=_policy_allow_ecs()))
        out = service.execute_api("ECS", "ListServersDetails",
                                  params={"limit": 7, "body": {"k": "v"}})
    assert out["ok"] is True
    assert out["body"] == {"ok": True}
    cap = stub.captures[0]
    assert cap["method"] == "POST"
    assert cap["query"]["limit"] == "7"
    assert cap["body"] == {"k": "v"}
