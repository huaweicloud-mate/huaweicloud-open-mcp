# Review Log（interrogate 记录）

## Round 0 · design.md 设计检视（2026-10-16）

Reviewers：A（glm-5.3，10 findings）/ B（deepseek-v4.1-flash，10 findings）/ C（glm-5.3-flash，10 findings）。
Lead 判定前独立核实的事实：
- `explorer.fetch_apis` 的 src 调用方仅 `catalog.py` 与管道 `fetch_apis.py`；`retry_failed.py:31` 走 `fetch_detail` + 注入 `fetch_json_429`——设计中「retry_failed 共用 fetch_apis」为事实错误（B#6/C#5 属实）。
- 54 处测试经 `monkeypatch .../http.fetch_json` 封网（test_service/test_elicit_mcp/test_http_sessions_e2e/test_composite）；这些测试的 store 均为预填充（`_prep_store`），无一冷拉索引——seal 约定被侵蚀但今日无测试实际破（B#5 属实，风险级别降为 warning 而非 critical）。
- `test_fetch_apis_paginates_until_count` 断言 `calls[0].endswith(...productshort=ECS)`（test_apie_explorer.py:179-180）——console 优先必破此断言，设计「自然兼容」预判为假（A#4/B#4/C#4 共识，属实）。
- `open_url` 消费方确为三个 `fetch_json*`；`open_text`/mock lane 不经它——设计的波及面陈述有误但方向（不改既有契约）仍站得住（C#6）。

### Act On（修订设计的发现）

1. **快路径重试预算与慢失败模式**（A#1+B#1+C#2 三方共识，最重）。「console 失败 ≈ 数百 ms」的成本模型只对快失败成立；429/网络挂起/挑战页会让快路径先烧满整条退避梯（429: 4×20s；网络: 5×30s+4×5s≈170s）才回落，console 退化时 IDT 类产品从今日 ~28s 硬失败变成数分钟级阻塞，且每页重复付。**处置**：采纳不对称预算——console 尝试单次、短超时（5s）、零重试（回落即重试）；429 梯只挂在注册域腿；加 per-call 源粘性（同一次 `fetch_apis` 内 console 首败后本产品剩余页直接走注册域，~3 行无全局状态）。C#2 指出 page_sleep=0 时回退页连续打注册域会撞 ~3-5 连发限流——per-call 粘性 + 回退页计入 page_sleep 节流（pipeline 0.3s 在）部分缓解，运行时串行页间天然有服务端 5.4s 间隔（实测），不额外加延迟。
2. **终端失败语义未定义 + 「断点」名不符实**（C#1 主-raiser，B#3/A#10 同指）。「失败只丢当前页」字面读法 = 部分结果落 `set_apis` 永久缓存为完整索引——静默截断，比失败更糟。**处置**：明确终端失败（两源均失败）→ 抛异常、零缓存（今日语义），验收措辞从「已拉页不丢」改为「页级回退不回滚已拉页（进程内），跨调用无 checkpoint（记入不做的）」。「分页断点」更名「页级重试/回退」。
3. **测试兼容预判为假 + URL 断言必破**（三方共识）。`test_fetch_apis_paginates_until_count` 的 `endswith(productshort=...)` 断言在 console 优先下必红；`test_fetch_apis_empty_product_single_call` 的 `len(calls)==1` 依赖信封宽容度决策。**处置**：信封宽容度定为**宽容**（console 信封缺 `api_basic_infos` 时也接受 `apis` 键——探测两键，任一存在即用；这同时让既有注册域形 fake 在 console URL 下自然工作，测试适配面最小）。兼容表重做（见修订后设计）；catalog 的 `test_get_apis_remote_error` monkeypatch 目标随缺省 fetcher 变更而适配。
4. **seal 约定侵蚀**（B#5，核实后升级为 act）。54 处 `monkeypatch http.fetch_json` 封网在缺省换 `fetch_json_429_raise` 后不再覆盖索引腿。今日无测试实际破（store 预填充），但约定语义变假。**处置**：`fetch_json_429_raise` 实现为 `fetch_json_429` 的薄适配（B#6/A#5/C 同指：adapter 而非第四条独立重试环——顺带消除设计里 retries=5 vs 10 的自相矛盾）；在 `tests/test_apie_explorer.py` 顶部注释 + AGENTS.md S5 行写明「索引腿封网点 = `http.fetch_json_429`（adapter 的实现底座）」——monkeypatch 底座即同时封住 adapter。这样 54 处既有 seal（`fetch_json`）仍覆盖 products 腿，索引腿的新 seal 点是单一函数，约定更新成本一行。
5. **管道 provenance 谎报 + retry_failed 事实错误**（B#6/C#5/A#9 共识）。`fetch_apis.py:48` 写死 `"source": explorer.BASE`，双源后成谎；retry_failed 不经 fetch_apis。**处置**：`fetch_apis`（explorer）返回值不变，管道 `fetch_apis.py` 的 source 字段改为双源标记 `"console+registered (dual-source)"`——但更深的问题是管道（构建期、断点续传、可重跑）是否应走无 SLA 的 console 主源。**决定**：管道保持注册域单源（`fetch_apis.py:18` 显式传 `console_first=False` 参数——比 env 旋钮轻、比隐式继承诚实；管道的 0.3s 页间限速 + 重跑语义本就为慢源设计，5.4s/页在离线语境可接受，且保全 entity-index 构建的可复现性）。运行时缺省 console 优先。这同时消解 A#2/C#7 的「混合源污染提交产物」担忧：运行时混合列表只进内存 cache，构建产物恒注册域单源。
6. **200+非 JSON（挑战页/错误页）误分类为瞬时错**（C#8 主-raiser，A#1 部分同指）。`open_url` 成功路径 `json.loads` 抛 JSONDecodeError → 通用 except → 当瞬时错重试整条梯。console 退化最可能的形态恰是 HTML。**处置**：console 腿单次尝试 + `except Exception` 全捕即页级回落（#1 的不对称预算顺带解决——JSONDecodeError 也会被单次尝试吞掉后立即回落，无需单独分类）；测试矩阵加「console 返回 HTML 字符串 → 立即回落」用例。
7. **非 429 HTTP 错误不在回退触发清单**（A#3，B#7 同指 taxonomy 欠完整）。401/403/504 恰是内部接口下线形态。**处置**：与 #6 合并——console 腿 `except Exception` 全捕回退；注册域腿保持既有传播语义（HTTPError 重抛 → catalog 吞为 None）。B#7 的「终态错误类型变化」（今日 502 经 open_url 变错误体 → ValueError；换 adapter 后变 HTTPError 重抛）核实为兼容：catalog `except Exception` 全吞，工具层同为 `{ok:false,reason}`，但错误 reason 文案会变——记入设计为已知行为变更。
8. **AGENTS.md 同步面过窄**（C#5 部分）。数据源变化按仓库文档纪律应同步「项目上下文」+「数据流」，不止 S5 行。**处置**：修订实施的第 4 步为同步三处（项目上下文 APIE 元数据层段、数据流段、S5 测试接缝行）。

### Consider（记入设计「不做的」或后续项，不本轮实施）

- **跨源 ordering parity**（A#2+B#2）：offset 分页假设两源同序，实测未验证。缓解：#5 已把混合源限制在运行时内存 cache；再加一道廉价防御——`fetch_apis` 出口按 `(name, method)` 去重 + 去重命中记 WARNING（A#2 建议的「把静默腐蚀变成可观测信号」）。若两源真不同序，重复条目被去重、缺口仍可能——但 console 健康时整产品单源、console 退化时粘性让源切换最多一次/产品（per-call 粘性 #1），混合窗口收窄到单次切换。合并进 act #1 实现。
- **空批 ≠ 失败的 roster 漂移**（C#7）：console 不认识产品时回 `count=0` 空批 → 终止为成功 → 永久缓存空索引。防御：console 空批且 v4 products 的 `api_count>0` 时回落注册域确认——需要 store 里的 products 数据穿透到 explorer，违背分层。**降级处置**：不实现交叉校验；运行时 `list_apis` 对 `total==0` 已有自然可见性（agent 会看到空目录自行换路），记入已知边界。
- **fetch_products 的 429**（C#6 尾段）：products 腿同样零 429 容忍。实测 products 端点未触发过限流（155ms 单次、进程级缓存一次），风险低。**处置**：记入不做的+理由；真发生时症状是进程首次 list_products 失败、重试即愈。
- **jitter**（B#3 部分）：429 退避加 ±20% 抖动防并发羊群。B3（single-flight）不在本轮，羊群场景 = HTTP 多会话并发冷拉同产品，数量级低。**处置**：adapter 复用 `fetch_json_429` 原样（无 jitter，离线管道先例）；jitter 记后续项挂 R3。
- **detail 腿 429**（B#8）：B2 明确点名 detail 也零容忍，但实测 detail 无限流、72ms。**处置**：记入不做的+理由，与 C#6 尾段同条。
- **env 旋钮**（A#1/C#9/B#10 部分同指）：三方都质疑「不加旋钮」。#1 修复后（console 单次短超时快失败）旋钮的紧急价值确实缩水；但 uvx/wheel 消费者无法 git revert 是硬事实。**处置**：仍不加——理由更新为「#1 后快路径退化成本 = 每页一次 5s 超时探测 + 粘性短路（仅首页），可接受；旋钮留给 console 实际变卦时随修复发布」。如实记录三方异议与驳回理由。
- **验收 #4 降级为不阻塞**（B#4 部分）：URL 方言真值已由第 7 章 A/B 实测固化（参数名/信封/method 大小写），fake 测试的字面量即取自该实测——独立真值链成立，非同义反复。维持不阻塞，但设计里写明真值来源。

### Noted

- A#8（502/503/504 不重试）：adapter 复用 `fetch_json_429` 语义即维持现状（429/网络错才重试），网关类 5xx 快速失败回落——与 #1 的单次尝试策略自洽，不改。
- A#9（「出口恒为注册域归一形制」过强）：措辞弱化为「核心字段归一一致，键集随源异质」。
- C#10（page_sleep 与回退页交互）：修订设计写明「page_sleep 节流页、不节流页内回退；回退页计一页」。

### Dismissed

- A#1/B#1/C#2 中「需要全局熔断器/健康记分子系统」的表述——per-call 粘性（act #1 采纳）即其最小形态，全局熔断器是 R1 后台刷新语境的优化，本轮驳回其完整形态。
- C#1 的「partial-return + completeness marker」方向——需要穿透 catalog/store/信封的契约变更，超出 R2 止血预算，明确驳回（今日抛异常语义保留）。
- B#4 的「real-URL 冒烟应为 merge 阻塞项」——发布流程不归本 worktree 管；实测冒烟照做（人工验证），不设为代码验收门。

### Agreement Map

三方独立同指（最高信号）：快路径重试预算错误（A1=B1=C2）、测试兼容预判为假（A4=B4=C4）、fetcher 家族再添独立环 vs adapter（A5=B6=C 部分）、retry_failed/管道继承陈述失实（A6=B6尾=C5）。两方同指：非 429/挑战页 taxonomy（A3=C8）、跨源 ordering（A2=B2）、旋钮驳回存疑（A9=C9）。单方独有且被采纳：C#1 终端失败语义二读法（最有价值的单方发现——「部分索引永久缓存」读法直接推翻原设计措辞）、C#7 空批 roster 漂移、B#5 seal 侵蚀（lead 核实后升级）。

**修订动作**：design.md 按以上 act #1-#8 重写接缝 A/B 与测试接缝/验收章节；Consider 项落入「不做的（驳回预记录）」与「已知边界」。

## Round 1 · 接缝 A（fetch_json_429_raise adapter）实施检视（2026-10-16）

Reviewers：A/B/C 三方。共识发现（3/3 同指）：
1. 封网点契约（adapter 委托底座）只有 docstring 断言、无测试钉住——内联重写会让 5 个测试照绿而 seam 语义蒸发。
2. 200+非 JSON（挑战页）分支未文档未测——继承底座误分类为瞬时错，但 adapter 把它升格为运行时契约，须钉住防漂移。
3. 耗尽路径测试未钉调用数/退避序列（B#3/C#5）；测试文件 mid-file import 违文件 idiom。

**处置（全部吸收）**：
- 新增 `test_raise_delegates_to_base_seal_point`（monkeypatch 底座：kwargs 逐字转发 + err 通道转异常 + 零 urlopen）——「不 mock 自有模块」纪律的唯一例外，理由：委托本身是本接缝存在理由，属契约测试而非实现测试。
- 新增 `test_raise_non_json_200_treated_as_transient`（钉继承行为：2 calls + sleeps [1.0] + cause 链）。
- 耗尽路径补 `len(calls)`/`sleeps` 断言；import 归位顶部；docstring 补全五分支口径 + err.read() 已消费警示。
- 网络臂措辞修正（A#4）：「重抛」→「ApieHttpError 包装（__cause__）」。

驳回：
- A#6/B#4/C#1 部分「adapter 应当场被 explorer 消费否则是死代码」——按 Sequence Verifiable Units 分片落地，接缝 B 消费者即刻就位（本轮下一片）；文档已写明用途。
- C#3「dict-only 未强制 isinstance」——措辞已按建议弱化（dict-only 指 error channel，形状为端点契约假设），不加 guard（Laziness：端点恒返 dict，explorer 信封检查兜底）。
- C#4「retries=0 退化路径」——继承底座语义，非本 adapter 职责，不文档化边角。

结果：13/13 绿 + ruff 干净。

## Round 2 · 接缝 B（双源 fetch_apis）实施检视（2026-10-16）

Reviewers：A/B/C 三方，先验证基线（ruff/mypy/1484 全绿）后走查 rubric (a)-(h) 全部场景（三方独立 trace 一致：offset 去重前推进对齐源 count、method 归一先于去重键、无 console URL 泄漏路径、ValueError 保留 product/offset）。

共识发现（3/3 同指，全部 Act On）：
1. **不对称预算零回归保护**：console 腿 `retries=1/timeout=5/零退避`（Round 0 act #1 的核心决策）无任何测试钉住——退回缺省 kwargs 会复活 180s/页病态且全测试照绿；设计矩阵项 3/4（console HTML 挑战页、console 429/401/504 立即回落）未实现。**处置**：新增 `test_fetch_apis_console_leg_budget_and_immediate_fallback`（真 adapter→底座→urlopen 链，钉 `("console", 5), ("registered", 30)` 超时不对称 + `sleeps == []`）与 `test_fetch_apis_console_challenge_page_immediate_fallback`（200+HTML → 单次尝试立即回落）。
2. **AGENTS.md 三处同步未做**（验收 #5，实施步骤 4 排后）——AGENTS.md:23/51「注册域为唯一数据源」「console 域不再使用」已成假话。**处置**：完成三处（项目上下文元数据层段 + 新增「索引双源」原则条目、数据流段、S5 行含新封网点 `http.fetch_json_429`）。
3. **console 失败原因被吞**：`except Exception` 不绑异常，回落日志无法区分 429/挑战页/超时/401。**处置**：绑定 `%r` 嵌入 + WARNING 级（对齐 AGENTS.md 失败路径日志约定）。
4. **uv.lock 镜像重写搭车**：本地 mirror 环境的重锁工件，违反 AGENTS.md:347 记录的构建假设。**处置**：`git checkout -- uv.lock` 还原。

两方同指（Act On）：
5. **去重误伤管道单源语义 + "(cross-source)" 标签失实**：去重无条件跑，`console_first=False` 管道同键条目被丢弃且 WARNING 谎报跨源。**处置**：`mixed_sources` 门（源切换发生才丢弃；键恒收集以便切换后比对）+ 标签改 "cross-source shift" + 新增单源保序测试。
6. **死合取 `console_first and use_console`**：use_console ⇒ console_first 恒成立。**处置**：删除。

单方/nit（全部 Act On，与上述同批）：
- C#9（dedupe 测试实为跨信封非跨源）：测试重写为真跨源（console 页失败 + 注册域位移重发）。
- A#8（mid-file imports 违 idiom）：归位顶部。
- 模块 docstring / fetcher 契约注释过时（A#3/B#7/C#7）：更新（模块头补 console 方言 + 端点清单；:38 注释改 apis 缺省 429 感知）。

驳回：
- A#6/B#6（`count=0` 语义变化）：新 `count is not None` 使 count=0 立即终止；旧 `or` 合并把 0 当缺失。仅 count=0-带数据 的病态形态可达，实测语料无此形状，且新语义更符合字面契约（服务端声明 0 条即停）。有意识地接受，记此处。
- B#1 尾/C#1 尾（page_sleep 回退页计一页、零缓存断言）：前者已由 Round 0 C#10 设计语句覆盖且 sticky 测试隐式覆盖时序；后者（store 空断言）由 `test_fetch_apis_both_sources_fail_raises`（抛异常即无返回值可缓存）+ catalog `test_get_apis_remote_error`（None + 无 set）结构性覆盖，不另加测试。

结果：61 局部 → 1487 全量绿（+3 新测试）+ ruff/mypy 干净。

## Round 3 · 最终总检（2026-10-16）

Reviewers：A/B/C 三方独立跑全量验证（ruff/mypy/pytest 1487 全绿）+ 独立实测冒烟。三方 rubric 结论一致：retries=1 循环形状正确（range(1) 单次、sleep 守卫 0<0 不可达）、并发无新增危险（全部状态 call-local）、既有路径（products/detail/counts/catalog/retry_failed）零 diff、design 验收 1-5 全交付、不做的清单核实无越界、AGENTS.md 三处与代码行为逐条吻合。**实测冒烟（验收 #4）**：`fetch_apis("ECS")` 132 API / **0.29-0.49 s**（console 腿，对照纯注册域 11 s）；`fetch_apis("HSS")` 582 API / **0.87 s**（注册域恒 504 的产品复活）。

Findings：
- **[critical] uv.lock 镜像重写再次出现**（3/3 同指）：Round 2 处置后我的本地 `uv run`（mirror env）又重锁了它——评审员的验证运行同样会重写。**处置**：最终验证跑完后 `git checkout -- uv.lock` 收尾；根因记录：本地验证须 `--frozen` 或最后一步还原。
- **[nit] budget 测试恒真断言 + 死绑定**（3/3）：`assert _orig is ... or True`。**处置**：删除两行。
- **[nit] test_http 非 JSON 测试 mid-body import + 弱名字比对**（B）：**处置**：import 归顶、只留 isinstance 断言。
- **[nit] catalog.py docstring「注册域数据源」过时**（B/C）：**处置**：一行改为「索引双源、products/detail 注册域」。
- **[nit] 页 0 console 失败时 mixed_sources 武装全程去重 + 标签失实**（C#4）：单源语义仅对「未尝试切换」的调用成立。**处置**：接受为已知边界——实测语料 17,792 条零同键重复（entity-index 验证），(name, method) 是 get_api/execute_api 的下游寻址键、该键重复本身即歧义，WARNING 提供可观测性。记 review-log 本条即边界文档。

**裁决**：唯一阻塞项（uv.lock）为环境工件非代码缺陷；代码/测试/文档三方检视无 critical/warning 级 finding。Round 3 后状态 = merge-ready（不 commit，工作树留人工审查）。
