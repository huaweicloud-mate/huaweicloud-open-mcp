"""缺省姿态文件的语料守门测试（修订方案 D2'，principle-build-the-lever）。

缺省 policy 内容的正确性不再靠人审规则文本，由本测试对提交接口语料
（configs/entity-index.json，17,792 API）机械背书：

1. 零写放行硬不变量：PUT/DELETE/PATCH 方法接口的放行集合必须为空
   ——「写操作缺省拒绝」的机械形态；
2. 无名放行禁令：每个放行接口的命中规则必须是全局动词行或产品域
   精选行（first-match 的赢家可解释），点名拒绝层必须压过动词层；
3. 单源一致性：文件全局动词行与 safety.policy.READONLY_VERB_PREFIXES
   逐一相等（防文件与 readonly 授予档漂移）；
4. 敏感脊柱：对抗审查实证过的敏感/写样例逐个断言 deny（AgentIdentity
   凭证铸造、BMS 控制台、CCI Secret、两个 CodeArtsRepo PUT、Check 四
   PUT、F1 的 DeleteBlackWhiteIpList 海报案例）；
5. 覆盖披露：GET/HEAD 覆盖率 ≥ 95%（实测 96.9%），残量与敏感命名
   放行清单打印供人工复核（打印非断言，语料随上游演进）。

已知局限：语料为提交快照，上游新增接口在下次 api-refresh graph 重建
前不可见（AGENTS.md 记录）。
"""
import json

from common.paths import project_root
from safety.policy import READONLY_VERB_PREFIXES, evaluate, match_first, parse_policy

WRITE_METHODS = {"put", "delete", "patch"}
READ_METHODS = {"get", "head"}


def _load_default():
    path = project_root() / "configs" / "safety-policy.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    return entries, parse_policy(entries)


def _corpus():
    path = project_root() / "configs" / "entity-index.json"
    return json.loads(path.read_text(encoding="utf-8"))["apis"]


def _global_verbs(entries):
    """文件中的全局动词行（`*:Verb*=allow` 形），保持行序。"""
    verbs = []
    for line in entries:
        if not isinstance(line, str) or not line.startswith("*:") \
                or not line.endswith("*=allow"):
            continue
        verbs.append(line[2:-7])
    return verbs


def test_gate_zero_write_method_allowed():
    """硬不变量：缺省姿态零放行 PUT/DELETE/PATCH 方法接口。"""
    _entries, rules = _load_default()
    apis = _corpus()
    leaked = [a for a in apis
              if a["m"] in WRITE_METHODS and evaluate(rules, a["p"], a["n"])]
    assert leaked == [], f"写方法接口被缺省姿态放行: {leaked[:10]}"


def test_gate_every_allow_is_justified():
    """无解释的放行禁止：命中规则必须是全局动词行或产品域精选行。"""
    _entries, rules = _load_default()
    verbs = {v.lower() for v in READONLY_VERB_PREFIXES}
    for a in _corpus():
        if not evaluate(rules, a["p"], a["n"]):
            continue
        hit = match_first(rules, a["p"], a["n"])
        assert hit is not None
        if hit.product == "*":
            name = (a["n"] or "").lower()
            assert any(name.startswith(v) for v in verbs), \
                f"全局命中但名字不锚定动词: {a['p']}:{a['n']} <- {hit.api_pattern}"
            assert hit.api_pattern.endswith("*") and "*" not in hit.api_pattern[:-1], \
                f"全局动词行必须前缀锚定（不允许子串）: {hit.api_pattern}"
        else:
            assert hit.product.lower() == a["p"].lower(), \
                f"产品行命中了别的产品: {a['p']}:{a['n']} <- {hit.product}"


def test_gate_verb_table_single_source():
    """单源一致性：文件全局动词行 == READONLY_VERB_PREFIXES（顺序也一致）。"""
    entries, _rules = _load_default()
    assert _global_verbs(entries) == list(READONLY_VERB_PREFIXES)


def test_gate_sensitive_spine_denied():
    """对抗审查实证样例逐个断言（敏感面与写泄漏海报案例）。"""
    _entries, rules = _load_default()
    must_deny = [
        ("AgentIdentity", "GetResourceStsToken"),   # STS 凭证铸造
        ("AgentIdentity", "GetTokenVault"),
        ("BMS", "ShowServerRemoteConsole"),        # 远程登录地址
        ("CCI", "listCoreV1NamespacedSecret"),     # K8s Secret
        ("CCI", "readCoreV1NamespacedSecret"),
        ("CCI", "connectCoreV1GetNamespacedPodExec"),  # 容器命令执行
        ("CodeArtsRepo", "ListTemplatesTwo"),       # PUT 真写（含 List 子串）
        ("CodeArtsRepo", "ShowHasPipeline"),        # PUT 真写（含 Show 前缀）
        ("CPCS", "CheckClusterPort"),               # Check 族 PUT
        ("Meeting", "CheckVerifyCode"),
        ("Meeting", "CheckSlideVerifyCode"),
        ("RDS", "CheckInstanceForUpgrade"),
        ("AAD", "DeleteBlackWhiteIpList"),          # F1 海报案例（List 子串写）
        ("AAD", "AddBlackWhiteIpList"),
        ("CDN", "UpdateBlackWhiteList"),
        ("HSS", "DeleteVulWhiteList"),
        ("ELB", "DeleteListener"),
        ("ELB", "UpdateListener"),
        ("SFSTurbo", "DeleteBackendTarget"),        # get 子串写
        ("SMN", "CreateMessageTemplate"),           # get 子串写
        ("ECS", "DeleteServers"),
    ]
    for prod, api in must_deny:
        assert not evaluate(rules, prod, api), f"敏感/写样例被放行: {prod}:{api}"


def test_gate_read_posture_and_coverage():
    """读姿态主路径放行 + GET/HEAD 覆盖率下限 + 覆盖与敏感命名披露。"""
    _entries, rules = _load_default()
    must_allow = [
        ("ECS", "ListServersDetails"),
        ("ECS", "NovaListServers"),
        ("IAM", "KeystoneListProjects"),
        ("OBS", "GetObject"),
        ("AOM", "SearchTemplateById"),   # F3 样例：Search 族现开箱可用
        ("CCI", "readAppsV1NamespacedDeployment"),  # Read 族（K8s 命名）
    ]
    for prod, api in must_allow:
        assert evaluate(rules, prod, api), f"只读样例被拒: {prod}:{api}"

    apis = _corpus()
    reads = [a for a in apis if a["m"] in READ_METHODS]
    allowed = [a for a in reads if evaluate(rules, a["p"], a["n"])]
    ratio = len(allowed) / len(reads)
    print(f"\n缺省姿态 GET/HEAD 覆盖: {len(allowed)}/{len(reads)} = {ratio:.1%}"
          f"（残量 {len(reads) - len(allowed)} 走确认流）")
    assert ratio >= 0.95, f"GET/HEAD 覆盖率跌破 95%: {ratio:.1%}"

    allowed_all = [a for a in apis if evaluate(rules, a["p"], a["n"])]
    sensitive = ("sts", "token", "password", "secret", "console",
                 "credential", "exec", "privatekey")
    flagged = [(a["p"], a["n"]) for a in allowed_all
               if any(k in (a["n"] or "").lower() for k in sensitive)]
    print(f"放行总数 {len(allowed_all)}；敏感命名放行 {len(flagged)} 条"
          f"（样例 {flagged[:6]}）——人工复核面，见 AGENTS.md 已披露取舍")
