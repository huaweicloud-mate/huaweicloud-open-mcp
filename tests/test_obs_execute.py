"""OBS 执行 lane 单元测试（S9b/S9c/S9d/S9e）。

期望值来自手工构造的迷你 schema/op 片段与手写字面量（仿 apis fixtures 设计），
不依赖真实 raw/ data/。
"""

import json
import os

from apie import convert_openapi2 as conv
from common.types import ClientResponse
from mcp_openapi import execute_obs
from mcp_openapi.execute_obs import (
    ObsHttpClient,
    build_obs_request,
    build_obs_url,
    execute_obs_api,
    is_obs,
    parse_obs_error,
    render_response_body,
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


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def test_serialize_body_xml_x_xml_root_after_convert():
    """盲区堵点：真实形态 raw fixture（含 xml.name）经转换管线后根元素名必须保留。

    运行时 LiveFallback 走 conv.convert_api，clean_schema 会剥掉 xml 键；
    回归点是 x-xml-root 提升机制保证 serialize 输出官方根元素而非参数名。
    """
    with open(os.path.join(FIXTURES, "obs_create_bucket_raw.json"),
              encoding="utf-8") as f:
        raw = json.load(f)
    doc = conv.convert_api(raw)
    assert doc["definitions"]["CreateBucketRequestBody"]["x-xml-root"] == \
        "CreateBucketConfiguration"
    op = next(iter(doc["paths"]["/"].values()))
    xml = serialize_body_xml(op, doc, {"Location": "cn-north-4"})
    assert xml == ('<CreateBucketConfiguration '
                   'xmlns="http://obs.cn-north-4.myhuaweicloud.com/doc/2015-06-30/">'
                   "<Location>cn-north-4</Location></CreateBucketConfiguration>")


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


# ---------- B/E：媒体分流 + base64 上传 ----------

def test_build_obs_request_json_media_uses_json():
    # SetBucketPolicy 类：op consumes application/json
    op = {"consumes": ["application/json"],
          "parameters": [{"name": "bucket_name", "in": "query"},
                         {"in": "body", "name": "SetBucketPolicyRequestBody",
                          "schema": {}}]}
    body = {"Statement": [{"Effect": "Allow"}]}
    req = build_obs_request(op, "/", {"bucket_name": "b", "body": body}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert json.loads(req.body) == body  # type: ignore[arg-type]
    assert req.content_type == "application/json"


def test_build_obs_request_xml_media_default():
    # 无 op consumes → dict 默认走 XML
    op = {"parameters": [{"name": "bucket_name", "in": "query"},
                         {"in": "body", "name": "B", "schema": {}}]}
    req = build_obs_request(op, "/", {"bucket_name": "b", "body": {"Status": "Enabled"}}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert req.body == "<B><Status>Enabled</Status></B>"
    assert req.content_type == "application/xml"


def test_build_obs_request_b64_binary_upload():
    import base64
    raw_bytes = b"\x00\x01hello\xff"
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    op = {"consumes": ["application/octet-stream"],
          "parameters": [{"name": "bucket_name", "in": "query"},
                         {"name": "object_key", "in": "path"}]}
    req = build_obs_request(op, "/{object_key}",
                            {"bucket_name": "b", "object_key": "o.bin",
                             "_content_b64": b64, "body": "ignored"}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert req.body == raw_bytes
    assert req.content_type == "application/octet-stream"


def test_build_obs_request_invalid_b64_rejected():
    op = {"parameters": [{"name": "bucket_name", "in": "query"}]}
    out = build_obs_request(op, "/", {"bucket_name": "b", "_content_b64": "@@not-b64@@"}, {})
    assert out == "_content_b64 不是合法的 base64 编码"


def test_build_obs_request_normalizes_bool_query():
    op = {"parameters": [{"name": "bucket_name", "in": "query"},
                         {"name": "enabled", "in": "query", "type": "boolean"}]}
    req = build_obs_request(op, "/", {"bucket_name": "b", "enabled": True,
                                      "unknown_flag": False}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert req.query == {"enabled": "true", "unknown_flag": "false"}


def test_build_obs_request_auto_subresource_required():
    # 桶级配置接口的开关型子资源（?tagging）缺失时自动补空值；值型 required 不受影响
    op = {"parameters": [{"name": "bucket_name", "in": "query"},
                         {"name": "tagging", "in": "query", "required": True, "type": "string"},
                         {"name": "versionId", "in": "query", "required": True, "type": "string"}]}
    req = build_obs_request(op, "/", {"bucket_name": "b", "versionId": "v1"}, {})
    assert isinstance(req, execute_obs.ObsRequest)
    assert req.query["tagging"] == ""
    assert req.query["versionId"] == "v1"


def test_build_obs_request_missing_required_query_reports():
    op = {"parameters": [{"name": "bucket_name", "in": "query"},
                         {"name": "uploadId", "in": "query", "required": True, "type": "string"}]}
    out = build_obs_request(op, "/", {"bucket_name": "b"}, {})
    assert out == "缺少必填参数: uploadId"


# ---------- D：二进制下载占位 / C：响应头摘取 ----------

def test_render_response_body_binary_placeholder():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    out = render_response_body(200, "image/png", raw)
    assert out == {"note": "二进制响应未渲染", "size": len(raw), "content_type": "image/png"}


def test_render_response_body_text_passthrough_when_decodable():
    out = render_response_body(200, "application/x-custom", b"<List>x</List>")
    assert out == "<List>x</List>"


def test_render_response_body_error_and_xml_always_text():
    xml = "<Error><Code>X</Code></Error>".encode()
    assert render_response_body(404, "image/png", xml) == "<Error><Code>X</Code></Error>"
    assert render_response_body(200, "application/xml", xml) == "<Error><Code>X</Code></Error>"


def test_normalize_obs_picks_whitelisted_headers():
    resp: ClientResponse = {"status": 200, "headers": {
        "ETag": '"abc123"', "X-Obs-Request-Id": "req-1", "Date": "now",
        "Server": "obs"},
        "body": None}
    out = execute_obs._normalize_obs(resp)
    assert out["headers"] == {"etag": '"abc123"', "x-obs-request-id": "req-1"}


def test_normalize_obs_no_headers_when_absent():
    out = execute_obs._normalize_obs({"status": 204, "headers": {}, "body": None})
    assert "headers" not in out


def test_obs_http_client_auto_content_md5(monkeypatch):
    import base64 as b64
    import hashlib as hl
    import urllib.request as ur

    captured: dict = {}

    class _Resp:
        status = 200
        headers: dict = {}

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _Resp()

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)

    client = ObsHttpClient(credentials=None)
    body = "hello"
    expected_md5 = b64.b64encode(hl.md5(body.encode()).digest()).decode()
    client.request("put", "obs.cn-north-4.myhuaweicloud.com", bucket="b", body=body)
    sent = {k.lower(): v for k, v in captured["headers"].items()}
    assert sent["content-md5"] == expected_md5


def test_build_obs_url_virtual_hosted():
    # 带桶 → virtual-hosted（线上对桶级操作强制）；根路径 `/` 恒在
    url = build_obs_url("obs.cn-north-4.myhuaweicloud.com", "mybucket",
                        "dir/a b.txt", {"acl": "", "uploadId": "u1", "prefix": "p/"})
    assert url == ("https://mybucket.obs.cn-north-4.myhuaweicloud.com/dir/a%20b.txt"
                   "?acl&prefix=p%2F&uploadId=u1")
    assert build_obs_url("obs.cn-north-4.myhuaweicloud.com", "b", "",
                         {"tagging": ""}) == \
        "https://b.obs.cn-north-4.myhuaweicloud.com/?tagging"
    assert build_obs_url("obs.cn-north-4.myhuaweicloud.com", "", "") == \
        "https://obs.cn-north-4.myhuaweicloud.com/"


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
                body: str | bytes | None = None) -> ClientResponse:
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
