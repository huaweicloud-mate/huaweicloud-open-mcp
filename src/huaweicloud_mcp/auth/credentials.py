"""凭证加载：AK/SK 环境变量 / profile。"""

import configparser
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Credentials:
    ak: str = ""
    sk: str = ""
    security_token: str = field(default=None)
    project_id: str = field(default=None)
    domain_id: str = field(default=None)

    @property
    def ready(self):
        return bool(self.ak and self.sk)


def load_from_env(environ=None):
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


def load_profile(path=None):
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


def get_credentials():
    """provider chain：env → profile。"""
    return load_from_env() or load_profile()
