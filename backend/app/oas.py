"""OpenAPI document helpers: local $ref resolution, operation iteration, type normalisation."""
from __future__ import annotations

import copy
import re
from typing import Any, Iterator

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_PARAM_RE = re.compile(r"\{[^}/]+\}")


class RefError(ValueError):
    pass


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_pointer(doc: dict, ref: str) -> Any:
    """Resolve a local JSON pointer such as '#/components/schemas/Pet'."""
    if not ref.startswith("#"):
        raise RefError(f"Only local $ref values are supported (got '{ref}')")
    node: Any = doc
    for raw in ref[1:].split("/")[1:] if ref != "#" else []:
        token = unescape(raw)
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            raise RefError(f"$ref '{ref}' does not resolve")
    return node


def iter_refs(node: Any, pointer: str = "#") -> Iterator[tuple[str, str]]:
    """Yield (location, ref) for every $ref in the document."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield pointer, ref
        for k, v in node.items():
            yield from iter_refs(v, f"{pointer}/{str(k).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_refs(v, f"{pointer}/{i}")


def deref(doc: dict, node: Any) -> tuple[Any, str | None]:
    """Follow $ref chains on a single node. Returns (resolved_node, last_ref_name)."""
    name = None
    seen = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if ref in seen:
            raise RefError(f"Circular $ref chain at '{ref}'")
        seen.add(ref)
        name = ref.rsplit("/", 1)[-1]
        resolved = resolve_pointer(doc, ref)
        # OAS 3.1 allows siblings next to $ref; merge them over the target.
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        node = {**resolved, **siblings} if siblings and isinstance(resolved, dict) else resolved
    return node, name


def inline_schema(doc: dict, schema: Any, max_depth: int = 12, _depth: int = 0) -> Any:
    """Fully inline $refs for validation. Recursive schemas are cut off at max_depth (accept-anything)."""
    if _depth > max_depth:
        return {}
    if isinstance(schema, dict):
        if "$ref" in schema:
            schema, _ = deref(doc, schema)
            return inline_schema(doc, schema, max_depth, _depth + 1)
        return {k: inline_schema(doc, v, max_depth, _depth + (1 if k in ("properties", "items") else 0))
                for k, v in schema.items()}
    if isinstance(schema, list):
        return [inline_schema(doc, v, max_depth, _depth) for v in schema]
    return copy.deepcopy(schema)


def normalize_path(path: str) -> str:
    """'/pets/{petId}' -> '/pets/{}' so renamed path params still match."""
    return _PARAM_RE.sub("{}", path)


def param_key(param: dict) -> tuple[str, str]:
    loc = param.get("in", "")
    name = param.get("name", "")
    return (loc, name.lower() if loc == "header" else name)


def operations(doc: dict) -> Iterator[tuple[str, str, dict, list[dict]]]:
    """Yield (path, method, operation, effective_parameters) with path-level params merged in."""
    for path, item in (doc.get("paths") or {}).items():
        item, _ = deref(doc, item)
        if not isinstance(item, dict):
            continue
        path_params = [deref(doc, p)[0] for p in item.get("parameters", []) or []]
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            merged: dict[tuple[str, str], dict] = {param_key(p): p for p in path_params}
            for p in op.get("parameters", []) or []:
                p, _ = deref(doc, p)
                merged[param_key(p)] = p
            yield path, method, op, list(merged.values())


def schema_types(schema: dict, oas31: bool) -> set[str] | None:
    """Normalised set of JSON types, folding OAS 3.0 'nullable' into 'null'. None = unconstrained."""
    t = schema.get("type")
    if t is None:
        types = None
    elif isinstance(t, list):
        types = set(t)
    else:
        types = {t}
    if types is not None and not oas31 and schema.get("nullable") is True:
        types.add("null")
    return types


def is_oas31(doc: dict) -> bool:
    return str(doc.get("openapi", "")).startswith("3.1")
