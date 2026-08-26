# MCP Server 发现连接机制 —— 设计文档

## 1. 目标

扩展 `huaweicloud-open-mcp` stdio 网关，使 AI 客户端（Claude / opencode 等）能够通过渐进式工作流发现并连接托管在云端的华为云 MCP server，Agent 自主决策连接目标、网关代发调用。

## 2. 运行模式（二选一，完全隔离）

gateway 在两种模式中二选一启动，工具集互斥、配置隔离：

| 模式 | CLI | 工具集 | 协议 |
| --- | --- | --- | --- |
| **openapi**（默认） | `--mode openapi` | 现有 7 工具（Sync） | 元数据直读 + 签名直连华为云 OpenAPI |
| **discover** | `--mode discover` | 新 8 工具（Async） | MCP 发现 + Streamable HTTP 代理连接 |

环境变量 `HUAWEICLOUD_MCP_MODE` 可覆盖 CLI 默认值。

**隔离原则**：
- 工具注册互斥：`build_openapi_app` / `build_discover_app` 各自注册，运行时不会同时存在两组工具
- 配置互斥：`ServiceConfig`（openapi）与 `DiscoverConfig`（discover）互不消费对方字段
- INSTRUCTIONS 按 mode 注入，不混入另一份
- policy 文件统一但规则前缀天然隔离（`product:*` vs `server:*`）

## 3. 架构

```
AI 客户端 ──MCP stdio──▶ huaweicloud-open-mcp (discover mode)
                              │
  ┌─ 固定 8 工具 ─────────────┤
  │ list_mcp_servers           │
  │ get_mcp_server             │
  │ connect_mcp_server    ─────┤──── 目录源 (configs/mcp-server-catalog.example.json)
  │ list_server_tools     ─────┤     ↑ 缓存优先，预留 RemoteCatalogSource 接官方端点
  │ get_server_tool       ─────┤
  │ call_server_tool      ─────┤──── Safety Policy (server:serverId[:tool]=allow|deny)
  │ disconnect_mcp_server ─────┤
  └────────────────────────────┤
                               │── MCP Streamable HTTP client (mcp SDK 2.0)
                               │   → 云端 ECS / VPC / ... MCP Server
```

## 4. 渐进式工作流（7 步）

| 步骤 | 工具 | 职责 | 上下文控制 |
| --- | --- | --- | --- |
| 1 | `list_mcp_servers` | 列出华为云 MCP server 目录（keyword 搜索） | keyword 过滤 |
| 2 | `get_mcp_server` | 获取 server 详情（endpoint/认证/描述） | — |
| 3 | `connect_mcp_server` | 建立连接（过 policy，gateway 侧 Streamable HTTP client） | 仅限 mock 模式可覆盖 endpoint |
| 4 | `list_server_tools` | 目标 server 工具摘要（工具名+首行描述+必填参数名） | limit（默认 20）/offset/search |
| 5 | `get_server_tool` | 单个工具完整 schema（过 16KB 截断） | 只取调用目标一个 |
| 6 | `call_server_tool` | 代发调用（过 policy） | — |
| 7 | `disconnect_mcp_server` | 显式释放连接 | 空闲超时 300s 自动回收 |

步骤 4+5 为"两级读取"模式：摘要精准过滤 → 全文仅读目标工具，防止上下文暴涨（对齐 list_apis/get_api 模式）。

## 5. 目录数据源

```
configs/mcp-server-catalog.example.json   ← 入库，官方端点未就绪时的本地目录
```

Schema：
```json
{
  "id": "@huaweicloud/ecs",
  "name": "ECS MCP Server",
  "display_name": "弹性云服务器 MCP",
  "description": "查询与管理华为云 ECS 实例",
  "category": "计算",
  "endpoint": "https://ecs-mcp.example.com/mcp",
  "transport": "streamable-http",
  "auth": "none",
  "version": "1.0.0"
}
```

- 设计内已预留 `RemoteCatalogSource`，可用 `HUAWEICLOUD_MCP_SERVER_CATALOG_URL` 环境变量切到华为云官方目录端点
- 工具清单不落目录（会过期），由 `list_server_tools` 连接后实时拉取并缓存于 session registry

## 6. Safety Policy 扩展

在现有 `product:apiPattern=allow|deny` 语法基础上追加 server 规则（同文件、保行序）：

```
server:serverId=allow|deny              # 控制 connect_mcp_server
server:serverId:toolPattern=allow|deny  # 控制 call_server_tool（toolPattern fnmatch）
```

- 未配置 policy 时连接与调用全拒（与 execute_api 一致）
- openapi 模式只评估 product 规则；discover 模式只评估 server 规则
- 规则前缀天然隔离，同文件共存不冲突

## 7. 连接生命周期

- 显式 `disconnect_mcp_server` + 空闲超时（默认 300s，`HUAWEICLOUD_MCP_SESSION_IDLE_TIMEOUT`）惰性回收
- 并发会话上限（默认 5，`HUAWEICLOUD_MCP_MAX_SESSIONS`），超限时 LRU 断开
- 无后台线程：回收在 connect/list/call 操作时惰性检查

## 8. MCP 客户端

基于 `mcp==2.0.0` SDK（已是项目依赖）的官方组件，不手写协议：

- `streamable_http_client`：Streamable HTTP 传输（JSON + SSE 原生支持）
- `ClientSession` / `ClientSessionGroup`：会话管理与工具调用
- `mcpdiscover/sdk.py`：薄适配层定义 `SessionClient` 协议（connect/list_tools/call_tool/disconnect），实现类包装 SDK；单元测试注入 fake，集成测试用真 SDK 对本地 stub

## 9. 模块与文件变更

```
新增:
  src/mcp_discover/catalog.py     # CatalogSource 协议 + LocalCatalogSource
  src/mcp_discover/config.py      # DiscoverConfig
  src/mcp_discover/sdk.py         # SessionClient 协议 + 薄适配器
  src/mcp_discover/manager.py     # session registry（空闲超时/LRU/上限）
  src/mcp_discover/service.py     # DiscoverService 编排层
  src/mcp_discover/server.py      # discover mode server 装配
  tests/test_mcpdiscover_catalog.py      # S7a
  tests/test_mcpdiscover_policy.py       # S7b
  tests/test_mcpdiscover_manager.py      # S7c
  tests/test_mcpdiscover_client.py       # S7d 集成
  tests/fixtures/mcp_stub.py             # Streamable HTTP 本地 stub
  configs/mcp-server-catalog.example.json

修改:
  src/common/types.py               # 新 TypedDict 信封
  src/safety/policy.py              # PolicyRule.kind + evaluate_server/check_server
  src/safety/policy_store.py        # PolicyStore 热重载 + manage_policy 共用状态层（新增）
  main.py                           # --mode + build_discover_app + INSTRUCTIONS
  configs/safety-policy.example.json
  AGENTS.md
```

## 10. TDD 接缝（S7）

| 接缝 | 内容 | 测试方式 |
| --- | --- | --- |
| S7a | catalog 加载/搜索/缓存/clear | 纯函数单测，迷你目录 fixture |
| S7b | policy server 规则匹配 | 纯函数单测，手写字面量矩阵 |
| S7c | manager session 注册表 + idle/LRU | 纯函数单测，注入时钟 |
| S7d | SDK 适配层 | fake SessionClient 单测 + 真 SDK + 本地 stub 回环集成 |
| S7e | 8 工具业务函数 + mode 隔离 | 单测注入 catalog/manager/client 工厂 + test_server 工具注册验证 |

## 11. 实施切片（red→green）

| 切片 | 范围 | 测试 |
| --- | --- | --- |
| **1** | types + catalog + list_mcp_servers/get_mcp_server + server mode 隔离 + INSTRUCTIONS | S7a + S7e（部分）+ server mode 测试 |
| **2** | policy server 扩展 + SDK 适配层 + manager + connect/disconnect | S7b + S7c + S7d（单测） |
| **3** | list_server_tools/get_server_tool/call_server_tool + mcp_stub + 集成测试 | S7d（集成）+ S7e |
| **4** | mock 模式接线 + AGENTS/README 同步 + 全量 lint/mypy/coverage | — |