"""项目根路径解析。"""

from importlib.resources import files
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent

_CONFIGS_PACKAGE = "huaweicloud_open_mcp"


def project_root() -> Path:
    """仓库根目录（src/common/ 的上一级 src 的上一级）。"""
    return _PACKAGE_DIR.parent.parent


def config_path(name: str) -> Path:
    """configs/<name> 解析：仓库根 configs/ 优先（dev 真值源），缺失回退包内 configs 资源（安装态）。

    wheel 安装态由 hatch force-include 把仓库根 configs/ 映射为包数据
    huaweicloud_open_mcp/configs/，经 importlib.resources 定位。
    返回路径不保证存在（调用方自行处理缺失，如目录空列表、翻译空表）。
    """
    local = project_root() / "configs" / name
    if local.exists():
        return local
    resource = files(_CONFIGS_PACKAGE) / "configs" / name
    return Path(str(resource))
