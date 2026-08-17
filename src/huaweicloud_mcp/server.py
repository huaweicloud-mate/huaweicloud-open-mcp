"""华为云 MCP server：stdio 装配（mcp SDK）。

只做 MCP 协议装配，全部业务逻辑委托 ToolService。
"""

import argparse
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from .auth import credentials as cred_mod
from .safety import policy as safety
from .tools.service import ServiceConfig, ToolService
from .types import ExecuteResult


def build_config(args: argparse.Namespace) -> ServiceConfig:
    mock = (args.mock if args.mock is not None
            else os.environ.get("HUAWEICLOUD_MCP_MOCK", "") in ("1", "true", "yes"))
    policy_file = args.policy or os.environ.get("HUAWEICLOUD_MCP_POLICY_FILE")
    region = args.region or os.environ.get("HUAWEICLOUD_MCP_REGION") or None
    return ServiceConfig(
        region=region or "cn-north-4",
        mock=mock,
        policy_rules=safety.load_policy_file(policy_file) if policy_file else None,
        credentials=None if mock else cred_mod.get_credentials(),
    )


def build_app(service: ToolService | None = None) -> MCPServer:
    service = service or ToolService()
    server = MCPServer(name="huaweicloud-mcp", version="0.1.0")

    @server.tool()
    def list_products(category: str | None = None, keyword: str | None = None) -> dict[str, Any]:
        """列出华为云产品。可按分类或产品名关键词过滤，返回产品及接口数。"""
        return service.list_products(category=category, keyword=keyword)

    @server.tool()
    def get_product(product: str) -> dict[str, Any]:
        """查询单个产品的详情（产品名/product_short、分类、接口数、是否全局级服务）。"""
        return service.get_product(product)

    @server.tool()
    def list_apis(product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """列出产品的 API 目录。按 tag 或关键词（名称/summary）过滤，支持分页。"""
        return service.list_apis(product, tag=tag, search=search, limit=limit, offset=offset)

    @server.tool()
    def get_api(product: str, api: str, region: str | None = None) -> dict[str, Any]:
        """查询接口详情：方法/路径/参数（必填、类型、枚举、约束）/响应结构/相关模型定义。"""
        return service.get_api(product, api, region=region)

    @server.tool()
    def get_api_examples(product: str, api: str, region: str | None = None) -> dict[str, Any]:
        """查询接口的官方请求示例（x-request-examples），用于指导参数填写。"""
        return service.get_api_examples(product, api, region=region)

    @server.tool()
    def suggest_apis(task: str, product: str | None = None, limit: int = 10) -> dict[str, Any]:
        """按任务描述推荐最合适的 API（关键词加权匹配 name/summary/tags）。"""
        return service.suggest_apis(task, product=product, limit=limit)

    @server.tool()
    def execute_api(product: str, api: str, region: str | None = None,
                    params: dict[str, Any] | None = None) -> ExecuteResult:
        """执行华为云 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。

        params 为接口参数 dict：路径参数/query 参数直接平铺，请求体放 params["body"]。
        mock 模式下 params["_status_code"]/params["_number"] 控制 mock 数据。
        """
        return service.execute_api(product, api, region=region, params=params)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(prog="huaweicloud-mcp", description="华为云 MCP server（stdio）")
    parser.add_argument("--mock", action="store_true", default=None,
                        help="mock 模式：execute_api 指向 API Explorer mock 端点（无需凭证）")
    parser.add_argument("--policy", default=None, help="safety policy 文件路径")
    parser.add_argument("--region", default=None, help="默认 region（默认 cn-north-4）")
    args = parser.parse_args()

    config = build_config(args)
    if config.policy_rules is None:
        print("提示: 未配置 safety policy，execute_api 将拒绝所有执行（--policy 指定策略文件）",
              file=sys.stderr)
    app = build_app(ToolService(config))
    app.run("stdio")


if __name__ == "__main__":
    main()
