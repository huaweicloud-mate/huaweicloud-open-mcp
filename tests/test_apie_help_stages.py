"""api-refresh HELP_STAGES 接入（方案 B）：默认 refresh 范围不变。"""

import argparse
import json

from apie.help_docs import build_hints
from apie.memory_store import MemoryStore
from apie.refresh import (
    HELP_STAGES,
    STAGES,
    artifact_of,
    build_parser,
    cmd_refresh,
    cmd_single,
    cmd_status,
)
from mcp_openapi.server import build_openapi_app, build_openapi_config
from mcp_openapi.service import ToolService


def test_help_stages_not_in_core_stages():
    assert HELP_STAGES == ["helpdocs", "helphints"]
    assert "helpdocs" not in STAGES and "helphints" not in STAGES


def test_artifacts_registered():
    assert artifact_of("helpdocs") == "raw/help_docs.json"
    assert artifact_of("helphints") == "data/hints/help-docs-hints.json"


def test_subcommands_registered():
    parser = build_parser()
    for s in ("helpdocs", "helphints"):
        args = parser.parse_args([s, "--dry-run"])
        assert args.single == s or args.command == s


def test_refresh_default_range_excludes_help_stages(capsys):
    args = build_parser().parse_args(["refresh", "--dry-run"])
    rc = cmd_refresh(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "count" in out and "validate" in out
    assert "helpdocs" not in out and "helphints" not in out


def test_refresh_explicit_help_range(capsys):
    args = build_parser().parse_args(
        ["refresh", "--from", "helpdocs", "--to", "helphints", "--dry-run"])
    rc = cmd_refresh(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "helpdocs" in out and "helphints" in out


def test_single_help_stage_dry_run():
    args = build_parser().parse_args(["helpdocs", "--dry-run"])
    assert cmd_single(args, "helpdocs") == 0
    args = build_parser().parse_args(["helphints", "--dry-run", "--region", "cn-north-4"])
    assert cmd_single(args, "helphints") == 0


def test_status_shows_help_stages(capsys):
    args = build_parser().parse_args(["status", "--region", "cn-north-4"])
    cmd_status(args)
    out = capsys.readouterr().out
    assert "helpdocs" in out and "raw/help_docs.json" in out
    assert "helphints" in out and "data/hints/help-docs-hints.json" in out


# ---------- S13e：生成 hints 文件 → server 装配 → 注入行为端到端 ----------

GROUPS_ECS = [{"name": "计算", "products": [
    {"productshort": "ECS", "name": "弹性云服务器", "api_count": 1,
     "is_global": False, "link": None}]}]

APIS_ECS = [{"name": "NovaRebootServer", "method": "post", "summary": "重启云服务器",
             "tags": "状态管理", "product_short": "ECS", "info_version": "v2.1"}]

DOC_NRB = {
    "swagger": "2.0", "host": "ecs.cn-north-4.myhuaweicloud.com", "basePath": "/",
    "definitions": {},
    "paths": {"/v2.1/{project_id}/servers/{server_id}/action": {"post": {
        "operationId": "NovaRebootServer", "summary": "重启云服务器",
        "parameters": [], "responses": {"202": {"description": "Accepted"}}}}},
}


def test_generated_hints_file_end_to_end(tmp_path):
    hints_raw = build_hints([
        {"product": "ECS", "api": "NovaRebootServer", "url": "https://u/1",
         "detail_desc": "重启单台云服务器。", "matched_by": "name",
         "help_intro": "重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。"}])
    p = tmp_path / "help-docs-hints.json"
    p.write_text(json.dumps(hints_raw, ensure_ascii=False), encoding="utf-8")

    cfg = build_openapi_config(argparse.Namespace(
        mock=True, policy=None, region=None, mock_base=None,
        mock_passthrough=None, gate=None, audit_file=None, spill_dir=None,
        hints=str(p)))
    assert cfg.hints.api_notes_in_list_apis is False

    store = MemoryStore()
    store.set_products(GROUPS_ECS)
    store.set_apis("ECS", APIS_ECS)
    op = DOC_NRB["paths"]["/v2.1/{project_id}/servers/{server_id}/action"]["post"]
    store.set_api_cache(
        ("ecs", "NovaRebootServer", "cn-north-4"),
        (DOC_NRB, "/v2.1/{project_id}/servers/{server_id}/action", "post", op))
    svc = ToolService(store=store, config=cfg)

    app = build_openapi_app(svc)
    assert "帮助中心" in app.instructions

    # get_api：combined 注入（帮助中心功能介绍 + 链接）
    detail = svc.get_api("ECS", "NovaRebootServer")
    assert detail["hints"] == (
        "重启单台云服务器。\n当前API已废弃，请使用批量重启云服务器。\n官方帮助文档: https://u/1")

    # list_apis：条目级被抑制（顶层无产品 notes）
    listing = svc.list_apis("ECS")
    assert "hints" not in listing
    assert all("hints" not in a for a in listing["apis"])
