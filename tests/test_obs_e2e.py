"""OBS 真实 E2E：真实 AK/SK + 真实 OBS 端点（只读）。

默认跳过（conftest 规则），用 `-m e2e` 显式启用。
凭证走 .env（conftest 已注入 os.environ），遵循华为云 SDK 环境变量惯例：
    HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK
"""

import pytest

from common.auth import load_from_env
from mcp_openapi.execute_obs import ObsHttpClient

pytestmark = pytest.mark.e2e

OBS_HOST = "obs.cn-north-4.myhuaweicloud.com"


def _require_credentials():
    cred = load_from_env()
    if cred is None:
        pytest.skip("缺少 HUAWEICLOUD_SDK_AK/HUAWEICLOUD_SDK_SK（.env 或环境变量）")
    return cred


@pytest.fixture(scope="module")
def e2e_credentials():
    return _require_credentials()


@pytest.fixture(scope="module")
def e2e_obs_client(e2e_credentials):
    return ObsHttpClient(credentials=e2e_credentials, max_retries=0)


def test_obs_list_buckets_real(e2e_obs_client):
    resp = e2e_obs_client.request("GET", OBS_HOST, bucket="")
    assert resp["status"] == 200
    body = resp["body"]
    assert isinstance(body, str)
    assert "ListAllMyBucketsResult" in body


def test_obs_wrong_sk_rejected(e2e_credentials):
    from common.auth import Credentials
    bad = Credentials(ak=e2e_credentials.ak, sk="WRONG" + e2e_credentials.sk)
    client = ObsHttpClient(credentials=bad, max_retries=0)
    resp = client.request("GET", OBS_HOST, bucket="")
    assert resp["status"] in (401, 403)
