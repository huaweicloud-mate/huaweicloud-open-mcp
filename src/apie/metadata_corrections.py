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

doc 级指针 patch（2026-09 起，首例 FunctionGraph:CreateEvent 的畸形 pattern）：
patches 键以 `/` 开头视为对 doc 根的 JSON Pointer（RFC 6901，~0/~1 归一），
v1 前缀白名单 /definitions/（schema 事实修正类），其余前缀/空段快速失败。
指针 patch 二选一：replace（非空字符串，叶值落位，父链存在时含缺失新建）/
drop（布尔 true，删叶键——与 op 级行级子串列表形区分，语义随目标类而变）。
应用面三个（同一 patch 核心）：correct_doc 离线 in-place（op 级 + 指针级一次
落位）；correct_doc_cow 运行时 copy-on-write（get_api format 前 / execute_api
校验前，沿指针路径浅拷贝容器、未触及子树共享、原 doc 恒不改写——「缓存 doc
恒不改写」不变量；未命中/无 doc_patches/无需变更恒返回原对象零开销）；
correct_api_result 信封级（既有 op 级呈现面，机制不变）。v1 指针域不含 op 级
数据，调用方持有的 op 引用在 COW 后保持有效。
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from common.optconf import load_opt_file

logger = logging.getLogger("apie.metadata_corrections")

DEFAULT_CORRECTIONS_FILE = "metadata-corrections.json"

# 字段白名单 v1：纠偏可触及的 op/信封级元数据字段（扩展即加白名单）
ALLOWED_FIELDS = frozenset({"x-constraint"})

# doc 级指针 patch 的 v1 前缀白名单：仅 schema 事实修正类（definitions 内）
DOC_POINTER_PREFIX = "/definitions/"

_ENTRY_KEYS = frozenset({"evidence", "doc_url", "verified", "patches"})


@dataclass(frozen=True)
class FieldPatch:
    """单字段 patch（drop/replace 二选一）。apply 为字符串级核心，两个应用面共享。

    drop=() 仅用于 doc 指针 patch（布尔 drop 删叶键），不经 apply()——
    指针叶走 _apply_pointer_* 的键级语义。
    """

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
    """单 API 纠偏条目：patches 作用于 op 级字段、doc_patches 作用于文档根
    （JSON Pointer，load 时解析为段元组），evidence/doc_url/verified 仅台账。"""

    patches: dict[str, FieldPatch]
    doc_patches: dict[tuple[str, ...], FieldPatch] = field(default_factory=dict)
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


def _parse_doc_pointer(key: str, where: str) -> tuple[str, ...]:
    """JSON Pointer → 段元组：v1 前缀白名单 /definitions/；RFC 6901 ~0/~1 归一；空段拒绝。"""
    if not key.startswith(DOC_POINTER_PREFIX):
        raise ValueError(f"{where} 指针前缀不在白名单内（v1 允许 {DOC_POINTER_PREFIX}*）")
    segments = key.split("/")[1:]
    if any(seg == "" for seg in segments):
        raise ValueError(f"{where} 指针含空段: {key!r}")
    return tuple(seg.replace("~1", "/").replace("~0", "~") for seg in segments)


def _parse_pointer_patch(raw: Any, where: str) -> FieldPatch:
    """指针 patch 解析：replace（非空字符串）/ drop（布尔 true，删叶键）恰居其一。

    指针 drop 为布尔形（键级删除），与 op 级行级子串列表形（FieldPatch.apply）
    区分——语义随目标类而变，二选一纪律不变。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是 mapping")
    extra = set(raw) - {"drop", "replace"}
    if extra:
        raise ValueError(f"{where} 含未知键: {sorted(extra)}")
    drop = raw.get("drop")
    replace = raw.get("replace")
    if (drop is None) == (replace is None):
        raise ValueError(f"{where} 必须 drop/replace 二选一")
    if replace is not None:
        if not isinstance(replace, str) or not replace:
            raise ValueError(f"{where} 的 replace 必须是非空字符串")
        return FieldPatch(replace=replace)
    if drop is not True:
        raise ValueError(f"{where} 的 drop 必须为 true（指针 patch 删叶键，非行级子串）")
    return FieldPatch(drop=())


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
        doc_patches: dict[tuple[str, ...], FieldPatch] = {}
        for fname, fpatch in raw_patches.items():
            if fname.startswith("/"):
                ptr = _parse_doc_pointer(fname, f"{where} 的 {fname}")
                doc_patches[ptr] = _parse_pointer_patch(fpatch, f"{where} 的 {fname}")
                continue
            if fname not in ALLOWED_FIELDS:
                raise ValueError(f"{where} 的字段 {fname!r} 不在白名单内"
                                 f"（v1 允许: {sorted(ALLOWED_FIELDS)}）")
            patches[fname] = _parse_patch(fpatch, f"{where} 的 {fname}")
        entries[(p.casefold(), a.casefold())] = CorrectionEntry(
            patches=patches,
            doc_patches=doc_patches,
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


def _pointer_leaf(doc: dict[str, Any], pointer: tuple[str, ...]
                  ) -> tuple[dict[str, Any], str] | None:
    """沿指针只读遍历到叶父容器；父链缺失/中途非 dict 返回 None。"""
    node: Any = doc
    for seg in pointer[:-1]:
        if not isinstance(node, dict):
            return None
        node = node.get(seg)
    if not isinstance(node, dict):
        return None
    return node, pointer[-1]


def _pointer_would_change(doc: dict[str, Any], pointer: tuple[str, ...],
                          patch: FieldPatch) -> bool:
    """指针 patch 对原结构是否需变更（父链缺失/叶已为目标态 → False）。"""
    hit = _pointer_leaf(doc, pointer)
    if hit is None:
        return False
    parent, leaf = hit
    if patch.replace is not None:
        return parent.get(leaf) != patch.replace
    return leaf in parent  # drop


def _apply_pointer_inplace(doc: dict[str, Any], pointer: tuple[str, ...],
                           patch: FieldPatch) -> None:
    """指针 patch 的 in-place 应用（离线面）：叶值落位 / 删叶键；父链缺失 no-op。"""
    hit = _pointer_leaf(doc, pointer)
    if hit is None:
        return
    parent, leaf = hit
    if patch.replace is not None:
        if parent.get(leaf) != patch.replace:
            parent[leaf] = patch.replace
        return
    parent.pop(leaf, None)


def _apply_pointer_cow(original: dict[str, Any], pointer: tuple[str, ...],
                       patch: FieldPatch,
                       copied: dict[int, dict[str, Any]]) -> None:
    """指针 patch 的 copy-on-write 应用：沿路径浅拷贝容器（copied 按 id 去重，
    共享前缀只拷一次），叶值落位 / 删叶键；原结构恒只读。"""
    node = original
    for seg in pointer[:-1]:
        nxt = node.get(seg) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            return
        fresh = copied.get(id(nxt))
        if fresh is None:
            fresh = dict(nxt)
            copied[id(nxt)] = fresh
            copied[id(node)][seg] = fresh
        node = nxt
    parent = copied[id(node)]
    leaf = pointer[-1]
    if patch.replace is not None:
        if parent.get(leaf) != patch.replace:
            parent[leaf] = patch.replace
        return
    parent.pop(leaf, None)


def correct_doc_cow(doc: dict[str, Any], product: str, api: str,
                    corrections: MetadataCorrections | None) -> dict[str, Any]:
    """运行时 doc 级纠偏（copy-on-write）：未命中/无 doc_patches/无需变更恒返回原对象。

    命中且需变更时沿指针路径浅拷贝容器（未触及子树共享原对象），原 doc 恒不改写
    ——「缓存 doc 恒不改写」不变量。v1 指针白名单 /definitions/（schema 事实修正），
    op 级数据不在指针域内，调用方持有的 op 引用在 COW 后保持有效。既有 op 级
    条目（如 RDS x-constraint）不经本接口——呈现面走 correct_api_result，本接口
    对其零开销直通。
    """
    entry = corrections.for_api(product, api) if corrections else None
    if entry is None or not entry.doc_patches:
        return doc
    if not any(_pointer_would_change(doc, ptr, patch)
               for ptr, patch in entry.doc_patches.items()):
        return doc
    root: dict[str, Any] = dict(doc)
    copied: dict[int, dict[str, Any]] = {id(doc): root}
    for pointer, patch in entry.doc_patches.items():
        _apply_pointer_cow(doc, pointer, patch, copied)
    return root


def correct_doc(doc: dict[str, Any], product: str, api: str,
                corrections: MetadataCorrections | None) -> dict[str, Any]:
    """doc 级纠偏（离线管道组合根）：in-place 改写各 op 的字段与 doc 级指针
    目标，返回 doc。

    drop 全行删光从 op 移除键（format_api_detail 对缺失键输出 None，两形一致）；
    replace 无条件落位（含缺失新建）；指针 patch 每 doc 应用一次（叶值落位 /
    删叶键，父链缺失 no-op）。未命中条目/字段原样；幂等。
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
    for pointer, patch in entry.doc_patches.items():
        _apply_pointer_inplace(doc, pointer, patch)
    return doc


def load_metadata_corrections(path: str | None) -> MetadataCorrections:
    """加载纠偏配置文件：CLI/env 原始值 → MetadataCorrections 的唯一语义入口。

    分支纪律委托 common.optconf.load_opt_file（单一实现）：
    - None → 缺省档 DEFAULT_CORRECTIONS_FILE（config_path 解析，缺失静默
      MetadataCorrections.empty()，隐式缺省不 fail-fast）；
    - 空串 / "off"（大小写不敏感）→ 显式禁用；
    - 显式路径/裸名 → resolve_config_arg 解析加载，缺失 fail-fast。
    JSON 非法恒 fail-fast。命中条目记 INFO 台账（启动一次），调用时静默。
    """
    corrections = load_opt_file(path, parse=parse_metadata_corrections,
                                off=MetadataCorrections.empty(),
                                default_name=DEFAULT_CORRECTIONS_FILE)
    if corrections.entries:
        def _fmt(e: CorrectionEntry) -> str:
            return ",".join(sorted(e.patches)
                            + ["/" + "/".join(p) for p in sorted(e.doc_patches)])
        keys = ", ".join(f"{p.upper()}:{a}({_fmt(e)})"
                         for (p, a), e in corrections.entries.items())
        logger.info("metadata corrections loaded: %d entr%s: %s",
                    len(corrections.entries),
                    "y" if len(corrections.entries) == 1 else "ies", keys)
    return corrections
