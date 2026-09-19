# ADR-0004: 配置文件热刷新同步（HotFile 统一异步管线）

日期：2026-09-19 · 状态：已接受 · 关联：ADR-0001（corrections 单一应用面）、
ADR-0003（会话身份/PolicyStore 热重载先例）、AGENTS.md「校验规则」各配置面段

## 背景

openapi/discover 两模式的配置文件面（hints / deprecated index /
metadata-corrections / entity-index / discover catalog）此前均为**启动期一次
装载**：外部编辑配置必须重启进程才生效。仓库内唯一热刷新先例是
`PolicyStore`（pull-on-access stat 探测 + RLock + 坏文件沿用最近合法版本）。
需求：把已验证的热刷新语义推广到全部配置文件面，同时不破坏
「launcher fail-fast / 值对象穿线程 / 既有测试零波及」等既有纪律。

## 决策

1. **新内核 `common/hotconf.HotFile`（第 1 层，零内部依赖）**：唯一公共方法
   `get()`——每次调用至多一次 stat；stamp 相等零文件 IO。探测恒同步惰性
   （pull-on-access，无定时器/watcher）；**重载统一异步**（全部配置面同一
   管线）：stamp 变化 → 锁内置 pending + per-event daemon 线程 → 触发请求
   立即返回旧值（stale-until-ready 最终一致）→ 后台完成时持锁换值。
2. **内容哈希防抖全 face 无条件**：stamp 变化后先比对 sha256（与解析共用同
   一次读取），内容未变（touch/备份工具）仅推进水位——不解析、不触发
   on_reload（防 corrections 联动的缓存清除风暴）。
3. **完成时 stamp 比对**（后台化特有竞态）：重载期间文件再次变化则弃结果、
   清 pending，下次 get() 重触发——否则会「换入旧内容 + 盖上新水位」静默
   丢失一次变更。
4. **降级语义对齐 PolicyStore**：构造期急切加载 fail-fast（缺文件/JSON 非法/
   schema 非法透出）；运行期解析失败/文件短暂不可读 → 沿用最近合法版本 +
   WARNING（解析失败推进水位防刷屏），恢复自动采纳 + INFO。
5. **装配纪律（policy_store/policy_rules 加性先例）**：`ServiceConfig` 保留
   值对象快照字段，新增 `*_live` holder 字段（缺省 None）；service 归一访问
   器（`_hints()`/`_deprecated_index()`/`_entity_graph()`）优先 holder——
   holder=None 路径与既有行为逐字节一致，82 处既有 ServiceConfig 构造与
   ~56 处传值对象测试零波及。`load_opt_file` 与四个 `load_*` 原样保留
   （离线管道与既有测试依赖），新增 `watch_opt_file` 三分支孪生。
6. **corrections 联动为定向失效**：on_reload → `MemoryStore.clear_api_details()`
   （仅详情 LRU）——products/apis 列表与纠偏口径无关，全量 clear 会把发现
   链可用性绑到 API Explorer 在线状态（被否）。MemoryStore 补 per-op
   `threading.Lock`（修复既有无锁 LRU 竞态；锁绝不跨网络 I/O 持有）。竞态
   窗口收窄手段：LiveFallback 的 corrections 参数接受 provider（HotFile），
   **fetch 时点现读**（远端拉取返回后、compose 前取当前值）——窗口从「秒级
   远端往返」收窄到「compose+落缓存亚毫秒」；残余窗口文档化为可接受。
7. **锁序单向**：`HotFile.RLock → MemoryStore.Lock`，无反向路径（store 方法
   纯内存操作从不触 HotFile）；on_reload 在 HotFile 持锁状态执行，回调异常
   遏制（WARNING，不影响换值生效）；禁止 on_reload 触碰 PolicyStore。
8. **discover catalog 自持同款 idiom**（懒加载、非 fail-fast、无 schema 校验
   的目录语义与 HotFile 不同，不强塞同一 Module——单消费者不加 lazy 标志，
   避免模式标志稀释内核接口）：首次/换路径同步加载（现状语义），后续变更
   走后台重载 stale-until-ready；坏文件沿用旧目录 + 水位推进。
9. **entity index 后台重建**：`_ENGINE_KWARGS` 经 `snapshot_engine_kwargs()`
   装配期快照、parse 闭包捕获——重建线程不重读模块全局（防重建产物与初始
   引擎配置漂移）；旧 EntityGraph 无需 close（tantivy Searcher 为 Arc 快照，
   frozen 值对象单引用原子换，在途查询持旧引用自然 GC）；重建窗口瞬时内存
   ≈2×索引 + 50MB writer heap（已知代价）。
10. **明确非目标**：instructions 恒启动快照（MCPServer 构造期字符串，结构
    约束非设计遗漏）；auth_demote/credentials/mode 枚举保持启动期常量；
    缺省档文件启动缺失 → 静态 empty（不追踪后出现的文件，重启生效）——与
    「启动存在、运行期删除 → 沿用旧版」形成有意的不对称。

## 语义边界（调用方须知）

- **最终一致**：编辑配置后，触发刷新的那次请求读到旧值；下一次调用见新值。
  ms 级配置实际无感；entity 重建 ~600ms 窗口内查询恒用旧索引（零中断）。
- **已知 stat 边界**（继承 PolicyStore）：HFS+/FAT 等长同 tick 编辑漏检一次
  （heavy 哈希确认兜住误重建）；NFS 属性缓存探测延迟可达 ~60s（无损坏）；
  多进程部署各进程独立探测。
- 缓存 doc 语义从「恒已按启动配置纠偏」改为「按写入时点的纠偏世代纠偏」
  （ADR-0001 措辞随之修订）。

## 后果

- 配置编辑即热生效，无需重启（运维收益）；全部刷新语义集中在一个 ~170 行
  内核 + 各面薄接线（locality）。
- 进程内出现后台 daemon 线程（首个）：单飞守卫保证每实例至多一个在飞线程，
  全进程至多 = 配置面数并发；per-event 线程非常驻池，编辑停息即消亡。
- 代价：触发请求的 stale 窗口（上文）；watch_opt_file 返回 union
  （装配侧专用）；corrections provider union 参数（保 test_doc_compose 零波及）。

## 残余风险

- **F1**：corrections 亚毫秒残余窗口——compose 后、落缓存前完成 reload 时，
  旧口径 doc 可写入刚清空的缓存并驻留至下次 reload/LRU 驱逐。缓解选项
  （已预批未实施）：holder 世代号 + set_api_cache 前后世代复核。
- **F2**：daemon 线程无优雅停机——进程退出时在飞重载被硬杀，纯内存态无害。
- **F3**：`on_reload` 为公开可写属性，仅限装配阶段注入；有流量后改写属
  未定义行为（文档注明）。
