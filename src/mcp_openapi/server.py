"""openapi 模式 server 装配（MCP 协议层）。"""

import argparse
import functools
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from apie import mock as apie_mock
from apie.convert_openapi2 import AuthDemotePolicy
from apie.metadata_corrections import load_metadata_corrections
from common.audit import AuditSink, sink_from_path
from common.auth import credentials as cred_mod
from common.deployment import (
    ENV_AUTH_DEMOTE,
    ENV_AUTH_DEMOTE_PASS,
    ENV_DEPRECATED_INDEX,
    ENV_DEPRECATED_MODE,
    ENV_ENTITY_INDEX,
    ENV_METADATA_CORRECTIONS,
    ENV_OPENAPI_HINTS,
    ENV_REGION,
    ENV_SPILL_DIR,
    Deployment,
    resolve_deployment,
)
from common.elicit import PolicyConsent, ctx_elicit_fn, gated_manage_policy
from common.types import (
    ApiDetailResult,
    ApiListResult,
    ExamplesResult,
    ExecuteResult,
    ProductListResult,
    ProductResult,
    SearchApisResult,
    ToolError,
)
from safety.policy_store import PolicyStore

from .deprecated import load_deprecated_index
from .entity_graph import load_entity_index
from .hints import Hints, load_hints_file
from .service import ServiceConfig, ToolService
from .spill import parse_spill_config

INSTRUCTIONS_OPENAPI = """# 华为云 Open MCP 使用指引（OpenAPI 直连模式）

## 推荐工作流（渐进收窄，LLM 决策）

0. `search_apis`：用户意图未指明产品/API 时先用本工具跨产品检索（实体图谱，
   返回候选产品 + 代表 API + matched_via 匹配证据），再进入 1-5 步收窄；
   结果截断（truncated=true）时可用 `limit=-1` 取全部命中；
1. `list_products`：获取产品列表（含中文名/分类/是否全局级服务），基于用户任务语义确定产品范围
   （选定产品时同步与用户确认目标 region）；
2. `list_apis`：获取选定产品的 API 目录；返回结果含 `tag_groups` 全量 tag 概览，
   先用 `tag` 参数收窄目录，接口较多时配合 `search`/`limit`/`offset` 分页浏览；
3. `get_api`：确定候选接口后，调用前**必读**接口文档（必填参数、类型、枚举、x-constraint 约束）；
4. `get_api_examples`：官方请求示例，用于指导参数填写（可选）；
5. `execute_api`：执行接口。参数约定：路径/query 参数平铺在 params，请求体放 params["body"]。

## 执行安全

- `execute_api` 执行前强制过 safety policy（allowlist/denylist 白名单）；
- 未配置 policy 时所有执行被拒绝；
- 拒绝结果形如 {"ok": false, "reason": ...}，不要绕过，应改用被允许的接口；
- 被拒接口确属任务必需时：先经对话/交互式问询（如 question 工具）向用户确认，再调用
  `manage_policy(action="add", line=...)` 授予规则（如 "OBS:GetObject=allow"，
  或产品级 "VPC:*=allow"），规则热生效后重试即可通过；问询按五选一口径：
  api=最小规则（一次性，用后即焚）/ api_session=最小规则（会话内，本次会话内
  持续放行该 API，重启即失）/ product=产品级规则（会话内，覆盖该产品全部
  API，重启即失）/ readonly=产品级只读规则集（会话内，List/Show/Get/Query
  四条通配，浏览类任务首选）/ none=不授予；同会话第二次问询建议直接推荐
  session 或 readonly 档；
  部署开启 elicitation（--elicitation auto/required）时重新调用被拒工具，服务端会
  经 MCP elicitation 弹出同样的五选一提议（结果携带 `granted_rule` 字段）；
  默认 off 或客户端不支持 elicitation 时，拒绝原因会附带同样的兜底指引，
  按指引问询确认后再授予；
- 也可直接调用 `manage_policy`：**改动热生效、无需重启 server**；默认
  （--elicitation off）无弹窗，务必先向用户确认再调用；开启 elicitation 后
  add/remove 由服务端先经 elicitation 向用户确认；注意 `manage_policy` 是
  server 内置控制面工具，直接调用即可，不要经 execute_api 路由；
- 规则四档 scope：`once` 一次性（仅放行下一次执行，用后即焚，重启即失）/
  `session` 会话内（缺省，本次 code agent 会话，重启即失、无需回收）/
  `temporary` 临时（内存 + ttl_seconds 自动过期，缺省 3600s）/ `permanent` 永久
  （写入策略文件，跨重启）；仅 permanent 落盘，授予最小权限请优先用 once/会话内/临时；
  `remove` 跨层回收（先会话/临时后文件，首个语义命中移除）。
- 认证由网关 AK/SK 签名层自动供给：不要在 params 里构造 x-auth-token 等
  认证头（元数据 required 标注已按此口径归一；部署豁免名单内的 API 以
  get_api 返回的 required 为准）。

## 超大响应落盘（spill）

- 工具响应/结果超过 200k 字符时自动落盘：结果携带 `spill` 信封
  （path/format/bytes/note），`body` 或重字段为截断预览，完整数据以落盘文件为准；
- `execute_api` 可传 `params["_spill"]=false` 按次退出（控制键不进入请求）；
- 落盘文件为原始数据（JSON/text，原子写、绝对路径、不自动清理）：
  部署混装 data 模式时可用 query_data/transform_data 直接分析该文件
  （json 数组可直接作表）；纯 openapi 部署用文件读取工具或 shell 查看。

## Region 与多区域

- 接受 region 参数的工具：`get_api` / `get_api_examples` / `execute_api`
  （产品级目录工具不分区，无需传）；
- 默认与覆盖：未传时用部署默认（`--region` / `HUAWEICLOUD_MCP_REGION`，
  缺省 cn-north-4），按次传 region 即按次覆盖；
- 流程：尽早确认，此后 get_api → execute_api 全程同一 region——
  接口文档与端点均 region-aware，混用会读到不同 host 与参数上下文；
- region 决定端点：详情文档 host 即该 region 服务端点（如 ecs.<region>.myhuaweicloud.com）；
- 全局服务不传：`is_global=true` 的产品（如 IAM）无 region 语义；
- 无效 region 静默回退：传入该接口不支持的 region 时元数据回退为默认
  region 文档（无错误提示）；查不到资源先确认资源所在 region，再显式换 region 重试；
- project_id 匹配：project_id 按 region 隔离，凭证里是单一静态值——跨 region
  执行路径含 `{project_id}` 或依赖 `X-Project-Id` 头的 API 时，在 params 显式提供
  目标 region 的 project_id（路径参数同名传入 / 头参数 `X-Project-Id` 传入，
  均可覆盖静态凭证值）；
- mock 模式下 region 仅进 mock URL 的 region_id 参数，无实际语义。

## 其它

- 产品 `is_global` 为 true 的全局级服务（如 IAM）与地域级服务认证模型不同。
- OBS 对象上传/下载（PutObject/GetObject/AppendObject/UploadPart）恒走预签发 URL
  单口径：execute_api 直接返回 presign 信封（url/method/expires_in +
  signed_content_type + headers 照抄清单），客户端凭 URL 直连 OBS 收发字节，
  gateway 不经手数据流、不限大小；_presign_expires 可调有效期。
  Content-Type 参与签名：上传（PUT/POST）建议显式传 _presign_content_type
  锁定类型，直连时按信封 headers 原样携带；未锁定时签名按空 CT 签名，
  直连请求不得携带该头（curl 用 -H 'Content-Type:' 移除默认头），
  信封 note 字段会给出对应警示口径。
"""


def build_instructions(hints: Hints | None = None) -> str:
    """生成 instructions：基础指引 + 部署自定义指引段。"""
    text = INSTRUCTIONS_OPENAPI
    global_text = (hints.instructions if hints is not None else None)
    if global_text:
        text += ("\n## 部署自定义指引\n\n" + global_text.strip() + "\n")
    return text


def parse_auth_demote_policy(demote: str | None = None,
                             pass_list: str | None = None) -> AuthDemotePolicy:
    """装配解析：--auth-demote / --auth-demote-pass（或对应 env）→ 策略。

    demote：None/空串→默认开启；"on"→开启；"off"→禁用；其余 fail-fast。
    pass_list：逗号分隔条目，"PRODUCT:API"（精确）/ "PRODUCT" 或 "PRODUCT:*"
    （产品级）；条目缺产品名 fail-fast；大小写 casefold 归一。配置错误要响，
    仿 hints 严格校验先例。
    （2026-09 起自 apie.convert_openapi2 迁入：CLI 方言解析属装配侧，
    AuthDemotePolicy 值类型留 apie——转换期概念，消费方 apie.catalog/
    live_fallback 依赖。）
    """
    enabled = True
    if demote is not None:
        value = demote.strip().lower()
        if value == "off":
            enabled = False
        elif value not in ("", "on"):
            raise ValueError(
                f"无效的 --auth-demote 值: {demote!r}（可选 on/off）")
    exempt: set[tuple[str, str]] = set()
    if pass_list:
        for raw in pass_list.split(","):
            entry = raw.strip()
            if not entry:
                continue
            product, sep, api = entry.partition(":")
            p = product.strip().casefold()
            if not p:
                raise ValueError(
                    f"--auth-demote-pass 条目缺少产品名: {entry!r}")
            a = api.strip().casefold()
            if not sep or a in ("", "*"):
                a = "*"
            exempt.add((p, a))
    return AuthDemotePolicy(enabled=enabled, exempt=frozenset(exempt))


def build_openapi_config(args: argparse.Namespace, dep: Deployment | None = None, *,
                         data_enabled: bool = False,
                         policy_store: PolicyStore | None = None,
                         audit_sink: AuditSink | None = None) -> ServiceConfig:
    """openapi 模式 ServiceConfig 构建（internal seam；external seam 是
    huaweicloud_open_mcp.deployment.build_app）。

    dep 缺省时按 args+os.environ 自解析（测试直调原口）；共享旋钮
    （mock/policy/audit）由 Deployment 归一供给，store/sink 可由装配方
    构造注入（混装共享单例，消灭事后覆写）。
    """
    dep = dep or resolve_deployment(args)
    env = dep.env
    region = (getattr(args, "region", None)
              or env.get(ENV_REGION) or None)
    hints_file = (getattr(args, "hints", None)
                  or env.get(ENV_OPENAPI_HINTS))
    deprecated_file = (getattr(args, "deprecated_index", None)
                       or env.get(ENV_DEPRECATED_INDEX))
    deprecated_mode = (getattr(args, "deprecated_mode", None)
                       or env.get(ENV_DEPRECATED_MODE))
    if deprecated_mode and not deprecated_file:
        raise ValueError("--deprecated-mode 需要同时配置 --deprecated-index")
    if deprecated_mode not in (None, "annotate", "hide", "off"):
        raise ValueError(f"无效的 deprecated-mode: {deprecated_mode}"
                         "（可选 annotate/hide/off）")
    deprecated_index = load_deprecated_index(deprecated_file or None)
    entity_index_file = (getattr(args, "entity_index", None)
                         or env.get(ENV_ENTITY_INDEX))
    if policy_store is None:
        policy_store = PolicyStore(dep.policy_file) if dep.policy_file else None
    spill_raw = getattr(args, "spill_dir", None)
    if spill_raw is None:
        spill_raw = env.get(ENV_SPILL_DIR)
    demote_raw = (getattr(args, "auth_demote", None)
                  or env.get(ENV_AUTH_DEMOTE))
    demote_pass_raw = (getattr(args, "auth_demote_pass", None)
                       or env.get(ENV_AUTH_DEMOTE_PASS))
    corrections_raw = (getattr(args, "metadata_corrections", None)
                       or env.get(ENV_METADATA_CORRECTIONS))
    return ServiceConfig(
        region=region or "cn-north-4",
        mock=dep.mock,
        policy_store=policy_store,
        policy_rules=policy_store.rules() if policy_store else None,
        credentials=None if dep.mock else cred_mod.get_credentials(),
        mock_base=dep.mock_base or apie_mock.MOCK_BASE,
        mock_passthrough=dep.mock_passthrough,
        hints=load_hints_file(hints_file),
        deprecated_index=deprecated_index,
        deprecated_mode=deprecated_mode or ("annotate" if deprecated_file else "off"),
        entity_graph=load_entity_index(entity_index_file),
        auth_demote=parse_auth_demote_policy(demote_raw, demote_pass_raw),
        corrections=load_metadata_corrections(corrections_raw),
        audit_sink=audit_sink if audit_sink is not None else sink_from_path(dep.audit_file),
        spill=parse_spill_config(spill_raw, data_enabled=data_enabled),
    )


def build_openapi_app(service: ToolService | None = None, *,
                      log_level: str = "INFO",
                      elicit_mode: str = "off") -> MCPServer:
    svc = service or ToolService()
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=build_instructions(svc.config.hints),
                       log_level=log_level)  # type: ignore[arg-type]
    register_openapi_tools(server, svc, consent_mode=elicit_mode)
    return server


def register_openapi_tools(server: MCPServer, svc: ToolService, *,
                           consent_mode: str,
                           include_manage_policy: bool = True) -> None:
    """注册 openapi 模式工具（7 领域工具 + manage_policy 共 8；混装装配复用）。

    include_manage_policy：混装时 manage_policy 全局只注册一次（由 composite 决定归属）。
    """

    def _consent(ctx: Context | None) -> PolicyConsent:
        assert ctx is not None, "Context injected by MCP framework"
        # choice→scope 映射内聚于 PolicyConsent：api=一次性（scope=once，一次用户
        # 确认只放行一次执行）/ api_session=最小规则会话内（scope=session）/
        # product=产品级（scope=session，会话内生效）
        grant = functools.partial(svc.manage_policy, "add")
        return PolicyConsent(consent_mode, ctx_elicit_fn(ctx), grant)

    @server.tool()
    def search_apis(query: str, limit: int = 8,
                    category: str | None = None) -> SearchApisResult | ToolError:
        """第 0 步：跨产品全局检索（实体图谱）。用户意图未指明产品/API 时先用本工具。

        返回候选产品（中文名/分类/link）+ 每产品代表 API + matched_via 匹配证据
        （别名/口语关键词/tag 命中），据此再用 list_apis/get_api 收窄。
        limit 默认 8、上限 20；limit=-1 为不限制哨兵（返回全部命中，truncated 恒 false）。
        废弃接口治理同 list_apis（--deprecated-mode annotate 标注 / hide 隐藏）。
        图谱为构建期快照（非实时）；未配置实体索引时返回拒绝。
        """
        return svc.search_apis(query, limit=limit, category=category)

    @server.tool()
    def list_products(category: str | None = None,
                      keyword: str | None = None) -> ProductListResult | ToolError:
        """第一步：列出华为云产品（分类、中文名、是否全局级服务）。

        基于用户任务语义选择目标产品；不确定时用 keyword 按产品名/中文名搜索。
        选定产品后用 list_apis 浏览其 API 目录。
        """
        return svc.list_products(category=category, keyword=keyword)

    @server.tool()
    def get_product(product: str) -> ProductResult | ToolError:
        """确认单个产品详情（分类/是否全局级服务）。全局级服务（is_global=true）认证模型不同。
        """
        return svc.get_product(product)

    @server.tool()
    def list_apis(product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        """第二步：列出产品的 API 目录。

        结果含 tag_groups（产品全量 tag 概览，不受过滤影响）：先用 tag 收窄目录，
        接口较多时用 search/limit/offset 分页浏览。选定候选接口后用 get_api 读文档。
        """
        return svc.list_apis(product, tag=tag, search=search, limit=limit, offset=offset)

    @server.tool()
    def get_api(product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        """第三步：获取接口完整文档（方法/路径/参数必填性/类型/枚举/x-constraint 约束/响应结构）。

        执行前必读；x-constraint 描述调用前置条件与限制。
        region 可选，缺省部署默认（cn-north-4）；返回文档含该 region 端点与
        参数上下文，须与 execute_api 使用同一 region。
        """
        return svc.get_api(product, api, region=region)

    @server.tool()
    def get_api_examples(product: str, api: str,
                         region: str | None = None) -> ExamplesResult | ToolError:
        """获取接口的官方请求示例（x-request-examples），用于指导参数填写。

        region 语义同 get_api，与 execute_api 保持一致。
        """
        return svc.get_api_examples(product, api, region=region)

    @server.tool()
    async def execute_api(product: str, api: str, region: str | None = None,
                          params: dict | None = None,
                          ctx: Context | None = None) -> ExecuteResult:
        """第四步：执行华为云 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。

        params 约定：路径参数/query 参数直接平铺，请求体放 params["body"]。
        mock 模式下 params["_status_code"]/params["_number"] 控制 mock 数据。
        region 决定目标端点，与 get_api 使用同一 region；路径含 `{project_id}`
        或依赖 `X-Project-Id` 头的 API 跨 region 执行时，在 params 显式提供目标
        region 的 project_id；无效 region 时元数据静默回退默认 region 文档。
        OBS 对象上传/下载（PutObject/GetObject/AppendObject/UploadPart）恒走预签发：
        直接返回 presign 信封（url/method/expires_in + signed_content_type +
        headers 照抄清单），客户端凭 URL 直连 OBS 完成字节流，不经 gateway、
        不限大小；_presign_expires 有效期秒数默认 900。Content-Type 参与签名：
        上传建议显式传 _presign_content_type 锁定类型并按 headers 原样携带；
        未锁定时签名按空 CT 计算，直连请求不得携带 Content-Type 头
        （信封 note 字段给出警示口径）。桶管理类接口仍由 gateway 直连执行。

        被 policy 拒绝时不要绕过：直接重试本工具，server 将经 elicitation
        向用户弹窗五选一提议授予（用户确认后热生效并携带 granted_rule）：
        api=最小规则（一次性，用后即焚）/ api_session=最小规则（会话内，本次
        会话内持续放行该 API，重启即失）/ product=产品级规则如 "VPC:*=allow"
        （会话内放行该产品全部 API，重启即失）/ readonly=产品级只读规则集
        （会话内放行 List/Show/Get/Query 四组只读 API，浏览类任务首选，重启
        即失）/ none=不授予；
        默认 off 或客户端不支持 elicitation 时，拒绝原因附带同样的兜底指引
        （先经交互式问询向用户确认，再经 manage_policy 授予）；
        亦可经 manage_policy 授予（add/remove 前服务端先 elicit 确认）。

        超大响应落盘：响应超过 200k 字符时自动落盘，body 为截断预览，结果携带
        spill 信封（path/format/bytes/note 消费指引，部署混装 data 模式时指引
        query_data 直读该文件）；params["_spill"]=false 按次退出（控制键不进入
        请求）。未配置落盘目录时保持纯截断行为。
        """
        result = svc.execute_api(product, api, region=region, params=params)
        if isinstance(result, dict) and result.get("ok") is False:
            offer = svc.policy_denial_offer(product, api,
                                            denial_reason=result.get("reason"))
            if offer is not None:
                result = cast(ExecuteResult,
                              await _consent(ctx).offer_grant(offer, result))
        return result

    if include_manage_policy:
        @server.tool()
        async def manage_policy(action: str, line: str | None = None,
                                scope: str | None = None,
                                ttl_seconds: int | None = None,
                                ctx: Context | None = None) -> dict[str, Any]:
            """管理 safety policy（list/add/remove），改动热生效、无需重启 server。

            四档 scope：once 一次性（仅放行下一次执行，用后即焚，重启即失）/
            session 会话内（缺省，本次 code agent 会话，重启即失、无需回收）/
            temporary 临时（内存 + ttl_seconds 自动过期，缺省 3600s）/ permanent 永久
            （写入策略文件，跨重启）。仅 permanent 落盘。
            action=list 查看当前全部规则（结构化 rules 含 scope/expires_in + 文件全文）；
            action=add 新增规则（自动插到会遮蔽它的 deny 规则之前，如 "OBS:GetObject=allow"）；
            action=remove 按语义移除首个匹配规则（跨层：先会话/临时后文件；不接受
            scope/ttl_seconds）。
            安全约定：先经交互式问询（如 question 工具）向用户确认再 add/remove；开启
            elicitation 时由服务端弹窗确认，未开启/客户端不支持时由调用方自行完成问询确认。
            未配置 policy 文件时本工具拒绝执行（不创建文件）。
            """
            return await gated_manage_policy(
                _consent(ctx), svc.manage_policy, action, line=line,
                scope=scope, ttl_seconds=ttl_seconds)

build_config = build_openapi_config
build_app = build_openapi_app
