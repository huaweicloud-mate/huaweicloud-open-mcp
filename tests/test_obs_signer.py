"""OBS Header 签名单元测试（S9a）。

期望信息来源（独立真值）：
- StringToSign 结构来自华为云官方文档「Header中携带签名」的 StringToSign 构造示例（表4/6/7）；
- 签名值由 openssl 依据官方文档的 StringToSign + HMAC-SHA1 公式独立计算，非被测代码推导。

Sign = Base64(HMAC-SHA1(SK, StringToSign))
StringToSign = VERB + "\\n" + Content-MD5 + "\\n" + Content-Type + "\\n" + Date + "\\n"
             + CanonicalizedOBSHeaders + CanonicalizedResource
"""

from mcp_openapi.signer import obs

AK = "AccessKey"
SK = "SecretKey"


def test_string_to_sign_doc_table4_get_object():
    # 表4：获取对象（仅 Date 头域，无 Content-MD5/Content-Type/x-obs-*）
    sts = obs.obs_string_to_sign(
        "GET", bucket="bucket", object_key="object.txt",
        headers={"Date": "Sat, 12 Oct 2015 08:12:38 GMT"},
    )
    assert sts == "GET\n\n\nSat, 12 Oct 2015 08:12:38 GMT\n/bucket/object.txt"


def test_string_to_sign_doc_table6_with_headers():
    # 表6：带请求头字段上传对象（content-type + x-obs-acl，Content-MD5 缺省为空）
    sts = obs.obs_string_to_sign(
        "PUT", bucket="bucket", object_key="object.txt",
        headers={"Date": "Mon, 14 Oct 2015 12:08:34 GMT",
                 "Content-Type": "text/plain",
                 "x-obs-acl": "public-read"},
    )
    assert sts == ("PUT\n\ntext/plain\nMon, 14 Oct 2015 12:08:34 GMT\n"
                   "x-obs-acl:public-read\n/bucket/object.txt")


def test_string_to_sign_doc_table7_subresource():
    # 表7：获取对象 ACL（子资源 acl 无值）
    sts = obs.obs_string_to_sign(
        "GET", bucket="bucket", object_key="object.txt",
        query={"acl": ""},
        headers={"Date": "Sat, 12 Oct 2015 08:12:38 GMT"},
    )
    assert sts == "GET\n\n\nSat, 12 Oct 2015 08:12:38 GMT\n/bucket/object.txt?acl"


def test_signature_doc_table6():
    # goldens 由 openssl 计算：
    # printf 'PUT\n\ntext/plain\nMon, 14 Oct 2015 12:08:34 GMT\nx-obs-acl:public-read\n/bucket/object.txt'
    #   | openssl dgst -sha1 -hmac 'SecretKey' -binary | base64
    out = obs.sign_obs(
        "PUT", ak=AK, sk=SK, bucket="bucket", object_key="object.txt",
        headers={"Content-Type": "text/plain", "x-obs-acl": "public-read"},
        date="Mon, 14 Oct 2015 12:08:34 GMT",
    )
    assert out["Authorization"] == "OBS AccessKey:LrOGmklRTLb28024TIELh3UE74E="
    assert out["Date"] == "Mon, 14 Oct 2015 12:08:34 GMT"


def test_signature_doc_table7_subresource():
    out = obs.sign_obs(
        "GET", ak=AK, sk=SK, bucket="bucket", object_key="object.txt",
        query={"acl": ""},
        date="Sat, 12 Oct 2015 08:12:38 GMT",
    )
    assert out["Authorization"] == "OBS AccessKey:1xFGmA8aoV96E4Fdxt1IVxCnwxw="


def test_x_obs_date_empties_date_line():
    # 官方文档：Date 与 x-obs-date 并存时，Date 参数按空处理（以 x-obs-date 为准）
    sts = obs.obs_string_to_sign(
        "PUT", bucket="bucket", object_key="object.txt",
        headers={"Date": "Sat, 12 Oct 2015 08:12:38 GMT",
                 "x-obs-date": "Tue, 15 Oct 2015 07:20:09 GMT"},
    )
    assert sts.startswith("PUT\n\n\n\nx-obs-date:Tue, 15 Oct 2015 07:20:09 GMT\n")


def test_canonicalized_resource_only_whitelisted_subresources():
    # prefix/max-keys 等列表参数不进 CanonicalizedResource；subresource 按字典序
    res = obs.canonicalized_resource(
        "bucket", "dir/photo a.jpg",
        {"acl": "", "prefix": "p", "uploadId": "abc", "response-content-type": "text/plain"},
    )
    assert res == ("/bucket/dir/photo%20a.jpg"
                   "?acl&response-content-type=text/plain&uploadId=abc")


def test_object_key_escaping_keeps_slash():
    assert obs.escape_object_key("a/b c") == "a/b%20c"


def test_vhost_resource_trailing_slash():
    # 对齐官方 Go SDK conf.go：vhost 下桶名后恒有 `/`，无对象也保留
    assert obs.canonicalized_resource("b", "", virtual_hosted=True) == "/b/"
    assert obs.canonicalized_resource("b", "o.txt", virtual_hosted=True) == "/b/o.txt"
    # path-style 保持无尾斜杠
    assert obs.canonicalized_resource("b", "") == "/b"


def test_signature_vhost_bucket_only_golden():
    # goldens 由 openssl 计算（官方表格样例 CreateBucket 请求，vhost 资源含尾斜杠）：
    # printf 'PUT\n\napplication/xml\nFri, 06 Jul 2018 03:45:51 GMT\nx-obs-acl:private\n/newbucketname2/'
    #   | openssl dgst -sha1 -hmac 'SecretKey' -binary | base64
    out = obs.sign_obs(
        "PUT", ak=AK, sk=SK, bucket="newbucketname2",
        headers={"x-obs-acl": "private", "Content-Type": "application/xml"},
        date="Fri, 06 Jul 2018 03:45:51 GMT",
        virtual_hosted=True,
    )
    assert out["Authorization"] == f"OBS {AK}:7YQzx+sTPCwzCbOvP6EeQivd404="


# ---------- URL 中携带签名（S9f-a） ----------
#
# 期望信息来源（独立真值）：
# - StringToSign 结构来自华为云官方文档「URL中携带签名」表4/表5 原文
#   （Date 位替换为 Expires UNIX 秒时间戳，其余与 Header 方式一致）；
# - 官方示例未公开对应 SK，签名值按官方公式 Signature = URL-Encode(Base64(HMAC-SHA1(SK, STS)))
#   由 openssl 口径独立预计算为字面量（printf '<sts>' | openssl dgst -sha1 -hmac 'SecretKey'
#   -binary | base64），RFC3986 严格编码后写入断言，非被测代码推导。


def test_url_string_to_sign_doc_table4_structure():
    # 官方表4原文：GET /objectkey?...Expires=1532779451...
    sts = obs.url_string_to_sign("GET", bucket="examplebucket", object_key="objectkey",
                                 expires=1532779451)
    assert sts == "GET\n\n\n1532779451\n/examplebucket/objectkey"


def test_url_string_to_sign_doc_table5_temp_token():
    # 官方表5：临时 AK/SK 场景，security-token 作为子资源进入 CanonicalizedResource
    sts = obs.url_string_to_sign(
        "GET", bucket="bucket", object_key="objectkey", expires=1532779451,
        query={"x-obs-security-token": "TOKEN123"},
    )
    assert sts == ("GET\n\n\n1532779451\n"
                   "/bucket/objectkey?x-obs-security-token=TOKEN123")


def test_url_signature_goldens():
    # openssl 金标（SK='SecretKey'）：
    # A: printf 'GET\n\n\n1532779451\n/examplebucket/objectkey' → S0mZabLxzI3DxNGveZ4kio2q7iQ=
    # B: printf 'GET\n\n\n1532779451\n/bucket/object.txt?response-content-type=text/plain&versionId=v1'
    #      → AhHyPOd5z1UVPELdlpJ41b82Mrs=
    sts_a = obs.url_string_to_sign("GET", bucket="examplebucket", object_key="objectkey",
                                   expires=1532779451)
    assert obs.obs_signature(sts_a, SK) == "S0mZabLxzI3DxNGveZ4kio2q7iQ="
    sts_b = obs.url_string_to_sign(
        "GET", bucket="bucket", object_key="object.txt", expires=1532779451,
        query={"versionId": "v1", "response-content-type": "text/plain"},
    )
    assert sts_b == ("GET\n\n\n1532779451\n"
                     "/bucket/object.txt?response-content-type=text/plain&versionId=v1")
    assert obs.obs_signature(sts_b, SK) == "AhHyPOd5z1UVPELdlpJ41b82Mrs="


def test_sign_obs_url_assembles_signed_url():
    url = obs.sign_obs_url(
        "GET", ak="AccessKey", sk=SK, host="obs.cn-north-4.myhuaweicloud.com",
        bucket="bucket", object_key="dir/a b.zip", expires=1532779451,
        query={"response-content-disposition": "attachment"},
    )
    # virtual-hosted 寻址 + 对象名编码 + 子资源在 auth 三参数之前
    assert url.startswith(
        "https://bucket.obs.cn-north-4.myhuaweicloud.com/dir/a%20b.zip?")
    assert ("response-content-disposition=attachment&"
            "AccessKeyId=AccessKey&Expires=1532779451&Signature=") in url
    sig = url.rsplit("Signature=", 1)[1]
    # 金标：STS 为 GET\n\n\n1532779451\n/bucket/dir/a%20b.zip?response-content-disposition=attachment
    # printf 该串 | openssl dgst -sha1 -hmac 'SecretKey' -binary | base64
    #   = QM2ROs+zm00rYNkqm7ZBNFYOaH8= → RFC3986 严格编码
    assert sig == "QM2ROs%2Bzm00rYNkqm7ZBNFYOaH8%3D"


def test_sign_obs_url_put_with_content_type():
    url = obs.sign_obs_url(
        "PUT", ak="AccessKey", sk=SK, host="obs.example.com",
        bucket="bucket", object_key="upload.bin", expires=1532779451,
        content_type="text/plain",
    )
    assert url == (
        "https://bucket.obs.example.com/upload.bin?"
        "AccessKeyId=AccessKey&Expires=1532779451&Signature=IJjth67l56ui0skXKWNveY7Gbks%3D")
