# API Explorer 元数据链路性能分析报告

- 范围：`src/apie/explorer.py`、`src/apie/catalog.py`、`src/apie/memory_store.py`、`src/apie/live_fallback.py`、`src/mcp_openapi/service.py`（元数据工具调用路径）
- 性质：只读分析，未改动任何代码
- 日期：2026-10-16
- 方法：代码走读（引用 `文件:行号`）+ 真实端点实测（apiexplorer.cn-north-4.myhuaweicloud.com，开放端点、无需凭证）+ 项目自身代码路径的端到端计时 + 离线 CPU 基准

---

## 1. 摘要（先看结论）

| 关键数字 | 数值 | 来源 |
| --- | --- | --- |
| 冷启动 `list_apis("ECS")` 端到端 | **11.0 s**（2 页 × ~5.5 s，串行） | 实测（项目自身 `fetch_apis` 计时） |
| `/v2/apis` 单页服务端延迟 | **~5.4–5.8 s**（与连接方式无关，服务端固有问题） | 实测 |
| `/v3/apis/detail` 单次延迟 | **60–90 ms**（其中 ~40 ms 为每请求新建 TCP+TLS 握手） | 实测 |
| `/v4/products` 单次延迟 | **~155 ms**（69 KiB） | 实测 |
| 最大产品冷启动 `list_apis("IDT")`（930 API = 10 页） | **~55 s**（推算，10 × 5.5 s） | 数据集统计推算 |
| 运行时路径对 HTTP 429 的重试次数 | **0 次**（单次尝试即把错误体当数据返回/抛出） | 单元级实测证明 |
| 转换 CPU 成本（`compose_doc`） | **~0.93 ms**/doc | 离线基准 |
| `format_api_detail`（每次 `get_api` 都执行） | **~0.41 ms** | 离线基准 |
| 详情 LRU 上限 500 条 ≈ 内存 | **~11 MiB**（平均 22 KiB/doc） | 离线基准 |

**一句话结论**：本链路的性能问题几乎全部在网络往返与架构编排层（**B1 串行分页 × 服务端 5.4s/页**、**B2 零 429 容忍**、**B3 无 single-flight**、**B4 无连接复用**），CPU 侧（转换/格式化/守卫）全部在 1 ms 量级，可以忽略。**~97% 的冷启动元数据等待时间来自 `/v2/apis` 索引端点**，而项目 wheel 内已经携带了一份等价数据（`configs/entity-index.json`，17,792 个 API 的 name/method/summary/tags），却未在运行时利用。

> **补充（2026-10-16，第 7 章）**：与切换前的 console 内部接口 A/B 实测证实，B1 的 5.4 s/页**为注册域 `/v2/apis` 特有**——console `/new/v3/apis` 同数据仅 120–300 ms/页（18–34×），且注册域对 HSS 索引当前**硬性 504 不可用**。详见第 7 章与新增建议 R9（索引双源）。

---

## 2. 数据流与时序（工具调用 → 远端拉取）

### 2.1 通用前置（每次工具调用）

```
MCP SDK dispatch（sync 工具经 anyio.to_thread 并发派发，memory_store.py:20-22）
  → service 层装饰器（service.py:55-78 _guarded；common/audit @audited）
      _guarded → guard_result（spill.py:169-197）：每次调用对整个结果信封做一次
                 json.dumps 计体积（实测 51 KiB 结果 ≈ 0.46 ms，预算内原样返回）
      @audited → 审计 NDJSON 落盘（不在本报告关注路径）
  → 业务方法
```

### 2.2 发现面工具（产品/接口索引）

```
list_products / get_product                service.py:395-423
  → catalog.get_products(store)            catalog.py:22-32
      MemoryStore.products()               memory_store.py:33-38（进程级缓存，命中即返回）
      未命中 → explorer.fetch_products()   explorer.py:94-102
        1 × GET /v4/products（实测 ~155 ms / 69 KiB）
      → store.set_products（进程存活期永驻，无 TTL）
  → metadata.list_products / get_product   metadata.py:28-69（纯内存 O(310) 扫描，µs 级）

list_apis(product)                         service.py:427-453
  → catalog.get_apis(store, product)       catalog.py:35-45
      MemoryStore.apis(product)            memory_store.py:45-48（按产品永驻，无上限）
      未命中 → explorer.fetch_apis(product) explorer.py:105-130
        while True: 串行 GET /v2/apis?offset&limit=100&productshort
        【实测每页 ~5.4–5.8 s 服务端延迟；翻页以响应 count + 空批终止】
      → store.set_apis（按产品永驻）
  → metadata.list_apis                     metadata.py:74-117（过滤/tag Counter/分页，µs 级）

search_apis                                service.py:345-367
  → 构建期快照 configs/entity-index.json + tantivy 内存索引（不触网，~600 ms 装配期构建）
```

### 2.3 详情面工具（get_api / get_api_examples / execute_api 共用）

```
get_api / get_api_examples / execute_api   service.py:457-481 / 495-536
  → service.load_api_doc                   service.py:140-153
  → catalog.find_api_doc                   catalog.py:48-75
      MemoryStore.find_api(product, api, region)   memory_store.py:54-61
        键 = (product_lower, api_name, region)，LRU 上限 500，命中 O(1)
      未命中 → LiveFallback(store, auth_demote, corrections).fetch   catalog.py:66-68
        → explorer.fetch_detail            explorer.py:133-149
            GET /v3/apis/detail?productshort&name&region_id
            【实测 60–90 ms；urllib 每请求新建 TCP+TLS ≈ 40 ms 开销】
            1055（带 region 查询）→ 自动去 region 重试一次（explorer.py:143-144）
            全局服务（is_global，53 个产品）首次必吃这个双往返
        → compose_doc                      doc_compose.py:23-33
            convert_api（fix_2_doc/convert_3_to_2 → finalize → demote_auth_headers
            → _complete_path_params → convert_ref → normalize_doc）+ correct_doc
            【实测 ~0.93 ms CPU】
        → ApiLocation.find(doc, api)       api_location.py:24-36（全 paths 扫描，µs 级）
        → store.set_api_cache(key, location)   ← 仅成功路径写入（live_fallback.py:53-54）
  → get_api：metadata.format_api_detail    metadata.py:177-227
      【每次调用重渲染，实测 ~0.41 ms；$ref 深度 2 内联 + 传递闭包】
  → execute_api：validate_params → 签名 → 云端执行（本报告范围外）
```

### 2.4 失败语义（与性能强相关）

- `catalog.get_products/get_apis/find_api_doc` 全部 `except Exception` → WARNING → 返回 `None`（catalog.py:30-32, 43-45, 72-75）→ 工具层转 `{ok:false, reason:...}`。
- **任何一页失败 = 整个产品的索引拉取作废**：`fetch_apis` 的累积结果在本地变量里，异常即丢弃，下次调用从 offset 0 全部重来。
- 详情未命中（`ApiNotFoundError`）**不写缓存**（live_fallback.py:39-40 直接 `return None`）→ 每次调用都重新打远端。

### 2.5 典型工作流的冷启动时序（渐进式工作流第 0–5 步）

```
search_apis("云服务器")        ~0 ms    （本地快照索引）
list_products()                ~155 ms  （1 × /v4/products，进程级缓存）
list_apis("ECS")               ~11.0 s  （2 × /v2/apis 串行分页；IDT 为 10 页 ≈ 55 s）
get_api(ECS, ListServers...)   ~80 ms   （1 × /v3/apis/detail + ~1 ms 转换）
get_api_examples(...)          ~0 ms    （LRU 命中）
execute_api(...)               ~0 ms 元数据 + 云端执行
```

冷启动元数据总等待 ≈ **11.3 s，其中 97% 来自 `/v2/apis`**。缓存全热后每次调用 ≈ 1 ms。

---

## 3. 测量方法与基准数据

### 3.1 网络实测（真实端点，2026-10-16）

| 探测 | 结果 |
| --- | --- |
| `GET /v4/products` | 155–163 ms，69 KiB |
| `GET /v3/apis/detail ECS:ListServersDetails` | 71–98 ms，27–36 KiB |
| `GET /v2/apis?offset=0&limit=100&productshort=ECS` | **5447–5808 ms**，74 KiB |
| `GET /v2/apis?offset=100&limit=100`（第 2 页） | **5457 ms**，24 KiB |
| `GET /v2/apis?limit=500` | **HTTP 400** `APIGW.0106 Invalid query parameter: limit, too large`（服务端上限，无法加宽分页） |
| keep-alive 单连接连打 `/v2/apis` ×3 | 5491/5598/5436 ms（**连接复用不改变 /v2/apis 延迟 → 服务端处理/限流固有问题**） |
| 快速连续第 2 次 `/v2/apis`（不同产品） | **HTTP 429 Too Many Requests**（限流阈值激进：约连续 3–4 个请求后即 429） |
| detail 端点 keep-alive vs 新建连接（稳态） | **22–25 ms vs 60–70 ms** → 每请求握手开销 ≈ 40 ms（≈ 详情延迟的 50%） |
| 项目自身 `fetch_apis("ECS")` 端到端（注入计时 spy） | **11.0 s，2 请求**（5605 + 5410 ms） |

### 3.2 数据集统计（configs/entity-index.json，构建期快照）

- 218 个产品、17,792 个 API；中位数 46 API/产品，最大 IDT 930。
- **>100 API（≥2 页）的产品：52/218**；>300 API（≥4 页）：10 个；>500：2 个（IDT 930、HSS 582）。
- ECS 索引计数 131（快照）vs 132（实时 count）——分页估算可信。

### 3.3 CPU 离线基准（uv run，真实 36 KiB raw detail fixture）

| 操作 | 中位耗时 |
| --- | --- |
| `compose_doc`（convert + correct，缓存未命中路径） | 0.93 ms |
| `ApiLocation.find` | 0.003 ms |
| `format_api_detail`（每次 get_api，含缓存命中） | 0.41 ms（输出 51 KiB） |
| `guard_result` 体积检查（每次工具调用） | 0.46 ms |
| 单 doc pickle 体积 | 22 KiB → LRU 500 ≈ 10.7 MiB |

### 3.4 429 行为单元证明（monkeypatch urlopen 抛 HTTPError 429）

```
fetch_json: attempts=1 returned_keys=['error_msg']
```

`open_url`（http.py:86-97）捕获 HTTPError 并返回 `(错误体, err)`，**`_retry` 的 429 指数退避分支（http.py:37-43）对 fetch_json 家族不可达**——`http.py:123` 与 `http.py:138` 的 `except urllib.error.HTTPError: raise` 是死代码。429 重试感知的 `fetch_json_429`（http.py:144-166，20 s 退避 ×10）**只被离线管道 `retry_failed.py:31` 注入**；运行时 `catalog → LiveFallback → explorer` 走默认 `fetch_json` / `fetch_json_retry`，均不重试 429。

---

## 4. 瓶颈清单（按影响排序）

### B1 · `/v2/apis` 串行分页 × 服务端 5.4 s/页 —— 冷启动 list_apis 的决定性瓶颈

> **2026-10-16 补注**：第 7 章 A/B 实测证实本瓶颈为**注册域特有**（console `/new/v3/apis` 同数据 120–300 ms/页，且注册域 HSS 索引当前恒 504）。R1（快照种子）仍为首选根治方案；R9（console 索引双源）为互补的在线回退方案。

- **证据**：
  - `explorer.py:105-130`：`while True` 串行翻页，`PAGE_SIZE = 100`（explorer.py:28），`offset += len(batch)`，页间无并行、无检查点。
  - 实测端到端 `fetch_apis("ECS")` = **11.0 s**（2 页）；服务端单页 5.4–5.8 s 且 keep-alive 无改善（服务端固有）。
  - `limit > 100` 被服务端 400 拒绝（`APIGW.0106`）——无法加宽分页。
  - 数据集：52/218 产品 ≥2 页；IDT（930 API，10 页）冷启动 ≈ **55 s**；HSS 582 ≈ 33 s。
- **量化影响**：agent 对话中每次浏览一个冷产品，`list_apis` 阻塞 11 s（中位产品 1 页也要 5.5 s）。典型「search_apis → list_apis → 翻页浏览」工作流在索引端点上耗时 5.5–55 s/产品。52 个大产品是重灾区。
- **放大器**：无磁盘/快照回退——进程重启或 LRU 外的新产品即回到冷启动（`memory_store.py` 纯内存，AGENTS.md 设计如此「不落盘」）。

### B2 · 运行时路径零 429 容忍 —— 可靠性 + 性能复合问题

- **证据**：
  - `http.py:86-97` `open_url` 捕获 HTTPError 返回 `(body, err)`，`_retry`（http.py:37-43）的 429 分支不可达；实测 `fetch_json` 在 429 下 **1 次尝试**，把 `{"error_msg":...}` 错误体当数据返回。
  - `explorer.py:118-120`：`fetch_apis` 见 `"apis" not in d` → `ValueError("unexpected apis response ...")` → **整个产品拉取作废**（已拉的页全部丢弃，catalog.py:43-45 吞异常返回 None，工具层报「接口索引不可用（远端拉取失败）」）。
  - `explorer.py:141` + `http.py:129-141`：detail 路径 `fetch_json_retry` 文档语义即「HTTP 错误（含 429）原样返回」，`explorer.py:147-148` 把 err 重新抛出 → 同样无重试。
  - 实测限流阈值激进：连续 3–4 个 `/v2/apis` 请求即 429。**两个并发工具调用浏览不同产品即可互相触发**（见 B3）。
  - 唯一 429 感知的 `fetch_json_429`（20 s 退避）只接线在离线 `retry_failed.py:31`。
- **量化影响**：一次 429 = 该工具调用硬失败 + 下次调用**从 offset 0 全量重拉**（11–55 s 重付 + 更高概率再次 429 的恶性循环）。HTTP 多会话部署（ADR-0003）下多 agent 并发浏览时为高概率事件。

### B3 · 无 single-flight：并发冷拉取同键不去重

- **证据**：
  - `catalog.py:22-32 / 35-45 / 48-75`：三处均为「查缓存 → miss 即拉 → 回写」，无 per-key 锁/共享 future；`LiveFallback` 每次调用新构造（catalog.py:66-68）。
  - `memory_store.py:20-22` 文档确认：MCP sync 工具经 anyio.to_thread **并发派发**；HTTP 传输多会话进一步放大。
- **量化影响**：N 个并发同产品冷 `list_apis` = N × (5.4 s × 页数) 网络开销 + N 倍 429 风险（而正确实现只需 1 次拉取、其余等结果）。对流式 HTTP 部署的多客户端场景是常规路径而非边角。

### B4 · 每请求新建 TCP+TLS 连接（urllib 无连接复用）

- **证据**：`http.py:86-97` `open_url` 每次调用 `urllib.request.urlopen`（每请求新连接）。实测 detail 稳态：新建连接 60–70 ms vs keep-alive 22–25 ms → **~40 ms/请求握手开销 ≈ 详情延迟的 50%**。
- **量化影响**：每个详情缓存未命中多付 ~40 ms。execute 密集型会话（每 API 一轮 get_api → execute，LRU 又被逐出时）累积显著；对 `/v2/apis`（5.4 s 服务端）无效。1055 兜底双往返（explorer.py:143-144，53 个全局产品首次必触发）再乘 2。

### B5 · 负结果不缓存 + 全局服务 region 双往返

- **证据**：
  - `live_fallback.py:36-55`：`ApiNotFoundError` → `return None`，**不写缓存**；而 `memory_store.py:63-64` 的 `set_api_cache(key, hit | None)` 值类型已支持 None——设计预留了负缓存，运行时从未使用。
  - `explorer.py:142-144`：带 region 的 1055 → 去 region 重试。`service.py:457-458` region 缺省取部署默认 → 53 个 is_global 产品的详情首拉恒为 2 × RTT（~160 ms）。
  - 缓存键含 region（`memory_store.py:56`）：agent 跨 region 对比时同 API 重复拉取+转换。
- **量化影响**：agent 打错/探测不存在的 API 名 → 每次调用 ~80–160 ms 远端往返，进程存活期内永不收敛；全局服务跨 region 场景翻倍。

### B6 · 详情 LRU 500 偏小（长会话逐出 → 重拉重转换）

- **证据**：`memory_store.py:25` `max_details=500`；语料 17,792 API；实测平均 22 KiB/doc → 500 条仅 ~11 MiB。
- **量化影响**：宽泛审计型任务（多产品 × 多 API）超出 500 工作集后，每次重访付 ~80 ms 网络 + ~1 ms 转换。提高到 2,000–5,000 仅需 45–110 MiB——对服务进程很便宜。

### B7 · 无启动预热/预取

- **证据**：`server.py:316-324` `build_openapi_app` 装配后无任何后台预热线程；首次 `list_products` 付 155 ms、首次 `list_apis` 付全额分页成本。
- **量化影响**：首个用户请求的体验完全暴露冷启动成本（与 B1 复合）。

### B8 · 次要 CPU 项（脚注级，全部 <1 ms，无需优先处理）

| 项 | 证据 | 量级 |
| --- | --- | --- |
| `guard_result` 每次全信封 `json.dumps` 计体积 | spill.py:79-85, 184 | 0.46 ms/次（51 KiB 结果） |
| `format_api_detail` 每次 `get_api` 重渲染（含 LRU 命中） | service.py:464；metadata.py:177-227 | 0.41 ms/次 |
| `metadata.list_apis` 对按产品缓存的全列表重复做 product 过滤（恒全命中） | metadata.py:79 | µs 级 |
| `ApiLocation.find` 全 paths 扫描 | api_location.py:24-36 | 3 µs |
| `_apis` 字典无上限（218 产品全枚举 ≈ 数十 MiB 常驻） | memory_store.py:28 | 内存足迹，非延迟 |

---

## 5. 改进建议（按优先级）

### R1（P0，治 B1+B7）：用构建期快照做索引种子 + 后台 stale-while-revalidate

**这是收益最大、风险最低的单项改动。** `/v2/apis` 的 5.4 s/页是服务端固有延迟，交互路径上唯一正确的解法是**不在交互路径上调用它**。

- wheel 已随包分发 `configs/entity-index.json`（17,792 API，含 name/method/summary/tags/product——恰为 `list_apis` 信封所需字段，仅缺 `info_version`）。两个可选实施：
  - **a（推荐）**：`MemoryStore` 增加可选 snapshot 种子（装配期从 entity-index 或 `raw/apis_docs.json` 派生 `api_basic_infos` 形制注入 `_apis`），启动后台 daemon 线程逐产品调 `fetch_apis`（页间加 `page_sleep`，explorer.py:105 已有此参数）做增量校验/更新，stale-while-revalidate：请求先吃快照（~0 ms），后台刷新完成后换值。
  - **b（最小）**：仅种子、不自动刷新（接受快照陈旧性，配合现有 `api_count` 对比提示）。
- **量化收益**：冷 `list_apis` 11 s（ECS）/ 55 s（IDT）→ **~0 ms**（快照命中）；后台全量刷新 178 页 × 5.4 s ≈ 16 min，对 daemon 完全可接受。
- **注意**：保持「远端为唯一真值源」的语义——快照只是缓存种子，`clear()`/缓存键语义不变；entity-index 的构建频率决定陈旧窗口。

### R2（P0，治 B2）：给运行时路径接入 429 感知重试 + 分页断点

- 现状是三重缺陷叠加：不重试（http.py:86-97 契约）→ 整批作废（explorer.py:118-120 ValueError）→ 从 0 重来（无断点）。修复分层：
  - **最小改动**：`explorer.fetch_apis` 识别 429 形态错误体（`error_code` 且无 `apis` 键）→ 以 20 s 级退避重试当前页（复用 `fetch_json_429` 语义），并以当前 offset 为断点续拉而非作废。
  - **结构性改动（推荐）**：让 explorer 的 fetcher 可注入（`fetch_detail` 已有 `fetch_json` 参数先例，explorer.py:133-134；给 `fetch_apis`/`fetch_products` 补同款），运行时装配注入 429 感知实现，与离线管道同 idiom。
  - **谨慎项**：把 `open_url` 改为对 429 重抛以激活 `_retry` 既有分支（http.py:37-43）语义上更正，但波及全部 fetcher 消费方，需逐一审计（real/mock/OBS lane 共用 `open_with_retry`，不受影响，但 `open_text`/帮助中心抓取依赖「错误返回不抛」契约）。
- **量化收益**：限流场景从「硬失败 + 11–55 s 全量重付」变为「一次 20 s 退避 + 从断点续拉」。

### R3（P1，治 B3）：single-flight（per-key 共享拉取）

- 标准做法：`catalog` 三入口（get_products/get_apis/find_api_doc）各持 per-key future 表（锁内注册、锁外拉取），首个调用者执行网络拉取，并发者等待共享结果。注意锁序：不与 `MemoryStore._lock` 嵌套（网络 I/O 已在 store 锁外，memory_store.py:22 设计如此，保持）。
- **量化收益**：N 并发同键冷调用：N × (5.4 s × 页) → 1 × (5.4 s × 页)；同时直接降低 B2 的 429 触发面（并发同键请求合并为 1 个远端序列）。
- **实现提示**：R1 落地后此项主要保护详情键（80 ms 级）与快照未覆盖产品，优先级可降半档。

### R4（P1，治 B4）：元数据抓取连接复用

- 方案 a：`common/http.py` 增加线程本地的 `http.client.HTTPSConnection` 池（host 维度，异常时重建），`open_url` 走池；方案 b：若接受新依赖，元数据 lane 换 `urllib3`（`PoolManager` 一行解决）。
- **量化收益**：详情未命中延迟 60–70 ms → 22–25 ms（**~40 ms/请求，≈ 减半**）；1055 兜底双往返场景 ×2 受益。
- **注意**：对 `/v2/apis` 无效（实测服务端 5.4 s 与连接无关）——不要期望它治 B1。

### R5（P2，治 B5）：负结果短 TTL 缓存 + 全局服务 region 键归一

- `live_fallback.py` 在 `ApiNotFoundError` / 非 dict / 无 paths 分支调用 `set_api_cache(key, None)`（类型已支持，memory_store.py:63-64），LRU 命中 None 即短路返回；建议配短 TTL（60–300 s）防「上游新上线 API 被负缓存钉死」，实现上可在 ApiHit 为 None 的条目旁挂时间戳或在 TTL 到期后允许一次重试。
- 全局服务（`is_global=true`，53 产品）：详情拉取 region-insensitive 化——先查 `(product, api, default_region)` 或维护 region 无关次级键。风险提示：doc 的 host/端点随 region 变化，需确认 `format_api_detail` 消费的 region 相关字段后再归一，或仅对 definitions/paths 共享、host 单独计算。
- **量化收益**：错误 API 名探测从「每次 80–160 ms」收敛为「每 TTL 一次」；全局服务跨 region 减半拉取。

### R6（P2，治 B6）：LRU 上限提升或可配置

- `MemoryStore(max_details=500)` → 2,000–5,000（45–110 MiB），或暴露 `HUAWEICLOUD_MCP_DETAIL_CACHE` env。一行改动，无语义风险。顺带可评估缓存 `format_api_detail` 输出信封（省 0.41 ms/次——仅在高频 get_api 场景值得，脚注级）。

### R7（P2，治 B7）：启动预热

- 后台 daemon 线程在装配完成后立即：拉 `/v4/products`（155 ms）写入 store；可选预热默认 region 的 top-N 常用产品索引。若 R1a 落地则本项被吸收（快照 + 后台刷新即预热）。

### R8（P3，明确不建议 / 低优先）

- **并行分页 `/v2/apis`：不建议**。实测限流阈值激进（连续 3–4 请求即 429），5.4 s 为服务端处理时间而非可绕过的排队；并行只会放大 429 面（且在 R2 之前一次 429 即整批作废）。若 R2 落地后仍想尝试，应以 2 并发 + 断点为上限做 A/B。
- **detail 预取（低优先，可选）**：`/v3/apis/detail` 未见限流（连续 5+ 请求正常）。若做「list_apis 当前页 lookahead 预取详情」（20 API × 80 ms 串行 = 1.6 s，4 并发 ≈ 0.4 s），须与 LRU 容量（R6）联动，否则预取反而挤出工作集。
- **CPU 侧微优化（B8 全部）**：当前全部 <1 ms，相对网络可忽略；仅在 profile 证明热路径后才值得动 `guard_result`/`format_api_detail`。

---

## 6. 优先级矩阵

| 建议 | 治理瓶颈 | 收益 | 成本/风险 | 优先级 |
| --- | --- | --- | --- | --- |
| R1 索引快照种子 + 后台刷新 | B1, B7 | 冷 list_apis 11–55 s → ~0 ms | 中（种子派生 + daemon 线程，快照陈旧性需运营约定） | **P0** |
| R2 429 重试 + 分页断点 | B2 | 硬失败 → 退避续拉；消除恶性重拉循环 | 低-中（fetcher 注入 idiom 已有先例） | **P0** |
| R3 single-flight | B3 | N 并发同键 → 1 次拉取 | 低（catalog 层 per-key future） | P1 |
| R4 连接复用 | B4 | 详情未命中 ~减半（-40 ms/请求） | 低 | P1 |
| R5 负缓存 + region 键归一 | B5 | 错误名/跨 region 场景收敛 | 低-中（region 归一需核实 doc 差异面） | P2 |
| R6 LRU 提升 | B6 | 长会话免重拉 | 极低 | P2 |
| R7 启动预热 | B7 | 首请求体验 | 极低（可并入 R1a） | P2 |
| R8 并行分页 / detail 预取 | — | — | — | P3 / 不建议（并行分页） |
| R9 console 索引双源/回退（见第 7 章） | B1, B2 | 冷 list_apis 5.4 s/页 → 0.12–0.3 s/页；HSS 等死产品复活 | 中（双源方言适配 + 内部接口无 SLA 风险） | P1（作为 R1 的在线互补） |

**落地顺序建议**：R2（止血）→ R1（根治冷启动）→ R3+R4（并发与连接）→ R5/R6/R7（长尾）。R1+R2 完成后，交互路径上 `/v2/apis` 仅存在于后台刷新线程，元数据链路的用户可见延迟将从「秒级到分钟级」收敛到「详情首拉 ~40 ms（R4 后）+ 其余 ~0 ms」。R9 可与 R1 并行评估——若接受双源复杂度，在线冷启动可直接降一个数量级。

---

## 7. 与历史 console 接口的 A/B 性能对比（2026-10-16 补充）

背景：commit `1508c59`（2026-09-12）把元数据源从 console 前端内部接口切到注册域。切换动机（commit message）：① `is_global` 修复——v5 误标 53 个全局服务 false；② `api_count` 恢复真值——v5 恒 0。本章用同场景 A/B 实测回答：B1（`/v2/apis` ~5.4 s/页）是否为注册域特有？回切/双源是否值得纳入建议？

对比端点（历史实现见 `git show 1508c59^:src/apie/catalog.py` / `live_fallback.py`）：

| 数据 | console（切换前） | registered（现行） |
| --- | --- | --- |
| products | `console.huaweicloud.com/apiexplorer/new/v5/products` | `apiexplorer.cn-north-4.myhuaweicloud.com/v4/products` |
| apis 索引 | `/new/v3/apis?offset&limit&product_short=`（信封 `api_basic_infos`） | `/v2/apis?offset&limit&productshort=`（信封 `apis`） |
| detail | `/new/v4/apis/detail?product_short=&name=&region_id=` | `/v3/apis/detail?productshort=&name=&region_id=` |
| count | `/new/v1/products/apis/count` | （无等价端点，v4/products 派生） |

方法：基准脚本全部置于 `/tmp/opencode/`（不入库）；全部请求带浏览器 UA；单项延迟取样间隔 ≥8 s 规避限流干扰、每组 3 次取中位数；串行分页与限流探测单独进行。

### 7.1 端点可达性

四个 console 端点**今天全部仍可达**（HTTP 200，带浏览器 UA）：products v5（210 ms / 69 KiB）、count v1（106 ms / 9 KiB / 218 产品）、apis v3 p0（240 ms / 21 KiB / batch=100）、detail v4（159 ms / 27 KiB）。

### 7.2 同场景 A/B 延迟（3 次取样中位数）

| 场景 | console 中位 | registered 中位 | reg/con |
| --- | ---: | ---: | ---: |
| products 全量 | 184 ms（331/125/184） | 122 ms（283/122/107） | **0.7×（reg 更快）** |
| ECS 索引 page0（limit=100） | **160 ms**（160/121/160） | **5489 ms**（5481/5489/5504） | **34.4×** |
| ECS 索引 page1（offset=100） | **300 ms**（300/138/306） | **5517 ms**（5517/5472/5542） | **18.4×** |
| HSS 索引 page0 | **146 ms**（146/122/160） | **HTTP 504 恒失败**（3/3 次，60 s 后 APIGW.0201 上游 30 s 超时） | **∞（不可用）** |
| HSS 索引 page5（offset=500） | **138 ms**（210/138/129） | **HTTP 504**（同上） | **∞** |
| detail ECS:ListServersDetails | 150 ms（152/150/144） | **72 ms**（166/72/71） | **0.5×（reg 更快）** |

补充实测：

- registered `/v2/apis` 延迟**方差巨大**：8 连发 limit=1 最小请求延迟 2.9–23.2 s（5495/10589/23246/3949/10516/2941/3683/3455 ms）；间隔干净的稳态 ~5.4–5.5 s。
- **HSS 在注册域全 offset/limit 组合恒 504**（off=0 lim=10 / off=0 lim=50 / off=100 lim=100 / off=200 lim=100 全部 APIGW.0201「Service APIEXPLORER timed out (30000 ms)」）——非分页位置问题，后端对该产品整体不可用；同产品 console 146 ms/页正常返回（count=582 与 v4 一致）。IDT（930）在注册域 8.3 s/页可用、OBS（94）4.6 s。
- 根因排除：**不是载荷体积**——注册域索引条目虽带 27 个键（含 paths/definitions/components/swagger 字段），但实测全部为空（100 条中 paths/definitions/parameters 非空数均为 0），整页 69 KiB vs console 20 KiB（条目中位 687 B vs 197 B）；5.4 s 为注册域后端处理成本，与传输量无关。

### 7.3 串行全量分页（真实冷启动口径，无页间休眠）

| 产品（页数） | console | registered |
| --- | --- | --- |
| IDT（930 API / 10 页） | **1.3 s 完整取回 930 条**（页中位 114 ms，0 错误） | **28.4 s 后于第 5 页 HTTP 429 中断**（仅取 400/930 条；按运行时语义整批作废→工具报「接口索引不可用」） |
| ECS（132 API / 2 页） | ~0.3–0.5 s（2 × 120–300 ms） | 11.0 s（2 × 5.5 s，本报告 §3.1 项目代码实测） |

结论：注册域口径下 IDT 冷启动**不是慢，是失败**（429 × 整批作废 × 无重试，B1+B2 复合）；console 口径 1.3 s 成功。

### 7.4 限流行为对比

| 维度 | console `/new/v3/apis` | registered `/v2/apis` |
| --- | --- | --- |
| 连续无间隔请求 | **15/15 全部 200**（100–200 ms/次，不同产品 limit=1） | **~3–5 连发即 429**（两次独立观测：3 成功第 4 次 429；串行分页 4 页成功第 5 页 429） |
| 429 后恢复 | 未观测到 429 | 窗口短（数十秒级）：429 后 ~1 min 的后续请求得到 504/200 而非 429；慢间距连发（3–23 s 间距）8/8 无 429（长延迟客观上自节流） |
| 慢间距连发 | 15/15 无异常，延迟稳定 100–300 ms（1 个 2 s 离群） | 8/8 无 429 但延迟 2.9–23.2 s |

### 7.5 分页参数与单页上限

console `/new/v3/apis` 与 registered `/v2/apis` **同样受 APIGW 编排层 limit≤100 硬上限**（limit=200/500/1000/2000 均 HTTP 400 `APIGW.0106 "Invalid query parameter: limit, too large"`）。两者页数相同；差异纯在每页延迟（114–300 ms vs 5.4 s+）。两域索引信封与参数名不同（`api_basic_infos`/`product_short` vs `apis`/`productshort`，method 大小写 POST vs post）——即 `explorer.py` 已归一的方言差异，条目核心字段（name/method/summary/tags/info_version）语义一致，batch/count 实测一致（ECS 132/132、HSS 582/582、IDT 930/930）。

### 7.6 数据质量核验（切换动机今日是否仍成立）

| 项 | console v5（切换动机所指缺陷） | registered v4 | 结论 |
| --- | --- | --- | --- |
| `api_count` | **仍恒 0**（307/307 产品全为 0） | 真值（89/307 为 0 = 无 API 产品，其余有值：ECS 132/RDS 361/IDT 930/HSS 582） | 缺陷今日仍在 |
| `is_global` | **仍误标**（v5≠v4 共 53 个：IAM/CDN/TMS/BSS/AAD/CCM/EdgeSec… v5 全 false，v4 正确 true） | 正确 | 缺陷今日仍在 |
| count 替代源 | **v1/products/apis/count 仍可用**（106 ms，218 产品真值：ECS 132/RDS 361/IDT 930/HSS 582，与 v4 一致） | — | console 侧可经 v1 补齐计数（旧管道 count 阶段正是此源） |
| 产品字段形状 | 与 v4 完全同键（attributive_product/description/has_data/icon/is_global/is_recommend/link/name/productshort） | 同左 | 无形状差异，纯数据差异 |
| detail parity | paths/definitions **逐字节一致**（4/4 抽样：ECS ListServersDetails/CreateServers、VPC ListVpcs、RDS ListInstances；顶层仅键名方言 productshort↔product_short 与 6 个展示字段差异） | 同左 | commit message 的 parity 主张今日仍成立 |

**关键归因**：切换动机的两个数据质量缺陷都**只在 products 端点（v5）**——索引（v3）与 detail（v4）在 console 侧无任何质量劣势（索引计数一致、detail 逐字节一致）。而性能病理**只在注册域的索引端点（/v2/apis）**——products 与 detail 注册域反而更快（122 vs 184 ms、72 vs 150 ms）。

### 7.7 结论与建议

**Q：B1（/v2/apis ~5.4 s/页）是否为注册域特有？**
**是。** 同一数据的 console `/new/v3/apis` 为 120–300 ms/页（18–34× 差距），稳态、无方差爆炸、无限流（15 连发无 429）；注册域额外存在 HSS 全量 504 硬不可用与 ~3–5 连发 429。B1 的 5.4 s 与后端处理相关而非载荷（69 KiB vs 20 KiB，swagger 字段全空）。

**Q：console 明显更快，回切/双源是否值得纳入建议清单？**
**值得，但形态应为「按端点分域的双源」而非整体回切**，理由：

1. **质量维度不支持整体回切**：v5 products 的 api_count 恒 0 与 is_global 53 处误标今日仍复现——当年切换动机仍然成立，products 必须留在注册域 v4（v1 count 可作计数交叉源）。
2. **速度维度要求索引换源**：索引是唯一病理端点，console v3 快 18–34× 且限流宽松；detail 两域 parity 且注册域更快（72 ms），无换源必要。
3. **因此最优形态**：products=注册域 v4（质量）；apis 索引=console v3 优先、注册域 v2 兜底（速度+可用性，HSS 类死产品自动经兜底或直接走 console 存活）；detail=注册域 v3（速度）。方言适配成本可控——`explorer.py` 已是方言归一深模块，v3 的 `product_short` 参数名/`api_basic_infos` 信封/method 大写三处差异与既有归一同 idiom（`_normalize_index_batch` 的逆向），历史实现（`git show 1508c59^:src/apie/catalog.py`）即现成参照。
4. **风险须如实计入**：console `/new/vN` 是**前端内部接口，无公开 SLA**，可能未经通知变更或下线（这也是当初切到注册域的合理动机之一）。双源必须做成「console 失败自动回落注册域」的方向（快路径可牺牲，慢路径保底），并在连续失败时熔断 console 源避免每次请求都双倍等待。内部接口另带浏览器 UA 要求（实测裸 urllib 默认 UA 也可达，但沿用 `common.http.HEADERS` 的 Mozilla UA 更稳妥）。

**新增建议 R9（P1，与 R1 互补）**：索引双源——`explorer.fetch_apis` 增加 console v3 数据源（优先）+ 注册域 v2 兜底，方言归一并入 explorer 深模块；预期冷 `list_apis` 从 5.4 s×页（或 504 失败）降为 0.12–0.3 s×页，HSS 类产品恢复可用。若 R1（快照种子+后台刷新）已落地，R9 的价值转为「后台刷新提速 + 快照陈旧窗口缩短」，优先级可降为 P2。**不建议整体回切 console**（丢 v4 质量），也不建议 products/detail 换源（注册域更快且质量更好）。

### 7.8 对第 4 章瓶颈清单的修订

- **B1** 定性修订：5.4 s/页 + HSS 504 + 连发 429 均为**注册域 /v2/apis 端点特有**，非 API Explorer 数据源固有（console 同数据 120–300 ms/页）。量化影响章节的数字（ECS 11 s / IDT 55 s 推算）不变，但 IDT 的真实结局比推算更糟（实测第 5 页 429 整批作废=硬失败）。
- **B2** 影响加重：注册域限流阈值（~3–5 连发）意味着**串行分页本身就可能在中途触发 429**（IDT 实测），当前零重试行为把「慢」升级为「失败」。
- 其余瓶颈（B3–B8）与数据源无关，结论不变。



```
# 端点延迟（真实端点，间隔请求避免限流）
/v4/products clean:               155 ms  69 KiB
/v2/apis ECS page0 clean:         5447 ms 74 KiB
/v2/apis ECS page1 clean:         5457 ms 24 KiB
/v3/apis/detail clean:             71 ms  27 KiB

# limit 上限探测
limit=500 ECS: HTTP 400  APIGW.0106 "Invalid query parameter: limit, too large"

# 项目自身 fetch_apis 端到端（注入计时）
fetch_apis('ECS') TOTAL: 11.0s, apis=132, requests=2
  req /apis: 5605 ms
  req /apis: 5410 ms

# 连接复用对照（detail 端点稳态）
new-conn (urllib, project): 60-70 ms   keep-alive: 22-25 ms   → 握手 ~40 ms/req

# 429 单元证明（monkeypatch urlopen 抛 HTTPError 429）
fetch_json: attempts=1 returned_keys=['error_msg']

# CPU 基准（36 KiB raw detail fixture）
compose_doc:        median 0.93 ms
format_api_detail:  median 0.41 ms（输出 51 KiB）
guard_result:       median 0.46 ms（51 KiB 信封）
doc pickle:         22 KiB → ×500 LRU ≈ 10.7 MiB

# 数据集（configs/entity-index.json）
218 products / 17,792 apis；中位 46 API/产品
>100 API: 52 产品；>300: 10；IDT 930（10 页）/ HSS 582（6 页）
ECS 快照 131 vs 实时 count 132
```

## 附录 C · console vs registered A/B 实测原始输出摘录（2026-10-16，第 7 章数据源）

```
# 端点可达性（console，浏览器 UA）
products v5: HTTP 200 210 ms 69 KiB    count v1: HTTP 200 106 ms 9 KiB
apis v3 p0: HTTP 200 240 ms 21 KiB     detail v4: HTTP 200 159 ms 27 KiB

# A/B 延迟（3 取中位，间隔 8 s）
products:    con 184 ms   reg 122 ms                （reg 快 0.7×）
ECS idx p0:  con 160 ms   reg 5489 ms               （34.4×）
ECS idx p1:  con 300 ms   reg 5517 ms               （18.4×）
HSS idx p0:  con 146 ms   reg 504 ×3（APIGW.0201 上游 30 s 超时）
HSS idx p5:  con 138 ms   reg 504
detail:      con 150 ms   reg 72 ms                 （reg 快 0.5×）

# HSS 注册域全组合 504
off=0 lim=10 / off=0 lim=50 / off=100 lim=100 / off=200 lim=100 → 全部 504
IDT page0 reg: 8349 ms（可用）；OBS page0 reg: 4586 ms

# 串行全量分页（无页间休眠）
console IDT: 10 页 930 条 1.3 s（页中位 114 ms，0 错误）
registered IDT: 第 5 页 HTTP 429 中断，28.4 s 仅 400/930 条（运行时整批作废）

# registered /v2/apis 延迟方差（8 连发 limit=1）
5495 / 10589 / 23246 / 3949 / 10516 / 2941 / 3683 / 3455 ms（8/8 无 429，长延迟自节流）

# 限流对比
console v3: 15 连发（不同产品 limit=1）15/15 HTTP 200（100–200 ms，1 个 2 s 离群）
registered v2: 3 成功第 4 次 429（一次观测）；4 页成功第 5 页 429（串行分页观测）

# 分页上限（两域同限）
console limit=200/500/1000/2000 → HTTP 400 APIGW.0106 "limit, too large"
registered limit=500 → HTTP 400 APIGW.0106（§3.1 既有观测）

# 载荷排除（5.4 s 根因非体积）
reg 索引条目 27 键（paths/definitions/parameters 非空数 0/100），页 69 KiB，条目中位 687 B
con 索引条目 8 键，页 20 KiB，条目中位 197 B

# 数据质量（今日核验）
v5 api_count: 307/307 全 0；v4: 89/307 为 0（无 API 产品），ECS 132/RDS 361/IDT 930/HSS 582
v1 count: 218 产品真值（ECS 132/RDS 361/IDT 930/HSS 582，与 v4 一致）
is_global v5≠v4: 53 个（IAM/CDN/TMS/BSS/AAD/CCM/EdgeSec/SCM/CCM… v5 false / v4 true）
detail parity: 4/4 抽样 paths/definitions 逐字节一致（ECS×2/VPC/RDS）

# 索引条目方言
con v3 键: alias_name/id/info_version/method/name/product_short/summary/tags（method 大写）
reg v2 键: 27 键含空 swagger 字段（method 小写，id 命名空间不同）
```

基准脚本（不入库）：`/tmp/opencode/ab_reach.py`、`ab_baseline.py`、`ab_hss_retry.py`、`ab_hss_narrow.py`、`ab_sequential.py`、`ab_limit_burst.py`、`ab_reg_burst.py`、`ab_quality.py`、`ab_payload.py`、`ab_detail_parity.py`。

## 附录 B · 关键代码位置索引

| 主题 | 位置 |
| --- | --- |
| /v2/apis 串行分页循环 | src/apie/explorer.py:105-130（PAGE_SIZE=100 :28） |
| 429 错误体当数据（无重试根因） | src/common/http.py:86-97（open_url 捕获返回）；死分支 :37-43, :123, :138 |
| fetch_apis 整批作废点 | src/apie/explorer.py:118-120（ValueError）；catalog 吞异常 catalog.py:43-45 |
| 429 感知 fetcher 仅离线接线 | src/apie/retry_failed.py:31（fetch_json_429）；运行时默认 explorer.py:141 |
| 无 single-flight | src/apie/catalog.py:22-32, 35-45, 48-75（查-拉-写，无 per-key 锁） |
| 每请求新连接 | src/common/http.py:88-91（urlopen per call） |
| 1055 双往返 | src/apie/explorer.py:143-144 |
| 负结果不缓存 | src/apie/live_fallback.py:39-40（None 直接返回）；类型已支持 memory_store.py:63-64 |
| LRU 500 上限 | src/apie/memory_store.py:25（max_details） |
| 详情缓存键含 region | src/apie/memory_store.py:56 |
| guard 体积检查 | src/mcp_openapi/spill.py:79-85, 184 |
| format_api_detail 每次重渲染 | src/mcp_openapi/service.py:464；src/apie/metadata.py:177-227 |
| 工具并发派发 | src/apie/memory_store.py:20-22（anyio.to_thread 文档） |
