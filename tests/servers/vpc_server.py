"""VPC MCP Server（测试用）：4 个只读工具。

启动: uv run test-vpc-mcp [--port PORT]

工具：
- list_vpcs(limit?)              查询 VPC 列表
- get_vpc(vpc_id)                查询单个 VPC 详情
- list_subnets(vpc_id?)          查询子网列表
- list_security_groups(limit?)   查询安全组列表
"""

import argparse
import json
from typing import Any

from tests.servers.base import BaseMcpHttpServer

VPCS: list[dict[str, Any]] = [
    {
        "vpc_id": "vpc-001", "name": "default-vpc", "cidr": "192.168.0.0/16",
        "status": "ACTIVE", "region": "cn-north-4",
    },
    {
        "vpc_id": "vpc-002", "name": "test-vpc", "cidr": "10.0.0.0/8",
        "status": "ACTIVE", "region": "cn-north-4",
    },
]

SUBNETS: list[dict[str, Any]] = [
    {"subnet_id": "subnet-001", "vpc_id": "vpc-001", "name": "default-subnet",
     "cidr": "192.168.1.0/24", "gateway_ip": "192.168.1.1", "status": "ACTIVE"},
    {"subnet_id": "subnet-002", "vpc_id": "vpc-001", "name": "db-subnet",
     "cidr": "192.168.2.0/24", "gateway_ip": "192.168.2.1", "status": "ACTIVE"},
    {"subnet_id": "subnet-003", "vpc_id": "vpc-002", "name": "test-subnet",
     "cidr": "10.0.1.0/24", "gateway_ip": "10.0.1.1", "status": "ACTIVE"},
]

SECURITY_GROUPS: list[dict[str, Any]] = [
    {"sg_id": "sg-001", "vpc_id": "vpc-001", "name": "default-sg",
     "description": "Default security group", "rules": [
         {"direction": "ingress", "protocol": "tcp", "port": "22", "source": "0.0.0.0/0"},
         {"direction": "ingress", "protocol": "tcp", "port": "443", "source": "0.0.0.0/0"},
     ]},
    {"sg_id": "sg-002", "vpc_id": "vpc-001", "name": "web-sg",
     "description": "Web server security group", "rules": [
         {"direction": "ingress", "protocol": "tcp", "port": "80", "source": "0.0.0.0/0"},
         {"direction": "ingress", "protocol": "tcp", "port": "8080", "source": "0.0.0.0/0"},
     ]},
    {"sg_id": "sg-003", "vpc_id": "vpc-002", "name": "test-sg",
     "description": "Test security group", "rules": []},
]


def make_server(port: int = 0) -> BaseMcpHttpServer:
    srv = BaseMcpHttpServer(name="vpc-mcp", version="1.0.0", port=port)

    def _make_result(data: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}

    @srv.tool("list_vpcs", "查询 VPC 列表",
              {"type": "object", "properties": {
                  "limit": {"type": "integer", "description": "最大返回数量"},
              }})
    def list_vpcs(args: dict[str, Any]) -> dict[str, Any]:
        limit = args.get("limit")
        result = VPCS[:limit] if limit and isinstance(limit, int) and limit > 0 else VPCS
        return _make_result({"vpcs": result, "count": len(result)})

    @srv.tool("get_vpc", "查询单个 VPC 详情",
              {"type": "object", "properties": {
                  "vpc_id": {"type": "string", "description": "VPC ID"},
              }, "required": ["vpc_id"]})
    def get_vpc(args: dict[str, Any]) -> dict[str, Any]:
        vid = args.get("vpc_id", "")
        for vpc in VPCS:
            if vpc["vpc_id"] == vid:
                return _make_result(vpc)
        return {"content": [{"type": "text", "text": json.dumps(
            {"error": f"VPC {vid} not found"}, ensure_ascii=False)}], "isError": True}

    @srv.tool("list_subnets", "查询子网列表，可按 VPC 过滤",
              {"type": "object", "properties": {
                  "vpc_id": {"type": "string", "description": "所属 VPC ID（可选）"},
                  "limit": {"type": "integer", "description": "最大返回数量"},
              }})
    def list_subnets(args: dict[str, Any]) -> dict[str, Any]:
        vpc_id = args.get("vpc_id")
        limit = args.get("limit")
        result = [s for s in SUBNETS if vpc_id is None or s["vpc_id"] == vpc_id]
        if limit and isinstance(limit, int) and limit > 0:
            result = result[:limit]
        return _make_result({"subnets": result, "count": len(result)})

    @srv.tool("list_security_groups", "查询安全组列表",
              {"type": "object", "properties": {
                  "limit": {"type": "integer", "description": "最大返回数量"},
              }})
    def list_security_groups(args: dict[str, Any]) -> dict[str, Any]:
        limit = args.get("limit")
        result = SECURITY_GROUPS[:limit] if limit and isinstance(limit, int) and limit > 0 else SECURITY_GROUPS
        return _make_result({"security_groups": result, "count": len(result)})

    return srv


def main() -> None:
    parser = argparse.ArgumentParser(description="VPC MCP Server (test)")
    parser.add_argument("--port", type=int, default=0, help="监听端口（默认自动分配）")
    args = parser.parse_args()

    srv = make_server(args.port)
    srv.start()
    print(f"VPC MCP server started: {srv.endpoint}")
    print("Press Ctrl+C to stop")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
