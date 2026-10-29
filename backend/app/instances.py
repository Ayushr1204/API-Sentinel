"""Deterministic example-instance generation from OpenAPI schemas, with targeted leaf values."""
from __future__ import annotations

import copy
from typing import Any

from .oas import deref, schema_types

UNSET = object()
_FORMAT_EXAMPLES = {
    "date-time": "2024-01-01T00:00:00Z",
    "date": "2024-01-01",
    "time": "12:00:00Z",
    "email": "user@example.com",
    "uuid": "123e4567-e89b-12d3-a456-426614174000",
    "uri": "https://example.com/resource",
    "url": "https://example.com/resource",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "byte": "aGVsbG8=",
    "password": "secret-value",
}
_TYPE_ORDER = ("object", "array", "string", "integer", "number", "boolean", "null")


def merged(doc: dict, schema: Any) -> dict:
    schema, _ = deref(doc, schema)
    if not isinstance(schema, dict):
        return {}
    if "allOf" not in schema:
        return schema
    out = {k: v for k, v in schema.items() if k != "allOf"}
    props, req = dict(out.get("properties") or {}), list(out.get("required") or [])
    for sub in schema["allOf"]:
        sub = merged(doc, sub)
        props.update(sub.get("properties") or {})
        req += [r for r in sub.get("required") or [] if r not in req]
        for k, v in sub.items():
            if k not in ("properties", "required") and k not in out:
                out[k] = v
    if props:
        out["properties"] = props
    if req:
        out["required"] = req
    return out


class Generator:
    def __init__(self, doc: dict, direction: str, oas31: bool):
        self.doc, self.direction, self.oas31 = doc, direction, oas31

    def _visible(self, schema: Any) -> bool:
        s, _ = deref(self.doc, schema)
        if not isinstance(s, dict):
            return True
        return not s.get("readOnly") if self.direction == "request" else not s.get("writeOnly")

    def primary_type(self, s: dict) -> str | None:
        types = schema_types(s, self.oas31)
        if not types:
            if "properties" in s:
                return "object"
            if "items" in s:
                return "array"
            return None
        non_null = [t for t in _TYPE_ORDER if t in types and t != "null"]
        return non_null[0] if non_null else "null"

    def default(self, schema: Any, depth: int = 0) -> Any:
        s = merged(self.doc, schema)
        if depth > 8:
            return None
        for key in ("example", "default", "const"):
            if key in s:
                return copy.deepcopy(s[key])
        if isinstance(s.get("examples"), list) and s["examples"]:
            return copy.deepcopy(s["examples"][0])
        if s.get("enum"):
            return copy.deepcopy(s["enum"][0])
        for kw in ("oneOf", "anyOf"):
            if s.get(kw):
                return self.default(s[kw][0], depth + 1)
        t = self.primary_type(s)
        if t == "object" or (t is None and "properties" in s):
            props = s.get("properties") or {}
            return {name: self.default(props[name], depth + 1)
                    for name in s.get("required") or [] if name in props and self._visible(props[name])}
        if t == "array":
            n = max(int(s.get("minItems", 1) or 0), 0)
            if s.get("maxItems") is not None:
                n = min(n, int(s["maxItems"]))
            return [self.default(s.get("items", {}), depth + 1) for _ in range(n)]
        if t == "string":
            value = _FORMAT_EXAMPLES.get(s.get("format", ""), "string")
            lo, hi = s.get("minLength"), s.get("maxLength")
            if lo is not None and len(value) < lo:
                value = value + "x" * (lo - len(value))
            if hi is not None and len(value) > hi:
                value = value[:hi]
            return value
        if t in ("integer", "number"):
            lo, hi = s.get("minimum"), s.get("maximum")
            v = 1 if lo is None else lo
            if hi is not None and v > hi:
                v = hi
            return int(v) if t == "integer" else float(v)
        if t == "boolean":
            return True
        if t == "null":
            return None
        return "value"

    def targeted(self, schema: Any, segs: list[str], leaf: Any = UNSET, omit_leaf: bool = False,
                 notes: list[str] | None = None) -> Any:
        """Minimal valid instance of `schema`, with the node at `segs` forced to `leaf` (or omitted)."""
        notes = notes if notes is not None else []
        s = merged(self.doc, schema)
        if not segs:
            return self.default(s) if leaf is UNSET else copy.deepcopy(leaf)
        seg, rest = segs[0], segs[1:]
        if seg == "[]":
            return [self.targeted(s.get("items", {}), rest, leaf, omit_leaf, notes)]
        if seg.startswith("<") and "[" in seg:
            kw, idx = seg[1:-1].split("[")
            alts = s.get(kw) or []
            i = int(idx.rstrip("]"))
            if i < len(alts):
                return self.targeted(alts[i], rest, leaf, omit_leaf, notes)
            notes.append(f"composition branch {seg} missing")
            return self.default(s)
        props = s.get("properties") or {}
        # minimal object: required, visible properties only (ignore object-level examples to stay targeted)
        obj = {name: self.default(props[name]) for name in s.get("required") or []
               if name in props and self._visible(props[name])}
        if not rest and omit_leaf:
            obj.pop(seg, None)
            return obj
        if seg in props:
            obj[seg] = self.targeted(props[seg], rest, leaf, omit_leaf, notes)
        elif leaf is not UNSET and not rest:
            obj[seg] = copy.deepcopy(leaf)
        else:
            notes.append(f"property '{seg}' not present in source schema")
        return obj

    def leaf_schema(self, schema: Any, segs: list[str]) -> dict | None:
        s = merged(self.doc, schema)
        for seg in segs:
            if seg == "[]":
                s = merged(self.doc, s.get("items", {}))
            elif seg.startswith("<"):
                kw, idx = seg[1:-1].split("[")
                alts = s.get(kw) or []
                i = int(idx.rstrip("]"))
                if i >= len(alts):
                    return None
                s = merged(self.doc, alts[i])
            else:
                props = s.get("properties") or {}
                if seg not in props:
                    return None
                s = merged(self.doc, props[seg])
        return s


def fill_required(instance: Any, target_gen: Generator, schema: Any, segs: list[str], omit_leaf: bool) -> Any:
    """Isolation: add any property the TARGET schema requires (with a valid default), except the property
    under test. This neutralises unrelated changes so a failing test is attributable to its own change."""
    def walk(inst: Any, sch: Any, rest: list[str], on_path: bool) -> None:
        s = merged(target_gen.doc, sch)
        if isinstance(inst, dict):
            props = s.get("properties") or {}
            for name in s.get("required") or []:
                is_leaf = on_path and len(rest) == 1 and rest[0] == name
                if name in inst or name not in props or not target_gen._visible(props[name]):
                    continue
                if is_leaf and omit_leaf:
                    continue
                inst[name] = target_gen.default(props[name])
            for name, val in list(inst.items()):
                if name not in props:
                    continue
                nxt = on_path and bool(rest) and rest[0] == name
                if nxt and len(rest) == 1:
                    continue  # the targeted leaf keeps its deliberate value
                walk(val, props[name], rest[1:] if nxt else [], nxt)
        elif isinstance(inst, list) and "items" in s:
            nxt = on_path and bool(rest) and rest[0] == "[]"
            if nxt and len(rest) == 1:
                return
            for item in inst:
                walk(item, s["items"], rest[1:] if nxt else [], nxt)

    if segs and any(seg.startswith("<") for seg in segs):
        return instance  # composition branches: leave as generated
    if not segs:
        return instance
    walk(instance, schema, list(segs), True)
    return instance


def violating_value(source_leaf: dict, kw: str, target_bound: Any, source_bound: Any, gen: Generator) -> Any:
    """A value that satisfies the source constraint but violates the target constraint."""
    if kw == "maxLength":
        return "x" * (int(target_bound) + 1)
    if kw == "minLength":
        n = int(target_bound) - 1
        return "x" * n if n >= max(int(source_bound or 0), 0) else UNSET
    if kw in ("maximum", "minimum"):
        integer = gen.primary_type(source_leaf) == "integer"
        step = 1 if integer else 0.5
        if kw == "maximum":
            v = target_bound + step
            if source_bound is not None and v > source_bound:
                v = (target_bound + source_bound) / 2
        else:
            v = target_bound - step
            if source_bound is not None and v < source_bound:
                v = (target_bound + source_bound) / 2
        return int(v) if integer and float(v).is_integer() else v
    if kw in ("maxItems", "minItems"):
        n = int(target_bound) + 1 if kw == "maxItems" else int(target_bound) - 1
        if n < 0:
            return UNSET
        return [gen.default(source_leaf.get("items", {})) for _ in range(n)]
    return UNSET
