# ADR-0005: Swagger 2.0 schema 归一单一 owner（allOf 保留 + total x- 默认）

日期：2026-09-23 · 状态：已接受 · 关联：ADR-0001（convert+correct 组合根）、
AGENTS.md「校验规则」schema 归一 / 转换规则段

## 背景

转换期 schema 形状归一此前散落在三处：`convert_openapi2.clean_schema`
（pop 列表删键）、`fix_schema_type`（全文档 type 改写，含 example 字面数据）、
以及 `oas2_parameter`/`clean_header`/`clean_response` 的局部 schema 改写。
`clean_schema` 把 `allOf` 当成 OAS3 残留一并 `pop`，而 `allOf` 是**合法
Swagger 2.0**——纯 `allOf` 组合定义塌缩为 `{}`，导致 `get_api` body
`schema: {}`、`execute.validate_params` 退化为全放行。全语料实测（17,963
API）：~2.1k `allOf` 定义，其中 ~900 塌缩为 `{}`；另有 `oneOf`（8 例真实
schema 位置）同样塌缩；`discriminator`/`example`/`xml`/`readOnly`/`title`/
`maxProperties` 等合法 2.0 键被误删；属性删除规则整条删掉 enum-only 等合法
属性。

## 决策

1. **单一 owner**：新增 `apie/schema_normalize.py`，公共面恰一个函数
   `normalize_doc(doc)`，作为 `convert_api` 的**终端 stage**（`convert_ref`
   之后、`correct_doc` 之前），替换 `clean_schema` + `fix_schema_type`
   （两者删除，`TYPE_MAP` 迁入模块）。replace-don't-layer。
2. **位置类型化 allowlist + total 默认**：allowlist 自官方 Swagger 2.0
   meta-schema 派生（`schema`/`primitivesItems`/`fileSchema` 三档）；allowlist
   外且非 `x-` 的键一律改名 `x-<key>` 无损保留。`^x-` 在 meta-schema 每个
   schema 位置被放行，故 **gate 0 结构性成立**，未来未知键零改动自动安全。
3. **`allOf` 保留、不展开**：`allOf` 合法且 jsonschema Draft4 原生组合；
   展开（flatten）有 `required` 冲突 / 属性碰撞 / `additionalProperties:false`
   语义风险。保留即修复塌缩 bug。组合（flatten）列为可选、证据门控的后续
   演进，默认不发。
4. **合法 2.0 键保留**（含此前被误删的 `allOf`/`discriminator`/`example`/
   `xml`/`readOnly`/`title`/`maxProperties`/`minProperties`/`externalDocs`）；
   非 2.0 键无损转 `x-`（`oneOf→x-oneOf`/`nullable→x-nullable`/
   `writeOnly→x-writeOnly`/`deprecated→x-deprecated` 等）；`xml` 保留整块 +
   `xml.name→x-xml-root` 提升（OBS 契约，`execute_obs._root_element_name`
   优先读 `xml.name`，两者并存）；属性删除规则收紧为「仅删非 dict/None」。
5. **区域感知遍历**：`definitions` + body-param `schema` + response `schema`
   （`type:"file"` 走 `fileSchema` 窄档），递归 `properties`/`items`/
   `additionalProperties`/`allOf` 成员；**排除**参数/头对象本身
   （`oas2_parameter`/`clean_header` 关切）、`example`/`examples` 载荷、
   corrections、`$ref` 重写（归 `convert_ref`）。参数/头对象仅做 type 归一
   （修复 `fix_schema_type` 退役后 `type:"Integer"` 等大小写值仍被映射）。
6. **schema 节点 copy-on-write**：重建 schema 节点，不原地改写原始 schema 节点**值**
   （消除 `fix_schema_type` 走遍全文档改写 example 字面数据的腐蚀类 bug）；容器层
   （`definitions`/`parameters`/`responses`/`paths.*` op）就地替换为归一后的新容器，
   与 `convert_api` 既有 raw-mutation 姿态一致；幂等。
7. **校验层不强制 `allOf`**：API Explorer 的 `allOf` 组合不可靠——跨分支约束冲突、
   且与官方 `x-request-examples` 不一致（如 `ROMA::CreateVpcChannelV2` 示例
   `protocol:"http"` vs 元数据 enum 大写）。故 `validate_params` 校验前剥离 `allOf`
   （直接声明的约束仍照常校验）；展示层（`get_api`）仍保留 `allOf`（信息不丢）。
   生产语义差分（相对修复前，全语料官方 body 示例）**0 新增拒绝**（口径见测试）。
8. **校验层忽略不可编译 / 非字符串 `pattern`**：API Explorer 部分 `pattern` 为服务端
   （Java/PCRE/ECMA-262）方言（`\p{L}` 属性类、`[\w-.]` 类内连字符等），服务端合法
   而 Python `re` 编译期抛 `re.error`；jsonschema 在 `iter_errors` 惰性编译 `pattern`，
   异常逃逸 `execute_api`。实测 **132 个 body 可达 API 含此类 pattern，其中 88 个的
   官方示例实际触发崩溃**。校验视图 `_validation_view`：只删编译失败 / 非字符串的
   `pattern` 键（其余 required/type/enum/可编译 pattern 照常强制），**不做宽 catch**
   （`iter_errors` 惰性 + `sorted()` 全消费，宽 catch 会吞掉真实校验错误）；展示层 /
   缓存 doc / 离线产物不动（元数据真值不丢）。全语料官方 body 示例校验 **0 崩溃**。
   属「Python 无法表示的约束不参与校验」的 fail-safe 放松；残余为对相关 API 的静默
   欠校验（服务端真值兜底）。校验视图**不进入** `enum`/`default`/`example`/`x-*`
   数据载荷（避免改写枚举成员语义、防误拒）。
9. **S4 `allOf` 扁平化：评估后否决**（维持「展示保留 / 校验剥离」双姿态）。证据：
   `allOf` 仅涉 **1.49% API（268 个 body 可达）**；这些 API 的 `get_api` 信封最大
   **39k**（全语料最大 187k，均 < 200k spill 阈值；`_resolve_schema` 已内联成员 ref）；
   扁平不满足语义（union 制造服务端禁止却接受的 schema；override 方向对
   `ROMA::ProjectVpcChannelInfo.type` integer-vs-string 判错），多分支 `required`
   需并集、ref 链、sibling `required` 均需处理（语料实测数百处）；display 扁平因
   Draft4 忽略 `$ref` 兄弟键被迫内联，多个信封变大；强制扁平会重新引入官方示例误拒
   （量级数十个）；保留 `allOf` 让 agent 看见元数据自相矛盾（唯一安全信号）。如需
   改善走 `metadata_corrections` 事实修正，而非结构重塑。

## 后果

- 全语料实测：转换后塌缩为 `{}` 的定义 **0**（修复前 ~900）、2,116 个定义保留
  `allOf`（**计数口径**：转换后输出含 `allOf` 的定义；原始未转换出现次数 1,528，
  按名去重 805 / 内容去重 970）、17,963 doc 过 Swagger 2.0 gate **0 invalid**。
- `get_api` body/response schema 恢复非空，且信封 `definitions` 为**传递闭包**
  （`metadata._expand_collected`：收集到的定义自身引用的定义名也纳入，深度 >2 的
  链式 `$ref` 不悬空）；直接声明的约束（`maxProperties`/enum-only 属性等）在校验层
  恢复；`allOf` **不强制**（决策 7），`x-oneOf`/`x-nullable` 被 Draft4 忽略仍宽松。
- 校验层另忽略**不可编译 / 非字符串 `pattern`**（决策 8）：132 个 body 可达 API 中
  88 个的官方示例原会抛 `re.error` 崩溃，现结构化放行；全语料官方 body 示例校验
  **0 崩溃**。
- **模块外 schema 读写点（显式边界）**：`oas2_parameter`/`clean_header`（参数/头对象
  键裁剪）、`clean_response`（`schema.examples` → response.examples 迁移）、
  `metadata._resolve_schema`（呈现期 `$ref` 内联 + 传递闭包）、`metadata_corrections`
  （doc-verified 事实修正，归一之后运行，自行保证 patch 后仍合法）。本模块只拥有
  **schema 形状归一**；校验松弛（`_validation_view`：剥离 `allOf` 与不可编译
  `pattern`）是**独立、不持久化**的校验层关切，不属本模块。
- 无 feature flag（会造成双有效性制度，违反 replace-don't-layer）；坏上游
  数据的事实修正仍走 `metadata_corrections`（本归一之后运行，顺序正确）。
- 已知边界：`oneOf` 等转 `x-` 后 Draft4 不识别（保留但宽松）；`allOf` 不展开
  时 `get_api` 呈现组合结构（信息保留、非扁平）；`convert_ref` 仍独立负责
  `$ref` 前缀重写。
