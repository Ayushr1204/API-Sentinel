"""Targeted compatibility-test generation: one or more executable checks per relevant change.

Every test asks the same question from the old client's point of view:
  "Does a message an existing client sends (request) or accepts (response) stay valid?"
Request tests build the instance from the BASE schema and validate it against the CANDIDATE.
Response tests build the instance from the CANDIDATE schema and validate it against the BASE.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .diff import BREAKING, NON_BREAKING, Change
from .instances import UNSET, Generator, fill_required, violating_value
from .oas import deref, is_oas31, normalize_path, operations, param_key

# informational changes that need no executable check
NO_TEST_RULES = {
    "PATH_ADDED", "OPERATION_ADDED", "OPERATION_DEPRECATED", "PATH_PARAM_RENAMED", "PARAM_DEPRECATED",
    "REQUEST_MEDIA_TYPE_ADDED", "RESPONSE_MEDIA_TYPE_ADDED", "SECURITY_REMOVED", "RESPONSE_STATUS_REMOVED",
    "RESPONSE_STATUS_ADDED", "PARAM_SERIALIZATION_CHANGED", "COMPOSITION_CHANGED", "REQUEST_BODY_REMOVED",
}


@dataclass
class TestCase:
    id: str
    change_ids: list[str]
    kind: str
    title: str
    rationale: str
    predicted: str  # incompatible | compatible | uncertain
    locator: dict
    payload: Any = None
    notes: list[str] = field(default_factory=list)
    origin: str = "deterministic"

    def to_dict(self) -> dict:
        return asdict(self)


def predicted_for(severity: str) -> str:
    return {BREAKING: "incompatible", NON_BREAKING: "compatible"}.get(severity, "uncertain")


def find_operation(doc: dict, method: str, path: str) -> tuple[str, dict, list[dict]] | None:
    key = normalize_path(path)
    for p, m, op, params in operations(doc):
        if m == method and normalize_path(p) == key:
            return p, op, params
    return None


def body_schema(doc: dict, op: dict, area: str, media_type: str | None, status: str | None) -> Any:
    if area == "request_body":
        rb = op.get("requestBody")
        if not rb:
            return None
        rb, _ = deref(doc, rb)
        return ((rb.get("content") or {}).get(media_type) or {}).get("schema")
    resp = (op.get("responses") or {}).get(status)
    if resp is None:
        return None
    resp, _ = deref(doc, resp)
    return ((resp.get("content") or {}).get(media_type) or {}).get("schema")


def leaf_directive(change: Change, gen: Generator, source_root: Any, target_gen: Generator,
                   target_root: Any) -> tuple[Any, bool, list[str]]:
    """Decide which value to place at the change's location (or whether to omit it)."""
    notes: list[str] = []
    rule = change.rule
    segs = change.segments
    if rule in ("REQUIRED_PROPERTY_ADDED", "PROPERTY_BECAME_REQUIRED", "PROPERTY_BECAME_OPTIONAL",
                "RESPONSE_PROPERTY_REMOVED") or (rule == "PROPERTY_ADDED" and change.direction == "request"):
        return UNSET, True, notes
    src_leaf = gen.leaf_schema(source_root, segs) if source_root is not None else None
    if rule == "ENUM_VALUE_REMOVED" and change.direction == "request":
        removed = [v for v in change.before or [] if v not in (change.after or [])]
        return (removed[0] if removed else UNSET), False, notes
    if rule == "ENUM_VALUE_ADDED" and change.direction == "response":
        added = [v for v in change.after or [] if v not in (change.before or [])]
        return (added[0] if added else UNSET), False, notes
    if rule in ("NULLABLE_ADDED", "NULLABLE_REMOVED"):
        return None, False, notes
    if rule == "ENUM_RESTRICTION_ADDED" and src_leaf is not None:
        allowed = change.after or []
        candidate_values = [gen.default(src_leaf), "unlisted-value", 999999]
        for v in candidate_values:
            if v not in allowed:
                return v, False, notes
    if rule == "ENUM_RESTRICTION_REMOVED":
        allowed = change.before or []
        sample = allowed[0] if allowed else None
        return (999999 if isinstance(sample, (int, float)) else "unlisted-value"), False, notes
    target_tighter = (rule == "CONSTRAINT_TIGHTENED" and change.direction == "request") or \
                     (rule == "CONSTRAINT_LOOSENED" and change.direction == "response")
    if target_tighter and src_leaf is not None:
        (kw, before), = change.before.items()
        after = change.after[kw]
        # request: old client sends values valid under base (source) — violate candidate (target).
        # response: new server sends values valid under candidate (source) — violate base (target).
        if change.direction == "request":
            source_bound, target_bound = before, after
        else:
            source_bound, target_bound = after, before
        if target_bound is not None:
            v = violating_value(src_leaf, kw, target_bound, source_bound, gen)
            if v is not UNSET:
                return v, False, notes
        notes.append("could not construct a boundary value; using a default value")
    return UNSET, False, notes


class TestGenerator:
    def __init__(self, base: dict, cand: dict):
        self.base, self.cand = base, cand
        self.tests: list[TestCase] = []

    def _add(self, change: Change, kind: str, title: str, rationale: str, payload: Any,
             notes: list[str] | None = None, predicted: str | None = None) -> None:
        self.tests.append(TestCase(
            id=f"T{len(self.tests) + 1:03d}", change_ids=[change.id], kind=kind, title=title,
            rationale=rationale, predicted=predicted or predicted_for(change.severity),
            locator={k: getattr(change, k) for k in
                     ("method", "path", "candidate_path", "area", "media_type", "status", "param", "segments",
                      "direction")},
            payload=payload, notes=notes or []))

    def generate(self, changes: list[Change]) -> list[TestCase]:
        for ch in changes:
            if ch.rule in NO_TEST_RULES:
                continue
            handler = getattr(self, f"_t_{ch.area}", None)
            if handler:
                handler(ch)
        return self.tests

    # ---------------------------------------------------------------- surface
    def _t_operation(self, ch: Change) -> None:
        if ch.rule in ("PATH_REMOVED", "OPERATION_REMOVED"):
            self._add(ch, "route_resolution", f"Old client call {ch.method.upper()} {ch.path} still routes",
                      "An existing client calls this operation; it must still exist in the candidate.",
                      {"method": ch.method, "path": ch.path})
        elif ch.rule == "OPERATION_ID_CHANGED":
            self._add(ch, "operation_id_binding", f"SDK binding '{ch.before}' still exists",
                      "Generated SDKs bind to operationId; the old identifier must still be present.",
                      {"operationId": ch.before})

    def _t_server(self, ch: Change) -> None:
        self._add(ch, "server_url", f"Server URL {ch.before} still declared",
                  "Clients configured with a base URL from the old spec must find it in the candidate.",
                  {"url": ch.before})

    def _t_security(self, ch: Change) -> None:
        if ch.rule == "SECURITY_SCHEME_CHANGED":
            self._add(ch, "security_scheme", f"Credentials for scheme '{ch.param[1]}' supplied the old way",
                      "Clients supply credentials exactly as the old scheme described.",
                      {"scheme": ch.param[1], "as_supplied": ch.before})
        else:
            sets = [r for r in (ch.before or []) if isinstance(r, dict)] or [{}]
            self._add(ch, "credential_compat", f"Old credentials accepted by {ch.method.upper()} {ch.path}",
                      "Each credential set that satisfied the old requirements must satisfy the new ones.",
                      {"credential_sets": sets})

    # ---------------------------------------------------------------- request side
    def _old_client_params(self, ch: Change) -> tuple[dict, list[str]] | None:
        found = find_operation(self.base, ch.method, ch.path)
        if not found:
            return None
        _, _, bparams = found
        gen = Generator(self.base, "request", is_oas31(self.base))
        out, notes = {}, []
        for p in bparams:
            key = param_key(p)
            target = ch.param and tuple(ch.param) == key
            if target and ch.rule == "PARAM_BECAME_REQUIRED":
                continue  # old client may omit it
            if not (p.get("required") or p.get("in") == "path" or target):
                continue
            schema = p.get("schema") or {}
            if target and ch.rule not in ("PARAM_REMOVED",):
                cand = find_operation(self.cand, ch.method, ch.path)
                cschema = next((cp.get("schema") for cp in (cand[2] if cand else []) if param_key(cp) == key), None)
                leaf, omit, n = leaf_directive(ch, gen, schema, Generator(self.cand, "request",
                                                                           is_oas31(self.cand)), cschema)
                notes += n
                value = gen.targeted(schema, ch.segments, leaf, omit, notes)
            else:
                value = gen.default(schema)
            out[f"{key[0]}:{p.get('name')}"] = value
        # Isolation: supply any other parameter the candidate newly requires, so only the targeted change counts.
        cand = find_operation(self.cand, ch.method, ch.path)
        if cand:
            cgen = Generator(self.cand, "request", is_oas31(self.cand))
            sent = {(k.split(":", 1)[0], k.split(":", 1)[1].lower() if k.startswith("header:") else k.split(":", 1)[1])
                    for k in out}
            for cp in cand[2]:
                key = param_key(cp)
                if key[0] == "path" or key in sent or not cp.get("required"):
                    continue
                if ch.param and tuple(ch.param) == key:
                    continue
                out[f"{key[0]}:{cp.get('name')}"] = cgen.default(cp.get("schema") or {})
                notes.append(f"isolation: also sends newly required {key[0]} parameter '{cp.get('name')}'")
        return out, notes

    def _t_parameter(self, ch: Change) -> None:
        res = self._old_client_params(ch)
        if res is None:
            return
        params, notes = res
        what = f"{ch.param[0]} parameter '{ch.param[1]}'" if ch.param else "parameters"
        self._add(ch, "request_parameters", f"Old client parameters accepted ({what})",
                  "Parameters an existing client sends (built from the base spec, targeting the change) are "
                  "validated against the candidate operation's parameter definitions.", {"params": params}, notes)

    def _t_request_body(self, ch: Change) -> None:
        if ch.rule in ("REQUEST_BODY_ADDED", "REQUEST_BODY_BECAME_REQUIRED"):
            self._add(ch, "request_body_presence", "Old client request without a body is accepted",
                      "Existing clients send no body; the candidate must not require one.",
                      {"body_present": False})
            return
        if ch.rule == "REQUEST_MEDIA_TYPE_REMOVED":
            self._add(ch, "request_media_type", f"Old client Content-Type {ch.media_type} still accepted",
                      "Clients send this media type today.", {"media_type": ch.media_type})
            return
        found = find_operation(self.base, ch.method, ch.path)
        cfound = find_operation(self.cand, ch.method, ch.path)
        if not found or not cfound:
            return
        src = body_schema(self.base, found[1], "request_body", ch.media_type, None)
        tgt = body_schema(self.cand, cfound[1], "request_body", ch.media_type, None)
        if src is None:
            return
        gen = Generator(self.base, "request", is_oas31(self.base))
        tgen = Generator(self.cand, "request", is_oas31(self.cand))
        leaf, omit, notes = leaf_directive(ch, gen, src, tgen, tgt)
        payload = gen.targeted(src, ch.segments, leaf, omit, notes)
        if tgt is not None:
            payload = fill_required(payload, tgen, tgt, ch.segments, omit)
        loc = ".".join(ch.segments) or "body"
        self._add(ch, "request_body", f"Old client body accepted ({loc})",
                  "A request body built from the base schema, targeting the changed field, is validated against "
                  "the candidate request schema.", payload, notes)

    # ---------------------------------------------------------------- response side
    def _t_response(self, ch: Change) -> None:
        if ch.rule == "SUCCESS_RESPONSE_REMOVED":
            self._add(ch, "response_status", f"Status {ch.status} still documented",
                      "Old clients branch on this success status.", {"status": ch.status})
            return
        if ch.rule == "RESPONSE_MEDIA_TYPE_REMOVED":
            self._add(ch, "response_media_type", f"Response media type {ch.media_type} still produced",
                      "Old clients parse this media type.", {"media_type": ch.media_type})
            return
        if ch.rule == "RESPONSE_HEADER_REMOVED":
            self._add(ch, "response_header", f"Response header {ch.before} still documented",
                      "Old clients may read this header.", {"header": ch.before})
            return
        found = find_operation(self.base, ch.method, ch.path)
        cfound = find_operation(self.cand, ch.method, ch.path)
        if not found or not cfound:
            return
        src = body_schema(self.cand, cfound[1], "response", ch.media_type, ch.status)
        tgt = body_schema(self.base, found[1], "response", ch.media_type, ch.status)
        if src is None:
            return
        gen = Generator(self.cand, "response", is_oas31(self.cand))
        tgen = Generator(self.base, "response", is_oas31(self.base))
        leaf, omit, notes = leaf_directive(ch, gen, src, tgen, tgt)
        payload = gen.targeted(src, ch.segments, leaf, omit, notes)
        if tgt is not None:
            payload = fill_required(payload, tgen, tgt, ch.segments, omit)
        loc = ".".join(ch.segments) or "body"
        predicted = None
        if ch.rule == "RESPONSE_PROPERTY_REMOVED" and not (ch.before or {}).get("was_required"):
            predicted = "uncertain"
            notes.append("the removed field was optional in the base schema, so a schema check cannot detect its "
                         "absence; impact depends on whether consumers read it")
        self._add(ch, "response_body", f"New response accepted by old client ({loc})",
                  "A response built from the candidate schema, targeting the changed field, is validated against "
                  "the base schema that existing clients were built from.", payload, notes, predicted)


def generate_tests(base: dict, cand: dict, changes: list[Change]) -> list[TestCase]:
    return TestGenerator(base, cand).generate(changes)
