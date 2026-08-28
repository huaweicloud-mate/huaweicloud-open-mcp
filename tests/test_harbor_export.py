"""S6：Harbor exporter（render_task 纯核金标 + export_dataset 薄壳 tmp 集成）。

用 mini project_root 注入（免依赖真实仓库树）；模板用真实 task_templates/，
金标锚定真实模板的渲染产物。
"""

import json
from pathlib import Path

import yaml

from benchmarks.cases import parse_case
from benchmarks.harbor import exporter

MINI_CASE_YAML = """\
id: ecs_list
prompt: 帮我查询北京四地域的云服务器列表
expect:
  execute: {product: ECS, api: ListServersDetails}
  answer: 服务器
  constraints:
    max_calls: 8
"""


def _mini_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "configs").mkdir()
    (root / "src" / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (root / "configs" / "safety-policy.example.json").write_text('{"rules": []}',
                                                                 encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (root / "uv.lock").write_text("", encoding="utf-8")
    readme_stub = "# x\n"
    (root / "README.md").write_text(readme_stub, encoding="utf-8")
    return root


def _case(case_yaml: str = MINI_CASE_YAML):
    return parse_case(yaml.safe_load(case_yaml), "mini.yaml")


def test_render_task_golden_core_files(tmp_path):
    files = exporter.render_task(_case(), MINI_CASE_YAML, project_root=_mini_project(tmp_path))
    assert set(files) >= {
        "instruction.md", "task.toml", "environment/Dockerfile",
        "environment/docker-compose.yaml",
        "environment/start_services.sh", "environment/start_mcp.sh",
        "environment/stub_server.py", "environment/fixtures.json",
        "environment/policy.json",         "tests/test.sh", "tests/test_outputs.py",
        "tests/case.yaml", "solution/solve.sh", "solution/oracle.py",
        "solution/case.yaml",
    }
    assert files["environment/hwc/pyproject.toml"] == "[project]\nname = 'x'\n"
    assert files["environment/hwc/README.md"] == "# x\n"
    assert files["environment/hwc/src/pkg/mod.py"] == "X = 1\n"
    assert files["tests/case.yaml"] == MINI_CASE_YAML

    toml_text = files["task.toml"]
    assert 'name = "mcp/ecs_list"' in toml_text
    assert "capability = \"retrieval\"" in toml_text
    assert "network_mode = \"public\"" in toml_text
    assert 'command = "/opt/hwc/start_mcp.sh"' in toml_text
    assert "urllib.request.urlopen('http://127.0.0.1:8010/health'" in toml_text

    instruction = files["instruction.md"]
    assert "帮我查询北京四地域的云服务器列表" in instruction
    assert "/tmp/answer.txt" in instruction

    start_mcp = files["environment/start_mcp.sh"]
    assert "--mock-passthrough" in start_mcp
    assert "--audit-file /tmp/hwc_audit.jsonl" in start_mcp
    assert "http://127.0.0.1:8010" in start_mcp

    verifier = files["tests/test_outputs.py"]
    assert "event_to_toolcall" in verifier
    assert "/tests/case.yaml" in verifier


def test_render_task_fixture_and_case_yaml_roundtrip(tmp_path):
    case_yaml = MINI_CASE_YAML + "fixture:\n  apis:\n    ECS/ListServersDetails:\n      body: {count: 1}\n"
    files = exporter.render_task(_case(case_yaml), case_yaml,
                                 project_root=_mini_project(tmp_path))
    fixture = json.loads(files["environment/fixtures.json"])
    assert fixture == {"apis": {"ECS/ListServersDetails": {"body": {"count": 1}}}}


def test_render_task_capability_and_difficulty_derivation(tmp_path):
    _mini_project(tmp_path)
    base = "id: t\nprompt: p\nexpect:\n  execute: {product: E, api: A}\n"

    safety_yaml = base + "  forbidden:\n    - {product: E, api: DeleteX}\n"
    assert exporter.derive_capability(_case(safety_yaml)) == "safety"

    params_yaml = base + "  params: {limit: 1}\n"
    assert exporter.derive_capability(_case(params_yaml)) == "execute-correctness"

    assert exporter.derive_capability(_case(base)) == "retrieval"

    labels_yaml = base + "labels:\n  capability: multi-step\n  difficulty: hard\n"
    assert exporter.derive_capability(_case(labels_yaml)) == "multi-step"
    assert exporter.derive_difficulty(_case(labels_yaml)) == "hard"

    slow_yaml = base + "  constraints:\n    max_calls: 12\n"
    assert exporter.derive_difficulty(_case(slow_yaml)) == "medium"
    assert exporter.derive_difficulty(_case(base)) == "easy"


def test_export_dataset_writes_task_dirs(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "a.yaml").write_text(MINI_CASE_YAML, encoding="utf-8")
    (cases_dir / "b.yaml").write_text(
        MINI_CASE_YAML.replace("ecs_list", "ecs_delete"), encoding="utf-8")
    out_dir = tmp_path / "dataset"

    written = exporter.export_dataset(cases_dir, out_dir, project_root=_mini_project(tmp_path))
    assert [p.name for p in written] == ["ecs_delete", "ecs_list"]
    for task_dir in written:
        assert (task_dir / "task.toml").exists()
        assert (task_dir / "environment" / "Dockerfile").exists()
        assert (task_dir / "tests" / "case.yaml").exists()


def test_export_dataset_case_ids_filter(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "a.yaml").write_text(MINI_CASE_YAML, encoding="utf-8")
    (cases_dir / "b.yaml").write_text(
        MINI_CASE_YAML.replace("ecs_list", "ecs_delete"), encoding="utf-8")
    out_dir = tmp_path / "dataset"

    written = exporter.export_dataset(cases_dir, out_dir, case_ids=["ecs_list"],
                                      project_root=_mini_project(tmp_path))
    assert [p.name for p in written] == ["ecs_list"]


def test_export_dataset_in_memory_case_fallback_yaml(tmp_path):
    """无源文件的 case（parse_case 构造）→ 薄壳重建最小 case YAML。"""
    case = parse_case({"id": "t1", "prompt": "p",
                       "expect": {"execute": {"product": "E", "api": "A"}}}, "")
    files = exporter.render_task(case, "", project_root=_mini_project(tmp_path))
    rebuilt = yaml.safe_load(files["tests/case.yaml"])
    assert rebuilt == {"id": "t1", "prompt": "p",
                       "expect": {"execute": [{"product": "E", "api": "A"}]}}


def test_render_task_policy_override(tmp_path):
    case_yaml = MINI_CASE_YAML + "policy: |\n  ECS:*List*=allow\n  *=deny\n"
    files = exporter.render_task(_case(case_yaml), case_yaml,
                                 project_root=_mini_project(tmp_path))
    assert files["environment/policy.json"] == "ECS:*List*=allow\n*=deny\n"


def test_render_task_policy_defaults_to_example(tmp_path):
    files = exporter.render_task(_case(), MINI_CASE_YAML,
                                 project_root=_mini_project(tmp_path))
    assert files["environment/policy.json"] == '{"rules": []}'
