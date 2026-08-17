# AGENTS.md

## 项目上下文

本项目实现华为云 MCP server：本地 stdio 形态的通用网关（Core 模式），用 7 个核心工具编排触达华为云全量 OpenAPI，供 AI 客户端（Claude Code / opencode / Cursor 等）通过自然语言查询与调用华为云服务。同类产品参考阿里云 OpenAPI MCP Server（Core 模式）与 AWS Labs MCP。

三层架构：

```text
┌─ MCP 网关层  src/huaweicloud_mcp/tools/ + server.py
│     list_products / get_product / list_apis / get_api /
│     get_api_examples / suggest_apis / execute_api
│        ↓ execute 前强制过 safety policy
├─ APIE 元数据层（参考 ../apis 项目重新实现，管道设计保持一致）
│     console.huaweicloud.com/apiexplorer 抓取 → OpenAPI 2.0 文档目录
│     data/openapi/{Product}/{Tag}.json  ★ 元数据产物（可重建，不入库）
├─ 执行层
│     signer/  自实现 SDK-HMAC-SHA256 签名（不依赖官方 SDK）
│     auth/    凭证加载（AK/SK 环境变量，project_id 自动获取）
│     HTTP 直连华为云；--mock 模式指向 API Explorer mock 端点
└─ 测试层（TDD，见「测试」章节）
```

核心原则：

- **TDD**：red→green 垂直切片，一次一个接缝、一个测试、一个最小实现。测试只写在预先确认的接缝（S1–S5）上；只 mock 系统边界（外部 HTTP），不 mock 自有模块；期望值必须来自独立真值（官方签名向量、已知 mock 响应），禁止同义反复断言。写测试前如接缝清单有变，先与用户确认。
- **APIE 管道参考 `../apis` 项目重新实现**：阶段划分、断点续传、tag 映射、Swagger 2.0 校验规则与其保持一致；本仓库为独立实现，不 import apis 代码。
- **安全**：`execute_api` 必须先过 safety policy（阿里云式 allowlist/denylist 模式匹配）；未配置 policy 时拒绝所有执行；凭证必须是最小权限 IAM 用户的 AK/SK。
- **mock 端点**（`https://apiexplorer.cn-north-4.myhuaweicloud.com/v1/mock/<product_short>/<api_name>?status_code=200&number=1&region_id=<region_id>`）：开放端点、无需凭证，用于集成测试与 `--mock` 模式全链路验证。实测行为：HTTP 状态恒为 200；`status_code=200` 返回与真实 API 同构的 mock 成功数据，其它 status_code 返回空 body（错误路径用单元层 urllib 打桩覆盖）。
- 数据产物（`raw/`、`data/`）可从 API 重建，不入库。

## 目录结构与数据流

| 路径 | 内容 | 生成脚本 | 可重建 |
| --- | --- | --- | --- |
| `raw/apis_count.json` | 产品接口计数 | `api-refresh count`（curl） | 是 |
| `raw/huawei_products.json` | 产品信息 | `api-refresh products`（curl） | 是 |
| `raw/apis_docs.json` | 接口索引（id/name/method/summary/tags/product_short/info_version），支撑 `list_apis`/`suggest_apis` | `api-refresh docs` | 是 |
| `raw/apis_detail.json` | 全量接口详情（断点文件 `raw/apis_detail_partial.json`）；非默认 region 在 `raw/{region}/` | `api-refresh details` + `retry` | 是 |
| `data/openapi/` | **元数据产物**：`{Product}/{Tag}.json` OpenAPI 2.0 文档（默认 region `cn-north-4` 平铺，非默认 region 在 `data/openapi/{region}/`） | `api-refresh`（split→convert→merge→organize） | 是 |
| `src/huaweicloud_mcp/apie/` | APIE 管道实现（fetch/split/convert/merge/organize/refresh/api_docs） | — | — |
| `src/huaweicloud_mcp/signer/` | SDK-HMAC-SHA256 签名 + HTTP 客户端（超时/429 退避/错误解析） | — | — |
| `src/huaweicloud_mcp/auth/` | 凭证加载（env/profile，project_id 自动获取） | — | — |
| `src/huaweicloud_mcp/safety/` | safety policy 解析与匹配 | — | — |
| `src/huaweicloud_mcp/tools/` | 7 工具业务函数（纯函数，不耦合 MCP 协议） | — | — |
| `src/huaweicloud_mcp/server.py` | stdio MCP server 装配（mcp SDK） | — | — |
| `configs/` | safety policy 示例、tag 中文→英文翻译映射 | — | — |
| `tests/` | TDD 测试（见「测试」章节） | — | — |

数据流（端到端）：`APIE 管道 → data/openapi/ + raw/apis_docs.json → 元数据工具（get_api 等）→ execute_api → safety 检查 → 签名 → 华为云 API（或 mock 端点）`。

## 命名约定

- **产品名**：以 `raw/apis_detail.json` 的驼峰 `product_short` 为准（如 `ECS`）；与 apis 项目的大小写去重映射保持一致。
- **tag 文件名**：英文 PascalCase，中文→英文映射维护在 `configs/tag_translations.json`；`sanitize_tag` 用 `_` 替换空格与 `/`。
- **工具名**：snake_case（`list_products`/`get_product`/`list_apis`/`get_api`/`get_api_examples`/`suggest_apis`/`execute_api`）。
- **环境变量**：遵循华为云 SDK 惯例——`HUAWEICLOUD_SDK_AK`/`HUAWEICLOUD_SDK_SK`/`HUAWEICLOUD_SDK_SECURITY_TOKEN`/`HUAWEICLOUD_SDK_PROJECT_ID`；MCP 自身配置用 `HUAWEICLOUD_MCP_*` 前缀（如 `HUAWEICLOUD_MCP_MOCK`、`HUAWEICLOUD_MCP_POLICY_FILE`）。
- **region**：默认 `cn-north-4` 平铺，非默认 region 带 `{region}` 目录/后缀（沿用 apis 的 region 目录规则）。

## 构建与运行命令

项目用 uv 管理（`pyproject.toml` + `.venv`）：

```bash
uv sync                                  # 安装依赖（含 dev）
uv run pytest                            # 跑全部测试（默认跳过 e2e）
uv run pytest -m e2e                     # 真实数据/凭证 E2E（需 AK/SK）
uv run pytest --cov=src/huaweicloud_mcp  # 覆盖率
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
uv run huaweicloud-mcp                    # 真实模式：AK/SK 签名直连华为云
uv run huaweicloud-mcp --mock             # mock 模式：execute_api 指向 API Explorer mock 端点（无需凭证）
uv run huaweicloud-mcp --policy configs/safety-policy.example.json  # 指定 safety policy 文件
```

前置依赖：`uv`；Swagger 2.0 schema 文件 `/tmp/swagger2_schema.json`（`curl -sL https://raw.githubusercontent.com/OAI/OpenAPI-Specification/main/_archive_/schemas/v2.0/schema.json`），丢失后重新下载。

## 测试（TDD）

测试接缝（已确认；变更需先与用户重新确认）：

| 接缝 | 内容 | 测试方式 | 独立真值 |
| --- | --- | --- | --- |
| S1 | `signer.sign(request) → Authorization 头` | 纯函数单测 | 华为云官方签名文档示例向量（先收集，不自行推导） |
| S2 | `safety.evaluate(policy, product, api) → allow/deny` | 纯函数单测 | 手写策略文件 + 预期字面量 |
| S3 | 7 个工具业务函数 `tools.*` | 单测，迷你样本 fixture | 自建迷你 OpenAPI 片段（仿 apis fixtures 设计，不依赖真实 raw/ data/） |
| S4 | `execute_api` HTTP 边界 | 集成测试直连 mock 端点 + 单元层 urllib 打桩注入错误（429/4xx/5xx） | mock 端点返回（HTTP 恒 200；`status_code` 非 200 返回空 body） |
| S5 | APIE 管道各阶段转换 | 纯函数单测 + 迷你样本集成 + `@pytest.mark.e2e` 全量 | Swagger 2.0 schema 校验 |

分层与纪律：

- **单元测试**（`tests/test_signer.py`、`tests/test_safety.py`、`tests/test_tools_*.py`、`tests/test_apie_*.py`）：纯函数，不联网、不碰真实数据。
- **集成测试**（`tests/test_execute_mock.py`）：直连 mock 端点，覆盖正常响应与错误注入；mock 模式下跳过签名。
- **E2E 测试**（`tests/test_e2e.py`）：真实 AK/SK + 真实 API（只读），标 `@pytest.mark.e2e` 默认跳过。
- red→green 垂直切片，禁止先写全部测试再写实现；禁止 mock 自有模块；期望值禁止用被测代码同法重算。

## 校验规则（必须满足）

- `data/openapi/` 全部文档通过 Swagger 2.0 schema 校验（valid 0 invalid）；转换修复规则与 apis 项目一致（consumes 字符串→数组、components→definitions、path 参数 required、3.0 字段清理、enum 去重等）。
- 签名实现必须通过官方文档测试向量；不得自行推导期望签名值。
- `execute_api` 响应规范化：错误统一转为结构化输出（`error_code`/`error_msg`/HTTP 状态），429 退避重试，响应体积超限截断。
- safety policy 匹配：按文件行序首个命中生效，`product:apiPattern=allow|deny`；无匹配默认 deny；无 policy 文件时 execute_api 全拒。

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
- MCP 工具增删或输入输出契约变化。
- safety policy 语法、默认行为（无 policy 时拒绝/放行）变化。
- 测试接缝（S1–S5）增删或重新确认。
- `pyproject.toml` 依赖或 CLI 入口变化（`api-refresh`/`api-docs`/`huaweicloud-mcp`）。
- mock 端点地址或 `--mock` 模式行为变化。

以下变化通常不需要更新：

- 只改日志文案、退避时长等不影响数据流的行为。
- 底层 HTTP 抓取细节（分页大小等），只要最终产物结构不变。
