"""openapi 模式 server 装配（MCP 协议层）。"""

import argparse
import os

from mcp.server.mcpserver import MCPServer

from apie import mock as apie_mock
from common.auth import credentials as cred_mod
from common.types import (
    ApiDetailResult,
    ApiListResult,
    ExamplesResult,
    ExecuteResult,
    ProductListResult,
    ProductResult,
    ToolError,
)
from safety import policy as safety

from .gate import Gate, load_gate_file
from .service import ServiceConfig, ToolService

INSTRUCTIONS_OPENAPI = """# 华为云 Open MCP 使用指引（OpenAPI 直连模式）

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


def build_instructions(gate: Gate) -> str:
    """按门栓生成 instructions：基础指引 + 产品授权范围。"""
    text = INSTRUCTIONS_OPENAPI + "\n## 产品授权范围\n\n- " + gate.describe() + "。\n"
    if gate.restrict:
        text += ("\n当用户请求的产品不在上述授权范围内时，不要调用任何工具，"
                 "直接回复该产品不在授权范围内。\n")
    return text


def build_openapi_config(args: argparse.Namespace) -> ServiceConfig:
    mock = (args.mock if args.mock is not None
            else os.environ.get("HUAWEICLOUD_MCP_MOCK", "") in ("1", "true", "yes"))
    policy_file = args.policy or os.environ.get("HUAWEICLOUD_MCP_POLICY_FILE")
    region = args.region or os.environ.get("HUAWEICLOUD_MCP_REGION") or None
    mock_base = args.mock_base or os.environ.get("HUAWEICLOUD_MCP_MOCK_BASE") or None
    gate_file = getattr(args, "gate", None) or os.environ.get("HUAWEICLOUD_MCP_OPENAPI_GATE")
    return ServiceConfig(
        region=region or "cn-north-4",
        mock=mock,
        policy_rules=safety.load_policy_file(policy_file) if policy_file else None,
        credentials=None if mock else cred_mod.get_credentials(),
        mock_base=mock_base or apie_mock.MOCK_BASE,
        gate=load_gate_file(gate_file) if gate_file else Gate.unrestricted(),
    )


def build_openapi_app(service: ToolService | None = None, *,
                      log_level: str = "INFO") -> MCPServer:
    svc = service or ToolService()
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=build_instructions(svc.config.gate),
                       log_level=log_level)  # type: ignore[arg-type]

    @server.tool()
    def list_products(category: str | None = None,
                      keyword: str | None = None) -> ProductListResult | ToolError:
        """第一步：列出华为云产品（分类、中文名、接口数、是否全局级服务）。

        基于用户任务语义选择目标产品；不确定时用 keyword 按产品名/中文名搜索。
        选定产品后用 list_apis 浏览其 API 目录。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        当用户请求的产品不在授权范围时，不要调用任何工具，直接回复拒绝。
        """
        return svc.list_products(category=category, keyword=keyword)

    @server.tool()
    def get_product(product: str) -> ProductResult | ToolError:
        """确认单个产品详情（分类/接口数/是否全局级服务）。全局级服务（is_global=true）认证模型不同。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.get_product(product)

    @server.tool()
    def list_apis(product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        """第二步：列出产品的 API 目录。

        结果含 tag_groups（产品全量 tag 概览，不受过滤影响）：先用 tag 收窄目录，
        接口较多时用 search/limit/offset 分页浏览。选定候选接口后用 get_api 读文档。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.list_apis(product, tag=tag, search=search, limit=limit, offset=offset)

    @server.tool()
    def get_api(product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        """第三步：获取接口完整文档（方法/路径/参数必填性/类型/枚举/x-constraint 约束/响应结构）。

        执行前必读；x-constraint 描述调用前置条件与限制。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.get_api(product, api, region=region)

    @server.tool()
    def get_api_examples(product: str, api: str,
                         region: str | None = None) -> ExamplesResult | ToolError:
        """获取接口的官方请求示例（x-request-examples），用于指导参数填写。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.get_api_examples(product, api, region=region)

    @server.tool()
    def execute_api(product: str, api: str, region: str | None = None,
                    params: dict | None = None) -> ExecuteResult:
        """第四步：执行华为云 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。

        params 约定：路径参数/query 参数直接平铺，请求体放 params["body"]。
        mock 模式下 params["_status_code"]/params["_number"] 控制 mock 数据。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.execute_api(product, api, region=region, params=params)

    return server


build_config = build_openapi_config
build_app = build_openapi_app
