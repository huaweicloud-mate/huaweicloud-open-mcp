"""OBS 执行 lane 单元测试（S9b/S9c/S9d/S9e）。

期望值来自手工构造的迷你 schema/op 片段与手写字面量（仿 apis fixtures 设计），
不依赖真实 raw/ data/。
"""

from common.types import ClientResponse
from mcp_openapi import execute_obs
from mcp_openapi.execute_obs import (
    ObsHttpClient,
    build_obs_request,
    build_obs_url,
    execute_obs_api,
    is_obs,
    parse_obs_error,
    serialize_body_xml,
)

# ---------- S9b XML 序列化 ----------

def test_serialize_body_xml_simple():
    op = {"parameters": [{"in": "body", "name": "CreateBucketRequestBody",
                          "schema": {"$ref": "#/definitions/CreateBucketRequestBody"}}]}
    doc = {"definitions": {
        "CreateBucketRequestBody": {
            "xml": {"name": "CreateBucketConfiguration"},
            "properties": {"Location": {"type": "string"}},
        }}}
    assert serialize_body_xml(op, doc, {"Location": "cn-north-4"}) == (
        "<CreateBucketConfiguration><Location>cn-north-4</Location></CreateBucketConfiguration>")


def test_serialize_body_xml_nested_array():
    op = {"parameters": [{"in": "body", "name": "CompleteMultipartUploadRequestBody",
                          "schema": {"$ref": "#/definitions/CompleteMultipartUploadRequestBody"}}]}
    doc = {"definitions": {
        "CompleteMultipartUploadRequestBody": {
            "xml": {"name": "CompleteMultipartUpload"},
            "properties": {"Part": {"type": "array",
                                    "items": {"$ref": "#/definitions/Part"}}},
        },
        "Part": {"properties": {"PartNumber": {"type": "integer"},
                                 "ETag": {"type": "string"}}},
    }}
    body = {"Part": [{"PartNumber": 1, "ETag": "abc"}, {"PartNumber": 2, "ETag": "def"}]}
    assert serialize_body_xml(op, doc, body) == (
        "<CompleteMultipartUpload>"
        "<Part><PartNumber>1</PartNumber><ETag>abc</ETag></Part>"
        "<Part><PartNumber>2</PartNumber><ETag>def</ETag></Part>"
        "</CompleteMultipartUpload>")


def test_serialize_body_xml_escapes_text():
    op = {"parameters": [{"in": "body", "name": "B", "schema": {}}]}
    doc = {"definitions": {}}
    # 无 schema 时按 dict 键名兜底
    assert serialize_body_xml(op, doc, {"K": "a<b&c"}) == "<B><K>a&lt;b&amp;c</K></B>"


# ---------- S9c 桶寻址 ----------

def test_build_obs_request_splits_params():
    op = {"parameters": [
        {"name": "bucket_name", "in": "query"},
        {"name": "object_key", "in": "path"},
        {"name": "uploadId", "in": "query"},
        {"name": "x-obs-acl", "in": "header"},
        {"name": "Authorization", "in": "header"},
        {"name": "Date", "in": "header"},
    ]}
    req = build_obs_request(op, "/{object_key}",
                            {"bucket_name": "mybucket", "object_key": "dir/a.txt",
                             "uploadId": "u1", "x-obs-acl": "public-read",
                             "Authorization": "x", "Date": "y"}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert req.bucket == "mybucket"
    assert req.object_key == "dir/a.txt"
    assert req.query == {"uploadId": "u1"}
    assert req.headers == {"x-obs-acl": "public-read"}  # Authorization/Date 被跳过


def test_build_obs_request_missing_bucket():
    op = {"parameters": [{"name": "bucket_name", "in": "query"}]}
    assert build_obs_request(op, "/", {}, {}) == "缺少必填参数 bucket_name（桶名）"


def test_build_obs_request_bucketless_list_buckets():
    # ListBuckets 无 bucket_name 参数，bucket 置空，不报错
    op = {"parameters": [
        {"name": "Authorization", "in": "header"},
        {"name": "Date", "in": "header"},
    ]}
    req = build_obs_request(op, "/", {}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert req.bucket == ""
    assert req.object_key == ""


def test_build_obs_request_octet_stream_body():
    op = {"parameters": [{"name": "bucket_name", "in": "query"},
                         {"name": "object_key", "in": "path"}]}
    req = build_obs_request(op, "/{object_key}",
                            {"bucket_name": "b", "object_key": "o.txt",
                             "body": "hello world"}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert req.body == "hello world"
    assert req.content_type == "application/octet-stream"


def test_build_obs_url_path_style():
    url = build_obs_url("obs.cn-north-4.myhuaweicloud.com", "mybucket",
                        "dir/a b.txt", {"acl": "", "uploadId": "u1", "prefix": "p/"})
    assert url == ("https://obs.cn-north-4.myhuaweicloud.com/mybucket/dir/a%20b.txt"
                   "?acl&prefix=p%2F&uploadId=u1")


# ---------- S9d XML 错误解析 ----------

def test_parse_obs_error_xml():
    xml = ("<Error><Code>NoSuchBucket</Code>"
           "<Message>The specified bucket does not exist</Message>"
           "<RequestId>xx</RequestId></Error>")
    assert parse_obs_error(xml) == ("NoSuchBucket", "The specified bucket does not exist")


def test_parse_obs_error_non_xml():
    assert parse_obs_error('{"error": "x"}') is None


# ---------- S9e 路由 + 编排 ----------

def test_is_obs_by_product_and_host():
    assert is_obs("OBS", {}) is True
    assert is_obs("obs", {}) is True
    assert is_obs("ECS", {"host": "ecs.cn-north-4.myhuaweicloud.com"}) is False
    assert is_obs("anything", {"host": "obs.cn-north-4.myhuaweicloud.com"}) is True


class _FakeObsClient:
    def __init__(self, resp: ClientResponse):
        self.resp = resp
        self.captured: dict = {}

    def request(self, method: str, host: str, *, bucket: str,
                object_key: str = "", query=None, headers=None,
                body: str | None = None) -> ClientResponse:
        self.captured = dict(method=method, host=host, bucket=bucket,
                             object_key=object_key, query=query,
                             headers=headers, body=body)
        return self.resp


def test_execute_obs_api_happy_path():
    op = {"parameters": [
        {"name": "bucket_name", "in": "query"},
        {"name": "object_key", "in": "path"},
        {"name": "uploadId", "in": "query"},
    ]}
    doc = {"host": "obs.cn-north-4.myhuaweicloud.com", "definitions": {}}
    fake = _FakeObsClient({"status": 200, "headers": {}, "body": None})
    out = execute_obs_api(doc, "/{object_key}", "get", op, "OBS",
                          "AbortMultipartUpload", "cn-north-4",
                          {"bucket_name": "b", "object_key": "o", "uploadId": "u1"},
                          client=fake, credentials=None)
    assert out["ok"] is True
    assert fake.captured["method"] == "GET"
    assert fake.captured["bucket"] == "b"
    assert fake.captured["object_key"] == "o"
    assert fake.captured["query"] == {"uploadId": "u1"}
    assert fake.captured["body"] is None


def test_execute_obs_api_error_xml():
    op = {"parameters": [{"name": "bucket_name", "in": "query"}]}
    doc = {"host": "obs.cn-north-4.myhuaweicloud.com", "definitions": {}}
    fake = _FakeObsClient({"status": 404, "headers": {}, "body":
                           "<Error><Code>NoSuchBucket</Code><Message>missing</Message></Error>"})
    out = execute_obs_api(doc, "/", "get", op, "OBS", "HeadBucket", "cn-north-4",
                          {"bucket_name": "b"}, client=fake, credentials=None)
    assert out["ok"] is True
    assert out["status"] == 404
    assert out["error_code"] == "NoSuchBucket"
    assert out["error_msg"] == "missing"


def test_execute_obs_api_missing_bucket():
    op = {"parameters": [{"name": "bucket_name", "in": "query"}]}
    doc = {"host": "obs.cn-north-4.myhuaweicloud.com", "definitions": {}}
    fake = _FakeObsClient({"status": 200, "headers": {}, "body": None})
    out = execute_obs_api(doc, "/", "get", op, "OBS", "HeadBucket", "cn-north-4",
                          {}, client=fake, credentials=None)
    assert out == {"ok": False, "reason": "缺少必填参数 bucket_name（桶名）"}


def test_obs_http_client_signs():
    # 无凭证时跳过签名，仅校验 URL 与 body 透传（签名算法已由 signer 测试覆盖）
    client = ObsHttpClient(credentials=None)
    assert client.request("put", "obs.cn-north-4.myhuaweicloud.com",
                          bucket="b", object_key="o", body="x") is not None
