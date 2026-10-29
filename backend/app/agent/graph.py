"""LangGraph investigation agent.

    START → baseline → plan ─┬─(queue empty)────────────────────────────────────────────→ decide → END
                             └→ retrieve → assess → refine → execute → verify → reflect ─┤
                                  ↑            (retry: same change, broadened query) ────┤
                                  └──────────── (next change, possibly re-prioritised) ──┘

reflect is where the agent decides what happens next from the tool results it just got:
  * bounded retry  – no verified consumer evidence and attempts left → retrieve again with operation-level terms
  * replanning     – a confirmed consumer break moves other changes on the same operation to the front
  * stopping       – queue empty, or the step budget is spent (remaining changes are listed as skipped)

Each node appends one trace event recording its mode:
  tool  – deterministic tool call (retrieval, test execution)
  rules – rule-based reasoning
  llm   – model reasoning (only when a local Ollama model is configured); claims are verified against evidence
"""
from __future__ import annotations

import json
import operator
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from ..evidence import EvidenceIndex, tokenize
from . import llm as llm_mod
from ..runner import Runner
from ..testgen import TestCase
from .tools import (IMPACT_FINDINGS, broadened_terms, check_target, derive_consumer_schema, extract_literal,
                    leaf_field, live_request, operation_lines, query_terms, rule_findings,
                    validate_against_derived)

MAX_TARGETS = 20
MAX_RETRIES = 1          # extra retrieval attempts per change
STEP_BUDGET = MAX_TARGETS + 8  # total retrieve calls per investigation
SAFE_FINDINGS = {"handles_new_value", "reads_with_null_check", "not_used"}
# findings whose mere existence means the consumer hits the change (so a failing baseline test applies to them)
DIRECT_PROOF = {"reads_header", "calls_sdk_method", "sends_removed_value"}


@dataclass
class Ctx:
    base: dict
    cand: dict
    analysis: dict
    index: EvidenceIndex
    context_files: list[str]
    llm: llm_mod.LLMClient | None = None
    target_url: str | None = None
    counters: dict = field(default_factory=lambda: {"R": 0})
    max_retries: int = MAX_RETRIES
    tools_unsupported: str | None = None  # reason, once the model rejected tool calling
    llm_disabled: dict | None = None  # set after a fatal model error: stop calling the model for this run
    llm_stats: dict = field(default_factory=lambda: {"ok": 0, "failed": 0, "skipped": 0, "errors": []})


class State(TypedDict, total=False):
    queue: list[str]
    current: str | None
    plan: dict
    retrieved: list[dict]
    assessment: dict
    refined: list[dict]
    refined_results: list[dict]
    pending: dict | None
    attempt: int
    attempt_log: list
    steps: int
    stop_reason: str | None
    skipped_budget: list
    retry: bool
    records: Annotated[list, operator.add]
    trace: Annotated[list, operator.add]
    decision: dict


def _event(state: State, node: str, started: float, out: dict) -> dict:
    t = out.pop("_trace")
    return {"seq": len(state.get("trace", [])) + 1, "node": node, "change_id": t.get("change_id"),
            "mode": t["mode"], "summary": t["summary"], "detail": t.get("detail", {}),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


def traced(node: str, fn: Callable[[State], dict]) -> Callable[[State], dict]:
    def wrapper(state: State) -> dict:
        started = time.perf_counter()
        out = fn(state)
        out["trace"] = [_event(state, node, started, out)]
        return out
    wrapper.__name__ = node
    return wrapper


def build_graph(ctx: Ctx):
    a = ctx.analysis
    changes = {c["id"]: c for c in a["changes"]}
    status = a["recommendation"]["change_status"]
    tests_by_change: dict[str, list[dict]] = {}
    for t in a["tests"]:
        for cid in t["change_ids"]:
            tests_by_change.setdefault(cid, []).append(t)
    results = {r["test_id"]: r for r in a["results"]}

    # ------------------------------------------------------------------ nodes
    def baseline(state: State) -> dict:
        rec = a["recommendation"]
        return {"_trace": {"mode": "tool", "summary": f"Deterministic baseline: {rec['decision']} "
                           f"({rec['counts']['changes']} changes, {rec['counts']['tests']} contract tests, "
                           f"{rec['counts']['tests_failed']} failed)",
                           "detail": {"decision": rec["decision"], "reasons": [r["code"] for r in rec["reasons"]],
                                      "evidence_chunks": len(ctx.index.chunks), "context_files": ctx.context_files,
                                      "target_url": ctx.target_url}}}

    def plan(state: State) -> dict:
        def bucket(c):
            ev = status[c["id"]]["evidence"]
            if c["severity"] == "BREAKING" and ev != "incompatibility_observed":
                return 0, "breaking, not confirmed by contract tests"
            if c["severity"] == "POTENTIALLY_BREAKING":
                return 1, "potentially breaking"
            if c["severity"] == "NON_BREAKING" and ev == "incompatibility_observed":
                return 2, "classified safe but a test failed"
            if c["severity"] == "BREAKING":
                return 3, "confirmed breaking: identify affected consumers"
            return None, None
        ranked = []
        for c in a["changes"]:
            b, why = bucket(c)
            if b is not None:
                ranked.append((b, c["id"], why))
        ranked.sort()
        chosen, skipped = ranked[:MAX_TARGETS], ranked[MAX_TARGETS:]
        queue = [cid for _, cid, _ in chosen]
        return {"queue": queue, "plan": {"targets": [{"change_id": cid, "why": why} for _, cid, why in chosen],
                                         "skipped": [cid for _, cid, _ in skipped]},
                "_trace": {"mode": "rules", "summary": f"Investigating {len(queue)} of {len(a['changes'])} changes"
                           + (f" ({len(skipped)} lowest-priority skipped)" if skipped else ""),
                           "detail": {"targets": [{"change_id": cid, "why": why} for _, cid, why in chosen]}}}

    def retrieve(state: State) -> dict:
        cid = state["queue"][0]
        c = changes[cid]
        attempt = state.get("attempt", 0)
        if attempt == 0:
            strategy, terms, k = "focused", query_terms(c), 6
        else:
            strategy, terms, k = "broadened", broadened_terms(c, ctx.base), 10
        ctx_hits = ctx.index.search(terms, k=k, kinds=("context",))
        spec_hits = ctx.index.search(terms, k=2, kinds=("spec",))
        evidence = [{**ch.public(), "score": s} for ch, s in ctx_hits + spec_hits]
        seen = {h for prev in state.get("attempt_log", []) for h in prev["hits"]}
        new = [e["id"] for e in evidence if e["id"] not in seen]
        return {"current": cid, "retrieved": evidence, "steps": state.get("steps", 0) + 1,
                "_trace": {"mode": "tool", "change_id": cid,
                           "summary": f"BM25 ({strategy}, attempt {attempt + 1}) over {len(ctx.index.chunks)} chunks: "
                                      f"{len(ctx_hits)} context and {len(spec_hits)} spec hits"
                                      + (f", {len(new)} new" if attempt else ""),
                           "detail": {"tool": "evidence.search", "strategy": strategy, "query": terms, "k": k,
                                      "hits": [{"id": e["id"], "citation": e["citation"], "score": e["score"]}
                                               for e in evidence]}}}

    def assess(state: State) -> dict:
        cid = state["current"]
        c = changes[cid]
        evidence = list(state["retrieved"])
        chunks = [ctx.index.get(e["id"]) for e in evidence]
        chunks = [ch for ch in chunks if ch is not None]
        tests = [{"id": t["id"], "kind": t["kind"], "predicted": t["predicted"],
                  "status": results[t["id"]]["status"], "verdict": results[t["id"]]["verdict"]}
                 for t in tests_by_change.get(cid, [])]
        has_context = any(ch.kind == "context" for ch in chunks)
        mode, note, model_reply = "rules", None, None
        tool_log: list[dict] = []
        problems: list[str] = []
        extra: dict[str, Any] = {}  # chunks the model found through its own tool calls

        def execute_tool(name: str, arguments: str) -> tuple[dict, dict]:
            """The only things the model can run: read-only searches over this run's evidence index."""
            entry = {"tool": name, "arguments": arguments[:300], "result_ids": [], "error": None}
            try:
                args = json.loads(arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as exc:
                entry["error"] = f"invalid arguments: {exc}"[:200]
                return {"error": "arguments must be a JSON object"}, entry
            entry["arguments"] = args
            if name == "search_evidence":
                query = args.get("query")
                if not isinstance(query, str) or not query.strip() or len(query) > 300:
                    entry["error"] = "query must be a non-empty string (max 300 chars)"
                    return {"error": entry["error"]}, entry
                kinds = {"consumer": ("context",), "spec": ("spec",)}.get(args.get("source"), ("context", "spec"))
                k = args.get("k") if isinstance(args.get("k"), int) and 1 <= args["k"] <= 8 else 5
                hits = ctx.index.search(tokenize(query), k=k, kinds=kinds)
                out = []
                for ch, score in hits:
                    extra.setdefault(ch.id, {**ch.public(), "score": score, "via": "model tool call"})
                    out.append({"chunk_id": ch.id, "citation": ch.public()["citation"], "kind": ch.kind,
                                "text": ch.text[:1500]})
                entry["result_ids"] = [o["chunk_id"] for o in out]
                return {"results": out}, entry
            if name == "get_chunk":
                ch = ctx.index.get(str(args.get("chunk_id", "")))
                if ch is None:
                    entry["error"] = "unknown chunk_id"
                    return {"error": "unknown chunk_id"}, entry
                extra.setdefault(ch.id, {**ch.public(), "score": None, "via": "model tool call"})
                entry["result_ids"] = [ch.id]
                return {"chunk_id": ch.id, "citation": ch.public()["citation"], "text": ch.text[:4000]}, entry
            entry["error"] = "unknown tool"
            return {"error": f"unknown tool {name!r}; available: search_evidence, get_chunk"}, entry

        if ctx.llm is not None and has_context and ctx.llm_disabled:
            ctx.llm_stats["skipped"] += 1
            note = (f"LLM not called: disabled earlier in this run after a {ctx.llm_disabled['category']} error "
                    f"({ctx.llm_disabled['message']}); used rules")
        elif ctx.llm is not None and has_context:
            raw = ""
            try:
                prompt = llm_mod.build_prompt(c, tests, evidence)
                if getattr(ctx.llm, "supports_tools", False) and hasattr(ctx.llm, "chat") and not ctx.tools_unsupported:
                    raw, tool_log, tnote = llm_mod.assess_with_tools(ctx.llm, llm_mod.SYSTEM, prompt, execute_tool)
                    if tnote:
                        ctx.tools_unsupported = tnote
                        note = tnote
                else:
                    raw = ctx.llm.complete(llm_mod.SYSTEM, prompt)
                    if ctx.tools_unsupported:
                        note = ctx.tools_unsupported
                data = llm_mod.parse_reply(raw)
                problems = data.pop("_problems", [])
                findings = [{"chunk_id": f["chunk_id"], "quote": f["quote"], "finding": f["finding"],
                             "detail": f["explanation"], "source": "llm",
                             **({"handled_values": data["handled_enum_values"]}
                                if f["finding"] == "enumerates_known_values" and data.get("handled_enum_values")
                                else {})}
                            for f in data["findings"]]
                impact = data["impact"]
                rationale = data.get("rationale", "")
                mode, model_reply = "llm", {"model": ctx.llm.name, "raw": llm_mod.redact(raw[:4000]),
                                            "live": not getattr(ctx.llm, "mocked", False)}
                ctx.llm_stats["ok"] += 1
                ctx.llm_stats["tool_calls"] = ctx.llm_stats.get("tool_calls", 0) + len(tool_log)
                if problems:
                    note = ((note + " ") if note else "") + f"{len(problems)} malformed finding(s) dropped: " + \
                        "; ".join(problems[:4])
            except llm_mod.LLMError as exc:
                ctx.llm_stats["failed"] += 1
                ctx.llm_stats["errors"].append(exc.to_dict())
                note = f"LLM unavailable ({exc.category}): {exc} Used rules instead."
                if exc.fatal:
                    ctx.llm_disabled = exc.to_dict()
                    note += " The model will not be called again in this run."
                mode = "rules"
            except Exception as exc:  # unusable reply (bad JSON / schema): fall back, and say so
                ctx.llm_stats["failed"] += 1
                ctx.llm_stats["errors"].append({"category": "bad_response", "message": llm_mod.redact(str(exc))[:200]})
                note = f"LLM reply unusable ({type(exc).__name__}: {llm_mod.redact(str(exc))[:200]}); used rules instead"
                mode = "rules"
        all_chunks = chunks + [ctx.index.get(i) for i in extra if ctx.index.get(i) is not None]
        rules = [{**f, "source": "rules"} for f in rule_findings(c, all_chunks, ctx.index, ctx.base)]
        cross_check = []
        if mode == "rules":
            findings = rules
            kinds = {f["finding"] for f in findings}
            if not has_context:
                impact, rationale = "unknown", "No consumer context was supplied, so consumer impact cannot be assessed."
            elif kinds & IMPACT_FINDINGS:
                impact = "consumer_impact"
                rationale = "; ".join(sorted({f["detail"] for f in findings if f["finding"] in IMPACT_FINDINGS}))
            else:
                impact = "unknown"
                rationale = ("Retrieved consumer code does not reference the changed element. That is absence of "
                             "evidence, not evidence of safety.") if not kinds else \
                    "; ".join(sorted({f["detail"] for f in findings}))
        else:
            # independent deterministic cross-check of the model's judgement (used to detect contradictions)
            cross_check = [{k: f.get(k) for k in ("chunk_id", "finding", "citation", "detail")}
                           for f in rules if f["finding"] in IMPACT_FINDINGS]
        merged_evidence = evidence + [e for i, e in extra.items() if i not in {x["id"] for x in evidence}]
        assessment = {"change_id": cid, "impact": impact, "findings": findings, "rationale": rationale,
                      "mode": mode, "note": note, "llm": model_reply, "tool_calls": tool_log,
                      "validation_problems": problems, "cross_check": cross_check}
        tools_txt = f", {len(tool_log)} model tool call(s)" if tool_log else ""
        return {"assessment": assessment, "retrieved": merged_evidence,
                "_trace": {"mode": mode, "change_id": cid,
                           "summary": f"Impact: {impact} ({len(findings)} finding(s){tools_txt})"
                                      + (f". {note}" if note else ""),
                           "detail": {"rationale": rationale,
                                      "findings": [{k: f.get(k) for k in ("chunk_id", "finding", "citation", "detail")}
                                                   for f in findings], "llm": model_reply,
                                      "tool_calls": tool_log, "validation_problems": problems,
                                      "rules_cross_check": cross_check}}}

    def refine(state: State) -> dict:
        cid = state["current"]
        c = changes[cid]
        assessment = state["assessment"]
        refined: list[dict] = []

        def new_id():
            ctx.counters["R"] += 1
            return f"R{ctx.counters['R']:03d}"

        impact_findings = [f for f in assessment["findings"] if f["finding"] in IMPACT_FINDINGS]
        if c["direction"] == "response" and impact_findings:
            derived = derive_consumer_schema(ctx.base, c, impact_findings)
            base_test = next((t for t in tests_by_change.get(cid, []) if t["kind"] == "response_body"), None)
            if derived and base_test:
                schema, steps = derived
                refined.append({"id": new_id(), "change_ids": [cid], "kind": "consumer_expectation",
                                "title": f"Response satisfies what consumers rely on ({leaf_field(c)})",
                                "rationale": "The base response schema is tightened with constraints taken from the "
                                             "cited consumer code, and the candidate response is validated against it.",
                                "derived_from": steps, "schema": schema, "payload": base_test["payload"],
                                "based_on": base_test["id"], "origin": "agent"})
        param_test = next((t for t in tests_by_change.get(cid, []) if t["kind"] == "request_parameters"), None)
        if c["direction"] == "request" and c.get("param") and param_test:
            pin, pname = c["param"]
            for f in impact_findings:
                found, value = extract_literal(pname, f.get("lines", []))
                if not found:
                    continue
                params = dict(param_test["payload"]["params"])
                key = next((k for k in params if k.lower() == f"{pin}:{pname}".lower()), f"{pin}:{pname}")
                params[key] = value
                refined.append({"id": new_id(), "change_ids": [cid], "kind": "consumer_request_replay",
                                "title": f"Replay the consumer's real value {pname}={value!r}",
                                "rationale": f"The cited consumer code sends {pname}={value!r}; that exact request "
                                             "is validated against the candidate parameter definitions.",
                                "derived_from": [{"constraint": f"{pname}={value!r}", "evidence": [f["chunk_id"]]}],
                                "payload": {"params": params}, "based_on": param_test["id"], "origin": "agent",
                                "source_test": param_test})
        if ctx.target_url:
            for t in tests_by_change.get(cid, [])[:1]:
                if t["kind"] in ("request_parameters", "request_body", "response_body", "credential_compat",
                                 "route_resolution", "response_header", "response_status"):
                    refined.append({"id": new_id(), "change_ids": [cid], "kind": "live_http",
                                    "title": f"Live replay of an old-client call ({t['locator']['method'].upper()} "
                                             f"{t['locator']['path']})",
                                    "rationale": "Sends what an existing client sends to the running deployment and "
                                                 "judges the reply by the old contract.",
                                    "based_on": t["id"], "source_test": t, "origin": "agent"})
        if refined:
            reason = ""
        elif not impact_findings:
            reason = "no verified consumer usage to encode"
        elif c["direction"] == "request":
            reason = "cited code sends no literal value to replay"
        else:
            reason = f"no executable refinement for {c['rule']}; evidence is combined with the baseline test"
        return {"refined": refined,
                "_trace": {"mode": "rules", "change_id": cid,
                           "summary": f"{len(refined)} refined test(s) generated" + (f" ({reason})" if reason else ""),
                           "detail": {"tests": [{"id": r["id"], "kind": r["kind"], "title": r["title"],
                                                 "derived_from": r.get("derived_from")} for r in refined]}}}

    def execute(state: State) -> dict:
        out = []
        for r in state["refined"]:
            started = time.perf_counter()
            try:
                if r["kind"] == "consumer_expectation":
                    errs = validate_against_derived(r["payload"], r["schema"], ctx.base)
                    res = {"status": "failed" if errs else "passed", "errors": errs,
                           "summary": "Candidate response violates what consumers rely on" if errs
                           else "Candidate response still satisfies what consumers rely on"}
                elif r["kind"] == "consumer_request_replay":
                    st = r["source_test"]
                    tc = TestCase(id=r["id"], change_ids=r["change_ids"], kind="request_parameters", title=r["title"],
                                  rationale=r["rationale"], predicted=st["predicted"], locator=st["locator"],
                                  payload=r["payload"])
                    rr = Runner(ctx.base, ctx.cand).run(tc)
                    res = {"status": rr.status, "errors": rr.errors, "summary": rr.summary}
                else:
                    res = live_request(ctx.target_url, ctx.base, ctx.cand, r["source_test"], changes[r["change_ids"][0]])
            except Exception as exc:
                res = {"status": "error", "errors": [], "summary": f"{type(exc).__name__}: {exc}"}
            res.update(test_id=r["id"], duration_ms=round((time.perf_counter() - started) * 1000, 2))
            out.append(res)
        return {"refined_results": out,
                "_trace": {"mode": "tool", "change_id": state["current"],
                           "summary": f"Executed {len(out)} refined test(s): " + ", ".join(
                               f"{sum(r['status'] == st for r in out)} {st}"
                               for st in ("failed", "passed", "inconclusive", "timeout", "infra_error", "error")
                               if any(r['status'] == st for r in out)) if out else "No refined tests to execute",
                           "detail": {"results": [{k: r.get(k) for k in ("test_id", "status", "summary")}
                                                  for r in out]}}}

    def verify(state: State) -> dict:
        cid = state["current"]
        assessment = state["assessment"]
        checked = []
        for f in assessment["findings"]:
            ch = ctx.index.get(str(f.get("chunk_id")))
            if ch is None or ch.kind != "context" and f["finding"] in IMPACT_FINDINGS:
                ok, why = False, "cited chunk does not exist or is not consumer context"
            elif f.get("source") == "llm":
                quote = " ".join(str(f.get("quote", "")).split())
                ok = bool(quote) and quote in " ".join(ch.text.split())
                why = "quote found verbatim in chunk" if ok else "quote not found in cited chunk"
            else:
                ok = all(line["text"] in ch.text for line in f.get("lines", [])) and bool(f.get("lines"))
                why = "matched lines present in chunk" if ok else "matched lines missing"
            if ok and f.get("operation_call"):
                same_file = [x for x in ctx.index.chunks if x.source == ch.source]
                if not any(f["operation_call"]["text"] in x.text for x in same_file):
                    ok, why = False, "operation call line not found in the consumer file"
            elif ok and f.get("source") == "llm" and f["finding"] in IMPACT_FINDINGS:
                # The same scoping the rules apply: a consumer can only be broken by a change to an operation it
                # calls. (Found in a real local-model run: findings from the getOrder() worker were cited for the
                # createOrder response.)
                call = next(((x.start_line + i, x.text.splitlines()[i].strip()[:200])
                             for x in ctx.index.chunks if x.source == ch.source and x.kind == "context"
                             for i in operation_lines(changes[cid], ctx.base, x.text)), None)
                if call is None:
                    ok = False
                    why = (f"{ch.source} never calls {(changes[cid].get('method') or '').upper()} "
                           f"{changes[cid].get('path')}, so this change cannot affect it")
                else:
                    f = {**f, "operation_call": {"line": call[0], "text": call[1]}}
            if f.get("finding") not in llm_mod.ALLOWED_FINDINGS:
                ok, why = False, f"unknown finding type {f.get('finding')!r}"
            checked.append({**f, "verified": ok, "verification": why})
        verified = [f for f in checked if f["verified"]]
        impact_verified = any(f["finding"] in IMPACT_FINDINGS for f in verified)
        refined_results = state.get("refined_results", [])
        by_kind = {}
        for r, t in zip(refined_results, state.get("refined", [])):
            by_kind.setdefault(t["kind"], []).append(r)
        consumer_failed = any(r["status"] == "failed" for k in ("consumer_expectation", "consumer_request_replay")
                              for r in by_kind.get(k, []))
        live_failed = any(r["status"] == "failed" for r in by_kind.get("live_http", []))
        baseline_failed = [t["id"] for t in tests_by_change.get(cid, []) if results[t["id"]]["status"] == "failed"]
        direct = [f for f in verified if f["finding"] in DIRECT_PROOF]
        confirming = [r["test_id"] for r in refined_results if r["status"] == "failed"]
        if direct and baseline_failed:
            confirming += baseline_failed
        if impact_verified and (consumer_failed or (direct and baseline_failed)):
            outcome = "consumer_break_confirmed"
        elif live_failed:
            outcome = "live_break_confirmed"
        elif impact_verified and changes[cid]["severity"] == "BREAKING" and \
                status[cid]["evidence"] == "incompatibility_observed":
            outcome = "affected_consumers_identified"
        elif impact_verified:
            outcome = "consumer_usage_found"
        elif assessment["mode"] == "llm" and assessment["impact"] == "no_consumer_impact" and \
                assessment.get("cross_check"):
            outcome = "contradictory_evidence"  # model says safe, deterministic rules see consumer usage
        elif assessment["impact"] == "no_consumer_impact" and verified and \
                all(f["finding"] in SAFE_FINDINGS for f in verified):
            outcome = "no_consumer_impact_claimed"
        elif assessment["mode"] == "llm" and assessment["impact"] == "consumer_impact":
            outcome = "unverified_model_claim"  # a model assertion without verifiable evidence proves nothing
        elif not ctx.context_files:
            outcome = "no_context"
        else:
            outcome = "no_usage_found"
        dropped = len(checked) - len(verified)
        record = {"change_id": cid, "outcome": outcome, "impact": assessment["impact"], "mode": assessment["mode"],
                  "rationale": assessment["rationale"], "note": assessment.get("note"), "findings": checked,
                  "evidence": state["retrieved"], "refined_tests": state.get("refined", []),
                  "refined_results": refined_results, "confirming_tests": confirming, "llm": assessment.get("llm"),
                  "tool_calls": assessment.get("tool_calls", []),
                  "validation_problems": assessment.get("validation_problems", []),
                  "cross_check": assessment.get("cross_check", [])}
        return {"pending": record,
                "_trace": {"mode": "rules", "change_id": cid,
                           "summary": f"Outcome: {outcome}; {len(verified)}/{len(checked)} findings verified"
                                      + (f", {dropped} discarded" if dropped else ""),
                           "detail": {"verification": [{k: f.get(k) for k in ("chunk_id", "finding", "verified",
                                                                                 "verification")} for f in checked]}}}

    def reflect(state: State) -> dict:
        rec = state["pending"]
        cid = rec["change_id"]
        attempt = state.get("attempt", 0)
        log = state.get("attempt_log", []) + [{
            "attempt": attempt + 1, "strategy": "focused" if attempt == 0 else "broadened",
            "hits": [e["id"] for e in rec["evidence"]], "outcome": rec["outcome"],
            "verified": sum(1 for f in rec["findings"] if f["verified"]),
            "discarded": sum(1 for f in rec["findings"] if not f["verified"])}]
        steps = state.get("steps", 0)
        insufficient = rec["outcome"] in ("no_usage_found", "unverified_model_claim") and bool(ctx.context_files)
        if insufficient and attempt < ctx.max_retries and steps < STEP_BUDGET:
            why = (f"{log[-1]['discarded']} finding(s) failed verification" if log[-1]["discarded"]
                   else f"no verified consumer usage in {len(rec['evidence'])} retrieved chunk(s)")
            return {"attempt": attempt + 1, "attempt_log": log, "pending": None, "retry": True,
                    "_trace": {"mode": "rules", "change_id": cid,
                               "summary": f"Retry {cid} (attempt {attempt + 2} of {ctx.max_retries + 1}): {why}; "
                                          "broadening the query with operation-level terms",
                               "detail": {"decision": "retry", "reason": why, "attempts": log}}}
        rec = {**rec, "attempts": log}
        queue = state["queue"][1:]
        moved: list[str] = []
        if rec["outcome"] in ("consumer_break_confirmed", "consumer_usage_found", "affected_consumers_identified"):
            c = changes[cid]
            same_op = [q for q in queue if changes[q].get("method") == c.get("method")
                       and changes[q].get("path") == c.get("path")]
            if same_op and same_op != queue[:len(same_op)]:
                moved = same_op
                queue = same_op + [q for q in queue if q not in same_op]
        stop = None
        if queue and steps >= STEP_BUDGET:
            stop = f"step budget of {STEP_BUDGET} retrievals spent; {len(queue)} change(s) not investigated"
        if stop:
            nxt = "decide (" + stop + ")"
        elif queue:
            nxt = f"next: {queue[0]}" + (f" (moved forward: {', '.join(moved)} share this operation with a "
                                           f"confirmed consumer)" if moved else "")
        else:
            nxt = "queue empty: decide"
        return {"records": [rec], "queue": [] if stop else queue, "current": None, "pending": None, "attempt": 0,
                "attempt_log": [], "retry": False, "stop_reason": stop,
                **({"skipped_budget": queue} if stop else {}),
                "_trace": {"mode": "rules", "change_id": cid,
                           "summary": f"{cid} settled as {rec['outcome']} after {len(log)} attempt(s); {nxt}",
                           "detail": {"decision": "stop" if stop else ("replan" if moved else "next"),
                                      "attempts": log, "moved_forward": moved, "remaining": queue}}}

    def decide(state: State) -> dict:
        rec = a["recommendation"]
        base_decision = rec["decision"]
        records = {r["change_id"]: r for r in state.get("records", [])}
        adjustments = []
        breaks = [r for r in records.values() if r["outcome"] in ("consumer_break_confirmed", "live_break_confirmed")
                  and changes[r["change_id"]]["severity"] in ("BREAKING", "POTENTIALLY_BREAKING")]
        decision = base_decision
        if breaks:
            decision = "BLOCK"
            for kind, label in (("consumer_break_confirmed", "Consumer code relies on behaviour the change breaks, "
                                 "and the refined test confirms it."),
                                ("live_break_confirmed", "A live replay of an old-client call violated the old "
                                 "contract.")):
                hits = [r for r in breaks if r["outcome"] == kind]
                if hits:
                    adjustments.append({
                        "code": kind.upper(), "change_ids": [r["change_id"] for r in hits], "text": label,
                        "evidence_ids": sorted({f["chunk_id"] for r in hits for f in r["findings"] if f["verified"]}),
                        "test_ids": sorted({t for r in hits for t in r["confirming_tests"]})})
        triggers = [c["id"] for c in a["changes"]
                    if (c["severity"] == "BREAKING" and status[c["id"]]["evidence"] != "incompatibility_observed")
                    or c["severity"] == "POTENTIALLY_BREAKING"
                    or status[c["id"]]["evidence"] in ("error",)
                    or (c["severity"] == "NON_BREAKING" and status[c["id"]]["evidence"] == "incompatibility_observed")]
        if base_decision == "HUMAN_REVIEW" and not breaks and triggers and \
                all(records.get(cid, {}).get("outcome") == "no_consumer_impact_claimed" for cid in triggers):
            decision = "PROCEED"
            adjustments.append({"code": "NO_CONSUMER_IMPACT", "change_ids": triggers,
                                "text": "Every change that needed review has verified evidence that consumers are "
                                        "unaffected.", "evidence_ids": sorted({f["chunk_id"] for cid in triggers
                                                                              for f in records[cid]["findings"]
                                                                              if f["verified"]}), "test_ids": []})
        conflicted = [cid for cid, r in records.items() if r["outcome"] in ("contradictory_evidence",
                                                                             "unverified_model_claim")]
        if conflicted:
            adjustments.append({"code": "UNRESOLVED_MODEL_EVIDENCE", "change_ids": conflicted, "evidence_ids": [],
                                "test_ids": [], "text": "The model's judgement could not be verified or contradicted "
                                "the deterministic evidence for these changes, so they stay with a human reviewer."})
        if decision == base_decision:
            unresolved = [cid for cid in triggers if records.get(cid, {}).get("outcome") not in
                          ("consumer_break_confirmed", "live_break_confirmed", "no_consumer_impact_claimed")]
            adjustments.append({"code": "BASELINE_KEPT", "change_ids": unresolved, "evidence_ids": [], "test_ids": [],
                                "text": "The investigation did not produce verified evidence that changes the "
                                        "baseline recommendation." + (f" {len(unresolved)} change(s) remain unresolved "
                                                                      "and need a reviewer." if unresolved else "")})
        if state.get("stop_reason"):
            adjustments.append({"code": "BUDGET_STOP", "change_ids": state.get("skipped_budget", []),
                                "evidence_ids": [], "test_ids": [],
                                "text": f"Investigation stopped early: {state['stop_reason']}."})
        out = {"decision": decision, "baseline_decision": base_decision, "adjustments": adjustments,
               "outcomes": {cid: r["outcome"] for cid, r in records.items()},
               "policy": "agent-v1: escalate only on verified evidence + failing refined test; de-escalate only when "
                         "every review trigger has verified no-impact evidence; never auto-release",
               "final_approval": "required: a human must approve or reject this release"}
        return {"decision": out,
                "_trace": {"mode": "rules", "summary": f"Recommendation {base_decision} → {decision}",
                           "detail": {"adjustments": [{k: x[k] for k in ("code", "change_ids", "test_ids")}
                                                      for x in adjustments]}}}

    def route(state: State) -> str:
        return "retrieve" if state.get("queue") else "decide"

    def after_reflect(state: State) -> str:
        return "retrieve" if state.get("retry") or state.get("queue") else "decide"

    g = StateGraph(State)
    for name, fn in (("baseline", baseline), ("plan", plan), ("retrieve", retrieve), ("assess", assess),
                     ("refine", refine), ("execute", execute), ("verify", verify), ("reflect", reflect),
                     ("decide", decide)):
        g.add_node(name, traced(name, fn))
    g.add_edge(START, "baseline")
    g.add_edge("baseline", "plan")
    g.add_conditional_edges("plan", route, {"retrieve": "retrieve", "decide": "decide"})
    g.add_edge("retrieve", "assess")
    g.add_edge("assess", "refine")
    g.add_edge("refine", "execute")
    g.add_edge("execute", "verify")
    g.add_edge("verify", "reflect")
    g.add_conditional_edges("reflect", after_reflect, {"retrieve": "retrieve", "decide": "decide"})
    g.add_edge("decide", END)
    return g.compile()


def _reasoning_label(client: Any, stats: dict, disabled: dict | None) -> str:
    if client is None:
        return "rules (no LLM configured)"
    if stats["ok"] and not (stats["failed"] or stats["skipped"]):
        return client.name
    if stats["ok"]:
        return f"{client.name} with rules fallback for {stats['failed'] + stats['skipped']} step(s)"
    if stats["failed"] or stats["skipped"]:
        why = disabled["category"] if disabled else (stats["errors"][-1]["category"] if stats["errors"] else "error")
        return f"rules (LLM {client.name} unavailable: {why})"
    return f"rules ({client.name} configured, but no step needed it: no consumer context)"


def _llm_mode(client: Any, stats: dict) -> str:
    """How the LLM took part, for the UI, the stored record and the docs. Never 'llm_assisted' unless at least one
    assessment really came from the model."""
    if client is None:
        return "rules_only"
    if stats["ok"] and not (stats["failed"] or stats["skipped"]):
        return "llm_assisted"
    if stats["ok"]:
        return "partial_fallback"
    if stats["failed"] or stats["skipped"]:
        return "rules_fallback"
    return "llm_not_needed"


def run_investigation(base: dict, cand: dict, analysis: dict, index: EvidenceIndex, context_files: list[str],
                      target_url: str | None = None, llm: Any = "default",
                      on_event: Callable[[dict], None] | None = None, max_retries: int = MAX_RETRIES) -> dict:
    if target_url:
        err = check_target(target_url)
        if err:
            raise ValueError(err)
    client = llm_mod.default_client() if llm == "default" else llm
    ctx = Ctx(base=base, cand=cand, analysis=analysis, index=index, context_files=context_files, llm=client,
              target_url=target_url, max_retries=max_retries)
    graph = build_graph(ctx)
    started = time.perf_counter()
    final: dict = {}
    sent = 0
    # stream the state after every node so callers can show real progress; the last state is the result
    for final in graph.stream({"trace": [], "records": []}, stream_mode="values",
                              config={"recursion_limit": 20 + 7 * (STEP_BUDGET + 2)}):
        trace = final.get("trace", [])
        if on_event:
            for ev in trace[sent:]:
                on_event(ev)
        sent = len(trace)
    stats = {k: v for k, v in ctx.llm_stats.items()}
    return {
        "id": uuid.uuid4().hex[:12],
        "analysis_id": analysis["id"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reasoning": _reasoning_label(client, stats, ctx.llm_disabled),
        "llm": {"configured": client.name if client else None, "mode": _llm_mode(client, stats),
                "provider": getattr(client, "provider", None), "assessments_by_llm": stats["ok"],
                "tool_calls": stats.get("tool_calls", 0), "live": bool(client) and not getattr(client, "mocked", False),
                "failed_calls": stats["failed"], "skipped_after_failure": stats["skipped"],
                "disabled": ctx.llm_disabled, "errors": stats["errors"][:5]},
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "context_files": context_files,
        "target_url": target_url,
        "evidence_chunks": len(index.chunks),
        "plan": final.get("plan"),
        "records": final.get("records", []),
        "trace": final.get("trace", []),
        "decision": final.get("decision"),
        "stop_reason": final.get("stop_reason"),
        "graph": {"nodes": ["baseline", "plan", "retrieve", "assess", "refine", "execute", "verify", "reflect",
                            "decide"],
                  "loop": "retrieve→assess→refine→execute→verify→reflect repeats per target change; reflect may "
                          f"retry a change once (max {MAX_RETRIES}) with a broadened query, re-prioritise the queue, "
                          f"or stop at the step budget ({STEP_BUDGET})"},
    }
