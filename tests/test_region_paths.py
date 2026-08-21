"""region_paths 路径计算单元测试。"""

from apie import region_paths


def test_default_region_paths(monkeypatch):
    monkeypatch.delenv("API_EXPLORER_REGION", raising=False)
    assert region_paths.raw_detail_path() == "raw/apis_detail.json"
    assert region_paths.raw_detail_partial_path() == "raw/apis_detail_partial.json"
    assert region_paths.by_tag_dir() == "apis_detail_by_tag"
    assert region_paths.openapi2_dir() == "apis_detail_by_tag_openapi2"
    assert region_paths.merged_dir() == "apis_detail_by_tag_merged"
    assert region_paths.openapi_out_dir() == "data/openapi"


def test_nondefault_region_paths():
    assert region_paths.raw_detail_path("cn-south-1") == "raw/cn-south-1/apis_detail.json"
    assert region_paths.raw_detail_partial_path("cn-south-1") == "raw/cn-south-1/apis_detail_partial.json"
    assert region_paths.by_tag_dir("cn-south-1") == "apis_detail_by_tag_cn-south-1"
    assert region_paths.openapi2_dir("cn-south-1") == "apis_detail_by_tag_openapi2_cn-south-1"
    assert region_paths.merged_dir("cn-south-1") == "apis_detail_by_tag_merged_cn-south-1"
    assert region_paths.openapi_out_dir("cn-south-1") == "data/openapi/cn-south-1"


def test_env_region_override(monkeypatch):
    monkeypatch.setenv("API_EXPLORER_REGION", "cn-east-3")
    assert region_paths.current_region() == "cn-east-3"
    assert region_paths.raw_detail_path() == "raw/cn-east-3/apis_detail.json"


def test_is_default_region():
    assert region_paths.is_default_region("cn-north-4")
    assert not region_paths.is_default_region("cn-south-1")
