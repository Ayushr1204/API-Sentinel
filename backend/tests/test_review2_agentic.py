"""Review 2 polish: bounded retry, replanning, LLM failure handling, job progress API, test-status taxonomy."""
import json
import time

import httpx
from fastapi.testclient import TestClient

from app import main
from app.agent import llm as llm_mod
from app.agent.llm import LLMError, OllamaClient, redact
from test_agent import investigate


def mock_ollama(handler):
    return OllamaClient(model="test-model:1b", transport=httpx.MockTransport(handler))


def ok_reply(content):
    return lambda req: httpx.Response(200, json={"message": {"role": "assistant", "content": content},
                                                 "done": True, "done_reason": "stop"})


# ---------------------------------------------------------------- error messages + circuit breaker
def test_error_messages_are_redacted():
    e = LLMError("model_error", "runner said token=abc123 and Bearer xyz")
    assert "abc123" not in str(e) and "xyz" not in str(e)
    assert redact("api_key=s3cret and Bearer xyz") == "[redacted] and [redacted]"


def test_fatal_llm_error_stops_further_calls():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404, json={"error": 'model "test-model:1b" not found, try pulling it first'})
    _, inv = investigate("orders-31-review", llm=mock_ollama(handler))
    assert len(calls) == 1  # one failed call, then the model is not called again
    assert inv["llm"]["failed_calls"] == 1 and inv["llm"]["skipped_after_failure"] >= 1
    assert inv["llm"]["disabled"]["category"] == "model_missing"
    assert inv["reasoning"] == "rules (LLM ollama:test-model:1b unavailable: model_missing)"
    assert inv["decision"]["decision"] == "BLOCK"  # rules still decide correctly


def test_llm_cannot_overturn_a_verified_block(monkeypatch):
    def reply(p):  # model insists nothing is affected, with a real (verifiable) quote
        ev = next((e for e in p["evidence"] if e["chunk_id"].startswith("E")), None)
        quote = ev["text"].splitlines()[0].strip() if ev else ""
        return json.dumps({"impact": "no_consumer_impact", "findings": [
            {"chunk_id": ev["chunk_id"] if ev else "E001", "quote": quote, "finding": "not_used", "explanation": "x"}],
            "rationale": "nothing uses it"})

    class Fake:
        name = "fake:optimist"

        def complete(self, system, user):
            return reply(json.loads(user))
    a, inv = investigate("petstore-breaking", llm=Fake())
    assert a["recommendation"]["decision"] == "BLOCK" and inv["decision"]["decision"] == "BLOCK"


# ---------------------------------------------------------------- bounded retry + replanning
def test_retry_recovers_evidence_hidden_by_noise():
    from app.evaluation import AGENT_CASES, _case_inputs
    from app.evidence import build_index
    from app.agent.graph import run_investigation
    from app.pipeline import run_analysis
    case = next(c for c in AGENT_CASES if c["name"] == "orders-noisy-consumers")
    base, cand, files = _case_inputs(case)
    a = run_analysis(base, cand)
    idx = build_index(base.doc, cand.doc, files)
    no_retry = run_investigation(base.doc, cand.doc, a, idx, [n for n, _ in files], None, llm=None, max_retries=0)
    full = run_investigation(base.doc, cand.doc, a, idx, [n for n, _ in files], None, llm=None)
    out = lambda inv: {r["change_id"]: r["outcome"] for r in inv["records"]}  # noqa: E731
    assert out(no_retry)["C005"] == "no_usage_found" and out(full)["C005"] == "consumer_break_confirmed"
    c005 = next(r for r in full["records"] if r["change_id"] == "C005")
    assert [x["strategy"] for x in c005["attempts"]] == ["focused", "broadened"]


def test_replanning_moves_same_operation_changes_forward():
    _, inv = investigate("petstore-breaking")
    replans = [e for e in inv["trace"] if e["node"] == "reflect" and e["detail"]["decision"] == "replan"]
    assert replans
    moved = replans[0]["detail"]["moved_forward"]
    nxt = [e for e in inv["trace"] if e["seq"] > replans[0]["seq"] and e["node"] == "retrieve"][0]
    assert nxt["change_id"] == moved[0]


# ---------------------------------------------------------------- live test statuses
def test_live_timeout_and_infra_failure_are_not_compat_results(monkeypatch):
    from app.agent import tools
    a, _ = investigate("petstore-breaking")
    change = next(c for c in a["changes"] if c["rule"] == "OPERATION_REMOVED" or c["rule"] == "PATH_REMOVED")
    test = next(t for t in a["tests"] if change["id"] in t["change_ids"])

    def boom(exc):
        def request(self, *args, **kw):
            raise exc
        return request
    from app.spec_loader import load_spec
    from app.config import settings
    d = settings.samples_dir / "petstore-breaking"
    b, c = load_spec((d / "base.yaml").read_bytes(), "b").doc, load_spec((d / "candidate.yaml").read_bytes(), "c").doc
    monkeypatch.setattr(httpx.Client, "request", boom(httpx.ReadTimeout("slow")))
    assert tools.live_request("http://127.0.0.1:9", b, c, test, change)["status"] == "timeout"
    monkeypatch.setattr(httpx.Client, "request", boom(httpx.ConnectError("refused")))
    assert tools.live_request("http://127.0.0.1:9", b, c, test, change)["status"] == "infra_error"


# ---------------------------------------------------------------- API: jobs + llm check + config
def test_job_api_streams_real_trace_events():
    c = TestClient(main.app)
    a = c.post("/api/analyses/sample/orders-31-review").json()
    r = c.post(f"/api/analyses/{a['id']}/investigations", data={"use_sample_context": "true"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    for _ in range(200):
        j = c.get(f"/api/investigation-jobs/{job_id}").json()
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "done", j
    inv = j["investigation"]
    assert j["event_count"] == len(inv["trace"])
    assert [e["seq"] for e in j["events"]] == [e["seq"] for e in inv["trace"]]
    tail = c.get(f"/api/investigation-jobs/{job_id}", params={"since": j["event_count"] - 2}).json()
    assert [e["seq"] for e in tail["events"]] == [e["seq"] for e in inv["trace"][-2:]]
    refused = c.post(f"/api/analyses/{a['id']}/investigations", data={"target_url": "http://169.254.169.254/"})
    assert refused.status_code == 422


def test_llm_check_endpoint(monkeypatch):
    c = TestClient(main.app)
    monkeypatch.setattr(llm_mod, "default_client", lambda: None)
    assert c.post("/api/llm/check").json()["category"] == "not_configured"
    ok = mock_ollama(ok_reply('{"ok": true}'))
    monkeypatch.setattr(llm_mod, "default_client", lambda: ok)
    body = c.post("/api/llm/check").json()
    assert body["ok"] is True and body["model"] == "test-model:1b" and body["provider"] == "ollama"

    def down(req):
        raise httpx.ConnectError("refused")
    bad = mock_ollama(down)
    monkeypatch.setattr(llm_mod, "default_client", lambda: bad)
    body = c.post("/api/llm/check").json()
    assert body["ok"] is False and body["category"] == "unavailable"
    cfg = c.get("/api/config").json()
    assert cfg["llm"]["provider"] == "ollama" and cfg["llm"]["endpoint"] == "http://localhost:11434"
