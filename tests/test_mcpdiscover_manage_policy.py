"""DiscoverService.manage_policy 单测：策略热生效（S2b discover 侧）。"""

from mcp_discover.config import DiscoverConfig
from mcp_discover.service import DiscoverService
from safety.policy_store import PolicyStore


def make_service(tmp_path, entries):
    import json
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return DiscoverService(DiscoverConfig(policy_store=PolicyStore(str(p)))), p


def test_grant_takes_effect_without_restart(tmp_path):
    svc, _ = make_service(tmp_path, ["*=deny"])
    assert svc._check_policy("@huaweicloud/ecs") is not None

    res = svc.manage_policy("add", "server:@huaweicloud/ecs=allow")
    assert res["ok"] is True
    assert "manage_policy" in res["policy"] or "server:@huaweicloud/ecs=allow" in res["policy"]
    assert svc._check_policy("@huaweicloud/ecs") is None          # 连接放行
    assert svc._check_policy("@huaweicloud/ecs", "list*") is not None  # 未授工具级


def test_remove_revokes_and_persists(tmp_path):
    svc, p = make_service(tmp_path,
                          ["server:@huaweicloud/ecs:list*=allow", "*=deny"])
    assert svc._check_policy("@huaweicloud/ecs", "ListTools") is None

    res = svc.manage_policy("remove", "server:@huaweicloud/ecs:list*=allow")
    assert res["ok"] is True
    assert svc._check_policy("@huaweicloud/ecs", "ListTools") is not None
    text = p.read_text(encoding="utf-8")
    assert "list*=allow" not in text                              # 文件已同步


def test_unconfigured_store_rejected():
    svc = DiscoverService(DiscoverConfig())
    out = svc.manage_policy("add", line="*=allow")
    assert out["ok"] is False and "--policy" in out["reason"]
