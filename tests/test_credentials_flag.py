"""--credentials 内联凭证旗标装配测试。

显式 payload 置顶、region 回落、malformed fail-fast、mock 短路、
未传时链条逐字节回归。
"""

import argparse
import json

import pytest

from mcp_openapi.server import build_openapi_config


def _args(**kw):
    base = dict(mock=False, policy=None, region=None, mock_base=None,
                mock_passthrough=None, hints=None, audit_file=None,
                spill_dir=None, deprecated_index=None, deprecated_mode=None,
                auth_demote=None, auth_demote_pass=None, credentials=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _isolate(monkeypatch, tmp_path):
    """隔离开发机真实凭证：SDK env 清空 + HOME 指向 tmp
    （vault 与 profile 均不可达），region env 一并清空保确定性。"""
    for var in ("HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK",
                "HUAWEICLOUD_SDK_SECURITY_TOKEN", "HUAWEICLOUD_MCP_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows 上 expanduser 优先读 USERPROFILE（HOME 仅 POSIX 语义）
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_flag_payload_wins_over_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("HUAWEICLOUD_SDK_AK", "ENV-AK")
    monkeypatch.setenv("HUAWEICLOUD_SDK_SK", "ENV-SK")
    cfg = build_openapi_config(_args(
        credentials=json.dumps({"ak": "FLAG-AK", "sk": "FLAG-SK"})))
    assert cfg.credentials.ak == "FLAG-AK"
    assert cfg.credentials.sk == "FLAG-SK"
    assert cfg.credentials.security_token is None


def test_flag_payload_region_fallback(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = build_openapi_config(_args(
        credentials=json.dumps({"ak": "A", "sk": "S", "region": "cn-south-1"})))
    assert cfg.region == "cn-south-1"


def test_flag_payload_region_loses_to_explicit_flag(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = build_openapi_config(_args(
        region="cn-east-3",
        credentials=json.dumps({"ak": "A", "sk": "S", "region": "cn-south-1"})))
    assert cfg.region == "cn-east-3"


def test_flag_payload_region_loses_to_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("HUAWEICLOUD_MCP_REGION", "cn-north-9")
    cfg = build_openapi_config(_args(
        credentials=json.dumps({"ak": "A", "sk": "S", "region": "cn-south-1"})))
    assert cfg.region == "cn-north-9"


def test_flag_payload_security_token_flows(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = build_openapi_config(_args(
        credentials=json.dumps({"ak": "A", "sk": "S", "securityToken": "T1"})))
    assert cfg.credentials.security_token == "T1"


def test_flag_payload_malformed_fails_fast(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        build_openapi_config(_args(credentials="{not json"))


def test_flag_payload_empty_string_fails_fast(monkeypatch, tmp_path):
    # 空串是显式输入：解析失败 fail-fast，不静默回落链条
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        build_openapi_config(_args(credentials=""))


def test_flag_payload_missing_ak_fails_fast(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        build_openapi_config(_args(credentials=json.dumps({"sk": "S"})))


def test_mock_short_circuits_payload(monkeypatch, tmp_path):
    # mock 恒无凭证，payload 不解析
    _isolate(monkeypatch, tmp_path)
    cfg = build_openapi_config(_args(
        mock=True, credentials=json.dumps({"ak": "A", "sk": "S"})))
    assert cfg.credentials is None


def test_mock_ignores_malformed_payload(monkeypatch, tmp_path):
    # mock 短路先于解析：非法 payload 同样被忽略
    _isolate(monkeypatch, tmp_path)
    cfg = build_openapi_config(_args(mock=True, credentials="{bad"))
    assert cfg.credentials is None


def test_no_flag_env_chain_unchanged(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("HUAWEICLOUD_SDK_AK", "ENV-AK")
    monkeypatch.setenv("HUAWEICLOUD_SDK_SK", "ENV-SK")
    cfg = build_openapi_config(_args())
    assert cfg.credentials is not None
    assert cfg.credentials.ak == "ENV-AK"


def test_no_flag_no_source_none(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = build_openapi_config(_args())
    assert cfg.credentials is None
