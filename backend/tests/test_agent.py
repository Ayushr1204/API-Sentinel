"""Agent tests. The LLM path is exercised with scripted fake clients (no network); real local inference through
Ollama is verified with the self-test (see tests/test_ollama.py for the adapter, all mocked)."""
import json
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.agent.graph import run_investigation
from app.agent.tools import check_target
from app.evidence import build_index, tokenize
from app.pipeline import run_analysis
from app.spec_loader import load_spec

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def setup(name, with_context=True):
    d = SAMPLES / name
    b, c = next(d.glob("base.*")), next(d.glob("candidate.*"))
    lb, lc = load_spec(b.read_bytes(), b.name), load_spec(c.read_bytes(), c.name)
    a = run_analysis(lb, lc)
    files = [(p.name, p.read_text()) for p in sorted((d / "context").iterdir())] \
        if with_context and (d / "context").exists() else []
    return lb.doc, lc.doc, a, build_index(lb.doc, lc.doc, files), [n for n, _ in files]


def investigate(name, with_context=True, llm=None, target=None):
    base, cand, a, idx, files = setup(name, with_context)
    return a, run_investigation(base, cand, a, idx, files, target_url=target, llm=llm)


def test_tokenizer_splits_identifiers():
    assert {"trackingurl", "tracking", "url"} <= set(tokenize("order.trackingUrl"))
    assert {"x-total-count", "total", "count"} <= set(tokenize('headers["X-Total-Count"]'))


def test_orders_escalates_with_cited_evidence():
    a, inv = investigate("orders-31-review")
    assert a["recommendation"]["decision"] == "HUMAN_REVIEW"
    assert inv["decision"]["decision"] == "BLOCK"
    out = inv["decision"]["outcomes"]
    changes = {c["id"]: c for c in a["changes"]}
    get_ops = [cid for cid, c in changes.items() if c["method"] == "get" and c["area"] == "response"]
    post_ops = [cid for cid, c in changes.items() if c["method"] == "post" and c["area"] == "response"]
    assert all(out[c] == "consumer_break_confirmed" for c in get_ops)
    # billing_worker only calls getOrder, so POST /orders response changes must NOT be attributed to it
    assert all(out[c] == "no_usage_found" for c in post_ops)


def test_every_verified_citation_matches_the_real_file_line():
    for sample in ("orders-31-review", "petstore-breaking"):
        _, inv = investigate(sample)
        ctx_dir = SAMPLES / sample / "context"
        for r in inv["records"]:
            for f in r["findings"]:
                assert f["verified"], f
                lines = (ctx_dir / f["citation"].split(":")[0]).read_text().splitlines()
                for ln in f["lines"]:
                    assert lines[ln["line"] - 1].strip()[:200] == ln["text"], (sample, f)


def test_refined_consumer_replay_uses_literal_from_evidence():
    a, inv = investigate("petstore-breaking")
    limit = next(c["id"] for c in a["changes"] if c["rule"] == "CONSTRAINT_TIGHTENED" and c["area"] == "parameter")
    rec = next(r for r in inv["records"] if r["change_id"] == limit)
    replay = [(t, res) for t, res in zip(rec["refined_tests"], rec["refined_results"])
              if t["kind"] == "consumer_request_replay"]
    assert replay and all(t["payload"]["params"]["query:limit"] == 100 for t, _ in replay)
    assert all(res["status"] == "failed" and "maximum of 50" in res["errors"][0]["message"] for _, res in replay)


def test_safe_sample_has_nothing_to_investigate():
    _, inv = investigate("petstore-safe")
    assert inv["plan"]["targets"] == [] and inv["decision"]["decision"] == "PROCEED"
    assert [e["node"] for e in inv["trace"]] == ["baseline", "plan", "decide"]


def test_without_context_baseline_is_kept():
    a, inv = investigate("orders-31-review", with_context=False)
    assert inv["decision"]["decision"] == a["recommendation"]["decision"] == "HUMAN_REVIEW"
    assert set(inv["decision"]["outcomes"].values()) == {"no_context"}


def test_trace_is_complete_and_ordered():
    _, inv = investigate("orders-31-review")
    seqs = [e["seq"] for e in inv["trace"]]
    assert seqs == list(range(1, len(seqs) + 1))
    nodes = [e["node"] for e in inv["trace"]]
    assert nodes[:2] == ["baseline", "plan"] and nodes[-1] == "decide"
    loop = nodes[2:-1]
    step = ["retrieve", "assess", "refine", "execute", "verify", "reflect"]
    assert loop == step * (len(loop) // 6) and len(loop) % 6 == 0
    reflects = [e for e in inv["trace"] if e["node"] == "reflect"]
    retries = [e for e in reflects if e["detail"]["decision"] == "retry"]
    # every retry is followed by a broadened retrieval of the same change
    for r in retries:
        nxt = inv["trace"][r["seq"]]  # seq is 1-based, so this is the following event
        assert nxt["node"] == "retrieve" and nxt["change_id"] == r["change_id"] and nxt["detail"]["strategy"] == "broadened"
    assert len(reflects) - len(retries) == len(inv["records"])
    assert all(len(rec["attempts"]) <= 2 for rec in inv["records"])  # bounded: at most one retry per change
    assert {e["mode"] for e in inv["trace"]} <= {"tool", "rules"}  # no LLM configured => never labelled llm


class ScriptedLLM:
    name = "fake:scripted"

    def __init__(self, reply_fn):
        self.reply_fn, self.calls = reply_fn, []

    def complete(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        return self.reply_fn(payload)


def test_llm_findings_with_fabricated_quotes_are_discarded():
    def reply(p):
        eid = p["evidence"][0]["chunk_id"] if p["evidence"] else "E999"
        return json.dumps({"impact": "consumer_impact", "confidence": "high",
                           "findings": [{"chunk_id": eid, "quote": "this text is not in the chunk",
                                         "finding": "reads_field", "explanation": "made up"}],
                           "rationale": "fabricated"})
    a, inv = investigate("orders-31-review", llm=ScriptedLLM(reply))
    assert any(e["mode"] == "llm" for e in inv["trace"] if e["node"] == "assess")
    assert all(not f["verified"] for r in inv["records"] for f in r["findings"])
    assert inv["decision"]["decision"] == "HUMAN_REVIEW"  # unverified claims never escalate


def test_llm_verified_quote_is_used():
    def reply(p):
        ev = next((e for e in p["evidence"] if e["citation"].startswith("billing_worker.py")
                   and "getOrder" in e["text"]), None)
        if ev is None:
            return json.dumps({"impact": "unknown", "findings": [], "rationale": "no relevant evidence"})
        quote = next(line.strip() for line in ev["text"].splitlines() if "getOrder" in line)
        return json.dumps({"impact": "consumer_impact", "findings": [
            {"chunk_id": ev["chunk_id"], "quote": quote, "finding": "calls_sdk_method", "explanation": "calls it"}],
            "rationale": "billing worker calls the generated method"})
    llm = ScriptedLLM(reply)
    a, inv = investigate("orders-31-review", llm=llm)
    op_change = next(c["id"] for c in a["changes"] if c["rule"] == "OPERATION_ID_CHANGED")
    rec = next(r for r in inv["records"] if r["change_id"] == op_change)
    assert rec["mode"] == "llm" and rec["findings"][0]["verified"]
    assert rec["outcome"] == "consumer_break_confirmed"
    assessed_with_context = [e for e in inv["trace"] if e["node"] == "assess" and e["mode"] == "llm"]
    assert inv["reasoning"] == "fake:scripted" and len(llm.calls) == len(assessed_with_context)
    assert inv["llm"]["assessments_by_llm"] == len(llm.calls) and inv["llm"]["failed_calls"] == 0


def test_llm_failure_falls_back_to_rules_and_says_so():
    _, inv = investigate("orders-31-review", llm=ScriptedLLM(lambda p: "I refuse to answer in JSON"))
    assessed = [e for e in inv["trace"] if e["node"] == "assess"]
    assert all(e["mode"] == "rules" and "LLM reply unusable" in e["summary"] for e in assessed)
    assert inv["decision"]["decision"] == "BLOCK"
    assert inv["reasoning"].startswith("rules (LLM fake:scripted unavailable")


def _widget_only(name="orders-31-review"):
    """The sample with only the null-safe widget as consumer context (no billing worker)."""
    d = SAMPLES / name
    b, c = next(d.glob("base.*")), next(d.glob("candidate.*"))
    lb, lc = load_spec(b.read_bytes(), b.name), load_spec(c.read_bytes(), c.name)
    files = [("order_status_widget.tsx", (d / "context" / "order_status_widget.tsx").read_text())]
    return lb.doc, lc.doc, run_analysis(lb, lc), files


def _no_impact_reply(p):
    ev = next(e for e in p["evidence"] if e["citation"].startswith("order_status_widget"))
    quote = next(line.strip() for line in ev["text"].splitlines() if "??" in line)
    return json.dumps({"impact": "no_consumer_impact", "findings": [
        {"chunk_id": ev["chunk_id"], "quote": quote, "finding": "reads_with_null_check", "explanation": "x"}],
        "rationale": "consumer guards"})


def test_deescalation_requires_verified_no_impact_for_every_trigger():
    base, cand, a, files = _widget_only()
    inv = run_investigation(base, cand, a, build_index(base, cand, files), [n for n, _ in files],
                            llm=ScriptedLLM(_no_impact_reply))
    assert inv["decision"]["decision"] == "PROCEED"
    assert inv["decision"]["adjustments"][0]["code"] == "NO_CONSUMER_IMPACT"
    # ...but a single trigger without verified evidence keeps it at review
    first = {}

    def mixed(p):  # never gives evidence for the first change investigated, even on retry
        first.setdefault("id", p["change"]["id"])
        if p["change"]["id"] == first["id"]:
            return json.dumps({"impact": "unknown", "findings": [], "rationale": "?"})
        return _no_impact_reply(p)
    inv2 = run_investigation(base, cand, a, build_index(base, cand, files), [n for n, _ in files],
                             llm=ScriptedLLM(mixed))
    assert inv2["decision"]["decision"] == "HUMAN_REVIEW"


def test_model_claiming_safety_against_rule_evidence_stays_with_a_human():
    """The billing worker really breaks; a model that calls everything safe is contradicted by the rules."""
    _, inv = investigate("orders-31-review", llm=ScriptedLLM(_no_impact_reply))
    outcomes = {r["change_id"]: r["outcome"] for r in inv["records"]}
    assert {outcomes[c] for c in ("C003", "C004", "C005")} == {"contradictory_evidence"}
    assert inv["decision"]["decision"] == "HUMAN_REVIEW"  # neither PROCEED (unsafe) nor a model-driven BLOCK
    assert any(x["code"] == "UNRESOLVED_MODEL_EVIDENCE" for x in inv["decision"]["adjustments"])


def test_model_assertion_alone_is_not_proof_of_incompatibility():
    def claim(p):  # claims breakage, cites a real chunk, but the quote is not in it
        ev = p["evidence"][0]
        return json.dumps({"impact": "consumer_impact", "findings": [
            {"chunk_id": ev["chunk_id"], "quote": "client.crash_on_refund(order)", "finding": "reads_field",
             "explanation": "made up"}], "rationale": "trust me"})
    base, cand, a, files = _widget_only()
    inv = run_investigation(base, cand, a, build_index(base, cand, files), [n for n, _ in files], llm=ScriptedLLM(claim))
    assert {r["outcome"] for r in inv["records"]} == {"unverified_model_claim"}
    assert inv["decision"]["decision"] == "HUMAN_REVIEW"


@pytest.mark.parametrize("url,ok", [("http://127.0.0.1:8100/v1", True), ("http://localhost:9", True),
                                    ("http://169.254.169.254/latest", False), ("file:///etc/passwd", False),
                                    ("http://user:pw@127.0.0.1/", False), ("ftp://127.0.0.1", False),
                                    ("http://example.com", False)])
def test_ssrf_guard(url, ok):
    assert (check_target(url) is None) == ok


@pytest.fixture(scope="module")
def demo_target():
    from demo_target.petstore_v2 import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8197, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    yield "http://127.0.0.1:8197/v1"
    server.should_exit = True
    t.join(timeout=5)


def test_live_http_attribution(demo_target):
    a, inv = investigate("petstore-breaking", target=demo_target)
    by_rule = {}
    for r in inv["records"]:
        c = next(x for x in a["changes"] if x["id"] == r["change_id"])
        for t, res in zip(r["refined_tests"], r["refined_results"]):
            if t["kind"] == "live_http":
                by_rule.setdefault((c["rule"], c["method"], c["path"]), []).append(res)
    get = lambda rule, m, p: by_rule[(rule, m, p)][0]
    assert get("OPERATION_REMOVED", "delete", "/pets/{petId}")["status"] == "failed"
    assert get("SECURITY_ADDED", "post", "/pets")["status"] == "failed"
    assert get("REQUIRED_PARAM_ADDED", "get", "/pets")["response"]["status"] == 400
    assert get("ENUM_VALUE_ADDED", "get", "/pets")["status"] == "failed"
    assert get("RESPONSE_HEADER_REMOVED", "get", "/pets")["status"] == "failed"
    # isolation: the response-side check sent the newly required header, so it observed a 200
    assert get("ENUM_VALUE_ADDED", "get", "/pets")["request"]["headers"].get("X-Tenant-Id")
    # optional field removal cannot be observed in a reply; must not be blamed on other changes
    assert get("RESPONSE_PROPERTY_REMOVED", "get", "/pets")["status"] == "passed"
    # auth-blocked POSTs are inconclusive for non-security changes, never failures
    assert get("REQUIRED_PROPERTY_ADDED", "post", "/pets")["status"] == "inconclusive"
