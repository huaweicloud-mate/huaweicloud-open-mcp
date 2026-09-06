"""data 模式 server 装配（MCP 协议层）。

工具集：单 data 模式仅注册 query_data（不注册 manage_policy——query_data
不涉 policy，授予流不存在；混装时 manage_policy 由 openapi/discover 侧提供）。
"""

import argparse
import os

from mcp.server.mcpserver import MCPServer

from common.audit import sink_from_path
from common.types import QueryDataResult, ToolError, TransformDataResult

from .service import DataConfig, DataService

INSTRUCTIONS_DATA = """# 华为云 Open MCP 使用指引（Data 数据分析模式）

## 工作流

1. `query_data`：对表数据执行只读 SQL 分析（DataFusion 引擎，本地计算）。
   - tables 约定：表名 → {"data": [对象数组]}（inline 小数据）或
     {"path": "文件路径"}（本地文件，按扩展名识别 csv/parquet/jsonl/json 数组；
     超大 json 数组建议转 jsonl）；
   - SQL 严格只读：仅 SELECT/WITH/EXPLAIN/SHOW/DESCRIBE，多语句拒绝；
   - 结果默认返回前 100 行，max_rows 可调（上限 1000），超限标记 truncated；
   - 典型组合（openapi,data 混装）：execute_api 拉取大数据 → 落地文件 →
     query_data 聚合，仅聚合结果进入上下文。
2. `transform_data`：把只读 SQL 变换结果落盘为新数据文件（转换/清洗/格式互转）。
   - out 约定：{"path": "输出文件路径", "format"?: "csv|parquet|ndjson"}，
     格式按扩展名自动识别、显式 format 可覆盖；目标已存在默认拒绝，
     显式 overwrite=true 才覆盖；
   - SQL 同样严格只读（写动作由引擎施加，SQL 无写语法可达）；
   - 返回产物元数据（path/format/rows/bytes）+ 小预览（默认 10 行），
     大结果不进上下文；
   - 典型场景：格式互转（json→parquet 等）、列裁剪/重命名/去重/join 后
     落盘新数据集。

## 安全口径

- query_data/transform_data 为本地计算工具：不访问云、不需要凭证、
  不受 safety policy 约束；
- 引擎仅允许只读查询，写盘仅经 transform_data 的结构化 out 参数
  （审计 NDJSON 记录写路径），无 SQL 级写语法可达；
- 结果体积受行数与字符预算双重截断（transform_data 另受预览行数上限）。
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
        .json 数组直接可用，可显式 format=csv|parquet|ndjson|json）。
        SQL 严格只读（SELECT/WITH/EXPLAIN/SHOW/DESCRIBE），多语句拒绝。
        max_rows 控制返回行数（默认 100，上限 1000），超限标记 truncated。
        本工具不访问云、不需要凭证、不受 safety policy 约束。
        """
        return ds.query_data(sql, tables=tables, max_rows=max_rows)

    @server.tool()
    def transform_data(sql: str, out: dict, tables: dict[str, dict] | None = None,
                       overwrite: bool = False,
                       preview_rows: int | None = None) -> TransformDataResult | ToolError:
        """把只读 SQL 变换结果落盘为新数据文件（转换/清洗/格式互转，本地计算）。

        out 约定：{"path": "输出文件路径", "format"?: "csv|parquet|ndjson"}——
        格式按扩展名自动识别，显式 format 可覆盖；目标已存在默认拒绝，
        显式 overwrite=true 才覆盖（原子落盘，失败不留半截文件）。
        tables 约定同 query_data；SQL 同样严格只读（写动作由引擎施加，
        SQL 无写语法可达）；SQL 执行两遍（预览+写盘），勿用 random 等易变函数。
        返回产物元数据（path/format/rows/bytes）+ 小预览（preview_rows 默认 10，
        上限 100），大结果不进上下文。本工具不访问云、不需要凭证、
        不受 safety policy 约束（写路径经审计 NDJSON 记录）。
        """
        return ds.transform_data(sql, tables=tables, out=out, overwrite=overwrite,
                                 preview_rows=preview_rows)


def build_data_app(config: DataConfig | None = None, *,
                   log_level: str = "INFO") -> MCPServer:
    ds = DataService(config)
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=INSTRUCTIONS_DATA,
                       log_level=log_level)  # type: ignore[arg-type]
    register_data_tools(server, ds)
    return server
