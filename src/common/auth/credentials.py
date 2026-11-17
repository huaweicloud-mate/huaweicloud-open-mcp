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


def _credentials_from_mapping(raw: dict) -> Credentials:
    """camelCase 方言 → Credentials（JSON vault 与 --credentials payload 单源）。

    键集与 credentials.json（devkit S1 vault）完全对齐：ak/sk/securityToken/
    region/configuredBySession；未知键容忍（后续字段零破坏），snake_case
    键不翻译（方言钉死）。调用方各自决定缺 ak/sk 的处置——vault 容错跳过
    （None，链继续回落），显式 payload fail-fast（ValueError）。
    """
    return Credentials(
        ak=_text(raw.get("ak")),
        sk=_text(raw.get("sk")),
        security_token=_opt_text(raw.get("securityToken")),
        region=_opt_text(raw.get("region")),
        configured_by_session=bool(raw.get("configuredBySession")),
    )


def parse_credentials_payload(raw: str) -> Credentials:
    """--credentials 内联 JSON payload（如 '{"ak":..,"sk":..,"securityToken":..,
    "region":..}'）→ Credentials。

    显式配置 fail-fast：非法 JSON / 非 dict 根 / ak 或 sk 缺失即 ValueError
    启动失败，绝不静默回落到 env/vault/profile（打错字后落到另一个身份比
    报错危险）。错误消息不回显 payload 原文——SK 在里面；json.JSONDecodeError
    的消息只含位置信息不含原文，可透传。
    """
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            f"--credentials payload 非法 JSON（第 {getattr(exc, 'lineno', '?')} 行"
            f"第 {getattr(exc, 'colno', '?')} 列附近；原文不回显，含秘密）") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--credentials payload 须为 JSON 对象（如 "
                         '{"ak":..,"sk":..,"securityToken":..,"region":..}）')
    cred = _credentials_from_mapping(parsed)
    if not cred.ready:
        raise ValueError("--credentials payload 缺少有效的 ak/sk"
                         "（键集与 credentials.json 对齐：ak/sk/securityToken/"
                         "region/configuredBySession；空值视为缺失）")
    return cred


def load_json_profile(path: str | None = None) -> Credentials | None:
    """devkit S1 vault（~/.config/huaweicloud/credentials.json）。

    键为 camelCase（securityToken/configuredBySession），方言解析与
    --credentials 内联 payload 单源共享（`_credentials_from_mapping`）；
    ak/sk 缺失归 ""，securityToken/region 空串或缺失归 None。坏文件容错
    跳过（vault 可能被写入方非原子更新）；ak/sk 不齐 → None，链继续回落。
    """
    path = path or os.path.expanduser("~/.config/huaweicloud/credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.loads(f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    cred = _credentials_from_mapping(raw)
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
