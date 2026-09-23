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

    运行时 LiveFallback 走 conv.convert_api，schema_normalize 保留 xml 键并
    提升 x-xml-root；回归点是根元素名经 xml.name/x-xml-root 保留，serialize
    输出官方根元素而非参数名。
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


# ---------- D：二进制响应统一走 parse_body（bytes → normalize 占位+落盘） ----------

def _fake_urlopen(monkeypatch, status: int, headers: dict, raw: bytes) -> None:
    import urllib.request as ur
    code = status
    hdrs = headers

    class _Resp:
        status = code
        headers = hdrs

        def read(self):
            return raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ur, "urlopen", lambda req, timeout=None: _Resp())


def _capture_urlopen(monkeypatch) -> list:
    """捕获发往 urllib 的请求头（每次调用一条 dict）。"""
    import urllib.request as ur
    captured: list = []

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
        captured.append(dict(req.headers))
        return _Resp()

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    return captured


def test_obs_http_client_binary_body_passthrough_bytes(monkeypatch):
    """OBS adapter 不再本地渲染二进制：raw bytes 原样进 ClientResponse
    （判据单点收拢于 parse_body，与 real/mock lane 同源）。"""
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    _fake_urlopen(monkeypatch, 200, {"Content-Type": "image/png"}, raw)
    client = ObsHttpClient(credentials=None)
    resp = client.request("get", "obs.cn-north-4.myhuaweicloud.com",
                          bucket="b", object_key="o.png")
    assert isinstance(resp["body"], bytes)
    assert resp["body"] == raw


def test_normalize_obs_binary_body_placeholder_and_spill(tmp_path):
    """统一占位契约：OBS 二进制与 real/mock lane 同形 + .bin 保真。"""
    from mcp_openapi.spill import SpillConfig
    raw = b"\x89PNG\r\n\x1a\n\x00\xff"
    out = execute_obs._normalize_obs(
        {"status": 200, "headers": {"Content-Type": "image/png"}, "body": raw},
        spill=SpillConfig(dir=tmp_path), stem="OBS-Get")
    assert out["body"]["binary"] is True
    assert out["body"]["content_type"] == "image/png"
    assert out["body"]["size"] == len(raw)
    assert out["spill"]["format"] == "bin"
    with open(out["spill"]["path"], "rb") as f:
        assert f.read() == raw                   # disk == wire


def test_obs_http_client_error_xml_text_roundtrip(monkeypatch):
    """错误 XML（合法 UTF-8 无 NUL）恒文本：parse_obs_error 判定不受影响。"""
    xml = b"<Error><Code>NoSuchKey</Code><Message>m</Message></Error>"
    _fake_urlopen(monkeypatch, 404, {"Content-Type": "application/xml"}, xml)
    client = ObsHttpClient(credentials=None)
    resp = client.request("get", "obs.cn-north-4.myhuaweicloud.com",
                          bucket="b", object_key="missing")
    assert resp["body"] == xml.decode()
    out = execute_obs._normalize_obs(resp)
    assert out["error_code"] == "NoSuchKey"
    assert out["error_msg"] == "m"


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


# ---------- S9g 临时凭证（securitytoken）OBS lane 注入 ----------

def test_obs_http_client_injects_security_token_header(monkeypatch):
    """临时凭证：ObsHttpClient 签名前注入 x-obs-security-token（官方「Header中携带签名」
    表5——头域随 x-obs- 前缀进 CanonicalizedHeaders 参与签名）。"""
    from common.auth.credentials import Credentials
    captured = _capture_urlopen(monkeypatch)
    cred = Credentials(ak="AK", sk="SK", security_token="TOK123")
    ObsHttpClient(credentials=cred).request(
        "get", "obs.cn-north-4.myhuaweicloud.com", bucket="b", object_key="o.txt")
    sent = {k.lower(): v for k, v in captured[0].items()}
    assert sent["x-obs-security-token"] == "TOK123"


def test_obs_http_client_security_token_participates_in_signature(monkeypatch):
    """token 值变化 → Authorization 随之变化（参与 HMAC-SHA1 签名的行为锚定）。"""
    from common.auth.credentials import Credentials
    captured = _capture_urlopen(monkeypatch)
    client1 = ObsHttpClient(credentials=Credentials(ak="AK", sk="SK", security_token="T1"))
    client2 = ObsHttpClient(credentials=Credentials(ak="AK", sk="SK", security_token="T2"))
    client1.request("get", "obs.cn-north-4.myhuaweicloud.com", bucket="b")
    client2.request("get", "obs.cn-north-4.myhuaweicloud.com", bucket="b")
    auth1 = {k.lower(): v for k, v in captured[0].items()}["authorization"]
    auth2 = {k.lower(): v for k, v in captured[1].items()}["authorization"]
    assert auth1.startswith("OBS AK:")
    assert auth1 != auth2


def test_obs_http_client_no_token_header_without_temp_credentials(monkeypatch):
    """回归红线：无临时凭证（永久 AK/SK 或 None）时不注入 x-obs-security-token。"""
    from common.auth.credentials import Credentials
    captured = _capture_urlopen(monkeypatch)
    ObsHttpClient(credentials=Credentials(ak="AK", sk="SK")).request(
        "get", "obs.cn-north-4.myhuaweicloud.com", bucket="b")
    ObsHttpClient(credentials=None).request(
        "get", "obs.cn-north-4.myhuaweicloud.com", bucket="b")
    for headers in captured:
        assert "x-obs-security-token" not in {k.lower() for k in headers}


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


# ---------- S9g 临时凭证（securitytoken）presign 子资源 ----------

def test_execute_presign_api_temp_token_in_url():
    """临时凭证 presign：x-obs-security-token 作为白名单子资源进 CanonicalizedResource
    签名并追加到 URL（官方「URL中携带签名」表5 形态）。签名以 SK9 对含 token 子资源的
    StringToSign 独立交叉验证。"""
    import base64
    import hashlib
    import hmac as _hmac
    from urllib.parse import unquote

    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
        "cn-north-4", {"bucket_name": "b", "object_key": "o"},
        credentials=Credentials(ak="AK9", sk="SK9", security_token="TOK"))
    assert out["ok"] is True
    url = out["presign"]["url"]
    assert "x-obs-security-token=TOK" in url
    expires_epoch = int(url.split("Expires=", 1)[1].split("&", 1)[0])
    sig = unquote(url.rsplit("Signature=", 1)[1])
    expected = base64.b64encode(_hmac.new(
        b"SK9", b"GET\n\n\n" + str(expires_epoch).encode() +
        b"\n/b/o?x-obs-security-token=TOK", hashlib.sha1).digest()).decode()
    assert sig == expected


def test_execute_presign_api_no_token_url_clean():
    """回归红线：无临时凭证时 URL 不含 x-obs-security-token（信封形态不变）。"""
    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
        "cn-north-4", {"bucket_name": "b", "object_key": "o"},
        credentials=Credentials(ak="AK9", sk="SK9"))
    assert "x-obs-security-token" not in out["presign"]["url"]
    assert set(out["presign"].keys()) == {"url", "method", "expires_in",
                                          "signed_content_type", "headers"}


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


# ---------- S9f-c GetObject 预签发 HEAD 预检 ----------

def _presign_call(client=None, api="GetObject", method="get",
                  params: dict | None = None):
    from common.auth.credentials import Credentials
    return execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", method, PRESIGN_OP_GET, "OBS", api,
        "cn-north-4", params if params is not None
        else {"bucket_name": "b", "object_key": "o"},
        credentials=Credentials(ak="AK", sk="SK"), client=client)


def test_presign_head_precheck_200_fills_expected():
    fake = _FakeObsClient({"status": 200,
                           "headers": {"Content-Length": "716",
                                       "ETag": '"abc123"'},
                           "body": None})
    out = _presign_call(client=fake)
    assert out["ok"] is True
    assert fake.captured["method"] == "HEAD"
    assert fake.captured["bucket"] == "b"
    assert fake.captured["object_key"] == "o"
    ps = out["presign"]
    assert ps["expected_size"] == 716
    assert ps["expected_etag"] == '"abc123"'
    assert "expected_size" in ps.get("note", "")


def test_presign_head_precheck_headers_case_insensitive():
    fake = _FakeObsClient({"status": 200,
                           "headers": {"content-length": "716", "etag": "abc"},
                           "body": None})
    ps = _presign_call(client=fake)["presign"]
    assert ps["expected_size"] == 716
    assert ps["expected_etag"] == "abc"


def test_presign_head_404_denies_presign():
    fake = _FakeObsClient({"status": 404, "headers": {}, "body": None})
    out = _presign_call(client=fake)
    assert out["ok"] is False
    assert "presign" not in out
    assert "不存在" in (out.get("reason") or "")
    assert "func_code.link" in (out.get("reason") or "")


def test_presign_head_404_with_error_xml_denies():
    fake = _FakeObsClient({"status": 404, "headers": {},
                           "body": "<Error><Code>NoSuchBucket</Code>"
                                   "<Message>missing</Message></Error>"})
    out = _presign_call(client=fake)
    assert out["ok"] is False
    assert "NoSuchBucket" in (out.get("reason") or "")


def test_presign_head_403_degrades():
    fake = _FakeObsClient({"status": 403, "headers": {}, "body": None})
    out = _presign_call(client=fake)
    assert out["ok"] is True
    ps = out["presign"]
    assert "expected_size" not in ps and "expected_etag" not in ps
    assert "预检" in ps.get("note", "")


def test_presign_head_network_error_degrades():
    class _Boom:
        def request(self, *args, **kwargs):
            raise OSError("network down")

    out = _presign_call(client=_Boom())
    assert out["ok"] is True
    ps = out["presign"]
    assert "expected_size" not in ps
    assert "预检" in ps.get("note", "")


def test_presign_no_client_regression_red_line():
    # client=None：信封与既有行为逐字段一致（回归红线）
    from common.auth.credentials import Credentials
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/{object_key}", "get", PRESIGN_OP_GET, "OBS", "GetObject",
        "cn-north-4", {"bucket_name": "b", "object_key": "o"},
        credentials=Credentials(ak="AK9", sk="SK9"), client=None)
    assert out["ok"] is True
    ps = out["presign"]
    assert set(ps.keys()) == {"url", "method", "expires_in",
                              "signed_content_type", "headers"}


def test_presign_put_no_head_even_with_client():
    fake = _FakeObsClient({"status": 200, "headers": {}, "body": None})
    out = _presign_call(client=fake, api="PutObject", method="put")
    assert out["ok"] is True
    assert fake.captured == {}      # 写对象不预检
    assert "expected_size" not in out["presign"]


def test_presign_head_versionid_passthrough():
    fake = _FakeObsClient({"status": 200,
                           "headers": {"Content-Length": "5", "ETag": "e"},
                           "body": None})
    _presign_call(client=fake, params={"bucket_name": "b", "object_key": "o",
                                       "versionId": "v1"})
    assert fake.captured["query"] == {"versionId": "v1"}


def test_presign_head_precheck_skipped_without_object_key():
    # 无 object_key（桶根列举形态）：不预检
    from common.auth.credentials import Credentials
    fake = _FakeObsClient({"status": 200, "headers": {}, "body": None})
    out = execute_obs.execute_presign_api(
        PRESIGN_DOC, "/", "get", {"parameters": [
            {"name": "bucket_name", "in": "query", "required": True}]},
        "OBS", "GetObject", "cn-north-4", {"bucket_name": "b"},
        credentials=Credentials(ak="A", sk="B"), client=fake)
    assert out["ok"] is True
    assert fake.captured == {}
    assert "expected_size" not in out["presign"]


# ---------- spill 透传（S12） ----------

def test_normalize_obs_spill_passthrough(tmp_path):
    from mcp_openapi.spill import SpillConfig
    big = {"buckets": ["b" * 100] * 4000}
    resp: ClientResponse = {"status": 200, "headers": {"ETag": '"e"'}, "body": big}
    out = execute_obs._normalize_obs(resp, spill=SpillConfig(dir=tmp_path))
    assert out["headers"] == {"etag": '"e"'}
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == big


def test_execute_obs_api_spill_passthrough(tmp_path):
    from mcp_openapi.spill import SpillConfig
    op = {"parameters": [{"name": "bucket_name", "in": "query"}]}
    doc = {"host": "obs.cn-north-4.myhuaweicloud.com", "definitions": {}}
    big = {"contents": ["c" * 200] * 2000}
    fake = _FakeObsClient({"status": 200, "headers": {}, "body": big})
    out = execute_obs_api(doc, "/", "get", op, "OBS", "ListObjects", "cn-north-4",
                          {"bucket_name": "b"}, client=fake, credentials=None,
                          spill=SpillConfig(dir=tmp_path))
    assert out["ok"] is True
    assert os.path.basename(out["spill"]["path"]).startswith("OBS-ListObjects-")
    with open(out["spill"]["path"], encoding="utf-8") as f:
        assert json.load(f) == big


# ---------- _jsonpath 投影（OBS lane：XML/错误信封降级，JSON 错误体可投影） ----------

def _extract(spec):
    from mcp_openapi.extract import parse_extract
    out = parse_extract(spec)
    assert not isinstance(out, str)
    return out


def test_normalize_obs_xml_str_body_extract_noop():
    """OBS 2xx XML body 为 str 载体：投影 no-op + note，body 现状不变。"""
    resp = {"status": 200, "headers": {}, "body": "<ListAllMyBucketsResult/>"}
    out = execute_obs._normalize_obs(resp, extract=_extract("$.a"))
    assert out["body"] == "<ListAllMyBucketsResult/>"
    assert "文本" in out["extract"]["note"]
    assert "truncated" not in out


def test_normalize_obs_xml_error_envelope_no_body_noop():
    """OBS XML <Error> 信封无 body 字段：投影 no-op + note，信封形状不变。"""
    resp = {"status": 404, "headers": {},
            "body": "<?xml?><Error><Code>NoSuchKey</Code></Error>"}
    out = execute_obs._normalize_obs(resp, extract=_extract("$.a"))
    assert out["error_code"] == "NoSuchKey"
    assert "body" not in out
    assert out["extract"]["note"]


def test_normalize_obs_json_error_body_extractable():
    """OBS 非 2xx 非 XML 错误回退分支：JSON 错误体可投影。"""
    resp = {"status": 400, "headers": {},
            "body": {"error": {"code": "E.1", "message": "boom"}}}
    out = execute_obs._normalize_obs(resp, extract=_extract("$.error.message"))
    assert out["body"] == "boom"
    assert out["error_code"] == "E.1"


def test_normalize_obs_json_success_body_extract():
    resp = {"status": 200, "headers": {},
            "body": {"buckets": [{"name": "b1"}, {"name": "b2"}]}}
    out = execute_obs._normalize_obs(resp, extract=_extract("$.buckets[*].name"))
    assert out["body"] == ["b1", "b2"]
    assert out["truncated"] is True


def test_execute_obs_api_passes_extract():
    """execute_obs_api 编排透传 extract（2xx JSON 控制面响应）。"""
    doc = {"swagger": "2.0", "host": "obs.cn-north-4.myhuaweicloud.com",
           "basePath": "/", "definitions": {}}
    op = {"operationId": "ListBuckets", "responses": {"200": {"description": "OK"}}}
    calls = []

    class _Client:
        def request(self, method, host, bucket=None, object_key=None,
                    query=None, headers=None, body=None):
            calls.append(method)
            return {"status": 200, "headers": {},
                    "body": {"buckets": [{"name": "b1"}, {"name": "b2"}]}}

    out = execute_obs.execute_obs_api(
        doc, "/", "get", op, "OBS", "ListBuckets", "cn-north-4", {},
        client=_Client(), extract=_extract("$.buckets[*].name"))
    assert out["ok"] is True
    assert out["body"] == ["b1", "b2"]
    assert calls == ["GET"]
