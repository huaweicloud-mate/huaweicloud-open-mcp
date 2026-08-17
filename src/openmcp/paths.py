"""项目根路径解析。"""

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent


def project_root() -> Path:
    """仓库根目录（src/openmcp/ 的上一级 src 的上一级）。"""
    return _PACKAGE_DIR.parent.parent
