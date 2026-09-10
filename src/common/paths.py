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


def resolve_config_arg(value: str) -> Path:
    """启动参数配置路径解析：存在的显式路径原样使用，否则回退 configs/<value>。

    优先级：显式存在的路径（绝对或 cwd 相对，现状零回归）> 仓库根 configs/<value>
    > 包内 configs/<value>（经 config_path，支持 uvx/pip 安装态裸文件名）。
    全部缺失抛 FileNotFoundError，消息列出全部尝试路径——调用方 fail-fast
    直接透传，无需 try/except。value 须非空（空值分支由调用方守卫）。
    """
    explicit = Path(value)
    if explicit.is_file():
        return explicit
    resolved = config_path(value)
    if resolved.is_file():
        return resolved
    local = project_root() / "configs" / value
    tried = [explicit, resolved] if local == resolved else [explicit, local, resolved]
    tried_text = "、".join(str(p) for p in tried)
    raise FileNotFoundError(f"配置文件不存在: {value}（已尝试: {tried_text}）")
