"""API Explorer 原始接口详情 → OpenAPI 2.0 规范化转换。

独立实现，转换规则与参考项目保持一致（见 AGENTS.md 校验规则章节）。
"""

import json
import re
from typing import Any, cast

TYPE_MAP = {
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

PARAM_ALLOWED = {
    "name", "in", "description", "required", "type", "format", "items",
    "collectionFormat", "default", "maximum", "exclusiveMaximum", "minimum",
    "exclusiveMinimum", "maxLength", "minLength", "pattern", "maxItems",
    "minItems", "uniqueItems", "enum", "multipleOf", "schema",
}

HEADER_ALLOWED = {
    "type", "format", "description", "default", "maximum", "exclusiveMaximum",
    "minimum", "exclusiveMinimum", "maxLength", "minLength", "pattern",
    "maxItems", "minItems", "uniqueItems", "enum", "multipleOf", "items",
    "collectionFormat", "required",
}

SCHEMA_KEYS = {
    "type", "format", "description", "default", "maximum", "exclusiveMaximum",
    "minimum", "exclusiveMinimum", "maxLength", "minLength", "pattern",
    "maxItems", "minItems", "uniqueItems", "enum", "multipleOf", "items",
    "properties", "required", "additionalProperties", "$ref",
}


def fix_schema_type(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    if "type" in schema and isinstance(schema["type"], str) and schema["type"] in TYPE_MAP:
        schema["type"] = TYPE_MAP[schema["type"]]
    for v in schema.values():
        if isinstance(v, dict):
            fix_schema_type(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    fix_schema_type(item)
    return schema


def remove_bool_required(obj: Any) -> Any:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "required" and isinstance(v, bool):
                del obj[k]
            else:
                remove_bool_required(v)
    elif isinstance(obj, list):
        for item in obj:
            remove_bool_required(item)
    return obj


def convert_ref(doc: dict[str, Any], obj: Any) -> Any:
    if isinstance(obj, dict):
        if "$ref" in obj and isinstance(obj["$ref"], str):
            ref = obj["$ref"]
            ref = re.sub(r"#/components/schemas/", "#/definitions/", ref)
            ref = re.sub(r"#/components/parameters/", "#/parameters/", ref)
            ref = re.sub(r"#/components/responses/", "#/responses/", ref)
            ref = re.sub(r"#/components/headers/", "#/headers/", ref)
            ref = re.sub(r"#/components/examples/", "#/examples/", ref)
            obj["$ref"] = ref
        for v in obj.values():
            convert_ref(doc, v)
    elif isinstance(obj, list):
        for item in obj:
            convert_ref(doc, item)
    return obj


def oas2_parameter(param: Any) -> Any:
    if not isinstance(param, dict) or "in" not in param:
        return param
    p = dict(param)
    if p.get("in") == "body":
        if "schema" not in p:
            schema = {"type": p.pop("type", "string")}
            if "format" in p:
                schema["format"] = p.pop("format")
            p["schema"] = schema
    elif "schema" in p:
        p["type"] = p["schema"].get("type", "string")
        fmt = p["schema"].get("format")
        if fmt is not None:
            p["format"] = fmt
        elif "format" in p:
            p.pop("format", None)
        for f in ("schema", "content"):
            p.pop(f, None)
    if p.get("in") == "path":
        p["required"] = True
    if p.get("in") in ("query", "header") and p.get("type") == "object":
        p["type"] = "string"
    return {k: v for k, v in p.items() if k in PARAM_ALLOWED or k.startswith("x-")}


def convert_3_to_2(api: dict[str, Any]) -> dict[str, Any]:
    doc = {k: v for k, v in api.items() if k in (
        "swagger", "openapi", "info", "host", "basePath", "schemes", "consumes",
        "produces", "paths", "definitions", "parameters", "responses",
        "securityDefinitions", "security", "tags", "externalDocs", "version")}
    doc["swagger"] = "2.0"
    doc.pop("openapi", None)

    comps = api.get("components") or {}
    defs = dict(api.get("definitions") or {})
    if isinstance(comps.get("schemas"), dict):
        defs.update(comps["schemas"])
    doc["definitions"] = defs

    params = dict(api.get("parameters") or {})
    if isinstance(comps.get("parameters"), dict):
        params.update(comps["parameters"])
    doc["parameters"] = params

    responses = dict(api.get("responses") or {})
    if isinstance(comps.get("responses"), dict):
        responses.update(comps["responses"])
    doc["responses"] = responses

    doc["headers"] = dict(comps.get("headers") or {})

    paths = api.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            params_list = [oas2_parameter(p) for p in (op.get("parameters") or [])]
            if "requestBody" in op:
                rb = op.pop("requestBody")
                content = rb.get("content") or {}
                media = content.get("application/json") or next(iter(content.values()), None)
                schema = media.get("schema") if media else None
                params_list.append({
                    "in": "body",
                    "name": f"{op.get('operationId', 'Body')}RequestBody",
                    "required": bool(rb.get("required", False)),
                    "schema": schema or {"type": "object"},
                })
            op["parameters"] = params_list

            responses2 = {}
            for code, resp in (op.get("responses") or {}).items():
                if not isinstance(resp, dict):
                    continue
                r2 = {"description": resp.get("description", "")}
                content = resp.get("content") or {}
                media = content.get("application/json") or next(iter(content.values()), None)
                if media:
                    schema = media.get("schema")
                    if schema:
                        r2["schema"] = schema
                    if media.get("example") is not None:
                        r2["examples"] = {"application/json": media["example"]}
                if isinstance(resp.get("headers"), dict):
                    r2["headers"] = resp["headers"]
                responses2[code] = r2
            op["responses"] = responses2

    doc["paths"] = paths
    return doc


def fix_2_doc(api: dict[str, Any]) -> dict[str, Any]:
    doc = {k: v for k, v in api.items() if k in (
        "swagger", "info", "host", "basePath", "schemes", "consumes",
        "produces", "paths", "definitions", "parameters", "responses",
        "securityDefinitions", "security", "tags", "externalDocs")}
    doc["swagger"] = "2.0"
    doc["info"] = {
        "title": f"{api.get('product_short', '')} API",
        "version": api.get("info_version") or api.get("version") or "1.0",
    }
    if api.get("host"):
        doc["host"] = api["host"]
    doc["basePath"] = api.get("base_path") or "/"

    schemes = api.get("schemes") or ["HTTPS"]
    doc["schemes"] = [s.lower() if isinstance(s, str) else "https" for s in schemes]

    consumes = api.get("consumes")
    if isinstance(consumes, str):
        try:
            parsed = json.loads(consumes)
            if not isinstance(parsed, list):
                parsed = [consumes]
        except Exception:
            parsed = [consumes]
        consumes = parsed
    if not consumes:
        consumes = ["application/json"]
    doc["consumes"] = consumes
    doc["produces"] = api.get("produces") or ["application/json"]

    doc["definitions"] = dict(api.get("definitions") or {})
    doc["parameters"] = dict(api.get("parameters") or {})
    doc["responses"] = dict(api.get("responses") or {})
    if api.get("security_definitions"):
        doc["securityDefinitions"] = api["security_definitions"]
    if api.get("security"):
        doc["security"] = api["security"]
    return doc


def clean_header(h: Any) -> Any:
    if not isinstance(h, dict):
        return h
    if "schema" in h:
        nh = {"type": h["schema"].get("type", "string")}
        for f in ("format", "maxLength", "minLength", "pattern", "enum", "items", "default"):
            if f in h["schema"]:
                nh[f] = h["schema"][f]
        for k, v in h.items():
            if k != "schema":
                nh[k] = v
        h = nh
    if "type" not in h:
        h = dict(h)
        h["type"] = "string"
    return {k: v for k, v in h.items() if k in HEADER_ALLOWED or k.startswith("x-")}


def clean_response(resp: Any, header_defs: dict[str, Any] | None = None) -> Any:
    if not isinstance(resp, dict):
        return resp
    r = {k: v for k, v in resp.items()
         if k in ("description", "schema", "headers", "examples", "default") or k.startswith("x-")}
    if not isinstance(r.get("description"), str) or not r["description"]:
        r["description"] = "Response"
    content = resp.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), None) if content else None
    if media and isinstance(media, dict):
        if media.get("schema") and "schema" not in r:
            r["schema"] = media["schema"]
        if media.get("example") is not None and "examples" not in r:
            r["examples"] = {"application/json": media["example"]}
    if isinstance(r.get("headers"), dict):
        cleaned = {}
        for name, h in r["headers"].items():
            if isinstance(h, dict) and isinstance(h.get("$ref"), str) and h["$ref"].startswith("#/headers/"):
                hname = h["$ref"].split("/")[-1]
                h = (header_defs or {}).get(hname, {"description": ""})
            cleaned[name] = clean_header(h)
        r["headers"] = cleaned
    schema = r.get("schema")
    if isinstance(schema, dict) and "examples" in schema:
        if "examples" not in r:
            r["examples"] = schema["examples"]
        del schema["examples"]
    return r


def clean_schema(obj: Any) -> Any:
    if isinstance(obj, dict):
        for k in ("nullable", "deprecated", "oneOf", "discriminator", "xml", "example", "externalDocs",
                  "writeOnly", "linkage_node_fields", "allOf"):
            obj.pop(k, None)
        if "type" in obj and isinstance(obj["type"], str) and obj["type"] in TYPE_MAP:
            obj["type"] = TYPE_MAP[obj["type"]]
        if isinstance(obj.get("required"), bool):
            del obj["required"]
        if isinstance(obj.get("enum"), list):
            seen = set()
            uniq = []
            for v in obj["enum"]:
                key = json.dumps(v, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    uniq.append(v)
            obj["enum"] = uniq
        props = obj.get("properties")
        if props is None:
            obj.pop("properties", None)
        if isinstance(props, dict):
            for pname, pval in list(props.items()):
                if not isinstance(pval, dict):
                    props.pop(pname, None)
                    continue
                for k in list(pval.keys()):
                    if k not in SCHEMA_KEYS and not k.startswith("x-"):
                        pval.pop(k, None)
                if pval.get("properties") is None and "$ref" not in pval and "type" not in pval:
                    props.pop(pname, None)
                    continue
                clean_schema(pval)
        items = obj.get("items")
        if isinstance(items, dict):
            clean_schema(items)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                clean_schema(item)
    return obj


def finalize(doc: dict[str, Any]) -> dict[str, Any]:
    doc["swagger"] = "2.0"
    if "info" not in doc:
        doc["info"] = {"title": doc.get("host", "API"), "version": "1.0"}
    doc["basePath"] = doc.get("basePath") or "/"
    if not doc.get("paths"):
        doc["paths"] = {}
    doc["definitions"] = dict(doc.get("definitions") or {})
    doc["parameters"] = dict(doc.get("parameters") or {})
    doc["responses"] = dict(doc.get("responses") or {})
    if doc.get("schemes"):
        doc["schemes"] = [s.lower() for s in doc["schemes"] if isinstance(s, str)]
    if isinstance(doc.get("consumes"), str):
        doc["consumes"] = [doc["consumes"]]
    if isinstance(doc.get("tags"), str):
        doc["tags"] = [{"name": doc["tags"]}]
    elif isinstance(doc.get("tags"), list):
        doc["tags"] = [
            {"name": t} if isinstance(t, str) else t
            for t in doc["tags"] if isinstance(t, (str, dict))
        ]
    doc.pop("version", None)
    doc.pop("openapi", None)
    for name, p in list(doc.get("parameters", {}).items()):
        if isinstance(p, dict):
            doc["parameters"][name] = oas2_parameter(p)
    header_defs = doc.get("headers") or {}
    for name, r in list(doc.get("responses", {}).items()):
        doc["responses"][name] = clean_response(r, header_defs)
    for schema in doc.get("definitions", {}).values():
        clean_schema(schema)
    for path, path_item in (doc.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            params = op.get("parameters")
            if isinstance(params, list):
                cleaned_params = []
                seen = set()
                for p in params:
                    pc = oas2_parameter(p)
                    key = f"{pc.get('name')}|{pc.get('in')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned_params.append(pc)
                op["parameters"] = cleaned_params
            for code, resp in (op.get("responses") or {}).items():
                op["responses"][code] = clean_response(resp, header_defs)
    doc.pop("headers", None)
    return doc


def convert_api(api: dict[str, Any]) -> dict[str, Any]:
    has3 = bool(api.get("components")) or any(
        (m.get("requestBody") is not None) or any(
            isinstance(c, dict) and c.get("content")
            for c in (m.get("responses") or {}).values())
        for p in (api.get("paths") or {}).values()
        for m in p.values() if isinstance(m, dict))

    if has3:
        doc = convert_3_to_2(api)
    else:
        doc = fix_2_doc(api)

    doc = finalize(doc)
    doc = convert_ref(doc, doc)
    doc = fix_schema_type(doc)
    return cast(dict[str, Any], doc)


def main() -> None:
    import os
    import shutil

    from . import region_paths

    src = region_paths.by_tag_dir()
    out = region_paths.openapi2_dir()
    shutil.rmtree(out, ignore_errors=True)

    total = 0
    stats = {"total": 0, "converted_3": 0, "converted_2": 0}
    for ps_dir in sorted(os.listdir(src)):
        pdir = os.path.join(src, ps_dir)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                data = json.load(f)
            apis = data.get("apis", {})
            converted = {}
            for key, api in apis.items():
                doc = convert_api(api)
                converted[key] = doc
                stats["total"] += 1
                if api.get("components"):
                    stats["converted_3"] += 1
                else:
                    stats["converted_2"] += 1
            total += len(converted)
            out_dir = os.path.join(out, ps_dir)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
                json.dump({"product_short": data.get("product_short"), "tag": data.get("tag"),
                           "api_count": len(converted), "apis": converted},
                          f, ensure_ascii=False, indent=2)
    print("total:", total, stats)


if __name__ == "__main__":
    main()
