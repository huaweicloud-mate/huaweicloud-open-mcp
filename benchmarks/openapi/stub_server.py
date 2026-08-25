"""本地 mock 端点 stub：确定性响应，隔离网络抖动（benchmark 用）。

URL 契约对齐 API Explorer mock 端点（apie/mock.py）：GET /v1/mock/<product>/<api>
?status_code=&number=&region_id=；实测行为保持一致——HTTP 状态恒 200，
status_code 非 200 返回空 body。
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CANNED: dict[tuple[str, str], dict[str, Any]] = {
    ("ecs", "listserversdetails"): {
        "count": 1,
        "servers": [
            {"id": "stub-srv-1", "name": "bench-server", "status": "ACTIVE",
             "flavor": "s6.small.1", "addresses": {}, "tags": []},
        ],
    },
    ("ecs", "listcloudservers"): {
        "count": 1,
        "servers": [
            {"id": "stub-srv-1", "name": "bench-server", "status": "ACTIVE",
             "flavor": "s6.small.1", "addresses": {}, "tags": []},
        ],
    },
    ("vpc", "listvpcs"): {
        "vpcs": [{"id": "stub-vpc-1", "name": "bench-vpc", "cidr": "192.168.0.0/16",
                   "status": "ACTIVE"}],
    },
    # 「先查后写」写操作的前置查询：返回非空列表，ID 对齐 prompt（mock 不校验值，字段名对齐真实 schema）
    ("ecs", "listrecyclebinservers"): {
        "servers": [{"id": "srv-001", "name": "bench-server", "status": "ACTIVE"}],
    },
    ("ecs", "showrecyclebin"): {
        "project_id": "proj123", "switch": "on",
        "policy": {"recycle_threshold_day": 1, "retention_hour": 7},
    },
    ("ecs", "listservergroups"): {
        "server_groups": [{"id": "ecs-group", "name": "ecs-group",
                           "policies": ["anti-affinity"], "members": [], "metadata": {}}],
    },
    ("ecs", "listscheduledevents"): {
        "events": [{"id": "evt-001", "type": "instance-rebuild", "state": "waiting"}],
        "page_info": {},
    },
}

DEFAULT_BODY: dict[str, Any] = {"ok": True, "stub": True}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        body = DEFAULT_BODY
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "mock":
            qs = urllib.parse.parse_qs(parsed.query)
            status_code = int(qs.get("status_code", ["200"])[0])
            canned = CANNED.get((parts[2].lower(), parts[3].lower()))
            if status_code != 200:
                self._respond(200, b"")
                return
            body = canned or DEFAULT_BODY
        self._respond(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def _respond(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


class StubServer:
    def __init__(self, port: int = 0):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return "http://" + str(host) + ":" + str(port)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "StubServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
