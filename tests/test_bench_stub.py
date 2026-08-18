"""S6：benchmark 本地 mock stub 单测（回环 HTTP，不联网）。"""

import json
import urllib.request

from benchmarks.stub_server import StubServer


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read()


def test_stub_lifecycle_and_canned_ecs():
    with StubServer() as s:
        assert s.base_url.startswith("http://127.0.0.1:")
        status, body = _get(f"{s.base_url}/v1/mock/ECS/ListServersDetails"
                            f"?status_code=200&number=1&region_id=cn-north-4")
        assert status == 200
        data = json.loads(body)
        assert data["count"] == 1
        assert data["servers"][0]["name"] == "bench-server"


def test_stub_canned_vpc():
    with StubServer() as s:
        _, body = _get(f"{s.base_url}/v1/mock/VPC/ListVpcs?status_code=200")
        data = json.loads(body)
        assert data["vpcs"][0]["name"] == "bench-vpc"


def test_stub_non_200_status_empty_body():
    with StubServer() as s:
        status, body = _get(f"{s.base_url}/v1/mock/ECS/ListServersDetails?status_code=404")
        assert status == 200  # 与真实 mock 端点一致：HTTP 恒 200
        assert body == b""


def test_stub_unknown_api_default_body():
    with StubServer() as s:
        _, body = _get(f"{s.base_url}/v1/mock/ECS/SomeUnknownApi")
        assert json.loads(body) == {"ok": True, "stub": True}
