"""Evaluation suite: labelled spec mutations (detection + baseline decision) and agent cases (escalation).

Labels come from how each mutation was designed (what an expert expects that edit to mean for existing clients),
written independently of the engine's output. Disagreements are reported, never hidden.

    python -m app.evaluation                 # print summary
    python -m app.evaluation --markdown ../docs/EVALUATION.md
"""
from __future__ import annotations

import argparse
import copy
import json
import time
from dataclasses import dataclass, field
from typing import Callable

from .agent.graph import run_investigation
from .config import settings
from .evidence import build_index
from .pipeline import run_analysis
from .spec_loader import load_spec

B, P, N = "BREAKING", "POTENTIALLY_BREAKING", "NON_BREAKING"
ITEM_OPS = ("GET /items", "POST /items", "GET /items/{itemId}")  # operations returning an Item


def base_spec() -> dict:
    item = {"type": "object", "required": ["id", "sku", "name", "qty"], "properties": {
        "id": {"type": "string", "readOnly": True},
        "sku": {"type": "string"},
        "name": {"type": "string", "maxLength": 80},
        "qty": {"type": "integer", "minimum": 0},
        "color": {"type": "string", "enum": ["red", "green", "blue"]},
        "createdAt": {"type": "string", "format": "date-time"}}}
    new_item = {"type": "object", "required": ["sku", "name"], "properties": {
        "sku": {"type": "string"},
        "name": {"type": "string", "maxLength": 80},
        "qty": {"type": "integer", "minimum": 0},
        "color": {"type": "string", "enum": ["red", "green", "blue"]}}}

    def ok(schema, code="200"):
        return {code: {"description": "ok", "content": {"application/json": {"schema": schema}}}}
    item_ref = {"$ref": "#/components/schemas/Item"}
    item_id = {"name": "itemId", "in": "path", "required": True, "schema": {"type": "string"}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Inventory API", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "security": [{"apiKey": []}],
        "paths": {
            "/items": {
                "get": {"operationId": "listItems", "security": [], "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}}],
                    "responses": ok({"type": "object", "required": ["items"], "properties": {
                        "items": {"type": "array", "items": item_ref},
                        "next": {"type": "string", "nullable": True}}})},
                "post": {"operationId": "createItem",
                         "requestBody": {"required": True, "content": {"application/json": {
                             "schema": {"$ref": "#/components/schemas/NewItem"}}}},
                         "responses": ok(item_ref, "201")}},
            "/items/{itemId}": {
                "get": {"operationId": "getItem", "parameters": [item_id],
                        "responses": {**ok(item_ref), "404": {"description": "not found"}}},
                "delete": {"operationId": "deleteItem", "parameters": [item_id],
                           "responses": {"204": {"description": "deleted"}}}}},
        "components": {"schemas": {"Item": item, "NewItem": new_item},
                       "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    }


@dataclass
class Case:
    name: str
    why: str
    mutate: Callable[[dict], None]
    expected: list[tuple[str, str, str, str | None]]  # (rule, severity, "METHOD /path" or "", field-or-None)
    decision: str
    to_31: bool = False


def _items(d):
    return d["components"]["schemas"]["Item"]


def _new(d):
    return d["components"]["schemas"]["NewItem"]


def _get_items(d):
    return d["paths"]["/items"]["get"]


def _rename_param(d):
    d["paths"]["/items/{id}"] = d["paths"].pop("/items/{itemId}")
    for op in d["paths"]["/items/{id}"].values():
        op["parameters"] = [{**p, "name": "id"} for p in op["parameters"]]


def _to_31(d):
    d["openapi"] = "3.1.0"
    nxt = _get_items(d)["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["next"]
    nxt.pop("nullable")
    nxt["type"] = ["string", "null"]


def _per_item_op(rule, sev, fld):
    return [(rule, sev, op, fld) for op in ITEM_OPS]


CASES: list[Case] = [
    Case("no-change", "Identical specs.", lambda d: None, [], "PROCEED"),
    Case("operation-removed", "DELETE removed; other method on the path remains.",
         lambda d: d["paths"]["/items/{itemId}"].pop("delete"),
         [("OPERATION_REMOVED", B, "DELETE /items/{itemId}", None)], "BLOCK"),
    Case("path-removed", "Whole path removed (two operations).", lambda d: d["paths"].pop("/items/{itemId}"),
         [("PATH_REMOVED", B, "GET /items/{itemId}", None), ("PATH_REMOVED", B, "DELETE /items/{itemId}", None)],
         "BLOCK"),
    Case("required-request-field", "New required field in the create body.",
         lambda d: (_new(d)["properties"].update(warehouse={"type": "string"}), _new(d)["required"].append("warehouse")),
         [("REQUIRED_PROPERTY_ADDED", B, "POST /items", "warehouse")], "BLOCK"),
    Case("optional-request-field", "New optional field in the create body.",
         lambda d: _new(d)["properties"].update(notes={"type": "string"}),
         [("PROPERTY_ADDED", N, "POST /items", "notes")], "PROCEED"),
    Case("request-enum-value-removed", "Clients may still send 'blue'.",
         lambda d: _new(d)["properties"]["color"]["enum"].remove("blue"),
         [("ENUM_VALUE_REMOVED", B, "POST /items", "color")], "BLOCK"),
    Case("response-enum-value-added", "Exhaustive clients may not know 'black'.",
         lambda d: _items(d)["properties"]["color"]["enum"].append("black"),
         _per_item_op("ENUM_VALUE_ADDED", P, "color"), "HUMAN_REVIEW"),
    Case("optional-response-field-removed", "Optional field gone: a schema check cannot see it; needs judgement.",
         lambda d: _items(d)["properties"].pop("createdAt"),
         _per_item_op("RESPONSE_PROPERTY_REMOVED", B, "createdAt"), "HUMAN_REVIEW"),
    Case("response-field-became-optional", "qty may now be absent.",
         lambda d: _items(d)["required"].remove("qty"),
         _per_item_op("PROPERTY_BECAME_OPTIONAL", B, "qty"), "BLOCK"),
    Case("response-type-changed", "qty integer → string.",
         lambda d: _items(d)["properties"]["qty"].update(type="string"),
         _per_item_op("TYPE_CHANGED", B, "qty"), "BLOCK"),
    Case("request-type-widened", "NewItem.qty integer → number accepts everything it did before.",
         lambda d: _new(d)["properties"]["qty"].update(type="number"),
         [("TYPE_WIDENED", N, "POST /items", "qty")], "PROCEED"),
    Case("param-max-tightened", "limit max 100 → 50 rejects limit=100.",
         lambda d: _get_items(d)["parameters"][0]["schema"].update(maximum=50),
         [("CONSTRAINT_TIGHTENED", B, "GET /items", None)], "BLOCK"),
    Case("param-max-loosened", "limit max 100 → 500.",
         lambda d: _get_items(d)["parameters"][0]["schema"].update(maximum=500),
         [("CONSTRAINT_LOOSENED", N, "GET /items", None)], "PROCEED"),
    Case("required-query-param-added", "Old clients never send 'region'.",
         lambda d: _get_items(d)["parameters"].append(
             {"name": "region", "in": "query", "required": True, "schema": {"type": "string"}}),
         [("REQUIRED_PARAM_ADDED", B, "GET /items", None)], "BLOCK"),
    Case("security-added", "Public list endpoint now inherits the API-key requirement.",
         lambda d: _get_items(d).pop("security"),
         [("SECURITY_ADDED", B, "GET /items", None)], "BLOCK"),
    Case("response-nullable-added", "name may now be null in responses.",
         lambda d: _items(d)["properties"]["name"].update(nullable=True),
         _per_item_op("NULLABLE_ADDED", P, "name"), "HUMAN_REVIEW"),
    Case("success-status-changed", "Create returns 200 instead of 201.",
         lambda d: d["paths"]["/items"]["post"]["responses"].update(
             {"200": d["paths"]["/items"]["post"]["responses"].pop("201")}),
         [("SUCCESS_RESPONSE_REMOVED", B, "POST /items", None), ("RESPONSE_STATUS_ADDED", P, "POST /items", None)],
         "BLOCK"),
    Case("response-media-type-changed", "GET item now returns application/hal+json only.",
         lambda d: d["paths"]["/items/{itemId}"]["get"]["responses"]["200"].update(
             content={"application/hal+json": d["paths"]["/items/{itemId}"]["get"]["responses"]["200"]["content"]
                      .pop("application/json")}),
         [("RESPONSE_MEDIA_TYPE_REMOVED", B, "GET /items/{itemId}", None),
          ("RESPONSE_MEDIA_TYPE_ADDED", N, "GET /items/{itemId}", None)], "BLOCK"),
    # traps: edits that look alarming but do not affect existing clients
    Case("trap-readonly-required-in-request", "readOnly 'id' added as required to NewItem: clients never send "
         "readOnly fields, so nothing changes for them.",
         lambda d: (_new(d)["properties"].update(id={"type": "string", "readOnly": True}), _new(d)["required"].append("id")),
         [], "PROCEED"),
    Case("trap-path-param-renamed", "{itemId} → {id}: same URLs on the wire.", _rename_param,
         [("PATH_PARAM_RENAMED", N, "GET /items/{itemId}", None), ("PATH_PARAM_RENAMED", N, "DELETE /items/{itemId}", None)],
         "PROCEED"),
    Case("oas31-nullable-via-type-array", "OAS 3.1: name type becomes ['string','null'] in responses.",
         lambda d: _items(d)["properties"]["name"].update(type=["string", "null"]),
         _per_item_op("NULLABLE_ADDED", P, "name"), "HUMAN_REVIEW", to_31=True),
]


def _load(doc: dict, name: str):
    spec = load_spec(json.dumps(doc), name)
    if not spec.valid:
        raise AssertionError(f"evaluation fixture {name} is invalid: {spec.errors}")
    return spec


def _matches(exp: tuple, ch: dict) -> bool:
    rule, _sev, op, fld = exp
    if ch["rule"] != rule:
        return False
    if op and not ch["location"].startswith(op):
        return False
    if fld and fld not in ch["location"].rsplit(" › ", 1)[-1]:
        return False
    return True


def run_detection() -> dict:
    rows, tp, fp, fn, sev_ok, dec_ok = [], 0, 0, 0, 0, 0
    analyses: dict[str, dict] = {}
    confusion: dict[str, dict[str, int]] = {}
    for case in CASES:
        base = base_spec()
        if case.to_31:
            _to_31(base)
        cand = copy.deepcopy(base)
        case.mutate(cand)
        a = run_analysis(_load(base, f"{case.name}-base"), _load(cand, f"{case.name}-cand"))
        analyses[case.name] = a
        changes = a["changes"]
        used, missing, wrong_sev = set(), [], []
        for exp in case.expected:
            hit = next((c for c in changes if c["id"] not in used and _matches(exp, c)), None)
            if hit is None:
                missing.append(f"{exp[0]} @ {exp[2] or 'document'}{' › ' + exp[3] if exp[3] else ''}")
                continue
            used.add(hit["id"])
            if hit["severity"] == exp[1]:
                sev_ok += 1
            else:
                wrong_sev.append(f"{hit['rule']} @ {hit['location']}: expected {exp[1]}, got {hit['severity']}")
        extra = [f"{c['rule']} ({c['severity']}) @ {c['location']}" for c in changes if c["id"] not in used]
        decision = a["recommendation"]["decision"]
        tp += len(used)
        fn += len(missing)
        fp += len(extra)
        dec_ok += decision == case.decision
        confusion.setdefault(case.decision, {}).setdefault(decision, 0)
        confusion[case.decision][decision] += 1
        rows.append({"case": case.name, "why": case.why, "oas": "3.1" if case.to_31 else "3.0",
                     "expected_changes": len(case.expected), "detected": len(changes), "matched": len(used),
                     "missing": missing, "unexpected": extra, "severity_mismatches": wrong_sev,
                     "expected_decision": case.decision, "decision": decision,
                     "tests": a["recommendation"]["counts"]["tests"],
                     "pass": not missing and not extra and not wrong_sev and decision == case.decision})
    expected_total = sum(len(c.expected) for c in CASES)
    return {
        "cases": rows,
        "metrics": {
            "cases": len(CASES), "cases_fully_correct": sum(r["pass"] for r in rows),
            "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
            "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
            "severity_accuracy": round(sev_ok / tp, 4) if tp else 1.0,
            "decision_accuracy": round(dec_ok / len(CASES), 4),
            "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "expected_changes": expected_total,
        },
        "decision_confusion": confusion,
        "baselines": detection_baselines(rows, analyses),
    }


DECOY = '''"""Decoy consumer: talks to a *different* service (shipments) that happens to use the same field names."""
import requests

def track(shipment_id):
    s = requests.get(f"https://ship.example.com/shipments/{shipment_id}").json()
    if s["state"] == "placed":
        return "waiting"
    elif s["state"] == "shipped":
        return s["trackingUrl"].removeprefix("https://")
    raise ValueError(s["state"])
'''

def noise_files() -> list[tuple[str, str]]:
    """Six unrelated services that share the Orders vocabulary (state, trackingUrl, total) but never call the
    Orders API. They crowd the focused retrieval and test both precision (no false escalation) and the retry."""
    out = []
    for svc in ("shipments", "returns", "carrier_feed", "warehouse", "notifications", "analytics"):
        out.append((f"{svc}_handler.py", f'''"""{svc} service: unrelated to the Orders API, same vocabulary."""
import requests

def handle_{svc}(item_id):
    record = requests.get(f"https://{svc}.example.com/{svc}/{{item_id}}").json()
    state = record["state"]
    if state == "placed":
        return record["trackingUrl"]
    if state == "shipped":
        return record["trackingUrl"].lower()
    if state == "paid":
        return record["state"]
    return record["trackingUrl"], record["state"], record["total"]
'''))
    return out


# affected: change ids whose consumers are really broken, labelled by reading the fixture code (None = not labelled)
ORDERS_AFFECTED = ["C003", "C004", "C005"]  # getOrder() renamed; exhaustive state switch; trackingUrl.removeprefix
AGENT_CASES = [
    {"name": "orders-consumer-relies-on-changes", "sample": "orders-31-review", "context": "sample",
     "expected": "BLOCK", "affected": ORDERS_AFFECTED,
     "why": "billing worker calls getOrder(), handles only old states and dereferences trackingUrl without a null "
            "check; the widget null-checks and only displays the state"},
    {"name": "orders-noisy-consumers", "sample": "orders-31-review", "context": "noisy", "expected": "BLOCK",
     "affected": ORDERS_AFFECTED,
     "why": "same consumers hidden among six unrelated services that use the same field names"},
    {"name": "orders-decoy-consumer", "sample": "orders-31-review", "context": "decoy", "expected": "HUMAN_REVIEW",
     "affected": [], "why": "same field names but a different API: must NOT escalate"},
    {"name": "orders-no-context", "sample": "orders-31-review", "context": "none", "expected": "HUMAN_REVIEW",
     "affected": [], "why": "no evidence: absence of evidence is not safety"},
    {"name": "petstore-breaking-with-consumers", "sample": "petstore-breaking", "context": "sample",
     "expected": "BLOCK", "affected": None, "why": "already blocked; agent must not de-escalate"},
    {"name": "petstore-safe", "sample": "petstore-safe", "context": "none", "expected": "PROCEED", "affected": [],
     "why": "nothing to investigate"},
]
SYSTEMS = [
    ("deterministic", "Deterministic only (diff + contract tests, no consumer analysis)"),
    ("keyword_rag", "Keyword RAG: escalate when retrieved code mentions the changed field (no verification)"),
    ("agent_no_retry", "Agent, rules mode, retries disabled"),
    ("agent", "Agent, rules mode (full workflow)"),
]
AFFECTED_OUTCOMES = {"consumer_break_confirmed", "consumer_usage_found", "affected_consumers_identified",
                     "live_break_confirmed"}


def _case_inputs(case: dict):
    d = settings.samples_dir / case["sample"]
    base = load_spec(next(d.glob("base.*")).read_bytes(), "base")
    cand = load_spec(next(d.glob("candidate.*")).read_bytes(), "candidate")
    sample_files = [(p.name, p.read_text(encoding="utf-8")) for p in sorted((d / "context").iterdir())] \
        if (d / "context").is_dir() else []
    files = {"sample": sample_files, "decoy": [("shipment_tracker.py", DECOY)], "none": [],
             "noisy": noise_files() + sample_files}[case["context"]]
    return base, cand, files


def _keyword_rag(a: dict, index, files) -> tuple[str, list[str]]:
    from .agent.tools import leaf_field, query_terms
    if not files:
        return a["recommendation"]["decision"], []
    hit = []
    for c in a["changes"]:
        if c["severity"] == "NON_BREAKING":
            continue
        token = leaf_field(c) or (str(c["before"]) if c["rule"] == "OPERATION_ID_CHANGED" else None)
        if not token:
            continue
        chunks = index.search(query_terms(c), k=6, kinds=("context",))
        if any(token in ch.text for ch, _ in chunks):
            hit.append(c["id"])
    return ("BLOCK" if hit else a["recommendation"]["decision"]), hit


def run_agent() -> dict:
    rows = []
    for case in AGENT_CASES:
        base, cand, files = _case_inputs(case)
        a = run_analysis(base, cand)
        index = build_index(base.doc, cand.doc, files)
        names = [n for n, _ in files]
        systems: dict[str, dict] = {"deterministic": {"decision": a["recommendation"]["decision"], "affected": []}}
        kd, kh = _keyword_rag(a, index, files)
        systems["keyword_rag"] = {"decision": kd, "affected": kh}
        for key, retries in (("agent_no_retry", 0), ("agent", 1)):
            inv = run_investigation(base.doc, cand.doc, a, index, names, None, llm=None, max_retries=retries)
            systems[key] = {"decision": inv["decision"]["decision"],
                            "affected": sorted(r["change_id"] for r in inv["records"] if r["outcome"] in AFFECTED_OUTCOMES),
                            "retries": sum(1 for e in inv["trace"] if e["node"] == "reflect"
                                           and e["detail"].get("decision") == "retry"),
                            "trace_steps": len(inv["trace"])}
        baseline = a["recommendation"]["decision"]
        for sysd in systems.values():
            sysd["pass"] = sysd["decision"] == case["expected"]
            sysd["false_escalation"] = sysd["decision"] == "BLOCK" and baseline != "BLOCK" and case["expected"] != "BLOCK"
        full = systems["agent"]
        rows.append({**{k: case[k] for k in ("name", "sample", "context", "expected", "why", "affected")},
                     "baseline": baseline, "agent": full["decision"], "pass": full["pass"],
                     "escalated": full["decision"] == "BLOCK" and baseline != "BLOCK",
                     "trace_steps": full["trace_steps"], "systems": systems})
    comparison = []
    for key, label in SYSTEMS:
        tp = fp = fn = 0
        for r in rows:
            if r["affected"] is None:
                continue
            pred, truth = set(r["systems"][key]["affected"]), set(r["affected"])
            tp, fp, fn = tp + len(pred & truth), fp + len(pred - truth), fn + len(truth - pred)
        comparison.append({
            "system": key, "label": label,
            "decision_accuracy": round(sum(r["systems"][key]["pass"] for r in rows) / len(rows), 4),
            "correct": sum(r["systems"][key]["pass"] for r in rows), "cases": len(rows),
            "false_escalations": sum(r["systems"][key]["false_escalation"] for r in rows),
            "impact_precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "impact_recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "impact_tp": tp, "impact_fp": fp, "impact_fn": fn})
    full = next(c for c in comparison if c["system"] == "agent")
    return {"cases": rows, "comparison": comparison,
            "metrics": {"cases": len(rows), "correct": full["correct"], "accuracy": full["decision_accuracy"],
                        "false_escalations": full["false_escalations"], "impact_recall": full["impact_recall"],
                        "impact_precision": full["impact_precision"], "reasoning_mode": "rules (LLM disabled)"}}


ADDITIVE = {"PATH_ADDED", "OPERATION_ADDED", "PROPERTY_ADDED", "OPTIONAL_PARAM_ADDED", "REQUEST_MEDIA_TYPE_ADDED",
            "RESPONSE_MEDIA_TYPE_ADDED", "RESPONSE_STATUS_ADDED", "PATH_PARAM_RENAMED"}


def detection_baselines(rows: list[dict], analyses: dict[str, dict]) -> list[dict]:
    """Two simpler release gates over the SAME detected changes, to show what classification and tests add."""
    out = []
    for key, label, fn in (
        ("naive_gate", "Naive gate: any non-additive change blocks", lambda a: "BLOCK" if any(
            c["rule"] not in ADDITIVE for c in a["changes"]) else "PROCEED"),
        ("severity_only", "Direction-aware severity, no contract tests", lambda a: "BLOCK" if any(
            c["severity"] == B for c in a["changes"]) else "HUMAN_REVIEW" if any(
            c["severity"] == P for c in a["changes"]) else "PROCEED"),
        ("full", "API Sentinel baseline (severity + executed contract tests)",
         lambda a: a["recommendation"]["decision"])):
        got = {r["case"]: fn(analyses[r["case"]]) for r in rows}
        wrong = [f"{r['case']}: {got[r['case']]} (expected {r['expected_decision']})" for r in rows
                 if got[r["case"]] != r["expected_decision"]]
        out.append({"system": key, "label": label, "correct": len(rows) - len(wrong), "cases": len(rows),
                    "decision_accuracy": round((len(rows) - len(wrong)) / len(rows), 4), "wrong": wrong})
    return out


def run_all() -> dict:
    t = time.perf_counter()
    out = {"detection": run_detection(), "agent": run_agent()}
    out["duration_ms"] = round((time.perf_counter() - t) * 1000, 1)
    return out


def to_markdown(r: dict) -> str:
    m, am = r["detection"]["metrics"], r["agent"]["metrics"]
    lines = [
        "# API Sentinel — Evaluation report", "",
        "Generated by `python -m app.evaluation --markdown ../docs/EVALUATION.md`. Labels encode what each mutation",
        "was designed to mean for existing clients; they were written independently of the engine output.", "",
        "## Change detection and baseline decision", "",
        f"* Cases: **{m['cases']}**, fully correct: **{m['cases_fully_correct']}**",
        f"* Precision **{m['precision']:.2%}**, recall **{m['recall']:.2%}** "
        f"({m['true_positives']} TP, {m['false_positives']} FP, {m['false_negatives']} FN over "
        f"{m['expected_changes']} expected changes)",
        f"* Severity accuracy on matched changes: **{m['severity_accuracy']:.2%}**",
        f"* Baseline decision accuracy: **{m['decision_accuracy']:.2%}**", "",
        "| Case | OAS | Expected | Got | Changes (exp/got) | Result | Notes |",
        "|---|---|---|---|---|---|---|"]
    for c in r["detection"]["cases"]:
        notes = "; ".join([f"missing: {x}" for x in c["missing"]] + [f"unexpected: {x}" for x in c["unexpected"]]
                          + c["severity_mismatches"]) or ""
        lines.append(f"| `{c['case']}` | {c['oas']} | {c['expected_decision']} | {c['decision']} | "
                     f"{c['expected_changes']}/{c['detected']} | {'pass' if c['pass'] else '**FAIL**'} | "
                     f"{notes.replace('|', '/')} |")
    lines += ["", "Decision confusion (rows = expected, columns = got):", "", "| expected \\ got | " +
              " | ".join(["PROCEED", "HUMAN_REVIEW", "BLOCK"]) + " |", "|---|---|---|---|"]
    for exp in ("PROCEED", "HUMAN_REVIEW", "BLOCK"):
        row = r["detection"]["decision_confusion"].get(exp, {})
        lines.append(f"| {exp} | " + " | ".join(str(row.get(g, 0)) for g in ("PROCEED", "HUMAN_REVIEW", "BLOCK")) + " |")
    lines += ["", "### Baselines for the release decision (same detected changes, simpler gates)", "",
              "| Gate | Correct | Accuracy | Wrong cases |", "|---|---|---|---|"]
    for b in r["detection"]["baselines"]:
        lines.append(f"| {b['label']} | {b['correct']}/{b['cases']} | {b['decision_accuracy']:.0%} | "
                     f"{'; '.join(b['wrong']) or 'none'} |")
    lines += ["", "## Agent investigation compared with simpler approaches", "",
              "Impact precision/recall are per change, over cases whose affected changes were labelled by reading "
              "the consumer code (`affected` in `AGENT_CASES`).", "",
              "| System | Decisions correct | False escalations | Impact precision | Impact recall |",
              "|---|---|---|---|---|"]
    fmt = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
    for c in r["agent"]["comparison"]:
        lines.append(f"| {c['label']} | {c['correct']}/{c['cases']} | {c['false_escalations']} | "
                     f"{fmt(c['impact_precision'])} ({c['impact_tp']} TP, {c['impact_fp']} FP) | "
                     f"{fmt(c['impact_recall'])} ({c['impact_fn']} missed) |")
    lines += ["", "| Case | Context | Expected | Deterministic | Keyword RAG | Agent (no retry) | Agent | Why |",
              "|---|---|---|---|---|---|---|---|"]
    for c in r["agent"]["cases"]:
        sy = c["systems"]
        lines.append(f"| `{c['name']}` | {c['context']} | {c['expected']} | " + " | ".join(
            f"{sy[k]['decision']}" + ("" if sy[k]["pass"] else " ✗") for k in
            ("deterministic", "keyword_rag", "agent_no_retry", "agent")) + f" | {c['why']} |")
    lines += ["", "## Limits of this evaluation", "",
              "* The corpus is small and was written by the developers of the engine; it checks the documented rules",
              "  and known traps, not real-world prevalence. Add real spec histories to `CASES` to extend it.",
              "* Agent systems run in rules mode. The LLM assessment path is exercised by scripted/mock tests only and",
              "  is not part of these numbers.",
              "* The suite was checked for sensitivity by sabotage: making the diff treat `readOnly` request fields as",
              "  visible fails `trap-readonly-required-in-request`, and making every consumer look like it calls the",
              "  affected operation makes `orders-decoy-consumer` falsely escalate to BLOCK. Both were caught.", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", help="write a markdown report to this path")
    ap.add_argument("--json", help="write the raw JSON report to this path")
    args = ap.parse_args()
    r = run_all()
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(r))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=2)
    m, am = r["detection"]["metrics"], r["agent"]["metrics"]
    print(f"detection: {m['cases_fully_correct']}/{m['cases']} cases, precision {m['precision']:.2%}, "
          f"recall {m['recall']:.2%}, severity {m['severity_accuracy']:.2%}, decisions {m['decision_accuracy']:.2%}")
    print(f"agent:     {am['correct']}/{am['cases']} cases, false escalations {am['false_escalations']}, impact "
          f"recall {am['impact_recall']:.0%}, precision {am['impact_precision']:.0%}")
    for b in r["detection"]["baselines"]:
        print(f"  gate {b['system']:<14} {b['correct']}/{b['cases']}")
    for c in r["agent"]["comparison"]:
        print(f"  agent-cmp {c['system']:<15} {c['correct']}/{c['cases']} decisions, {c['false_escalations']} false "
              f"escalations, impact P={c['impact_precision']} R={c['impact_recall']}")
    for c in r["detection"]["cases"] + r["agent"]["cases"]:
        if not c["pass"]:
            print("FAIL", json.dumps(c, default=str)[:600])


if __name__ == "__main__":
    main()
