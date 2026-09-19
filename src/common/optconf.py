"""可选文件配置加载 discipline（optconf）：None→缺省档 / off→禁用 / 路径→严格。

此前九处解析器共享同一形状却零共享代码，off 匹配大小写、缺省行为、错误策略
各自漂移（hints 宣称「对齐 spill idiom」而 spill 的 "off" 大小写敏感）。本模块
收拢其中的真共享族：

- ``load_opt_file``：文件资源加载器的唯一分支纪律——arg None → 缺省档
  （config_path 解析，缺失静默 off，隐式缺省不 fail-fast）；off/空串（大小写
  不敏感）→ off 值；显式路径/裸名 → resolve_config_arg（存在显式路径 > 仓库根
  configs/ > 包内 configs/）+ parse，缺失/JSON 非法 fail-fast。各 loader 的
  parse 函数与 off 值留在原地——共享的是分支纪律，不是 schema。
- ``is_off``：off 哨兵判定单点（空串或 "off"，strip + 大小写不敏感），供
  非文件形态的配置（如 spill 目录）复用，消灭大小写漂移。
- ``watch_opt_file``：``load_opt_file`` 的热刷新孪生（S23，装配侧专用）——
  分支纪律逐字一致，热分支返回 ``common.hotconf.HotFile``（运行期跟随文件，
  见 hotconf 契约），静态分支（off / 缺省档缺失）返回 off 值不建 holder。
  ``load_opt_file`` 保持原样：离线管道与既有测试依赖其纯加载语义。

枚举型解析器（elicit mode / parse_modes / deprecated_mode / auth-demote）
缺省与错误策略各不相同且为真差异，不入本模块（强行合一 = interface 与行为
差异一样宽，shallow）。
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from common.hotconf import HotFile
from common.paths import config_path, resolve_config_arg

T = TypeVar("T")


def is_off(value: str) -> bool:
    """off 哨兵判定（空串或 "off"，strip + 大小写不敏感）。"""
    text = value.strip().lower()
    return not text or text == "off"


def load_opt_file(arg: str | None, *, parse: Callable[[dict], T],
                  off: T, default_name: str | None = None) -> T:
    """加载可选 JSON 配置文件：CLI/env 原始值 → 值对象的唯一分支纪律。

    - None：default_name 给定时按 config_path 解析缺省档（仓库根 configs/ >
      包内 configs/），文件缺失静默返回 off（隐式缺省不 fail-fast）；
      未给定时直接返回 off（无缺省档语义的 loader）。
    - 空串 / "off"（strip + 大小写不敏感）→ 显式禁用，返回 off。
    - 其余视为显式路径/裸名 → resolve_config_arg 解析 + parse 加载，
      缺失 fail-fast（FileNotFoundError 列全候选）、JSON 非法恒 fail-fast。
    """
    if arg is None:
        if default_name is None:
            return off
        path = config_path(default_name)
        if not path.is_file():
            return off
        return parse(_read(path))
    if is_off(arg):
        return off
    return parse(_read(resolve_config_arg(arg)))


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data: dict = json.load(f)
        return data


def watch_opt_file(arg: str | None, *, parse: Callable[[dict], T],
                   off: T, default_name: str | None = None) -> HotFile[T] | T:
    """``load_opt_file`` 的热刷新孪生（装配侧专用）：分支纪律一致，热分支返回 HotFile。

    - None：default_name 给定且缺省档文件存在 → HotFile（急切加载 fail-fast）；
      缺失或未给 default_name → 静态 off（启动缺失不追踪后出现的文件，重启生效）；
    - 空串 / "off"（strip + 大小写不敏感）→ 静态 off；
    - 其余视为显式路径/裸名 → resolve_config_arg 解析 → HotFile（缺失 fail-fast）。

    静态分支与 load_opt_file 返回同一 off 对象（同一性可断言）；热分支的运行期
    刷新语义（stale-until-ready / 坏文件沿用旧版 / 单飞后台线程）见 common.hotconf。
    """
    if arg is None:
        if default_name is None:
            return off
        path = config_path(default_name)
        if not path.is_file():
            return off
        return HotFile(str(path), parse)
    if is_off(arg):
        return off
    return HotFile(str(resolve_config_arg(arg)), parse)
