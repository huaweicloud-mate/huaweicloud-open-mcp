# AGENTS.md

## 项目上下文

本项目实现华为云 Open MCP server：本地 stdio 形态的通用网关（Core 模式），openapi 模式用 7 个核心工具、discover 模式用 8 个发现连接工具，触达华为云全量 OpenAPI / 云端 MCP server，供 AI 客户端（Claude Code / opencode / Cursor 等）通过自然语言查询与调用华为云服务。同类产品参考阿里云 OpenAPI MCP Server（Core 模式）与 AWS Labs MCP。

支持两种运行模式（`--mode` 二选一，工具集互斥）：
- **openapi**（默认）：7 工具直连华为云 OpenAPI
- **discover**：8 工具发现连接云端华为云 MCP server，Agent 决策连接目标、gateway 代发调用

三层架构（两模式共用，discover 模式多一层代理连接）：

```text
┌─ MCP 网关层  src/mcp_openapi/ + src/mcp_discover/
│     [openapi]   list_products / get_product / list_apis / get_api /
│                 get_api_examples / execute_api / manage_policy
│     [discover]  list_mcp_servers / get_mcp_server / connect_mcp_server /
│                 list_server_tools / get_server_tool / call_server_tool /
│                 disconnect_mcp_server / manage_policy
│        ↓ execute/call 前强制过 safety policy（manage_policy 热更新策略）
├─ 连接代理层（discover 模式）  src/mcp_discover/
│     catalog.py  目录源（本地文件起步，预留官方端点）
│     sdk.py      SessionClient 协议 + mcp SDK 适配器
│     manager.py  session registry（空闲超时 / LRU 上限）
│     config.py   DiscoverConfig
├─ APIE 元数据层（参考 ../apis 项目重新实现，管道设计保持一致）
│     console.huaweicloud.com/apiexplorer 远端 API Explorer 为唯一数据源
│     ★ 不落盘，纯内存缓存 + 远端实时回退
├─ 执行层
│     src/mcp_openapi/signer/  自实现 SDK-HMAC-SHA256 签名（不依赖官方 SDK）
│     src/mcp_openapi/execute_obs.py + signer/obs.py  OBS 专用执行 lane
│           （OBS HMAC-SHA1 签名 / path-style 桶寻址 / XML body / XML 错误解析）
│     src/common/auth/    凭证加载（AK/SK 环境变量，project_id 自动获取）
│     HTTP 直连华为云；--mock 模式指向 API Explorer mock 端点
└─ 测试层（TDD，见「测试」章节）
```

日志约定：

- 文件为主：默认 `logs/{program}.log`（RotatingFileHandler 10MB×5），stderr 同步 WARNING+；`--log-level`/`--log-file` 或环境变量 `HUAWEICLOUD_MCP_LOG_LEVEL/FILE` 覆盖；`logs/` 不入库。
- stdio 协议安全：MCP server 的 stdout 是 JSON-RPC 通道，任何日志禁止写 stdout。
- 脱敏红线：`Authorization`/`X-Security-Token`/AK/SK 永不入日志；响应 body 仅 DEBUG 且截断。
- 审计：execute_api 的 policy 决策与执行结果记 INFO（`execute {product}:{api} region=.. mode=.. policy=..`）；元数据工具调用记 INFO（`list_products`/`get_product`/`list_apis`/`get_api`/`get_api_examples` + 参数，失败路径 WARNING）。

核心原则：

- **渐进式工作流**：LLM 决策驱动收窄——`list_products` 定产品 → `list_apis`（含 `tag_groups` 全量 tag 概览）定目录 → `get_api` 读文档 → `execute_api` 执行；完整指引写在 server instructions（initialize 响应）与各工具 description。
- **实时回退**：元数据不走本地磁盘，全部通过 API Explorer 远端实时拉取；`apie/memory_store.py` 纯内存缓存（产品列表/API 列表永驻，API 详情 LRU 上限 500）；`catalog.py` 缓存优先→远端回退。
- **TDD**：red→green 垂直切片，一次一个接缝、一个测试、一个最小实现。测试只写在预先确认的接缝（S1–S9）上；只 mock 系统边界（外部 HTTP），不 mock 自有模块；期望值必须来自独立真值（官方签名向量、已知 mock 响应），禁止同义反复断言。写测试前如接缝清单有变，先与用户确认。
- **APIE 管道参考 `../apis` 项目重新实现**：阶段划分、断点续传、tag 映射、Swagger 2.0 校验规则与其保持一致；本仓库为独立实现，不 import apis 代码。
- **安全**：openapi 模式可配产品门栓（`Gate`，产品级白名单）在提示词与元数据层隐藏越界产品；`execute_api` 依次过门栓（产品粗滤）→ safety policy（API 级 allowlist/denylist 模式匹配）；未配置 policy 时拒绝所有执行；凭证必须是最小权限 IAM 用户的 AK/SK。
- **mock 端点**（`https://apiexplorer.cn-north-4.myhuaweicloud.com/v1/mock/<product_short>/<api_name>?status_code=200&number=1&region_id=<region_id>`）：开放端点、无需凭证，用于集成测试与 `--mock` 模式全链路验证。实测行为：HTTP 状态恒为 200；`status_code=200` 返回与真实 API 同构的 mock 成功数据，其它 status_code 返回空 body（错误路径用单元层 urllib 打桩覆盖）。
- 数据产物（`raw/`、`data/`）可从 API 重建，不入库。

## 目录结构与数据流

| 路径 | 内容 | 生成脚本 | 可重建 |
| --- | --- | --- | --- |
| `raw/apis_count.json` | 产品接口计数 | `api-refresh count`（curl） | 是 |
| `raw/huawei_products.json` | 产品信息 | `api-refresh products`（curl） | 是 |
| `raw/apis_docs.json` | 接口索引（id/name/method/summary/tags/product_short/info_version），支撑 `list_apis` | `api-refresh docs` | 是 |
| `raw/apis_detail.json` | 全量接口详情（断点文件 `raw/apis_detail_partial.json`）；非默认 region 在 `raw/{region}/` | `api-refresh details` + `retry` | 是 |
| `data/openapi/` | 管道产物：`{Product}/{Tag}.json` OpenAPI 2.0 文档；MCP server 不依赖此目录 | `api-refresh`（split→convert→merge→organize） | 是 |
| `src/apie/` | APIE 管道实现（fetch/split/convert/merge/organize/refresh/api_docs + http 抓取助手 + mock 端点客户端）+ `memory_store.py` 纯内存缓存 + `catalog.py` 远端优先功能接口（缓存命中直接返回，未命中实时拉取） | — | — |
| `src/mcp_openapi/signer/` | SDK-HMAC-SHA256 签名 + 真实模式 HTTP 客户端（超时/429 退避/错误解析） | — | — |
| `src/mcp_openapi/signer/obs.py` | OBS Header 签名（HMAC-SHA1）：`Authorization: OBS AK:Signature`，CanonicalizedResource/子资源白名单/对象名编码，对齐官方 Go SDK；含预签发 URL 口径 `url_string_to_sign`/`sign_obs_url`（Expires 替换 Date 位） | — | — |
| `src/mcp_openapi/execute_obs.py` | OBS 执行 lane：`is_obs` 路由谓词 + 桶寻址（带桶 virtual-hosted/无桶端点根）+ consumes 三态 body 分流（json/xml/octet-stream）+ 开关型子资源自动补全 + `Content-MD5` 自动计算 + dict→XML 序列化（根元素经转换管线 `x-xml-root` 保留）+ XML `<Error>` 解析 + 二进制响应占位 + `_presign` 预签发编排（零字节搬运）+ `ObsHttpClient` 适配器 + 编排 | — | — |
| `src/common/auth/` | 凭证加载（env/profile，project_id 自动获取） | — | — |
| `src/safety/` | safety policy 解析与匹配（PolicyRule 含 kind=product/server；支持 `product:apiPattern=` 与 `server:serverId[:toolPattern]=` 两种规则前缀）+ `policy_store.py` PolicyStore（策略状态层：文件↔内存双向同步、mtime 热重载、原子落盘，供 manage_policy 与运行时热更新共用） | — | — |
| `src/mcp_openapi/` | openapi 模式（metadata/execute 纯函数 + `gate.py` 产品门栓 + service 编排层 + server 装配；配置/客户端工厂注入；元数据加载委托 apie.catalog） | — | — |
| `src/mcp_discover/` | discover 模式（catalog.py 目录源 + config.py + sdk.py SessionClient 协议 + manager.py session 注册表 + service.py + server.py） | — | — |
| `src/common/types.py` | 跨模块共享类型：ClientResponse/ExecuteResult/ToolError + 六工具结果信封 + MCP discover 结果信封（McpServerItem/*Result） | — | — |
| `src/common/paths.py` | 项目根路径解析（统一 project_root） | — | — |
| `src/common/logconf.py` | 日志配置：文件为主（logs/{program}.log 轮转）+ stderr WARNING+ 兜底 | — | — |
| `main.py` | CLI 入口（按 --mode 分发 openapi/discover 两条路径） | — | — |
| `configs/` | safety policy 示例（含 server 规则）、`openapi-gate.example.json` 产品门栓示例、tag 中文→英文翻译映射、`mcp-server-catalog.example.json` 本地目录 | — | — |
| `tests/` | TDD 测试（见「测试」章节） | — | — |
| `benchmarks/` | LLM Agent 级工作流 benchmark（cases/ 用例、stub_server、scorer/report 纯函数、runner；`results/` 运行产物不入库，`baseline-*.json` 除外） | — | — |

数据流（端到端 openapi 模式）：`API Explorer 远端 → 内存缓存（MemoryStore）→ 元数据工具（get_api 等）→ 门栓过滤（Gate）→ execute_api → safety 检查 → 签名 → 华为云 API（或 mock 端点）`。缓存未命中时自动从 API Explorer 实时拉取并缓存。OBS 产品在 execute_api 处经 `is_obs` 分流到 OBS lane（HMAC-SHA1 签名 + virtual-hosted 桶寻址 + consumes 分流 body + 自动 Content-MD5 + 开关型子资源补全），其余产品走 SDK-HMAC-SHA256 直连。

数据流（end-to-end discover 模式）：`configs/mcp-server-catalog.example.json → list_mcp_servers/get_mcp_server → connect_mcp_server → safety 检查 → Streamable HTTP client（mcp SDK）→ 云端 MCP server → list_server_tools/get_server_tool → call_server_tool → safety 检查 → 代发调用`。

## 模块依赖关系

依赖严格单向、无环，自底向上四层：

```text
第4层  src/main.py          入口，按 --mode 延迟 import 对应 server（避免同时装载两套）
           │
第3层  src/mcp_openapi/       src/mcp_discover/
         ├ service            ├ service
         ├ execute            ├ catalog
         ├ server             ├ config
         └ signer/sign+client ├ manager → sdk
                              └ server
           │                        │
第2层  src/safety/policy       src/apie/
         + policy_store
          （纯函数零依赖；        ├ catalog → live_fallback → convert_openapi2
            store 仅依赖 policy）
                              ├ memory_store（纯内存缓存，零内部依赖）
                              ├ metadata / mock / api_docs(CLI)
                              └ 管道文件（fetch/split/merge/organize/validate/refresh/retry）
           │                        │
第1层  src/common/            （types / http / paths / logconf / auth/credentials，均零内部依赖）
```

| 模块 | 依赖 |
| --- | --- |
| `common/` | 无内部依赖（纯基础设施）；仅 `auth/__init__` re-export `auth.credentials` |
| `safety/policy` | 无依赖（纯函数）；`safety/policy_store` 仅依赖 `safety/policy` |
| `apie/` | → `common`（http/types/paths/logconf）；`catalog`→`live_fallback`→`convert_openapi2` 链、`memory_store`；`mock`→common.http/types |
| `mcp_openapi/` | → `apie`（catalog/metadata/mock/memory_store）+ `safety` + `common`；`service`→`execute`+`execute_obs`+`signer.client`+`signer.obs`；`signer.client`→`signer.sign`；`execute_obs`→`execute`+`signer.obs`+`common` |
| `mcp_discover/` | → `safety` + `common`（**不依赖 apie**）；`service`→`catalog/config/manager/sdk`；`manager`→`sdk` |
| `main.py` | → `common.logconf` + 延迟 import `mcp_openapi.server/service`、`mcp_discover.server` |
| `benchmarks/` | 与 `src/` 零耦合：经子进程 spawn console script `huaweicloud-open-mcp` 驱动，不 import src 任何包 |

关键设计结论：

- **`apie` 是 `mcp_openapi` 独享依赖**：APIE 元数据层只服务 openapi 直连模式；`mcp_discover` 只依赖 `safety` + `common`，两模式互不 import。
- **`safety` + `common` 是两模式公共底座**，二者本身零内部依赖，为最底层可独立复用模块。
- **`metadata.py` 归入 `apie`**：避免 `apie ↔ mcp_openapi` 循环依赖——`api_docs` CLI（apie 内）与 `mcp_openapi/service` 共用同一套 `metadata` 纯函数。
- **`convert_openapi2` 在 apie 顶层**（非管道子目录）：同时被 `live_fallback`（运行时远端回退）与管道文件（离线 refresh）使用。
- **`main.py` 延迟导入**：`mcp_openapi.server` / `mcp_discover.server` 在 `main()` 体内按 mode 分支导入。

## 命名约定

- **产品名**：以 `raw/apis_detail.json` 的驼峰 `product_short` 为准（如 `ECS`）；与 apis 项目的大小写去重映射保持一致。
- **tag 文件名**：英文 PascalCase，中文→英文映射维护在 `configs/tag_translations.json`；`sanitize_tag` 用 `_` 替换空格与 `/`。
- **工具名**：snake_case（`list_products`/`get_product`/`list_apis`/`get_api`/`get_api_examples`/`execute_api`/`manage_policy`）；discover 模式工具（`list_mcp_servers`/`get_mcp_server`/`connect_mcp_server`/`list_server_tools`/`get_server_tool`/`call_server_tool`/`disconnect_mcp_server`/`manage_policy`）。
- **环境变量**：遵循华为云 SDK 惯例——`HUAWEICLOUD_SDK_AK`/`HUAWEICLOUD_SDK_SK`/`HUAWEICLOUD_SDK_SECURITY_TOKEN`/`HUAWEICLOUD_SDK_PROJECT_ID`；MCP 自身配置用 `HUAWEICLOUD_MCP_*` 前缀（如 `HUAWEICLOUD_MCP_MOCK`、`HUAWEICLOUD_MCP_POLICY_FILE`、`HUAWEICLOUD_MCP_OPENAPI_GATE`）。discover 模式新增：`HUAWEICLOUD_MCP_MODE`（运行模式）、`HUAWEICLOUD_MCP_SERVER_CATALOG`（目录文件路径）、`HUAWEICLOUD_MCP_SESSION_IDLE_TIMEOUT`、`HUAWEICLOUD_MCP_MAX_SESSIONS`。
- **region**：默认 `cn-north-4` 平铺，非默认 region 带 `{region}` 目录/后缀（沿用 apis 的 region 目录规则）。

## 构建与运行命令

项目用 uv 管理（`pyproject.toml` + `.venv`）：

```bash
uv sync                                  # 安装依赖（含 dev）
uv run pytest                            # 跑全部测试（默认跳过 e2e）
uv run pytest -m e2e                     # 真实数据/凭证 E2E（需 AK/SK）
uv run pytest --cov=src/common --cov=src/apie --cov=src/safety --cov=src/mcp_openapi --cov=src/mcp_discover  # 覆盖率
uv run ruff check src tests              # lint（ruff，规则 E/F/W/I，line-length 120）
uv run ruff check src tests --fix        # 自动修复可修问题
uv run mypy src                          # 类型检查（全量类型标注）
```

CLI 入口（`pyproject.toml` 注册 console scripts）：

```bash
uv run api-refresh status                # 查看管道各阶段产物状态
uv run api-refresh refresh               # 整链刷新（已存在产物跳过，--force 全重跑）
uv run api-refresh <stage>               # 单步：count/products/docs/details/retry/split/convert/merge/organize/validate
uv run api-refresh details --region cn-south-1
uv run api-docs products                 # 产品列表
uv run api-docs apis ECS --tag 状态管理   # 接口列表
uv run api-docs api ECS ListServersDetails  # 接口详情（OpenAPI 2.0）
uv run api-docs search 云服务器 --product ECS
```

MCP server 启动（stdio，由 MCP 客户端拉起）：

```bash
uv run huaweicloud-open-mcp                    # openapi 真实模式：AK/SK 签名直连华为云（默认）
uv run huaweicloud-open-mcp --mock             # mock 模式：execute_api 指向 API Explorer mock 端点（无需凭证）
uv run huaweicloud-open-mcp --mode discover    # discover 模式：发现连接云端 MCP server（环境变量 HUAWEICLOUD_MCP_MODE）
uv run huaweicloud-open-mcp --mock-base http://127.0.0.1:8000  # 自定义 mock 端点基础地址（benchmark 本地 stub 用；环境变量 HUAWEICLOUD_MCP_MOCK_BASE）
uv run huaweicloud-open-mcp --policy configs/safety-policy.example.json  # 指定 safety policy 文件
uv run huaweicloud-open-mcp --log-level DEBUG  # 日志级别（默认 INFO）；--log-file 指定文件（默认 logs/huaweicloud-open-mcp.log）
```

工作流 benchmark（LLM Agent 级，`opencode run` 驱动，评估精度/耗时/token）：

```bash
uv run python -m benchmarks.runner --dry-run                  # 只校验用例
uv run python -m benchmarks.runner --backend stub --repeat 3  # stub 后端（确定性，默认）；real 为真实 mock 端点
uv run python -m benchmarks.runner --baseline-save            # 保存基线到 benchmarks/results/baseline-{backend}.json
uv run python -m benchmarks.runner --baseline-compare --fail-on-regression  # 对比基线，pass 率回退退出码 3
```

benchmark 设计见 `benchmarks/README.md`（用例 schema、分层评分口径、spike 结论）。

前置依赖：`uv`；Swagger 2.0 schema 文件 `/tmp/swagger2_schema.json`（`curl -sL https://raw.githubusercontent.com/OAI/OpenAPI-Specification/main/_archive_/schemas/v2.0/schema.json`），丢失后重新下载。

## 测试（TDD）

测试接缝（已确认；变更需先与用户重新确认）：

| 接缝 | 内容 | 测试方式 | 独立真值 |
| --- | --- | --- | --- |
| S1 | `signer.sign(request) → Authorization 头` | 纯函数单测 | 华为云官方签名文档示例向量（先收集，不自行推导） |
| S2 | `safety.evaluate(policy, product, api) → allow/deny` | 纯函数单测 | 手写策略文件 + 预期字面量 |
| S2b | `safety/policy_store.py` PolicyStore（rules 热重载 / add_rule / remove_rule / text，文件↔内存双向同步） | 单测：tmp 文件注入 + 内容哈希 stat 替身；服务层「拒→add→同实例立即放行」 | 直接回读磁盘原始内容 + `parse_policy` 交叉验证 |
| S3 | 各工具业务函数 `mcp_openapi.service` / `apie.metadata`（含 manage_policy 编排） | 单测，迷你样本 fixture | 自建迷你 OpenAPI 片段（仿 apis fixtures 设计，不依赖真实 raw/ data/） |
| S4 | `execute_api` HTTP 边界 | 集成测试直连 mock 端点 + 单元层 urllib 打桩注入错误（429/4xx/5xx） | mock 端点返回（HTTP 恒 200；`status_code` 非 200 返回空 body） |
| S5 | APIE 管道各阶段转换 + `apie.memory_store` 内存缓存层（set/get/clear/LRU）+ `apie.catalog` 功能接口（内存缓存优先→远端回退决策，monkeypatch `apie.http.fetch_json` 边界） | 纯函数单测 + 迷你样本集成 + `@pytest.mark.e2e` 全量 | Swagger 2.0 schema 校验；monkeypatch 注入 HTTP 响应控制远端回退路径 |
| S6 | benchmark 纯函数（`benchmarks/cases.py` 加载校验、`scorer.py` 分层评分、`report.py` 统计/基线对比、`trace.py` export/NDJSON 提取 + export JSON info 的 token 读取、`stub_server.py` 本地回环） | 纯函数单测（trace 用 spike 实测格式的迷你 fixture；stub 用回环 HTTP） | 手写字面量 + 独立构造的样例调用序列 |
| S7a | `mcp_discover/catalog.py` 目录加载/搜索/缓存/clear | 纯函数单测，迷你目录 fixture + 注入 CatalogSource | 文件系统状态变化（删除文件后仍缓存命中） |
| S7b | `safety.evaluate_server(policy, server, tool) → allow/deny` | 纯函数单测，手写字面量矩阵（含向后兼容 product 规则） | 手写策略文件 + 预期字面量 |
| S7c | `mcp_discover/manager.py` session 注册表 + idle 回收 + LRU | 纯函数单测，注入时钟 | 手写字面量 |
| S7d | `mcp_discover/sdk.py` MCP client 适配层 | fake SessionClient 单测 + 真 mcp SDK + 本地 stub 回环集成 | stub 返回确定性 JSON-RPC 响应 |
| S7e | discover 工具业务函数 + mode 隔离注册（8 工具） | 单测注入 catalog/manager/client 工厂 + server 工具注册验证 | 字面量 + 互斥工具集合断言 |
| S8 | `mcp_openapi/gate.py` 产品门栓（parse/allows/filter_products/describe/load）+ service 门栓过滤/拒绝 + server instructions/docstring 注入范围 | 纯函数单测 + service 注入 gate + server 装配断言 | 手写字面量 + 门栓示例配置 |
| S9a | `mcp_openapi/signer/obs.py` OBS HMAC-SHA1 签名（StringToSign/CanonicalizedResource/CanonicalizedOBSHeaders/Signature） | 纯函数单测 | 官方文档「Header 中携带签名」StringToSign 构造示例（表4/6/7）+ openssl 按官方公式计算的签名值 |
| S9b | dict→OBS XML 序列化（`serialize_body_xml`，含 `xml.name` 根元素、`$ref` 嵌套、数组、转义） | 纯函数单测，迷你 schema fixture | 手写期望 XML 字面量 |
| S9c | 桶/对象寻址（`build_obs_request` 参数切分 + `build_obs_url` path-style URL） | 纯函数单测 | 手写 URL/参数字面量 |
| S9d | OBS XML `<Error>` 解析（`parse_obs_error`） | 纯函数单测 | 手写 `<Error>` 片段 |
| S9e | `is_obs` 路由谓词 + `execute_obs_api` 编排 + `ObsHttpClient` 签名发送 + `service` OBS 分派 | 单测注入 OBS client 工厂 | 手写字面量 + 注入 client |
| S9f | 预签发 URL（`signer.obs.url_string_to_sign` / `sign_obs_url` / `build_presign_base` + `execute_presign_api` 编排 + service `_presign` 分派与热更新协同） | 纯函数单测 + service 注入凭证 | 官方《URL中携带签名》表4/5 结构原文 + openssl 口径金标 |

分层与纪律：

- **单元测试**（`tests/test_signer.py`、`tests/test_obs_signer.py`、`tests/test_obs_execute.py`、`tests/test_safety.py`、`tests/test_tools_*.py`、`tests/test_apie_*.py`、`tests/test_service.py`、`tests/test_client.py`、`tests/test_gate.py`、`tests/test_bench_*.py`、`tests/test_mcp_discover_*.py`）：纯函数，不联网、不碰真实数据；service 层用 monkeypatch HTTP 注入 + 客户端工厂注入。
- **集成测试**（`tests/test_execute_mock.py`：直连 mock 端点，覆盖正常响应与错误注入；mock 模式下跳过签名；`tests/test_execute_mcp_mock.py`：真 mcp SDK client → 本地 MCP stub 回环 HTTP，覆盖 Streamable HTTP 协议全链路）。
- **E2E 测试**（`tests/test_e2e.py`：真实 AK/SK 只读调用；`tests/test_obs_e2e.py`：OBS 只读 ListBuckets + 错误签名拒绝；`tests/test_workflow_e2e.py`：openapi 渐进式工作流全链，真实 API Explorer 远端数据；`tests/test_workflow_obs_e2e.py`：OBS 全链路 real 模式，元数据 live 回退 + 临时桶自清理式写链路（CreateBucket/SetBucketTagging/PutObject 文本/GetObject 读回/_presign 预签发 URL 客户端直连 PUT-GET-过期负路径，finally 删除对象/标签/桶）；`tests/test_workflow_discover_e2e.py`：discover 渐进式工作流全链，mock 模式 + 本地 MCP stub 回环，无外网依赖）：标 `@pytest.mark.e2e` 默认跳过；凭证优先读环境变量，缺省时自动从项目根 `.env` 加载（`conftest.py` 最小加载器，已存在的环境变量不覆盖；`.env` 已 gitignore，禁止提交）。E2E 红线为「只读 + 自清理临时资源」——写操作仅允许走带唯一前缀的临时桶并在 finally 清理。。
- red→green 垂直切片，禁止先写全部测试再写实现；禁止 mock 自有模块；期望值禁止用被测代码同法重算。

## 校验规则（必须满足）

- `data/openapi/` 全部文档通过 Swagger 2.0 schema 校验（valid 0 invalid）；转换修复规则与 apis 项目一致（consumes 字符串→数组、components→definitions、path 参数 required、3.0 字段清理、enum 去重等）。
- 签名实现必须通过官方文档测试向量；不得自行推导期望签名值。OBS 用 `Authorization: OBS AK:Signature`（HMAC-SHA1），StringToSign 结构对齐官方文档「Header中携带签名」与官方 Go SDK（obs/authV2.go），子资源白名单以官方 SDK `allowedResourceParameterNames` 为准；virtual-hosted 寻址下 CanonicalizedResource 桶名后恒带 `/`。
- OBS 执行 lane 约定：带桶 virtual-hosted、无桶端点根（URL 恒带根路径 `/`）；body 按 op consumes 分流 json/xml/octet-stream；XML 根元素经转换管线把 `xml.name` 提升为 `x-xml-root` 保留并注入官方命名空间；带 body 的写请求自动补 `Content-MD5`；元数据中 required 空值型子资源（tagging/acl/lifecycle 等开关）缺失时自动补 `""`；成功响应透出白名单头（ETag/x-obs-request-id 等）。
- OBS 预签发 URL（`_presign`，S9f）：大文件上传/下载不经 gateway —— params 传 `_presign=true` 时 gate/policy 判定后仅签发访问 URL（`presign.url/method/expires_in`），客户端直连 OBS 收发字节，部署拓扑无关且不限大小；`_presign_expires` 相对秒数默认 900（换算 epoch 入签名 Date 位）、`_presign_content_type` 可锁定 PUT 类型；StringToSign 与 Header 方式唯一差异为 Expires 替换 Date 位，auth 三参数（AccessKeyId/Expires/Signature）不入签，签名值 RFC3986 严格编码；独立真值为官方《URL中携带签名》表4/5 结构原文 + openssl 口径金标；非 OBS 产品传 `_presign` 显式拒绝；二进制 GetObject 不带 `_presign` 时仍返回占位摘要；进度归客户端宿主能力，gateway 无感知。
- `execute_api` 响应规范化：错误统一转为结构化输出（`error_code`/`error_msg`/HTTP 状态），429 退避重试，响应体积超限截断。
- safety policy 匹配：按文件行序首个命中生效，`product:apiPattern=allow|deny`；MCP discover 扩展 `server:serverId[:toolPattern]=allow|deny`；无匹配默认 deny；无 policy 文件时 execute_api/call_server_tool 全拒。
- safety policy 热更新（`PolicyStore` + `manage_policy`）：策略文件为唯一真值源，运行期按 stat（mtime/size/inode）热重载，外部编辑即时生效、无需重启；`manage_policy(action=list/add/remove)` 两模式同构，add/remove 先校验再 `tmp+os.replace` 原子写盘并刷新内存，静止态 memory==file；新 allow 规则自动插到首个会遮蔽它的 deny 规则之前（典型即 `*=deny` 兜底行前）；语义重复 add 幂等不改盘；文件被写坏/短暂消失时沿用最近合法版本记 WARNING，恢复后自动重新采纳；未配置 --policy 时 manage_policy 拒绝且不创建文件；启动时急切加载保留坏文件快速失败。
- 产品门栓（`Gate`）：openapi 模式产品级白名单，未配置时不限制；配置后未列出产品默认拒；越界产品在 `list_products` 静默隐藏，其余工具返回「不在 openapi mcp 授权范围内」；`execute_api` 先过门栓再过 policy。

## 文档维护

修改脚本行为时同步更新本文件：

- 数据流、脚本输入输出、默认路径变化 → 更新「目录结构与数据流」「构建与运行命令」。
- 工具清单、工具输入输出契约变化 → 更新「项目上下文」「命名约定」。
- 接缝清单、测试分层变化 → 更新「测试（TDD）」。
- 转换/校验/安全规则变化 → 更新「校验规则」。
- `pyproject.toml` 依赖、CLI 入口变化 → 更新「构建与运行命令」。

## AGENTS.md 刷新条件

以下变化必须同步更新本文件：

- APIE 管道阶段增删，或产物路径/命名规则变化。
- MCP 工具增删或输入输出契约变化（含 discover 模式工具）。
- safety policy 语法、默认行为（无 policy 时拒绝/放行）变化（含 server 规则）。
- 产品门栓（`Gate`）默认语义（未配置时限制/不限制）或门控范围变化。
- 测试接缝（S1–S9）增删或重新确认。
- `pyproject.toml` 依赖或 CLI 入口变化（`api-refresh`/`api-docs`/`huaweicloud-open-mcp`）。
- mock 端点地址或 `--mock`/`--mode` 模式行为变化（含 `--mock-base`）。
- benchmark 用例 schema、评分口径、runner 参数变化 → 同步 `benchmarks/README.md`。
- discover 连接代理层模块（`mcp_discover/`）或目录数据源（`configs/mcp-server-catalog.example.json`）变化。

以下变化通常不需要更新：

- 只改日志文案、退避时长等不影响数据流的行为。
- 底层 HTTP 抓取细节（分页大小等），只要最终产物结构不变。
