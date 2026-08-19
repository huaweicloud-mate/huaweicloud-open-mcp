"""MCP Streamable HTTP server 通用基类。

JSON-RPC over HTTP：initialize / tools/list / tools/call / notifications/initialized。
线程安全的 ThreadingHTTPServer，tool handler 通过装饰器注册。
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

ToolHandler = Callable[..., dict[str, Any]]


class _Handler(BaseHTTPRequestHandler):
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

        outer: "BaseMcpHttpServer" = self.server._mcp_server  # type: ignore[attr-defined]

        if method == "initialize":
            session_id = str(uuid.uuid4())
            outer._sessions[session_id] = {"initialized": False}
            result = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": outer.name, "version": outer.version},
                "capabilities": {"tools": {}},
            }
            self._respond_jsonrpc(req_id, result, session_id=session_id)
        elif method == "notifications/initialized":
            if session_id in outer._sessions:
                outer._sessions[session_id]["initialized"] = True
            self.send_response(204)
            self.end_headers()
        elif method == "tools/list":
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in outer._tools.values()
            ]
            self._respond_jsonrpc(req_id, {"tools": tools})
        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            tool = outer._tools.get(tool_name)
            if tool is None:
                self._respond_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}")
                return
            arguments = params.get("arguments") or {}
            try:
                result = tool.handler(arguments)
            except Exception as exc:
                result = {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                    "isError": True,
                }
            self._respond_jsonrpc(req_id, result)
        else:
            self._respond_jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    def _respond_jsonrpc(self, req_id: int | None, result: Any,
                         session_id: str | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "result": result}
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


class ToolDef:
    def __init__(self, name: str, description: str, input_schema: dict[str, Any],
                 handler: ToolHandler):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


class BaseMcpHttpServer:
    """MCP Streamable HTTP 本地 server 基类（线程安全）。"""

    def __init__(self, name: str, version: str, port: int = 0):
        self.name = name
        self.version = version
        self._tools: dict[str, ToolDef] = {}
        self._sessions: dict[str, dict[str, bool]] = {}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd._mcp_server = self  # type: ignore[attr-defined]

    def tool(self, name: str, description: str,
             input_schema: dict[str, Any] | None = None) -> Callable[[ToolHandler], ToolHandler]:
        """装饰器：注册工具。handler 接收 arguments dict，返回 MCP result dict。"""

        def decorator(fn: ToolHandler) -> ToolHandler:
            self._tools[name] = ToolDef(
                name=name,
                description=description,
                input_schema=input_schema or {"type": "object", "properties": {}},
                handler=fn,
            )
            return fn

        return decorator

    @property
    def endpoint(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/mcp"

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._sessions.clear()
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "BaseMcpHttpServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
