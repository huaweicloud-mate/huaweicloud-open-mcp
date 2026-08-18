import json
import os
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

_ENV_FILE = os.path.join(ROOT, ".env")


def _load_dotenv(path):
    """最小 .env 加载器：已存在的环境变量优先（不覆盖）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv(_ENV_FILE)


def fixture_path(name):
    return os.path.join(FIXTURES, name)


def load_fixture(name):
    with open(fixture_path(name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def fixture_dir():
    return FIXTURES


@pytest.fixture
def mini_detail():
    return load_fixture("apis_detail.json")


@pytest.fixture
def mini_docs():
    return load_fixture("apis_docs.json")


@pytest.fixture
def mini_products():
    return load_fixture("huawei_products.json")


@pytest.fixture
def mini_count():
    return load_fixture("apis_count.json")


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """每个测试独立的临时工作区，含迷你 raw/ 数据。
    设置 HUAWEICLOUD_MCP_DATA_ROOT 环境变量让 catalog 使用此根。"""
    wd = tmp_path / "ws"
    raw = wd / "raw"
    raw.mkdir(parents=True)
    for name in ("apis_count.json", "apis_docs.json", "huawei_products.json", "apis_detail.json"):
        shutil.copy(fixture_path(name), raw / name)
    monkeypatch.setenv("HUAWEICLOUD_MCP_DATA_ROOT", str(wd))
    # 清空上次测试可能残留的 store registry
    from openmcp.apie import catalog
    catalog._stores.clear()
    return str(wd)


@pytest.fixture
def swagger_schema():
    schema_path = os.environ.get("SWAGGER2_SCHEMA", "/tmp/swagger2_schema.json")
    if not os.path.exists(schema_path):
        pytest.skip(f"Swagger 2.0 schema 缺失: {schema_path}")
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


def pytest_collection_modifyitems(config, items):
    """默认跳过 e2e 标记测试；用 `-m e2e` 显式启用。"""
    if config.getoption("-m") == "e2e":
        return
    skip_e2e = pytest.mark.skip(reason="真实数据 E2E，默认跳过（用 -m e2e 启用）")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
