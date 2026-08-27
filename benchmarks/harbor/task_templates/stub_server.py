"""Harbor 专用 mock 端点 stub（单文件、纯标准库、CLI 形态）。

与 benchmarks/openapi/stub_server.py 完全解耦：该 stub 服务于 Harbor 任务环境
（exporter 拷贝进各 task 的 environment/，容器内独立进程运行）。

HTTP 契约（对齐 API Explorer mock 端点，扩展 passthrough 观测能力）：
- GET/POST /v1/mock/<product>/<api>?status_code=&number=&region_id=[&passthrough query]
- HTTP 状态恒 200；status_code 非 200 返回空 body；未知 api 落 default_body
- POST body（passthrough 的 JSON body）原样记入台账
- GET /health → {"ok": true}

fixture 文件（--fixture，JSON）：
{
  "default_body": {...},                    // 可选，缺省 {"ok": true, "stub": true}
  "apis": {
    "ECS/ListServersDetails": {             // key: <product>/<api>，大小写不敏感
      "body": {...},                        // 该 api 的罐头响应
      "status_code": 200,                    // 可选（保留扩展位）
      "by_region": {"cn-south-1": {...}}     // 可选 region 覆盖（同结构）
    }
  }
}

请求台账（--ledger，NDJSON）：{"ts", "method", "product", "api", "query", "body"}
供 verifier 做 wire 级断言（调用是否到达、参数是否命中线缆）。

用法：python stub_server.py --port 8010 --fixture fixtures.json --ledger ledger.jsonl
"""

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_BODY: dict[str, Any] = {"ok": True, "stub": True}
MOCK_PREFIX = "/v1/mock/"
HEALTH_PATH = "/health"


# ---------- 纯核 ----------

def load_fixture(path: str | None) -> dict[str, Any]:
    """加载 fixture 文件；路径为空或文件缺失时返回空 fixture（全部落默认 body）。"""
    if not path:
        return {}
    fp = Path(path)
    if not fp.exists():
        return {}
    data = json.loads(fp.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _entry_body(entry: dict[str, Any], region: str) -> dict[str, Any]:
    override = entry.get("by_region", {}).get(region)
    source = override if isinstance(override, dict) else entry
    body = source.get("body")
    return body if isinstance(body, dict) else dict(DEFAULT_BODY)


def resolve_response(fixture: dict[str, Any], product: str, api: str, region: str,
                     status_code: int) -> tuple[int, dict[str, Any] | None]:
    """解析响应：非 200 status_code → 空 body；未命中 → default_body；region 覆盖优先。

    fixture 的 apis key 大小写不敏感（<product>/<api> 统一按小写匹配）。
    """
    if status_code != 200:
        return 200, None
    key = f"{product}/{api}".lower()
    entry = None
    for k, v in fixture.get("apis", {}).items():
        if k.lower() == key:
            entry = v
            break
    if not isinstance(entry, dict):
        default = fixture.get("default_body", DEFAULT_BODY)
        return 200, dict(default) if isinstance(default, dict) else dict(DEFAULT_BODY)
    return 200, _entry_body(entry, region)


def append_ledger(path: str | Path | None, record: dict[str, Any]) -> None:
    """追加一条请求台账（NDJSON）。best-effort：写失败不抛出（stderr 告警）。"""
    if path is None:
        return
    try:
        line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **record},
                          ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[stub_server] ledger write failed: {e}", file=sys.stderr)


# ---------- HTTP 壳 ----------

def build_handler(fixture: dict[str, Any], ledger_path: str | Path | None = None
                  ) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle(None)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except (ValueError, UnicodeDecodeError):
                body = None
            self._handle(body)

        def _handle(self, parsed_body: dict[str, Any] | None) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == HEALTH_PATH:
                self._respond(200, {"ok": True})
                return
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) != 4 or f"/{parts[0]}/{parts[1]}/" != MOCK_PREFIX:
                self._respond(200, dict(DEFAULT_BODY))
                return
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            flat_query = {k: v[0] for k, v in qs.items()}
            status_code = int(flat_query.get("status_code", "200") or "200")
            region = flat_query.get("region_id", "")
            product, api = parts[2], parts[3]
            http_status, body = resolve_response(fixture, product, api, region, status_code)
            append_ledger(ledger_path, {
                "method": self.command, "product": product, "api": api,
                "query": flat_query, "body": parsed_body,
            })
            self._respond(http_status, body)

        def _respond(self, status: int, payload: dict[str, Any] | None) -> None:
            data = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    if payload is not None else b"")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stub_server",
                                     description="Harbor 任务环境 mock 端点 stub")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--fixture", default=None, help="罐头响应 fixture（JSON）")
    parser.add_argument("--ledger", default=None, help="请求台账（NDJSON，追加写）")
    args = parser.parse_args(argv)

    fixture = load_fixture(args.fixture)
    handler = build_handler(fixture, args.ledger)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"[stub_server] listening on http://127.0.0.1:{args.port} "
          f"fixture={'loaded' if fixture else 'empty'} ledger={args.ledger or '-'}",
          file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
