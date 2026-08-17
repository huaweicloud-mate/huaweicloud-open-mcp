"""safety policy：阿里云式 allowlist/denylist 模式匹配。

规则格式：`product:apiPattern=allow|deny`，每行一条。
- product 可用 `*`；apiPattern 为 fnmatch 通配。
- 按文件行序首个命中生效；无匹配默认 deny。
- `#` 开头与空行忽略；格式非法抛 ValueError。
- 策略文件支持 JSON 数组（保序）或纯文本行两种格式。
"""

import fnmatch
import json
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PolicyRule:
    product: str
    api_pattern: str
    allow: bool


def parse_policy(lines: Sequence[str]) -> list[PolicyRule]:
    """把规则行列表解析为 PolicyRule 列表，保持行序。"""
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
        if ":" not in target:
            if target == "*":
                product, pattern = "*", "*"
            else:
                raise ValueError(f"策略规则缺少 product 前缀（product:apiPattern=action）: {line!r}")
        else:
            product, _, pattern = target.partition(":")
        product = product.strip() or "*"
        pattern = pattern.strip() or "*"
        rules.append(PolicyRule(product=product, api_pattern=pattern, allow=action == "allow"))
    return rules


def evaluate(rules: Sequence[PolicyRule], product: str, api: str) -> bool:
    """按行序首个命中生效；无匹配默认 deny。product/api 大小写不敏感。"""
    for rule in rules:
        if rule.product != "*" and rule.product.lower() != product.lower():
            continue
        if fnmatch.fnmatch(api.lower(), rule.api_pattern.lower()):
            return rule.allow
    return False


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
