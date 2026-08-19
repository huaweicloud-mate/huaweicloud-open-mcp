"""MCP Streamable HTTP 本地 stub server（test 集成用）。

别名：本地 HTTP server 说 MCP JSON-RPC 协议。
- POST /mcp JSON-RPC：initialize / tools/list / tools/call
- Mcp-Session-Id 头跟踪会话
- 线程安全，用于 SDK client 回环集成测试
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CANNED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_servers",
        "description": "List all ECS cloud servers with optional status filter",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by server status, e.g. ACTIVE",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_server",
        "description": "Get details of a single ECS cloud server",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {
                    "type": "string",
                    "description": "The server instance ID",
                },
            },
            "required": ["server_id"],
        },
    },
]

CANNED_RESULTS: dict[str, dict[str, Any]] = {
    "list_servers": {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"servers": [
                        {"id": "srv-1", "name": "stub-ecs-1", "status": "ACTIVE"},
                        {"id": "srv-2", "name": "stub-ecs-2", "status": "SHUTOFF"},
                    ]}
                ),
            }
        ]
    },
    "get_server": {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"id": "srv-1", "name": "stub-ecs-1", "status": "ACTIVE",
                     "flavor": "s6.small.1", "vcpus": 1, "ram": 1024}
                ),
            }
        ]
    },
}


class _MCPHandler(BaseHTTPRequestHandler):
    """MCP Streamable HTTP JSON-RPC handler。"""

    # 会话状态：key = session_id, value = {"initialized": bool}
    _sessions: dict[str, dict[str, bool]] = {}

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._respond_jsonrpc_error(None, -32700, "Parse error")
            return

        method = request.get("method", "")
        req_id = request.get("id")
        session_id = self.headers.get("Mcp-Session-Id") or ""

        if method == "initialize":
            session_id = str(uuid.uuid4())
            self._sessions[session_id] = {"initialized": False}
            result = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "stub-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            }
            self._respond_jsonrpc(req_id, result, session_id=session_id)
        elif method == "notifications/initialized":
            if session_id in self._sessions:
                self._sessions[session_id]["initialized"] = True
            self.send_response(204)
            self.end_headers()
        elif method == "tools/list":
            self._respond_jsonrpc(req_id, {"tools": CANNED_TOOLS})
        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            result = CANNED_RESULTS.get(tool_name, {"content": [{"type": "text", "text": "{}"}]})
            self._respond_jsonrpc(req_id, result)
        else:
            self._respond_jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    def _respond_jsonrpc(self, req_id: int | None, result: Any,
                         session_id: str | None = None) -> None:
        payload = {"jsonrpc": "2.0", "result": result}
        if req_id is not None:
            payload["id"] = req_id
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_jsonrpc_error(self, req_id: int | None, code: int,
                                message: str) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
        if req_id is not None:
            payload["id"] = req_id
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass

    @classmethod
    def clear_sessions(cls) -> None:
        cls._sessions.clear()


class StubMcpServer:
    """线程安全的 MCP Streamable HTTP stub server。"""

    def __init__(self, port: int = 0):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _MCPHandler)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return "http://" + str(host) + ":" + str(port)

    @property
    def endpoint(self) -> str:
        return self.base_url + "/mcp"

    def start(self) -> None:
        _MCPHandler.clear_sessions()
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "StubMcpServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
