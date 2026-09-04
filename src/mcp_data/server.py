"""data 模式 server 装配（MCP 协议层）。

工具集：单 data 模式仅注册 query_data（不注册 manage_policy——query_data
不涉 policy，授予流不存在；混装时 manage_policy 由 openapi/discover 侧提供）。
"""

import argparse
import os

from mcp.server.mcpserver import MCPServer

from common.audit import sink_from_path
from common.types import QueryDataResult, ToolError

from .service import DataConfig, DataService

INSTRUCTIONS_DATA = """# 华为云 Open MCP 使用指引（Data 数据分析模式）

## 工作流

1. `query_data`：对表数据执行只读 SQL 分析（DataFusion 引擎，本地计算）。
   - tables 约定：表名 → {"data": [对象数组]}（inline 小数据）或
     {"path": "文件路径"}（本地文件，按扩展名识别 csv/parquet/jsonl；
     JSON 数组文件请先转存 jsonl）；
   - SQL 严格只读：仅 SELECT/WITH/EXPLAIN/SHOW/DESCRIBE，多语句拒绝；
   - 结果默认返回前 100 行，max_rows 可调（上限 1000），超限标记 truncated；
   - 典型组合（openapi,data 混装）：execute_api 拉取大数据 → 落地文件 →
     query_data 聚合，仅聚合结果进入上下文。

## 安全口径

- query_data 为本地计算工具：不访问云、不需要凭证、不受 safety policy 约束；
- 引擎仅允许只读查询，无文件写副作用；结果体积受行数与字符预算双重截断。
"""


def build_data_config(args: argparse.Namespace) -> DataConfig:
    """从 CLI/env 构建 DataConfig（data 模式无 policy/凭证/mock 语义）。"""
    audit_file = (getattr(args, "audit_file", None)
                  or os.environ.get("HUAWEICLOUD_MCP_AUDIT_FILE"))
    return DataConfig(audit_sink=sink_from_path(audit_file))


def register_data_tools(server: MCPServer, ds: DataService) -> None:
    """注册 data 模式工具（混装装配复用；instructions 由各 builder 自持）。"""

    @server.tool()
    def query_data(sql: str, tables: dict[str, dict] | None = None,
                   max_rows: int | None = None) -> QueryDataResult | ToolError:
        """对临时注册的表执行只读 SQL 分析（DataFusion 引擎，本地计算）。

        tables 约定：表名 → {"data": [对象数组]}（inline 小数据）或
        {"path": "文件路径"}（本地文件，csv/parquet/jsonl 按扩展名识别，
        可显式 format=csv|parquet|ndjson；JSON 数组文件不支持，请转 jsonl）。
        SQL 严格只读（SELECT/WITH/EXPLAIN/SHOW/DESCRIBE），多语句拒绝。
        max_rows 控制返回行数（默认 100，上限 1000），超限标记 truncated。
        本工具不访问云、不需要凭证、不受 safety policy 约束。
        """
        return ds.query_data(sql, tables=tables, max_rows=max_rows)


def build_data_app(config: DataConfig | None = None, *,
                   log_level: str = "INFO") -> MCPServer:
    ds = DataService(config)
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=INSTRUCTIONS_DATA,
                       log_level=log_level)  # type: ignore[arg-type]
    register_data_tools(server, ds)
    return server
