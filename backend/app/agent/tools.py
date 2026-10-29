"""Tools the investigation agent calls. All deterministic and side-effect free except live HTTP."""
from __future__ import annotations

import copy
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..evidence import Chunk, EvidenceIndex
from ..instances import Generator
from ..oas import inline_schema, is_oas31
from ..runner import validate_instance
from ..testgen import body_schema, find_operation

RESPONSE_READ_RULES = {"RESPONSE_PROPERTY_REMOVED", "PROPERTY_BECAME_OPTIONAL", "NULLABLE_ADDED", "TYPE_CHANGED",
                       "ENUM_VALUE_ADDED", "ENUM_RESTRICTION_REMOVED", "FORMAT_CHANGED", "CONSTRAINT_LOOSENED"}
IMPACT_FINDINGS = {"reads_field", "reads_without_null_check", "enumerates_known_values", "calls_sdk_method",
                   "reads_header", "sends_removed_value", "sends_field", "calls_operation"}


def leaf_field(change: dict) -> str | None:
    segs = [s for s in change.get("segments") or [] if s != "[]" and not s.startswith("<")]
    return segs[-1] if segs else None


def query_terms(change: dict) -> list[str]:
    terms: list[str] = []
    leaf = leaf_field(change)
    if leaf:
        terms += [leaf, leaf]  # weight the field name
    if change.get("component"):
        terms.append(change["component"])
    if change.get("param"):
        terms.append(change["param"][1])
    if change.get("path"):
        terms += [p for p in re.split(r"[/{}]", change["path"]) if p]
    if change["rule"] == "OPERATION_ID_CHANGED":
        terms += [str(change.get("before")), str(change.get("before"))]
    if change["rule"] == "RESPONSE_HEADER_REMOVED":
        terms.append(str(change.get("before")))
    for key in ("before", "after"):
        v = change.get(key)
        if isinstance(v, list):
            terms += [str(x) for x in v if isinstance(x, (str, int))]
    return terms


def broadened_terms(change: dict, base: dict) -> list[str]:
    """Second-attempt query: operation-level vocabulary consumers actually write (SDK method name from the
    operationId, path words, tags, schema component), on top of the focused change terms."""
    terms = list(query_terms(change))
    if change.get("method") and change.get("path"):
        found = find_operation(base, change["method"], change["path"])
        if found:
            op = found[1]
            if op.get("operationId"):
                terms += [str(op["operationId"])] * 2
            terms += [str(t) for t in op.get("tags") or []]
            terms += [w for w in re.split(r"\W+", str(op.get("summary") or "")) if len(w) > 3]
        terms += [p for p in re.split(r"[/{}]", change["path"]) if p] + [change["method"]]
    if change.get("component"):
        terms += [change["component"]] * 2
    return terms


def _lines(chunk: Chunk, pattern: re.Pattern) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(chunk.text.splitlines()):
        if pattern.search(line):
            out.append((chunk.start_line + i, line.strip()[:200]))
    return out


def _field_read(field: str) -> re.Pattern:
    f = re.escape(field)
    return re.compile(rf"(\.{f}\b|\[\s*['\"]{f}['\"]\s*\]|get\(\s*['\"]{f}['\"])")


_NULL_GUARD = re.compile(r"(is None|is not None|== ?null|!= ?null|=== ?null|!== ?null|\?\.|\?\?|Optional\[|if not )")
_HTTP_CALL = re.compile(r"\b(get|post|put|patch|delete)\s*\(", re.I)


def _payload_key(field: str) -> re.Pattern:
    """`"field": value` / `field: value` — the consumer is BUILDING a payload, not reading a response."""
    return re.compile(rf"['\"]{re.escape(field)}['\"]\s*:")


def path_regex(path: str) -> re.Pattern:
    """'/pets/{petId}' -> matches '/pets/123', '/pets/{pet_id}', '/pets/${id}' but not '/pets' or '/pets/1/photos'."""
    parts = []
    for seg in path.strip("/").split("/"):
        parts.append(r"[^/\s\"'?#]+" if seg.startswith("{") else re.escape(seg))
    return re.compile("/" + "/".join(parts) + r"(?![\w/-])")


def operation_lines(change: dict, base: dict | None, text: str) -> list[int]:
    """0-based line indexes in `text` that call the change's operation (path + compatible method, or SDK method)."""
    if not change.get("path") or not change.get("method"):
        return []
    method = change["method"].lower()
    pr = path_regex(change["path"])
    op_id = None
    if base is not None:
        found = find_operation(base, change["method"], change["path"])
        op_id = found[1].get("operationId") if found else None
    out = []
    for i, line in enumerate(text.splitlines()):
        if op_id and re.search(rf"\b{re.escape(op_id)}\s*\(", line):
            out.append(i)
            continue
        if pr.search(line):
            calls = {m.lower() for m in _HTTP_CALL.findall(line)}
            if not calls or method in calls:
                out.append(i)
    return out


def rule_findings(change: dict, chunks: list[Chunk], index: EvidenceIndex | None = None,
                  base: dict | None = None) -> list[dict]:
    """Rule-based reading of retrieved consumer chunks. Every finding cites a chunk and exact line(s), and is only
    counted when the consumer file actually calls the operation the change belongs to."""
    findings: list[dict] = []
    rule, field = change["rule"], leaf_field(change)

    file_calls: dict[str, list[tuple[int, str]]] = {}

    def calls_op(source: str) -> list[tuple[int, str]]:
        if source not in file_calls:
            hits = []
            for ch in (index.chunks if index else chunks):
                if ch.source == source and ch.kind == "context":
                    lines = ch.text.splitlines()
                    hits += [(ch.start_line + i, lines[i].strip()[:200]) for i in operation_lines(change, base, ch.text)]
            file_calls[source] = sorted(set(hits))
        return file_calls[source]

    def add(chunk: Chunk, kind: str, lines: list[tuple[int, str]], detail: str, **extra) -> None:
        via = calls_op(chunk.source)
        findings.append({"chunk_id": chunk.id, "citation": f"{chunk.source}:{lines[0][0]}" if lines else chunk.cite(),
                         "finding": kind, "lines": [{"line": n, "text": t} for n, t in lines[:4]],
                         "detail": detail,
                         "operation_call": {"line": via[0][0], "text": via[0][1]} if via else None, **extra})

    for c in chunks:
        if c.kind != "context" or not calls_op(c.source):
            continue  # this consumer never calls the affected operation
        if change["direction"] == "response" and rule in ("ENUM_VALUE_ADDED", "ENUM_RESTRICTION_REMOVED"):
            old = [v for v in (change.get("before") or []) if isinstance(v, str)]
            new = [v for v in (change.get("after") or []) if isinstance(v, str) and v not in old]
            present = [v for v in old if re.search(rf"['\"]{re.escape(v)}['\"]", c.text)]
            handles_new = [v for v in new if re.search(rf"['\"]{re.escape(v)}['\"]", c.text)]
            if len(present) >= 2 and not handles_new:
                pat = re.compile("|".join(rf"['\"]{re.escape(v)}['\"]" for v in present))
                add(c, "enumerates_known_values", _lines(c, pat),
                    f"Consumer handles known values {present} but not the new value(s) {new}", handled_values=present)
            elif handles_new:
                pat = re.compile("|".join(rf"['\"]{re.escape(v)}['\"]" for v in handles_new))
                add(c, "handles_new_value", _lines(c, pat), f"Consumer already references {handles_new}")
            continue
        if change["direction"] == "response" and field and rule in RESPONSE_READ_RULES:
            building = _payload_key(field)
            hits = [h for h in _lines(c, _field_read(field)) if not building.search(h[1])]
            if hits:
                if rule == "NULLABLE_ADDED":
                    if _NULL_GUARD.search(c.text):
                        add(c, "reads_with_null_check", hits, f"Consumer reads '{field}' and guards against null")
                    else:
                        add(c, "reads_without_null_check", hits,
                            f"Consumer reads '{field}' with no null handling visible in the snippet")
                else:
                    add(c, "reads_field", hits, f"Consumer reads '{field}' from the response")
            continue
        if rule == "OPERATION_ID_CHANGED" and change.get("before"):
            hits = _lines(c, re.compile(rf"\b{re.escape(str(change['before']))}\s*\("))
            if hits:
                add(c, "calls_sdk_method", hits, f"Consumer calls generated method '{change['before']}()'")
            continue
        if rule == "RESPONSE_HEADER_REMOVED" and change.get("before"):
            hits = _lines(c, re.compile(re.escape(str(change["before"])), re.I))
            if hits:
                add(c, "reads_header", hits, f"Consumer reads header '{change['before']}'")
            continue
        if change["direction"] in ("request", "surface"):
            if rule == "ENUM_VALUE_REMOVED":
                removed = [v for v in change.get("before") or [] if v not in (change.get("after") or [])]
                name = (change.get("param") or [None, None])[1] or field
                if removed and name:
                    vals = "|".join(re.escape(str(v)) for v in removed)
                    pat = re.compile(rf"['\"]?{re.escape(name)}['\"]?\s*[:=]\s*['\"]({vals})['\"]", re.I)
                    hits = _lines(c, pat)
                    if hits:
                        add(c, "sends_removed_value", hits, f"Consumer sends removed value(s) {removed} as '{name}'")
                        continue
            if field and rule in ("REQUEST_PROPERTY_REMOVED", "CONSTRAINT_TIGHTENED", "TYPE_CHANGED",
                                  "PATTERN_CHANGED", "FORMAT_CHANGED") and change.get("area") == "request_body":
                hits = _lines(c, re.compile(rf"['\"]?\b{re.escape(field)}\b['\"]?\s*[:=]"))
                if hits:
                    add(c, "sends_field", hits, f"Consumer sets '{field}' in the request body")
                    continue
            if change.get("param") and rule in ("CONSTRAINT_TIGHTENED", "TYPE_CHANGED", "PATTERN_CHANGED"):
                pname = change["param"][1]
                hits = _lines(c, re.compile(rf"['\"]?\b{re.escape(pname)}\b['\"]?\s*[:=]", re.I))
                if hits:
                    add(c, "sends_field", hits, f"Consumer sends parameter '{pname}'")
                    continue
            via = calls_op(c.source)
            in_chunk = [(n, t) for n, t in via if c.start_line <= n <= c.end_line]
            if in_chunk:
                add(c, "calls_operation", in_chunk,
                    f"Consumer calls {change['method'].upper()} {change['path']}")
    # one finding per (file, kind, first line): overlapping chunks repeat the same lines
    seen, unique = set(), []
    for f in findings:
        key = (f["citation"], f["finding"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


_LITERAL = r"(-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"|true|false|True|False)"


def extract_literal(name: str, lines: list[dict]) -> tuple[bool, Any]:
    """Find `name: <literal>` / `name=<literal>` in cited lines. Returns (found, value)."""
    pat = re.compile(rf"['\"]?\b{re.escape(name)}\b['\"]?\s*[:=]\s*{_LITERAL}", re.I)
    for line in lines:
        m = pat.search(line["text"])
        if m:
            raw = m.group(1)
            if raw[0] in "'\"":
                return True, raw[1:-1]
            if raw.lower() in ("true", "false"):
                return True, raw.lower() == "true"
            return True, float(raw) if "." in raw else int(raw)
    return False, None


def derive_consumer_schema(base: dict, change: dict, findings: list[dict]) -> tuple[Any, list[dict]] | None:
    """Base response schema, tightened to what cited consumer code relies on."""
    found = find_operation(base, change["method"], change["path"])
    if not found or change.get("area") != "response":
        return None
    schema = body_schema(base, found[1], "response", change.get("media_type"), change.get("status"))
    if schema is None:
        return None
    root = copy.deepcopy(inline_schema(base, schema))
    segs = change.get("segments") or []
    if not segs:
        return None
    parent = root
    for seg in segs[:-1]:
        if seg == "[]":
            parent = parent.setdefault("items", {})
        elif seg.startswith("<"):
            return None
        else:
            parent = parent.setdefault("properties", {}).setdefault(seg, {})
    leaf = segs[-1]
    if leaf == "[]":
        return None
    leaf_schema = parent.setdefault("properties", {}).setdefault(leaf, {})
    steps = []
    kinds = {f["finding"] for f in findings}
    cites = lambda k: [f["chunk_id"] for f in findings if f["finding"] == k]
    if kinds & {"reads_field", "reads_without_null_check"}:
        req = parent.setdefault("required", [])
        if leaf not in req:
            req.append(leaf)
        steps.append({"constraint": f"'{leaf}' must be present (consumer reads it)",
                      "evidence": cites("reads_field") + cites("reads_without_null_check")})
    if "reads_without_null_check" in kinds:
        leaf_schema["nullable"] = False
        t = leaf_schema.get("type")
        if isinstance(t, list):
            leaf_schema["type"] = [x for x in t if x != "null"]
        steps.append({"constraint": f"'{leaf}' must not be null (no null guard in consumer)",
                      "evidence": cites("reads_without_null_check")})
    handled = sorted({v for f in findings if f["finding"] == "enumerates_known_values" for v in f.get("handled_values", [])})
    if handled:
        leaf_schema["enum"] = handled
        steps.append({"constraint": f"'{leaf}' must be one of {handled} (values the consumer handles)",
                      "evidence": cites("enumerates_known_values")})
    return (root, steps) if steps else None


def validate_against_derived(payload: Any, schema: Any, base: dict) -> list[dict]:
    return validate_instance(payload, schema, base, "response")


# ---------------------------------------------------------------------------- live HTTP
def allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in os.environ.get("SENTINEL_LIVE_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
            if h.strip()}


def check_target(url: str) -> str | None:
    """SSRF guard: only http(s) to explicitly allow-listed hosts. Returns an error string or None."""
    try:
        u = urlparse(url)
    except ValueError:
        return "Target URL is not a valid URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "Target URL must be http(s)://host[:port][/base-path]"
    if u.username or u.password:
        return "Credentials in the target URL are not allowed"
    if u.hostname.lower() not in allowed_hosts():
        return (f"Host '{u.hostname}' is not in SENTINEL_LIVE_ALLOWED_HOSTS ({', '.join(sorted(allowed_hosts()))}). "
                "Add it explicitly to test a remote deployment.")
    return None


def _fill_path(path: str, params: dict) -> str:
    def repl(m):
        return str(params.get(f"path:{m.group(1)}", 1))
    return re.sub(r"\{([^}/]+)\}", repl, path)


def _relevant_live(errors: list[dict], segs: list[str]) -> tuple[list[dict], list[dict]]:
    """Errors at the changed location ('[]' matches any array index); everything else is collateral."""
    if not segs or any(s.startswith("<") for s in segs):
        return errors, []
    leaf = segs[-1]

    def match(ep: list[str], tp: list[str]) -> bool:
        return len(ep) >= len(tp) and all(t == "[]" and e.isdigit() or t == e for e, t in zip(ep, tp))

    rel, coll = [], []
    for e in errors:
        ep = [] if e["path"] == "(root)" else e["path"].split("/")
        parent_ok = len(ep) == len(segs) - 1 and match(ep + [leaf], segs) and f"'{leaf}'" in e["message"]
        (rel if match(ep, segs) or parent_ok else coll).append(e)
    return rel, coll


def live_request(target: str, base: dict, cand: dict, test: dict, change: dict) -> dict:
    """Replay an existing client's call against a running deployment and judge the reply by the OLD contract.

    Isolation: the request also satisfies anything else the candidate newly requires (except the change under
    test), so a failure is attributable to this change. Attribution rules:
      * a non-2xx status counts only against request-side/surface changes (401/403 only against security changes);
        for response-side changes it means the response could not be observed -> inconclusive;
      * body errors count only at the changed location.
    """
    loc = test["locator"]
    method, path = loc["method"], loc["path"]
    found_b = find_operation(base, method, path)
    found_c = find_operation(cand, method, path)
    if not found_b:
        return {"status": "error", "summary": "operation not in base spec", "errors": []}
    gen = Generator(base, "request", is_oas31(base))
    params = dict(test["payload"].get("params", {})) if test["kind"] == "request_parameters" else {}
    for p in found_b[2]:  # old client always sends its required params
        k = f"{p.get('in')}:{p.get('name')}"
        if (p.get("required") or p.get("in") == "path") and k not in params:
            params[k] = gen.default(p.get("schema") or {})
    isolation = []
    target_param = tuple(x.lower() for x in change.get("param") or ()) if change.get("area") == "parameter" else ()
    if found_c:
        cgen = Generator(cand, "request", is_oas31(cand))
        have = {k.lower() for k in params}
        for p in found_c[2]:
            k = f"{p.get('in')}:{p.get('name')}"
            if p.get("required") and p.get("in") != "path" and k.lower() not in have and \
                    (p.get("in", "").lower(), p.get("name", "").lower()) != target_param:
                params[k] = cgen.default(p.get("schema") or {})
                isolation.append(k)
    url_path = _fill_path(found_b[0], params)
    query = {k.split(":", 1)[1]: v for k, v in params.items() if k.startswith("query:")}
    headers = {k.split(":", 1)[1]: str(v) for k, v in params.items() if k.startswith("header:")}
    body = None
    if test["kind"] == "request_body":
        body = test["payload"]
    elif found_b[1].get("requestBody"):
        bs = body_schema(base, found_b[1], "request_body", "application/json", None)
        if bs is not None:
            body = gen.default(bs)
            cs = body_schema(cand, found_c[1], "request_body", "application/json", None) if found_c else None
            if cs is not None and isinstance(body, dict):
                body = _fill_all(body, cand, cs)
    url = target.rstrip("/") + url_path
    request = {"method": method.upper(), "url": url, "query": query, "headers": headers, "body": body,
               "isolation_params": isolation}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False) as client:
            r = client.request(method.upper(), url, params=query, headers=headers,
                               json=body if body is not None else None)
    except httpx.TimeoutException:
        return {"status": "timeout", "summary": f"No reply from {url} within 5 s; the deployment did not answer, "
                "so nothing about compatibility can be concluded", "errors": [], "request": request}
    except httpx.HTTPError as exc:
        return {"status": "infra_error", "summary": f"Could not reach the deployment ({type(exc).__name__}); "
                "infrastructure failure, not a compatibility result", "errors": [], "request": request}
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    raw = r.content[:1_000_000]
    code = str(r.status_code)
    response = {"status": r.status_code, "body_excerpt": raw[:600].decode("utf-8", "replace")}
    head = f"{method.upper()} {url_path} → {r.status_code} in {elapsed} ms"
    if not code.startswith("2"):
        security = change.get("area") == "security"
        auth_block = r.status_code in (401, 403)
        attributable = change["direction"] in ("request", "surface") and (auth_block == security)
        if attributable:
            return {"status": "failed", "summary": f"{head}; existing clients' call is rejected",
                    "errors": [{"path": "status", "message": f"got {code}; old contract documents "
                                f"{sorted((found_b[1].get('responses') or {}).keys())}"}],
                    "request": request, "response": response}
        why = "blocked by authentication (no credentials are sent)" if auth_block else \
            "the response under test could not be observed"
        return {"status": "inconclusive", "summary": f"{head}; {why}", "errors": [], "request": request,
                "response": response}
    if change["rule"] == "RESPONSE_HEADER_REMOVED":
        name = str(change.get("before"))
        present = name.lower() in {h.lower() for h in r.headers.keys()}
        return {"status": "passed" if present else "failed",
                "summary": f"{head}; header '{name}' {'still sent' if present else 'missing from the reply'}",
                "errors": [] if present else [{"path": f"headers/{name}", "message": "header not present in live reply"}],
                "request": request, "response": {**response, "headers": dict(list(r.headers.items())[:20])}}
    errors: list[dict] = []
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    schema = body_schema(base, found_b[1], "response", ctype, code) if ctype else None
    if schema is not None and raw:
        try:
            errors = validate_instance(r.json(), schema, base, "response")
        except ValueError:
            errors = [{"path": "(root)", "message": "response is not valid JSON"}]
    rel, coll = (_relevant_live(errors, change.get("segments") or [])
                 if change["direction"] == "response" else (errors, []))
    return {"status": "failed" if rel else "passed",
            "summary": f"{head}; " + ("reply violates the old contract at the changed location" if rel
                                      else "old contract satisfied at the changed location"),
            "errors": rel[:20], "collateral": coll[:20], "request": request, "response": response}


def _fill_all(body: dict, cand: dict, schema: Any) -> dict:
    """Add every property the candidate requires at the top level (isolation for live replays)."""
    from ..instances import merged
    s = merged(cand, schema)
    cgen = Generator(cand, "request", is_oas31(cand))
    for name in s.get("required") or []:
        props = s.get("properties") or {}
        if name not in body and name in props:
            body[name] = cgen.default(props[name])
    return body
