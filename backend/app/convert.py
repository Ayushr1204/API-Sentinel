"""Bring every supported spec format into one internal model before diffing.

Supported inputs: Swagger 2.0, OpenAPI 3.0.x, 3.1.x and 3.2.x, in any pair and in either direction.

* Swagger 2.0 is translated to OpenAPI 3.0 (`swagger2_to_oas3`): host/basePath/schemes become `servers`,
  `definitions` become `components/schemas`, body and formData parameters become `requestBody`,
  response `schema` + `produces` become `content`, `securityDefinitions` become `securitySchemes`.
* Every document is then normalised to OpenAPI 3.1 semantics (`canonicalize`): 3.0 `nullable` becomes a JSON
  Schema type array, boolean `exclusiveMinimum/Maximum` become numeric bounds, and 3.2 `additionalOperations`
  are folded into the path item. The original version is kept in `x-sentinel-source`.

The aim is that two documents describing the same API in different formats compare as equal, so only real
contract changes are reported. Conversions are pure functions: the input document is never modified.
"""
from __future__ import annotations

import copy
import re
from typing import Any

SOURCE_KEY = "x-sentinel-source"
EXTRA_METHODS_KEY = "x-sentinel-methods"
_PARAM_SCHEMA_KEYS = ("type", "format", "items", "enum", "default", "maximum", "exclusiveMaximum", "minimum",
                      "exclusiveMinimum", "maxLength", "minLength", "pattern", "maxItems", "minItems",
                      "uniqueItems", "multipleOf")
_REF_MAP = (("#/definitions/", "#/components/schemas/"), ("#/responses/", "#/components/responses/"))


def spec_family(doc: dict) -> tuple[str, str]:
    """('swagger', '2.0') or ('openapi', '3.1.0'); ('', '') when neither field is present."""
    if "swagger" in doc:
        return "swagger", str(doc.get("swagger"))
    if "openapi" in doc:
        return "openapi", str(doc.get("openapi"))
    return "", ""


def format_label(family: str, version: str) -> str:
    return f"Swagger {version}" if family == "swagger" else f"OpenAPI {version}"


# ---------------------------------------------------------------- Swagger 2.0 -> OpenAPI 3.0
def _rewrite_refs(node: Any, param_targets: dict[str, str]) -> Any:
    """Rewrite Swagger $refs to their OpenAPI 3 locations (recursively, returning a new structure)."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                if v.startswith("#/parameters/"):
                    v = param_targets.get(v, v.replace("#/parameters/", "#/components/parameters/"))
                for old, new in _REF_MAP:
                    if v.startswith(old):
                        v = new + v[len(old):]
                out[k] = v
            else:
                out[k] = _rewrite_refs(v, param_targets)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(v, param_targets) for v in node]
    return node


def _schema2(schema: Any) -> Any:
    """Swagger schema object -> OpenAPI 3.0 schema object."""
    if isinstance(schema, list):
        return [_schema2(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "x-nullable":
            out["nullable"] = bool(v)
        elif k == "discriminator" and isinstance(v, str):
            out["discriminator"] = {"propertyName": v}
        elif k in ("properties", "patternProperties", "definitions") and isinstance(v, dict):
            out[k] = {name: _schema2(s) for name, s in v.items()}
        elif k in ("items", "additionalProperties", "not") or k in ("allOf", "anyOf", "oneOf"):
            out[k] = _schema2(v)
        else:
            out[k] = v
    if out.get("type") == "file":
        out["type"], out["format"] = "string", "binary"
    return out


def _param_schema(p: dict) -> dict:
    schema = {k: p[k] for k in _PARAM_SCHEMA_KEYS if k in p}
    if "items" in schema:
        schema["items"] = _schema2(schema["items"])
    if p.get("x-nullable"):
        schema["nullable"] = True
    return _schema2(schema)


def _style(p: dict) -> dict:
    fmt = p.get("collectionFormat")
    if p.get("type") != "array" or fmt is None:
        return {}
    if fmt == "multi":
        return {"style": "form", "explode": True}
    if fmt == "ssv":
        return {"style": "spaceDelimited", "explode": False}
    if fmt == "pipes":
        return {"style": "pipeDelimited", "explode": False}
    if fmt == "tsv":  # no OpenAPI 3 equivalent; keep the closest non-exploded form
        return {"style": "form", "explode": False, "x-collectionFormat": "tsv"}
    return {"style": "simple" if p.get("in") in ("path", "header") else "form", "explode": False}


def _param3(p: dict) -> dict:
    out = {k: p[k] for k in ("name", "in", "description", "required", "deprecated", "allowEmptyValue") if k in p}
    out.update(_style(p))
    out["schema"] = _param_schema(p)
    out.update({k: v for k, v in p.items() if k.startswith("x-") and k != "x-nullable"})
    return out


def _resolve_local(doc: dict, node: Any) -> Any:
    seen = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/"):
        ref = node["$ref"]
        if ref in seen:
            break
        seen.add(ref)
        target: Any = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
        if target is None:
            break
        node = target
    return node


def _form_body(params: list[dict], consumes: list[str]) -> dict:
    props, required = {}, []
    has_file = False
    for p in params:
        schema = _param_schema(p)
        has_file = has_file or p.get("type") == "file"
        if p.get("description"):
            schema["description"] = p["description"]
        props[p["name"]] = schema
        if p.get("required"):
            required.append(p["name"])
    media = [m for m in consumes if m in ("multipart/form-data", "application/x-www-form-urlencoded")]
    if not media:
        media = ["multipart/form-data" if has_file else "application/x-www-form-urlencoded"]
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {"required": bool(required), "content": {m: {"schema": copy.deepcopy(schema)} for m in media}}


def _response3(resp: dict, produces: list[str]) -> dict:
    if "$ref" in resp:
        return {"$ref": resp["$ref"]}
    out: dict[str, Any] = {"description": resp.get("description", "")}
    if "schema" in resp:
        out["content"] = {m: {"schema": _schema2(copy.deepcopy(resp["schema"]))} for m in produces}
    if resp.get("headers"):
        out["headers"] = {name: {k: v for k, v in {"description": h.get("description"),
                                                    "schema": _param_schema(h)}.items() if v is not None}
                          for name, h in resp["headers"].items()}
    out.update({k: v for k, v in resp.items() if k.startswith("x-")})
    return out


def _security_scheme(s: dict) -> dict:
    t = s.get("type")
    base = {k: s[k] for k in ("description",) if k in s}
    if t == "basic":
        return {**base, "type": "http", "scheme": "basic"}
    if t == "apiKey":
        return {**base, "type": "apiKey", "name": s.get("name"), "in": s.get("in")}
    if t == "oauth2":
        flow_name = {"implicit": "implicit", "password": "password", "application": "clientCredentials",
                     "accessCode": "authorizationCode"}.get(s.get("flow"), "implicit")
        flow = {"scopes": s.get("scopes", {})}
        if "authorizationUrl" in s:
            flow["authorizationUrl"] = s["authorizationUrl"]
        if "tokenUrl" in s:
            flow["tokenUrl"] = s["tokenUrl"]
        return {**base, "type": "oauth2", "flows": {flow_name: flow}}
    return {**base, **s}


def swagger2_to_oas3(src: dict) -> dict:
    """Translate a (validated) Swagger 2.0 document into an equivalent OpenAPI 3.0.3 document."""
    doc = copy.deepcopy(src)
    g_consumes = doc.get("consumes") or ["application/json"]
    g_produces = doc.get("produces") or ["application/json"]

    # global parameters: body ones become requestBodies, formData ones are inlined where used
    param_targets: dict[str, str] = {}
    comp_params, comp_bodies = {}, {}
    for name, p in (doc.get("parameters") or {}).items():
        if p.get("in") == "body":
            param_targets[f"#/parameters/{name}"] = f"#/components/requestBodies/{name}"
            comp_bodies[name] = {"required": bool(p.get("required")), "description": p.get("description", ""),
                                 "content": {m: {"schema": _schema2(p.get("schema", {}))} for m in g_consumes}}
        elif p.get("in") != "formData":
            comp_params[name] = _param3(p)

    out: dict[str, Any] = {"openapi": "3.0.3", "info": doc.get("info", {})}
    host, base_path = doc.get("host"), doc.get("basePath", "") or ""
    if host:
        out["servers"] = [{"url": f"{scheme}://{host}{base_path}"} for scheme in (doc.get("schemes") or ["https"])]
    elif base_path:
        out["servers"] = [{"url": base_path}]
    for key in ("tags", "externalDocs", "security"):
        if key in doc:
            out[key] = doc[key]

    paths: dict[str, Any] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        new_item: dict[str, Any] = {k: v for k, v in item.items() if k.startswith("x-")}
        path_params = [_resolve_local(doc, p) for p in item.get("parameters", []) or []]
        for method in ("get", "put", "post", "delete", "options", "head", "patch"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            consumes = op.get("consumes") or g_consumes
            produces = op.get("produces") or g_produces
            new_op = {k: v for k, v in op.items()
                      if k not in ("parameters", "responses", "consumes", "produces", "schemes")}
            merged: dict[tuple[str, str], Any] = {}
            for raw in path_params + list(op.get("parameters", []) or []):
                resolved = _resolve_local(doc, raw)
                key = (resolved.get("in", ""), resolved.get("name", ""))
                # keep body/global refs as refs when the target is a reusable component
                merged[key] = raw if isinstance(raw, dict) and "$ref" in raw and resolved.get("in") != "formData" \
                    else resolved
            params, form = [], []
            for key, p in merged.items():
                resolved = _resolve_local(doc, p)
                if resolved.get("in") == "formData":
                    form.append(resolved)
                elif resolved.get("in") == "body":
                    if "$ref" in p:
                        new_op["requestBody"] = {"$ref": p["$ref"]}
                    else:
                        body = {"required": bool(p.get("required")),
                                "content": {m: {"schema": _schema2(copy.deepcopy(p.get("schema", {})))}
                                            for m in consumes}}
                        if p.get("description"):
                            body["description"] = p["description"]
                        new_op["requestBody"] = body
                elif "$ref" in p:
                    params.append({"$ref": p["$ref"]})
                else:
                    params.append(_param3(p))
            if form:
                new_op["requestBody"] = _form_body(form, consumes)
            if params:
                new_op["parameters"] = params
            new_op["responses"] = {str(code): _response3(r, produces)
                                   for code, r in (op.get("responses") or {}).items()}
            new_item[method] = new_op
        paths[path] = new_item
    out["paths"] = paths

    components: dict[str, Any] = {}
    if doc.get("definitions"):
        components["schemas"] = {n: _schema2(s) for n, s in doc["definitions"].items()}
    if comp_params:
        components["parameters"] = comp_params
    if comp_bodies:
        components["requestBodies"] = comp_bodies
    if doc.get("responses"):
        components["responses"] = {n: _response3(r, g_produces) for n, r in doc["responses"].items()}
    if doc.get("securityDefinitions"):
        components["securitySchemes"] = {n: _security_scheme(s) for n, s in doc["securityDefinitions"].items()}
    if components:
        out["components"] = components
    out.update({k: v for k, v in doc.items() if k.startswith("x-")})
    return _rewrite_refs(out, param_targets)


# ---------------------------------------------------------------- any OpenAPI 3.x -> 3.1 semantics
_SCHEMA_CHILD_MAPS = ("properties", "patternProperties", "$defs", "dependentSchemas")
_SCHEMA_CHILD_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_CHILD_ONE = ("items", "additionalProperties", "not", "if", "then", "else", "contains",
                     "propertyNames", "unevaluatedItems", "unevaluatedProperties", "additionalItems")


def _schema31(schema: Any, from30: bool) -> Any:
    if isinstance(schema, list):
        return [_schema31(s, from30) for s in schema]
    if not isinstance(schema, dict):
        return schema
    s = dict(schema)
    for key in _SCHEMA_CHILD_MAPS:
        if isinstance(s.get(key), dict):
            s[key] = {k: _schema31(v, from30) for k, v in s[key].items()}
    for key in _SCHEMA_CHILD_LISTS:
        if isinstance(s.get(key), list):
            s[key] = [_schema31(v, from30) for v in s[key]]
    for key in _SCHEMA_CHILD_ONE:
        if isinstance(s.get(key), (dict, list)):
            s[key] = _schema31(s[key], from30)
    if from30:
        if s.get("nullable") is True and isinstance(s.get("type"), str):
            s["type"] = [s["type"], "null"]
            if isinstance(s.get("enum"), list) and None not in s["enum"]:
                s["enum"] = [*s["enum"], None]
            s.pop("nullable")
        elif s.get("nullable") is False:
            s.pop("nullable")
        # (nullable without a type is kept: schema_types() still honours it)
        for bound, excl in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
            if s.get(excl) is True and bound in s:
                s[excl] = s.pop(bound)
            elif s.get(excl) is False:
                s.pop(excl)
    return s


def _media31(media: Any, from30: bool) -> Any:
    if not isinstance(media, dict):
        return media
    out = dict(media)
    for mt, obj in media.items():
        if isinstance(obj, dict):
            obj = dict(obj)
            for key in ("schema", "itemSchema"):
                if key in obj:
                    obj[key] = _schema31(obj[key], from30)
            out[mt] = obj
    return out


def _with_schema(obj: Any, from30: bool) -> Any:
    """Parameter / header object: normalise its schema or content."""
    if not isinstance(obj, dict) or "$ref" in obj and len(obj) == 1:
        return obj
    obj = dict(obj)
    if "schema" in obj:
        obj["schema"] = _schema31(obj["schema"], from30)
    if "content" in obj:
        obj["content"] = _media31(obj["content"], from30)
    return obj


def _response31(resp: Any, from30: bool) -> Any:
    if not isinstance(resp, dict):
        return resp
    resp = dict(resp)
    if "content" in resp:
        resp["content"] = _media31(resp["content"], from30)
    if isinstance(resp.get("headers"), dict):
        resp["headers"] = {k: _with_schema(v, from30) for k, v in resp["headers"].items()}
    return resp


def _operation31(op: dict, from30: bool) -> dict:
    op = dict(op)
    if isinstance(op.get("parameters"), list):
        op["parameters"] = [_with_schema(p, from30) for p in op["parameters"]]
    if isinstance(op.get("requestBody"), dict) and "content" in op["requestBody"]:
        op["requestBody"] = {**op["requestBody"], "content": _media31(op["requestBody"]["content"], from30)}
    if isinstance(op.get("responses"), dict):
        op["responses"] = {code: _response31(r, from30) for code, r in op["responses"].items()}
    return op


_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query")


def canonicalize(doc: dict) -> dict:
    """Return a copy of a validated OpenAPI 3.x (or converted Swagger) document in OpenAPI 3.1 semantics."""
    family, version = spec_family(doc)
    source = doc.get(SOURCE_KEY) or {"format": family, "version": version, "label": format_label(family, version)}
    from30 = version.startswith("3.0")
    out = copy.deepcopy(doc)
    out["openapi"] = "3.1.0"
    out[SOURCE_KEY] = source
    paths = {}
    for path, item in (out.get("paths") or {}).items():
        if not isinstance(item, dict):
            paths[path] = item
            continue
        item = dict(item)
        extra = []
        for name, op in (item.pop("additionalOperations", None) or {}).items():  # OpenAPI 3.2
            key = str(name).lower()
            if key not in _METHODS and isinstance(op, dict):
                item[key] = op
                extra.append(key)
        if extra:
            item[EXTRA_METHODS_KEY] = sorted(extra)
        if isinstance(item.get("parameters"), list):
            item["parameters"] = [_with_schema(p, from30) for p in item["parameters"]]
        for m in (*_METHODS, *extra):
            if isinstance(item.get(m), dict):
                item[m] = _operation31(item[m], from30)
        paths[path] = item
    out["paths"] = paths
    comps = out.get("components")
    if isinstance(comps, dict):
        comps = dict(comps)
        if isinstance(comps.get("schemas"), dict):
            comps["schemas"] = {k: _schema31(v, from30) for k, v in comps["schemas"].items()}
        for key in ("parameters", "headers"):
            if isinstance(comps.get(key), dict):
                comps[key] = {k: _with_schema(v, from30) for k, v in comps[key].items()}
        if isinstance(comps.get("responses"), dict):
            comps["responses"] = {k: _response31(v, from30) for k, v in comps["responses"].items()}
        if isinstance(comps.get("requestBodies"), dict):
            comps["requestBodies"] = {k: ({**v, "content": _media31(v["content"], from30)}
                                          if isinstance(v, dict) and "content" in v else v)
                                      for k, v in comps["requestBodies"].items()}
        out["components"] = comps
    return out


def to_internal(doc: dict) -> dict:
    """Any supported, validated document -> the internal OpenAPI 3.1 model."""
    family, version = spec_family(doc)
    if family == "swagger":
        converted = swagger2_to_oas3(doc)
        converted[SOURCE_KEY] = {"format": "swagger", "version": version, "label": format_label(family, version)}
        return canonicalize(converted)
    return canonicalize(doc)


_VERSION_PARTS = re.compile(r"^(\d+)\.(\d+)")


def version_tuple(version: str) -> tuple[int, int]:
    m = _VERSION_PARTS.match(version or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
