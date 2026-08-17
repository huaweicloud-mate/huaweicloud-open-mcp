"""日志配置：文件为主 + stderr 兜底。

- 默认日志文件 logs/{program}.log（RotatingFileHandler 10MB×5），目录自动创建
- stderr 同步 WARNING+（stdio 协议安全：stdout 永不被日志污染）
- 级别/文件：参数 > 环境变量（HUAWEICLOUD_MCP_LOG_LEVEL / HUAWEICLOUD_MCP_LOG_FILE）
- 命名空间：logging.getLogger("openmcp") 及其子级
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

LOG_DIR = "logs"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

ROOT_LOGGER = "openmcp"


def resolve_level(level: str | None) -> int:
    name = (level or os.environ.get("HUAWEICLOUD_MCP_LOG_LEVEL") or "INFO").upper()
    if name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        name = "INFO"
    return cast(int, getattr(logging, name))


def configure_logging(*, program: str, level: str | None = None,
                      log_file: str | None = None) -> str:
    """配置 openmcp 日志器，返回实际日志文件路径。重复调用会重置 handler。"""
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(resolve_level(level))
    logger.propagate = False
    logger.handlers.clear()

    log_file = log_file or os.environ.get("HUAWEICLOUD_MCP_LOG_FILE")
    if log_file is None:
        log_dir = Path(LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(log_dir / f"{program}.log")
    else:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_FORMAT)

    fh = RotatingFileHandler(log_file, maxBytes=MAX_BYTES,
                             backupCount=BACKUP_COUNT, encoding="utf-8")
    fh.setLevel(logger.level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler()  # stderr
    sh.setLevel(logging.WARNING)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    return log_file
