"""Executes compatibility tests. Every check really runs: schema checks use OpenAPI-aware JSON Schema
validators (OAS 3.0 or 3.1 semantics) and structural checks read the candidate/base documents."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from openapi_schema_validator import OAS30Validator, OAS31Validator, oas30_format_checker, oas31_format_checker

from .oas import deref, inline_schema, is_oas31, normalize_path, param_key
from .testgen import TestCase, body_schema, find_operation


@dataclass
class TestResult:
    test_id: str
    status: str            # passed | failed | error
    observed: str | None   # compatible | incompatible | None (error)
    verdict: str           # confirms_prediction | contradicts_prediction | inconclusive
    summary: str
    errors: list[dict] = field(default_factory=list)
    collateral: list[dict] = field(default_factory=list)  # errors outside the targeted location
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_hidden_required(schema: Any, direction: str) -> Any:
    """readOnly properties are never sent and writeOnly never returned, so neither can be 'required'."""
    if isinstance(schema, list):
        return [_strip_hidden_required(s, direction) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _strip_hidden_required(v, direction) for k, v in schema.items()}
    props = out.get("properties")
    if isinstance(props, dict) and isinstance(out.get("required"), list):
        hidden = "readOnly" if direction == "request" else "writeOnly"
        out["required"] = [r for r in out["required"]
                           if not (isinstance(props.get(r), dict) and props[r].get(hidden))]
        if not out["required"]:
            del out["required"]
    return out


def validate_instance(instance: Any, schema: Any, doc: dict, direction: str) -> list[dict]:
    inlined = _strip_hidden_required(inline_schema(doc, schema), direction)
    if is_oas31(doc):
        validator = OAS31Validator(inlined, format_checker=oas31_format_checker)
    else:
        validator = OAS30Validator(inlined, format_checker=oas30_format_checker)
    errs = sorted(validator.iter_errors(instance), key=lambda e: (list(e.absolute_path), e.message))
    return [{"path": "/".join(str(p) for p in e.absolute_path) or "(root)", "message": e.message[:300]}
            for e in errs[:20]]


class Runner:
    def __init__(self, base: dict, cand: dict):
        self.base, self.cand = base, cand

    def run(self, test: TestCase) -> TestResult:
        start = time.perf_counter()
        try:
            handler = getattr(self, f"_r_{test.kind}")
            errors, summary = handler(test)
            errors, collateral = _split_relevant(errors, test)
            status = "failed" if errors else "passed"
            observed = "incompatible" if errors else "compatible"
            if test.predicted == "uncertain":
                verdict = "inconclusive" if not errors else "risk_confirmed"
            else:
                verdict = "confirms_prediction" if observed == test.predicted else "contradicts_prediction"
        except Exception as exc:  # report, never hide
            collateral = []
            errors, summary, status, observed, verdict = (
                [{"path": "(runner)", "message": f"{type(exc).__name__}: {exc}"}],
                "Test could not be executed", "error", None, "inconclusive")
        return TestResult(test_id=test.id, status=status, observed=observed, verdict=verdict, summary=summary,
                          errors=errors, collateral=collateral, duration_ms=round((time.perf_counter() - start) * 1000, 2))

    # ------------------------------------------------------------------ structural
    def _r_route_resolution(self, t: TestCase):
        p = t.payload
        found = find_operation(self.cand, p["method"], p["path"])
        if found:
            return [], f"{p['method'].upper()} {p['path']} resolves to {found[0]} in the candidate"
        return [{"path": "paths", "message": f"No candidate operation matches {p['method'].upper()} {p['path']}"}], \
            "Route no longer exists; existing calls would receive 404/405"

    def _r_operation_id_binding(self, t: TestCase):
        op_id = t.payload["operationId"]
        for item in (self.cand.get("paths") or {}).values():
            item, _ = deref(self.cand, item)
            for op in item.values():
                if isinstance(op, dict) and op.get("operationId") == op_id:
                    return [], f"operationId '{op_id}' still exists"
        return [{"path": "operationId", "message": f"operationId '{op_id}' not found in candidate"}], \
            "SDK method bound to the old operationId would disappear"

    def _r_server_url(self, t: TestCase):
        urls = [s.get("url") for s in self.cand.get("servers") or []]
        if t.payload["url"] in urls:
            return [], "Server URL still declared"
        return [{"path": "servers", "message": f"'{t.payload['url']}' not in candidate servers {urls}"}], \
            "Configured base URL no longer declared"

    def _r_security_scheme(self, t: TestCase):
        name = t.payload["scheme"]
        cs = ((self.cand.get("components") or {}).get("securitySchemes") or {}).get(name)
        if cs is None:
            return [{"path": f"securitySchemes/{name}", "message": "scheme no longer defined"}], "Scheme removed"
        cs, _ = deref(self.cand, cs)
        errs = [{"path": f"securitySchemes/{name}/{k}", "message": f"expected {v!r}, candidate has {cs.get(k)!r}"}
                for k, v in (t.payload["as_supplied"] or {}).items() if cs.get(k) != v]
        return errs, ("Credentials supplied the old way are no longer recognised" if errs
                      else "Scheme placement unchanged")

    def _r_credential_compat(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        if not found:
            return [{"path": "paths", "message": "operation missing in candidate"}], "Operation missing"
        op = found[1]
        reqs = [r for r in (op.get("security", self.cand.get("security")) or []) if isinstance(r, dict)]
        if not reqs or any(len(r) == 0 for r in reqs):
            return [], "Candidate accepts anonymous calls"
        errs = []
        for creds in t.payload["credential_sets"]:
            ok = any(all(n in creds and set(sc or []) <= set(creds[n] or []) for n, sc in alt.items())
                     for alt in reqs)
            if not ok:
                errs.append({"path": "security", "message": f"credentials {creds or 'none (anonymous)'} satisfy "
                             f"none of {reqs}"})
        return errs, "Old credentials rejected" if errs else "Old credentials still accepted"

    # ------------------------------------------------------------------ request
    def _r_request_parameters(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        if not found:
            return [{"path": "paths", "message": "operation missing in candidate"}], "Operation missing"
        cpath, _, cparams = found
        sent = dict(t.payload["params"])
        # map renamed path parameters by position in the template
        bnames = _template_names(t.locator["path"])
        cnames = _template_names(cpath)
        for bn, cn in zip(bnames, cnames):
            if f"path:{bn}" in sent and bn != cn:
                sent[f"path:{cn}"] = sent.pop(f"path:{bn}")
        lowered = {(k.split(":", 1)[0], k.split(":", 1)[1].lower() if k.startswith("header:") else k.split(":", 1)[1]): v
                   for k, v in sent.items()}
        errs = []
        for p in cparams:
            key = param_key(p)
            required = bool(p.get("required")) or p.get("in") == "path"
            if key not in lowered:
                if required:
                    errs.append({"path": f"{key[0]}:{p.get('name')}", "message": "required parameter not sent by "
                                 "existing clients"})
                continue
            if p.get("schema") is not None:
                for e in validate_instance(lowered[key], p["schema"], self.cand, "request"):
                    errs.append({"path": f"{key[0]}:{p.get('name')}/{e['path']}", "message": e["message"]})
        return errs, ("Old client parameters rejected" if errs else "Old client parameters accepted")

    def _r_request_body_presence(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        rb = found[1].get("requestBody") if found else None
        if rb:
            rb, _ = deref(self.cand, rb)
            if rb.get("required"):
                return [{"path": "requestBody", "message": "candidate requires a request body"}], \
                    "Requests without a body would be rejected"
        return [], "Body still optional"

    def _r_request_media_type(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        rb = found[1].get("requestBody") if found else None
        content = (deref(self.cand, rb)[0].get("content") or {}) if rb else {}
        mt = t.payload["media_type"]
        if mt in content:
            return [], f"{mt} accepted"
        return [{"path": "requestBody/content", "message": f"{mt} not in {sorted(content)}"}], \
            "Clients' Content-Type would be rejected (415)"

    def _r_request_body(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        if not found:
            return [{"path": "paths", "message": "operation missing in candidate"}], "Operation missing"
        schema = body_schema(self.cand, found[1], "request_body", t.locator["media_type"], None)
        if schema is None:
            return [{"path": "requestBody", "message": "candidate has no schema for this media type"}], \
                "No candidate schema"
        errs = validate_instance(t.payload, schema, self.cand, "request")
        return errs, ("Candidate rejects the body existing clients send" if errs
                      else "Candidate accepts the body existing clients send")

    # ------------------------------------------------------------------ response
    def _r_response_status(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        codes = sorted((found[1].get("responses") or {}).keys()) if found else []
        if t.payload["status"] in codes:
            return [], "Status still documented"
        return [{"path": "responses", "message": f"{t.payload['status']} not in {codes}"}], \
            "Success status clients branch on is gone"

    def _r_response_media_type(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        resp = (found[1].get("responses") or {}).get(t.locator["status"]) if found else None
        content = (deref(self.cand, resp)[0].get("content") or {}) if resp else {}
        mt = t.payload["media_type"]
        if mt in content:
            return [], f"{mt} still produced"
        return [{"path": "content", "message": f"{mt} not in {sorted(content)}"}], "Clients' parser would fail"

    def _r_response_header(self, t: TestCase):
        found = find_operation(self.cand, t.locator["method"], t.locator["path"])
        resp = (found[1].get("responses") or {}).get(t.locator["status"]) if found else None
        headers = (deref(self.cand, resp)[0].get("headers") or {}) if resp else {}
        if t.payload["header"] in headers:
            return [], "Header still documented"
        return [{"path": "headers", "message": f"{t.payload['header']} not in {sorted(headers)}"}], \
            "Header no longer documented"

    def _r_response_body(self, t: TestCase):
        found = find_operation(self.base, t.locator["method"], t.locator["path"])
        if not found:
            return [{"path": "paths", "message": "operation missing in base"}], "Operation missing"
        schema = body_schema(self.base, found[1], "response", t.locator["media_type"], t.locator["status"])
        if schema is None:
            return [{"path": "response", "message": "base has no schema for this response"}], "No base schema"
        errs = validate_instance(t.payload, schema, self.base, "response")
        return errs, ("Existing clients' schema rejects the new response" if errs
                      else "Existing clients' schema accepts the new response")


def _split_relevant(errors: list[dict], test: TestCase) -> tuple[list[dict], list[dict]]:
    """Keep only errors at (or directly about) the targeted location; the rest are collateral."""
    loc = test.locator
    if test.kind == "request_parameters" and loc.get("param"):
        pin, pname = loc["param"]
        def rel(e):
            head = e["path"].split("/", 1)[0]
            hin, _, hname = head.partition(":")
            return hin == pin and (hname.lower() == pname.lower() if pin == "header" else hname == pname)
    elif test.kind in ("request_body", "response_body") and loc.get("segments"):
        segs = loc["segments"]
        if any(s.startswith("<") for s in segs):
            return errors, []
        target = ["0" if s == "[]" else s for s in segs]
        leaf = segs[-1]
        def rel(e):
            ep = [] if e["path"] == "(root)" else e["path"].split("/")
            if ep[:len(target)] == target:
                return True
            return target[:len(ep)] == ep and f"'{leaf}'" in e["message"]
    else:
        return errors, []
    relevant = [e for e in errors if rel(e)]
    return relevant, [e for e in errors if not rel(e)]


def _template_names(path: str) -> list[str]:
    import re
    return re.findall(r"\{([^}/]+)\}", path)


def run_tests(base: dict, cand: dict, tests: list[TestCase]) -> list[TestResult]:
    runner = Runner(base, cand)
    return [runner.run(t) for t in tests]
