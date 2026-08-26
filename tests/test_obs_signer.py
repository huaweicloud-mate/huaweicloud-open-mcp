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
