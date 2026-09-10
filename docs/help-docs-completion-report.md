# 帮助中心功能介绍补全 & 废弃接口治理 — 实施报告

日期：2026-09-10 ｜ 范围：S13（补全管线）+ S14（废弃治理）+ S14f（L4 动态归属）
状态：**全量完成，已提交**（`4cfd2d3` → `54d298e` 共 5 个提交），881 测试通过，ruff/mypy 干净

---

## 1. 背景与目标

API Explorer `detail` 接口返回的 `op.description` 普遍极简（如 NovaRebootServer 仅“重启单台云服务器。”），而内嵌的帮助中心页面（support.huaweicloud.com）拥有完整的「功能介绍」（含废弃提示、约束、替代接口链接、官方文档 URL）。

目标：爬取帮助中心 → 与 apiexplorer 17,963 个接口匹配 → 仅差集口径生成 hints 配置，经既有 S10 注入机制增强 `get_api`；顺带产出废弃接口索引，治理 `list_apis` 发现面。

**核心约束（用户确认的口径）**：仅差集补全 / 功能介绍全文+链接 / 产物不入库（configs 部署副本除外）/ 限速爬取 / **list_apis 不补全**（仅 get_api）/ 废弃信号仅取帮助中心 / 默认 annotate / get_api 恒可用 / annotate 用结构化字段。

## 2. 关键调研发现（决定了管线形态）

| 发现 | 影响 |
|---|---|
| API Explorer 无端点暴露帮助文档 URL（`x-publishpath` 恒空，doc 类端点 404） | 映射必须自建 |
| sitemap.xml 存在（6.5MB）但**大量陈旧死链**（api-ecs 197 种子中 193 个 404）且**当前形态 API 页不在 sitemap** | sitemap 仅作种子 + 同 docset 链接 BFS 发现 |
| 裸 curl 触发 "Security Verification"，带浏览器 UA 的 urllib 可过 | 挑战检测 + 30/60/120s 退避 |
| apiexplorer `op.deprecated` 几乎无信号（ECS 实测 1/45），帮助中心 title `（废弃）` 才是真值源 | 废弃索引取帮助中心 |
| 帮助中心合并文档集（`密码安全中心 DEW` 实际服务 KMS/CSMS/KPS/CPCS 四产品） | L4 动态归属（S14f） |

## 3. 交付架构（Deep Module：2 个管线模块 + 1 个运行时值对象 + 1 个机制参数）

```
┌─ 构建期（api-refresh HELP_STAGES，不在默认 refresh 范围）
│   helpdocs: apie/fetch_help_docs.py
│     sitemap 种子探测 → 同 docset BFS → Crawler（UA/限速 0.4s/挑战退避/404 快速失败）
│     → raw/help_docs.json（断点续传 + failed 台账 + stale 自动升级）
│   helphints: apie/build_help_hints.py
│     纯函数核心 apie/help_docs.py（解析/别名/match_apis/diff/build_hints）
│     → help_completions.json + report.json + deprecated.json + hints 文件
│
├─ 运行时（零爬取依赖，文件契约隔离）
│   S10: --hints → Hints（api_notes_in_list_apis=false → get_api 独占增强）
│   S14: --deprecated-index/--deprecated-mode → DeprecatedIndex
│         → annotate（结构化标注）/ hide（metadata.list_apis(exclude_apis=…) 分页前过滤）
│
└─ 匹配链：overrides[docset] → 别名精确层 → L4 候选（ApiName 唯一命中才归属）
          → api_name → cn_name==summary 兜底 → 差集（min_gain≥20）→ hints
```

## 4. 全量运行结果

### 4.1 爬取（helpdocs，约 2.5h）

| 指标 | 值 |
|---|---|
| 文档集 | 105 个 `api-*` 种子 → **57 个有内容 docset**（BFS 后） |
| 页面 | **8,088 页 / 0 失败**（193 个 sitemap 死链快速失败） |
| 带 ApiName 页 | 3,958 |
| 废弃标记页 | 251 |
| details 基线 | 17,963/17,963（67 瞬时失败经 retry 全清） |

### 4.2 匹配与补全（helphints）

| 指标 | 初版 | +overrides | +L4 归属（最终） |
|---|---|---|---|
| matched | 2,668 | 2,883 | **3,039** |
| completions | 496 | 498 | **499**（26 产品） |
| product 级未匹配 | 1,461 | 362 | **205** |
| deprecated 索引 | 99 | 99 | **99**（6 产品） |
| ambiguous | 44 | 67 | 67 |

补全 Top：RDS 286、ECS 70、DLI 31、CDN 20、NLP 9、IAM 8…  废弃 Top：ECS 45、DLI 39、SMN 11。

### 4.3 台账（剩余 unmatched 的构成与处置）

- `api` 1,783：产品对上但页面 ApiName 不在接口目录（隐藏/改名/下线接口）——不硬匹配（误补风险）
- `product` 205：apiexplorer 无对应产品的文档集（合作伙伴中心/客户运营能力/企业管理/DLV/BRS）——无归属保持台账
- `duplicate` 138：同 (product, api) 先到占位——去重语义，非损失

## 5. 运行时验证（全量产物实跑）

| 用例 | 结果 |
|---|---|
| `get_api(ECS, NovaRebootServer)` | hints=功能介绍+「当前API已废弃，请使用批量重启云服务器 - BatchRebootServers」+官方 URL ✓ |
| `get_api(CDN/DLI, …)` | 跨产品注入 ✓ |
| `list_apis(ECS, tag=状态管理)` annotate（缺省） | total=20 不变，6 条带 `deprecated: true + replacement` ✓ |
| 同上 hide | total=14（20−6 口径一致），tag_groups 同步 ✓ |
| `get_api` 对废弃接口 | 恒可用（发现面收窄 ≠ 详情拒绝）✓ |
| `list_apis` 条目 hints | 恒零注入（`api_notes_in_list_apis=false`）✓ |
| 未配置索引/hints | 行为逐字段不变（回归红线）✓ |
| mode 无索引 | 启动快速失败 ✓ |

## 6. 代码与测试

| 类别 | 内容 |
|---|---|
| 新模块 | `apie/help_docs.py`（纯函数核心）、`apie/fetch_help_docs.py`（爬取编排）、`apie/build_help_hints.py`（派生落盘）、`mcp_openapi/deprecated.py`（DeprecatedIndex） |
| 扩展 | `common/http.open_text`、`apie/metadata.list_apis(exclude_apis=…)`、S10 开关、refresh `HELP_STAGES`（方案 B：默认 refresh 范围不变）、CLI 两 flag + 环境变量 |
| 测试 | S13a–f + S14a–f 共 12 接缝、42 个新用例；fixture 依真实页面精简；产物 tmp_path 独立回读 |
| 文档 | AGENTS.md（接缝/校验规则/命令/产物表）+ README 双语镜像 |

提交序列：`4cfd2d3`（S13+S14 主体）→ `107ff6f`（首批 overrides）→ `83b33fd`（部署态挂载+路径笔误修正）→ `f5cc462`（L4 动态归属）→ `54d298e`（configs 副本同步）。

## 7. 后续工作（非阻塞）

1. **剩余 product 级未匹配**：205 条需产品侧确认（如 api-dew 的 KMS/CSMS 拆分是否有开放 API 对应）；apiexplorer 无对应产品的文档集维持台账
2. **ambiguous 67 条**：中文名兜底歧义，可经 `--overrides` 逐个消解（收益递减，按需处理）
3. **hints 时效**：帮助中心内容随时间变化，建议按季度重跑 `api-refresh helpdocs && helphints`（增量断点已支持：done 跳过、failed 重试）
4. **S10 schema 潜在扩展**（本次不做）：若 list_apis 未来需要轻量废弃提示，可评估 flag 第三态
