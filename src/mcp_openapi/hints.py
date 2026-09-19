"""openapi 自定义提示注入（Hints）：部署侧配置驱动的发现链提示。

与 DeprecatedIndex 同风格：--hints 配置文件 → Hints 值对象 → service 在发现
工具结果信封附加提示字段、server instructions 追加全局段。未配置
（empty()）时行为与现状完全一致。

粒度：全局 instructions + 产品级 notes + 产品级 SOP（sops）+ API 级 apis。
产品键归一化 upper（productshort 惯例）；API 键归一化 lower（大小写不敏感，
对齐 apie.live_fallback 匹配语义）。合并策略内聚于 combined_notes：产品在前、
空段跳过、换行连接。sops 为 mapping-only（任务名 → string | array[string] 或
{"description"?, "steps"?} dict），parse 期一次加工为结构化任务单源
（_SopTask）；product_sops() 访问时惰性 join 全文渲染文本，product_sops_index()
派生轻量索引（name + description，不含步骤——发现面瘦身，全文经
get_product/list_apis 的 include_sops opt-in 拉取）。
"""

from dataclasses import dataclass, field
from typing import Any

from common.optconf import load_opt_file
from common.types import SopIndexEntry


def _clean(text: Any, where: str) -> str | None:
    """字符串原样保留；空串视为未配置；非字符串类型抛错（启动快速失败）。"""
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError(f"{where} 必须是字符串")
    return text if text.strip() else None


_SOP_TASK_KEYS = {"description", "steps"}


@dataclass(frozen=True)
class _SopTask:
    """单个 SOP 任务（模块私有：测试面经 product_sops / product_sops_index 访问器）。

    body 为步骤渲染正文（string 原文 / array 自动编号 ``1. x``）；description
    为可选任务描述。渲染方言（编号压实、空段丢弃、任务间空行）内聚本模块。
    """

    name: str
    description: str | None
    body: str


def _render_steps(steps_raw: list[Any], where: str) -> str | None:
    """步骤数组 → 编号正文；空数组/全空步骤返回 None（按 _clean 纪律丢弃并压实编号）。"""
    steps: list[str] = []
    for i, step in enumerate(steps_raw):
        text = _clean(step, f"{where} 步骤 {i + 1}")
        if text is not None:
            steps.append(text)
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) if steps else None


def _render_sops(raw: Any, where: str) -> list[_SopTask]:
    """产品级 SOP 值 → 结构化任务单源（mapping-only，任务按配置顺序）。

    任务值三形态：string（步骤原文）/ array[string]（步骤数组，自动编号）/
    dict（新形态 ``{"description"?: str, "steps"?: string | array[string]}``，
    键白名单严格校验，未知键抛错启动快速失败）。description/steps 均缺或全空
    的任务按 _clean 纪律丢弃；全空则返回空列表（视为未配置）。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where} sops 必须是 mapping")
    tasks: list[_SopTask] = []
    for tname, tval in raw.items():
        if not isinstance(tname, str) or not tname.strip():
            raise ValueError(f"{where} sops 任务名必须是非空字符串")
        label = tname.strip()
        task_where = f"{where} sops 任务 {label}"
        description: str | None
        body: str | None
        if isinstance(tval, str):
            description = None
            body = _clean(tval, task_where)
        elif isinstance(tval, list):
            description = None
            body = _render_steps(tval, task_where)
        elif isinstance(tval, dict):
            extra = set(tval) - _SOP_TASK_KEYS
            if extra:
                raise ValueError(f"{task_where} 含未知键: {sorted(extra)}")
            description = _clean(tval.get("description"), f"{task_where} description")
            steps_raw = tval.get("steps")
            if isinstance(steps_raw, list):
                body = _render_steps(steps_raw, task_where)
            else:
                body = _clean(steps_raw, f"{task_where} steps")
        else:
            raise ValueError(
                f"{task_where} 必须是字符串、字符串数组或 description/steps mapping")
        if description is None and body is None:
            continue
        tasks.append(_SopTask(name=label, description=description, body=body or ""))
    return tasks


@dataclass(frozen=True)
class Hints:
    """提示注入值对象。products: {PRODUCT_UPPER: (notes, {API_LOWER: text})}；
    sops: {PRODUCT_UPPER: [_SopTask]}（产品级 SOP 结构化单源，parse 期一次加工）。

    api_notes_in_list_apis（缺省 True = 现状）：False 时 list_apis 条目级
    API 提示被抑制（顶层产品级与 get_api 合并提示不受影响）。
    """

    instructions: str | None = None
    products: dict[str, tuple[str | None, dict[str, str]]] = field(default_factory=dict)
    sops: dict[str, list[_SopTask]] = field(default_factory=dict)
    api_notes_in_list_apis: bool = True

    def product_notes(self, product: str) -> str | None:
        """产品级提示（未配置返回 None）。"""
        entry = self.products.get((product or "").upper())
        return entry[0] if entry else None

    def product_sops(self, product: str) -> str | None:
        """产品级 SOP 全文渲染文本（访问时惰性 join；未配置返回 None）。

        每任务渲染为 ``任务名：\\n正文``，description 存在时并入正文首行；
        任务间空行。旧形态（string/array）渲染与历史实现逐字节一致。
        """
        tasks = self.sops.get((product or "").upper())
        if not tasks:
            return None
        parts: list[str] = []
        for t in tasks:
            head = f"{t.name}：\n{t.description}" if t.description else f"{t.name}："
            parts.append(f"{head}\n{t.body}" if t.body else head)
        return "\n\n".join(parts)

    def product_sops_index(self, product: str) -> list[SopIndexEntry] | None:
        """产品级 SOP 轻量索引（name + 可选 description，不含步骤；未配置返回 None）。"""
        tasks = self.sops.get((product or "").upper())
        if not tasks:
            return None
        entries: list[SopIndexEntry] = []
        for t in tasks:
            entry: SopIndexEntry = {"name": t.name}
            if t.description:
                entry["description"] = t.description
            entries.append(entry)
        return entries

    def api_notes(self, product: str, api: str) -> str | None:
        """仅 API 级提示（不含产品级；未配置返回 None）。"""
        entry = self.products.get((product or "").upper())
        if not entry:
            return None
        return entry[1].get((api or "").lower())

    def combined_notes(self, product: str, api: str) -> str | None:
        """合并文案：产品在前、API 在后、空段跳过、换行连接；双空返回 None。"""
        parts = [t for t in (self.product_notes(product), self.api_notes(product, api)) if t]
        return "\n".join(parts) if parts else None

    @classmethod
    def empty(cls) -> "Hints":
        """未配置提示：全部查询返回 None（no-op）。"""
        return cls()


def parse_hints(raw: Any) -> Hints:
    """把配置解析为 Hints。支持产品值 string 简写或 {notes, apis} 对象两种形态。

    严格校验：非 mapping、未知键、非法值类型抛 ValueError（启动快速失败）。
    """
    if not isinstance(raw, dict):
        raise ValueError("hints 配置必须是 mapping")
    unknown = set(raw) - {"instructions", "products", "api_notes_in_list_apis"}
    if unknown:
        raise ValueError(f"hints 配置含未知键: {sorted(unknown)}")
    instructions = _clean(raw.get("instructions"), "hints instructions")
    flag = raw.get("api_notes_in_list_apis", True)
    if not isinstance(flag, bool):
        raise ValueError("hints api_notes_in_list_apis 必须是布尔值")
    products: dict[str, tuple[str | None, dict[str, str]]] = {}
    sops: dict[str, list[_SopTask]] = {}
    raw_products = raw.get("products") or {}
    if not isinstance(raw_products, dict):
        raise ValueError("hints products 必须 mapping")
    for key, val in raw_products.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("hints products 键必须是非空字符串")
        where = f"hints 产品 {key}"
        notes: str | None
        apis: dict[str, str] = {}
        sop_tasks: list[_SopTask] = []
        if isinstance(val, str):
            notes = _clean(val, where)
        elif isinstance(val, dict):
            extra = set(val) - {"notes", "apis", "sops"}
            if extra:
                raise ValueError(f"{where} 含未知键: {sorted(extra)}")
            notes = _clean(val.get("notes"), f"{where} notes")
            raw_apis = val.get("apis") or {}
            if not isinstance(raw_apis, dict):
                raise ValueError(f"{where} 的 apis 必须 mapping")
            for akey, aval in raw_apis.items():
                if not isinstance(akey, str) or not akey.strip():
                    raise ValueError(f"{where} 的 apis 键必须是非空字符串")
                text = _clean(aval, f"{where} API {akey}")
                if text is not None:
                    apis[akey.strip().lower()] = text
            sop_tasks = _render_sops(val["sops"], where) if "sops" in val else []
        else:
            raise ValueError(f"{where} 必须是字符串或 mapping")
        products[key.strip().upper()] = (notes, apis)
        if sop_tasks:
            sops[key.strip().upper()] = sop_tasks
    return Hints(instructions=instructions, products=products, sops=sops,
                 api_notes_in_list_apis=flag)


DEFAULT_HINTS_FILE = "help-docs-hints.json"


def load_hints_file(path: str | None) -> Hints:
    """加载 hints 配置文件：CLI/env 原始值 → Hints 的唯一语义入口。

    分支纪律委托 common.optconf.load_opt_file（单一实现）：
    - None → 缺省档 DEFAULT_HINTS_FILE（config_path 解析，文件缺失静默
      Hints.empty()，隐式缺省不 fail-fast）；
    - 空串 / "off"（大小写不敏感）→ 显式禁用 Hints.empty()；
    - 显式路径/裸名 → resolve_config_arg 解析加载，缺失 fail-fast
      （FileNotFoundError 列全候选）。JSON 非法恒 fail-fast（静默仅豁免
      「文件不存在」，不豁免「内容写坏」）。
    """
    return load_opt_file(path, parse=parse_hints, off=Hints.empty(),
                         default_name=DEFAULT_HINTS_FILE)
