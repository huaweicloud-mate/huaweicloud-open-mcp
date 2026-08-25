# 华为云 Open MCP

本地 stdio 形态的华为云 Open MCP server（通用网关 Core 模式），供 AI 客户端（Claude Code / opencode / Cursor 等）通过自然语言查询与调用华为云服务。

## 运行模式（二选一）

| 模式 | 启动 | 工具数 | 能力 |
| --- | --- | --- | --- |
| **openapi**（默认） | `--mode openapi` | 6 | 元数据直读 → 签名直连华为云 OpenAPI |
| **discover** | `--mode discover` | 7 | 发现目录 → 连接云端 MCP server → 代发调用 |

## openapi 模式（6 工具）

```bash
uv run huaweicloud-open-mcp                        # 真实模式：AK/SK 签名直连华为云
uv run huaweicloud-open-mcp --mock                 # mock 模式：execute_api 指向 mock 端点
```

渐进式工作流：`list_products → get_product → list_apis → get_api → (get_api_examples) → execute_api`

## discover 模式（7 工具）

```bash
uv run huaweicloud-open-mcp --mode discover        # 发现 + 连接云端 MCP server
uv run huaweicloud-open-mcp --mode discover --mock # mock 模式：连接指向本地 stub
```

渐进式工作流：`list_mcp_servers → get_mcp_server → connect_mcp_server → list_server_tools → get_server_tool → call_server_tool → disconnect_mcp_server`

### 发现连接工作流说明

1. **`list_mcp_servers`**：列出华为云 MCP server 目录（中文名/分类/认证模型），用 `keyword` 搜索匹配任务语义
2. **`get_mcp_server`**：确认 server 详情（endpoint/传输层/描述）
3. **`connect_mcp_server`**：建立连接（过 safety policy；真实模式 endpoint 严格取自目录）
4. **`list_server_tools`**：**摘要** 列表（工具名+首行描述+必填参数名），用 `search`/`limit`/`offset` 收窄
5. **`get_server_tool`**：**单个工具完整 schema**（参数类型/枚举/约束），仅取调用目标，防上下文暴涨
6. **`call_server_tool`**：代发调用（过 safety policy）
7. **`disconnect_mcp_server`**：显式释放（空闲 5 分钟自动回收）

步骤 4+5 为"两级读取"：摘要过滤 → 全文只读目标工具，最多消耗一行摘要 + 一个 schema。

### discover 模式配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HUAWEICLOUD_MCP_MODE` | `openapi` | 运行模式 |
| `HUAWEICLOUD_MCP_SERVER_CATALOG` | `configs/mcp-server-catalog.example.json` | MCP server 目录文件路径 |
| `HUAWEICLOUD_MCP_SESSION_IDLE_TIMEOUT` | `300` | 空闲超时（秒），超时自动断开 |
| `HUAWEICLOUD_MCP_MAX_SESSIONS` | `5` | 并发连接上限，超限 LRU 断开 |
| `HUAWEICLOUD_MCP_MOCK_BASE` | — | mock 模式 stub 端点地址 |
| `HUAWEICLOUD_MCP_POLICY_FILE` | — | safety policy 文件路径 |

## safety policy

统一文件，两种规则前缀天然隔离：

```
# openapi 模式
ECS:*List*=allow
*=deny

# discover 模式
server:@huaweicloud/ecs=allow           # connect 级
server:@huaweicloud/ecs:list*=allow     # tool 级
```

未配置 policy 时所有执行/连接/调用全拒。示例见 `configs/safety-policy.example.json`。

## 工具链

- APIE 元数据管道：`api-refresh`（抓取华为云 API Explorer → OpenAPI 2.0 文档）
- 元数据查询 CLI：`api-docs`
- MCP server：`huaweicloud-open-mcp`
- 工作流 benchmark：`uv run python -m benchmarks.runner`（见 `benchmarks/README.md`）

详细设计见 `AGENTS.md`、[docs/architecture.md](docs/architecture.md)（总体）、[docs/mcp-openapi.md](docs/mcp-openapi.md)（openapi 模式）、[docs/mcp-discovery.md](docs/mcp-discovery.md)（discover 模式）。

## 前置依赖

- `uv`（Python 3.10+）
- `uv sync` 安装依赖
-（可选）Swagger 2.0 schema `/tmp/swagger2_schema.json`（校验用，丢失重新下载）