"""openapi 自定义提示注入（Hints）：部署侧配置驱动的发现链提示。

与产品门栓 Gate 同风格：--hints 配置文件 → Hints 值对象 → service 在发现
工具结果信封附加提示字段、server instructions 追加全局段。未配置
（empty()）时行为与现状完全一致。

粒度：全局 instructions + 产品级 notes + API 级 apis。产品键归一化 upper
（对齐 Gate）；API 键归一化 lower（大小写不敏感，对齐 apie.live_fallback
匹配语义）。合并策略内聚于 combined_notes：产品在前、空段跳过、换行连接。
"""

import json
from dataclasses import dataclass, field
from typing import Any


def _clean(text: Any, where: str) -> str | None:
    """字符串原样保留；空串视为未配置；非字符串类型抛错（启动快速失败）。"""
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError(f"{where} 必须是字符串")
    return text if text.strip() else None


@dataclass(frozen=True)
class Hints:
    """提示注入值对象。products: {PRODUCT_UPPER: (notes, {API_LOWER: text})}。"""

    instructions: str | None = None
    products: dict[str, tuple[str | None, dict[str, str]]] = field(default_factory=dict)

    def product_notes(self, product: str) -> str | None:
        """产品级提示（未配置返回 None）。"""
        entry = self.products.get((product or "").upper())
        return entry[0] if entry else None

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
    unknown = set(raw) - {"instructions", "products"}
    if unknown:
        raise ValueError(f"hints 配置含未知键: {sorted(unknown)}")
    instructions = _clean(raw.get("instructions"), "hints instructions")
    products: dict[str, tuple[str | None, dict[str, str]]] = {}
    raw_products = raw.get("products") or {}
    if not isinstance(raw_products, dict):
        raise ValueError("hints products 必须 mapping")
    for key, val in raw_products.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("hints products 键必须是非空字符串")
        where = f"hints 产品 {key}"
        notes: str | None
        apis: dict[str, str] = {}
        if isinstance(val, str):
            notes = _clean(val, where)
        elif isinstance(val, dict):
            extra = set(val) - {"notes", "apis"}
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
        else:
            raise ValueError(f"{where} 必须是字符串或 mapping")
        products[key.strip().upper()] = (notes, apis)
    return Hints(instructions=instructions, products=products)


def load_hints_file(path: str | None) -> Hints:
    """加载 hints 配置文件。无路径时返回空提示（no-op）；JSON 非法抛错。"""
    if not path:
        return Hints.empty()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return parse_hints(data)
