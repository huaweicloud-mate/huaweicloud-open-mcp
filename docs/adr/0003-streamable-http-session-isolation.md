# ADR-0003: Streamable HTTP 传输与会话隔离（session-keyed policy overlay）

日期：2026-09-18 · 状态：已接受 · 关联：ADR-0002（build_app external seam）、
AGENTS.md「safety policy 四档 scope」session 档边界记录

## 背景

server 仅支持 stdio（cli.py 硬编码 `app.run("stdio")`），需补充 streamable-http
传输。mcp SDK（2.0.0）的 `MCPServer.run` 原生支持
`"stdio" | "sse" | "streamable-http"`，且 `streamable_http_app()` 可作 ASGI
组合——协议实现免费，设计工作在**装配与会话隔离语义**：

- PolicyStore 的 session/temporary/once 三档是进程级内存 overlay（stdio 单会话
  下等价于会话级）；HTTP 单进程多会话下 A 客户端 manage_policy 授予的会话内
  规则会被 B 客户端继承（**泄权**）——AGENTS.md 已记录的限制必须在 v1 修掉。
- SDK 事实（设计期四路探针 + 复审裁决）：stateful 模式下 `mcp-session-id` 头
  经 transport 校验（非 initialize POST 逐一验证 header==会话 id）后即权威
  会话身份；工具层 Context 无公开 `session_id` 属性；消息经内存流跨 task 派发
  （ASGI 中间件里设 contextvar 传不到 handler，SDK `ServerMiddleware` 是唯一
  同时看得见会话身份、又包得住 dispatch 的咽喉点）；`session_idle_timeout`
  不经 `streamable_http_app()` 透传。

经 Design-It-Twice 四路并行设计（最小接口 / ASGI-first / 开箱即用 /
Ports&Adapters）+ 词汇镜检复审（F1–F8 修订），收敛为本决策。

## 决策

1. **会话隔离 = PolicyStore 内建 session-keyed overlay**：`_overlay` 单列表
   改为按会话键分桶的 dict，`None` 桶 = 历史命名空间（stdio 逐字节回归由
   结构保证，不靠测试努力）；方法签名零变化；ctor 增 `session_fn`（当前会话
   键解析器，缺省 `lambda: None`）与 `strict_sessions`（见 3）两个配置位。
   once 焚毁/插入不变量/TTL 剪枝逐桶复用，桶查找进既有 RLock 临界区。
   不选 per-session facade（浅模块、双重记账）、不选显式穿参（~30 处签名 +
   审计 input 快照契约污染）——ambient contextvar 的隐式性是 interface 的
   真实增长（ordering 约束：middleware 先于 handler、绑定先于 authorize），
   用构造注入 + build_app 单点配对 + I1–I7 不变量显式化补偿。
2. **会话身份经 SDK `ServerMiddleware` 绑 ambient contextvar**（新模块
   `common/sessions.py`，第 1 层零内部依赖、duck-typed 不 import mcp；
   公共面恰 2 名：`current_session_key` + `SessionScopeMiddleware`，头解析
   与 set/reset 为 internal seams）。解析梯子单函数收拢、公开 header 优先
   （transport 已校验，权威且公开 API），SDK 私有路径仅作兜底腿，S17 spike
   钉死本 SDK 版本的可用腿。
3. **fail-closed（I2）**：`strict_sessions=True`（HTTP 装配按 `dep.transport`
   置位）时，无会话键的 session/temporary/once 写入 → 结构化拒绝，永不落
   None 桶；permanent 与会话无关恒落文件。该守卫同时兜住两种 None（modern
   单交换协议无身份 / middleware 漏配），错误不可归因——运维排查序写入文档。
   否决「无身份请求绑临时桶」：那使 `temporary 3600s` 静默变成「至请求末」，
   scope 语义说谎比可操作报错更糟。
4. **传输面 = 4 个分立旋钮**：`--transport {stdio,http}`（`streamable-http`
   别名归一）、`--http-host`（默认 127.0.0.1，loopback 自动获得 SDK DNS
   rebinding 防护；容器开箱由镜像层 ENV 置 0.0.0.0，非 loopback 绑定
   WARNING）、`--http-port`（默认 8000）、`--http-path`（默认 /mcp）。
   env：`HUAWEICLOUD_MCP_TRANSPORT/HTTP_HOST/HTTP_PORT/HTTP_PATH`。
   非法值 fail-fast（有意区别于 parse_modes 的宽容回退——传输拓扑错误必须响）。
   不提供 `--http-max-sessions`（overlay 内存由 store 内部护栏封顶，transport
   层会话上限留 v2）。
5. **服务分派归 cli**：stdio → `app.run("stdio")`（逐字节现状）；http →
   `app.run("streamable-http", host, port, streamable_http_path)`。
   **不自建 TransportPort**（SDK `run` 已是该 port 的双 adapter，自建过不了
   删除测试）；**不建 `build_asgi`/`serve_http` 模块**（`streamable_http_app()`
   /`custom_route("/healthz")` 已是机制，自建是抄写）。嵌入 ASGI 走
   `build_app(...).streamable_http_app()` 配方（见后果 F1 残余风险）。
6. **会话回收 = store 内三重惰性 GC**（idle 1800s + 绝对年龄 86400s + 桶数
   cap 4096，time_fn 注入）：死会话桶不可达（terminate 后 404 + uuid4 不可
   猜），回收是内存卫生非正确性。SDK `exit_stack` 精确回调（可达性依赖内部
   对象）留 v2。桶仅写路径创建，读路径零分配。
7. **audit 归因**：`_audit_write` 单点 enrich——键非 None 时事件顶层加
   `session` 字段（`input` 快照纯净性不动，`build_audit_event` 契约形状不动，
   verifier 只读 tool/input/ok 加性键零回归）；stdio 恒 None 恒不加键
   （NDJSON 逐字节）。
8. **明确不支持/不做**：SSE（MCP 规范已废弃方向）；stateless_http（无会话
   身份使 session 档授权无所附着，旋钮不暴露）；多 worker/多进程（进程内存
   状态 fork 下不可预测）；v1 不内置 HTTP 认证与 TLS（定位本机/可信网段，
   跨主机须反代终结；ASGI 中间件槽位已预留）。
9. **discover SessionManager 保持进程共享**：授权接缝是 policy 不是连接所有
   权，远端连接池复用不构成泄权面；按 (session, server) 键控击穿 LRU 语义
   ——记录为已知边界。
10. **elicitation 语义不变**：授予经 grant partial → `add_rule` → 当前会话桶
    自动隔离；PolicyConsent 零改动（文档措辞「session 档=进程存活期」更新为
    「=MCP 连接会话」）。

### build_app 原则界定（复审 F5）

build_app 对 transport 的关系拆为两半：**serving 与返回类型
transport-agnostic**（永不 run/listen、恒返回 MCPServer，ADR-0002 形状不破）；
**augmentation 允许 transport-aware**（strict_sessions 置位、/healthz 注册、
instructions 传输说明段——读 `dep.transport`）。此界定防止后续维护者按
「build_app 必须完全 transport 无知」的错误原则拆掉增强。

## 不变量（I1–I7，测试引用编号）

- I1 生效规则 = `overlay[current_key] ++ 文件规则`，整体 first-match；互斥桶，
  评估点恰有一个活跃桶。
- I2 strict_sessions 且键 None 时 session/temporary/once 写入结构化拒绝。
- I3 跨会话不可见——含 `list_rules`（A 看不到 B 的授予）。
- I4 once「下一次执行」= 同会话内下一次 dispatch；焚毁限授予会话桶内。
- I5 remove 跨层序 = 当前会话桶 → 文件，不触他会话桶（重连后 remove 旧会话
  授予失败，文案提示「授予属先前会话」）。
- I6 三重惰性 GC（注入 time_fn 测试）。
- I7 stdio 三红线：None 桶=历史列表、audit 无 session 键、run 路径逐字节。

## 后果

- 新增 `common/sessions.py`（layer 1）；PolicyStore/audit/cli/deployment 按
  上述修订；TDD 接缝 S17–S22 并入测试表。
- **F1 残余风险（已记录）**：嵌入方调 `build_app(args)` 未声明 transport
  （stdio 缺省 → strict_sessions=False）后自行 `streamable_http_app()`：
  携带身份的会话仍隔离，但无身份请求的 session 档授予落共享 None 桶。
  缓解 = 配方强制「组合 ASGI ⇒ args 声明 `--transport http`」
  （README + AGENTS.md known limits）；无法在不包装 MCPServer（违反
  ADR-0002 返回契约）的前提下检测。
- AGENTS.md「session 档语义」段落同步：stdio 下等价进程存活期不变；HTTP 下
  = MCP 连接会话（重连即新会话，授予随新会话重新评估）。
- 元数据浏览型会话 30 分钟不触 store 的授予会被 idle GC 回收（心跳 = store
  操作）——已披露的 locality 代价，入 known limits。
- async handler 内联调 sync 业务在事件循环内阻塞（单会话慢执行拖慢同进程
  其它会话）——v1 不修，`anyio.to_thread` 包裹属独立立项，ambient key 已
  天然兼容该改造。
