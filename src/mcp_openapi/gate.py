"""openapi 产品准入门栓（Gate）：控制哪些产品可经 openapi 模式可见/调用。

产品级白名单；未配置（`unrestricted()`）时不限制。与 safety policy 双层：
Gate 产品级粗滤在前，safety API 级细规则在后（execute_api 依次都过）。
"""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Gate:
    """产品准入门栓。restrict=False 时不限制（默认）；restrict=True 时严格白名单。"""

    allowed: frozenset[str] = field(default_factory=frozenset)
    restrict: bool = False

    def allows(self, product: str) -> bool:
        """产品是否准入（大小写不敏感，按 productshort）。"""
        if not self.restrict:
            return True
        return (product or "").upper() in self.allowed

    def filter_products(self, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """过滤产品分组：隐藏越界产品，并折叠空分类。不限制时原样返回。"""
        if not self.restrict:
            return groups
        out: list[dict[str, Any]] = []
        for g in groups:
            kept = [p for p in (g.get("products") or [])
                    if isinstance(p, dict)
                    and (p.get("productshort") or "").upper() in self.allowed]
            if not kept:
                continue
            ng = dict(g)
            ng["products"] = kept
            out.append(ng)
        return out

    def describe(self) -> str:
        """生成注入 instructions 的准入范围文案。"""
        if not self.restrict:
            return "产品范围：不限制（未配置 openapi 门栓）"
        names = sorted(self.allowed)
        listed = "、".join(names) if names else "（空）"
        return f"产品范围：仅 {listed} 可用，其余产品不可见/不可调用"

    @classmethod
    def unrestricted(cls) -> "Gate":
        """默认门栓：不限制。"""
        return cls(restrict=False)


def parse_gate(raw: Any) -> Gate:
    """把配置解析为 Gate。支持 {"products": [...]} 或纯字符串列表；归一化 upper。"""
    if isinstance(raw, dict):
        products = raw.get("products") or []
    elif isinstance(raw, list):
        products = raw
    else:
        raise ValueError("gate 配置必须是 mapping（含 products 列表）或字符串列表")
    if not all(isinstance(x, str) for x in products):
        raise ValueError("gate products 每项必须是字符串")
    allowed = frozenset(x.strip().upper() for x in products if x.strip())
    return Gate(allowed=allowed, restrict=True)


def load_gate_file(path: str | None) -> Gate:
    """加载 gate 配置文件。无路径时返回不限制门栓；JSON 格式非法抛 ValueError。"""
    if not path:
        return Gate.unrestricted()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return parse_gate(data)
