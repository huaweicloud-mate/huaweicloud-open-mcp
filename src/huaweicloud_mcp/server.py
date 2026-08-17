"""华为云 MCP server：stdio 装配（mcp SDK）。

只做 MCP 协议装配，全部业务逻辑委托 ToolService。
"""

import argparse
import os
import sys

from mcp.server.mcpserver import MCPServer

from .auth import credentials as cred_mod
from .safety import policy as safety
from .tools.service import ServiceConfig, ToolService
from .types import (
    ApiDetailResult,
    ApiListResult,
    ExamplesResult,
    ExecuteResult,
    ProductListResult,
    ProductResult,
    ToolError,
)

INSTRUCTIONS = """# 华为云 MCP 使用指引

## 推荐工作流（渐进收窄，LLM 决策）

1. `list_products`：获取产品列表（含中文名/分类/接口数），基于用户任务语义确定产品范围；
2. `list_apis`：获取选定产品的 API 目录；返回结果含 `tag_groups` 全量 tag 概览，
   先用 `tag` 参数收窄目录，接口较多时配合 `search`/`limit`/`offset` 分页浏览；
3. `get_api`：确定候选接口后，调用前**必读**接口文档（必填参数、类型、枚举、x-constraint 约束）；
4. `get_api_examples`：官方请求示例，用于指导参数填写（可选）；
5. `execute_api`：执行接口。参数约定：路径/query 参数平铺在 params，请求体放 params["body"]。

## 执行安全

- `execute_api` 执行前强制过 safety policy（allowlist/denylist 白名单）；
- 未配置 policy 时所有执行被拒绝；
- 拒绝结果形如 {"ok": false, "reason": ...}，不要绕过，应改用被允许的接口或询问用户。

## 其它

- region 默认 cn-north-4；非默认 region 需显式传 region 参数；
- 产品 `is_global` 为 true 的全局级服务（如 IAM）与地域级服务认证模型不同。
"""


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
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0", instructions=INSTRUCTIONS)

    @server.tool()
    def list_products(category: str | None = None, keyword: str | None = None) -> ProductListResult | ToolError:
        """第一步：列出华为云产品（分类、中文名、接口数、是否全局级服务）。

        基于用户任务语义选择目标产品；不确定时用 keyword 按产品名/中文名搜索。
        选定产品后用 list_apis 浏览其 API 目录。
        """
        return service.list_products(category=category, keyword=keyword)

    @server.tool()
    def get_product(product: str) -> ProductResult | ToolError:
        """确认单个产品详情（分类/接口数/是否全局级服务）。全局级服务（is_global=true）认证模型不同。"""
        return service.get_product(product)

    @server.tool()
    def list_apis(product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        """第二步：列出产品的 API 目录。

        结果含 tag_groups（产品全量 tag 概览，不受过滤影响）：先用 tag 收窄目录，
        接口较多时用 search/limit/offset 分页浏览。选定候选接口后用 get_api 读文档。
        """
        return service.list_apis(product, tag=tag, search=search, limit=limit, offset=offset)

    @server.tool()
    def get_api(product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        """第三步：获取接口完整文档（方法/路径/参数必填性/类型/枚举/x-constraint 约束/响应结构）。

        执行前必读；x-constraint 描述调用前置条件与限制。
        """
        return service.get_api(product, api, region=region)

    @server.tool()
    def get_api_examples(product: str, api: str, region: str | None = None) -> ExamplesResult | ToolError:
        """获取接口的官方请求示例（x-request-examples），用于指导参数填写。"""
        return service.get_api_examples(product, api, region=region)

    @server.tool()
    def execute_api(product: str, api: str, region: str | None = None,
                    params: dict | None = None) -> ExecuteResult:
        """第四步：执行华为云 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。

        params 约定：路径参数/query 参数直接平铺，请求体放 params["body"]。
        mock 模式下 params["_status_code"]/params["_number"] 控制 mock 数据。
        """
        return service.execute_api(product, api, region=region, params=params)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(prog="huaweicloud-open-mcp", description="华为云 MCP server（stdio）")
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
