"""Swagger 2.0 schema 归一（schema 形状 → 2.0 合法）的单一 owner。

设计（codebase-design，见 AGENTS.md 校验规则 / ADR-0005）：
- 公共面恰一个函数 ``normalize_doc(doc)``；无 policy/knob 参数。
- **位置类型化 allowlist**（自官方 Swagger 2.0 meta-schema 派生）+
  **total 默认**：allowlist 外且非 ``x-`` 的键一律改名 ``x-<key>`` 保留
  （``^x-`` 在每个 schema 位置都被 meta-schema 放行）——键级 gate 0 结构性
  成立，未来新键零改动自动安全。
- 合法 2.0 键**保留**（含此前被误删的 ``allOf``/``discriminator``/``example``/
  ``xml``/``readOnly``/``title``/``maxProperties``/``minProperties``/
  ``externalDocs``）；非 2.0 键**无损转 ``x-``**（``oneOf→x-oneOf`` 等）。
- ``allOf`` **保留**（不展开；Draft4 原生组合，展开有 merge 风险）。
- ``xml`` 保留整块，同时提升 ``xml.name → x-xml-root``（OBS 契约）。
- 区域感知遍历：``definitions`` + body-param ``schema`` + response ``schema``，
  递归 ``properties``/``items``/``additionalProperties``/``allOf`` 成员；
  **严格位置** ``primitivesItems``（参数/头 items）与 ``fileSchema``
  （``type:"file"`` 响应 schema）用窄 allowlist；**排除**参数/头对象本身、
  ``example``/``examples`` 载荷、corrections、``$ref`` 重写（归 ``convert_ref``）。
- **schema 节点 copy-on-write**（重建，绝不原地改写原始 schema 节点）；容器层
  （``doc["definitions"]``/``parameters``/``responses``、``paths.*`` 的 op/
  path-item 容器）就地替换为归一后的新容器——与 ``convert_api`` 既有的
  raw-mutation 姿态一致（本模块不改写 schema 节点值，故 ``fix_schema_type``
  改写 example 字面数据的腐蚀类 bug 不再存在）。
- 对畸形 schema 值容错（不抛异常）；幂等。

顺序约束：`convert_api` 的**终端 stage**（``convert_ref`` 之后、``correct_doc``
之前），替换 ``clean_schema`` + ``fix_schema_type``。
"""

from __future__ import annotations

import json
from typing import Any

# 官方 Swagger 2.0 meta-schema（/tmp/swagger2_schema.json）
# definitions.{schema,primitivesItems,fileSchema}.properties，加 ^x- 扩展放行。
_SCHEMA_KEYS = frozenset({
    "$ref", "additionalProperties", "allOf", "default", "description",
    "discriminator", "enum", "example", "exclusiveMaximum", "exclusiveMinimum",
    "externalDocs", "format", "items", "maxItems", "maxLength", "maxProperties",
    "maximum", "minItems", "minLength", "minProperties", "minimum", "multipleOf",
    "pattern", "properties", "readOnly", "required", "title", "type",
    "uniqueItems", "xml",
})

_PRIMITIVES_ITEMS_KEYS = frozenset({
    "collectionFormat", "default", "enum", "exclusiveMaximum", "exclusiveMinimum",
    "format", "items", "maxItems", "maxLength", "maximum", "minItems", "minLength",
    "minimum", "multipleOf", "pattern", "type", "uniqueItems",
})

_FILE_SCHEMA_KEYS = frozenset({
    "default", "description", "example", "externalDocs", "format", "readOnly",
    "required", "title", "type",
})

_ALLOWED_BY_POSITION = {
    "schema": _SCHEMA_KEYS,
    "primitives": _PRIMITIVES_ITEMS_KEYS,
    "file": _FILE_SCHEMA_KEYS,
}

# 非标准 type 值 → 标准（历史 clean_schema/fix_schema_type 同表，逐字节兼容）。
_TYPE_MAP = {
    "long": "integer",
    "int": "integer",
    "float": "number",
    "double": "number",
    "decimal": "number",
    "String": "string",
    "Boolean": "boolean",
    "Integer": "integer",
    "Number": "number",
    "Array": "array",
    "Object": "object",
    "text": "string",
    "1": "string",
    "0": "string",
    "A": "string",
    "": "string",
    "Bigint": "integer",
    "container": "object",
    "xml": "object",
}

_EXT = "x-"
_DROP = object()


def _map_type(value: Any) -> Any:
    if isinstance(value, str) and value in _TYPE_MAP:
        return _TYPE_MAP[value]
    return value


def _dedup_enum(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for v in values:
        try:
            key = json.dumps(v, sort_keys=True)
        except (TypeError, ValueError):
            key = repr(v)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _norm_items(value: Any, position: str) -> Any:
    child_position = "primitives" if position == "primitives" else "schema"
    if isinstance(value, dict):
        return _norm_schema(value, child_position)
    if isinstance(value, list):
        members = [_norm_schema(x, child_position) for x in value if isinstance(x, dict)]
        return members if members else _DROP
    return _DROP


def _norm_schema(node: Any, position: str = "schema") -> Any:
    """归一单个 schema 节点（copy-on-write，返回新 dict）。"""
    if not isinstance(node, dict):
        return node
    allowed = _ALLOWED_BY_POSITION[position]
    existing_ext = {k for k in node if isinstance(k, str) and k.startswith(_EXT)}
    out: dict[str, Any] = {}
    for k, v in node.items():
        if not isinstance(k, str):
            continue
        if k == "discriminator":
            if position == "schema" and isinstance(v, str):
                out[k] = v
            elif _EXT + k not in existing_ext:
                out[_EXT + k] = v
        elif k in allowed:
            coerced = _coerce(k, v, position)
            if coerced is not _DROP:
                out[k] = coerced
        elif k.startswith(_EXT):
            out[k] = v
        else:
            # total 默认：无损保留为非 2.0 扩展（键级 gate 0 结构性成立）。
            # 已存在同名显式 x- 键时显式优先（确定性，不覆盖）。
            ext = _EXT + k
            if ext not in existing_ext:
                out[ext] = v
    # xml.name → x-xml-root（OBS 根元素契约；与 xml 并存，setdefault 幂等）
    xml = out.get("xml")
    if isinstance(xml, dict) and isinstance(xml.get("name"), str) and xml["name"]:
        out.setdefault("x-xml-root", xml["name"])
    return out


def _coerce(key: str, value: Any, position: str) -> Any:
    if key == "type":
        return _map_type(value)
    if key == "required":
        return value if isinstance(value, list) and value else _DROP
    if key == "enum":
        return _dedup_enum(value) if isinstance(value, list) and value else _DROP
    if key == "properties":
        if not isinstance(value, dict):
            return _DROP
        props: dict[str, Any] = {}
        for name, pval in value.items():
            if isinstance(pval, dict):
                props[name] = _norm_schema(pval, "schema")
        return props
    if key == "items":
        return _norm_items(value, position)
    if key == "additionalProperties":
        if isinstance(value, dict):
            return _norm_schema(value, "schema")
        if isinstance(value, bool):
            return value
        return _DROP
    if key == "allOf":
        if not isinstance(value, list):
            return _DROP
        members = [_norm_schema(m, "schema") for m in value if isinstance(m, dict)]
        return members if members else _DROP
    if key == "xml":
        if not isinstance(value, dict):
            return _DROP
        return dict(value)
    return value


def _norm_param(param: Any) -> Any:
    """参数对象：仅 body 的 schema 归一；其余仅 type/items 归一（不 prune）。

    参数/头**对象**的键裁剪归 ``oas2_parameter``/``clean_header``；本模块只
    负责其中 schema 区域的形状与 type 归一。
    """
    if not isinstance(param, dict):
        return param
    p = dict(param)
    if p.get("in") == "body":
        if isinstance(p.get("schema"), dict):
            p["schema"] = _norm_schema(p["schema"], "schema")
    else:
        if "type" in p:
            p["type"] = _map_type(p["type"])
        if isinstance(p.get("items"), (dict, list)):
            items = _norm_items(p["items"], "primitives")
            if items is not _DROP:
                p["items"] = items
            else:
                p.pop("items", None)
    return p


def _norm_param_list(params: Any) -> Any:
    if isinstance(params, list):
        return [_norm_param(p) for p in params]
    return params


def _norm_param_container(container: Any) -> Any:
    if isinstance(container, dict):
        return {name: _norm_param(p) for name, p in container.items()}
    if isinstance(container, list):
        return [_norm_param(p) for p in container]
    return container


def _norm_header(header: Any) -> Any:
    if not isinstance(header, dict):
        return header
    h = dict(header)
    if "type" in h:
        h["type"] = _map_type(h["type"])
    if isinstance(h.get("items"), (dict, list)):
        items = _norm_items(h["items"], "primitives")
        if items is not _DROP:
            h["items"] = items
        else:
            h.pop("items", None)
    return h


def _norm_response(resp: Any) -> Any:
    if not isinstance(resp, dict):
        return resp
    r = dict(resp)
    schema = r.get("schema")
    if isinstance(schema, dict):
        position = "file" if schema.get("type") == "file" else "schema"
        r["schema"] = _norm_schema(schema, position)
    headers = r.get("headers")
    if isinstance(headers, dict):
        r["headers"] = {name: _norm_header(h) for name, h in headers.items()}
    return r


def _norm_response_container(container: Any) -> Any:
    if isinstance(container, dict):
        return {code: _norm_response(resp) for code, resp in container.items()}
    return container


def normalize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """归一 ``doc`` 内全部 schema 区域（返回同一 doc 容器）。

    Invariants:
      - 输出的 schema 节点**键** ⊆ 对应位置 allowlist ∪ ``x-``（键级 gate 0
        结构性成立；值级畸形（如空 enum）按 meta-schema 收口处理）。
      - 幂等；对畸形 schema 值容错（不抛异常）。
      - 不改写 ``example``/``examples`` 载荷，不重写 ``$ref``。
      - schema 节点重建（copy-on-write）；容器层就地替换。
    """
    defs = doc.get("definitions")
    if isinstance(defs, dict):
        doc["definitions"] = {
            name: _norm_schema(node, "schema") if isinstance(node, dict) else node
            for name, node in defs.items()
        }

    if "parameters" in doc:
        doc["parameters"] = _norm_param_container(doc["parameters"])
    if "responses" in doc:
        doc["responses"] = _norm_response_container(doc["responses"])

    paths = doc.get("paths")
    if isinstance(paths, dict):
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            if "parameters" in path_item:
                path_item["parameters"] = _norm_param_list(path_item["parameters"])
            for method, op in path_item.items():
                if not isinstance(op, dict):
                    continue
                if "parameters" in op:
                    op["parameters"] = _norm_param_list(op["parameters"])
                if "responses" in op:
                    op["responses"] = _norm_response_container(op["responses"])
    return doc
