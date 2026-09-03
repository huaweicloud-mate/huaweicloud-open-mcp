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
    """默认 add = 会话内：同实例立即放行；文件不动，重启等价不可见。"""
    svc, p = make_service(tmp_path, ["*=deny"])
    assert svc._check_policy("@huaweicloud/ecs") is not None
    before = p.read_text(encoding="utf-8")

    res = svc.manage_policy("add", "server:@huaweicloud/ecs=allow")
    assert res["ok"] is True
    assert res["scope"] == "session"
    assert p.read_text(encoding="utf-8") == before
    assert svc._check_policy("@huaweicloud/ecs") is None          # 连接放行
    assert svc._check_policy("@huaweicloud/ecs", "list*") is not None  # 未授工具级

    other, _ = make_service(tmp_path, ["*=deny"])                 # 重启等价
    assert other._check_policy("@huaweicloud/ecs") is not None


def test_add_permanent_persists_and_list(tmp_path):
    """scope=permanent 落盘；list 返回结构化 rules（评估序，overlay 前置）。"""
    svc, p = make_service(tmp_path, ["*=deny"])
    assert svc.manage_policy("add", "server:@huaweicloud/ecs=allow",
                             scope="permanent")["ok"] is True
    assert "server:@huaweicloud/ecs=allow" in p.read_text(encoding="utf-8")

    svc.manage_policy("add", "server:@huaweicloud/oss:list*=allow",
                      scope="temporary", ttl_seconds=120)
    listed = svc.manage_policy("list")
    assert listed["ok"] is True
    assert [r["scope"] for r in listed["rules"]] == [
        "temporary", "permanent", "permanent"]
    assert listed["rules"][0]["line"] == "server:@huaweicloud/oss:list*=allow"
    assert 0 < listed["rules"][0]["expires_in"] <= 120
    assert listed["rules"][1]["line"] == "server:@huaweicloud/ecs=allow"


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


# ---------- 一次性授权（once，call_tool dispatch 前） ----------

class FakeSession:
    """最小 SessionClient 替身：记录调用台账。"""

    def __init__(self):
        self.calls: list[tuple] = []

    async def connect(self, endpoint: str):
        self.calls.append(("connect", endpoint))
        return {"protocol_version": "1.0",
                "server_info": {"name": "fake", "version": "1.0"}}

    async def list_tools(self):
        self.calls.append(("list_tools", ()))
        return []

    async def call_tool(self, tool: str, arguments: dict):
        self.calls.append(("call_tool", tool, arguments))
        return {"ok": True}

    async def disconnect(self):
        self.calls.append(("disconnect", ()))


def test_call_tool_once_rule_allows_first_call_only(tmp_path):
    """server 工具规则 once：首次 call_tool 放行并焚毁，二次拒绝；代发仅一次。"""
    import asyncio

    from mcp_discover.manager import SessionManager

    svc, _ = make_service(tmp_path, ["*=deny"])
    fake = FakeSession()
    svc.manager = SessionManager(client_factory=lambda: fake)
    asyncio.run(svc.manager.connect("srv1", "http://s/mcp"))
    assert svc.manage_policy("add", "server:srv1:call*=allow", scope="once")["ok"] is True

    first = asyncio.run(svc.call_tool("srv1", "callThing", {"a": 1}))
    assert first["ok"] is True
    second = asyncio.run(svc.call_tool("srv1", "callThing", {"a": 1}))
    assert second["ok"] is False
    assert "safety policy 拒绝调用" in (second.get("reason") or "")
    tool_calls = [c for c in fake.calls if c[0] == "call_tool"]
    assert tool_calls == [("call_tool", "callThing", {"a": 1})]
