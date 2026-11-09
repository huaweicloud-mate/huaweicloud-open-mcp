"""凭证加载：AK/SK 环境变量 / JSON vault / profile。"""

import configparser
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Credentials:
    ak: str = ""
    sk: str = ""
    security_token: str | None = None
    project_id: str | None = None
    domain_id: str | None = None
    region: str | None = None
    configured_by_session: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.ak and self.sk)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _opt_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def load_json_profile(path: str | None = None) -> Credentials | None:
    """devkit S1 vault（~/.config/huaweicloud/credentials.json）。

    键为 camelCase（securityToken/configuredBySession）；ak/sk 缺失归 ""，
    securityToken/region 空串或缺失归 None。坏文件容错跳过（vault 可能被
    写入方非原子更新）；ak/sk 不齐 → None，链继续回落。
    """
    path = path or os.path.expanduser("~/.config/huaweicloud/credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.loads(f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    cred = Credentials(
        ak=_text(raw.get("ak")),
        sk=_text(raw.get("sk")),
        security_token=_opt_text(raw.get("securityToken")),
        region=_opt_text(raw.get("region")),
        configured_by_session=bool(raw.get("configuredBySession")),
    )
    if not cred.ready:
        return None
    return cred


def load_from_env(environ: dict[str, str] | None = None) -> Credentials | None:
    env = environ if environ is not None else os.environ
    ak = env.get("HUAWEICLOUD_SDK_AK")
    sk = env.get("HUAWEICLOUD_SDK_SK")
    if not ak or not sk:
        return None
    return Credentials(
        ak=ak,
        sk=sk,
        security_token=env.get("HUAWEICLOUD_SDK_SECURITY_TOKEN"),
        project_id=env.get("HUAWEICLOUD_SDK_PROJECT_ID"),
        domain_id=env.get("HUAWEICLOUD_SDK_DOMAIN_ID"),
    )


def load_profile(path: str | None = None) -> Credentials | None:
    path = path or os.path.expanduser("~/.huaweicloud/credentials")
    if not os.path.exists(path):
        return None
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if not parser.has_section("basic"):
        return None
    ak = parser.get("basic", "ak", fallback="")
    sk = parser.get("basic", "sk", fallback="")
    if not ak or not sk:
        return None
    return Credentials(
        ak=ak,
        sk=sk,
        security_token=parser.get("basic", "security_token", fallback=None),
        project_id=parser.get("basic", "project_id", fallback=None),
        domain_id=parser.get("basic", "domain_id", fallback=None),
    )


def get_credentials() -> Credentials | None:
    """provider chain：env → JSON vault → profile。"""
    return load_from_env() or load_json_profile() or load_profile()
