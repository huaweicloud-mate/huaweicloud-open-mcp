"""上游元数据纠偏（MetadataCorrections）：配置驱动的 get_api 元数据修正。

API Explorer 元数据偶有与官方帮助文档不一致的过期文案（首例：RDS
StartupInstance 的 x-constraint 残留「该接口仅支持PostgreSQL引擎」，官方帮助
文档 rds_05_0026 接口约束无引擎限制——功能上线时 PostgreSQL 先行，后扩展到
其它引擎，文档已更新而 Explorer 元数据未同步）。纠偏条目为 doc-verified 的
逐 API 精确键事实修正：shipped configs/metadata-corrections.json 随 wheel
分发，--metadata-corrections / HUAWEICLOUD_MCP_METADATA_CORRECTIONS 支持部署
自助，off/空串显式禁用。

接缝：presentation 层（hints 同位）——service.get_api 对 format_api_detail
的新鲜信封 copy-on-write 纠偏，缓存 doc 恒不改写；离线管道 convert main()
经 correct_doc 组合同一 patch 核心。纠偏生效面 = get_api 信封 + 离线产物。
红线：逐 API 精确键，禁止通用模式删除（「数据库代理(PostgreSQL)」等 tag 的
「仅支持PostgreSQL」是真约束，泛化会误杀）；drop/replace 语义幂等，上游修复
后自动 no-op，条目可退场。

schema（严格校验，非法配置启动快速失败）：

    {"RDS:StartupInstance": {
        "evidence": "官方帮助文档 rds_05_0026（2026-05-28 更新）接口约束无引擎限制",
        "doc_url": "https://support.huaweicloud.com/api-rds/rds_05_0026.html",
        "patches": {"x-constraint": {"drop": ["该接口仅支持PostgreSQL引擎"]}}}}

键 PRODUCT:API（casefold 归一）；patch 二选一：drop（行级子串删行）/
replace（整字段无条件替换）；字段白名单 v1 仅 x-constraint（扩展即加白名单）；
evidence/doc_url/verified 为台账注记，不进结果信封。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from common.paths import resolve_config_arg

logger = logging.getLogger("apie.metadata_corrections")

DEFAULT_CORRECTIONS_FILE = "metadata-corrections.json"

# 字段白名单 v1：纠偏可触及的 op/信封级元数据字段（扩展即加白名单）
ALLOWED_FIELDS = frozenset({"x-constraint"})

_ENTRY_KEYS = frozenset({"evidence", "doc_url", "verified", "patches"})


@dataclass(frozen=True)
class FieldPatch:
    """单字段 patch（drop/replace 二选一）。apply 为字符串级核心，两个应用面共享。"""

    drop: tuple[str, ...] | None = None
    replace: str | None = None

    def apply(self, value: str) -> str | None:
        """对字符串值应用 patch。drop 无命中原样返回；全行删光返回 None。"""
        if self.replace is not None:
            return self.replace
        lines = value.split("\n")
        kept = [ln for ln in lines
                if not any(sub in ln for sub in (self.drop or ()))]
        if len(kept) == len(lines):
            return value
        return "\n".join(kept) or None


@dataclass(frozen=True)
class CorrectionEntry:
    """单 API 纠偏条目：patches 作用于文档/信封，evidence/doc_url/verified 仅台账。"""

    patches: dict[str, FieldPatch]
    evidence: str | None = None
    doc_url: str | None = None
    verified: str | None = None


@dataclass(frozen=True)
class MetadataCorrections:
    """纠偏值对象。entries: {(PRODUCT_lower, API_lower): CorrectionEntry}。"""

    entries: dict[tuple[str, str], CorrectionEntry] = field(default_factory=dict)

    def for_api(self, product: str, api: str) -> CorrectionEntry | None:
        """精确键查找（casefold 归一；未配置返回 None）。"""
        return self.entries.get(((product or "").casefold(),
                                 (api or "").casefold()))

    @classmethod
    def empty(cls) -> "MetadataCorrections":
        """未配置纠偏：全部查找返回 None（no-op）。"""
        return cls()


def _clean(text: Any, where: str) -> str | None:
    """字符串原样保留；空串视为未配置；非字符串类型抛错（启动快速失败）。"""
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError(f"{where} 必须是字符串")
    return text if text.strip() else None


def _parse_patch(raw: Any, where: str) -> FieldPatch:
    """patch 解析：drop（非空子串列表）/ replace（非空字符串）恰居其一。"""
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是 mapping")
    extra = set(raw) - {"drop", "replace"}
    if extra:
        raise ValueError(f"{where} 含未知键: {sorted(extra)}")
    drop = raw.get("drop")
    replace = raw.get("replace")
    if (drop is None) == (replace is None):
        raise ValueError(f"{where} 必须 drop/replace 二选一")
    if drop is not None:
        if not isinstance(drop, list) or not drop:
            raise ValueError(f"{where} 的 drop 必须是非空字符串列表")
        if any(not isinstance(s, str) or not s for s in drop):
            raise ValueError(f"{where} 的 drop 条目必须是非空字符串")
        return FieldPatch(drop=tuple(drop))
    if not isinstance(replace, str) or not replace:
        raise ValueError(f"{where} 的 replace 必须是非空字符串")
    return FieldPatch(replace=replace)


def parse_metadata_corrections(raw: Any) -> MetadataCorrections:
    """把配置解析为 MetadataCorrections。

    严格校验：非 mapping、键缺冒号/空段、缺 patches、字段白名单外、
    patch 形状非法、未知键抛 ValueError（启动快速失败）。
    """
    if not isinstance(raw, dict):
        raise ValueError("metadata corrections 配置必须是 mapping")
    entries: dict[tuple[str, str], CorrectionEntry] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or ":" not in key:
            raise ValueError(f"纠偏键必须是 PRODUCT:API 形式: {key!r}")
        product, _, api = key.partition(":")
        p, a = product.strip(), api.strip()
        if not p or not a:
            raise ValueError(f"纠偏键 PRODUCT:API 两侧均须非空: {key!r}")
        where = f"纠偏条目 {key}"
        if not isinstance(val, dict):
            raise ValueError(f"{where} 必须是 mapping")
        extra = set(val) - _ENTRY_KEYS
        if extra:
            raise ValueError(f"{where} 含未知键: {sorted(extra)}")
        raw_patches = val.get("patches")
        if not isinstance(raw_patches, dict) or not raw_patches:
            raise ValueError(f"{where} 的 patches 必须是非空 mapping")
        patches: dict[str, FieldPatch] = {}
        for fname, fpatch in raw_patches.items():
            if fname not in ALLOWED_FIELDS:
                raise ValueError(f"{where} 的字段 {fname!r} 不在白名单内"
                                 f"（v1 允许: {sorted(ALLOWED_FIELDS)}）")
            patches[fname] = _parse_patch(fpatch, f"{where} 的 {fname}")
        entries[(p.casefold(), a.casefold())] = CorrectionEntry(
            patches=patches,
            evidence=_clean(val.get("evidence"), f"{where} evidence"),
            doc_url=_clean(val.get("doc_url"), f"{where} doc_url"),
            verified=_clean(val.get("verified"), f"{where} verified"),
        )
    return MetadataCorrections(entries=entries)


def _patch_envelope_field(out: dict[str, Any], fname: str,
                          patch: FieldPatch) -> dict[str, Any] | None:
    """信封字段 patch。返回新 dict（copy-on-write）；无需变更返回 None。"""
    current = out.get(fname)
    if patch.replace is not None:
        if current == patch.replace:
            return None
        return {**out, fname: patch.replace}
    if not isinstance(current, str):
        return None
    patched = patch.apply(current)
    if patched == current:
        return None
    return {**out, fname: patched}


def correct_api_result(result: dict[str, Any],
                       corrections: MetadataCorrections | None) -> dict[str, Any]:
    """get_api 信封纠偏（copy-on-write）：未命中/无需变更返回原对象。

    drop 对缺失/非字符串字段 no-op；全行删光置 None（保键形，format_api_detail
    恒输出 x-constraint 键）；replace 无条件落位（含缺失新建）。
    evidence/doc_url/verified 仅台账，不进信封。
    """
    if not corrections:
        return result
    entry = corrections.for_api(str(result.get("product", "")),
                                str(result.get("api", "")))
    if entry is None:
        return result
    updated = result
    for fname, patch in entry.patches.items():
        patched = _patch_envelope_field(updated, fname, patch)
        if patched is not None:
            updated = patched
    return updated


def correct_doc(doc: dict[str, Any], product: str, api: str,
                corrections: MetadataCorrections | None) -> dict[str, Any]:
    """doc 级纠偏（离线管道组合根）：in-place 改写各 op 的字段，返回 doc。

    drop 全行删光从 op 移除键（format_api_detail 对缺失键输出 None，两形一致）；
    replace 无条件落位（含缺失新建）。未命中条目/字段原样；幂等。
    """
    if not corrections:
        return doc
    entry = corrections.for_api(product, api)
    if entry is None:
        return doc
    for path_item in (doc.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for fname, patch in entry.patches.items():
                current = op.get(fname)
                if patch.replace is not None:
                    if current != patch.replace:
                        op[fname] = patch.replace
                    continue
                if not isinstance(current, str):
                    continue
                patched = patch.apply(current)
                if patched is None:
                    op.pop(fname, None)
                elif patched != current:
                    op[fname] = patched
    return doc


def load_metadata_corrections(path: str | None) -> MetadataCorrections:
    """加载纠偏配置文件：CLI/env 原始值 → MetadataCorrections 的唯一语义入口。

    - None（--metadata-corrections 与 env 均未配置）→ 缺省档：裸名
      DEFAULT_CORRECTIONS_FILE 经 resolve_config_arg 解析，文件缺失静默
      MetadataCorrections.empty()（隐式缺省不 fail-fast）；
    - 空串 / "off"（strip + 大小写不敏感，对齐 spill idiom）→ 显式禁用；
    - 显式路径/裸名 → 解析加载，缺失 fail-fast（FileNotFoundError 列全候选）。
    JSON 非法恒 fail-fast。命中条目记 INFO 台账（启动一次），调用时静默。
    """
    corrections = (_load(DEFAULT_CORRECTIONS_FILE, missing_ok=True)
                   if path is None
                   else (MetadataCorrections.empty()
                         if not path.strip() or path.strip().lower() == "off"
                         else _load(path, missing_ok=False)))
    if corrections.entries:
        keys = ", ".join(f"{p.upper()}:{a}({','.join(sorted(e.patches))})"
                         for (p, a), e in corrections.entries.items())
        logger.info("metadata corrections loaded: %d entr%s: %s",
                    len(corrections.entries),
                    "y" if len(corrections.entries) == 1 else "ies", keys)
    return corrections


def _load(path: str, *, missing_ok: bool) -> MetadataCorrections:
    """内部接缝：resolve + open + parse；missing_ok 仅豁免文件不存在。"""
    try:
        resolved = resolve_config_arg(path)
    except FileNotFoundError:
        if missing_ok:
            return MetadataCorrections.empty()
        raise
    with open(resolved, encoding="utf-8") as f:
        data = json.load(f)
    return parse_metadata_corrections(data)
