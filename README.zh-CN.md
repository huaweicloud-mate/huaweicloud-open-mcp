<div align="center">
  <img src="logo.png" alt="Huawei Cloud Open MCP" width="240"/>
  <p><sub><a href="README.md">English</a> | 中文</sub></p>
  <p>
    <a href="https://pypi.org/project/huaweicloud-open-mcp/">
      <img src="https://img.shields.io/pypi/v/huaweicloud-open-mcp?include_prereleases=1" alt="PyPI">
    </a>
  </p>
</div>

# 华为云 Open MCP

**Open Connect. Explore What's Next.**
**开放连接，探索下一个可能**

一个开放的本地 [Model Context Protocol](https://modelcontextprotocol.io) 网关，把 code agent —— opencode、Codex、Cursor 以及任何支持 MCP 的客户端 —— 以自然语言连接到华为云。无需逐服务手写封装：Agent 逐步探索全量目录（300+ 产品、17000+ API），收窄到一次具体调用并执行，请求在本地完成签名。这是面向个人的本地化部署方案：网关完全运行在你自己的机器上——AK/SK 永不出本机。

三种模式经 `--mode` 逗号组合混用（如 `openapi,data`）：`openapi`（默认）直连华为云 OpenAPI；`discover` 发现连接云端华为云 MCP server（实验性，暂无文档）；`data` 用 DataFusion 对 inline/本地数据执行只读 SQL 分析与转换落盘——本地计算工具，不需要凭证、不受 safety policy 约束。典型闭环（`openapi,data` 混装）：execute_api 拉取大数据 → 落地文件 → query_data 聚合 / transform_data 整形落盘，仅聚合结果或产物元数据进入模型上下文。

## 工作原理

- **渐进式工作流** —— Agent 逐步探索：`list_products → get_product → list_apis → get_api → (get_api_examples) → execute_api`，从 17000+ API 收窄到一次具体调用。每一步都把 LLM 上下文控制在有界范围；完整工作流指引已内置于 server instructions。
- **元数据驱动、零 SDK** —— API 元数据从华为云 API Explorer 实时拉取并缓存在内存；请求在本地签名（自实现 SDK-HMAC-SHA256）后直连华为云。
- **默认安全** —— 每次 `execute_api` 必须通过 safety policy（allowlist/denylist）；未配置 policy 时全部拒绝。规则热更新，Agent 可经 `manage_policy` 申请最小授予。

## 快速开始

连接一次，剩下的交给 Agent 探索。

### 前置要求

- Python 3.10+，[uv](https://docs.astral.sh/uv/) 在 PATH 上（或 `pip`）—— 见[兼容性](#兼容性)
- 一个 code agent：[opencode](https://opencode.ai) 或 [Codex](https://developers.openai.com/codex/) —— 任何支持 MCP 的客户端均可
- 一对华为云 AK/SK，来自**最小权限 IAM 子用户**（推荐：仅授予计划查询所需的只读权限）
- 可访问 `apiexplorer.cn-north-4.myhuaweicloud.com` 的网络

### 兼容性

**支持范围** —— 依包元数据：

- **操作系统**：Windows、macOS、Linux —— 基础包为纯 Python，任何能运行 Python 的平台均可；可选 `[datafusion]` extra（data 模式）提供 Windows x86_64、macOS x86_64/arm64、Linux x86_64/aarch64（manylinux）原生 wheel
- **Python**：3.10+（`requires-python = ">=3.10"`）；`[datafusion]` extra 覆盖同一范围

**实测** —— 完整单测 + 集成套件（`uv run pytest`，e2e 默认排除；经 dev 依赖组包含 data 模式测试），Linux x86_64：

| Python | 3.10 | 3.11 | 3.12 | 3.13 |
|---|---|---|---|---|
| 完整套件（含 data 模式） | 通过 | 通过 | 通过 | 通过 |

Windows 与 macOS 预期可用——两平台的依赖解析已验证、代码无 OS 特定分支——但未经实机测试；目前尚无 CI 矩阵。

### 安装

快速开始经 [uvx](https://docs.astral.sh/uv/) 运行网关——无需安装步骤：首次调用自动拉取包。偏好持久安装时：

```bash
pip install huaweicloud-open-mcp                  # 或 uv tool install / pipx install——装完在步骤 3 用 huaweicloud-open-mcp 替换 uvx huaweicloud-open-mcp
pip install "huaweicloud-open-mcp[datafusion]"    # 可选 extra：data 模式 SQL 引擎
```

### 步骤 1：提供凭证

网关从 `~/.huaweicloud/credentials`（INI 格式，`[basic]` 节）读取你的 AK/SK —— Windows 上即 `%USERPROFILE%\.huaweicloud\credentials`。偏好环境变量内联方式见[凭证](#凭证)。

在你的用户主目录创建 `.huaweicloud` 目录，再在其中创建 `credentials` 文件，内容如下：

```ini
[basic]
ak = your-access-key-id
sk = your-secret-access-key

# 可选 —— 按需取消注释：
# security_token = <temporary-security-token>
# project_id = <project-id>
# domain_id = <domain-id>
```

可选键（按需取消注释）：`security_token`（临时凭证）、`project_id`（缺省自动解析）、`domain_id`（全局级服务，完整支持开发中）。妥善保管该文件 —— 里面有你的密钥：macOS/Linux 执行 `chmod 600 ~/.huaweicloud/credentials`；Windows 下用户主目录中的文件默认仅本账户可读。server 启动时读取该文件；日志行 `server start: ... credentials=configured`（见 `--log-file`）可确认已加载。

### 步骤 2：创建只读 safety policy

除非 policy 文件显式允许，网关拒绝每一次 `execute_api`；未配置 policy 时全部拒绝。

在你的主目录创建一个 policy 文件（例如 `hwc-policy.json`），内容如下：

```json
[
  "ECS:*List*=allow",
  "*=deny"
]
```

每条规则形如 `product:apiPattern=allow|deny` —— fnmatch 风格通配、大小写不敏感、`#` 开头为注释。规则自上而下评估、首个命中生效：本文件允许所有名字含 `List` 的 ECS API，其余全部拒绝。客户端需要该文件的**绝对路径** —— macOS/Linux 如 `/home/you/hwc-policy.json`，Windows 如 `C:\Users\you\hwc-policy.json` —— 因为客户端用自己的工作目录拉起 server。

### 步骤 3：将网关注册到你的 code agent

**opencode** —— 添加到 `opencode.json`（项目级，所有 OS 通用）或全局配置（macOS/Linux：`~/.config/opencode/opencode.json`；Windows 建议用项目级文件，或经 `OPENCODE_CONFIG` 环境变量指向绝对路径）：

```json
{
  "mcp": {
    "huaweicloud": {
      "type": "local",
      "command": [
        "uvx", "huaweicloud-open-mcp",
        "--policy", "/home/you/hwc-policy.json"
      ],
      "enabled": true
    }
  }
}
```

**Codex** —— 添加到 `~/.codex/config.toml`（Windows：`%USERPROFILE%\.codex\config.toml`），或执行（单行命令，任意 shell 通用）：

```
codex mcp add huaweicloud -- uvx huaweicloud-open-mcp --policy /home/you/hwc-policy.json
```

```toml
[mcp_servers.huaweicloud]
command = "uvx"
args = ["huaweicloud-open-mcp", "--policy", "/home/you/hwc-policy.json"]
```

把示例路径换成步骤 2 里你自己的绝对路径（Windows 形如 `C:\Users\you\hwc-policy.json`；JSON/TOML 字符串中需写成转义反斜杠形式 `"C:\\Users\\you\\hwc-policy.json"`）。

**输出：** 启动你的 Agent —— 七个网关工具出现，以 server 名为前缀（`huaweicloud_list_products`、`huaweicloud_get_product`、`huaweicloud_list_apis`、`huaweicloud_get_api`、`huaweicloud_get_api_examples`、`huaweicloud_execute_api`、`huaweicloud_manage_policy`）。Codex 中 `codex mcp list` 可见该 server，TUI 内 `/mcp` 确认已连接。

凭证来自步骤 1 —— 客户端配置中不含任何密钥。若偏好环境变量内联，见[凭证](#凭证)。

### 步骤 4：浏览产品目录（首次真实调用）

**输入**（对 Agent 说）：

> 列出可用的华为云产品。

Agent 调用 `list_products`，从华为云 API Explorer 实时拉取元数据。

**输出**（节选）：

```json
{
  "ok": true,
  "total": 310,
  "products": [
    { "product": "ECS", "name": "弹性云服务器", "category": "计算",
      "link": "https://www.huaweicloud.com/product/ecs.html" },
    { "product": "EVS", "name": "云硬盘", "category": "存储",
      "link": "https://www.huaweicloud.com/product/evs.html" }
  ]
}
```

### 步骤 5：执行真实只读 API

**输入**（对 Agent 说）：

> 列出我在 cn-north-4 的 ECS 云服务器。

Agent 走渐进式工作流 —— `list_apis(ECS)` 找到 API、`get_api` 读参数，随后 `execute_api` 用你的 AK/SK 本地签名、直连真实华为云。

**输出**（节选）：

```json
{
  "ok": true,
  "product": "ECS",
  "api": "ListServersDetails",
  "status": 200,
  "body": {
    "servers": [
      { "name": "ecs-01", "status": "ACTIVE", "id": "1d4e…" }
    ],
    "count": 2
  }
}
```

`"count": 0` 且 `servers` 为空同样是成功 —— 该 region 下账号没有实例；换个 `region` 再问（例如 `cn-east-3`）。

安全说明：请求在本地签名，SK 永不出本机；policy 文件把 Agent 限定在只读 ECS `List*` API 内。放宽策略请逐条渐进 —— 见 [Safety policy](#safety-policy)。

### 还没有账号？Mock 模式

同一流程无需凭证即可体验：跳过步骤 1，并在步骤 3 的 server 命令中加 `--mock`（`uvx huaweicloud-open-mcp --mock --policy <policy路径>`，如 `/home/you/hwc-policy.json` 或 `C:\Users\you\hwc-policy.json`）。

**输出：** 步骤 4 完全一致 —— 产品目录仍是真实元数据；步骤 5 返回与真实响应同构的模拟数据（mock 端点，不涉及华为云账号）。

## OBS 上传与下载

OBS 对象类 API（`PutObject` / `GetObject` / `AppendObject` / `UploadPart`）的字节流从不经过网关。真实模式下 `execute_api` **恒**返回预签名 URL 信封——无需任何标志——客户端直连 OBS 收发字节，不限大小：

```json
{
  "ok": true,
  "presign": {
    "url": "https://<bucket>.obs.<region>.myhuaweicloud.com/<key>?AccessKeyId=...&Expires=...&Signature=...",
    "method": "PUT",
    "expires_in": 900,
    "signed_content_type": "application/octet-stream",
    "headers": { "Content-Type": "application/octet-stream" }
  }
}
```

用任意 HTTP 客户端消费该 URL——网关不经手数据：

```bash
curl -X PUT --upload-file big.dat '<url>' -H 'Content-Type: application/octet-stream'
```

关键口径：

- Content-Type 参与签名。上传建议传 `_presign_content_type` 锁定类型，并按 `headers` 清单原样携带；未锁定时签名按空 Content-Type 计算，直连请求不得携带该头（`curl -H 'Content-Type:'`）。命中该情形时信封的 `note` 字段会给出警示。
- `_presign_expires` 调整有效期（秒，默认 900）。
- 其余 OBS 接口（桶管理、tagging、ACL 等）照常经网关执行；需要 URL 时可显式传 `_presign=true`。非 OBS 产品传 `_presign` 会被拒绝。mock 模式继续走 mock 端点。

## 工具（openapi 模式）

| 工具 | 职责 |
| --- | --- |
| `list_products` | 全量华为云产品目录 —— 标识符、显示名、分类、产品页链接；`keyword`/`category` 过滤 |
| `get_product` | 单产品详情（分类、API 数、是否全局级） |
| `list_apis` | 产品 API 目录，含 `tag_groups` 全量 tag 概览；`tag`/`search`/`limit`/`offset` 收窄 |
| `get_api` | 单 API 完整文档（参数、必填、枚举、约束）—— 执行前必读。超大文档（超 200k 字符）完整信封落盘，响应中重字段以占位替换 |
| `get_api_examples` | 单 API 官方请求示例 |
| `execute_api` | 执行一个 API：路径/query 参数平铺、请求体放 `body`；错误结构化返回、429 自动退避重试。超大响应（超 200k 字符）自动落盘：结果携带 `spill` 信封（`path`/`format`/`bytes`/`note`），`body` 保留截断预览；`_spill=false` 可按次退出 |
| `manage_policy` | 运行期增删查 safety policy 规则（热生效、无需重启） |

## 工具（data 模式）

本地数据分析与转换，DataFusion 引擎（可选依赖：`pip install "huaweicloud-open-mcp[datafusion]"`；未安装时工具返回安装指引）。

| 工具 | 职责 |
| --- | --- |
| `query_data` | 对命名表执行只读 SQL：`{"表名": {"data": [对象数组]}}`（inline）或 `{"表名": {"path": "文件"}}`（本地 csv/parquet/jsonl/json 数组，按扩展名自动识别）；返回列 schema + JSON-safe 行，行数与字符预算双重截断 |
| `transform_data` | 把只读 SQL 变换结果落盘为新数据文件：`out = {"path", "format"?}`（csv/parquet/jsonl），原子写盘，默认拒绝覆盖（`overwrite=true` 显式放行）；返回产物元数据（path/format/rows/bytes）+ 小预览 |

严格只读 SQL：仅 `SELECT`/`WITH`/`EXPLAIN`/`SHOW`/`DESCRIBE` 通过守卫，多语句与写型语句（`INSERT`/`CREATE`/`COPY TO`/…）一律拒绝——`transform_data` 的写盘动作由引擎在守卫**之后**经结构化 `out` 参数施加（审计 NDJSON 记录写路径），SQL 无写语法可达。两工具均不访问云、不需要凭证、**不受** safety policy 约束——仅应在信任该 Agent 会话读写本地文件的部署中启用。

## Safety policy

policy 文件是 JSON 数组（或纯文本）规则列表，自上而下评估、首个命中生效：

```json
[
  "ECS:*List*=allow",
  "VPC:*Show*=allow",
  "*=deny"
]
```

- 规则格式 `product:apiPattern=allow|deny` —— fnmatch 风格通配、product/API 大小写不敏感、`#` 行为注释。
- 未配置 `--policy` → 全部执行被拒。
- `manage_policy` add 的授予档位：`once`（一次性，用后即焚）· `session`（缺省；仅本次 Agent 会话）· `temporary`（TTL 自动过期）· `permanent`（写入 policy 文件）。
- 一切热生效：外部编辑文件即时生效；经 `manage_policy` 增删亦然。优先授予最小规则（`once`/`session`），产品级仅在确有必要时使用。
- 拒绝结果附可操作原因；开启 `--elicitation auto|required` 后，server 会经 MCP elicitation 提议授予（四选一：api=最小规则（一次性）/ api_session=最小规则（会话内）/ product=产品级规则（会话内）/ none=不授予）。默认 `off`，保证跨客户端行为可预期。

包内附带更丰富的示例：`configs/safety-policy.example.json`。

## 自定义提示注入（可选）

hints 配置文件允许部署方向发现链注入自有指引：全局 `instructions` 追加到 server instructions 末尾，产品级 `notes` 与 API 级文案附加到发现结果（`list_products` / `get_product` / `list_apis` / `get_api`）。

```json
{
  "instructions": "本部署面向运维巡检场景：批量查询优先用 List*/Show* 接口。",
  "products": {
    "ECS": {
      "notes": "查询云服务器列表优先用 ListServersDetails。",
      "apis": {
        "ResizeServer": "变更规格前先用 ListFlavors 确认售罄情况。"
      }
    },
    "OBS": "对象上传/下载恒返回 presign 信封，网关不经手字节流。"
  }
}
```

- 官方元数据永不被替换 —— 提示以独立 `hints` 字段伴随返回（产品级 + API 级合并，产品在前）。
- 仅注入成功发现结果，拒绝路径（门栓/policy）永不注入；`get_api_examples` 与 `execute_api` 恒不注入。
- 产品键与 `apis` 键均大小写不敏感；产品值可以是纯字符串（仅产品提示）或含 `notes` / `apis` 的对象。
- 可选顶层布尔键 `api_notes_in_list_apis`（缺省 `true`）：为 `false` 时 `list_apis` 条目不携带 API 级提示（顶层产品级与 `get_api` 合并提示不受影响）——帮助中心补全生成文件显式置 `false`，使 `get_api` 成为唯一增强面。
- 启动时加载（无热更新）；配置非法启动即快速失败。
- 缺省文件：未传 `--hints` 且未设置 `HUAWEICLOUD_MCP_OPENAPI_HINTS` 时，自动加载 `configs/help-docs-hints.json`（仓库根副本优先 → 安装包内置副本）；文件缺失静默跳过。传 `--hints off`（或环境变量置空）显式禁用。显式路径/裸名缺失仍快速失败。
- 文件路径支持裸文件名：存在的显式路径（绝对或 cwd 相对）原样使用；否则按 `configs/<名字>` 解析（仓库根 `configs/` 优先 → 安装包内置副本——`uvx`/`pip` 安装态免写全路径）。

示例：`configs/openapi-hints.example.json`（可经 `--hints openapi-hints.example.json` 裸名加载）。

### 帮助中心功能介绍补全（可选，构建期）

`api-refresh` 额外提供两个阶段（不在默认 refresh 范围），从官方帮助中心收割更完善的「功能介绍」并生成 hints 配置：

```bash
uv run api-refresh helpdocs    # 抓取解析帮助中心页面（sitemap 种子 + 同文档集链接 BFS，断点续传）
uv run api-refresh helphints   # 匹配 apiexplorer 接口并差集比较，产出 data/help_completions/ 与 data/hints/help-docs-hints.json
uv run huaweicloud-open-mcp --hints data/hints/help-docs-hints.json
```

- 仅差集口径：仅当帮助中心功能介绍明显比 API Explorer 描述更完善时才补全（`--min-gain`，默认 20 字符）。
- 每条 note 为功能介绍全文（`--cap` 截断，默认 2000 字符，超长加 …）+ 官方文档 URL。
- 限速爬取（0.4s/页）+ 人机验证退避 + 断点续传；产物可重建不入库。完整管线规则见 [AGENTS.md](AGENTS.md)。
- `--hints` / `--deprecated-index` 支持裸文件名，按 `configs/` 解析（仓库根优先 → 安装包内置副本）。

同一管线顺带产出废弃接口索引（`data/help_completions/deprecated.json`），信号取自帮助中心自身的 `（废弃）` 标题（apiexplorer 的 `op.deprecated` 元数据不可靠——ECS 试点：45 vs 1）。挂载后治理发现面：

```bash
uv run huaweicloud-open-mcp --deprecated-index data/help_completions/deprecated.json                 # annotate（缺省）：list_apis 条目带 deprecated: true + replacement
uv run huaweicloud-open-mcp --deprecated-index ... --deprecated-mode hide                            # hide：list_apis 直接过滤废弃条目（计数同口径）
```

- `annotate`/`hide` 仅影响 `list_apis` 发现面；`get_api`/`execute_api` 恒可用（发现面收窄 ≠ 详情拒绝）。
- `off`：即使挂载索引也不治理。显式传 mode 而未配置 `--deprecated-index` → 启动快速失败。
- 未配置 `--deprecated-index` 时行为逐字段不变（无 mode 即不治理）。

## 配置

### CLI 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--mode <modes>` | `openapi` | 运行模式，逗号组合（`openapi`/`discover`/`data`，如 `openapi,data`；env `HUAWEICLOUD_MCP_MODE`） |
| `--mock` | off | `execute_api` 指向 API Explorer mock 端点（无需凭证） |
| `--mock-base <url>` | — | mock 端点基础地址覆盖（env `HUAWEICLOUD_MCP_MOCK_BASE`） |
| `--mock-passthrough` | off | mock 模式转发 execute 业务参数到端点（env `HUAWEICLOUD_MCP_MOCK_PASSTHROUGH`） |
| `--policy <file>` | — | safety policy 文件；缺失 → 全部执行被拒 |
| `--region <id>` | `cn-north-4` | 默认 region |
| `--gate <file>` | — | 可选产品门栓（allowlist；未列出产品对 Agent 隐藏） |
| `--hints <file\|off>` | `configs/help-docs-hints.json`（缺失静默跳过） | 可选自定义提示注入配置（部署侧指引注入 instructions 与发现结果）；`off` 显式禁用；支持裸文件名按 `configs/` 解析 |
| `--deprecated-index <file>` | — | 可选废弃接口索引；启用 list_apis 的 annotate/hide 治理；支持裸文件名按 `configs/` 解析 |
| `--deprecated-mode <annotate\|hide\|off>` | `annotate`（配置索引时） | `annotate`=`list_apis` 条目附加 `deprecated: true` + `replacement`（总数/tag_groups 不变）；`hide`=分页前过滤废弃条目（计数同口径）；`off`=不治理；仅影响 `list_apis`——`get_api`/`execute_api` 恒可用；显式传 mode 需同时配置 `--deprecated-index`（环境变量 `HUAWEICLOUD_MCP_DEPRECATED_MODE`） |
| `--elicitation auto\|required\|off` | `off` | policy 变更的 MCP elicitation 确认 |
| `--spill-dir <dir>` | 系统临时目录（`hwc-mcp-spill`） | 超大响应/信封落盘目录（空串或 `off` 禁用落盘，回落纯截断） |
| `--audit-file <file>` | disabled | 审计落盘（NDJSON）：每次工具调用一行 `{ts, tool, input, ok}` |
| `--log-level` / `--log-file` | `INFO` / `logs/huaweicloud-open-mcp.log` | 日志（轮转文件；stderr 同步 WARNING+） |

### 环境变量

| 变量 | 用途 |
| --- | --- |
| `HUAWEICLOUD_SDK_AK` / `HUAWEICLOUD_SDK_SK` | Access Key / Secret Key（真实模式）；profile 文件替代方式见[凭证](#凭证) |
| `HUAWEICLOUD_SDK_SECURITY_TOKEN` | 可选临时安全凭证 token |
| `HUAWEICLOUD_SDK_PROJECT_ID` | 可选；缺省自动解析 |
| `HUAWEICLOUD_SDK_DOMAIN_ID` | 可选；为全局级服务预留（完整支持开发中） |
| `HUAWEICLOUD_MCP_MODE` | 等价 `--mode` |
| `HUAWEICLOUD_MCP_REGION` | 等价 `--region` |
| `HUAWEICLOUD_MCP_MOCK` | 等价 `--mock`（`1`/`true`/`yes`） |
| `HUAWEICLOUD_MCP_MOCK_BASE` | mock 端点基础地址覆盖 |
| `HUAWEICLOUD_MCP_MOCK_PASSTHROUGH` | 等价 `--mock-passthrough` |
| `HUAWEICLOUD_MCP_POLICY_FILE` | 等价 `--policy` |
| `HUAWEICLOUD_MCP_OPENAPI_GATE` | 等价 `--gate` |
| `HUAWEICLOUD_MCP_OPENAPI_HINTS` | 等价 `--hints` |
| `HUAWEICLOUD_MCP_DEPRECATED_INDEX` | 等价 `--deprecated-index` |
| `HUAWEICLOUD_MCP_DEPRECATED_MODE` | 等价 `--deprecated-mode` |
| `HUAWEICLOUD_MCP_AUDIT_FILE` | 等价 `--audit-file` |
| `HUAWEICLOUD_MCP_SPILL_DIR` | 等价 `--spill-dir` |
| `HUAWEICLOUD_MCP_ELICIT` | 等价 `--elicitation` |
| `HUAWEICLOUD_MCP_LOG_LEVEL` / `HUAWEICLOUD_MCP_LOG_FILE` | 等价 `--log-level` / `--log-file` |

## 凭证

网关按顺序从两个来源加载 AK/SK：**环境变量 → `~/.huaweicloud/credentials`**（Windows：`%USERPROFILE%\.huaweicloud\credentials`）。两者同时配置时环境变量优先。

### 方式 A —— Profile 文件（快速开始主路径）

`~/.huaweicloud/credentials` —— 即[步骤 1](#步骤-1提供凭证)创建的文件：

```ini
[basic]
ak = your-access-key-id
sk = your-secret-access-key

# 可选 —— 按需取消注释：
# security_token = <temporary-security-token>
# project_id = <project-id>
# domain_id = <domain-id>
```

- `[basic]` 节含 `ak` 与 `sk` 为必需；三个可选键与下方环境变量一一对应。
- 文件缺失会被静默跳过 —— server 照常启动但没有凭证（元数据工具仍可用，见下）。
- 妥善保管 —— 文件里是你的密钥：macOS/Linux 执行 `chmod 600 ~/.huaweicloud/credentials`；Windows 下用户主目录中的文件默认仅本账户可读。

### 方式 B —— 环境变量（客户端注册内联）

改为在客户端注册中设置环境变量：

**opencode** —— 在 `command` 旁加 `environment` 块：

```json
"environment": {
  "HUAWEICLOUD_SDK_AK": "your-access-key-id",
  "HUAWEICLOUD_SDK_SK": "your-secret-access-key"
}
```

**Codex** —— 在 `--` 前加 `--env` 参数（单行命令，任意 shell 通用）：

```
codex mcp add huaweicloud --env HUAWEICLOUD_SDK_AK=your-access-key-id --env HUAWEICLOUD_SDK_SK=your-secret-access-key -- uvx huaweicloud-open-mcp --policy /home/you/hwc-policy.json
```

| 变量 | 用途 |
| --- | --- |
| `HUAWEICLOUD_SDK_AK` / `HUAWEICLOUD_SDK_SK` | 必需成对 |
| `HUAWEICLOUD_SDK_SECURITY_TOKEN` | 可选临时安全凭证 token |
| `HUAWEICLOUD_SDK_PROJECT_ID` | 可选；缺省自动解析 |
| `HUAWEICLOUD_SDK_DOMAIN_ID` | 可选；为全局级服务预留（完整支持开发中） |

### 行为要点

- 无凭证时元数据工具（`list_products`、`list_apis`、`get_api` 等）照常工作 —— 数据来自公开的 API Explorer；只有 `execute_api` 需要凭证。
- 使用专用最小权限 IAM 子用户的 AK/SK —— 网关能做的事不会超出该用户本身的权限。
- 签名在本地完成；SK 永不出本机，凭证永不入日志。

## 故障排查

| 症状 | 可能原因 | 处理 |
| --- | --- | --- |
| 客户端显示 "failed to connect" 或 server 立即退出 | `--policy` 是相对路径或文件缺失 —— policy 文件有问题时 server 快速失败 | 使用存在的文件的绝对路径 |
| 七个工具从未出现在 Agent 中 | `uvx` 不在客户端 PATH 上 | `which uvx`（macOS/Linux）或 `where uvx`（Windows）定位后在命令中改用绝对路径 |
| 元数据工具正常但 `execute_api` 失败 | 凭证未加载（`[basic]` 节为空，或环境变量覆盖了一个空文件） | 修正 `~/.huaweicloud/credentials`（见[凭证](#凭证)）或设置 `HUAWEICLOUD_SDK_*` 环境变量 |
| `execute_api` 返回 `{"ok": false, "reason": ...}` 且提及 policy | 该 API 未被 policy 文件允许（快速开始文件仅允许 `ECS:*List*`） | 编辑 policy 文件 —— 热生效无需重启 server —— 或（先向用户确认后）让 Agent 经 `manage_policy` 加规则 |
| 401 / SignatureDoesNotMatch | AK 或 SK 错误 | 核对正在生效的凭证来源（env 优先于 profile 文件） |
| 华为云返回 403 权限错误 | IAM 用户缺少该权限 | 为该 API 授予最小 IAM 权限 |
| 明明有服务器却返回 `"count": 0` | 资源在另一个 region | 带 `region` 再问，如 `cn-east-3` |
| Mock 调用挂起或超时 | 到 API Explorer 端点无网络路由 | 检查 `apiexplorer.cn-north-4.myhuaweicloud.com` 的代理/防火墙 |

更深入的诊断：注册命令中加 `--log-level DEBUG --log-file <日志文件>`（如 macOS/Linux 的 `/tmp/hwc-mcp.log`、Windows 的 `%TEMP%\hwc-mcp.log`）后查看日志文件。

## 文档

探索网关背后的设计：

| 文档 | 类型 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 总体设计（分层、模块、日志、测试） |
| [docs/mcp-openapi.md](docs/mcp-openapi.md) | openapi 模式设计（工作流、签名、OBS lane） |
| [AGENTS.md](AGENTS.md) | 贡献者约定（TDD 接缝、发布流程） |
| [benchmarks/README.md](benchmarks/README.md) | 工作流 benchmark 设计 |

## 开发

```bash
uv sync                                  # 安装依赖（含 dev）
uv run huaweicloud-open-mcp              # 源码运行
uv run pytest                            # 单测 + 集成（默认跳过 e2e）
uv run pytest -m e2e                     # 真实凭证 E2E
uv run ruff check src tests              # lint
uv run mypy src                          # 类型检查
```

配套 CLI：`api-refresh`（离线 APIE 管道：抓取 API Explorer → OpenAPI 2.0 文档，含帮助中心补全阶段 `helpdocs`/`helphints`）与 `api-docs`（终端元数据查询）。详见 [AGENTS.md](AGENTS.md)。

### 发布

发布区分 TestPyPI 与正式 PyPI 仓库，上传 URL 经 `pyproject.toml` 具名 index（`[[tool.uv.index]]`）固化：

```bash
scripts/publish test                      # TestPyPI（token：UV_PUBLISH_TOKEN_TEST）
scripts/publish prod                      # 正式 PyPI（token：UV_PUBLISH_TOKEN_PROD，带确认门；--yes 供 CI 跳过）
scripts/publish <test|prod> --skip-build  # 复用已有 dist/ 产物重发
```

说明：

- TestPyPI 与 PyPI 账号、API token 相互独立——分别在两站 Account Settings 申请；脚本严格按目标取用对应环境变量、无共享回退，凭证不会拿错。
- 每次构建先清空 `dist/`（`uv build` + `uvx twine check`），陈旧产物不会混入上传。
- `prod` 发布前打印目标 URL、项目版本与产物清单，需手动输入 yes 确认。
- 版本号在单个 index 内不可重复：已上传过的版本永不可重发（TestPyPI 验证通过后升版本号再发正式仓）。

## 许可证

[Apache-2.0](LICENSE)
