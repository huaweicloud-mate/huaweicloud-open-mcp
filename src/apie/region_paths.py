import os

DEFAULT_REGION = "cn-north-4"


def current_region() -> str:
    return os.environ.get("API_EXPLORER_REGION", DEFAULT_REGION)


def is_default_region(region: str | None = None) -> bool:
    return (region or current_region()) == DEFAULT_REGION


def raw_detail_path(region: str | None = None) -> str:
    r = region or current_region()
    if r == DEFAULT_REGION:
        return "raw/apis_detail.json"
    return f"raw/{r}/apis_detail.json"


def raw_detail_partial_path(region: str | None = None) -> str:
    r = region or current_region()
    if r == DEFAULT_REGION:
        return "raw/apis_detail_partial.json"
    return f"raw/{r}/apis_detail_partial.json"


def by_tag_dir(region: str | None = None) -> str:
    r = region or current_region()
    if r == DEFAULT_REGION:
        return "apis_detail_by_tag"
    return f"apis_detail_by_tag_{r}"


def openapi2_dir(region: str | None = None) -> str:
    r = region or current_region()
    if r == DEFAULT_REGION:
        return "apis_detail_by_tag_openapi2"
    return f"apis_detail_by_tag_openapi2_{r}"


def merged_dir(region: str | None = None) -> str:
    r = region or current_region()
    if r == DEFAULT_REGION:
        return "apis_detail_by_tag_merged"
    return f"apis_detail_by_tag_merged_{r}"


def openapi_out_dir(region: str | None = None) -> str:
    r = region or current_region()
    if r == DEFAULT_REGION:
        return "data/openapi"
    return f"data/openapi/{r}"
