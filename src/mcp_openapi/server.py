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
- 被拒接口确属任务必需时：先向用户确认，再调用 `manage_policy(action="add", line=...)`
  授予最小规则（如 "OBS:GetObject=allow"），规则热生效后重试即可通过；
  部署开启 elicitation（--elicitation auto/required）时重新调用被拒工具，服务端会
  经 MCP elicitation 向用户弹窗提议授予，结果携带 `granted_rule` 字段；
- 也可直接调用 `manage_policy`：**改动热生效、无需重启 server**；默认
  （--elicitation off）无弹窗，务必先向用户确认再调用；开启 elicitation 后
  add/remove 由服务端先经 elicitation 向用户确认；
  临时授权建议在任务完成后用 `manage_policy(action="remove", ...)` 回收。

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


def build_instructions(gate: Gate) -> str:
    """按门栓生成 instructions：基础指引 + 产品授权范围。"""
    text = INSTRUCTIONS_OPENAPI + "\n## 产品授权范围\n\n- " + gate.describe() + "。\n"
    if gate.restrict:
        text += ("\n当用户请求的产品不在上述授权范围内时，不要调用任何工具，"
                 "直接回复该产品不在授权范围内。\n")
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
        audit_sink=sink_from_path(audit_file),
    )


def build_openapi_app(service: ToolService | None = None, *,
                      log_level: str = "INFO",
                      elicit_mode: str = "off") -> MCPServer:
    svc = service or ToolService()
    consent_mode = elicit_mode
    server = MCPServer(name="huaweicloud-open-mcp", version="0.1.0",
                       instructions=build_instructions(svc.config.gate),
                       log_level=log_level)  # type: ignore[arg-type]

    def _consent(ctx: Context | None) -> PolicyConsent:
        assert ctx is not None, "Context injected by MCP framework"
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
        向用户提议授予最小规则（用户确认后热生效并携带 granted_rule）；
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

    @server.tool()
    async def manage_policy(action: str, line: str | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        """管理 safety policy（list/add/remove），改动热生效并写回策略文件，无需重启 server。

        action=list 查看当前全部规则；action=add 新增规则（自动插到会遮蔽它的 deny
        规则之前，如 "OBS:GetObject=allow"）；action=remove 按语义删除首个匹配规则。
        安全约定：add/remove 由服务端先经 elicitation 向用户确认，授予最小规则；
        客户端不支持 elicitation 时退回约定由调用方先行确认。临时授权用完即回收。
        未配置 policy 文件时本工具拒绝执行（不创建文件）。
        """
        if ((action or "").strip().lower() in ("add", "remove")
                and (line or "").strip()):
            blocked = await _consent(ctx).gate_change(action, line or "")
            if blocked:
                return {"ok": False, "action": action, "reason": blocked}
        return svc.manage_policy(action, line=line)

    return server


build_config = build_openapi_config
build_app = build_openapi_app
