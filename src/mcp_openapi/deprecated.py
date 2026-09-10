"""openapi 废弃接口索引（DeprecatedIndex）：部署侧配置驱动的发现面治理。

与 Gate/Hints 同 idiom：--deprecated-index 配置文件 → DeprecatedIndex 值对象 →
service 在 list_apis 上做 annotate（结构化标注）/ hide（过滤）塑形。
未配置（empty()）时行为与现状完全一致。数据由帮助中心补全管线生成
（data/help_completions/deprecated.json）。

注意：annotate/hide 仅影响 list_apis 发现面；get_api/execute_api 恒可用
（发现面收窄 ≠ 详情拒绝）。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from common.paths import resolve_config_arg


def _clean_str(val: Any, where: str) -> str | None:
    """字符串原样保留；None 合法（未提供）；其它类型抛错（启动快速失败）。"""
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"{where} 必须是字符串或 null")
    return val


@dataclass(frozen=True)
class DeprecatedEntry:
    """单个废弃接口的指引条目。"""

    replacement: str | None = None
    doc_url: str | None = None


@dataclass(frozen=True)
class DeprecatedIndex:
    """废弃接口索引值对象。products: {PRODUCT_UPPER: {API_LOWER: entry}}。"""

    products: dict[str, dict[str, DeprecatedEntry]] = field(default_factory=dict)

    def entry(self, product: str, api: str) -> DeprecatedEntry | None:
        """单个接口的废弃条目（未废弃/未收录返回 None）。"""
        return self.products.get((product or "").upper(), {}).get((api or "").lower())

    def names(self, product: str) -> frozenset[str]:
        """产品内全部废弃接口名（API_LOWER），hide 模式排除集用。"""
        return frozenset(self.products.get((product or "").upper(), {}))

    @classmethod
    def empty(cls) -> "DeprecatedIndex":
        """未配置索引：全部查询返回 None/空集（no-op）。"""
        return cls()


def parse_deprecated_index(raw: Any) -> DeprecatedIndex:
    """把配置解析为 DeprecatedIndex。严格校验：非法结构抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("deprecated-index 配置必须是 mapping")
    unknown = set(raw) - {"products"}
    if unknown:
        raise ValueError(f"deprecated-index 配置含未知键: {sorted(unknown)}")
    products: dict[str, dict[str, DeprecatedEntry]] = {}
    raw_products = raw.get("products") or {}
    if not isinstance(raw_products, dict):
        raise ValueError("deprecated-index products 必须 mapping")
    for key, apis in raw_products.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("deprecated-index products 键必须是非空字符串")
        if not isinstance(apis, dict):
            raise ValueError(f"deprecated-index 产品 {key} 必须 mapping")
        entries: dict[str, DeprecatedEntry] = {}
        for akey, val in apis.items():
            if not isinstance(akey, str) or not akey.strip():
                raise ValueError(f"deprecated-index 产品 {key} 的接口键必须是非空字符串")
            where = f"deprecated-index {key}.{akey}"
            if not isinstance(val, dict):
                raise ValueError(f"{where} 必须 mapping")
            extra = set(val) - {"replacement", "doc_url"}
            if extra:
                raise ValueError(f"{where} 含未知键: {sorted(extra)}")
            entries[akey.strip().lower()] = DeprecatedEntry(
                replacement=_clean_str(val.get("replacement"), f"{where} replacement"),
                doc_url=_clean_str(val.get("doc_url"), f"{where} doc_url"))
        products[key.strip().upper()] = entries
    return DeprecatedIndex(products=products)


def load_deprecated_index(path: str | None) -> DeprecatedIndex:
    """加载废弃索引文件。无路径时返回空索引（no-op）；JSON 非法抛错。

    路径支持裸文件名：经 common.paths.resolve_config_arg 解析
    （存在的显式路径原样 > 仓库根 configs/ > 包内 configs/）。
    """
    if not path:
        return DeprecatedIndex.empty()
    with open(resolve_config_arg(path), encoding="utf-8") as f:
        data = json.load(f)
    return parse_deprecated_index(data)
