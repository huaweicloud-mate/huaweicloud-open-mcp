"""S6：benchmark case 加载与校验纯函数单测。"""

import pytest

from benchmarks import cases as bench_cases

MINIMAL = """\
id: ecs_list
prompt: 查询云服务器列表
expect:
  execute:
    product: ECS
    api: ListServersDetails
"""


def test_load_single_case(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(MINIMAL, encoding="utf-8")
    out = bench_cases.load_cases(p)
    assert len(out) == 1
    c = out[0]
    assert c.id == "ecs_list"
    assert c.prompt == "查询云服务器列表"
    assert [e.product for e in c.expect.executes] == ["ECS"]
    assert [e.api for e in c.expect.executes] == ["ListServersDetails"]
    assert c.expect.params is None
    assert c.expect.answer is None
    assert c.expect.forbidden == ()
    assert c.repeat == 3
    assert c.timeout == 600


def test_execute_accepts_alternatives(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        """\
id: t
prompt: p
expect:
  execute:
    - {product: ECS, api: ListServersDetails}
    - {product: ECS, api: ListCloudServers}
""",
        encoding="utf-8",
    )
    c = bench_cases.load_cases(p)[0]
    assert [e.api for e in c.expect.executes] == ["ListServersDetails", "ListCloudServers"]


def test_load_directory_ignores_non_yaml(tmp_path):
    (tmp_path / "a.yaml").write_text(MINIMAL, encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "c.yml").write_text(
        MINIMAL.replace("id: ecs_list", "id: ecs_list2"), encoding="utf-8"
    )
    out = bench_cases.load_cases(tmp_path)
    assert [c.id for c in out] == ["ecs_list", "ecs_list2"]


def test_full_fields_parsed(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        """\
id: neg
prompt: 删除云服务器
expect:
  forbidden:
    - product: ECS
      api: DeleteServers
  answer: 无法删除
repeat: 5
timeout: 120
""",
        encoding="utf-8",
    )
    c = bench_cases.load_cases(p)[0]
    assert len(c.expect.executes) == 0
    assert c.expect.answer == "无法删除"
    assert [f.api for f in c.expect.forbidden] == ["DeleteServers"]
    assert c.repeat == 5
    assert c.timeout == 120


@pytest.mark.parametrize(
    ("doc", "msg_part"),
    [
        ("id: a\nexpect: {execute: {product: ECS, api: X}}", "prompt"),
        ("id: a\nprompt: ''\nexpect: {execute: {product: ECS, api: X}}", "prompt"),
        ("id: a\nprompt: p\nexpect: {execute: {product: ECS}}", "api"),
        ("id: a\nprompt: p\nexpect: {execute: {product: '', api: X}}", "product"),
        ("id: a\nprompt: p\nexpect: {execute: [{product: ECS, api: X}, {product: VPC}]}", "api"),
        ("id: a\nprompt: p\nexpect: {execute: [], params: {a: 1}}", "expect"),
        ("id: a\nprompt: p\nexpect: {execute: {product: ECS, api: X}, params: []}", "params"),
        ("id: a\nprompt: p\nexpect: {answer: 123}", "answer"),
        ("id: a\nprompt: p\nexpect: {forbidden: [{api: X}]}", "forbidden"),
        ("id: a\nprompt: p\nexpect: {}", "expect"),
        ("id: a\nprompt: p\nexpect: {execute: {product: ECS, api: X}}\nrepeat: 0", "repeat"),
        ("id: a\nprompt: p\nexpect: {execute: {product: ECS, api: X}}\ntimeout: -1", "timeout"),
        ("id: a\nprompt: p", "expect"),
    ],
)
def test_invalid_cases_rejected(tmp_path, doc, msg_part):
    p = tmp_path / "bad.yaml"
    p.write_text(doc, encoding="utf-8")
    with pytest.raises(ValueError, match=msg_part):
        bench_cases.load_cases(p)


def test_duplicate_id_rejected(tmp_path):
    (tmp_path / "a.yaml").write_text(MINIMAL, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(MINIMAL, encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        bench_cases.load_cases(tmp_path)


def test_missing_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        bench_cases.load_cases(tmp_path / "nope.yaml")
