# ADR-0001: 元数据纠偏应用面收敛 3→1（生产者时点）

日期：2026-09-18 · 状态：已接受 · 重开：AGENTS.md「校验规则—元数据纠偏」记录的三个应用面落位

## 背景

元数据纠偏（`apie/metadata_corrections.py`）原设计有三个应用面，共享同一
patch 核心：

1. `correct_doc`——离线 convert 阶段 in-place（op 级 + doc 级指针一次落位）；
2. `correct_doc_cow`——运行时 copy-on-write（`get_api` format 前、
   `execute_api` validate 前），保护缓存 doc 不被改写；
3. `correct_api_result`——get_api 信封级 COW（op 级呈现面）。

摩擦：**「转换后的 doc 必已纠偏」这条不变量住在消费方**。`live_fallback`
产出未纠偏 doc 进缓存，每个 service 消费点必须记得调用 COW（get_api 与
execute_api 两处）——新增消费点即泄漏点；离线管道另有一套组合。纠偏语义的
locality 分裂在三处。

## 决策

纠偏唯一应用面 = **生产者时点**：`apie/doc_compose.py` 的
`compose_doc(raw, *, product, api, auth_demote, corrections)`（convert +
correct 同一核心）：

- 运行时：`LiveFallback.fetch` 在**缓存写入前** in-place 落位——
  「缓存 doc 恒已纠偏」由构造保证，而非消费方机制保证；
- 离线：convert main() 委托同一 `compose_doc`；
- `correct_doc_cow` 与 `correct_api_result` 删除（对已纠偏 doc 恒 no-op，
  按 "one adapter = hypothetical seam" 属假想 seam）。

corrections 配置沿 auth_demote 先例穿线程（`ServiceConfig` →
`load_api_doc` → `catalog.find_api_doc` → `LiveFallback`），均为启动期
常量，doc 随首次转换固化进缓存。

## 理由

- **deletion test**：删掉 COW 机制，不变量无需在调用方重建——生产时点
  in-place 使 copy-on-write 整层不必要；
- **locality**：纠偏语义单点（compose_doc），新增消费点天然继承不变量；
- **interface 即测试面**：service 的 get_api/execute_api 对 doc 纯读，
  测试 fixture 预填已纠偏 doc 即模拟生产者产物，纠偏行为在
  compose_doc/live_fallback 层测试。

## 后果

- 「缓存 doc 恒不改写」措辞升级为「缓存 doc 必已纠偏且不再改写」；
- 信任边界不变：纠偏配置仍是部署侧/官方文档核实的事实修正，逐 API 精确
  键红线不泛化；
- 若未来出现绕过 live_fallback 的缓存写入路径，须复用 compose_doc
  （组合根是唯一转换组合点）。
