"""ECS MCP Server（测试用）：5 个工具 + 内存可变状态。

启动: uv run test-ecs-mcp [--port PORT]

工具：
- list_servers(status?, limit?)  查询实例列表
- get_server(server_id)          查询单个实例详情
- start_server(server_id)        启动实例（SHUTOFF→ACTIVE）
- stop_server(server_id)         停止实例（ACTIVE→SHUTOFF）
- create_server(name, flavor, image_id)  创建实例

状态在内存中可变（启停/创建操作后 list_servers 可见变化）。
"""

import argparse
import json
from typing import Any

from tests.servers.base import BaseMcpHttpServer

INITIAL_SERVERS: list[dict[str, Any]] = [
    {
        "server_id": "ecs-001", "name": "web-server-1", "status": "ACTIVE",
        "flavor": "s6.small.1", "image_id": "ubuntu-22.04",
        "vcpus": 1, "ram": 1024,
        "addresses": {"private": ["192.168.1.10"]},
    },
    {
        "server_id": "ecs-002", "name": "db-server-1", "status": "ACTIVE",
        "flavor": "s6.large.2", "image_id": "centos-7.9",
        "vcpus": 2, "ram": 4096,
        "addresses": {"private": ["192.168.1.20"]},
    },
    {
        "server_id": "ecs-003", "name": "test-server", "status": "SHUTOFF",
        "flavor": "s6.small.1", "image_id": "ubuntu-22.04",
        "vcpus": 1, "ram": 1024,
        "addresses": {"private": []},
    },
]


def make_server(port: int = 0) -> BaseMcpHttpServer:
    srv = BaseMcpHttpServer(name="ecs-mcp", version="1.0.0", port=port)
    servers = [dict(s) for s in INITIAL_SERVERS]
    _next_id = 4

    def _make_result(data: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}

    @srv.tool("list_servers", "查询 ECS 实例列表，可按状态过滤",
              {"type": "object", "properties": {
                  "status": {"type": "string", "description": "过滤状态：ACTIVE / SHUTOFF / ERROR"},
                  "limit": {"type": "integer", "description": "最大返回数量"},
              }})
    def list_servers(args: dict[str, Any]) -> dict[str, Any]:
        status = args.get("status")
        limit = args.get("limit")
        result = [s for s in servers if status is None or s["status"] == status]
        if limit and isinstance(limit, int) and limit > 0:
            result = result[:limit]
        return _make_result({"servers": result, "count": len(result)})

    @srv.tool("get_server", "查询单个 ECS 实例详情",
              {"type": "object", "properties": {
                  "server_id": {"type": "string", "description": "实例 ID"},
              }, "required": ["server_id"]})
    def get_server(args: dict[str, Any]) -> dict[str, Any]:
        sid = args.get("server_id", "")
        for s in servers:
            if s["server_id"] == sid:
                return _make_result(s)
        return {"content": [{"type": "text", "text": json.dumps(
            {"error": f"server {sid} not found"}, ensure_ascii=False)}], "isError": True}

    @srv.tool("start_server", "启动 ECS 实例（SHUTOFF→ACTIVE）",
              {"type": "object", "properties": {
                  "server_id": {"type": "string", "description": "要启动的实例 ID"},
              }, "required": ["server_id"]})
    def start_server(args: dict[str, Any]) -> dict[str, Any]:
        sid = args.get("server_id", "")
        for s in servers:
            if s["server_id"] == sid:
                if s["status"] == "ACTIVE":
                    return _make_result({"ok": False, "message": f"server {sid} is already ACTIVE"})
                s["status"] = "ACTIVE"
                return _make_result({"ok": True, "server_id": sid, "status": "ACTIVE"})
        return {"content": [{"type": "text", "text": json.dumps(
            {"error": f"server {sid} not found"}, ensure_ascii=False)}], "isError": True}

    @srv.tool("stop_server", "停止 ECS 实例（ACTIVE→SHUTOFF）",
              {"type": "object", "properties": {
                  "server_id": {"type": "string", "description": "要停止的实例 ID"},
              }, "required": ["server_id"]})
    def stop_server(args: dict[str, Any]) -> dict[str, Any]:
        sid = args.get("server_id", "")
        for s in servers:
            if s["server_id"] == sid:
                if s["status"] == "SHUTOFF":
                    return _make_result({"ok": False, "message": f"server {sid} is already SHUTOFF"})
                s["status"] = "SHUTOFF"
                return _make_result({"ok": True, "server_id": sid, "status": "SHUTOFF"})
        return {"content": [{"type": "text", "text": json.dumps(
            {"error": f"server {sid} not found"}, ensure_ascii=False)}], "isError": True}

    @srv.tool("create_server", "创建 ECS 实例",
              {"type": "object", "properties": {
                  "name": {"type": "string", "description": "实例名称"},
                  "flavor": {"type": "string", "description": "规格"},
                  "image_id": {"type": "string", "description": "镜像 ID"},
              }, "required": ["name", "flavor", "image_id"]})
    def create_server(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal _next_id
        sid = f"ecs-{_next_id:03d}"
        _next_id += 1
        new_srv = {
            "server_id": sid,
            "name": args["name"],
            "flavor": args["flavor"],
            "image_id": args["image_id"],
            "status": "ACTIVE",
            "vcpus": 1,
            "ram": 1024,
            "addresses": {"private": []},
        }
        servers.append(new_srv)
        return _make_result({"ok": True, "server": new_srv})

    return srv


def main() -> None:
    parser = argparse.ArgumentParser(description="ECS MCP Server (test)")
    parser.add_argument("--port", type=int, default=0, help="监听端口（默认自动分配）")
    args = parser.parse_args()

    srv = make_server(args.port)
    srv.start()
    print(f"ECS MCP server started: {srv.endpoint}")
    print("Press Ctrl+C to stop")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
