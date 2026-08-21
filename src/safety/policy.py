"""safety policy：阿里云式 allowlist/denylist 模式匹配。

规则格式：`product:apiPattern=allow|deny`，每行一条。
- product 可用 `*`；apiPattern 为 fnmatch 通配。
- 按文件行序首个命中生效；无匹配默认 deny。
- `#` 开头与空行忽略；格式非法抛 ValueError。
- 策略文件支持 JSON 数组（保序）或纯文本行两种格式。

MCP discover 扩展：
- `server:serverId=allow|deny`              控制 connect_mcp_server
- `server:serverId:toolPattern=allow|deny`  控制 call_server_tool
- 与 product 规则同文件、保行序；kind 字段区分，前缀天然隔离。
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
    kind: str = field(default="product")
    connect_only: bool = field(default=False)

    def __post_init__(self) -> None:
        if self.kind not in ("product", "server"):
            raise ValueError(f"PolicyRule.kind must be 'product' or 'server', got {self.kind!r}")


def parse_policy(lines: Sequence[str]) -> list[PolicyRule]:
    """把规则行列表解析为 PolicyRule 列表，保持行序。

    支持两种前缀：
    - product:apiPattern=allow|deny  (kind="product"，向后兼容)
    - server:serverId[:toolPattern]=allow|deny  (kind="server")
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

        kind = "product"
        if target.startswith("server:"):
            kind = "server"
            target = target[len("server:"):]

        product: str
        pattern: str
        connect_only = False
        if ":" not in target:
            if kind == "product":
                if target == "*":
                    product, pattern = "*", "*"
                else:
                    raise ValueError(f"策略规则缺少 product 前缀（product:apiPattern=action）: {line!r}")
            else:
                product = target.strip() or "*"
                pattern = "*"
                connect_only = True
        else:
            if kind == "server":
                product, _, pattern = target.partition(":")
            else:
                product, _, pattern = target.partition(":")
        product = product.strip() or "*"
        pattern = pattern.strip() or "*"

        rules.append(PolicyRule(
            product=product, api_pattern=pattern, allow=action == "allow",
            kind=kind, connect_only=connect_only,
        ))
    return rules


def evaluate(rules: Sequence[PolicyRule], product: str, api: str) -> bool:
    """按行序首个命中生效；无匹配默认 deny。product/api 大小写不敏感。
    仅评估 kind="product" 规则；kind="server" 规则由 evaluate_server 处理。
    """
    for rule in rules:
        if rule.kind != "product":
            continue
        if rule.product != "*" and rule.product.lower() != product.lower():
            continue
        if fnmatch.fnmatch(api.lower(), rule.api_pattern.lower()):
            return rule.allow
    return False


def evaluate_server(rules: Sequence[PolicyRule], server: str, tool: str | None = None) -> bool:
    """评估 server 类规则。按行序首个命中生效；无匹配默认 deny。

    tool=None 时匹配 connect 级规则（api_pattern="*"）；
    tool 非空时 fnmatch 匹配 tool 名。
    """
    for rule in rules:
        if rule.kind != "server":
            continue
        if rule.product != "*" and rule.product.lower() != server.lower():
            continue
        if tool is not None:
            if rule.connect_only:
                continue
            if fnmatch.fnmatch(tool.lower(), rule.api_pattern.lower()):
                return rule.allow
        else:
            if not rule.connect_only:
                continue
            return rule.allow
    return False


def check(rules: Sequence[PolicyRule] | None, product: str, api: str) -> str | None:
    """检查执行是否被策略允许。返回 None 表示允许，返回字符串为拒绝原因。"""
    if rules is None:
        return "safety policy 未配置，execute_api 全部拒绝"
    if not evaluate(rules, product, api):
        return f"safety policy 拒绝执行 {product}:{api}"
    return None


def check_server(rules: Sequence[PolicyRule] | None, server: str, tool: str | None = None) -> str | None:
    """检查 server 连接/调用是否被策略允许。返回 None 表示允许，返回字符串为拒绝原因。"""
    if rules is None:
        return "safety policy 未配置，discover 连接与调用全部拒绝"
    if not evaluate_server(rules, server, tool=tool):
        if tool is not None:
            return f"safety policy 拒绝调用 {server}:{tool}"
        return f"safety policy 拒绝连接 {server}"
    return None


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
