"""响应落盘（spill，S12）：execute 超限全保真落盘 + openapi 工具通用信封守卫。

外部接缝（公开 interface，包内仅 execute.py / service.py 两个消费者）：
- spill_body(raw, *, cfg, stem) -> SpillInfo | None   层级 1：截断前保真落盘
- guard_result(result, *, cfg, stem) -> result        层级 2：信封超限收缩
内部接缝（private）：_spill_payload（原子写/唯一命名/格式嗅探/note 模板——唯一落盘机制）。

口径：
- 预算单一真值 MAX_RESPONSE_CHARS（execute.py re-export 共享）
- 原子落盘：同级 .tmp-part + os.replace（与 data engine 写 lane 同一不变量）
- best-effort：落盘失败返回 None 并记 WARNING，调用方回落纯截断，响应永不因落盘失败而失败
- disclosure：note 为部署感知消费指引（data_enabled 决定是否指引 query_data）
- refusal（ok 非 True）与已带 spill 的信封（lane 已保真落盘）恒不落盘、不收缩
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.types import SpillInfo

logger = logging.getLogger("mcp_openapi.spill")

# 响应体积预算单一真值：body 截断（层级 1 触发）与信封守卫（层级 2 触发）共用
MAX_RESPONSE_CHARS = 200_000

_STEM_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")
_STEM_MAX = 80

_NOTE_WITH_DATA = ("完整数据已落盘；可用 query_data 读该文件分析"
                   "（json 数组可直接作表），或用文件读取工具查看")
_NOTE_WITHOUT_DATA = "完整数据已落盘；可用文件读取工具或 shell 查看"


@dataclass(frozen=True)
class SpillConfig:
    """落盘配置值对象：目录 + 部署感知披露 + 预算。

    dir 不保证存在（首次落盘时 mkdir）；ServiceConfig.spill 为 None 表示禁用
    （回落纯截断，guard/spill 全部 no-op）。
    """

    dir: Path
    data_enabled: bool = False
    budget: int = MAX_RESPONSE_CHARS

    @classmethod
    def default(cls) -> SpillConfig:
        """自动落盘默认目录：系统临时目录（不污染用户工作区）。"""
        return cls(dir=Path(tempfile.gettempdir()) / "hwc-mcp-spill")


def parse_spill_config(value: str | None, *,
                       data_enabled: bool = False) -> SpillConfig | None:
    """--spill-dir / HUAWEICLOUD_MCP_SPILL_DIR 解析（部署感知披露在此入参）。

    None/缺省 → 自动落盘默认目录；空串或 "off" → 禁用（回落纯截断）；
    其余视为目录路径。
    """
    if value is None:
        return SpillConfig.default()
    stripped = value.strip()
    if stripped in ("", "off"):
        return None
    return SpillConfig(dir=Path(stripped).expanduser(), data_enabled=data_enabled)


# ---------- 内部接缝：唯一落盘机制 ----------

def _size(raw: Any) -> int:
    """序列化体积口径（与 execute._render_body 一致：str 计原文长度）。"""
    if raw is None:
        return 0
    if isinstance(raw, str):
        return len(raw)
    return len(json.dumps(raw, ensure_ascii=False, default=str))


def _note(cfg: SpillConfig) -> str:
    return _NOTE_WITH_DATA if cfg.data_enabled else _NOTE_WITHOUT_DATA


def _sniff_format(raw: Any) -> tuple[str, str]:
    if isinstance(raw, bytes):
        return "bin", ".bin"
    if isinstance(raw, str):
        return "text", ".txt"
    return "json", ".json"


def _payload_bytes(raw: Any) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    return json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8")


def _unique_path(directory: Path, stem: str, ext: str) -> Path:
    clean = _STEM_SANITIZE.sub("_", stem)[:_STEM_MAX] or "spill"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for _ in range(5):
        candidate = directory / f"{clean}-{ts}-{secrets.token_hex(2)}{ext}"
        if not candidate.exists():
            return candidate
    return directory / f"{clean}-{ts}-{secrets.token_hex(8)}{ext}"


def _spill_payload(raw: Any, *, directory: Path, stem: str, note: str) -> SpillInfo | None:
    """唯一落盘机制：格式保真 + 唯一命名 + 原子替换；失败返回 None（best-effort）。"""
    fmt, ext = _sniff_format(raw)
    payload = _payload_bytes(raw)
    target: Path
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = _unique_path(directory, stem, ext)
        tmp = target.with_name(target.name + ".tmp-part")
        tmp.write_bytes(payload)
        os.replace(tmp, target)
    except OSError as e:
        logger.warning("spill failed dir=%s stem=%s: %s", directory, stem, e)
        return None
    logger.info("spill path=%s bytes=%d", target, len(payload))
    return {"path": str(target), "format": fmt, "bytes": len(payload), "note": note}


# ---------- 层级 1：截断前保真落盘 ----------

def spill_body(raw: Any, *, cfg: SpillConfig, stem: str) -> SpillInfo | None:
    """完整原始响应体保真落盘（调用方在截断之前调用）。失败返回 None。"""
    if raw is None:
        return None
    return _spill_payload(raw, directory=cfg.dir, stem=stem, note=_note(cfg))


# ---------- 层级 2：通用信封守卫 ----------

def _stub(value: Any) -> Any:
    """类型兼容占位（保持 TypedDict 输出契约合法）。"""
    if isinstance(value, dict):
        return {"_truncated": True, "_original_size_bytes": _size(value)}
    return []


def _largest_container(result: dict[str, Any]) -> str | None:
    """下一个收缩目标：序列化体积最大的容器字段（键名排序保证平局确定性）。"""
    best: str | None = None
    best_size = -1
    for key in sorted(result):
        if key == "spill":
            continue
        value = result[key]
        if isinstance(value, (dict, list)):
            size = _size(value)
            if size > best_size:
                best, best_size = key, size
    return best


def guard_result(result: Any, *, cfg: SpillConfig, stem: str) -> Any:
    """openapi 工具通用信封守卫。

    预算内原样返回（恒同一对象，零行为变化）；ok 非 True（refusal）、已带
    spill 的信封（lane 已保真落盘，body 预览为有意保留的截断形态）、以及
    truncated 无 spill 的信封（lane 已作出不落盘决策——`_spill=false` 或
    落盘失败，回落纯截断口径）恒不落盘、不收缩；
    超限时先把完整原始信封落盘，再按体积降序逐个把最大容器字段替换为占位，
    直至回到预算内或无容器可收缩（保形保留，真值在 spill 文件；最终信封含
    spill 字段自身体积，允许小幅超出预算）。
    落盘失败时保形回落（不做无据收缩）。
    """
    if (not isinstance(result, dict) or result.get("ok") is not True
            or "spill" in result or result.get("truncated") is True):
        return result
    if _size(result) <= cfg.budget:
        return result
    info = spill_body(result, cfg=cfg, stem=stem)
    if info is None:
        return result
    out = dict(result)
    while _size(out) > cfg.budget:
        victim = _largest_container(out)
        if victim is None:
            break
        out[victim] = _stub(out[victim])
    out["spill"] = info
    out["truncated"] = True
    return out
