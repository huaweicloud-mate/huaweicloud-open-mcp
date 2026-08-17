"""E2E 测试：真实 AK/SK + 真实华为云 API（只读）。

默认跳过（conftest 规则），用 `-m e2e` 显式启用：
    HUAWEICLOUD_SDK_AK=... HUAWEICLOUD_SDK_SK=... \
    HUAWEICLOUD_SDK_PROJECT_ID=... uv run pytest -m e2e

凭证环境变量遵循华为云 SDK 惯例。
"""

import os

import pytest

from huaweicloud_mcp.auth.credentials import Credentials, load_from_env
from huaweicloud_mcp.signer.client import HttpClient

pytestmark = pytest.mark.e2e


def _require_credentials():
    cred = load_from_env()
    if cred is None:
        pytest.skip("缺少 HUAWEICLOUD_SDK_AK/HUAWEICLOUD_SDK_SK 环境变量")
    return cred


@pytest.fixture(scope="module")
def e2e_credentials():
    return _require_credentials()


@pytest.fixture(scope="module")
def e2e_client(e2e_credentials):
    return HttpClient(credentials=e2e_credentials, max_retries=0)


@pytest.fixture(scope="module")
def e2e_project_id(e2e_credentials, e2e_client):
    if e2e_credentials.project_id:
        return e2e_credentials.project_id
    resp = e2e_client.request("GET", "iam.cn-north-4.myhuaweicloud.com",
                              "/v3/auth/projects", query={"name": "cn-north-4"})
    projects = (resp.get("body") or {}).get("projects") or []
    assert projects, f"IAM 项目查询失败: {resp}"
    return projects[0]["id"]


def test_real_iam_list_projects(e2e_client):
    resp = e2e_client.request("GET", "iam.cn-north-4.myhuaweicloud.com",
                              "/v3/auth/projects", query={"name": "cn-north-4"})
    assert resp["status"] == 200
    projects = resp["body"].get("projects")
    assert isinstance(projects, list) and projects
    assert any(p["name"] == "cn-north-4" for p in projects)


def test_real_ecs_list_servers_readonly(e2e_client, e2e_project_id):
    resp = e2e_client.request("GET", "ecs.cn-north-4.myhuaweicloud.com",
                              f"/v1/{e2e_project_id}/cloudservers/detail", query={"limit": 1})
    assert resp["status"] == 200
    body = resp["body"]
    assert "count" in body
    assert "servers" in body


def test_real_ecs_wrong_sk_rejected(e2e_credentials, e2e_project_id):
    bad = Credentials(ak=e2e_credentials.ak, sk="WRONG" + e2e_credentials.sk,
                      project_id=e2e_project_id)
    client = HttpClient(credentials=bad, max_retries=0)
    resp = client.request("GET", "ecs.cn-north-4.myhuaweicloud.com",
                          f"/v1/{e2e_project_id}/cloudservers/detail", query={"limit": 1})
    assert resp["status"] in (401, 403)
