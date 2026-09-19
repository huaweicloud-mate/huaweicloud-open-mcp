"""ToolService：八工具的业务编排层。

职责：配置（region/mock/policy/凭证/hints/纠偏）、拥有 MemoryStore 并注入
catalog、调用元数据纯函数层（apie.metadata）与执行层（execute/execute_obs）。
"""

from __future__ import annotations

import functools
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence, TypeVar, cast

from apie import catalog, metadata
from apie import mock as apie_mock
from apie.api_location import ApiLocation
from apie.convert_openapi2 import AuthDemotePolicy
from apie.memory_store import MemoryStore
from apie.metadata_corrections import CorrectionsProvider, MetadataCorrections
from common.audit import AuditSink
from common.audit import audited as _audited
from common.auth.credentials import Credentials
from common.elicit import DenialOffer
from common.hotconf import HotFile
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
from safety import policy as safety_policy
from safety.policy_store import PolicyStore, manage_policy_ops

from . import execute, execute_obs
from .deprecated import DeprecatedIndex
from .entity_graph import EntityGraph
from .execute_obs import ObsHttpClient
from .hints import Hints
from .signer.client import HttpClient
from .spill import SpillConfig, guard_result

logger = logging.getLogger("mcp_openapi.service")

DEFAULT_REGION = "cn-north-4"

_R = TypeVar("_R")


def _guarded(fn: Callable[..., _R]) -> Callable[..., _R]:
    """S12 通用信封守卫（与 _audited 同层横切）：预算内原样返回，工具集增删自动继承。

    refusal（ok 非 True）与 lane 已落盘信封由 guard_result 内部恒等跳过；
    spill 未配置（None）时直通。stem 从函数名与 product/api 绑定参数推导。
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(self: ToolService, *args: Any, **kwargs: Any) -> _R:
        result = fn(self, *args, **kwargs)
        cfg = self.config.spill
        if cfg is None or not isinstance(result, dict):
            return result
        bound = sig.bind(self, *args, **kwargs)
        bound.apply_defaults()
        parts = [fn.__name__]
        for name in ("product", "api"):
            value = bound.arguments.get(name)
            if isinstance(value, str) and value:
                parts.append(value)
        return cast(_R, guard_result(result, cfg=cfg, stem="-".join(parts)))

    return wrapper


@dataclass
class ServiceConfig:
    region: str = DEFAULT_REGION
    mock: bool = False
    policy_rules: Sequence[safety_policy.PolicyRule] | None = None
    policy_store: PolicyStore | None = None
    credentials: Credentials | None = None
    mock_base: str = apie_mock.MOCK_BASE
    mock_passthrough: bool = False
    http_client_factory: Callable[[], execute.SignedClient] | None = None
    mock_client_factory: Callable[[], apie_mock.MockApiClient] | None = None
    obs_client_factory: Callable[[], execute_obs.ObsClient] | None = None
    hints: Hints = Hints.empty()
    deprecated_index: DeprecatedIndex = DeprecatedIndex.empty()
    deprecated_mode: str = "off"
    entity_graph: EntityGraph = EntityGraph.empty()
    auth_demote: AuthDemotePolicy = AuthDemotePolicy()
    corrections: MetadataCorrections = MetadataCorrections.empty()
    audit_sink: AuditSink | None = None
    spill: SpillConfig | None = field(default_factory=SpillConfig.default)
    # 热刷新 holder（S23，加性字段——policy_store/policy_rules 先例）：
    # 注入时运行期跟随配置文件（stale-until-ready），缺省 None 走上方启动
    # 快照字段（既有测试与行为逐字节不变）。装配点 build_openapi_config。
    hints_live: HotFile[Hints] | None = None
    deprecated_live: HotFile[DeprecatedIndex] | None = None
    corrections_live: HotFile[MetadataCorrections] | None = None
    entity_live: HotFile[EntityGraph] | None = None


class ToolService:
    def __init__(self, config: ServiceConfig | None = None, *,
                 store: MemoryStore | None = None):
        self.config = config or ServiceConfig()
        self.store = store or MemoryStore()
        self._mock_client: apie_mock.MockApiClient | None = None
        # S23 corrections 热刷新联动：配置文件换值后定向失效详情缓存
        #（products/apis 列表与纠偏口径无关，保持缓存）。装配期一次性接线，
        # 有流量后不再改写（on_reload 在 HotFile 持锁状态执行）。
        live = self.config.corrections_live
        if live is not None:
            live.on_reload = lambda _value: self.store.clear_api_details()

    def _make_http_client(self) -> execute.SignedClient:
        if self.config.http_client_factory is not None:
            return self.config.http_client_factory()
        return HttpClient(credentials=self.config.credentials)

    def _make_obs_client(self) -> execute_obs.ObsClient:
        if self.config.obs_client_factory is not None:
            return self.config.obs_client_factory()
        return ObsHttpClient(credentials=self.config.credentials)

    def _make_mock_client(self) -> apie_mock.MockApiClient:
        if self.config.mock_client_factory is not None:
            return self.config.mock_client_factory()
        if self._mock_client is None:
            self._mock_client = apie_mock.MockApiClient(base_url=self.config.mock_base)
        return self._mock_client

    def load_api_doc(self, product: str, api_name: str, region: str | None = None
                     ) -> ApiLocation | None:
        """查找接口 OpenAPI 文档（内存缓存或远端拉取），返回 (doc, path, method, op) 或 None。

        返回的 doc 按写入时点的纠偏世代纠偏（生产时点落位——live_fallback/
        doc_compose，ADR-0001）；corrections 走现读源（S23：注入 holder 时
        传 provider，LiveFallback 在 fetch 时点现读；否则用启动快照值对象），
        热刷新经 on_reload → MemoryStore.clear_api_details() 定向失效。
        auth_demote 为启动期常量随转换固化进缓存。
        """
        return catalog.find_api_doc(self.store, product, api_name,
                                    region or self.config.region,
                                    auth_demote=self.config.auth_demote,
                                    corrections=self._corrections_source())

    def _corrections_source(self) -> "MetadataCorrections | CorrectionsProvider":
        """corrections 现读源（S23）：注入 holder 时传 provider（fetch 时点
        现读，竞态窗口从秒级收窄到亚毫秒），否则用启动快照值对象。"""
        live = self.config.corrections_live
        if live is not None:
            return live
        return self.config.corrections

    def _effective_policy_rules(self) -> Sequence[safety_policy.PolicyRule] | None:
        """当前生效规则：注入 PolicyStore 时实时热加载，否则用启动快照。"""
        if self.config.policy_store is not None:
            return self.config.policy_store.rules()
        return self.config.policy_rules

    def _hints(self) -> Hints:
        """当前生效提示（S23）：注入 hints_live 时实时热加载，否则用启动快照。

        每次工具调用恰读一次（方法开头取快照下传），同一响应内提示口径一致。
        """
        live = self.config.hints_live
        if live is not None:
            return live.get()
        return self.config.hints

    def _deprecated_index(self) -> DeprecatedIndex:
        """当前生效废弃索引（S23）：注入 deprecated_live 时实时热加载，否则用启动快照。"""
        live = self.config.deprecated_live
        if live is not None:
            return live.get()
        return self.config.deprecated_index

    def _entity_graph(self) -> EntityGraph:
        """当前生效实体图谱（S23）：注入 entity_live 时实时热加载，否则用启动快照。"""
        live = self.config.entity_live
        if live is not None:
            return live.get()
        return self.config.entity_graph

    def _check_policy(self, product: str, api: str) -> str | None:
        """检查 safety policy，返回错误描述或 None（放行）。"""
        return safety_policy.check(self._effective_policy_rules(), product, api)

    def _authorize(self, product: str, api: str) -> str | None:
        """dispatch 前的原子授权门（once 规则首次放行即焚毁）。

        委托 PolicyStore.authorize（RLock 内评估+焚毁一体）；无 store
        （启动快照模式）时直通——once 规则只存在于 store overlay。
        """
        store = self.config.policy_store
        if store is None:
            return None
        return store.authorize(product, api)

    def policy_denial_offer(self, product: str, api: str,
                            denial_reason: str | None = None) -> DenialOffer | None:
        """policy 拒绝且可授予时构造 elicitation 提议；其余返回 None。

        未配置 policy store（无可写文件）或 policy 放行 → None；
        denial_reason 非空时须与本方法复查结果一致（确保被增强的确实是
        policy 拒绝而非其它拒绝源），不一致 → None。
        coarse_rule 为产品级规则（product:*=allow，session 档授予选项）；
        readonly_rules 为产品级只读规则集（List/Show/Get/Query 四条，
        session 档授予选项，safety.policy.readonly_grant_rules 构造）。
        """
        if self.config.policy_store is None:
            return None
        err = self._check_policy(product, api)
        if err is None:
            return None
        if denial_reason is not None and denial_reason != err:
            return None
        return DenialOffer(subject=f"{product}:{api}",
                           rule=safety_policy.grant_rule(product, api), reason=err,
                           coarse_rule=safety_policy.grant_rule(product, "*"),
                           readonly_rules=safety_policy.readonly_grant_rules(product))

    @_audited
    @_guarded
    def manage_policy(self, action: str, line: str | list[str] | None = None,
                      scope: str | None = None,
                      ttl_seconds: int | None = None) -> dict[str, Any]:
        """管理 safety policy（list/add/remove），改动即时生效无需重启。

        四档 scope：permanent（写策略文件，跨重启）/ temporary（内存 + TTL 自动
        过期，ttl_seconds 缺省 3600）/ session（内存，缺省档——本次 code agent
        会话，stdio 单进程下等价进程存活期，重启即失）/
        once（内存，一次性——首次放行即焚毁）。
        remove 跨层先 overlay 后文件并回报 scope；不接受 scope/ttl_seconds。
        line 支持传字符串数组批量 add/remove（整批共享 scope，add 任一行非法
        整批不应用；信封附加 results 逐条结果）。
        安全约定：调用方（Agent）应先经交互式问询（如 question 工具）向用户
        确认再 add/remove；审计日志强制记录。
        信封语义委托 safety.policy_store.manage_policy_ops（两模式单一实现）。
        """
        return manage_policy_ops(self.config.policy_store, action,
                                 line, scope, ttl_seconds)

    # ---------- 提示注入（Hints：配置驱动塑形，copy-on-write） ----------

    def _with_product_hints(self, out: Any, product: str, hints: Hints) -> Any:
        """顶层附加产品级提示与 SOP（未配置时不加字段）。hints 由调用方单次快照下传。

        sops 与 hints 并列独立字段（notes 为一句话口径、SOP 为流程，粒度不同源）；
        两者皆未配置时原对象返回（未配置路径与现状逐字节一致）。
        """
        notes = hints.product_notes(product)
        sops = hints.product_sops(product)
        if not notes and not sops:
            return out
        patched = dict(out)
        if notes:
            patched["hints"] = notes
        if sops:
            patched["sops"] = sops
        return patched

    def _with_combined_hints(self, out: Any, product: str, api: str, hints: Hints) -> Any:
        """get_api 顶层附加合并提示（产品在前、API 在后；合并策略内聚 Hints）。"""
        notes = hints.combined_notes(product, api)
        return {**out, "hints": notes} if notes else out

    def _annotate_product_items(self, out: Any, hints: Hints) -> Any:
        """list_products 条目级：配置了 notes 的产品条目附加 hints。"""
        items = out.get("products") or []
        annotated = [(p, hints.product_notes(p.get("product", ""))) for p in items]
        if not any(notes for _, notes in annotated):
            return out
        return {**out, "products": [
            {**p, "hints": notes} if notes else p for p, notes in annotated]}

    def _annotate_deprecated(self, out: Any, product: str,
                             index: DeprecatedIndex) -> Any:
        """annotate 模式：条目级结构化标注 deprecated + replacement（S14）。"""
        items = out.get("apis") or []
        decorated: list[Any] = []
        changed = False
        for a in items:
            entry = index.entry(product, a.get("name", ""))
            if entry is not None:
                changed = True
                a = {**a, "deprecated": True}
                if entry.replacement:
                    a["replacement"] = entry.replacement
            decorated.append(a)
        return {**out, "apis": decorated} if changed else out

    def _annotate_list_apis(self, out: Any, product: str, hints: Hints) -> Any:
        """list_apis：顶层产品级提示 + 当前页条目级 API 级提示。

        api_notes_in_list_apis=False 时条目级被抑制（S13f），顶层保留。
        hints 为调用方单次快照（S23：顶层与条目级同一口径，杜绝撕裂）。
        """
        new_out = self._with_product_hints(out, product, hints)
        if not hints.api_notes_in_list_apis:
            return new_out
        items = new_out.get("apis") or []
        annotated = [(a, hints.api_notes(product, a.get("name", ""))) for a in items]
        if not any(text for _, text in annotated):
            return new_out
        return {**new_out, "apis": [
            {**a, "hints": text} if text else a for a, text in annotated]}

    # ---------- 元数据工具 ----------

    @_audited
    @_guarded
    def search_apis(self, query: str, limit: int = 8,
                    category: str | None = None) -> SearchApisResult | ToolError:
        """第 0 步：实体图谱跨产品检索（构建期快照，非实时）。"""
        logger.info("search_apis query=%r limit=%s category=%s",
                    query, limit, category or "-")
        graph = self._entity_graph()
        if graph.version == 0:
            logger.warning("search_apis entity_index=missing")
            return {"ok": False,
                    "reason": "实体索引未配置（部署侧 --entity-index），"
                              "请改用 list_products 定位产品"}
        # 废弃治理（S15 扩展）：与 list_apis 同索引同模式——
        # hide 排名前排除（机制参数，模块索引无关）；annotate service 层塑形
        mode = self.config.deprecated_mode
        exclude = None
        if mode == "hide":
            idx = self._deprecated_index()
            exclude = {ps.lower(): idx.names(ps) for ps in graph.products}
        out = graph.search_apis(query, limit=limit, category=category,
                                exclude_apis=exclude)
        if mode == "annotate":
            out = self._annotate_search_deprecated(out, self._deprecated_index())
        return cast(SearchApisResult, out)

    def _annotate_search_deprecated(self, out: Any, index: DeprecatedIndex) -> Any:
        """search_apis annotate：条目级结构化标注 deprecated + replacement
        （S14 同 idiom，copy-on-write 仅变更时重建）。已知边界：twin 归并行
        内孪生成员 api 按主产品索引查询，可能欠标注（hide 路径无此问题）。"""
        decorated_rows: list[Any] = []
        changed = False
        for row in out.get("products") or []:
            product = row.get("product", "")
            decorated_apis: list[Any] = []
            row_changed = False
            for a in row.get("apis") or []:
                entry = index.entry(product, a.get("name", ""))
                if entry is not None:
                    row_changed = True
                    a = {**a, "deprecated": True}
                    if entry.replacement:
                        a["replacement"] = entry.replacement
                decorated_apis.append(a)
            if row_changed:
                changed = True
                row = {**row, "apis": decorated_apis}
            decorated_rows.append(row)
        return {**out, "products": decorated_rows} if changed else out

    @_audited
    @_guarded
    def list_products(self, category: str | None = None,
                      keyword: str | None = None) -> ProductListResult | ToolError:
        logger.info("list_products category=%s keyword=%s", category or "-", keyword or "-")
        groups = catalog.get_products(self.store)
        if groups is None:
            logger.warning("list_products metadata=missing")
            return {"ok": False, "reason": "产品列表不可用（远端拉取失败）"}
        out = metadata.list_products(groups, category=category, keyword=keyword)
        return cast(ProductListResult, self._annotate_product_items(out, self._hints()))

    @_audited
    @_guarded
    def get_product(self, product: str) -> ProductResult | ToolError:
        logger.info("get_product product=%s", product)
        groups = catalog.get_products(self.store)
        if groups is None:
            logger.warning("get_product product=%s metadata=missing", product)
            return {"ok": False, "reason": "产品列表不可用（远端拉取失败）"}
        out = metadata.get_product(groups, product)
        if out is None:
            logger.warning("get_product product=%s result=not_found", product)
            return {"ok": False, "reason": f"产品 {product} 未找到"}
        return cast(ProductResult, self._with_product_hints(out, product, self._hints()))

    @_audited
    @_guarded
    def list_apis(self, product: str, tag: str | None = None, search: str | None = None,
                  limit: int = 20, offset: int = 0) -> ApiListResult | ToolError:
        logger.info("list_apis product=%s tag=%s search=%s limit=%d offset=%d",
                    product, tag or "-", search or "-", limit, offset)
        apis = catalog.get_apis(self.store, product)
        if apis is None:
            logger.warning("list_apis product=%s metadata=missing", product)
            return {"ok": False, "reason": "接口索引不可用（远端拉取失败）"}
        # S23 单快照：hints / 废弃索引各读一次，同一响应内口径一致（防撕裂）
        hints = self._hints()
        index = self._deprecated_index()
        mode = self.config.deprecated_mode
        out = metadata.list_apis(apis, product, tag=tag, search=search,
                                 limit=limit, offset=offset,
                                 exclude_apis=(index.names(product)
                                               if mode == "hide" else None))
        if mode == "annotate":
            out = self._annotate_deprecated(out, product, index)
        return cast(ApiListResult, self._annotate_list_apis(out, product, hints))

    @_audited
    @_guarded
    def get_api(self, product: str, api: str, region: str | None = None) -> ApiDetailResult | ToolError:
        region = region or self.config.region
        logger.info("get_api %s:%s region=%s", product, api, region)
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            logger.warning("get_api %s:%s region=%s result=not_found", product, api, region)
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        out: Any = metadata.format_api_detail(hit, product)
        return cast(ApiDetailResult,
                    self._with_combined_hints(out, product, api, self._hints()))

    @_audited
    @_guarded
    def get_api_examples(self, product: str, api: str,
                         region: str | None = None) -> ExamplesResult | ToolError:
        region = region or self.config.region
        logger.info("get_api_examples %s:%s region=%s", product, api, region)
        hit = self.load_api_doc(product, api, region)
        if hit is None:
            logger.warning("get_api_examples %s:%s region=%s result=not_found",
                           product, api, region)
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}
        op = hit.op
        return {"ok": True, "product": product, "api": api,
                "examples": metadata.extract_examples(op)}

    # ---------- 执行工具 ----------

    def _real_executor(self) -> execute.RealApiExecutor:
        client: execute.SignedClient = self._make_http_client()
        return execute.RealApiExecutor(client, self.config.credentials)

    def _mock_executor(self) -> execute.MockApiExecutor:
        return execute.MockApiExecutor(self._make_mock_client(),
                                       passthrough=self.config.mock_passthrough)

    @_audited
    @_guarded
    def execute_api(self, product: str, api: str, region: str | None = None,
                    params: dict[str, Any] | None = None) -> ExecuteResult:
        """执行 API。safety policy 细检；lane 决策（mock/obs/real）单点求值。

        spill：超限响应自动落盘（S12）；params["_spill"]=false 按次退出
        （控制键在 dispatch 前剥离，不进入 query/body）。
        """
        if (api or "").strip() == "manage_policy":
            return {"ok": False, "reason": (
                "manage_policy 是 server 内置控制面工具，请直接调用 manage_policy 工具"
                "（不经 execute_api 路由）")}
        region = region or self.config.region
        params = dict(params or {})
        spill_cfg = self.config.spill
        if params.pop("_spill", None) is False:
            spill_cfg = None
        mode = "mock" if self.config.mock else "real"
        policy_err = self._check_policy(product, api)
        if policy_err:
            logger.warning("execute %s:%s region=%s mode=%s policy=%s",
                           product, api, region, mode,
                           "unconfigured" if self._effective_policy_rules() is None else "deny")
            return {"ok": False, "reason": policy_err}

        hit = self.load_api_doc(product, api, region)
        if hit is None:
            return {"ok": False, "reason": f"接口 {api} 未找到（产品 {product}）"}

        # lane 决策单点（C6）：is_obs 恒一次求值；审计 mode 归 lane/executor 所有
        is_obs_op = execute_obs.is_obs(product, hit.doc)
        lane = "mock" if self.config.mock else ("obs" if is_obs_op else "real")

        # 预签发分支：OBS 专用，gateway 只签名不搬运字节；先于主分流
        if params.get("_presign"):
            if not is_obs_op:
                return {"ok": False,
                        "reason": "_presign 仅支持 OBS 产品（其余服务无预签发语义）"}
            gate_err = self._authorize(product, api)   # 预签发前消费一次性授权
            if gate_err:
                logger.warning("execute %s:%s region=%s mode=%s policy=%s",
                               product, api, region, mode, "deny")
                return {"ok": False, "reason": gate_err}
            return execute_obs.execute_presign_api(
                hit.doc, hit.path, hit.method, hit.op, product, api, region,
                params, credentials=self.config.credentials,
                client=None if self.config.mock else self._make_obs_client())

        # OpenAPI 元数据 schema 校验（policy 接缝）：mock/real 共享；
        # OBS lane（XML body/自身参数切分）不适用，跳过
        if not is_obs_op:
            err = execute.validate_params(hit.doc, hit.path, hit.op, params,
                                          self.config.credentials)
            if err:
                logger.warning("execute %s:%s schema=reject reason=%s", product, api, err)
                return {"ok": False, "reason": err}

        # dispatch 前原子授权门：once 规则首次放行即焚毁（校验失败不会到达此处）
        gate_err = self._authorize(product, api)
        if gate_err:
            logger.warning("execute %s:%s region=%s mode=%s policy=%s",
                           product, api, region, mode, "deny")
            return {"ok": False, "reason": gate_err}

        logger.info("execute %s:%s region=%s mode=%s policy=allow",
                    product, api, region, lane)

        if lane == "mock":
            return execute.execute_api(hit, product, api, region, params,
                                       executor=self._mock_executor(),
                                       spill=spill_cfg)

        if lane == "obs":
            if execute_obs.is_object_data_api(api, hit.op):
                # 对象数据面单口径：恒返回预签名 URL，gateway 不搬运对象字节
                return execute_obs.execute_presign_api(
                    hit.doc, hit.path, hit.method, hit.op, product, api,
                    region, params, credentials=self.config.credentials,
                    client=self._make_obs_client())
            return execute_obs.execute_obs_api(
                hit.doc, hit.path, hit.method, hit.op, product, api, region,
                params, client=self._make_obs_client(),
                credentials=self.config.credentials,
                spill=spill_cfg)

        return execute.execute_api(hit, product, api, region, params,
                                   executor=self._real_executor(),
                                   spill=spill_cfg)
