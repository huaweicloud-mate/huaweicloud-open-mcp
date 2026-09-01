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


# ---------- policy_denial_offer（E1 discover 服务层） ----------

def test_policy_denial_offer_server_constructed(tmp_path):
    from common.elicit import DenialOffer

    svc, _ = make_service(tmp_path, ["*=deny"])
    denial_reason = "safety policy 拒绝连接 @huaweicloud/ecs"
    offer = svc.policy_denial_offer("@huaweicloud/ecs", denial_reason=denial_reason)
    assert isinstance(offer, DenialOffer)
    assert offer.subject == "@huaweicloud/ecs"
    assert offer.rule == "server:@huaweicloud/ecs=allow"

    call_reason = "safety policy 拒绝调用 @huaweicloud/ecs:ListInstances"
    offer = svc.policy_denial_offer("@huaweicloud/ecs", "ListInstances",
                                    denial_reason=call_reason)
    assert offer is not None
    assert offer.rule == "server:@huaweicloud/ecs:ListInstances=allow"
    assert offer.subject == "@huaweicloud/ecs:ListInstances"


def test_policy_denial_offer_server_none_cases(tmp_path):
    svc, _ = make_service(tmp_path, ["server:@huaweicloud/ecs=allow"])
    assert svc.policy_denial_offer("@huaweicloud/ecs") is None          # 放行
    assert svc.policy_denial_offer("@huaweicloud/ecs", "list*") is not None

    unconfigured = DiscoverService(DiscoverConfig())
    assert unconfigured.policy_denial_offer("@huaweicloud/ecs") is None  # 未配置 store

    # reason 不一致（门栓外的其它拒绝）→ 不提议
    assert svc.policy_denial_offer("@huaweicloud/ecs", "list*",
                                   denial_reason="其它原因") is None
