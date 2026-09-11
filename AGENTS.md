# AGENTS.md

## 项目上下文

本项目实现华为云 Open MCP server：本地 stdio 形态的通用网关（Core 模式），openapi 模式用 8 个核心工具、discover 模式用 8 个发现连接工具、data 模式用 2 个本地数据工具（DataFusion 只读 SQL 分析与转换落盘），触达华为云全量 OpenAPI / 云端 MCP server / 本地数据，供 AI 客户端（Claude Code / opencode / Cursor 等）通过自然语言查询、调用、转换与分析华为云服务。同类产品参考阿里云 OpenAPI MCP Server（Core 模式）与 AWS Labs MCP。

支持三种运行模式（`--mode` 可逗号组合混用，单 server 同时注册所选模式工具集）：
- **openapi**（默认）：8 工具直连华为云 OpenAPI
- **discover**：8 工具发现连接云端华为云 MCP server，Agent 决策连接目标、gateway 代发调用
- **data**：2 工具 `query_data`（DataFusion 只读 SQL 聚合分析）+ `transform_data`（转换落盘：SQL 变换结果写 csv/parquet/jsonl，原子落盘+覆盖门+小预览；均不访问云、不需要凭证、不受 safety policy 约束）。典型闭环（openapi,data 混装）：execute_api 拉大数据 → 落地文件 → query_data 聚合 / transform_data 整形落盘，仅聚合或元数据+预览进上下文

三层架构（模式共用，discover 模式多一层代理连接，data 模式独立于云侧执行层）：

```text
┌─ MCP 网关层  src/mcp_openapi/ + src/mcp_discover/ + src/mcp_data/
│     [openapi]   search_apis / list_products / get_product / list_apis /
│                 get_api / get_api_examples / execute_api / manage_policy
│     [discover]  list_mcp_servers / get_mcp_server / connect_mcp_server /
│                 list_server_tools / get_server_tool / call_server_tool /
│                 disconnect_mcp_server / manage_policy
│     [data]      query_data（DataFusion 只读 SQL 聚合分析）+ transform_data（转换落盘：
│                 原子写盘+覆盖门+小预览；不经 gate/policy，无 manage_policy——
│                 混装时 manage_policy 由 openapi/discover 侧提供且全局只注册一次）
│        ↓ openapi/discover 工具 execute/call 前强制过 safety policy；拒绝时经 elicitation 提议授予 / manage_policy 前 elicitation 确认（热更新策略）
│        ↓ data 工具无 policy 门（口径见「校验规则」）；仅只读守卫 + 双重截断
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
│           （OBS HMAC-SHA1 签名 / virtual-hosted 桶寻址 / XML body / XML 错误解析 /
│             对象数据面 presign 单口径）
│     src/common/auth/    凭证加载（AK/SK 环境变量，project_id 自动获取）
│     HTTP 直连华为云；--mock 模式指向 API Explorer mock 端点
└─ 测试层（TDD，见「测试」章节）
```

日志约定：

- 文件为主：默认 `logs/{program}.log`（RotatingFileHandler 10MB×5），stderr 同步 WARNING+；logging 挂 root logger 接管全部模块命名空间（main/common.*/safety.*/mcp_openapi.*、mcp_discover.*/apie.* 等），三方噪音库（httpx/httpcore）固定 WARNING；`--log-level`/`--log-file` 或环境变量 `HUAWEICLOUD_MCP_LOG_LEVEL/FILE` 覆盖；`logs/` 不入库。
- stdio 协议安全：MCP server 的 stdout 是 JSON-RPC 通道，任何日志禁止写 stdout。
- 脱敏红线：`Authorization`/`X-Security-Token`/AK/SK 永不入日志；响应 body 仅 DEBUG 且截断。
- 审计：execute_api 的 policy 决策与执行结果记 INFO（`execute {product}:{api} region=.. mode=.. policy=..`）；元数据工具调用记 INFO（`list_products`/`get_product`/`list_apis`/`get_api`/`get_api_examples` + 参数，失败路径 WARNING）；data 模式 query_data 记 INFO（`query_data tables=.. max_rows=.. sql=..` 前 120 字符，失败路径 WARNING）。

核心原则：

- **渐进式工作流**：LLM 决策驱动收窄——意图未指明产品时先 `search_apis`（实体图谱跨产品检索，返回候选产品 + 代表 API + matched_via 证据，S15）→ `list_products` 定产品 → `list_apis`（含 `tag_groups` 全量 tag 概览）定目录 → `get_api` 读文档 → `execute_api` 执行；完整指引写在 server instructions（initialize 响应）与各工具 description。`list_products`/`get_product` 输出不含 `api_count`（远端 v5/products 该字段恒 0，2026-09 删除；产品级计数非有效信号，规模信息由 `list_apis` 的 `total`/`tag_groups` 承担）；`apie/catalog.get_api_counts` 已随之删除，管道 count 阶段（`raw/apis_count.json`，`fetch_apis` 翻页依赖）不受影响。
- **实时回退**：元数据不走本地磁盘，全部通过 API Explorer 远端实时拉取；`apie/memory_store.py` 纯内存缓存（产品列表/API 列表永驻，API 详情 LRU 上限 500）；`catalog.py` 缓存优先→远端回退。
- **TDD**：red→green 垂直切片，一次一个接缝、一个测试、一个最小实现。测试只写在预先确认的接缝（S1–S13）上；只 mock 系统边界（外部 HTTP），不 mock 自有模块；期望值必须来自独立真值（官方签名向量、已知 mock 响应），禁止同义反复断言。写测试前如接缝清单有变，先与用户确认。
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
| `raw/help_docs.json` | 帮助中心文档页解析记录（url/docset/api_name/cn_name/product_display/func_intro/links；仅构建期，不入库） | `api-refresh helpdocs`（断点 `raw/help_docs_partial.json`） | 是 |
| `data/help_completions/` | 补全列表 `help_completions.json`（仅差集条目：product/api/doc_url/detail_desc/help_intro/matched_by）+ `report.json`（匹配/差集/unmatched/ambiguous 台账与 per-product 统计） | `api-refresh helphints` | 是 |
| `data/hints/help-docs-hints.json` | 由补全列表生成的 S10 hints 配置（`api_notes_in_list_apis=false`，note=功能介绍截断+官方文档 URL），经 `--hints` 挂载 | `api-refresh helphints` | 是 |
| `data/help_completions/deprecated.json` | 废弃接口索引（`{products: {P: {api: {replacement, doc_url}}}}`，与差集口径解耦），经 `--deprecated-index` 挂载 | `api-refresh helphints` | 是 |
| `configs/entity-knowledge.json` | 实体知识库（三 section：`aliases`/`relations` 带 provenance、`api_keywords`/`fingerprints` 指纹台账）；curated 种子 + `--llm` 抽取合并（curated 永不覆盖） | `api-refresh graph --llm`（指纹增量） | LLM 条目不可确定性重建（提交入库） |
| `data/graph/entity-index.json` | 实体关联图谱产物（products/apis/tag_products，运行时 `search_apis` 唯一数据契约；提交副本 `configs/entity-index.json` 随 wheel） | `api-refresh graph` | 是 |
| `data/openapi/` | 管道产物：`{Product}/{Tag}.json` OpenAPI 2.0 文档；MCP server 不依赖此目录 | `api-refresh`（split→convert→merge→organize） | 是 |
| `src/apie/` | APIE 管道实现（fetch/split/convert/merge/organize/refresh/api_docs + http 抓取助手 + mock 端点客户端）+ `memory_store.py` 纯内存缓存 + `catalog.py` 远端优先功能接口（缓存命中直接返回，未命中实时拉取）+ 帮助中心补全管线（`help_docs.py` 解析/匹配/差集/hints 生成纯函数核心 + `fetch_help_docs.py` 种子探测+BFS 抓取编排 + `build_help_hints.py` 差集派生落盘）+ 实体图谱构建管线（`build_entity_graph.py` 确定性构建纯函数 + `entity_extract.py` LLM 构建期语义抽取编排：transport/sleep 注入、三道闸校验、指纹增量、curated 优先合并） | — | — |
| `src/mcp_openapi/signer/` | SDK-HMAC-SHA256 签名 + 真实模式 HTTP 客户端（超时/429 退避/错误解析） | — | — |
| `src/mcp_openapi/signer/obs.py` | OBS Header 签名（HMAC-SHA1）：`Authorization: OBS AK:Signature`，CanonicalizedResource/子资源白名单/对象名编码，对齐官方 Go SDK；含预签发 URL 口径 `url_string_to_sign`/`sign_obs_url`（Expires 替换 Date 位） | — | — |
| `src/mcp_openapi/execute_obs.py` | OBS 执行 lane：`is_obs` 路由谓词 + `OBJECT_DATA_APIS`/`is_object_data_api` 对象数据面名单（真实模式恒走 presign 单口径）+ 桶寻址（带桶 virtual-hosted/无桶端点根）+ consumes 三态 body 分流（json/xml/octet-stream）+ 开关型子资源自动补全 + `Content-MD5` 自动计算 + dict→XML 序列化（根元素经转换管线 `x-xml-root` 保留）+ XML `<Error>` 解析 + 二进制响应占位 + `_presign` 预签发编排（零字节搬运）+ `ObsHttpClient` 适配器 + 编排 | — | — |
| `src/common/auth/` | 凭证加载（env/profile，project_id 自动获取） | — | — |
| `src/safety/` | safety policy 解析与匹配（PolicyRule 含 kind=product/server 与 once 标记；支持 `product:apiPattern=` 与 `server:serverId[:toolPattern]=` 两种规则前缀）+ `policy_store.py` PolicyStore（策略状态层：文件↔内存双向同步、mtime 热重载、原子落盘、四档 scope——permanent 落盘 / temporary 内存+TTL 惰性剪枝 / session 内存 overlay（code-agent 会话档，stdio 下等价进程存活期）/ once 一次性用后即焚，`authorize`/`authorize_server` dispatch 前原子授权门，生效规则=overlay 前置++文件规则整体 first-match，供 manage_policy 与运行时热更新共用） | — | — |
| `src/mcp_openapi/` | openapi 模式（metadata/execute 纯函数 + `gate.py` 产品门栓 + `hints.py` 自定义提示注入 + `entity_graph.py` 实体图谱检索（EntityGraph 值对象：parse 严格校验 + search_apis 纯词典评分/twin 归并/2-gram 回退 + load 三分支） + service 编排层 + server 装配；配置/客户端工厂注入；元数据加载委托 apie.catalog；service 层 8 个工具方法经 `_audited` 装饰器统一写审计事件，`build_audit_event` 定义对 verifier 的已发布 payload 契约 `tool/input/ok`） | — | — |
| `src/mcp_openapi/spill.py` | 响应落盘（S12）：`SpillConfig` 值对象（目录/data_enabled 部署感知披露/budget）+ `parse_spill_config` 装配解析 + `spill_body` 层级 1（截断前全保真落盘，execute 三 lane 经 `normalize_response` 共用）+ `guard_result` 层级 2（8 工具信封通用守卫，service 层 `_guarded` 装饰器统一接线）；内部接缝 `_spill_payload` 唯一落盘机制（原子写/唯一命名/格式保真 json-text-bin/note 模板） | — | — |
| `src/mcp_discover/` | discover 模式（catalog.py 目录源 + config.py + sdk.py SessionClient 协议 + manager.py session 注册表 + service.py + server.py） | — | — |
| `src/mcp_data/` | data 模式（engine.py DataFusion 惰性封装：表注册/只读守卫/规范化/双重截断 + service.py DataService audit 信封 + server.py 装配；datafusion 为 optional extra `[datafusion]`，未安装返回友好错误） | — | — |
| `src/common/types.py` | 跨模块共享类型：ClientResponse/ExecuteResult/ToolError + 六工具结果信封 + MCP discover 结果信封（McpServerItem/*Result）+ QueryDataResult/QueryColumn（data 工具信封）+ SpillInfo/PresignInfo（落盘与预签发信封）+ SearchApisResult/SearchProductHit/SearchApiHit/SearchRelated（search_apis 信封） | — | — |
| `src/common/elicit.py` | PolicyConsent：safety policy 变更的 elicitation 交互语义（offer_grant 拒绝提议授予（粗规则存在时四选一 GrantChoiceConfirm：api=最小 / api_session=最小 session 档 / product=产品级 session 档 / none） / gate_change 变更确认门 / gated_manage_policy 两模式共享的 manage_policy 工具体（确认门+service 委派，docstring 留各 registrar） / fallback_hint 未问询路径拒绝兜底指引 / parse_elicit_mode / PolicyChangeConfirm+GrantChoiceConfirm 表单 schema / ElicitFn adapter 契约 + ctx_elicit_fn MCP Context 归一化 adapter（confirm/choice 独立归一）） | — | — |
| `src/common/paths.py` | 项目根路径解析（统一 project_root） | — | — |
| `src/common/logconf.py` | 日志配置：文件为主（logs/{program}.log 轮转）+ stderr WARNING+ 兜底 | — | — |
| `src/huaweicloud_open_mcp/` | server 入口包：`cli.py` CLI（按 --mode 分发 openapi/discover 两条路径）+ `__main__.py`（`python -m`）+ `__version__`（发布版本单一真值源，hatch dynamic version 读取） | — | — |
| `benchmarks/` | LLM Agent 级工作流 benchmark（`openapi/cases/` 用例 + 旧 stub_server + scorer/report/trace/runner；`harbor/` Harbor 集成：`conventions.py` 路径常量单一真值源 + `build_agent_opencode_config`、`exporter.py`（render_task 纯核/export_dataset 薄壳）、`task_templates/` 模板组（stub_server/Dockerfile/task.toml/instruction/verifier/oracle/脚本）、`opencode_agent.py`（仅 harbor 运行时加载，本项目不声明 harbor 依赖）；`results/` 不入库，`baseline-*.json` 除外） | — | — |
| `scripts/` | 发布脚本（`publish`：test/prod 双仓区分，目标 URL 来自 `pyproject.toml` 具名 index，token 按 `UV_PUBLISH_TOKEN_TEST`/`UV_PUBLISH_TOKEN_PROD` 严格取用无回退，正式仓确认门） | — | — |
| `configs/` | safety policy 示例（含 server 规则）、`openapi-gate.example.json` 产品门栓示例、`openapi-hints.example.json` 自定义提示注入示例、tag 中文→英文翻译映射、`mcp-server-catalog.example.json` 本地目录；发布时经 hatch force-include 打进 wheel（`huaweicloud_open_mcp/configs/` 包数据），运行时经 `common.paths.config_path` 解析（仓库根优先 → 安装态包内资源回退）；`--hints`/`--deprecated-index` 額外支持裸文件名（经 `resolve_config_arg`：显式路径 > 仓库根 configs > 包内 configs） | — | — |
| `tests/` | TDD 测试（见「测试」章节） | — | — |
| `datasets/` | Harbor 任务数据集（exporter 从 cases + task_templates 重建，不入库）；`datasets/mcp-regression/<case_id>/` 每目录一个自包含 Harbor task（instruction/task.toml/environment 内嵌 hwc 源码树+stub+fixtures/solution oracle/tests verifier 壳） | `python -m benchmarks.harbor.exporter`（经 export_dataset） | 是 |

数据流（端到端 openapi 模式）：`API Explorer 远端 → 内存缓存（MemoryStore）→ 元数据工具（get_api 等）→ 门栓过滤（Gate）→ execute_api → safety 检查 → 签名 → 华为云 API（或 mock 端点）`。导航（意图未指明产品时）走 `configs/entity-index.json（构建期快照）→ search_apis 纯词典检索 → 候选产品+API 范围`，与远端元数据无关。缓存未命中时自动从 API Explorer 实时拉取并缓存。OBS 产品在 execute_api 处经 `is_obs` 分流到 OBS lane（HMAC-SHA1 签名 + virtual-hosted 桶寻址 + consumes 分流 body + 自动 Content-MD5 + 开关型子资源补全），其余产品走 SDK-HMAC-SHA256 直连。

数据流（end-to-end discover 模式）：`configs/mcp-server-catalog.example.json → list_mcp_servers/get_mcp_server → connect_mcp_server → safety 检查 → Streamable HTTP client（mcp SDK）→ 云端 MCP server → list_server_tools/get_server_tool → call_server_tool → safety 检查 → 代发调用`。

数据流（end-to-end data 模式）：`tables 映射（inline 对象数组 / 本地文件 csv|parquet|ndjson|json 数组）→ 一次性 SessionContext 注册 → 只读 SQL 守卫（sqlparse 语句分型白名单）→ DataFusion 执行 → JSON-safe 规范化（时间→ISO/Decimal→str/bytes→占位）→ 读 lane 双重截断（行数默认 100 上限 1000 + 200k 字符预算）→ QueryDataResult 信封；写 lane（transform_data）→ 结构化 out 参数（写目标非 SQL 语句）→ 覆盖门（默认拒，overwrite=true 放行）→ .tmp-part 流式写盘 → os.replace 原子替换 → 产物回读（rows/bytes）+ 小预览（默认 10 行上限 100）→ TransformDataResult 信封`。不访问网络、不需要凭证、不经 gate/policy。

## 模块依赖关系

依赖严格单向、无环，自底向上四层：

```text
第4层  src/huaweicloud_open_mcp/  入口包，按 --mode（可逗号组合）延迟 import 对应 server
           │                      + composite.py（混装装配：共享 store/sink + manage_policy 去重）
           │
第3层  src/mcp_openapi/       src/mcp_discover/       src/mcp_data/
         ├ service            ├ service               ├ service（audit 信封 + DataError 翻译）
         ├ execute            ├ catalog               ├ engine（datafusion 惰性封装）
         ├ server             ├ config                └ server
         └ signer/sign+client ├ manager → sdk
                              └ server
           │                        │                     │
第2层  src/safety/policy       src/apie/                  │
         + policy_store                                  │
          （纯函数零依赖；        ├ catalog → live_fallback → convert_openapi2
            store 仅依赖 policy）│                          │
                              ├ memory_store（纯内存缓存，零内部依赖）
                              ├ metadata / mock / api_docs(CLI)
                              └ 管道文件（fetch/split/merge/organize/validate/refresh/retry）
           │                        │
第1层  src/common/            （types / http / paths / logconf / audit / auth/credentials，均零内部依赖）
```

| 模块 | 依赖 |
| --- | --- |
| `common/` | 无内部依赖（纯基础设施）；仅 `auth/__init__` re-export `auth.credentials`；`elicit` 额外依赖 pydantic（mcp 传递依赖），不 import mcp/safety/service |
| `safety/policy` | 无依赖（纯函数）；`safety/policy_store` 仅依赖 `safety/policy` |
| `apie/` | → `common`（http/types/paths/logconf）；`catalog`→`live_fallback`→`convert_openapi2` 链、`memory_store`；`mock`→common.http/types |
| `mcp_openapi/` | → `apie`（catalog/metadata/mock/memory_store）+ `safety` + `common`；`service`→`execute`+`execute_obs`+`signer.client`+`signer.obs`；`signer.client`→`signer.sign`；`execute_obs`→`execute`+`signer.obs`+`common` |
| `mcp_discover/` | → `safety` + `common`（**不依赖 apie**）；`service`→`catalog/config/manager/sdk`；`manager`→`sdk` |
| `mcp_data/` | → 仅 `common`（audit/types；**不依赖 safety/apie**——query_data 无 policy 门）；`engine` 运行时惰性 import `datafusion`/`pyarrow`/`sqlparse`（optional extra） |
| `huaweicloud_open_mcp` | → `common.logconf` + 延迟 import `mcp_openapi.server/service`、`mcp_discover.server`、`mcp_data.server`；`composite`→三模式 server/config + `safety.policy_store` |
| `benchmarks/` | 与 `src/` 零耦合：经子进程 spawn console script `huaweicloud-open-mcp` 驱动，不 import src 任何包 |

关键设计结论：

- **`apie` 是 `mcp_openapi` 独享依赖**：APIE 元数据层只服务 openapi 直连模式；`mcp_discover` 只依赖 `safety` + `common`，两模式互不 import。
- **`mcp_data` 只依赖 `common`**：data 模式是纯本地计算（无云交互、无 policy 门），engine 的 datafusion 运行时为 optional extra 惰性加载，base 安装不携带。
- **`safety` + `common` 是各模式公共底座**，二者本身零内部依赖，为最底层可独立复用模块。
- **`metadata.py` 归入 `apie`**：避免 `apie ↔ mcp_openapi` 循环依赖——`api_docs` CLI（apie 内）与 `mcp_openapi/service` 共用同一套 `metadata` 纯函数。
- **`convert_openapi2` 在 apie 顶层**（非管道子目录）：同时被 `live_fallback`（运行时远端回退）与管道文件（离线 refresh）使用。
- **`audited`/`build_audit_event` 归入 `common.audit`**：审计装饰器与事件 payload 契约的第二消费方（mcp_data service）出现后从 mcp_openapi.service 下沉（原位置无 re-export；verifier 契约是 NDJSON payload 形状，不受模块路径影响）。
- **入口包化**：入口在 `src/huaweicloud_open_mcp/` 包（顶层 `main.py` 已废——site-packages 通用名冲突）；单模式时 `main()` 体内按 mode 分支导入对应 server；混装经 `composite.build_composite_app`（共享 PolicyStore/AuditSink、合并 instructions、manage_policy 全局注册一次，归属优先级 openapi > discover）。

## 命名约定

- **产品名**：以 `raw/apis_detail.json` 的驼峰 `product_short` 为准（如 `ECS`）；与 apis 项目的大小写去重映射保持一致。
- **tag 文件名**：英文 PascalCase，中文→英文映射维护在 `configs/tag_translations.json`；`sanitize_tag` 用 `_` 替换空格与 `/`。
- **工具名**：snake_case（`search_apis`/`list_products`/`get_product`/`list_apis`/`get_api`/`get_api_examples`/`execute_api`/`manage_policy`）；discover 模式工具（`list_mcp_servers`/`get_mcp_server`/`connect_mcp_server`/`list_server_tools`/`get_server_tool`/`call_server_tool`/`disconnect_mcp_server`/`manage_policy`）；data 模式工具（`query_data`/`transform_data`）。
- **环境变量**：遵循华为云 SDK 惯例——`HUAWEICLOUD_SDK_AK`/`HUAWEICLOUD_SDK_SK`/`HUAWEICLOUD_SDK_SECURITY_TOKEN`/`HUAWEICLOUD_SDK_PROJECT_ID`；MCP 自身配置用 `HUAWEICLOUD_MCP_*` 前缀（如 `HUAWEICLOUD_MCP_MOCK`、`HUAWEICLOUD_MCP_POLICY_FILE`、`HUAWEICLOUD_MCP_OPENAPI_GATE`（产品门栓）、`HUAWEICLOUD_MCP_OPENAPI_HINTS`（自定义提示注入配置）、`HUAWEICLOUD_MCP_AUDIT_FILE`（审计 NDJSON 落盘路径）、`HUAWEICLOUD_MCP_MOCK_PASSTHROUGH`（mock 模式转发业务参数）、`HUAWEICLOUD_MCP_ELICIT`（policy 变更 elicitation 确认模式 auto/required/off，默认 off）、`HUAWEICLOUD_MCP_SPILL_DIR`（openapi 超限落盘目录，空串/off 禁用，缺省系统临时目录 hwc-mcp-spill）、`HUAWEICLOUD_MCP_DEPRECATED_INDEX`（openapi 废弃接口索引文件）、`HUAWEICLOUD_MCP_DEPRECATED_MODE`（list_apis 废弃处理模式 annotate/hide/off）、`HUAWEICLOUD_MCP_ENTITY_INDEX`（openapi 实体图谱索引，缺省加载 configs/entity-index.json，缺失静默禁用））；构建期 LLM 抽取（api-refresh graph --llm）用独立前缀 `ENTITY_LLM_BASE_URL`/`ENTITY_LLM_API_KEY`/`ENTITY_LLM_MODEL`（通用 OpenAI-compatible，缺一即跳过抽取）。discover 模式新增：`HUAWEICLOUD_MCP_MODE`（运行模式，可逗号组合：openapi/discover/data，如 `openapi,data`）、`HUAWEICLOUD_MCP_SERVER_CATALOG`（目录文件路径）、`HUAWEICLOUD_MCP_SESSION_IDLE_TIMEOUT`、`HUAWEICLOUD_MCP_MAX_SESSIONS`。
- **region**：默认 `cn-north-4` 平铺，非默认 region 带 `{region}` 目录/后缀（沿用 apis 的 region 目录规则）。

## 构建与运行命令

项目用 uv 管理（`pyproject.toml` + `.venv`）：

```bash
uv sync                                  # 安装依赖（含 dev，dev 组含 datafusion/sqlparse）
uv sync --extra datafusion               # data 模式运行时依赖（发布态：pip install "huaweicloud-open-mcp[datafusion]"）
uv run pytest                            # 跑全部测试（默认跳过 e2e）
uv run pytest -m e2e                     # 真实数据/凭证 E2E（需 AK/SK）
uv run pytest --cov=src/common --cov=src/apie --cov=src/safety --cov=src/mcp_openapi --cov=src/mcp_discover --cov=src/mcp_data  # 覆盖率
uv run ruff check src tests              # lint（ruff，规则 E/F/W/I，line-length 120）
uv run ruff check src tests --fix        # 自动修复可修问题
uv run mypy src                          # 类型检查（全量类型标注）
```

构建与发布（PyPI）：

```bash
scripts/publish test                      # TestPyPI 发布（token：UV_PUBLISH_TOKEN_TEST）
scripts/publish prod                      # 正式 PyPI 发布（token：UV_PUBLISH_TOKEN_PROD，确认门，--yes 供 CI 跳过）
scripts/publish <test|prod> --skip-build  # 复用已有 dist/ 产物重发
```

发布约定：目标上传 URL 由 `pyproject.toml` `[[tool.uv.index]]` 具名 index 固化（`testpypi` → `test.pypi.org/legacy/`、`pypi` → `upload.pypi.org/legacy/`），经 `uv publish --index <name>` 引用；脚本内构建（清空 dist/ 后 `uv build` + `uvx twine check`）+ token 严格按目标取用（`UV_PUBLISH_TOKEN_TEST`/`UV_PUBLISH_TOKEN_PROD`，无共享回退）+ 正式仓确认门（打印目标 URL/版本/产物清单，需输入 yes）。TestPyPI 与 PyPI 账号、API token 相互独立，分别在两站 Account Settings 申请；同仓版本号不可重复上传，TestPyPI 验证通过后升版本号再发正式仓。

GitHub Actions CI/CD（`.github/workflows/`）：

- `ci.yml`（push 任意分支 / PR；同 ref 并发取消旧跑，`permissions: contents: read`）：lint（ruff + mypy 门禁）+ test 矩阵（ubuntu/macos/windows × Python 3.10–3.13 共 12 job；Swagger 2.0 schema best-effort 下载到 runner.temp 并经 `SWAGGER2_SCHEMA` env 注入（conftest 已支持），失败仅相关用例 skip；schema/curl 步骤强制 bash shell（规避 pwsh `$VAR` 不展开环境变量与 runner 尾部 `exit $LASTEXITCODE` 穿透 `||` 兜底的平台差异））+ build（ubuntu-only：`uv build` + `uvx twine check` + 全新 venv 装 wheel 冒烟 `huaweicloud-open-mcp --help` + dist 产物上传）。测试默认档即 `uv run pytest`（e2e 由 conftest 跳过；`test_execute_mock.py` 外呼 API Explorer mock 端点，托管 runner 可达）。
- `release.yml`（tag `v*` 推送 → 正式 PyPI + GitHub Release 附 dist；workflow_dispatch 手动选 `target=test|prod`，默认 test）：版本守卫（tag 触发时强制 `v{__version__} == tag`）→ 构建 + twine check → `uv publish --index <testpypi|pypi>`（token 取仓库 secrets `UV_PUBLISH_TOKEN_TEST`/`UV_PUBLISH_TOKEN_PROD`，与 `scripts/publish` 同名约定）→ tag 触发时 `softprops/action-gh-release`。CI 内无交互确认门（tag 推送即显式发布动作）。

CLI 入口（`pyproject.toml` 注册 console scripts）：

```bash
uv run api-refresh status                # 查看管道各阶段产物状态
uv run api-refresh refresh               # 整链刷新（已存在产物跳过，--force 全重跑）
uv run api-refresh <stage>               # 单步：count/products/docs/graph/details/retry/split/convert/merge/organize/validate
uv run api-refresh graph                 # 实体图谱构建（确定性；产物 data/graph/entity-index.json + configs 副本）
uv run api-refresh graph --llm           # 构建期 LLM 语义抽取（别名/产品关联/API 关键词；ENTITY_LLM_* env；指纹增量）
uv run api-refresh details --region cn-south-1
uv run api-refresh helpdocs              # 帮助中心文档页抓取（HELP_STAGES，不在默认 refresh 范围；--docset/--limit 走 python -m apie.fetch_help_docs）
uv run api-refresh helphints             # 差集补全列表 + hints 生成（--product/--min-gain/--cap/--overrides 走 python -m apie.build_help_hints）
uv run api-docs products                 # 产品列表
uv run api-docs apis ECS --tag 状态管理   # 接口列表
uv run api-docs api ECS ListServersDetails  # 接口详情（OpenAPI 2.0）
uv run api-docs search 云服务器 --product ECS
```

Harbor 评测数据集（重建不入库；本地需 docker + harbor CLI，harbor 仅在运行时环境要求 py>=3.12）：

```bash
uv run python -m benchmarks.harbor.exporter \
  benchmarks/openapi/cases datasets/mcp-regression [--case-id <id>]...
harbor run -p datasets/mcp-regression/ecs_list_servers \
  --agent benchmarks.harbor.opencode_agent:OpencodeAgent -m <provider/model>
```

MCP server 启动（stdio，由 MCP 客户端拉起）：

```bash
uv run huaweicloud-open-mcp                    # openapi 真实模式：AK/SK 签名直连华为云（默认）
uvx huaweicloud-open-mcp                       # PyPI 安装态运行（uvx 自动拉取，入口包 huaweicloud_open_mcp）
uv run python -m huaweicloud_open_mcp          # 等价入口（python -m 形态）
uv run huaweicloud-open-mcp --mock             # mock 模式：execute_api 指向 API Explorer mock 端点（无需凭证）
uv run huaweicloud-open-mcp --mode discover    # discover 模式：发现连接云端 MCP server（环境变量 HUAWEICLOUD_MCP_MODE）
uv run huaweicloud-open-mcp --mode data        # data 模式：DataFusion 只读 SQL 本地分析（需 [datafusion] extra）
uv run huaweicloud-open-mcp --mode openapi,data  # 混装：单 server 同时注册所选模式工具集（manage_policy 只注册一次）
uv run huaweicloud-open-mcp --mock-base http://127.0.0.1:8000  # 自定义 mock 端点基础地址（benchmark 本地 stub 用；环境变量 HUAWEICLOUD_MCP_MOCK_BASE）
uv run huaweicloud-open-mcp --mock --mock-passthrough   # mock 模式转发 execute 业务参数到端点（环境变量 HUAWEICLOUD_MCP_MOCK_PASSTHROUGH）
uv run huaweicloud-open-mcp --audit-file /tmp/hwc_audit.jsonl   # 审计事件 NDJSON 落盘（环境变量 HUAWEICLOUD_MCP_AUDIT_FILE）
uv run huaweicloud-open-mcp --policy configs/safety-policy.example.json  # 指定 safety policy 文件
uv run huaweicloud-open-mcp --hints configs/openapi-hints.example.json   # 自定义提示注入（环境变量 HUAWEICLOUD_MCP_OPENAPI_HINTS）
uv run huaweicloud-open-mcp --hints off               # 显式禁用（未配置时缺省加载 configs/help-docs-hints.json，缺失静默跳过）
uv run huaweicloud-open-mcp --deprecated-index data/help_completions/deprecated.json  # 废弃接口索引（缺省模式 annotate；环境变量 HUAWEICLOUD_MCP_DEPRECATED_INDEX）
uv run huaweicloud-open-mcp --entity-index data/graph/entity-index.json  # 实体图谱索引（缺省加载 configs/entity-index.json，缺失静默禁用；off 显式禁用；环境变量 HUAWEICLOUD_MCP_ENTITY_INDEX）
uv run huaweicloud-open-mcp --hints openapi-hints.example.json        # 裸文件名（显式路径 > 仓库根 configs > 包内 configs；--deprecated-index 同口径）
uv run huaweicloud-open-mcp --deprecated-index ... --deprecated-mode hide             # hide：list_apis 直接过滤废弃条目（get_api 恒可用）
uv run huaweicloud-open-mcp --elicitation off  # 关闭 elicitation 确认（默认即 off；交互客户端可 --elicitation auto/required 开启确认门；环境变量 HUAWEICLOUD_MCP_ELICIT）
uv run huaweicloud-open-mcp --spill-dir /tmp/spill   # 超大响应落盘目录（缺省系统临时目录 hwc-mcp-spill 自动落盘；空串/off 禁用；环境变量 HUAWEICLOUD_MCP_SPILL_DIR）
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
| S0 | `common.paths` 配置资源解析：`config_path(name)`（仓库根 configs/ 优先 → 安装态包内 configs 资源回退，wheel 由 hatch force-include 映射）+ `resolve_config_arg(value)`（启动参数路径：存在的显式路径原样 > 仓库根 configs > 包内 configs，全缺失 FileNotFoundError 列全候选） | 纯函数单测（monkeypatch project_root/config_path + 真实仓库布局 cwd 无关断言） | tmp 目录文件系统状态 + 包内资源路径结构 |
| S1 | `signer.sign(request) → Authorization 头` | 纯函数单测 | 华为云官方签名文档示例向量（先收集，不自行推导） |
| S2 | `safety.evaluate(policy, product, api) → allow/deny` + `match_first`/`match_server_first`（first-match 命中规则对象，PolicyStore.authorize 的 internal seam） | 纯函数单测 | 手写策略文件 + 预期字面量 |
| S2b | `safety/policy_store.py` PolicyStore（rules 热重载 / add_rule(scope, ttl_seconds) / remove_rule 跨层 / text / list_rules，文件↔内存双向同步 + 四档 scope overlay + `authorize`/`authorize_server` 原子授权门（once 用后即焚、并发恰一放行）） | 单测：tmp 文件注入 + 内容哈希 stat 替身 + 注入时钟（time_fn）；服务层「拒→add→同实例立即放行」 | 直接回读磁盘原始内容 + `parse_policy` 交叉验证 |
| S3 | 各工具业务函数 `mcp_openapi.service` / `apie.metadata`（含 manage_policy 编排；`_audited` 审计挂钩：8 工具统一记 `{ts, tool, input, ok}` NDJSON，input 为显式入参快照，异常路径记 ok=False；execute_api 一次性授权消费点（校验失败/接口未找到不烧）+ manage_policy 误路由引导） | 单测，迷你样本 fixture；audit sink 以 tmp 文件/内存替身注入 | 自建迷你 OpenAPI 片段（仿 apis fixtures 设计，不依赖真实 raw/ data/）+ 回读磁盘原始 NDJSON |
| S4 | `execute_api` HTTP 边界（含 mock passthrough：`--mock-passthrough` 开启时标量→query、body→POST JSON、`_` 控制键剥离，编码对齐 real 模式 HttpClient；默认关保持 API Explorer 契约） | 集成测试直连 mock 端点 + 本地回环 CaptureServer + 单元层 urllib 打桩注入错误（429/4xx/5xx） | mock 端点返回（HTTP 恒 200；`status_code` 非 200 返回空 body）+ 回环 stub 请求台账 |
| S5 | APIE 管道各阶段转换 + `apie.memory_store` 内存缓存层（set/get/clear/LRU）+ `apie.catalog` 功能接口（内存缓存优先→远端回退决策，monkeypatch `apie.http.fetch_json` 边界） | 纯函数单测 + 迷你样本集成 + `@pytest.mark.e2e` 全量 | Swagger 2.0 schema 校验；monkeypatch 注入 HTTP 响应控制远端回退路径 |
| S6 | benchmark 纯函数（`benchmarks/cases.py` 加载校验（含可选 `fixture`/`labels`/`policy` 扩展字段）、`scorer.py` 分层评分 + `event_to_toolcall`（审计事件→trace 输入适配，双消费者：legacy runner 与 harbor verifier）、`report.py` 统计/基线对比、`trace.py` export/NDJSON 提取 + export JSON info 的 token 读取、`stub_server.py` 本地回环、`runner.py build_benchdir_config`（legacy 与 harbor agent 双适配器）、`harbor/exporter.py` render_task 纯核/export_dataset 薄壳、`harbor/conventions.py build_agent_opencode_config`、`harbor/task_templates/stub_server.py` fixture 引擎（resolve_response/append_ledger 纯核 + GET/POST 回环）） | 纯函数单测（trace 用 spike 实测格式的迷你 fixture；stub 用回环 HTTP；exporter 用 mini project_root + 金标字面量） | 手写字面量 + 独立构造的样例调用序列 |
| S7a | `mcp_discover/catalog.py` 目录加载/搜索/缓存/clear | 纯函数单测，迷你目录 fixture + 注入 CatalogSource | 文件系统状态变化（删除文件后仍缓存命中） |
| S7b | `safety.evaluate_server(policy, server, tool) → allow/deny` | 纯函数单测，手写字面量矩阵（含向后兼容 product 规则） | 手写策略文件 + 预期字面量 |
| S7c | `mcp_discover/manager.py` session 注册表 + idle 回收 + LRU | 纯函数单测，注入时钟 | 手写字面量 |
| S7d | `mcp_discover/sdk.py` MCP client 适配层 | fake SessionClient 单测 + 真 mcp SDK + 本地 stub 回环集成 | stub 返回确定性 JSON-RPC 响应 |
| S7e | discover 工具业务函数 + mode 隔离注册（8 工具；call_tool 一次性授权消费点） | 单测注入 catalog/manager/client 工厂 + server 工具注册验证 | 字面量 + 互斥工具集合断言 |
| S8 | `mcp_openapi/gate.py` 产品门栓（parse/allows/filter_products/describe/load）+ service 门栓过滤/拒绝 + server instructions/docstring 注入范围 | 纯函数单测 + service 注入 gate + server 装配断言 | 手写字面量 + 门栓示例配置 |
| S9a | `mcp_openapi/signer/obs.py` OBS HMAC-SHA1 签名（StringToSign/CanonicalizedResource/CanonicalizedOBSHeaders/Signature） | 纯函数单测 | 官方文档「Header 中携带签名」StringToSign 构造示例（表4/6/7）+ openssl 按官方公式计算的签名值 |
| S9b | dict→OBS XML 序列化（`serialize_body_xml`，含 `xml.name` 根元素、`$ref` 嵌套、数组、转义） | 纯函数单测，迷你 schema fixture | 手写期望 XML 字面量 |
| S9c | 桶/对象寻址（`build_obs_request` 参数切分 + `build_obs_url` path-style URL） | 纯函数单测 | 手写 URL/参数字面量 |
| S9d | OBS XML `<Error>` 解析（`parse_obs_error`） | 纯函数单测 | 手写 `<Error>` 片段 |
| S9e | `is_obs` 路由谓词 + `OBJECT_DATA_APIS` 名单判定 + `execute_obs_api` 编排 + `ObsHttpClient` 签名发送 + `service` OBS 分派（对象数据面强制 presign） | 单测注入 OBS client 工厂 | 手写字面量 + 注入 client |
| S9f | 预签发 URL（`signer.obs.url_string_to_sign` / `sign_obs_url` / `build_presign_base` + `execute_presign_api` 编排 + service 对象数据面自动 presign / 显式 `_presign` 分派与热更新协同） | 纯函数单测 + service 注入凭证 | 官方《URL中携带签名》表4/5 结构原文 + openssl 口径金标 |
| S10 | `mcp_openapi/hints.py` 自定义提示注入（parse_hints/load_hints_file + Hints 值对象 product_notes/api_notes/combined_notes 合并策略）+ `ServiceConfig.hints` service 注入（list_products 条目级、get_product/list_apis 顶层、list_apis 条目级 API 级、get_api 顶层合并；拒绝路径与 get_api_examples/execute_api 恒不注入）+ server `build_instructions` 部署自定义指引段 + `--hints`/env 装配（load_hints_file 三分支：None→缺省档 configs/help-docs-hints.json 缺失静默、off/空串→显式禁用、显式路径 fail-fast） | 纯函数单测（键归一化经 lookup 方法验证，不 peek 字段内部）+ service 注入 Hints 信封断言 + 装配断言（monkeypatch project_root 密封真实仓库） | 手写字面量 + off/缺省文件缺失时结果为 empty 的回归红线 |
| S11 | data 模式：`mcp_data/engine.py`（外部接缝 `run_query`（读）/`run_transform`（写）双 lane：表注册/只读守卫/执行/JSON-safe 规范化/双重截断 + 写 lane 原子落盘/覆盖门/产物回读；内部接缝纯函数 `assert_readonly_sql`/`json_safe`/`truncate_rows`/`_sniff_output_format`/`_count_written`；datafusion 惰性 import 未安装→`DataError` 安装指引）+ `mcp_data/service.py` DataService（audit 信封 + DataError→ToolError 翻译，engine 模块形注入 Protocol）+ `mcp_data/server.py` 装配（单 data 模式工具集恰 `{query_data, transform_data}`）+ `huaweicloud_open_mcp/composite.py` 混装（工具集并集/manage_policy 去重/instructions 合并/共享 store）+ `cli.parse_modes` 逗号多值 | engine 纯函数单测（手写字面量矩阵）+ 真 datafusion 集成（inline/csv/parquet/ndjson/join/聚合/三格式互转/覆盖门/原子落盘/COPY 拒绝，无网络）+ service 注入 engine 模块形替身 + InMemoryTransport e2e | 手写字面量 + Python 独立重算的聚合真值 + 产物文件独立回读 + monkeypatch `sys.modules["datafusion"]=None` 模拟未安装 |
| S12 | 响应落盘：`mcp_openapi/spill.py`（`spill_body` 层级 1 截断前全保真 + `guard_result` 层级 2 通用信封守卫 + `SpillConfig`/`parse_spill_config` 装配；内部接缝 `_spill_payload` 唯一落盘机制）+ execute 三 lane 接线（`normalize_response(resp, spill, stem)`）+ service `_guarded` 装饰器（8 工具统一） + `_spill=false` 控制键剥离（guard 同步跳过）+ cli/server/composite 装配（`--spill-dir`/`HUAWEICLOUD_MCP_SPILL_DIR`、data_enabled 部署感知披露） | 纯函数单测（tmp_path 文件回读/原子性/唯一名/格式保真/best-effort 失败；guard 预算矩阵/占位替换/逐级收缩/refusal 与已落盘与已截断 no-op） + execute/service/obs 注入测试（超限文件独立回读、未配置回归红线、控制键不进 query） |
| S15a | 实体图谱确定性构建 `apie/build_entity_graph.py`（`build_graph` 纯函数：节点口径（仅 huawei_products 收录且有 API 的产品）/ twin 同名互指（主产品运行时按 link>API 数>字典序）/ attributive / knowledge 三 section 防御式并入（未知产品/API 引用丢弃）/ tag_products 覆盖计数 / API 名大小写不敏感关键词挂接）+ `refresh_knowledge_file`（--llm 编排：env 缺失跳过、transport 注入、原子落盘）+ stage 落盘壳（data/graph + configs 双写） | 纯函数单测（mini fixture 金标字面量）+ stage tmp_path 回读 | 手写期望产物字面量（twin/attributive/semantic 边形状、k 字段、tag_products） |
| S15b | 运行时检索 `mcp_openapi/entity_graph.py`（`parse_entity_index` 严格校验 / `EntityGraph.search_apis` 评分矩阵：alias 通道每产品每查询一次（嵌入用户原句=12 > 术语等于别名=12 > 术语∈别名=6，防 LLM 多别名互嵌碎片逐条累加——实测 专属云主机/独享主机）、product_short 精确 12 / name 5（gram 2）/ 判别 tag bonus 2（coverage ≤3）/ API 三通道分离封顶（kw exact 5·cap 10 / kw contains 2·cap 4（gram 通道跳过 kw）/ 非 kw cap 6（gram 4）；kw 精确视同 summary 级）/ 2-gram 弱结果回退（触发=无 API 级接战或 top<4，并集去重）/ twin 归并（link 非空 > API 数 > 字典序，分数取 max）/ matched_via 证据（优先级排序 + 去重 + cap 3）/ allowed 机制参数排名前过滤 / limit clamp 1-20）+ `load_entity_index` 三分支（None→bundle 缺失静默空 / off→显式禁用 / 路径→fail-fast，裸文件名经 resolve_config_arg） | 纯函数单测（手写期望分数与证据列表的独立真值矩阵） | 手写 mini 图谱 fixture + 逐查询手算分数/证据；_terms/_extend_terms 切分矩阵 |
| S15c | service/server/cli 装配：`ToolService.search_apis`（gate restrict→allowed 排名前过滤、空图谱拒绝、`_audited`/`_guarded` 接线、审计 input 快照 {query, limit, category}）+ server 第 8 工具注册 + `--entity-index`/env 三分支装配 + instructions 第 0 步 + composite 工具集并集（8+8+2=17，manage_policy 去重 10） | service 注入测试 + build_openapi_config 装配断言 + build_instructions 断言 | 手写字面量 + InMemoryTransport 工具集断言（test_composite） |
| S15e | LLM 构建期抽取 `apie/entity_extract.py`（`refresh_knowledge` 编排纯函数：roster/API 指纹增量（sha256，roster 变更→产品级重抽，产品 API 变更→该产品关键词重抽）、分批（产品 60/批、API 120/批）、三道闸校验（严格 JSON 解析容忍围栏 / 交叉验证 target∈产品集·alias≠中文名·长度 1-8·kw 1-12·每 API ≤8·去自环·note 截 60 / 丢弃台账 WARNING）、批失败跳过且指纹不更新（可恢复）、sleep_fn 注入）+ `merge_knowledge` internal seam（世代语义：update 提供 key 才替换 llm 代，否则沿用旧 llm；curated 永不覆盖；api_keywords/fingerprints 按产品 overlay）+ `load_llm_env`/`make_openai_transport`（common.http post_json_retry 429 退避） | 纯函数单测（fake transport 罐头 + 记录器；校验/合并/指纹矩阵；输入不变性） | 手写罐头响应与期望 knowledge 字面量；指纹重复性（同输入同指纹） |
| S15d | `api-refresh graph` 阶段接线（STAGES docs 后、MODULE/ artifact_of/status、`--llm` 参数透传）+ benchmark 导航用例（无产品名口语 prompt，capability: navigation） | stage 单步 dry-run + runner --dry-run 校验 | 真实 raw/ 数据产物生成 + `search_apis` 实数据冒烟（云主机/重启服务器/对象存储等口语查询命中） | 手写字面量 + tmp_path 独立回读 + discover service.py 占位先例 |
| S13a | 帮助中心抓取原语：`common.http.open_text`（HTML 文本抓取，headers 合并）+ `fetch_help_docs.Crawler`（限速 + 挑战页 30/60/120s 退避 + 瞬时异常指数退避 + `PermanentError`（404/410）快速失败；`fetch_fn`/`sleep_fn` 注入） | 纯函数单测（monkeypatch urllib + 手写挑战页/正常页 fixture；sleep 序列矩阵） | 手写挑战页/正常页字面量（实测响应形制） |
| S13b | `apie/help_docs.py` 解析纯函数：`parse_sitemap`（docset 分组去重）/`parse_api_page`（title→api_name/cn_name/is_api_ref/product_display）/`extract_func_intro`（功能介绍段提取）/`extract_links`（同 docset 绝对链接） | 纯函数单测 | 真实页面截取的迷你 HTML fixture（NovaRebootServer/CreateServers/章节页） |
| S13c | `apie/help_docs.py` 匹配差集生成：`build_alias_index`（歧义丢弃）/`match_apis`（overrides→别名→api_name→summary 兜底，duplicate/ambiguous 台账）/`detail_descriptions`/`diff_completions`（仅差集阈值）/`build_hints`（S10 schema + round-trip） | 纯函数单测 + `parse_hints` round-trip | 手写 mini index/records 字面量矩阵 |
| S13d | `apie/fetch_help_docs.py` + `apie/build_help_hints.py` 编排：种子探测+BFS（done 页用记录 links 不重取）+ 断点续传/failed 台账去重与清账/dead_seeds/limit 预算 + `build_completions` 三产物落盘回读 + HELP_STAGES 接入（refresh 默认范围不变） | 注入 fetch_fn/sleep_fn + tmp_path 文件回读 | 手写字面量 + 产物文件独立回读 |
| S13e | 端到端：`build_hints` 产物 → `build_openapi_config` 装配 → instructions 段 + `get_api` 注入 + `list_apis` 条目零注入 | tmp hints 文件 + build_config 装配断言 + service 注入 | 生成文件字面量（S13c build_hints 产出） |
| S13f | S10 开关 `api_notes_in_list_apis`（缺省 true 回归红线；false 时 list_apis 条目级抑制、顶层产品级与 get_api 不受影响） | `parse_hints` 矩阵 + service 注入断言 | 手写字面量 |
| S14a | `help_docs.parse_api_page.is_deprecated`（title `（废弃）` 后缀）+ `extract_replacement`（功能介绍替代接口名） | 纯函数单测 | NovaRebootServer（废弃+替代链接）/CreateServers 正常页 fixture |
| S14b | `mcp_openapi/deprecated.py` DeprecatedIndex（entry/names/empty + parse/load 严格校验，仿 Hints idiom） | 纯函数单测 | 手写字面量矩阵 |
| S14c | `metadata.list_apis(exclude_apis=…)`：分页前过滤 + total/tag_groups 同口径 + 缺省回归 | 纯函数单测 | 手写 mini 目录字面量 |
| S14d | service `_annotate_deprecated`/hide 排除集 + 装配默认档推导（索引→annotate、mode 无索引快速失败、env 装配） | service 注入 + build_config 断言 | 手写字面量 |
| S14e | `build_completions` 第三产物 deprecated.json（差集解耦/products 过滤）+ stale record 重取语义 | tmp 文件回读 | 手写字面量 |
| E1 | `common/elicit.py` PolicyConsent（parse_elicit_mode / offer_grant / gate_change / fallback_hint / ctx_elicit_fn 归一化）+ `safety/policy.py` grant_rule/grant_server_rule | 纯函数单测，脚本化 ElicitFn + 记录型 grant 注入 | 手写字面量 + `parse_policy` 交叉验证规则文本 |
| E2 | elicitation 端到端：两模式 server 装配（execute_api/call_server_tool/connect/manage_policy 的 PolicyConsent 接线 + `build_*_app(elicit_mode)`；openapi execute 与 discover call_tool 最小授予 api 选择 scope=once、api_session/product 选择恒 session，connect 为 session） | 真 mcp SDK client + InMemoryTransport 内存回环（脚本化 elicitation callback；`common.http.fetch_json` monkeypatch 封死元数据网络） | tmp policy 文件回读 + 结构化结果断言（granted_rule/一次性规则用后即焚、api_session 与产品级会话内持续放行、重启等价失效；显式 scope=permanent 才落盘） |

分层与纪律：

- **单元测试**（`tests/test_signer.py`、`tests/test_obs_signer.py`、`tests/test_obs_execute.py`、`tests/test_safety.py`、`tests/test_tools_*.py`、`tests/test_apie_*.py`、`tests/test_service.py`、`tests/test_client.py`、`tests/test_gate.py`、`tests/test_bench_*.py`、`tests/test_mcp_discover_*.py`、`tests/test_data_engine.py`、`tests/test_data_service.py`）：纯函数，不联网、不碰真实数据；service 层用 monkeypatch HTTP 注入 + 客户端工厂注入。
- **集成测试**（`tests/test_execute_mock.py`：直连 mock 端点，覆盖正常响应与错误注入；mock 模式下跳过签名；`tests/test_execute_mcp_mock.py`：真 mcp SDK client → 本地 MCP stub 回环 HTTP，覆盖 Streamable HTTP 协议全链路；`tests/test_data_server.py`/`tests/test_composite.py`：真 mcp SDK client + InMemoryTransport 内存回环，data 装配与混装工具集/共享 policy，无外网依赖；`tests/test_data_engine.py` 的 run_query 段：真 datafusion 本地执行）。
- **E2E 测试**（`tests/test_e2e.py`：真实 AK/SK 只读调用；`tests/test_obs_e2e.py`：OBS 只读 ListBuckets + 错误签名拒绝；`tests/test_workflow_e2e.py`：openapi 渐进式工作流全链，真实 API Explorer 远端数据；`tests/test_workflow_obs_e2e.py`：OBS 全链路 real 模式，元数据 live 回退 + 临时桶自清理式写链路（CreateBucket/SetBucketTagging/PutObject 文本/GetObject 读回/_presign 预签发 URL 客户端直连 PUT-GET-过期负路径，finally 删除对象/标签/桶）；`tests/test_workflow_discover_e2e.py`：discover 渐进式工作流全链，mock 模式 + 本地 MCP stub 回环，无外网依赖）：标 `@pytest.mark.e2e` 默认跳过；凭证优先读环境变量，缺省时自动从项目根 `.env` 加载（`conftest.py` 最小加载器，已存在的环境变量不覆盖；`.env` 已 gitignore，禁止提交）。E2E 红线为「只读 + 自清理临时资源」——写操作仅允许走带唯一前缀的临时桶并在 finally 清理。。
- red→green 垂直切片，禁止先写全部测试再写实现；禁止 mock 自有模块；期望值禁止用被测代码同法重算。

## 校验规则（必须满足）

- `data/openapi/` 全部文档通过 Swagger 2.0 schema 校验（valid 0 invalid）；转换修复规则与 apis 项目一致（consumes 字符串→数组、components→definitions、path 参数 required、3.0 字段清理、enum 去重等）。
- 签名实现必须通过官方文档测试向量；不得自行推导期望签名值。OBS 用 `Authorization: OBS AK:Signature`（HMAC-SHA1），StringToSign 结构对齐官方文档「Header中携带签名」与官方 Go SDK（obs/authV2.go），子资源白名单以官方 SDK `allowedResourceParameterNames` 为准；virtual-hosted 寻址下 CanonicalizedResource 桶名后恒带 `/`。
- OBS 执行 lane 约定：带桶 virtual-hosted、无桶端点根（URL 恒带根路径 `/`）；控制面接口（桶管理/tagging/acl 等小报文）body 按 op consumes 分流 json/xml/octet-stream；XML 根元素经转换管线把 `xml.name` 提升为 `x-xml-root` 保留并注入官方命名空间，个别接口元数据错标经 `ROOT_ELEMENT_OVERRIDES` 纠偏（SetObjectAcl 元数据错标 `ObjectAccessControlPolicy`，线上根以官方 x-request-examples 的 `AccessControlPolicy` 为准，e2e 实证错标报 MalformedACLError）；XML body 序列化（S9b）：布尔标量输出 XML Schema 词法形 `true`/`false`；数组 item 元素名按「items xml 名暗示 → `ARRAY_ITEM_ELEMENT_NAMES` 登记 → 属性名」推导，推导名=属性名时逐项重复（Tag/Part/Object/Rule/CORSRule 等），不同时包 `<属性名>` 容器（ACL：`AccessControlList`→`Grant`，登记原因是元数据对 ACL 容器建模为裸数组且不带 item 元素名，definitions $ref 定义名不可作线上元素名——DeleteObjects 的 DeleteObject 即反例）；容器 dict 形状（`{"AccessControlList": {"Grant": [...]}}`）与裸数组形状（`"AccessControlList": [...]`）收敛同一 XML；带 body 的写请求自动补 `Content-MD5`；元数据中 required 空值型子资源（tagging/acl/lifecycle 等开关）缺失时自动补 `""`；成功响应透出白名单头（ETag/x-obs-request-id 等）。
- OBS 对象数据面单口径 presign（S9f）：对象字节搬运接口（PutObject/GetObject/AppendObject/UploadPart，见 `OBJECT_DATA_APIS` + `is_object_data_api`）真实模式下**恒**走预签发 URL——无需任何标志，gate/policy 判定后仅签发访问 URL（`presign.url/method/expires_in` + `signed_content_type` + `headers` 照抄清单），客户端直连 OBS 收发字节，部署拓扑无关且不限大小；`_presign_expires` 相对秒数默认 900（换算 epoch 入签名 Date 位）、`_presign_content_type` 可锁定 PUT 类型；Content-Type 参与签名：锁定时客户端按信封 `headers` 原样携带，未锁定时签名按空 CT 计算，带 body 的 method（PUT/POST）信封附 `note` 警示直连不得携带该头（否则 SignatureDoesNotMatch）；显式 `_presign=true` 对非名单 OBS 接口仍可手动签 URL；StringToSign 与 Header 方式唯一差异为 Expires 替换 Date 位，auth 三参数（AccessKeyId/Expires/Signature）不入签，签名值 RFC3986 严格编码；独立真值为官方《URL中携带签名》表4/5 结构原文 + openssl 口径金标；非 OBS 产品传 `_presign` 显式拒绝；mock 模式豁免强制（继续走 mock 端点）；CopyObject 为服务端复制（字节不过 gateway、需签 x-obs-copy-source 头）保留直连；进度归客户端宿主能力，gateway 无感知。
- `execute_api` 响应规范化：错误统一转为结构化输出（HTTP 状态 + `error_code`/`error_msg` + `body`），429 退避重试，响应体积超限截断。错误响应 `body` 恒透出原始体（真值源，截断规则与成功分支共用 `_render_body`）；`error_code`/`error_msg` 为尽力规范化字段——错误体多形态兼容抽取（`_extract_error_fields`，仅非 2xx 调用，分层 first-hit）：L1 平坦 code 候选（`error_code`/`code`/`errorCode`，`error_code` 优先）+ msg 候选（`error_msg`/`message`/`errorMsg`/`msg`/`error_description`，独立匹配）；L2 仅 msg 键（code 为 null）；L3 嵌套 `error` dict 递归（IAM v3/Keystone，数字 code 强制 str）或 `error` 字符串直取；L4 `errors` 列表首元素递归（SWR/Docker registry v2）；L5 单键 dict 包装递归（OpenStack nova 系）。标量过滤：非空 str / 非 bool int 采纳，dict/list/bool/空串跳过落到下层；深度上限 3；title/details 不作 msg 键；全不命中保持 null 由 body 兜底（未知形状双 null）。OBS XML `<Error>` 仍由 `parse_obs_error` 先行拦截，OBS 回退 lane 自动共享本机制。
- execute_api OpenAPI 元数据 schema 校验（默认开启，`execute.validate_params` 纯函数，presign 后/三 lane 分流前共享）：只校验文档声明了的参数（未声明宽容透传、`_` 控制键跳过）；query 必填/类型/枚举**严格**（integer/number/boolean 不接受字符串形式，bool 混入数值显式排除，`type(v) is int` 防 bool 陷阱）；header 只查必填（协议即字符串）；body 用 jsonschema Draft4 + doc.definitions resolver 校验；失败返回 `{"ok": false, "reason": 可操作描述（参数/期望/实际值）}` 供 agent 自纠。路径参数校验归 real lane（build_request 内守卫——mock URL 不含 path，mock 环境无凭证时路径填充必然缺失，提前校验会系统性误杀）；OBS lane 跳过（XML body 不适用）；校验失败经 `_audited` 记 ok=False。`_path_param_values` 是路径参数知识的内部接缝（validator/builder 共享，build_request 因此降为纯 mechanism）。
- safety policy 匹配：按文件行序首个命中生效，`product:apiPattern=allow|deny`；MCP discover 扩展 `server:serverId[:toolPattern]=allow|deny`；无匹配默认 deny；无 policy 文件时 execute_api/call_server_tool 全拒。
- safety policy 四档 scope（`PolicyStore.add_rule(scope=..., ttl_seconds=...)`，2026-09 起）：`permanent` 永久（写策略文件，跨重启）/ `temporary` 临时（内存 overlay + TTL，ttl_seconds 缺省 3600s，取规则时惰性剪枝）/ `session` 会话内（内存 overlay，**缺省档**，重启即失、无需回收）/ `once` 一次性（内存 overlay，**仅放行下一次执行，用后即焚**，重启即失；与 ttl_seconds 互斥）。**`session` 档语义（2026-09 起）= 当前 code agent 会话**（AI 客户端与 gateway 的一次连接会话）：本 gateway 是 stdio 单进程单会话，故实现为进程级内存 overlay（重启即失），stdio 部署下「进程存活期」与 code-agent 会话等价；**边界**：若未来以 Streamable HTTP / supergateway 一进程多会话部署，须按 `ctx.session_id`（MCP Context 暴露，stdio 下恒 None）键控，当前未实现——进程级 overlay 会跨 code-agent 会话泄权，属已记录限制。生效规则 = overlay（插入序）++ 文件规则，整体行序 first-match——overlay allow 穿透文件具体 deny 与 `*=deny` 兜底（与落盘插位语义一致），overlay deny 可临时收紧；overlay 内复用「插到首个遮蔽 deny 之前」不变量；`remove` 跨层匹配（先会话/临时后文件）并回报 `scope`，不接受 scope/ttl_seconds 参数；`manage_policy` list 返回结构化 rules（line/scope/expires_in，评估序）+ 文件全文；ttl_seconds 仅与 temporary 组合且须为正；未配置 policy 文件时全档全拒（红线不变）；TTL 时钟经 time_fn 注入（默认 time.monotonic）。**默认档由「add 即落盘」改为 session 属破坏性变更**：需跨重启持久须显式 `scope="permanent"`。
- safety policy dispatch 前原子授权门（`PolicyStore.authorize(product, api)` / `authorize_server(server, tool)`，2026-09 起）：interface 与 `check`/`check_server` 同构（None=放行、str=拒绝原因），评估与 once 焚毁在同一 RLock 临界区内完成——并发派发下同一 once 规则恰有一个请求放行，其余落 deny；deny 路径永不焚毁；once 焚毁仅记 store 内部 INFO，调用方对 allow/allow_once 不可区分。调用顺序约束：早检 `check` 先行（廉价拒绝，元数据拉取之前），`authorize` 须在每次 dispatch 尝试前恰好调用一次；`check` 与 `authorize` 之间 once 被并发消费时 `authorize` 落 deny。openapi 消费点在 execute_api 的 presign 分支内与 mock/obs/real 主分流前（参数校验失败/接口未找到不烧授权）；discover 消费点在 call_tool 代发前；`connect` 连接级授予保持 session（授权生命周期 = code-agent 会话即进程存活，连接是持续操作状态故不焚毁；注意「会话」非 discover 到远端 MCP server 的连接会话——后者由 SessionManager 空闲回收/LRU 管，断开/回收后 session 档授权仍在）；`match_first`/`match_server_first` 为 safety 包 internal seam，服务层不接触规则对象。
- safety policy 热更新（`PolicyStore` + `manage_policy`）：策略文件为唯一真值源，运行期按 stat（mtime/size/inode）热重载，外部编辑即时生效、无需重启；`manage_policy(action=list/add/remove)` 两模式同构，add/remove 先校验再 `tmp+os.replace` 原子写盘并刷新内存，静止态 memory==file；读改写与热重载全段由进程内互斥锁（RLock）串行化，MCP 工具并发派发不丢更新（last-writer-wins 回归测试覆盖）；新 allow 规则自动插到首个会遮蔽它的 deny 规则之前（典型即 `*=deny` 兜底行前）；语义重复 add 幂等不改盘；文件被写坏/短暂消失时沿用最近合法版本记 WARNING，恢复后自动重新采纳；未配置 --policy 时 manage_policy 拒绝且不创建文件；启动时急切加载保留坏文件快速失败。
- safety policy elicitation 确认（`common/elicit.py` PolicyConsent + `--elicitation`/`HUAWEICLOUD_MCP_ELICIT`，**默认 off**，2026-09 起）：默认关闭时不发任何 MCP elicitation——`manage_policy` add/remove 直接生效热更新（缺省会话内档、不落盘；`gate_change` 恒放行）；policy 拒绝返回但 reason 追加 `fallback_hint` 兜底指引（prompt 约定：引导 LLM 经对话/交互式问询——如 question 工具——向用户确认后经 manage_policy 授予，选项语义与四选一表单一致）+ 审计 NDJSON（唯一硬保障）。开启后才启用协议级确认门——`manage_policy` add/remove 前服务端经 elicitation 向用户确认（`gate_change`，confirm 才生效）；`execute_api`/`call_server_tool`/`connect_mcp_server` 被 policy 拒绝时服务端经 elicitation 提议授予（`offer_grant`，accept → 经 manage_policy 授予并在 denial 上附 `granted_rule` + 引导重试；**openapi execute_api 与 discover call_tool 为四选一 GrantChoiceConfirm 表单——api=最小规则 `scope="once"`（一次用户确认仅放行下一次执行，用后即焚）/ api_session=最小规则 `scope="session"`（会话内持续放行该单 API/工具，重启即失；单功能粒度，与 product 选项的广度区分）/ product=产品级规则（openapi `product:*=allow`、discover 服务级全工具 `server:X:*=allow`，`scope="session"` 会话内放行该产品/服务全部目标，重启即失）/ none=不授予；coarse_rule 缺失时退化为单一确认；discover connect 无产品级选项（call 级规则匹配不到 connect 检查），保持会话内单一授予**；choice→scope 映射内聚于 PolicyConsent（api→minimal_scope 由调用点注入，api_session/product→恒 session）；grant 失败原因附入 reason）。**兜底指引口径（fallback_hint）**：凡 policy 拒绝且存在可授予 offer、但未发生任何 elicitation 的路径（off 档 / auto 与 required 下客户端不支持）——denial reason 追加可操作指引（经交互式问询确认后 manage_policy 授予，coarse 存在时并列四选项）；decline/cancel/refuse 与授予路径不加（用户已表态或已入流程）。模式语义：off 从不 elicit，拒绝路径 reason 附兜底指引；auto 尝试 elicit，客户端不支持（`ctx.elicit` 失败）→ 降级为 prompt 兜底记 WARNING（reason 附兜底指引）；required 不支持 → manage_policy 返回可操作 reason（fail-closed），拒绝路径不提议（reason 附兜底指引）。规则文本由 `safety/policy.py` grant_rule/grant_server_rule 构造（parse_policy 交叉验证）；门栓拒绝/未配置 policy/非 denial 结果一律不提议；decline/cancel 不写审计 NDJSON（仅 WARNING，`_audited` 契约不变；accept 的授予经 manage_policy 正常入审计）。**默认 off 的原因**：① 各 code agent 对 MCP elicitation 支持参差（2026-09 实测——Claude Code/Cursor/VS Code 支持；opencode 1.x 不声明 elicitation capability（client 仅声明 roots），v2 分支已实现（上游 PR #35064）但未发版且 headless 有挂起风险（#36076）；Codex 部分支持且有 bug；Gemini CLI 不支持）；② auto 的降级放行使确认门语义随客户端能力漂移——同一部署在不同 agent 下「一个有门、一个没门」；③ 默认关闭保证跨客户端行为一致、可预测，符合项目 fail-safe 默认哲学（未配置 policy 全拒）。需要交互确认门的部署显式传 `--elicitation auto/required`（或 `HUAWEICLOUD_MCP_ELICIT`）。
- 产品门栓（`Gate`）：openapi 模式产品级白名单，未配置时不限制；配置后未列出产品默认拒；越界产品在 `list_products` 静默隐藏，其余工具返回「不在 openapi mcp 授权范围内」；`execute_api` 先过门栓再过 policy。
- data 模式 `query_data`/`transform_data`（2026-09 起）：**明确不受 safety policy 约束**（policy 红线「未配置全拒」不覆盖本模式）——本地计算工具，不访问云、不需要凭证，policy 语义针对云侧调用；数据边界由引擎约束：SQL 严格只读（sqlparse 语句分型，白名单 SELECT/WITH/EXPLAIN/SHOW/DESCRIBE，多语句拒绝，SELECT INTO/内嵌 INTO 拒绝，CREATE EXTERNAL TABLE/COPY TO/INSERT 等写型语句不可达）；表源仅 inline 对象数组与本地文件（csv/parquet/ndjson/json 数组按扩展名 auto 识别，可显式 format；.json 数组全量载入内存，超大文件建议 jsonl）；一次性 SessionContext 用完即弃（无状态，无会话/注册表持久）；结果 JSON-safe（时间→ISO/Decimal→str/bytes→占位/非有限浮点→null）；表名 `^[A-Za-z_][A-Za-z0-9_]{0,127}$`；datafusion/pyarrow/sqlparse 为 optional extra `[datafusion]`，未安装返回 `DataError` 安装指引；审计 NDJSON 照常记录（两事件与两模式同构，input 含 sql/tables 快照）；混装时 `manage_policy` 仅由 openapi/discover 侧注册一次，与 data 工具无关。部署侧注意：data 工具能读本地文件（SQL 任意文件读取能力），仅应在信任该 agent 会话的本地部署启用。
  - `query_data` 读 lane：双重截断（max_rows 默认 100 上限 1000 + 200k 字符预算，行边界切分，单行超预算保留首行标记 truncated）→ QueryDataResult（columns/rows/total_rows/returned_rows/truncated/tables）。
  - `transform_data` 写 lane：**写目标为结构化 `out` 参数而非 SQL 语句**（写路径可审计的前提）；`out = {"path", "format"?}`（csv/parquet/ndjson，扩展名嗅探、显式覆盖）；覆盖门默认拒绝（提示显式 `overwrite=true`），放行时先移除再写；`.tmp-part` 同级临时文件 + `os.replace` 原子落盘（失败清理不留半截）；SQL 执行两遍（预览流式取前 N 行 + 写盘），源表须静态确定（勿用 random 等易变函数）；产物行数从落盘文件回读（parquet 元数据/csv 减表头/jsonl 数行，`_count_written`）；csv 恒带表头；预览默认 10 行上限 100（仅结果形态可见，不承载完整数据）→ TransformDataResult（path/format/rows/bytes/columns/preview）。
- 响应落盘（`mcp_openapi/spill.py`，2026-09 起）：openapi 工具响应/结果超 200k 字符（`MAX_RESPONSE_CHARS` 单一真值，execute re-export 共享）自动落盘——层级 1 `spill_body` 在 execute 三 lane（real/mock/OBS）共用咽喉点 `normalize_response` 截断**前**把完整原始 body 保真写盘（dict/list→.json、str→.txt、bytes→.bin），envelope 附加 `spill` 信封（path/format/bytes/note，仿 presign 先例），body 保留既有截断预览形态（严格附加、契约不破坏）；层级 2 `guard_result` 为 8 个 openapi 工具通用信封守卫（service 层 `_guarded` 装饰器统一接线，与 `_audited` 同层）：预算内恒同一对象原样返回（零行为变化），超限先把完整原始信封落盘再按序列化体积降序把最大容器字段替换为类型兼容占位（dict→`{"_truncated": true, "_original_size_bytes": N}`、list→`[]`），直至回预算内或无容器可收缩（保形保留，真值在 spill 文件）；refusal（ok 非 True）与已带 spill 的信封（lane 已保真落盘，预览为有意保留）恒不落盘不收缩；落盘失败 best-effort（返回 None 记 WARNING，回落纯截断，响应永不因落盘失败而失败）；原子写（.tmp-part + os.replace，与 data engine 写 lane 同一不变量）、唯一命名（stem 净化 + UTC 时间戳 + 随机后缀）、不自动清理（部署侧关注点）；部署感知披露：note 两套模板由 `SpillConfig.data_enabled` 选择（混装 data 模式指引 query_data 直读文件，纯 openapi 指引文件读取/shell——不向 agent 披露本部署不存在的工具），同一标志驱动 instructions 措辞；`params[\"_spill\"]=false` 按次退出（service 层读取后剥离，控制键不进 query；guard 对 truncated 无 spill 的信封同步跳过——lane 已作出不落盘决策即回落纯截断口径）；`--spill-dir`/`HUAWEICLOUD_MCP_SPILL_DIR` 缺省系统临时目录 hwc-mcp-spill 自动落盘，空串/off 禁用回落纯截断（未配置与未超限路径与既有行为逐字段一致，回归红线）；审计 NDJSON 契约 `{ts, tool, input, ok}` 不动，落盘事件记 INFO（spill=path bytes=N）。
- 自定义提示注入（`Hints`，2026-09 起）：openapi 模式部署侧提示（`--hints`/`HUAWEICLOUD_MCP_OPENAPI_HINTS`，启动加载、无热更新、仅 openapi 模式；路径支持裸文件名，经 `resolve_config_arg` 按显式路径 > 仓库根 configs > 包内 configs 解析，`load_hints_file` 内部接线）；配置 schema `{"instructions", "products": {PRODUCT: "notes" | {"notes", "apis": {API: text}}}}`，产品键归一化 upper（对齐 Gate）、API 键归一化 lower（大小写不敏感，对齐 live_fallback），严格校验非法配置启动快速失败；注入语义为**附加式**——官方 `summary`/`description` 永不替换，提示以独立 `hints` 字段伴随返回（`get_api` 顶层为产品+API 合并文案，合并策略内聚 `Hints.combined_notes`：产品在前、空段跳过、换行连接、双空不加字段）；注入点仅 6 处——server instructions 追加「部署自定义指引」段（全局 text 非空时）、`list_products` 条目级、`get_product`/`list_apis` 顶层产品级、`list_apis` 当前页条目级 API 级、`get_api` 顶层合并；门栓/policy 拒绝路径与 `get_api_examples`/`execute_api` 恒不注入（拒绝不泄漏越权提示、审计契约不动）；**缺省档（2026-09 起）**：`--hints` 与 env 均未配置时自动加载 `configs/help-docs-hints.json`（仓库根副本优先 → 包内副本，缺失**静默** `Hints.empty()`——隐式缺省不 fail-fast；`load_hints_file` 三分支：None→缺省档、空串/`off`（strip+大小写不敏感，对齐 spill idiom）→显式禁用、其余显式路径缺失 fail-fast，JSON 非法恒 fail-fast），`off`/空串为显式禁用逃生门；塑造发生在 service 层（与 gate.filter_products 同层，`apie/metadata.py` 纯函数不感知部署配置），全部 copy-on-write 不改写 metadata 返回对象；顶层可选布尔键 `api_notes_in_list_apis`（S13f，缺省 true=现状）为 false 时 `list_apis` 条目级 API 提示被抑制（顶层产品级与 `get_api` 合并提示不受影响），帮助中心补全生成文件显式置 false。
- 帮助中心功能介绍补全（`apie/help_docs.py` + `fetch_help_docs.py` + `build_help_hints.py`，2026-09 起）：构建期管线，**不在默认 refresh 范围**（`HELP_STAGES` 独立清单，方案 B——显式 `api-refresh helpdocs`/`helphints` 或 `refresh --from helpdocs` 触发）。数据源为 support.huaweicloud.com：sitemap 仅作种子（实测大量陈旧死链且当前 API 页往往不在 sitemap），页面经「同 docset 绝对链接」BFS 补齐；裸 urllib+浏览器 UA（curl 会被人机验证拦截），挑战页（`<title>Security Verification</title>`）按 30/60/120s 退避，404/410 包装 `PermanentError` 快速失败（不进重试梯子），限速 0.4s/页，断点续传+failed 台账（重跑=done 跳过、failed 重试、成功清账）。映射三层：docset→product（`api-*` 前缀 + product_display 别名索引 `build_alias_index` 精确层，歧义丢弃，`--overrides` JSON 消解；S14f L4 动态候选——display 中文主干与产品中文名互嵌（≥3 字，去括号/尾部英文 token/品牌前缀后）或 display 英文 token 恰为 productshort，候选集合经页面 ApiName 在候选产品目录中**唯一命中**才归属（合并文档集如 `密码安全中心 DEW`→KMS 60/CSMS 35/KPS 18/CPCS 43，不猜、不可归属入台账），build_alias_index 不预展开 L4（带版本号变体无法穷举），判定统一在 match_apis 内经 `alias_candidates` 动态做）→ (product, api_name 大小写不敏感) 优先 → cn_name 去括号后缀==summary 兜底（歧义入台账）。**仅差集口径**：空白归一后 help_intro 与 detail description 相同或增益 < min_gain（默认 20）不补全；生成 hints 恒 `api_notes_in_list_apis=false`、note=功能介绍全文（cap 默认 2000 截断加 …）+ `\n官方帮助文档: {url}`，产物 `data/help_completions/`（补全列表+report 台账）与 `data/hints/help-docs-hints.json` 均不入库可重建；生成文件经 S13c 的 parse_hints round-trip 测试固化。ECS 试点实测：131 API 匹配 126、补全 70、歧义 0。**废弃接口索引（S14，2026-09 起）**：`parse_api_page` 识别 title `（废弃）`/`（已废弃）` 后缀（apiexplorer 自身 `op.deprecated` 不可靠，ECS 实测仅 1/45），`extract_replacement` 从功能介绍「请使用 … - ApiName」提取替代接口；`helphints` 顺带产出 `data/help_completions/deprecated.json`（与差集口径解耦，匹配成功即入索引，honor products 过滤）；运行时 `mcp_openapi/deprecated.py` DeprecatedIndex（仿 Hints idiom）+ `--deprecated-index`/`--deprecated-mode` 装配（路径支持裸文件名，经 `resolve_config_arg` 解析同 `--hints`）——**配置索引且未显式传 mode 默认 annotate**（list_apis 条目加结构化 `deprecated: true`+`replacement`，total/tag_groups 不变），显式 `hide` 时 `metadata.list_apis(exclude_apis=…)` 分页前过滤（total/tag_groups 同口径），显式 mode 无索引启动快速失败；**annotate/hide 仅影响 list_apis 发现面，get_api/execute_api 恒可用**（发现面收窄 ≠ 详情拒绝）；未配置索引恒现状（回归红线）。done 记录缺 `is_deprecated` 字段视为 stale，重跑 helpdocs 自动重取升级。
- 实体关联图谱与 `search_apis`（S15，2026-09 起）：渐进式工作流第 0 步——用户意图未指明产品/API 时的跨产品检索。**构建期两段**：①确定性基座（`apie/build_entity_graph.py` 纯函数：节点=huawei_products 收录且有 API 的产品（218/310，死端排除）、twin=同名中文产品两两互指（主产品运行时按 link 非空 > API 数 > 字典序判定，ECS/HCSECS 16 组）、attributive=归属（child→parent）、tag_products 覆盖计数）+ knowledge 三 section 防御式并入（未知产品/API 引用一律丢弃，构建为运行时前最后防线）；②LLM 构建期语义抽取（`apie/entity_extract.py`，`--llm` 且 `ENTITY_LLM_BASE_URL/API_KEY/MODEL` 齐备时）：产品级别名+语义关联（roster 指纹控制）、API 级口语查询关键词（产品出题、目标约束在真实 API 清单内、sha256 指纹增量仅重抽变化产品）、三道闸（严格 JSON 解析容忍围栏 / 交叉验证长度·存在性·去重·封顶 / 丢弃台账 WARNING）、`merge_knowledge` 世代合并（update 提供 key 才替换 llm 代；**curated 永不覆盖**；批失败跳过且指纹不更新，可恢复重跑）、增量落盘（`on_update` 每产品完成即原子写 knowledge，长任务中断凭指纹台账续跑只补缺口；默认批 20 产品/80 API，`--batch-products/--batch-apis` 可调）。产物 `data/graph/entity-index.json` + 提交副本 `configs/entity-index.json`（wheel bundle 唯一运行时依赖）。**运行时纯词典**（`mcp_openapi/entity_graph.py` EntityGraph 值对象，零 LLM/零网络）：术语切分（空白/标点 + ASCII↔CJK 边界，连续 CJK 不分词）→ alias 通道每产品每查询一次（嵌入用户原句=12 > 精确=12 > 术语∈别名=6，防多别名互嵌碎片逐条累加——实测 专属云主机/独享主机）→ 产品评分（product_short 精确 12 / name 5（gram 2）/ 判别 tag bonus 2（coverage ≤3）/ API 三通道分离封顶：kw exact 5·cap 10、kw contains 2·cap 4、非 kw cap 6（gram 通道跳过 kw、封顶 2/4））→ **2-gram 弱结果回退**（触发=原术语无 API 级接战或 top<4；扩展 2-gram 术语重排取并集）→ twin 归并（分数取 max、主产品+related twin 边）→ `matched_via` 证据（优先级排序去重 cap 3，供 LLM 校准信任）。LLM 输出只存在于构建期产物与 knowledge 文件（带 provenance），运行时检索恒为纯词典逻辑；`--entity-index` 三分支（None→bundle 缺失静默空 / off→显式禁用 / 路径→fail-fast，裸文件名经 resolve_config_arg）；search_apis 为元数据工具：gate restrict 经 allowed 机制参数排名前过滤（total/truncated 与过滤后集合一致）、无 policy 语义、无 hints/deprecated 注入、产物为构建期快照（与 hints 同性质，非实时）。
- 审计 NDJSON（`HUAWEICLOUD_MCP_AUDIT_FILE` / `--audit-file`）：service 层 8 个 openapi 工具每次调用经 `AuditSink.record` 追加一行 `{"ts", "tool", "input", "ok"}`（ts ISO8601 UTC 由 sink 注入；input 为显式入参快照不含默认值；ok 缺省视为 true；异常路径记 ok=False 后原样抛出）；best-effort 永不抛出（写失败 WARNING）；事件 payload 契约由 `mcp_openapi.service.build_audit_event` 定义并经 `scorer.event_to_toolcall` 成为 harbor verifier 的 trace 输入源；未配置 sink 时零开销跳过。
- mock passthrough（`--mock-passthrough` / `HUAWEICLOUD_MCP_MOCK_PASSTHROUGH`，默认关）：开启时 `MockApiClient.mock_request(params=...)` 把 execute 业务参数转发到 mock 端点——`_` 前缀控制键剥离、扁平标量→query（bool→`str(v).lower()` 对齐 real 模式 HttpClient，扁平 dict/list→JSON 串）、`params["body"]`→POST JSON body（无 body 保持 GET，`status_code/number/region_id` 三元组不变）；service 只透传原始 params，剥离职责在 `apie/mock.py` 编码层。
- Harbor 数据集（`datasets/mcp-regression/`，exporter 重建不入库）：任务目录自包含（environment/ 内嵌 hwc 源码树（src+configs+benchmarks，verifier 复用 scorer）+ stub_server + fixtures.json + policy.json）；task.name=`mcp/<case_id>`；环境网络 public（本地 docker provider 的 egress control 依赖宿主 nft_fib 内核探针 + docker.io 镜像可达，均不满足；严格隔离环境部署时改回 no-network，agent 阶段恒 public 供 LLM 外呼）；verifier 评分语义全部来自 `benchmarks.scorer`（薄壳读 `/tmp/hwc_audit.jsonl` + `/tmp/answer.txt` + `/tests/case.yaml`），reward=通过项/(通过+失败)（skip 不计）由 test.sh 写 `/logs/verifier/reward.txt`（harbor 契约）；oracle 读 `/solution/case.yaml`（/tests 仅 verifier 阶段上传）。case YAML 可选扩展字段 `fixture`（stub 罐头）/`labels`（capability/difficulty，缺省按 expect 推导）/`policy`（per-case policy 覆盖 example）。
- Harbor 任务环境关键口径（harbor 0.22 实测）：base compose 的 command 是 `sleep infinity`，task 的 `environment/docker-compose.yaml` 必须覆盖为 `start_services.sh`（stub 才会启动）；healthcheck 用 python 标准库探活（python:3.12-slim 无 curl）；python:3.12-slim 基础镜像需本地 retag 或 daemon 配 mirror（docker.io 直连不可达）；uv.lock 的 wheel URL 固定指向 files.pythonhosted.org（UV_DEFAULT_INDEX 不生效），依赖安装须 `uv export` + pip 走 `mirrors.huaweicloud.com/repository/pypi/simple`；运行期不依赖 uv（start_mcp.sh 直接 exec console script，test.sh 直接 python -m pytest）。opencode 于镜像内预装（npm/apt 均走华为云源；内置 OpenCode agent 的 nvm 安装链走 raw.githubusercontent.com 不可达，故用自定义 OpencodeAgent），容器内 permission 须 `{"*": "allow"}`（edit/write 细粒度键不生效），provider 连接经 `MODEL_CONNECTION = ModelConnectionSpec(passthrough=True, api_key_envs=("MAAS_API_KEY",))` + `MAAS_BASE_URL` 解析写入 opencode.json。

## 文档维护

修改脚本行为时同步更新本文件：

- README.md 与 README.zh-CN.md 为双语镜像：任一侧结构或内容变更必须同步另一侧。
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
- 测试接缝（S1–S13）增删或重新确认。
- `pyproject.toml` 依赖或 CLI 入口变化（`api-refresh`/`api-docs`/`huaweicloud-open-mcp`）。
- mock 端点地址或 `--mock`/`--mode` 模式行为变化（含 `--mock-base`）。
- benchmark 用例 schema、评分口径、runner 参数变化 → 同步 `benchmarks/README.md`。
- discover 连接代理层模块（`mcp_discover/`）或目录数据源（`configs/mcp-server-catalog.example.json`）变化。

以下变化通常不需要更新：

- 只改日志文案、退避时长等不影响数据流的行为。
- 底层 HTTP 抓取细节（分页大小等），只要最终产物结构不变。
