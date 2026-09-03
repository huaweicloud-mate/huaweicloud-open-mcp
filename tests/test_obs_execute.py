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


# 真实 SetObjectAcl param 级元数据形状：AccessControlList 为裸数组，items 内联
# （无 $ref、无 xml 名暗示），线上 item 元素名 Grant 由模块内登记表提供。
# 注意：该接口元数据 x-xml-root 错标为 ObjectAccessControlPolicy（响应包装属性名
# 混入，SetBucketAcl 元数据即正确标为 AccessControlPolicy），线上根元素以官方
# x-request-examples 为准 —— AccessControlPolicy，由根元素纠偏登记表覆盖。
ACL_OP = {
    "parameters": [
        {"in": "body", "name": "SetObjectAclRequestBody",
         "schema": {
             "required": ["Owner"],
             "properties": {
                 "Owner": {"$ref": "#/definitions/Owner"},
                 "Delivered": {"type": "boolean"},
                 "AccessControlList": {
                     "type": "array",
                     "items": {
                         "properties": {
                             "Grantee": {"$ref": "#/definitions/Grantee"},
                             "Permission": {"type": "string"},
                         },
                     },
                 },
             },
             "x-xml-root": "ObjectAccessControlPolicy",
         }},
    ],
}
ACL_DOC = {
    "host": "obs.cn-north-4.myhuaweicloud.com",
    "definitions": {
        "Owner": {"properties": {"ID": {"type": "string"}}},
        "Grantee": {"properties": {"ID": {"type": "string"},
                                    "Canned": {"type": "string"}}},
    },
}
ACL_XML_GOLD = (
    '<AccessControlPolicy xmlns="http://obs.cn-north-4.myhuaweicloud.com/doc/2015-06-30/">'
    "<Owner><ID>b4bf1b36d9ca43d984fbcb9491b6fce9</ID></Owner>"
    "<AccessControlList>"
    "<Grant><Grantee><ID>b4bf1b36d9ca43d984fbcb9491b6fce9</ID></Grantee>"
    "<Permission>FULL_CONTROL</Permission></Grant>"
    "<Grant><Grantee><Canned>Everyone</Canned></Grantee>"
    "<Permission>READ</Permission></Grant>"
    "</AccessControlList>"
    "</AccessControlPolicy>")


def test_serialize_body_xml_acl_array_wraps_container():
    """裸数组形状（schema 忠实）：容器元素 AccessControlList 包裹逐个 Grant。

    金标结构取自官方示例（Grantee 支持 ID / Canned 两种形态）；
    修复前此形状把每个 Grant 渲染成 <AccessControlList>，服务端报 MalformedACLError。
    """
    body = {
        "Owner": {"ID": "b4bf1b36d9ca43d984fbcb9491b6fce9"},
        "AccessControlList": [
            {"Grantee": {"ID": "b4bf1b36d9ca43d984fbcb9491b6fce9"},
             "Permission": "FULL_CONTROL"},
            {"Grantee": {"Canned": "Everyone"}, "Permission": "READ"},
        ],
    }
    assert serialize_body_xml(ACL_OP, ACL_DOC, body) == ACL_XML_GOLD


def test_serialize_body_xml_boolean_lowercase():
    """XML Schema boolean 词法形：true/false 小写（Delivered/Quiet 等字段）。"""
    body = {"Owner": {"ID": "d1"}, "Delivered": False,
            "AccessControlList": [{"Grantee": {"ID": "d1"},
                                   "Permission": "FULL_CONTROL"}]}
    assert serialize_body_xml(ACL_OP, ACL_DOC, body) == (
        '<AccessControlPolicy xmlns="http://obs.cn-north-4.myhuaweicloud.com/doc/2015-06-30/">'
        "<Owner><ID>d1</ID></Owner>"
        "<Delivered>false</Delivered>"
        "<AccessControlList><Grant><Grantee><ID>d1</ID></Grantee>"
        "<Permission>FULL_CONTROL</Permission></Grant></AccessControlList>"
        "</AccessControlPolicy>")
    body_true = {"Delivered": True, "AccessControlList": []}
    assert serialize_body_xml(ACL_OP, ACL_DOC, body_true) == (
        '<AccessControlPolicy xmlns="http://obs.cn-north-4.myhuaweicloud.com/doc/2015-06-30/">'
        "<Delivered>true</Delivered>"
        "<AccessControlList></AccessControlList>"
        "</AccessControlPolicy>")


def test_serialize_body_xml_root_override_for_wrong_metadata():
    """根元素纠偏：SetObjectAcl 元数据 x-xml-root 错标 ObjectAccessControlPolicy
    （e2e 实证服务端报 MalformedACLError），线上根以官方示例 AccessControlPolicy
    为准；纠偏按元数据声明值命中，不影响正确元数据（SetBucketAcl 等）。"""
    body = {"Owner": {"ID": "x"}, "AccessControlList": []}
    xml = serialize_body_xml(ACL_OP, ACL_DOC, body) or ""
    assert xml.startswith('<AccessControlPolicy xmlns=')
    assert "ObjectAccessControlPolicy" not in xml


def test_serialize_body_xml_acl_container_dict_shape_same_output():
    """XML 形状 dict（容器对象 + Grant 键）与裸数组形状收敛到同一金标。"""
    body = {
        "Owner": {"ID": "b4bf1b36d9ca43d984fbcb9491b6fce9"},
        "AccessControlList": {"Grant": [
            {"Grantee": {"ID": "b4bf1b36d9ca43d984fbcb9491b6fce9"},
             "Permission": "FULL_CONTROL"},
            {"Grantee": {"Canned": "Everyone"}, "Permission": "READ"},
        ]},
    }
    assert serialize_body_xml(ACL_OP, ACL_DOC, body) == ACL_XML_GOLD


def test_serialize_body_xml_inline_array_keeps_repeat():
    """反例回归：DeleteObjects 内联 Object 数组——item 元素名与属性名一致时
    重复渲染、不包容器（definitions 形式 $ref 名 DeleteObject 不是线上元素名）。"""
    op = {"parameters": [{"in": "body", "name": "DeleteObjectsRequestBody",
                          "schema": {
                              "properties": {
                                  "Quiet": {"type": "boolean"},
                                  "Object": {"type": "array", "items": {
                                      "properties": {"Key": {"type": "string"},
                                                      "VersionId": {"type": "string"}}}},
                              },
                              "x-xml-root": "Delete",
                          }}]}
    doc = {"definitions": {}}
    body = {"Quiet": True,
            "Object": [{"Key": "k1"}, {"Key": "k2", "VersionId": "v2"}]}
    assert serialize_body_xml(op, doc, body) == (
        "<Delete><Quiet>true</Quiet>"
        "<Object><Key>k1</Key></Object>"
        "<Object><Key>k2</Key><VersionId>v2</VersionId></Object>"
        "</Delete>")


def test_serialize_body_xml_scalar_array_repeat():
    """标量数组（CORS AllowedMethod）：逐项重复属性名元素。"""
    op = {"parameters": [{"in": "body", "name": "SetBucketCorsRequestBody",
                          "schema": {
                              "properties": {
                                  "CORSRule": {"type": "array", "items": {
                                      "properties": {
                                          "AllowedOrigin": {"type": "string"},
                                          "AllowedMethod": {"type": "array",
                                                             "items": {"type": "string"}},
                                      }}}},
                              "x-xml-root": "CORSConfiguration",
                          }}]}
    doc = {"definitions": {}}
    body = {"CORSRule": [{"AllowedOrigin": "*", "AllowedMethod": ["GET", "PUT"]}]}
    assert serialize_body_xml(op, doc, body) == (
        "<CORSConfiguration><CORSRule><AllowedOrigin>*</AllowedOrigin>"
        "<AllowedMethod>GET</AllowedMethod><AllowedMethod>PUT</AllowedMethod>"
        "</CORSRule></CORSConfiguration>")


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


# ---------- S9f-b 预签发 URL 编排 ----------

PRESIGN_OP_GET = {"parameters": [{"name": "bucket_name", "in": "query", "required": True},
                                 {"name": "object_key", "in": "path", "required": True}]}
PRESIGN_DOC = {"host": "obs.cn-north-4.myhuaweicloud.com", "definitions": {}}


def test_execute_presign_api_get_object():
    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
        "cn-north-4", {"bucket_name": "my-bucket", "object_key": "dir/a b.zip"},
        credentials=Credentials(ak="CRED-AK", sk="SK-TEST"))
    assert out["ok"] is True
    assert out["product"] == "OBS" and out["api"] == "GetObject"
    ps = out["presign"]
    assert ps["method"] == "GET" and ps["expires_in"] == 900
    url = ps["url"]
    assert url.startswith("https://my-bucket.obs.cn-north-4.myhuaweicloud.com/dir/a%20b.zip?")
    assert "AccessKeyId=CRED-AK" in url and "Signature=" in url
    exp = int(url.split("Expires=", 1)[1].split("&", 1)[0])
    import time as _t
    assert abs(exp - (_t.time() + 900)) < 5


def test_execute_presign_api_custom_expires_and_content_type():
    from urllib.parse import unquote

    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "put", PRESIGN_OP_GET, "OBS", "PutObject",
        "cn-north-4", {"bucket_name": "b", "object_key": "u.bin",
                       "_presign_expires": 60, "_presign_content_type": "text/plain"},
        credentials=Credentials(ak="AK9", sk="SK9"))
    assert out["ok"] is True and out["presign"]["method"] == "PUT"
    assert out["presign"]["expires_in"] == 60
    url = out["presign"]["url"]
    expires_epoch = int(url.split("Expires=", 1)[1].split("&", 1)[0])
    sig = url.rsplit("Signature=", 1)[1]
    expected_b64 = __import__("base64").b64encode(__import__("hmac").new(
        b"SK9", b"PUT\n\ntext/plain\n" + str(expires_epoch).encode() +
        b"\n/b/u.bin", __import__("hashlib").sha1).digest()).decode()
    assert unquote(sig) == expected_b64   # CT 锁定参与签名（独立公式交叉验证）
    ps = out["presign"]
    assert ps["signed_content_type"] == "text/plain"
    assert ps["headers"] == {"Content-Type": "text/plain"}   # 照抄清单
    assert "note" not in ps


def test_execute_presign_put_default_warns_empty_content_type():
    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "put", PRESIGN_OP_GET, "OBS", "PutObject",
        "cn-north-4", {"bucket_name": "b", "object_key": "u.bin"},
        credentials=Credentials(ak="AK9", sk="SK9"))
    assert out["ok"] is True
    ps = out["presign"]
    assert ps["signed_content_type"] == ""
    assert ps["headers"] == {}
    note = ps.get("note") or ""
    assert "Content-Type" in note
    assert "_presign_content_type" in note      # 指引锁定方式


def test_execute_presign_get_envelope_clean():
    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
        "cn-north-4", {"bucket_name": "b", "object_key": "o"},
        credentials=Credentials(ak="AK9", sk="SK9"))
    assert out["ok"] is True
    ps = out["presign"]
    assert ps["signed_content_type"] == ""
    assert ps["headers"] == {}
    assert "note" not in ps          # GET 无 body，无 CT 警示


def test_execute_presign_api_invalid_expires():
    from common.auth.credentials import Credentials
    for bad in ("abc", "0", "-5"):
        out = execute_obs.execute_presign_api(
            PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
            "cn-north-4", {"bucket_name": "b", "object_key": "o",
                           "_presign_expires": bad},
            credentials=Credentials(ak="A", sk="B"))
        assert out["ok"] is False
        assert "_presign_expires" in (out.get("reason") or "")


def test_execute_presign_api_requires_credentials():
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
        "cn-north-4", {"bucket_name": "b", "object_key": "o"}, credentials=None)
    assert out["ok"] is False and "AK/SK" in (out.get("reason") or "")


def test_execute_presign_api_missing_object_key_passthrough():
    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
        "cn-north-4", {"bucket_name": "b"},
        credentials=Credentials(ak="A", sk="B"))
    assert out["ok"] is False
    assert "object_key" in (out.get("reason") or "")
