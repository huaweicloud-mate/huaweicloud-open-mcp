# ADR-0002: 部署装配收敛为单入口 deployment.build_app

日期：2026-09-18 · 状态：已接受 · 取代：composite.build_composite_app（已删除）

## 背景

装配知识散落 5 文件：cli.main 按 mode 分发（三套 builder 调用 + 启动日志）、
三个 mode server 各持 `build_X_config`/`build_X_app`、composite 在混装路径
构建第二个 PolicyStore 再 mutate 覆写 `policy_store`/`audit_sink`/共享单例。
后果：

- 加一个部署 flag 触 5 文件（cli + builder + ServiceConfig + composite + 测试）；
- mock 三元组/policy/audit 的「args 优先 env 兜底」解析三处逐字重复；
- ~12 处 `getattr(args, ...)` Namespace 兼容补丁（openapi builder 容错、
  discover builder 直取属性，两者已分叉）；
- 22 个 `HUAWEICLOUD_MCP_*` env 名以内联字符串散布 62 处；
- 共享不变量（混装共享 store/sink）由事后 mutate 保证而非构造保证。

## 决策

1. **external seam 唯一**：`huaweicloud_open_mcp.deployment.build_app(modes,
   args, *, env, log_level, elicit_mode, openapi_service, discover_service,
   data_service) -> MCPServer`。单模式与混装同一路径；三个 service 注入参数
   即测试 adapter；env 可整体注入（测试不必 monkeypatch os.environ）。
2. **共享控制面构造注入**：PolicyStore/AuditSink 在 build_app 构建一次，
   作为 builder 参数传入——composite 的事后覆写与双重构建删除。
3. **Deployment 值对象**（`common/deployment.py`）：跨模式共享旋钮
   （mock/mock_base/mock_passthrough/policy_file/audit_file）归一单点 +
   全部 env 名常量唯一定义；mode 专属旋钮解析留在各 builder、经 `dep.env`
   读取（getattr 补丁消灭于归一层）。`common/deployment.py` 落 layer 1
   （零内部依赖），mode builder（layer 3）可引用而不破坏分层。
4. **internal seam 显式化**：`build_X_config(args, dep=None, *, ...)` 的
   dep=None 原口保留（配置解析测试直调不迁移）；`build_X_app` 保留为
   internal seams；`register_*_tools` 保持公开（InMemoryTransport 测试 seam）。
5. **cli 薄化**：仅 argv 解析 + 日志配置 + `build_app` + run；启动日志与
   policy 缺失警告随 build_app。
6. **单模式 instructions 逐字节回归**：`len(modes)==1` 走 mode 自持
   instructions；混装经 merge_instructions（自 composite 迁入）。

## 理由（codebase-design 词汇）

- **deletion test**：删 composite 的覆写逻辑，共享不变量须在各 builder 重建
  ——收拢后由构造一次保证；删 build_app，装配知识重新散落 5 文件。
- **locality**：新部署 flag 的触点从 5 文件收缩为「Deployment 旋钮（若跨
  模式）+ 消费 builder」两处；env 名漂移在常量单点编译期可见。
- **两个真实 adapter**：openapi/discover/data 三 mode builder 即 seam 上的
  三个 adapter——seam 真实，非假想。
- **interface 即测试面**：装配断言（工具集并集/manage_policy 去重/共享
  policy 闭环）迁 `build_app`；builder 级配置解析断言走 internal seam 原口。

## 后果

- composite.py 删除；`build_composite_app` 由 build_app 取代（test_composite
  迁移导入，语义断言不变）。
- 启动日志措辞微调：discover 混装被去重时附 `manage_policy=openapi` 后缀
  （原 composite 静默去重）。
- `parse_auth_demote_policy` 自 `apie.convert_openapi2` 迁入
  `mcp_openapi.server`（CLI 方言解析属装配侧；`AuthDemotePolicy` 值类型留
  apie——转换期概念）。
- 未来 Streamable HTTP 多会话部署若需要 per-session 旋钮，build_app 是
  唯一需要感知的接缝（见 AGENTS.md session 档边界记录）。
