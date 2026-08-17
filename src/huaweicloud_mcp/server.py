"""华为云 MCP server：stdio 装配（mcp SDK）。"""

import argparse
import os

from mcp.server.mcpserver import MCPServer

from .auth import credentials as cred_mod
from .safety import policy as safety
from .signer.client import HttpClient
from .tools import execute, metadata

DEFAULT_REGION = "cn-north-4"


class ServerConfig:
    def __init__(self, mock=False, policy_rules=None, credentials=None, region=None):
        self.mock = mock
        self.policy_rules = policy_rules
        self.credentials = credentials
        self.region = region or DEFAULT_REGION


def build_app(config: ServerConfig) -> MCPServer:
    server = MCPServer(name="huaweicloud-mcp", version="0.1.0")

    def _region(region):
        return region or config.region

    @server.tool()
    def list_products(category: str = None, keyword: str = None) -> dict:
        """列出华为云产品。可按分类或产品名关键词过滤，返回产品及接口数。"""
        groups = metadata.load_products()
        if groups is None:
            return {"ok": False, "reason": "本地元数据缺失，请先运行 api-refresh products"}
        return metadata.list_products(groups, counts=metadata.load_counts(),
                                      category=category, keyword=keyword)

    @server.tool()
    def get_product(product: str) -> dict:
        """查询单个产品的详情（产品名/product_short、分类、接口数、是否全局级服务）。"""
        groups = metadata.load_products()
        if groups is None:
            return {"ok": False, "reason": "本地元数据缺失，请先运行 api-refresh products"}
        out = metadata.get_product(groups, product, counts=metadata.load_counts())
        if out is None:
            return {"ok": False, "reason": f"产品 {product} 未找到"}
        out["ok"] = True
        return out

    @server.tool()
    def list_apis(product: str, tag: str = None, search: str = None,
                  limit: int = 20, offset: int = 0) -> dict:
        """列出产品的 API 目录。按 tag 或关键词（名称/summary）过滤，支持分页。"""
        docs = metadata.load_docs()
        if docs is None:
            return {"ok": False, "reason": "本地接口索引缺失，请先运行 api-refresh docs"}
        out = metadata.list_apis(docs, product, tag=tag, search=search, limit=limit, offset=offset)
        out["ok"] = True
        return out

    @server.tool()
    def get_api(product: str, api: str, region: str = None) -> dict:
        """查询接口详情：方法/路径/参数（必填、类型、枚举、约束）/响应结构/相关模型定义。"""
        hit = metadata.load_api_doc(product, api, _region(region))
        if hit is None:
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        doc, path, method, op = hit
        out = metadata.format_api_detail(doc, product, path, method, op)
        out["ok"] = True
        return out

    @server.tool()
    def get_api_examples(product: str, api: str, region: str = None) -> dict:
        """查询接口的官方请求示例（x-request-examples），用于指导参数填写。"""
        hit = metadata.load_api_doc(product, api, _region(region))
        if hit is None:
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        _, _, _, op = hit
        return {"ok": True, "product": product, "api": api,
                "examples": metadata.extract_examples(op)}

    @server.tool()
    def suggest_apis(task: str, product: str = None, limit: int = 10) -> dict:
        """按任务描述推荐最合适的 API（关键词加权匹配 name/summary/tags）。"""
        docs = metadata.load_docs()
        if docs is None:
            return {"ok": False, "reason": "本地接口索引缺失，请先运行 api-refresh docs"}
        out = metadata.suggest_apis(docs, task, product=product, limit=limit)
        out["ok"] = True
        return out

    @server.tool()
    def execute_api(product: str, api: str, region: str = None, params: dict = None) -> dict:
        """执行华为云 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。

        params 为接口参数 dict：路径参数/query 参数直接平铺，请求体放 params["body"]。
        mock 模式下 params["_status_code"]/params["_number"] 控制 mock 数据。
        """
        region = _region(region)
        hit = metadata.load_api_doc(product, api, region)
        if hit is None:
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        doc, path, method, op = hit
        client = HttpClient(credentials=config.credentials, mock=config.mock)
        return execute.execute_api(
            doc, path, method, op, product, api, region,
            params or {},
            policy_rules=config.policy_rules,
            client=client,
            credentials=config.credentials,
            mock=config.mock,
        )

    return server


def load_policy_from_args(policy_file):
    if policy_file is None:
        return None
    return safety.load_policy_file(policy_file)


def main():
    parser = argparse.ArgumentParser(prog="huaweicloud-mcp", description="华为云 MCP server（stdio）")
    parser.add_argument("--mock", action="store_true", default=None,
                        help="mock 模式：execute_api 指向 API Explorer mock 端点（无需凭证）")
    parser.add_argument("--policy", default=None, help="safety policy 文件路径")
    parser.add_argument("--region", default=None, help="默认 region（默认 cn-north-4）")
    args = parser.parse_args()

    mock = args.mock if args.mock is not None else os.environ.get("HUAWEICLOUD_MCP_MOCK", "") in ("1", "true", "yes")
    policy_file = args.policy or os.environ.get("HUAWEICLOUD_MCP_POLICY_FILE")
    region = args.region or os.environ.get("HUAWEICLOUD_MCP_REGION") or DEFAULT_REGION

    config = ServerConfig(
        mock=mock,
        policy_rules=load_policy_from_args(policy_file),
        credentials=None if mock else cred_mod.get_credentials(),
        region=region,
    )
    if config.policy_rules is None:
        print("提示: 未配置 safety policy，execute_api 将拒绝所有执行（--policy 指定策略文件）",
              file=os.sys.stderr)
    app = build_app(config)
    app.run("stdio")


if __name__ == "__main__":
    main()
