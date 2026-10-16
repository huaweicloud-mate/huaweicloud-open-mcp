# 实施设计：元数据链路止血（R2 + R9）· 修订版（Round 0 检视后）

依据：`analysis-report.md`（本 worktree，含第 7 章 console A/B 对比）。检视记录见 `review-log.md`（Round 0：三方 10+10+10 findings，act 8 项全部吸收）。
范围圈定：**R2（429 感知重试，页级）** 与 **R9（索引双源：console 优先 + 注册域页级回落）**，运行时路径。R1（快照种子 + 后台刷新）单独立项；R3/R4 与数据源正交，不混入本轮——Laziness Protocol：一轮只做同一接缝族的最小变更。

## 消费者与继承者视角

对 agent 用户（经 list_apis 工具消费）：冷启动 `list_apis` 从「5.4 s × 页（HSS 恒失败、IDT 第 5 页 429 整批作废）」变为「console 健康 ~0.15 s × 页；console 退化时每页多付 ≤5 s 探测（仅首页，粘性后短路）」；429/网络错从硬失败变为退避重试。对下一个接手者：双源方言归一全沉 `explorer.py` 深模块，`catalog` 及以上零感知（仅 monkeypatch seal 点变更）；管道构建产物保持注册域单源（可复现性不受 console 无 SLA 影响）。

## 数据形状（先定类型）

一次「页拉取尝试」的结局是三态：成功（dict 信封）/ 瞬时错（429、网络错——退避重试）/ 终态错（非 429 HTTP、非 JSON 体——立即失败或回落）。现状把三态压扁成 dict-only 契约导致 429 被误当数据。

```python
# common/http.py —— fetch_json_429 的薄适配（零新重试环，节奏由底座单点维护）
def fetch_json_429_raise(url, *, retries=..., backoff_429=..., backoff=..., timeout=...) -> dict:
    """dict-only 抛错契约：成功 → body；429/网络错 → 底座退避后仍失败 → 重抛；
    非 429 HTTP 错 → HTTPError 重抛。实现 = fetch_json_429 + err 通道转异常。"""
```

console 索引方言（第 7 章实测独立真值）：参数 `product_short`（下划线）、信封 `api_basic_infos`、method 大写、`count` 键同名。归一后核心字段（name/method/summary/tags/info_version/product_short）与注册域一致；键集随源异质（console 8 键无空 swagger 字段，注册域 27 键）——「出口恒为注册域形制」措辞按检视弱化。

## 模块接缝

### 接缝 A（R2）：`common/http.py` 新增 `fetch_json_429_raise`——adapter，非第四条重试环

- 实现：`body, err = fetch_json_429(url, ...); if err is not None: raise err; return body`。零新重试逻辑，429/网络退避节奏由 `fetch_json_429` 单点维护（消除原设计 retries=5 vs 10 自相矛盾——Round 0 act #4）。
- 参数透传 `fetch_json_429` 缺省（retries=10, backoff_429=20.0, backoff=5.0, timeout=30）。
- 终态错误类型变化（已知行为变更，记档）：今日 502 经 `open_url` 变错误体 → explorer ValueError；换 adapter 后 HTTPError 重抛。catalog `except Exception` 全吞，工具层同为 `{ok:false, reason}`，仅 reason 文案变。
- 不改 `open_url`/`fetch_json`/`fetch_json_retry` 契约。`open_url` 消费方核实为仅三个 `fetch_json*`（`open_text`/mock lane 不经它——原设计波及面陈述有误，方向不变）。
- 测试 seal 约定：索引腿封网点 = monkeypatch `http.fetch_json_429`（adapter 的底座，封底座即封 adapter）；54 处既有 `fetch_json` seal 继续覆盖 products 腿。写进测试文件头注释与 AGENTS.md S5。

### 接缝 B（R9）：`apie/explorer.py` `fetch_apis` 双源 + 页级回退 + per-call 粘性

分页循环骨架（count+空批终止、offset 前进、page_sleep 页间节流）不变。变更：

1. **不对称重试预算（Round 0 act #1）**：console 腿单次尝试、`timeout=5`（健康页 120–300 ms）、零重试——回落即重试。429/网络退避梯只挂注册域腿（经 adapter）。挑战页/HTML（JSONDecodeError）、401/403/504（HTTPError）、连接挂起（timeout）在 console 腿统一 `except Exception` 页级回落（act #6/#7 合并处置）。
2. **per-call 源粘性**：同一次 `fetch_apis` 调用内 console 首败后，本产品剩余页直接走注册域（~3 行局部变量，无全局状态、无并发故事——act #1）。混合窗口收窄到单次源切换/产品。
3. **URL/信封**：`CONSOLE_APIS_BASE`，参数 `product_short`；信封探测宽容——`api_basic_infos` 或 `apis` 任一存在即用（缺则映射为空列表交由空批终止/回退判定），`_console_batch` 只做信封键适配，字段归一复用 `_normalize_index_batch`（method 大写由其小写化处理）。
4. **429 感知缺省 fetcher**：`fetch_apis` 的 `fetch_json` 注入缺省从 `http.fetch_json` 改为 `http.fetch_json_429_raise`（仅注册域腿使用；console 腿固定单次 `open_url` 直调或带 timeout 的浅封装）。既有注入 fake 的测试不受影响。
5. **`console_first: bool = True` 参数**：管道 `fetch_apis.py:18` 显式传 `console_first=False`——构建产物（raw/apis_docs.json → entity-index）保持注册域单源，可复现性不绑 console 无 SLA；`source` 字段不再谎报（act #5）。运行时 catalog 缺省 True。
6. **出口去重防御**：`(name, method)` 去重，命中记 WARNING（跨源排序 parity 未实测验证——Round 0 Consider 项的廉价防御，把静默腐蚀变成可观测信号）。
7. `page_sleep` 语义写明：节流页、不节流页内回退；回退页计一页（Round 0 C#10）。

**终端失败语义（Round 0 act #2，明确）**：两源均失败（注册域腿重试耗尽）→ 抛异常、零缓存（今日语义不变）。「断点」更名为**页级重试/页级回退**——进程内已拉页不回滚，跨调用无 checkpoint（next call 从 offset 0 重来，记入不做的；console 健康时重付 ~1.3 s/IDT 可接受）。

### 接缝 C：`catalog.py` 零代码改动

出口契约不变。既有 catalog 测试中 `test_get_apis_remote_fetch`/`test_get_apis_remote_error` 的 monkeypatch 目标从 `explorer.http.fetch_json` 改为 `explorer.http.fetch_json_429`（seal 底座）——测试适配，非代码变更（act #3）。

## 测试接缝（对照 AGENTS.md S 系列，变更前列出）

S5 行（apie.explorer 注册域数据源）追加：「console v3 索引双源（console 优先页级回落 + per-call 粘性 + 429 感知缺省 fetcher adapter）」。S5 catalog 行追加 seal 点变更说明。

**兼容表（对照实际断言重做，Round 0 act #3）**：

| 既有测试 | 预判 | 处置 |
| --- | --- | --- |
| `test_fetch_apis_paginates_until_count`（:179-180 断言 `productshort=` URL 字面量） | **必红**（call 0 变 console URL `product_short=`） | 适配：URL 断言改 console 形，或断言序列 [console, console] |
| `test_fetch_apis_empty_product_single_call`（:190-191 `len(calls)==1`） | 宽容信封下 fake 的 `{"apis": []}` 在 console URL 上可用 → 空批终止 | 仍绿（宽容信封决策保住）；补 console 形 fake 用例 |
| `test_fetch_apis_missing_count_falls_to_empty_break` / `test_fetch_apis_page_sleep` | fake 按 offset 解析、sleep 断言与源无关 | 仍绿 |
| `test_fetch_apis_error_body_raises`（:205-207） | fake 恒返 `{"error_code": "X"}`：console 空 → 回落注册域也空 → 抛 | 仍绿（异常类型不变）；调用数 1→2 无断言 |
| `test_get_apis_remote_fetch`/`_remote_error`（catalog） | monkeypatch `fetch_json` 被缺省 swap 绕过 | **适配**：patch 目标改 `fetch_json_429` |
| 54 处 service/elicit/composite/http_sessions seal | store 预填充，无一冷拉索引 | 仍绿；seal 语义变更写档 |

新增矩阵（fake fetcher，不联网；独立真值 = 第 7 章实测形制字面量）：
1. console 优先成功（URL 序列 + 出口归一形制：`api_basic_infos` + method 大写 → product_short/method 小写）。
2. console 页失败（网络错）→ 该页注册域回落 → 后续页粘性直走注册域。
3. console 返回 HTML 字符串（JSONDecodeError 类）→ 立即回落（无退避）。
4. console 429/401/504 → 立即回落（单次尝试）。
5. 注册域腿 429 → 底座退避后成功（fake 计数 + fake sleep 序列）。
6. 两源均终态失败 → 抛异常、零缓存。
7. `console_first=False`（管道）：URL 序列恒注册域。
8. 出口 `(name, method)` 去重 + WARNING。
9. adapter 矩阵（接缝 A）：429 一次后成功 / 429 耗尽重抛 / 网络错退避 / 非 429 即抛 / 正常返回。

## 不做的（驳回预记录，含 Round 0 异议如实记录）

- **全局熔断器/健康记分**：per-call 粘性为其最小形态；完整形态是 R1 后台刷新语境优化。三方异议已听取（A#1/B#1/C#2），以不对称预算 + 粘性替代。
- **env 旋钮 `HUAWEICLOUD_MCP_APIS_SOURCE`**：三方质疑驳回理由（A#1/C#9/B#10）。修订后理由：不对称预算下 console 退化成本 = 每页一次 5 s 探测且粘性短路（仅首页付）；uvx 消费者的紧急逃生留给 console 实际变卦时随修复发布（wheel 发版节奏 < 典型内部接口存续期）。若实测打脸，旋钮是一行 `is_off` 先例的低成本补课。
- **跨调用 checkpoint / partial-return**：需要穿透 catalog/store/信封的 completeness marker（C#1 方向明确驳回——超出止血预算，今日抛异常语义保留）。
- **跨源 ordering parity 实测验证**：不阻塞本轮（混合窗口已被粘性收窄到单次切换 + 去重防御 + 管道单源）；R1 落地时随后台刷新 A/B 验证。
- **空批 roster 漂移交叉校验**（C#7）：需要 products 数据穿透 explorer 违背分层；运行时 `total==0` 有自然可见性。记已知边界。
- **fetch_products/detail 腿 429 感知**（C#6 尾/B#8）：实测两腿无限流且快（155 ms/72 ms、进程级缓存单次）；detail 重试语义由 `fetch_json_retry` 家族既有行为承担。真发生时症状自愈（重试即愈），届时一并切 adapter。
- **jitter**：羊群场景依赖 R3（single-flight）缺位 + HTTP 多会话并发冷拉同键，量级低；挂 R3。

## 已知边界

- 混合源列表键集异质（console 8 键 vs 注册域 27 键、id 命名空间不同）——仅运行时内存 cache 可见，核心字段归一一致，无消费者读 `id`（核实：metadata.py:79-104、build_entity_graph 均用核心字段）。
- 终态错误 reason 文案变化（ValueError → HTTPError 路径）。
- console 侧 count 漂移理论上可早终止（终止读当前页 count，两源同键同名，实测 count 一致：ECS 132/132、HSS 582/582、IDT 930/930）。

## 迁移与回滚

零数据迁移。管道 `console_first=False` 显式钉住注册域（构建可复现性不变）。回滚 = `git revert`（repo 态）；wheel 态的紧急逃生 = 上一版本 wheel（不新增旋钮，见驳回记录）。

## 验收标准

1. `uv run ruff check src tests && uv run mypy src && uv run pytest` 全绿（默认档）。
2. 新增矩阵 1–9 全绿；既有测试按兼容表预判（2 适配 + 其余仍绿）。
3. 管道 `api-refresh docs` 语义不变（`console_first=False` 路径测试覆盖）。
4. 人工实测冒烟（不阻塞、不入测试）：console 健康 `fetch_apis("ECS")` < 2 s。独立真值链：URL/信封/method 方言字面量取自第 7 章实测，非同义反复。
5. AGENTS.md 同步三处（Round 0 act #8）：项目上下文「APIE 元数据层」段、数据流段、S5 测试接缝行。

## 实施顺序（Sequence Verifiable Units）

1. 接缝 A：adapter 红→绿（纯函数矩阵）。
2. 接缝 B：先 `console_first` 参数 + URL/信封（绿：console 路径用例 + 管道单源用例），再页级回退 + 粘性 + 去重（回退矩阵），每小步红→绿。
3. 接缝 C：catalog 测试适配 + 全量回归。
4. 文档同步（AGENTS.md 三处）+ 全量验证。
