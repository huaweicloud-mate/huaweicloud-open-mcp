"""openapi 模式 server 装配（MCP 协议层）。"""

import argparse
import functools
import os
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from apie import mock as apie_mock
from common.audit import sink_from_path
from common.auth import credentials as cred_mod
from common.elicit import PolicyConsent, ctx_elicit_fn
from common.types import (
    ApiDetailResult,
    ApiListResult,
    ExamplesResult,
    ExecuteResult,
    ProductListResult,
    ProductResult,
    ToolError,
)
from safety.policy_store import PolicyStore

from .gate import Gate, load_gate_file
from .hints import Hints, load_hints_file
from .service import ServiceConfig, ToolService

INSTRUCTIONS_OPENAPI = """# 华为云 Open MCP 使用指引（OpenAPI 直连模式）

## 推荐工作流（渐进收窄，LLM 决策）

1. `list_products`：获取产品列表（含中文名/分类/接口数），基于用户任务语义确定产品范围；
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
  或产品级 "VPC:*=allow"），规则热生效后重试即可通过；问询按四选一口径：
  api=最小规则（一次性，用后即焚）/ api_session=最小规则（会话内，本次会话内
  持续放行该 API，重启即失）/ product=产品级规则（会话内，覆盖该产品全部
  API，重启即失）/ none=不授予；
  部署开启 elicitation（--elicitation auto/required）时重新调用被拒工具，服务端会
  经 MCP elicitation 弹出同样的四选一提议（结果携带 `granted_rule` 字段）；
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

## 其它

- region 默认 cn-north-4；非默认 region 需显式传 region 参数；
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


def build_instructions(gate: Gate, hints: Hints | None = None) -> str:
    """按门栓生成 instructions：基础指引 + 产品授权范围 + 部署自定义指引段。"""
    text = INSTRUCTIONS_OPENAPI + "\n## 产品授权范围\n\n- " + gate.describe() + "。\n"
    if gate.restrict:
        text += ("\n当用户请求的产品不在上述授权范围内时，不要调用任何工具，"
                 "直接回复该产品不在授权范围内。\n")
    global_text = (hints.instructions if hints is not None else None)
    if global_text:
        text += ("\n## 部署自定义指引\n\n" + global_text.strip() + "\n")
    return text


def build_openapi_config(args: argparse.Namespace) -> ServiceConfig:
    mock = (args.mock if args.mock is not None
            else os.environ.get("HUAWEICLOUD_MCP_MOCK", "") in ("1", "true", "yes"))
    policy_file = args.policy or os.environ.get("HUAWEICLOUD_MCP_POLICY_FILE")
    region = args.region or os.environ.get("HUAWEICLOUD_MCP_REGION") or None
    mock_base = args.mock_base or os.environ.get("HUAWEICLOUD_MCP_MOCK_BASE") or None
    mock_passthrough = (args.mock_passthrough if getattr(args, "mock_passthrough", None) is not None
                        else os.environ.get("HUAWEICLOUD_MCP_MOCK_PASSTHROUGH", "")
                        in ("1", "true", "yes"))
    gate_file = getattr(args, "gate", None) or os.environ.get("HUAWEICLOUD_MCP_OPENAPI_GATE")
    hints_file = getattr(args, "hints", None) or os.environ.get("HUAWEICLOUD_MCP_OPENAPI_HINTS")
    audit_file = (getattr(args, "audit_file", None)
                  or os.environ.get("HUAWEICLOUD_MCP_AUDIT_FILE"))
    policy_store = PolicyStore(policy_file) if policy_file else None
    return ServiceConfig(
        region=region or "cn-north-4",
        mock=mock,
        policy_store=policy_store,
        policy_rules=policy_store.rules() if policy_store else None,
        credentials=None if mock else cred_mod.get_credentials(),
        mock_base=mock_base or apie_mock.MOCK_BASE,
        mock_passthrough=mock_passthrough,
        gate=load_gate_file(gate_file) if gate_file else Gate.unrestricted(),
        hints=load_hints_file(hints_file),
        audit_sink=sink_from_path(audit_file),
    )


def build_openapi_app(service: ToolService | None = None, *,
                      log_level: str = "INFO",
                      elicit_mode: str = "off") -> MCPServer:
    svc = service or ToolService()
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=build_instructions(svc.config.gate, svc.config.hints),
                       log_level=log_level)  # type: ignore[arg-type]
    register_openapi_tools(server, svc, consent_mode=elicit_mode)
    return server


def register_openapi_tools(server: MCPServer, svc: ToolService, *,
                           consent_mode: str,
                           include_manage_policy: bool = True) -> None:
    """注册 openapi 模式 7 工具（混装装配复用；instructions 由 builder 自持）。

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
    def list_products(category: str | None = None,
                      keyword: str | None = None) -> ProductListResult | ToolError:
        """第一步：列出华为云产品（分类、中文名、接口数、是否全局级服务）。

        基于用户任务语义选择目标产品；不确定时用 keyword 按产品名/中文名搜索。
        选定产品后用 list_apis 浏览其 API 目录。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        当用户请求的产品不在授权范围时，不要调用任何工具，直接回复拒绝。
        """
        return svc.list_products(category=category, keyword=keyword)

    @server.tool()
    def get_product(product: str) -> ProductResult | ToolError:
        """确认单个产品详情（分类/接口数/是否全局级服务）。全局级服务（is_global=true）认证模型不同。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.get_product(product)

    @server.tool()
    def list_apis(product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        """第二步：列出产品的 API 目录。

        结果含 tag_groups（产品全量 tag 概览，不受过滤影响）：先用 tag 收窄目录，
        接口较多时用 search/limit/offset 分页浏览。选定候选接口后用 get_api 读文档。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.list_apis(product, tag=tag, search=search, limit=limit, offset=offset)

    @server.tool()
    def get_api(product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        """第三步：获取接口完整文档（方法/路径/参数必填性/类型/枚举/x-constraint 约束/响应结构）。

        执行前必读；x-constraint 描述调用前置条件与限制。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.get_api(product, api, region=region)

    @server.tool()
    def get_api_examples(product: str, api: str,
                         region: str | None = None) -> ExamplesResult | ToolError:
        """获取接口的官方请求示例（x-request-examples），用于指导参数填写。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
        """
        return svc.get_api_examples(product, api, region=region)

    @server.tool()
    async def execute_api(product: str, api: str, region: str | None = None,
                          params: dict | None = None,
                          ctx: Context | None = None) -> ExecuteResult:
        """第四步：执行华为云 API。执行前强制过 safety policy（未配置 policy 时全部拒绝）。

        params 约定：路径参数/query 参数直接平铺，请求体放 params["body"]。
        mock 模式下 params["_status_code"]/params["_number"] 控制 mock 数据。
        OBS 对象上传/下载（PutObject/GetObject/AppendObject/UploadPart）恒走预签发：
        直接返回 presign 信封（url/method/expires_in + signed_content_type +
        headers 照抄清单），客户端凭 URL 直连 OBS 完成字节流，不经 gateway、
        不限大小；_presign_expires 有效期秒数默认 900。Content-Type 参与签名：
        上传建议显式传 _presign_content_type 锁定类型并按 headers 原样携带；
        未锁定时签名按空 CT 计算，直连请求不得携带 Content-Type 头
        （信封 note 字段给出警示口径）。桶管理类接口仍由 gateway 直连执行。

        被 policy 拒绝时不要绕过：直接重试本工具，server 将经 elicitation
        向用户弹窗四选一提议授予（用户确认后热生效并携带 granted_rule）：
        api=最小规则（一次性，用后即焚）/ api_session=最小规则（会话内，本次
        会话内持续放行该 API，重启即失）/ product=产品级规则如 "VPC:*=allow"
        （会话内放行该产品全部 API，重启即失）/ none=不授予；
        默认 off 或客户端不支持 elicitation 时，拒绝原因附带同样的兜底指引
        （先经交互式问询向用户确认，再经 manage_policy 授予）；
        亦可经 manage_policy 授予（add/remove 前服务端先 elicit 确认）。

        授权范围见 instructions；仅授权产品可见/可调用，越界返回拒绝。
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
            if ((action or "").strip().lower() in ("add", "remove")
                    and (line or "").strip()):
                blocked = await _consent(ctx).gate_change(action, line or "")
                if blocked:
                    return {"ok": False, "action": action, "reason": blocked}
            return svc.manage_policy(action, line=line, scope=scope, ttl_seconds=ttl_seconds)


build_config = build_openapi_config
build_app = build_openapi_app
