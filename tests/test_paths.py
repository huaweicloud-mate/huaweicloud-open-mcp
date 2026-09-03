"""S0: common/paths.config_path 配置资源路径解析。

口径：仓库根 configs/ 优先（dev 真值源），缺失回退包内 configs 资源
（wheel 安装态由 hatch force-include 映射为 huaweicloud_open_mcp/configs）。
"""

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
