"""Deterministic, direction-aware structural diff of two OpenAPI documents.

Request side (data clients SEND): tightening breaks old clients.
Response side (data clients RECEIVE): loosening / removal breaks old clients.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .oas import deref, is_oas31, normalize_path, operations, param_key, schema_types

BREAKING = "BREAKING"
POTENTIAL = "POTENTIALLY_BREAKING"
NON_BREAKING = "NON_BREAKING"
SEVERITY_ORDER = {BREAKING: 0, POTENTIAL: 1, NON_BREAKING: 2}
MAX_SCHEMA_DEPTH = 25
_TEMPLATE = re.compile(r"\{([^}/]+)\}")


@dataclass
class Change:
    rule: str
    severity: str
    direction: str  # request | response | surface
    message: str
    method: str | None = None
    path: str | None = None            # path in base spec (or candidate if added)
    candidate_path: str | None = None
    area: str | None = None            # parameter | request_body | response | operation | security | server
    media_type: str | None = None
    status: str | None = None
    param: list[str] | None = None     # [in, name]
    segments: list[str] = field(default_factory=list)  # schema path below body/param root
    component: str | None = None       # nearest named schema component
    before: Any = None
    after: Any = None
    id: str = ""

    @property
    def location(self) -> str:
        parts = []
        if self.method and self.path:
            parts.append(f"{self.method.upper()} {self.path}")
        elif self.path:
            parts.append(self.path)
        if self.area == "parameter" and self.param:
            parts.append(f"{self.param[0]} parameter '{self.param[1]}'")
        if self.area == "request_body":
            parts.append(f"request body{f' ({self.media_type})' if self.media_type else ''}")
        if self.area == "response":
            parts.append(f"response {self.status}{f' ({self.media_type})' if self.media_type else ''}")
        if self.segments:
            parts.append(".".join(self.segments).replace(".[]", "[]"))
        return " › ".join(parts) if parts else "document"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["location"] = self.location
        return d


def _jsonable(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


class Differ:
    def __init__(self, base: dict, cand: dict):
        self.base, self.cand = base, cand
        self.b31, self.c31 = is_oas31(base), is_oas31(cand)
        self.changes: list[Change] = []

    # ------------------------------------------------------------------ public
    def run(self) -> list[Change]:
        self._servers()
        self._security_schemes()
        self._operations()
        # stable ordering + IDs
        self.changes.sort(key=lambda c: (SEVERITY_ORDER[c.severity], c.path or "", c.method or "",
                                         c.area or "", c.status or "", c.media_type or "",
                                         ".".join(c.segments), c.rule))
        seen, unique = set(), []
        for c in self.changes:
            key = (c.rule, c.method, c.path, c.area, c.status, c.media_type, tuple(c.param or ()),
                   tuple(c.segments))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        for i, c in enumerate(unique, 1):
            c.id = f"C{i:03d}"
        self.changes = unique
        return unique

    def add(self, **kw) -> None:
        self.changes.append(Change(**kw))

    # ------------------------------------------------------------------ surface
    def _servers(self) -> None:
        b = {s.get("url") for s in self.base.get("servers") or [] if isinstance(s, dict)}
        c = {s.get("url") for s in self.cand.get("servers") or [] if isinstance(s, dict)}
        for url in sorted(b - c):
            if c:  # if candidate declares no servers at all, OAS defaults to '/', treat as unspecified
                self.add(rule="SERVER_URL_REMOVED", severity=POTENTIAL, direction="surface", area="server",
                         message=f"Server URL '{url}' is no longer listed; clients configured with it may fail.",
                         before=url, after=sorted(c))

    def _security_schemes(self) -> None:
        bs = (self.base.get("components") or {}).get("securitySchemes") or {}
        cs = (self.cand.get("components") or {}).get("securitySchemes") or {}
        for name in sorted(set(bs) & set(cs)):
            b, _ = deref(self.base, bs[name])
            c, _ = deref(self.cand, cs[name])
            keys = ("type", "in", "name", "scheme", "bearerFormat")
            if any(b.get(k) != c.get(k) for k in keys):
                self.add(rule="SECURITY_SCHEME_CHANGED", severity=BREAKING, direction="surface", area="security",
                         param=["securityScheme", name],
                         message=f"Security scheme '{name}' changed how credentials are supplied.",
                         before={k: b.get(k) for k in keys if k in b}, after={k: c.get(k) for k in keys if k in c})

    # ------------------------------------------------------------------ operations
    def _operations(self) -> None:
        b_ops = {(normalize_path(p), m): (p, op, params) for p, m, op, params in operations(self.base)}
        c_ops = {(normalize_path(p), m): (p, op, params) for p, m, op, params in operations(self.cand)}
        b_paths = {normalize_path(p) for p in (self.base.get("paths") or {})}
        c_paths = {normalize_path(p) for p in (self.cand.get("paths") or {})}

        for key in sorted(set(b_ops) - set(c_ops)):
            path, op, _ = b_ops[key]
            if key[0] not in c_paths:
                rule, msg = "PATH_REMOVED", f"Path '{path}' was removed; every call to it will fail."
            else:
                rule, msg = "OPERATION_REMOVED", f"{key[1].upper()} {path} was removed."
            self.add(rule=rule, severity=BREAKING, direction="surface", method=key[1], path=path,
                     area="operation", message=msg, before=op.get("operationId"))
        for key in sorted(set(c_ops) - set(b_ops)):
            path, op, _ = c_ops[key]
            rule = "PATH_ADDED" if key[0] not in b_paths else "OPERATION_ADDED"
            self.add(rule=rule, severity=NON_BREAKING, direction="surface", method=key[1], path=path,
                     candidate_path=path, area="operation", message=f"{key[1].upper()} {path} was added.",
                     after=op.get("operationId"))
        for key in sorted(set(b_ops) & set(c_ops)):
            bp, bop, bparams = b_ops[key]
            cp, cop, cparams = c_ops[key]
            self._operation(key[1], bp, cp, bop, cop, bparams, cparams)

    def _operation(self, method, bp, cp, bop, cop, bparams, cparams) -> None:
        ctx = dict(method=method, path=bp, candidate_path=cp)
        if bp != cp:
            self.add(rule="PATH_PARAM_RENAMED", severity=NON_BREAKING, direction="surface", area="operation",
                     message=f"Path template renamed from '{bp}' to '{cp}' (same URL shape).",
                     before=bp, after=cp, **ctx)
        if bop.get("operationId") != cop.get("operationId") and bop.get("operationId"):
            self.add(rule="OPERATION_ID_CHANGED", severity=POTENTIAL, direction="surface", area="operation",
                     message="operationId changed; generated SDK method names will change.",
                     before=bop.get("operationId"), after=cop.get("operationId"), **ctx)
        if not bop.get("deprecated") and cop.get("deprecated"):
            self.add(rule="OPERATION_DEPRECATED", severity=NON_BREAKING, direction="surface", area="operation",
                     message="Operation marked deprecated.", **ctx)
        self._op_security(bop, cop, ctx)
        self._parameters(bparams, cparams, ctx)
        self._request_body(bop.get("requestBody"), cop.get("requestBody"), ctx)
        self._responses(bop.get("responses") or {}, cop.get("responses") or {}, ctx)

    def _effective_security(self, doc: dict, op: dict) -> list[dict]:
        sec = op.get("security", doc.get("security"))
        return [r for r in (sec or []) if isinstance(r, dict)]

    def _op_security(self, bop, cop, ctx) -> None:
        b = self._effective_security(self.base, bop)
        c = self._effective_security(self.cand, cop)
        b_open = not b or any(len(r) == 0 for r in b)
        c_open = not c or any(len(r) == 0 for r in c)
        if b_open and not c_open:
            self.add(rule="SECURITY_ADDED", severity=BREAKING, direction="request", area="security",
                     message="Operation now requires authentication; anonymous clients will be rejected.",
                     before=b, after=c, **ctx)
            return
        if c_open:
            if not b_open:
                self.add(rule="SECURITY_REMOVED", severity=NON_BREAKING, direction="request", area="security",
                         message="Operation no longer requires authentication.", before=b, after=c, **ctx)
            return

        def accepted(old: dict) -> bool:  # some candidate alternative satisfiable with old credentials
            return any(all(n in old and set(sc or []) <= set(old[n] or []) for n, sc in alt.items())
                       for alt in c)
        lost = [r for r in b if not accepted(r)]
        if lost:
            self.add(rule="SECURITY_REQUIREMENT_CHANGED", severity=BREAKING, direction="request", area="security",
                     message="Credentials accepted before no longer satisfy the operation's security requirements.",
                     before=b, after=c, **ctx)

    # ------------------------------------------------------------------ parameters
    def _parameters(self, bparams, cparams, ctx) -> None:
        # Path templates were matched by shape, so map base path-param names onto candidate names by position.
        renames = dict(zip(_TEMPLATE.findall(ctx["path"]), _TEMPLATE.findall(ctx["candidate_path"])))
        bmap = {}
        for p in bparams:
            key = param_key(p)
            if key[0] == "path" and key[1] in renames:
                key = ("path", renames[key[1]])
            bmap[key] = p
        cmap = {param_key(p): p for p in cparams}
        for key in sorted(set(bmap) - set(cmap)):
            if key[0] == "path":
                continue  # path params are covered by path matching
            self.add(rule="PARAM_REMOVED", severity=POTENTIAL, direction="request", area="parameter",
                     param=list(key), message=f"{key[0]} parameter '{key[1]}' was removed; the server may now "
                     "ignore or reject it.", before=bmap[key].get("required", False), **ctx)
        for key in sorted(set(cmap) - set(bmap)):
            p = cmap[key]
            req = bool(p.get("required")) or key[0] == "path"
            self.add(rule="REQUIRED_PARAM_ADDED" if req else "OPTIONAL_PARAM_ADDED",
                     severity=BREAKING if req else NON_BREAKING, direction="request", area="parameter",
                     param=list(key), after={"required": req},
                     message=f"New {'required' if req else 'optional'} {key[0]} parameter '{key[1]}'.", **ctx)
        for key in sorted(set(bmap) & set(cmap)):
            b, c = bmap[key], cmap[key]
            pctx = dict(ctx, area="parameter", param=list(key))
            breq, creq = bool(b.get("required")), bool(c.get("required"))
            if not breq and creq:
                self.add(rule="PARAM_BECAME_REQUIRED", severity=BREAKING, direction="request",
                         message=f"{key[0]} parameter '{key[1]}' is now required.", before=False, after=True, **pctx)
            elif breq and not creq:
                self.add(rule="PARAM_BECAME_OPTIONAL", severity=NON_BREAKING, direction="request",
                         message=f"{key[0]} parameter '{key[1]}' is now optional.", before=True, after=False, **pctx)
            for attr in ("style", "explode"):
                if b.get(attr) != c.get(attr):
                    self.add(rule="PARAM_SERIALIZATION_CHANGED", severity=POTENTIAL, direction="request",
                             message=f"Serialization '{attr}' of parameter '{key[1]}' changed.",
                             before=b.get(attr), after=c.get(attr), **pctx)
            if not b.get("deprecated") and c.get("deprecated"):
                self.add(rule="PARAM_DEPRECATED", severity=NON_BREAKING, direction="request",
                         message=f"Parameter '{key[1]}' marked deprecated.", **pctx)
            if b.get("schema") is not None and c.get("schema") is not None:
                self._schema(b["schema"], c["schema"], "request", pctx, [], set(), None)

    # ------------------------------------------------------------------ request body
    def _request_body(self, b_rb, c_rb, ctx) -> None:
        rctx = dict(ctx, area="request_body")
        b = deref(self.base, b_rb)[0] if b_rb else None
        c = deref(self.cand, c_rb)[0] if c_rb else None
        if b is None and c is None:
            return
        if b is None:
            req = bool(c.get("required"))
            self.add(rule="REQUEST_BODY_ADDED", severity=BREAKING if req else NON_BREAKING, direction="request",
                     message=f"A {'required' if req else 'optional'} request body was added.", after=req, **rctx)
            return
        if c is None:
            self.add(rule="REQUEST_BODY_REMOVED", severity=POTENTIAL, direction="request",
                     message="Request body was removed; data clients send may be ignored or rejected.", **rctx)
            return
        if not b.get("required") and c.get("required"):
            self.add(rule="REQUEST_BODY_BECAME_REQUIRED", severity=BREAKING, direction="request",
                     message="Request body is now required.", before=False, after=True, **rctx)
        bc, cc = b.get("content") or {}, c.get("content") or {}
        for mt in sorted(set(bc) - set(cc)):
            self.add(rule="REQUEST_MEDIA_TYPE_REMOVED", severity=BREAKING, direction="request", media_type=mt,
                     message=f"Request media type '{mt}' is no longer accepted.", **rctx)
        for mt in sorted(set(cc) - set(bc)):
            self.add(rule="REQUEST_MEDIA_TYPE_ADDED", severity=NON_BREAKING, direction="request", media_type=mt,
                     message=f"Request media type '{mt}' is now accepted.", **rctx)
        for mt in sorted(set(bc) & set(cc)):
            bs, cs = (bc[mt] or {}).get("schema"), (cc[mt] or {}).get("schema")
            if bs is not None and cs is not None:
                self._schema(bs, cs, "request", dict(rctx, media_type=mt), [], set(), None)

    # ------------------------------------------------------------------ responses
    def _responses(self, br, cr, ctx) -> None:
        for code in sorted(set(br) - set(cr)):
            success = str(code).startswith("2")
            self.add(rule="SUCCESS_RESPONSE_REMOVED" if success else "RESPONSE_STATUS_REMOVED",
                     severity=BREAKING if success else NON_BREAKING, direction="response", area="response",
                     status=str(code), message=f"Response {code} is no longer documented." +
                     (" Clients expecting it will mis-handle replies." if success else ""), **ctx)
        for code in sorted(set(cr) - set(br)):
            success = str(code).startswith("2")
            self.add(rule="RESPONSE_STATUS_ADDED", severity=POTENTIAL if success else NON_BREAKING,
                     direction="response", area="response", status=str(code),
                     message=f"New response status {code}." + (" Clients may not handle a new success code."
                                                                 if success else ""), **ctx)
        for code in sorted(set(br) & set(cr)):
            b, _ = deref(self.base, br[code])
            c, _ = deref(self.cand, cr[code])
            rctx = dict(ctx, area="response", status=str(code))
            bh, ch = b.get("headers") or {}, c.get("headers") or {}
            for h in sorted(set(bh) - set(ch)):
                self.add(rule="RESPONSE_HEADER_REMOVED", severity=POTENTIAL, direction="response",
                         message=f"Response header '{h}' is no longer documented.", before=h, **rctx)
            bc, cc = b.get("content") or {}, c.get("content") or {}
            for mt in sorted(set(bc) - set(cc)):
                self.add(rule="RESPONSE_MEDIA_TYPE_REMOVED", severity=BREAKING, direction="response",
                         media_type=mt, message=f"Response media type '{mt}' is no longer returned.", **rctx)
            for mt in sorted(set(cc) - set(bc)):
                self.add(rule="RESPONSE_MEDIA_TYPE_ADDED", severity=NON_BREAKING, direction="response",
                         media_type=mt, message=f"Response media type '{mt}' added.", **rctx)
            for mt in sorted(set(bc) & set(cc)):
                bs, cs = (bc[mt] or {}).get("schema"), (cc[mt] or {}).get("schema")
                if bs is not None and cs is not None:
                    self._schema(bs, cs, "response", dict(rctx, media_type=mt), [], set(), None)

    # ------------------------------------------------------------------ schemas
    def _merged(self, doc: dict, schema: dict) -> dict:
        """Flatten allOf into a single object view (properties + required)."""
        if "allOf" not in schema:
            return schema
        out = {k: v for k, v in schema.items() if k != "allOf"}
        props = dict(out.get("properties") or {})
        required = list(out.get("required") or [])
        for sub in schema["allOf"]:
            sub, _ = deref(doc, sub)
            sub = self._merged(doc, sub)
            props.update(sub.get("properties") or {})
            required += [r for r in sub.get("required") or [] if r not in required]
            for k, v in sub.items():
                if k not in ("properties", "required") and k not in out:
                    out[k] = v
        if props:
            out["properties"] = props
        if required:
            out["required"] = required
        return out

    def _schema(self, b_raw, c_raw, direction, ctx, segs, visited, component) -> None:
        if len(segs) > MAX_SCHEMA_DEPTH:
            return
        b, bname = deref(self.base, b_raw)
        c, cname = deref(self.cand, c_raw)
        if not isinstance(b, dict) or not isinstance(c, dict):
            return
        component = cname or bname or component
        pair = (bname, cname)
        if bname and cname:
            if pair in visited:
                return  # recursion
            visited = visited | {pair}
        b, c = self._merged(self.base, b), self._merged(self.cand, c)
        req = direction == "request"
        common = dict(ctx, segments=list(segs), component=component, direction=direction)

        # --- types
        bt, ct = schema_types(b, self.b31), schema_types(c, self.c31)
        if bt is not None and ct is not None and bt != ct:
            b_core, c_core = bt - {"null"}, ct - {"null"}
            null_added = "null" in ct and "null" not in bt
            null_removed = "null" in bt and "null" not in ct
            if b_core != c_core:
                widened = c_core >= b_core or (b_core == {"integer"} and c_core >= {"number"})
                narrowed = c_core <= b_core or (c_core == {"integer"} and b_core >= {"number"})
                ok = widened if req else narrowed
                self.add(rule="TYPE_CHANGED" if not ok else ("TYPE_WIDENED" if req else "TYPE_NARROWED"),
                         severity=NON_BREAKING if ok else BREAKING,
                         message=f"Type changed from {sorted(bt)} to {sorted(ct)}.",
                         before=sorted(bt), after=sorted(ct), **common)
                if not ok:
                    return  # deeper comparison is meaningless once the type is incompatible
            if null_added:
                self.add(rule="NULLABLE_ADDED", severity=NON_BREAKING if req else POTENTIAL,
                         message="Value may now be null." + ("" if req else " Clients may not handle null."),
                         before=False, after=True, **common)
            if null_removed:
                self.add(rule="NULLABLE_REMOVED", severity=BREAKING if req else NON_BREAKING,
                         message="Value may no longer be null." + (" Clients sending null will be rejected."
                                                                     if req else ""),
                         before=True, after=False, **common)

        # --- enums
        be, ce = b.get("enum"), c.get("enum")
        if be is not None or ce is not None:
            bset = {_jsonable(v): v for v in be or []}
            cset = {_jsonable(v): v for v in ce or []}
            if be is not None and ce is not None:
                removed = [bset[k] for k in sorted(set(bset) - set(cset))]
                added = [cset[k] for k in sorted(set(cset) - set(bset))]
                if removed:
                    self.add(rule="ENUM_VALUE_REMOVED", severity=BREAKING if req else NON_BREAKING,
                             message=f"Enum value(s) {removed} removed." +
                             (" Clients sending them will be rejected." if req else ""),
                             before=be, after=ce, **common)
                if added:
                    self.add(rule="ENUM_VALUE_ADDED", severity=NON_BREAKING if req else POTENTIAL,
                             message=f"Enum value(s) {added} added." +
                             ("" if req else " Clients with exhaustive handling may fail on unknown values."),
                             before=be, after=ce, **common)
            elif ce is not None:
                self.add(rule="ENUM_RESTRICTION_ADDED", severity=BREAKING if req else NON_BREAKING,
                         message=f"Value is now restricted to {ce}.", before=None, after=ce, **common)
            else:
                self.add(rule="ENUM_RESTRICTION_REMOVED", severity=NON_BREAKING if req else POTENTIAL,
                         message="Enum restriction removed; any value may now appear.", before=be, after=None,
                         **common)

        # --- constraints
        self._constraints(b, c, req, common)

        # --- format
        if b.get("format") != c.get("format") and (b.get("format") or c.get("format")):
            self.add(rule="FORMAT_CHANGED", severity=POTENTIAL, message=f"Format changed from "
                     f"'{b.get('format')}' to '{c.get('format')}'.", before=b.get("format"), after=c.get("format"),
                     **common)

        # --- objects
        bprops, cprops = b.get("properties") or {}, c.get("properties") or {}
        if bprops or cprops:
            self._properties(b, c, bprops, cprops, direction, ctx, segs, visited, component)

        # --- arrays
        if b.get("items") is not None and c.get("items") is not None:
            self._schema(b["items"], c["items"], direction, ctx, segs + ["[]"], visited, component)

        # --- composition
        for kw in ("oneOf", "anyOf"):
            bl, cl = b.get(kw), c.get(kw)
            if bl is None and cl is None:
                continue
            if bl is None or cl is None or len(bl) != len(cl):
                self.add(rule="COMPOSITION_CHANGED", severity=POTENTIAL,
                         message=f"'{kw}' alternatives changed ({len(bl or [])} → {len(cl or [])}).",
                         before=len(bl or []), after=len(cl or []), **common)
            else:
                for i, (bs, cs) in enumerate(zip(bl, cl)):
                    self._schema(bs, cs, direction, ctx, segs + [f"<{kw}[{i}]>"], visited, component)

    def _constraints(self, b, c, req, common) -> None:
        # (keyword, True if larger value = looser)
        for kw, larger_looser in (("maxLength", True), ("maxItems", True), ("maximum", True),
                                  ("maxProperties", True), ("minLength", False), ("minItems", False),
                                  ("minimum", False), ("minProperties", False)):
            bv, cv = b.get(kw), c.get(kw)
            if bv == cv or not (isinstance(bv, (int, float, type(None))) and isinstance(cv, (int, float, type(None)))):
                continue
            if bv is None:
                tightened = True
            elif cv is None:
                tightened = False
            else:
                tightened = (cv < bv) if larger_looser else (cv > bv)
            if req:
                sev = BREAKING if tightened else NON_BREAKING
            else:
                sev = NON_BREAKING if tightened else POTENTIAL
            self.add(rule="CONSTRAINT_TIGHTENED" if tightened else "CONSTRAINT_LOOSENED", severity=sev,
                     message=f"'{kw}' changed from {bv} to {cv}.", before={kw: bv}, after={kw: cv}, **common)
        if b.get("pattern") != c.get("pattern") and c.get("pattern"):
            self.add(rule="PATTERN_CHANGED", severity=POTENTIAL if req else NON_BREAKING,
                     message=f"pattern changed from {b.get('pattern')!r} to {c.get('pattern')!r}.",
                     before=b.get("pattern"), after=c.get("pattern"), **common)

    def _properties(self, b, c, bprops, cprops, direction, ctx, segs, visited, component) -> None:
        req = direction == "request"
        breq, creq = set(b.get("required") or []), set(c.get("required") or [])

        def visible(doc, schema) -> bool:  # readOnly never sent by clients; writeOnly never returned
            s, _ = deref(doc, schema)
            if not isinstance(s, dict):
                return True
            return not s.get("readOnly") if req else not s.get("writeOnly")

        bvis = {k for k, v in bprops.items() if visible(self.base, v)}
        cvis = {k for k, v in cprops.items() if visible(self.cand, v)}
        closed = c.get("additionalProperties") is False
        for name in sorted(bvis - set(cprops)):
            common = dict(ctx, segments=segs + [name], component=component, direction=direction)
            if req:
                self.add(rule="REQUEST_PROPERTY_REMOVED", severity=BREAKING if closed else POTENTIAL,
                         message=f"Property '{name}' removed from request schema" +
                         (" and additionalProperties is false: clients sending it will be rejected."
                          if closed else "; values clients send may be silently ignored."), before=name, **common)
            else:
                self.add(rule="RESPONSE_PROPERTY_REMOVED", severity=BREAKING,
                         message=f"Property '{name}' removed from response; clients reading it will break.",
                         before={"property": name, "was_required": name in breq}, **common)
        for name in sorted(cvis - set(bprops)):
            common = dict(ctx, segments=segs + [name], component=component, direction=direction)
            if req and name in creq:
                self.add(rule="REQUIRED_PROPERTY_ADDED", severity=BREAKING,
                         message=f"New required request property '{name}'; existing clients do not send it.",
                         after=name, **common)
            else:
                self.add(rule="PROPERTY_ADDED", severity=NON_BREAKING,
                         message=f"New {'optional request' if req else 'response'} property '{name}'.",
                         after=name, **common)
        for name in sorted(bvis & cvis):
            common = dict(ctx, segments=segs + [name], component=component, direction=direction)
            if name not in breq and name in creq:
                self.add(rule="PROPERTY_BECAME_REQUIRED", severity=BREAKING if req else NON_BREAKING,
                         message=f"Property '{name}' is now required." +
                         (" Clients omitting it will be rejected." if req else ""), before=False, after=True,
                         **common)
            elif name in breq and name not in creq:
                self.add(rule="PROPERTY_BECAME_OPTIONAL", severity=NON_BREAKING if req else BREAKING,
                         message=f"Property '{name}' is no longer required." +
                         ("" if req else " Responses may omit it; clients relying on it will break."),
                         before=True, after=False, **common)
            self._schema(bprops[name], cprops[name], direction, ctx, segs + [name], visited, component)


def diff_specs(base: dict, cand: dict) -> list[Change]:
    return Differ(base, cand).run()
