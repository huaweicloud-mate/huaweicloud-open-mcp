"""凭证加载单元测试。"""

from common.auth import credentials


def test_load_from_env_full(monkeypatch):
    monkeypatch.setenv("HUAWEICLOUD_SDK_AK", "AK1")
    monkeypatch.setenv("HUAWEICLOUD_SDK_SK", "SK1")
    monkeypatch.setenv("HUAWEICLOUD_SDK_SECURITY_TOKEN", "TOKEN")
    monkeypatch.setenv("HUAWEICLOUD_SDK_PROJECT_ID", "PID")
    cred = credentials.load_from_env()
    assert cred.ak == "AK1"
    assert cred.sk == "SK1"
    assert cred.security_token == "TOKEN"
    assert cred.project_id == "PID"


def test_load_from_env_minimal(monkeypatch):
    for var in ("HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK", "HUAWEICLOUD_SDK_SECURITY_TOKEN",
                "HUAWEICLOUD_SDK_PROJECT_ID", "HUAWEICLOUD_SDK_DOMAIN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HUAWEICLOUD_SDK_AK", "AK2")
    monkeypatch.setenv("HUAWEICLOUD_SDK_SK", "SK2")
    cred = credentials.load_from_env()
    assert cred.ak == "AK2"
    assert cred.security_token is None
    assert cred.project_id is None


def test_load_from_env_missing_ak(monkeypatch):
    monkeypatch.delenv("HUAWEICLOUD_SDK_AK", raising=False)
    monkeypatch.setenv("HUAWEICLOUD_SDK_SK", "SK")
    assert credentials.load_from_env() is None


def test_load_from_env_missing_sk(monkeypatch):
    monkeypatch.setenv("HUAWEICLOUD_SDK_AK", "AK")
    monkeypatch.delenv("HUAWEICLOUD_SDK_SK", raising=False)
    assert credentials.load_from_env() is None


def test_load_profile_basic(tmp_path):
    p = tmp_path / "credentials"
    p.write_text("[basic]\nak = PAK\nsk = PSK\nproject_id = PPID\n", encoding="utf-8")
    cred = credentials.load_profile(path=str(p))
    assert cred.ak == "PAK"
    assert cred.sk == "PSK"
    assert cred.project_id == "PPID"


def test_load_profile_security_token(tmp_path):
    p = tmp_path / "credentials"
    p.write_text("[basic]\nak = PAK\nsk = PSK\nsecurity_token = PTOK\n", encoding="utf-8")
    cred = credentials.load_profile(path=str(p))
    assert cred.security_token == "PTOK"


def test_load_profile_no_basic(tmp_path):
    p = tmp_path / "credentials"
    p.write_text("[global]\nak = GAK\nsk = GSK\n", encoding="utf-8")
    cred = credentials.load_profile(path=str(p))
    assert cred is None


def _write_profile(monkeypatch, tmp_path, ak: str) -> None:
    home = tmp_path / "home"
    (home / ".huaweicloud").mkdir(parents=True)
    (home / ".huaweicloud" / "credentials").write_text(
        f"[basic]\nak = {ak}\nsk = FSK\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    # Windows 上 expanduser 优先读 USERPROFILE（HOME 仅 POSIX 语义）
    monkeypatch.setenv("USERPROFILE", str(home))


def test_get_credentials_env_first(monkeypatch, tmp_path):
    monkeypatch.setenv("HUAWEICLOUD_SDK_AK", "EAK")
    monkeypatch.setenv("HUAWEICLOUD_SDK_SK", "ESK")
    _write_profile(monkeypatch, tmp_path, "PAK")
    cred = credentials.get_credentials()
    assert cred is not None
    assert cred.ak == "EAK"


def test_get_credentials_profile_fallback(monkeypatch, tmp_path):
    # env 缺失时回落 profile（HOME 指向 tmp 隔离开发机真实凭证）
    monkeypatch.delenv("HUAWEICLOUD_SDK_AK", raising=False)
    monkeypatch.delenv("HUAWEICLOUD_SDK_SK", raising=False)
    _write_profile(monkeypatch, tmp_path, "FAK")
    cred = credentials.get_credentials()
    assert cred is not None
    assert cred.ak == "FAK"
    assert cred.sk == "FSK"
