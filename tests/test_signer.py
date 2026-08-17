"""signer 单元测试。

期望值来自华为云官方 Go SDK（huaweicloud-sdk-go-v3 core/auth/signer/signer_test.go）
与官方签名算法实现的公开测试向量，非自行推导。
"""

from openmcp.signer import sign

AK = "AccessKey"
SK = "SecretKey"
HOST = "example.huaweicloud.com"
SDK_DATE = "20060102T150405Z"


def test_official_vector_get():
    headers = sign.sign_request(
        method="GET",
        host=HOST,
        path="/path",
        query={"limit": 1},
        headers={"X-Sdk-Date": SDK_DATE, "TEST_UNDERSCORE": "TEST_VALUE"},
        body=None,
        ak=AK,
        sk=SK,
    )
    assert headers["X-Sdk-Date"] == SDK_DATE
    assert headers["Authorization"] == (
        "SDK-HMAC-SHA256 Access=AccessKey, SignedHeaders=x-sdk-date, "
        "Signature=5a2ce64c865e0e6046321c6f3d5a77ba8413eeaf355c3166c03d58d02ac79624"
    )


def test_official_vector_post():
    # 官方 SDK 用 json.Encoder 序列化 struct，尾部带 \n；官方向量的 body 字节即此。
    # signer 对传入的 wire bytes 原样求哈希，与官方向量一致。
    headers = sign.sign_request(
        method="POST",
        host=HOST,
        path="/path",
        query={"key": "value"},
        headers={"X-Sdk-Date": SDK_DATE, "TEST_UNDERSCORE": "TEST_VALUE",
                 "Content-Type": "application/json"},
        body=b'{"Name":"test","Id":1}\n',
        ak=AK,
        sk=SK,
    )
    assert headers["Authorization"] == (
        "SDK-HMAC-SHA256 Access=AccessKey, SignedHeaders=x-sdk-date, "
        "Signature=cecc2af119b18ab70b4d094c0750f3b42c02f254903179a0fc2cc72fc9db4f59"
    )


def test_official_vector_canonical_query_string():
    qs = sign.canonical_query_string({
        "limit": 1,
        "enable": True,
        "test": "一 (&=?!#%.*)",
        "path": "/tmp/123",
        "multi": [1, 2, 3],
    })
    assert qs == ("enable=true&limit=1&multi=1&multi=2&multi=3"
                  "&path=%2Ftmp%2F123&test=%E4%B8%80%20%28%26%3D%3F%21%23%25.%2A%29")


def test_escape_keeps_unreserved():
    assert sign.escape("azAZ09_-~.") == "azAZ09_-~."


def test_escape_percent_encodes():
    assert sign.escape(" ") == "%20"
    assert sign.escape("/") == "%2F"
    assert sign.escape("一") == "%E4%B8%80"


def test_canonical_uri_trailing_slash():
    assert sign.canonical_uri("/path") == "/path/"
    assert sign.canonical_uri("/") == "/"


def test_canonical_uri_escapes_segment():
    assert sign.canonical_uri("/a b/c") == "/a%20b/c/"


def test_signed_headers_exclude_underscore_and_content_type():
    sh = sign.signed_headers_list({"X-Sdk-Date": "t", "X-Auth": "v", "TEST_UNDERSCORE": "x",
                                   "Content-Type": "application/json", "X-Custom": "1"})
    assert sh == ["x-auth", "x-custom", "x-sdk-date"]


def test_sign_string_to_sign_uses_date():
    cr = ("GET\n/path/\n\nhost:example.huaweicloud.com\nx-sdk-date:20060102T150405Z\n"
          "\nx-sdk-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    sts = sign.string_to_sign(cr, SDK_DATE)
    assert sts.startswith("SDK-HMAC-SHA256\n20060102T150405Z\n")
    assert sts.split("\n")[2] == sign.sha256_hex(cr.encode("utf-8"))
