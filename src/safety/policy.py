"""safety policy：阿里云式 allowlist/denylist 模式匹配。

规则格式：`product:apiPattern=allow|deny`，每行一条。
- product 可用 `*`；apiPattern 为 fnmatch 通配。
- 按文件行序首个命中生效；无匹配默认 deny。
- `#` 开头与空行忽略；格式非法抛 ValueError。
- 策略文件支持 JSON 数组（保序）或纯文本行两种格式。
"""

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class PolicyRule:
    product: str
    api_pattern: str
    allow: bool
    once: bool = field(default=False)


READONLY_VERB_PREFIXES: tuple[str, ...] = (
    "List", "Show", "Get", "Query", "Search", "Read",
    "Describe", "Count", "Check", "BatchShow", "BatchList", "BatchGet",
)
"""只读动词前缀表（单源）：缺省姿态文件的全局动词行与 elicitation readonly
授予档共用。锚定语义（`:Verb*` 仅匹配名字以前缀开头）是 2026-11 对抗审查
实测后的刻意选择——子串形态 `*List*` 会放行 248 个含子串的真写接口
（DeleteBlackWhiteIpList/DeleteListener/UpdateCoreGatewayTarget 等）。
正确性由 tests/test_default_policy_gate.py 按真实接口语料机械守护。"""


def parse_policy(lines: Sequence[str]) -> list[PolicyRule]:
    """把规则行列表解析为 PolicyRule 列表，保持行序。

    规则格式：`product:apiPattern=allow|deny`（product 可用 `*`，apiPattern 为
    fnmatch 通配）；`#` 注释与空行忽略；格式非法抛 ValueError。
    """
    rules: list[PolicyRule] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "=" not in text:
            raise ValueError(f"策略规则缺少 '=': {line!r}")
        target, _, action = text.rpartition("=")
        action = action.strip().lower()
        if action not in ("allow", "deny"):
            raise ValueError(f"策略动作必须是 allow/deny: {line!r}")

        if target.startswith("server:"):
            raise ValueError(
                f"server: 规则已随 discover 模式移除（规则行: {line!r}）；"
                "本网关仅支持 product:apiPattern=allow|deny")

        product: str
        pattern: str
        if ":" not in target:
            if target == "*":
                product, pattern = "*", "*"
            else:
                raise ValueError(f"策略规则缺少 product 前缀（product:apiPattern=action）: {line!r}")
        else:
            product, _, pattern = target.partition(":")
        product = product.strip() or "*"
        pattern = pattern.strip() or "*"

        rules.append(PolicyRule(
            product=product, api_pattern=pattern, allow=action == "allow",
        ))
    return rules


def evaluate(rules: Sequence[PolicyRule], product: str, api: str) -> bool:
    """按行序首个命中生效；无匹配默认 deny。product/api 大小写不敏感。"""
    for rule in rules:
        if rule.product != "*" and rule.product.lower() != product.lower():
            continue
        if fnmatch.fnmatch(api.lower(), rule.api_pattern.lower()):
            return rule.allow
    return False


def match_first(rules: Sequence[PolicyRule], product: str, api: str) -> PolicyRule | None:
    """按行序返回首个命中的 product 类规则；无匹配返回 None。

    internal seam：供 PolicyStore.authorize 与测试消费，服务层不接触规则对象。
    匹配口径与 evaluate 完全一致（大小写不敏感）。
    """
    for rule in rules:
        if rule.product != "*" and rule.product.lower() != product.lower():
            continue
        if fnmatch.fnmatch(api.lower(), rule.api_pattern.lower()):
            return rule
    return None


def check(rules: Sequence[PolicyRule] | None, product: str, api: str) -> str | None:
    """检查执行是否被策略允许。返回 None 表示允许，返回字符串为拒绝原因。"""
    if rules is None:
        return "safety policy 未配置，execute_api 全部拒绝"
    if not evaluate(rules, product, api):
        return f"safety policy 拒绝执行 {product}:{api}"
    return None


def grant_rule(product: str, api: str) -> str:
    """构造最小 product allow 规则文本（拒绝后的提议授予用）。

    api 传 "*" 即产品级规则（如 VPC:*=allow，放行该产品全部 API）。
    """
    return f"{product}:{api}=allow"


def readonly_grant_rules(product: str) -> list[str]:
    """构造产品级只读规则集（拒绝后 readonly 提议授予用，S-B）。

    从 READONLY_VERB_PREFIXES 单源派生，前缀锚定（`:List*` 仅匹配以 List
    开头的接口名）。覆盖绝大多数只读 op 的命名动词：List/Show/Get/Query/
    Search/Read/Describe/Count/Check 与 Batch 组合形态，session 档整组
    授予；动词在名中段的读接口（KeystoneList* 等）与极少数非命名只读
    API 仍需单独最小授予。
    """
    return [f"{product}:{verb}*=allow" for verb in READONLY_VERB_PREFIXES]


def simulate_grant(rules: Sequence[PolicyRule], candidate_lines: Sequence[str],
                   product: str, api: str) -> bool:
    """纯函数：在 rules 前叠加 candidate_lines 后，product:api 是否放行。

    叠加位置对齐 PolicyStore 会话档 overlay 语义（overlay 前置 ++
    文件规则，first-match）——候选 allow 可穿透文件 deny（对 readonly 档
    这是已披露的已知取舍：确认后可越过点名拒绝层）。用于 offer 构造期
    判定 readonly 档对当前接口的边际增益（零增益时向用户标注，防确认
    循环空转）。候选行解析失败按零增益处理（fail-closed）。
    """
    try:
        candidates = parse_policy(candidate_lines)
    except ValueError:
        return False
    return evaluate(candidates + list(rules), product, api)


def load_policy_file(path: str) -> list[PolicyRule]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    try:
        data = json.loads(content)
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return parse_policy(data)
    except json.JSONDecodeError:
        pass
    return parse_policy(content.splitlines())
