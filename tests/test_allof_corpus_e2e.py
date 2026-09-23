"""真实语料 schema 归一 e2e 红线段（默认跳过；`-m e2e` 启用）。

固化 ADR-0005 的语料级不变量：转换后**无定义塌缩为 `{}`**（修复前 ~900）
且全量文档过 Swagger 2.0 gate（0 invalid）。需要 `raw/apis_detail.json` 与
`SWAGGER2_SCHEMA`（缺省 /tmp/swagger2_schema.json）。
"""

import json
import os

import pytest

pytestmark = pytest.mark.e2e

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RAW = os.path.join(_ROOT, "raw", "apis_detail.json")
_SCHEMA = os.environ.get("SWAGGER2_SCHEMA", "/tmp/swagger2_schema.json")


@pytest.mark.skipif(not os.path.exists(_RAW), reason="raw/apis_detail.json 缺失")
def test_corpus_no_definition_collapse():
    from apie.convert_openapi2 import convert_api

    with open(_RAW, encoding="utf-8") as f:
        data = json.load(f)
    collapse: list[str] = []
    allof = 0
    for key, api in data["apis"].items():
        doc = convert_api(api)
        for name, definition in (doc.get("definitions") or {}).items():
            if not isinstance(definition, dict):
                continue
            if not definition:
                collapse.append(f"{key}:{name}")
            if "allOf" in definition:
                allof += 1
    assert collapse == [], f"定义塌缩为 {{}}: {collapse[:8]}"
    assert allof > 0, "应保留 allOf 定义"


@pytest.mark.skipif(not os.path.exists(_RAW), reason="raw/apis_detail.json 缺失")
@pytest.mark.skipif(not os.path.exists(_SCHEMA), reason="Swagger 2.0 schema 缺失")
def test_corpus_swagger2_gate():
    import jsonschema

    from apie.convert_openapi2 import convert_api

    with open(_SCHEMA, encoding="utf-8") as f:
        validator = jsonschema.Draft4Validator(json.load(f))
    with open(_RAW, encoding="utf-8") as f:
        data = json.load(f)
    invalid: list[str] = []
    for key, api in data["apis"].items():
        errs = list(validator.iter_errors(convert_api(api)))
        if errs:
            invalid.append(f"{key}: {errs[0].message[:100]}")
    assert invalid == [], f"gate 失败 {len(invalid)} 个: {invalid[:5]}"
