"""S0: common/paths 配置资源路径解析（config_path + resolve_config_arg）。

口径：仓库根 configs/ 优先（dev 真值源），缺失回退包内 configs 资源
（wheel 安装态由 hatch force-include 映射为 huaweicloud_open_mcp/configs）。
resolve_config_arg 面向启动参数：存在的显式路径原样使用，否则回退 configs/<name>。
"""

from pathlib import Path

import pytest

from common import paths


def test_dev_repo_configs_take_priority(tmp_path, monkeypatch):
    """仓库根 configs/ 存在时直接返回该文件。"""
    local = tmp_path / "configs" / "tag_translations.json"
    local.parent.mkdir(parents=True)
    local.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    assert paths.config_path("tag_translations.json") == local


def test_fallback_to_package_resource(tmp_path, monkeypatch):
    """仓库根缺失时回退包内 configs 资源路径。"""
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    p = paths.config_path("mcp-server-catalog.example.json")
    assert p.parts[-3:] == ("huaweicloud_open_mcp", "configs",
                            "mcp-server-catalog.example.json")


def test_real_repo_layout_cwd_independent(tmp_path, monkeypatch):
    """真实仓库布局解析到仓库 configs/，且与 cwd 无关。"""
    monkeypatch.chdir(tmp_path)
    expected = paths.project_root() / "configs" / "safety-policy.example.json"
    assert paths.config_path("safety-policy.example.json") == expected
    assert expected.exists()


# ---------- resolve_config_arg（启动参数：显式路径优先，裸名回退 configs） ----------


def test_resolve_arg_existing_cwd_relative_path_wins(tmp_path, monkeypatch):
    """存在的 cwd 相对路径原样返回，优先于仓库 configs/ 同名文件（现状零回归）。"""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "my.json").write_text("{}", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs" / "my.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(paths, "project_root", lambda: repo)
    resolved = paths.resolve_config_arg("my.json")
    assert resolved == Path("my.json")            # 原样返回（相对形态保留）
    assert resolved.resolve() == (cwd / "my.json").resolve()  # 且指向 cwd 文件


def test_resolve_arg_absolute_path_as_is(tmp_path, monkeypatch):
    """存在的绝对路径原样返回，不做 configs 拼接。"""
    abs_file = tmp_path / "abs" / "foo.json"
    abs_file.parent.mkdir()
    abs_file.write_text("{}", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    assert paths.resolve_config_arg(str(abs_file)) == abs_file


def test_resolve_arg_bare_name_repo_configs_priority(tmp_path, monkeypatch):
    """裸文件名（cwd 无此文件）→ 仓库根 configs/ 优先。"""
    repo = tmp_path / "repo"
    cfg = repo / "configs" / "deploy-hints.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(paths, "project_root", lambda: repo)
    assert paths.resolve_config_arg("deploy-hints.json") == cfg


def test_resolve_arg_package_fallback_exists(tmp_path, monkeypatch):
    """config_path 候选存在即返回（包内资源路径，无需仓库根配合）。"""
    pkg = tmp_path / "pkg" / "configs" / "x.json"
    pkg.parent.mkdir(parents=True)
    pkg.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "config_path", lambda name: pkg)
    assert paths.resolve_config_arg("x.json") == pkg


def test_resolve_arg_missing_raises_with_all_candidates(tmp_path, monkeypatch):
    """全缺失 fail-fast：FileNotFoundError 消息列出显式路径与两条 configs 候选。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path / "repo")
    with pytest.raises(FileNotFoundError) as excinfo:
        paths.resolve_config_arg("missing.json")
    msg = str(excinfo.value)
    assert "missing.json" in msg
    assert str(tmp_path / "repo" / "configs" / "missing.json") in msg
    assert "huaweicloud_open_mcp" in msg
