"""openapi 自定义提示注入（Hints）：部署侧配置驱动的发现链提示。

与 DeprecatedIndex 同风格：--hints 配置文件 → Hints 值对象 → service 在发现
工具结果信封附加提示字段、server instructions 追加全局段。未配置
（empty()）时行为与现状完全一致。

粒度：全局 instructions + 产品级 notes + 产品级 SOP（sops）+ API 级 apis。
产品键归一化 upper（productshort 惯例）；API 键归一化 lower（大小写不敏感，
对齐 apie.live_fallback 匹配语义）。合并策略内聚于 combined_notes：产品在前、
空段跳过、换行连接。sops 为 mapping-only（任务名 → string | array[string]），
parse 期一次渲染为文本（任务名行 + 编号步骤，任务间空行），product_sops()
为纯 dict 查找。
"""

from dataclasses import dataclass, field
from typing import Any

from common.optconf import load_opt_file


def _clean(text: Any, where: str) -> str | None:
    """字符串原样保留；空串视为未配置；非字符串类型抛错（启动快速失败）。"""
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError(f"{where} 必须是字符串")
    return text if text.strip() else None


def _render_sops(raw: Any, where: str) -> str | None:
    """产品级 SOP 值 → 渲染文本（mapping-only：任务名 → string | array[string]）。

    每任务渲染为 ``任务名：\\n内容``（string 原文 / array 自动编号 ``1. x``），
    任务按配置顺序、任务间空行；空任务名/空内容按 _clean 纪律丢弃
    （空步骤丢弃且编号压实）；全空返回 None（视为未配置）。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where} sops 必须是 mapping")
    tasks: list[str] = []
    for tname, tval in raw.items():
        if not isinstance(tname, str) or not tname.strip():
            raise ValueError(f"{where} sops 任务名必须是非空字符串")
        label = tname.strip()
        if isinstance(tval, str):
            body = _clean(tval, f"{where} sops 任务 {label}")
            if body is None:
                continue
        elif isinstance(tval, list):
            steps = []
            for i, step in enumerate(tval):
                text = _clean(step, f"{where} sops 任务 {label} 步骤 {i + 1}")
                if text is not None:
                    steps.append(text)
            if not steps:
                continue
            body = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        else:
            raise ValueError(f"{where} sops 任务 {label} 必须是字符串或字符串数组")
        tasks.append(f"{label}：\n{body}")
    return "\n\n".join(tasks) if tasks else None


@dataclass(frozen=True)
class Hints:
    """提示注入值对象。products: {PRODUCT_UPPER: (notes, {API_LOWER: text})}；
    sops: {PRODUCT_UPPER: 渲染文本}（产品级 SOP，mapping-only 配置 parse 期渲染）。

    api_notes_in_list_apis（缺省 True = 现状）：False 时 list_apis 条目级
    API 提示被抑制（顶层产品级与 get_api 合并提示不受影响）。
    """

    instructions: str | None = None
    products: dict[str, tuple[str | None, dict[str, str]]] = field(default_factory=dict)
    sops: dict[str, str] = field(default_factory=dict)
    api_notes_in_list_apis: bool = True

    def product_notes(self, product: str) -> str | None:
        """产品级提示（未配置返回 None）。"""
        entry = self.products.get((product or "").upper())
        return entry[0] if entry else None

    def product_sops(self, product: str) -> str | None:
        """产品级 SOP 渲染文本（未配置返回 None）。"""
        return self.sops.get((product or "").upper())

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
    sops: dict[str, str] = {}
    raw_products = raw.get("products") or {}
    if not isinstance(raw_products, dict):
        raise ValueError("hints products 必须 mapping")
    for key, val in raw_products.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("hints products 键必须是非空字符串")
        where = f"hints 产品 {key}"
        notes: str | None
        apis: dict[str, str] = {}
        sop_text: str | None = None
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
            sop_text = _render_sops(val["sops"], where) if "sops" in val else None
        else:
            raise ValueError(f"{where} 必须是字符串或 mapping")
        products[key.strip().upper()] = (notes, apis)
        if sop_text is not None:
            sops[key.strip().upper()] = sop_text
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
