"""日志配置：stderr 恒在，文件通道按需启用。

- **缺省不落盘**（MCP server）：只挂 stderr，级别跟随 ``--log-level``（缺省
  INFO）——server 由 MCP 客户端拉起，CWD 不可控，不往用户目录写 ``logs/``。
- 文件通道仅经 ``--log-file``/``HUAWEICLOUD_MCP_LOG_FILE`` 显式给定，或由
  构建期管道经 ``write_default_file=True`` 启用（``logs/{program}.log``，
  RotatingFileHandler 10MB×5）——管道命令是人站在仓库里跑的，留痕有价值。
- 文件在时 stderr 退为 WARNING+ 兜底（文件承载全量）；文件不在时 stderr 即
  唯一通道，承载全量。
- off 哨兵（空串 / "off"，大小写不敏感）显式禁用文件通道，判定委托
  ``common.optconf.is_off``（与 --spill-dir/--hints 同一惯例）。
- 级别：参数 > 环境变量（HUAWEICLOUD_MCP_LOG_LEVEL / HUAWEICLOUD_MCP_LOG_FILE）
- 接管点：root logger——全部模块 logger（main/common.*/safety.*/mcp_openapi.*、
  mcp_data.*/apie.* 等）经传播汇入；三方噪音库（httpx/httpcore）固定 WARNING
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from common.optconf import is_off

LOG_DIR = "logs"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

NOISY_LOGGERS = ("httpx", "httpcore")


def resolve_level(level: str | None) -> int:
    name = (level or os.environ.get("HUAWEICLOUD_MCP_LOG_LEVEL") or "INFO").upper()
    if name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        name = "INFO"
    return cast(int, getattr(logging, name))


def configure_logging(*, program: str, level: str | None = None,
                      log_file: str | None = None,
                      write_default_file: bool = False) -> str | None:
    """配置 root logger（全模块命名空间汇入），返回实际日志文件路径（不落盘时 None）。

    文件通道四分支（显式值优先于 write_default_file——显式 off 恒生效）：
    显式 off/空串 → 无文件；显式路径 → 该路径；未配置且 write_default_file →
    ``logs/{program}.log``；未配置且不写缺省档 → 无文件（MCP server 缺省）。
    重复调用会重置 handler。
    """
    logger = logging.getLogger()
    logger.setLevel(resolve_level(level))
    logger.handlers.clear()

    explicit = log_file if log_file is not None else os.environ.get("HUAWEICLOUD_MCP_LOG_FILE")
    path: str | None
    if explicit is not None and is_off(explicit):
        path = None                                  # 显式禁用（空串非「未配置」）
    elif explicit is not None:
        path = explicit
    elif write_default_file:
        path = str(Path(LOG_DIR) / f"{program}.log")
    else:
        path = None                                  # 未配置：不落盘

    formatter = logging.Formatter(_FORMAT)

    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=MAX_BYTES,
                                 backupCount=BACKUP_COUNT, encoding="utf-8")
        fh.setLevel(logger.level)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    sh = logging.StreamHandler()  # stderr
    # 文件承载全量 → stderr 兜底；无文件 → stderr 是唯一通道，跟随级别
    sh.setLevel(logging.WARNING if path is not None else logger.level)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    return path
