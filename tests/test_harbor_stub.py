"""Harbor 专用 stub server（S6 单元层：纯核 importlib 加载 + 本地回环）。

被测对象是单文件模板 benchmarks/harbor/task_templates/stub_server.py
（部署形态：exporter 拷贝进各 task 的 environment/，容器内独立进程运行）。
"""

import importlib.util
import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

STUB_PATH = (Path(__file__).resolve().parent.parent / "benchmarks" / "harbor"
             / "task_templates" / "stub_server.py")

FIXTURE = {
    "default_body": {"ok": True, "stub": True},
    "apis": {
        "ECS/ListServersDetails": {
            "body": {"count": 1, "servers": [{"id": "stub-srv-1", "name": "bench-server"}]},
        },
        "ECS/CreateServers": {
            "body": {"job_id": "stub-job-1"},
            "by_region": {"cn-south-1": {"body": {"job_id": "stub-job-south"}}},
        },
    },
}


def _load_module():
    spec = importlib.util.spec_from_file_location("harbor_stub_server", STUB_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_hit():
    mod = _load_module()
    status, body = mod.resolve_response(FIXTURE, "ECS", "ListServersDetails",
                                        "cn-north-4", 200)
    assert (status, body) == (200, {"count": 1, "servers": [{"id": "stub-srv-1",
                                                             "name": "bench-server"}]})


def test_resolve_case_insensitive():
    mod = _load_module()
    _, body = mod.resolve_response(FIXTURE, "ecs", "listserversdetails", "cn-north-4", 200)
    assert body["servers"][0]["id"] == "stub-srv-1"


def test_resolve_miss_falls_back_to_default_body():
    mod = _load_module()
    _, body = mod.resolve_response(FIXTURE, "VPC", "ListVpcs", "cn-north-4", 200)
    assert body == {"ok": True, "stub": True}


def test_resolve_region_override():
    mod = _load_module()
    _, body = mod.resolve_response(FIXTURE, "ECS", "CreateServers", "cn-south-1", 200)
    assert body == {"job_id": "stub-job-south"}
    _, body = mod.resolve_response(FIXTURE, "ECS", "CreateServers", "cn-north-4", 200)
    assert body == {"job_id": "stub-job-1"}


def test_resolve_non_200_empty_body():
    mod = _load_module()
    status, body = mod.resolve_response(FIXTURE, "ECS", "ListServersDetails",
                                        "cn-north-4", 400)
    assert (status, body) == (200, None)


def test_ledger_appends_ndjson(tmp_path):
    mod = _load_module()
    fp = tmp_path / "ledger.jsonl"
    mod.append_ledger(fp, {"method": "GET", "product": "ECS", "api": "X"})
    mod.append_ledger(fp, {"method": "POST", "product": "ECS", "api": "Y"})
    lines = [json.loads(line) for line in fp.read_text(encoding="utf-8").splitlines()]
    assert [line["product"] for line in lines] == ["ECS", "ECS"]
    for line in lines:
        line.pop("ts")
    assert lines[0] == {"method": "GET", "product": "ECS", "api": "X"}


def test_ledger_never_raises(tmp_path):
    mod = _load_module()
    mod.append_ledger(tmp_path, {"method": "GET"})  # 路径是目录，不应抛出


# ---------- 回环：真 HTTP ----------

def _start_server(fixture, ledger=None):
    mod = _load_module()
    handler = mod.build_handler(fixture, ledger)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    return httpd, f"http://{host}:{port}"


def _request(url: str, *, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_loopback_get_canned_and_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    httpd, base = _start_server(FIXTURE, ledger)
    try:
        query = urllib.parse.urlencode({'status_code': 200, 'number': 1,
                                        'region_id': 'cn-north-4'})
        status, raw = _request(f"{base}/v1/mock/ECS/ListServersDetails?{query}")
        assert status == 200
        body = json.loads(raw)
        assert body["servers"][0]["name"] == "bench-server"
        lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        assert lines[0]["method"] == "GET"
        assert lines[0]["product"] == "ECS"
        assert lines[0]["api"] == "ListServersDetails"
        assert lines[0]["query"]["region_id"] == "cn-north-4"
    finally:
        httpd.shutdown()


def test_loopback_post_body_captured(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    httpd, base = _start_server(FIXTURE, ledger)
    try:
        query = urllib.parse.urlencode({'status_code': 200, 'number': 1,
                                        'region_id': 'cn-north-4'})
        status, _raw = _request(f"{base}/v1/mock/ECS/CreateServers?{query}",
                                method="POST", body={"server": {"name": "vm-1"}})
        assert status == 200
        lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert lines[0]["method"] == "POST"
        assert lines[0]["body"] == {"server": {"name": "vm-1"}}
    finally:
        httpd.shutdown()


def test_loopback_status_code_non_200_empty_body(tmp_path):
    httpd, base = _start_server(FIXTURE)
    try:
        query = urllib.parse.urlencode({'status_code': 400, 'number': 1,
                                        'region_id': 'cn-north-4'})
        status, raw = _request(f"{base}/v1/mock/ECS/ListServersDetails?{query}")
        assert status == 200
        assert raw == b""
    finally:
        httpd.shutdown()


def test_loopback_health(tmp_path):
    httpd, base = _start_server(FIXTURE)
    try:
        status, raw = _request(f"{base}/health")
        assert status == 200
        assert json.loads(raw) == {"ok": True}
    finally:
        httpd.shutdown()


def test_loopback_unknown_api_default_body(tmp_path):
    httpd, base = _start_server(FIXTURE)
    try:
        query = urllib.parse.urlencode({'status_code': 200, 'number': 1,
                                        'region_id': 'cn-north-4'})
        status, raw = _request(f"{base}/v1/mock/VPC/ListVpcs?{query}")
        assert status == 200
        assert json.loads(raw) == {"ok": True, "stub": True}
    finally:
        httpd.shutdown()
