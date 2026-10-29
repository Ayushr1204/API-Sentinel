"""Ollama (local inference) adapter.

EVERYTHING HERE IS MOCKED (httpx.MockTransport): no Ollama install, no downloaded model, no network, no credits.
These tests prove the adapter's request format, error handling and safety behaviour, not that a local model runs:
real inference is verified only with "Run self-test" (POST /api/llm/selftest) on a machine where Ollama is running."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.agent import graph
from app.agent import llm as llm_mod
from app.agent.llm import LLMError, OllamaClient
from test_agent import investigate

MODEL = "qwen3.5:4b"


def reply(content="", tool_calls=None, done_reason="stop", thinking=None, prompt_tokens=120):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if thinking:
        msg["thinking"] = thinking
    return httpx.Response(200, json={"model": MODEL, "created_at": "2026-10-28T00:00:00Z", "message": msg,
                                     "done": True, "done_reason": done_reason, "total_duration": 1_500_000_000,
                                     "prompt_eval_count": prompt_tokens, "eval_count": 30})


def call(name, arguments):
    """Ollama's native tool-call shape: arguments is a JSON object, not a string."""
    return {"function": {"name": name, "arguments": arguments}}


def ollama(handler, model=None):
    return OllamaClient(model=model, transport=httpx.MockTransport(handler))


def chat_only(fn):
    """Route /api/chat to fn; anything else is a test bug."""
    def handler(req):
        assert req.url.path == "/api/chat", req.url.path
        return fn(json.loads(req.content))
    return handler


def runtime(models=(MODEL,), caps=("completion", "tools", "thinking"), chat=None):
    """A fake Ollama server: /api/version, /api/tags, /api/show and (optionally) /api/chat."""
    def handler(req):
        p = req.url.path
        if p == "/api/version":
            return httpx.Response(200, json={"version": "0.12.9"})
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "model": m, "size": 3_400_000_000} for m in models]})
        if p == "/api/show":
            return httpx.Response(200, json={"capabilities": list(caps), "details": {"family": "qwen35"}})
        if p == "/api/chat" and chat:
            return chat(json.loads(req.content))
        return httpx.Response(404, json={"error": "not found"})
    return handler


# ---------------------------------------------------------------- 1. configuration and defaults
def test_defaults_and_provider_selection(monkeypatch):
    monkeypatch.delenv("SENTINEL_LLM")
    assert llm_mod.default_client() is None  # Ollama is never assumed: no provider named -> rules mode
    monkeypatch.setenv("SENTINEL_LLM_PROVIDER", "ollama")
    c = llm_mod.default_client()
    assert isinstance(c, OllamaClient) and c.model == MODEL and c.name == f"ollama:{MODEL}"
    assert c.base_url == "http://localhost:11434" and c.num_ctx == 8192 and c.temperature == 0.0
    assert c.think is False and c.timeout == 180.0 and c.supports_tools and not c.mocked
    assert llm_mod.configuration_problem() is None
    monkeypatch.delenv("SENTINEL_LLM_PROVIDER")
    monkeypatch.setenv("LLM_PROVIDER", "local")  # alias from the brief
    assert isinstance(llm_mod.default_client(), OllamaClient)


@pytest.mark.parametrize("name", ["huggingface", "openai", "gemini", "anthropic"])
def test_removed_hosted_providers_are_refused_not_used(monkeypatch, name):
    """Hosted providers were removed; naming one leaves the app in rules-only mode and says why."""
    monkeypatch.delenv("SENTINEL_LLM")
    monkeypatch.setenv("SENTINEL_LLM_PROVIDER", name)
    assert llm_mod.default_client() is None
    assert "only LLM provider is 'ollama'" in llm_mod.configuration_problem()
    assert not hasattr(llm_mod, "OpenAIClient") and not hasattr(llm_mod, "HuggingFaceClient")


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("SENTINEL_LLM_MODEL", "some-other-model")  # OLLAMA_MODEL takes precedence
    monkeypatch.setenv("OLLAMA_MODEL", "granite4:3b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11500/")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "not-a-number")
    monkeypatch.setenv("SENTINEL_LLM_TEMPERATURE", "0.2")
    monkeypatch.setenv("OLLAMA_THINK", "true")
    c = OllamaClient()
    assert c.model == "granite4:3b" and c.base_url == "http://127.0.0.1:11500"
    assert c.num_ctx == 16384 and c.timeout == 180.0 and c.temperature == 0.2 and c.think is True


def test_config_endpoint_reports_the_local_provider(monkeypatch):
    monkeypatch.delenv("SENTINEL_LLM")
    monkeypatch.setenv("SENTINEL_LLM_PROVIDER", "ollama")
    r = TestClient(main.app).get("/api/config")
    llm = r.json()["llm"]
    assert llm["provider"] == "ollama" and llm["model"] == MODEL
    assert llm["endpoint"] == "http://localhost:11434"


# ---------------------------------------------------------------- 4. request format + successful inference
def test_request_uses_native_chat_api():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), headers=dict(req.headers), body=json.loads(req.content))
        return reply('{"ok": true}', thinking="internal notes")
    c = ollama(handler)
    assert c.complete("sys", "user") == '{"ok": true}'  # thinking text is never returned as the answer
    b = seen["body"]
    assert seen["url"] == "http://localhost:11434/api/chat" and "authorization" not in seen["headers"]
    assert b["model"] == MODEL and b["stream"] is False and b["format"] == "json" and b["think"] is False
    assert b["options"] == {"temperature": 0.0, "num_ctx": 8192} and b["keep_alive"] == "10m"
    assert [m["role"] for m in b["messages"]] == ["system", "user"] and "tools" not in b
    assert c.last_stats["prompt_tokens"] == 120 and c.last_stats["total_ms"] == 1500


def test_check_reports_success_and_stats():
    out = llm_mod.check(ollama(chat_only(lambda b: reply('{"ok": true}'))))
    assert out["ok"] and out["resolved_model"] == MODEL and out["stats"]["output_tokens"] == 30


def test_full_context_is_flagged():
    c = ollama(chat_only(lambda b: reply('{"ok": true}', prompt_tokens=8192)))
    c.complete("s", "u")
    assert "OLLAMA_NUM_CTX" in c.note


# ---------------------------------------------------------------- 2, 3, 9. unavailable / missing model / timeout / errors
def test_ollama_not_running():
    def down(req):
        raise httpx.ConnectError("[WinError 10061] No connection could be made")
    with pytest.raises(LLMError) as ei:
        ollama(down).complete("s", "u")
    e = ei.value
    assert e.category == "unavailable" and e.fatal and "localhost:11434" in str(e) and "ollama.com/download" in str(e)

    def connect_timeout(req):
        raise httpx.ConnectTimeout("timed out")
    with pytest.raises(LLMError) as ei:
        ollama(connect_timeout).complete("s", "u")
    assert ei.value.category == "unavailable"


def test_missing_model_names_the_pull_command():
    def handler(req):
        return httpx.Response(404, json={"error": f'model "{MODEL}" not found, try pulling it first'})
    with pytest.raises(LLMError) as ei:
        ollama(handler).complete("s", "u")
    assert ei.value.category == "model_missing" and ei.value.fatal and f"ollama pull {MODEL}" in str(ei.value)


def test_timeout_is_not_retried():
    calls = []

    def slow(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow")
    with pytest.raises(LLMError) as ei:
        ollama(slow).complete("s", "u")
    assert ei.value.category == "timeout" and ei.value.fatal and len(calls) == 1  # one slow call, not several


@pytest.mark.parametrize("status,error,category", [
    (500, "model requires more system memory (9.1 GiB) than is available (5.2 GiB)", "resources"),
    (500, "CUDA error: out of memory", "resources"),
    (500, "llama runner process has terminated: exit status 2", "model_error"),
    (400, "invalid options", "bad_request"),
])
def test_runtime_errors_are_classified(status, error, category):
    with pytest.raises(LLMError) as ei:
        ollama(lambda r: httpx.Response(status, json={"error": error})).complete("s", "u")
    assert ei.value.category == category and ei.value.fatal


# ---------------------------------------------------------------- 5. malformed output
def test_malformed_replies_are_rejected():
    with pytest.raises(LLMError) as ei:
        ollama(lambda r: httpx.Response(200, text="<html>proxy</html>")).complete("s", "u")
    assert ei.value.category == "bad_response"
    with pytest.raises(LLMError) as ei:
        ollama(chat_only(lambda b: reply("", done_reason="length"))).complete("s", "u")
    assert ei.value.category == "bad_response" and "OLLAMA_NUM_CTX" in str(ei.value)


def test_schema_invalid_assessment_falls_back_to_rules():
    c = ollama(chat_only(lambda b: reply(json.dumps({"impact": "probably broken", "findings": []}))))
    _, inv = investigate("orders-31-review", llm=c)
    assert all(r["mode"] == "rules" for r in inv["records"]) and inv["decision"]["decision"] == "BLOCK"
    assert inv["llm"]["mode"] == "rules_fallback" and inv["llm"]["assessments_by_llm"] == 0


def test_model_without_thinking_mode_drops_the_flag_once():
    bodies = []

    def handler(b):
        bodies.append(b)
        if "think" in b:
            return httpx.Response(400, json={"error": f'"{MODEL}" does not support thinking'})
        return reply('{"ok": true}')
    c = ollama(chat_only(handler))
    assert c.complete("s", "u") == '{"ok": true}' and "think" in c.dropped_params
    c.complete("s", "u")
    assert len(bodies) == 3 and "think" not in bodies[-1]


# ---------------------------------------------------------------- 6, 7. structured assessment + real tool round trip
def _tool_flow(log):
    """Round 1: the model asks for search_evidence. Round 2: it answers citing a chunk the BACKEND returned."""
    def handler(b):
        log.append(b)
        tool_msgs = [m for m in b["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            assert b["tools"][0]["function"]["name"] == "search_evidence" and "format" not in b
            return reply("", tool_calls=[call("search_evidence", {"query": "getOrder trackingUrl", "source": "consumer"})])
        result = json.loads(tool_msgs[-1]["content"])
        hit = next((r for r in result.get("results", []) if "getOrder" in r["text"]), None)
        if hit is None:
            return reply(json.dumps({"impact": "unknown", "findings": [], "rationale": "nothing found"}))
        quote = next(line.strip() for line in hit["text"].splitlines() if "getOrder" in line)
        return reply(json.dumps({"impact": "consumer_impact", "rationale": "billing worker calls getOrder()",
                                 "findings": [{"chunk_id": hit["chunk_id"], "quote": quote,
                                               "finding": "calls_sdk_method", "explanation": "uses the SDK"}]}))
    return handler


def test_tool_calls_are_executed_by_the_backend_and_returned_in_ollama_format():
    log = []
    _, inv = investigate("orders-31-review", llm=ollama(chat_only(_tool_flow(log))))
    rec = next(r for r in inv["records"] if r["change_id"] == "C003")
    assert rec["mode"] == "llm" and rec["findings"][0]["verified"] and rec["outcome"] == "consumer_break_confirmed"
    ev = next(e for e in inv["trace"] if e["node"] == "assess" and e["change_id"] == "C003")
    calls = ev["detail"]["tool_calls"]
    assert calls[0]["tool"] == "search_evidence" and calls[0]["error"] is None and calls[0]["result_ids"]
    assert rec["findings"][0]["chunk_id"] in calls[0]["result_ids"]  # cited evidence came from the executed tool
    assert ev["detail"]["llm"]["live"] is False  # mocked transport is never labelled as real inference
    second = next(b for b in log if any(m["role"] == "tool" for m in b["messages"]))
    asst = next(m for m in second["messages"] if m["role"] == "assistant" and m.get("tool_calls"))
    assert isinstance(asst["tool_calls"][0]["function"]["arguments"], dict)  # objects, as Ollama expects
    tool = next(m for m in second["messages"] if m["role"] == "tool")
    assert tool["tool_name"] == "search_evidence" and json.loads(tool["content"])["results"]
    assert inv["llm"]["tool_calls"] >= 1 and inv["llm"]["mode"] in ("llm_assisted", "partial_fallback")


def test_unknown_tools_and_invalid_arguments_are_refused():
    def handler(b):
        if not any(m["role"] == "tool" for m in b["messages"]):
            return reply("", tool_calls=[call("search_evidence", {"query": 42}), call("run_shell", {"cmd": "del *"}),
                                         call("get_chunk", {"chunk_id": "E999"})])
        return reply(json.dumps({"impact": "unknown", "findings": [], "rationale": "no evidence"}))
    _, inv = investigate("orders-31-review", llm=ollama(chat_only(handler)))
    ev = next(e for e in inv["trace"] if e["node"] == "assess" and e["detail"].get("tool_calls"))
    errs = {c["tool"]: c["error"] for c in ev["detail"]["tool_calls"]}
    assert errs["search_evidence"].startswith("query must be") and errs["run_shell"] == "unknown tool"
    assert errs["get_chunk"] == "unknown chunk_id"
    assert all(not c["result_ids"] for c in ev["detail"]["tool_calls"])


def test_model_without_tool_support_is_assessed_in_json_mode():
    def handler(b):
        if "tools" in b:
            return httpx.Response(400, json={"error": f"registry.ollama.ai/library/{MODEL} does not support tools"})
        assert b["format"] == "json"
        return reply(json.dumps({"impact": "unknown", "findings": [], "rationale": "no evidence"}))
    _, inv = investigate("orders-31-review", llm=ollama(chat_only(handler)))
    assert all(r["mode"] == "llm" for r in inv["records"])
    assert all("does not support tool calling" in (r["note"] or "") for r in inv["records"])


# ---------------------------------------------------------------- 10. bounded tool rounds, tool calls and steps
def test_tool_loop_is_bounded():
    bodies = []

    def handler(b):
        bodies.append(b)
        if "tools" in b:  # a model that never stops calling tools
            return reply("", tool_calls=[call("search_evidence", {"query": f"q{len(bodies)}"})] * 3)
        return reply(json.dumps({"impact": "unknown", "findings": [], "rationale": "budget"}))
    text, log, _ = llm_mod.assess_with_tools(ollama(chat_only(handler)), "sys", "user",
                                             lambda n, a: ({"results": []}, {"tool": n, "arguments": a,
                                                                             "result_ids": [], "error": None}))
    assert len(bodies) == llm_mod.MAX_TOOL_ROUNDS + 1  # 3 tool rounds + 1 final answer, never more
    assert "tools" not in bodies[-1] and bodies[-1]["format"] == "json"
    executed = [e for e in log if e["error"] is None]
    assert len(executed) == llm_mod.MAX_TOOL_CALLS and len(log) == 3 * llm_mod.MAX_TOOL_ROUNDS
    assert json.loads(text)["impact"] == "unknown"


def test_step_budget_stops_the_investigation(monkeypatch):
    monkeypatch.setattr(graph, "STEP_BUDGET", 1)
    calls = []

    def handler(b):
        calls.append(1)
        return reply(json.dumps({"impact": "unknown", "findings": [], "rationale": "x"}))
    a, inv = investigate("orders-31-noisy-consumers", llm=ollama(chat_only(handler)))
    assert inv["stop_reason"] and "step budget" in inv["stop_reason"]
    assert any(x["code"] == "BUDGET_STOP" for x in inv["decision"]["adjustments"])
    assert inv["decision"]["decision"] != "PROCEED" and len(calls) <= 2


# ---------------------------------------------------------------- 11. fallback when local inference fails
@pytest.mark.parametrize("exc,category", [(httpx.ConnectError("refused"), "unavailable"),
                                          (httpx.ReadTimeout("slow"), "timeout")])
def test_failed_local_inference_uses_rules_and_is_marked(exc, category):
    calls = []

    def handler(req):
        calls.append(1)
        raise exc
    rules_a, rules_inv = investigate("orders-31-review", llm=None)
    _, inv = investigate("orders-31-review", llm=ollama(handler))
    assert len(calls) == 1  # circuit breaker: one failed request per run, not one per change
    assert inv["llm"]["disabled"]["category"] == category and inv["llm"]["mode"] == "rules_fallback"
    assert inv["reasoning"].startswith(f"rules (LLM ollama:{MODEL} unavailable: {category})")
    assert inv["decision"]["decision"] == rules_inv["decision"]["decision"]  # same as rules-only
    assert all(r["mode"] == "rules" for r in inv["records"])
    assert rules_inv["llm"]["mode"] == "rules_only" and rules_inv["reasoning"] == "rules (no LLM configured)"


def test_rules_only_switch(monkeypatch):
    monkeypatch.setenv("SENTINEL_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SENTINEL_LLM", "off")
    assert llm_mod.default_client() is None and llm_mod.configuration_problem() is None


# ---------------------------------------------------------------- 12, 13. release-decision safety
def test_local_model_cannot_overturn_a_test_confirmed_block():
    c = ollama(chat_only(lambda b: reply(json.dumps({"impact": "no_consumer_impact", "findings": [],
                                                     "rationale": "looks fine to me"}))))
    a, inv = investigate("petstore-breaking", llm=c)
    assert a["recommendation"]["decision"] == "BLOCK" and inv["decision"]["decision"] == "BLOCK"
    assert inv["decision"]["final_approval"].startswith("required")


def test_infrastructure_failures_are_not_evidence_of_a_break(monkeypatch):
    """Live replays that time out or hit infrastructure errors, plus a model that is down, must not escalate."""
    statuses = iter(["timeout", "infra_error"] * 20)
    monkeypatch.setattr(graph, "live_request", lambda *a, **k: {"status": next(statuses), "errors": [],
                                                                "summary": "simulated infrastructure failure"})

    def down(req):
        raise httpx.ConnectError("refused")
    a, inv = investigate("orders-31-review", with_context=False, llm=ollama(down),
                         target="http://127.0.0.1:8100")
    assert a["recommendation"]["decision"] == "HUMAN_REVIEW"
    assert inv["decision"]["decision"] == "HUMAN_REVIEW"  # not BLOCK
    assert not any(o in ("live_break_confirmed", "consumer_break_confirmed")
                   for o in inv["decision"]["outcomes"].values())


# ---------------------------------------------------------------- 14. diagnostics API
def test_status_reports_runtime_model_and_capabilities(monkeypatch):
    client = ollama(runtime())
    monkeypatch.setattr(llm_mod, "default_client", lambda: client)
    rt = TestClient(main.app).get("/api/llm/status").json()["runtime"]
    assert rt["reachable"] and rt["version"] == "0.12.9" and rt["model_present"] is True
    assert rt["capabilities"] == ["completion", "tools", "thinking"] and rt["error"] is None
    assert rt["endpoint"] == "http://localhost:11434"


def test_status_distinguishes_missing_model_and_unreachable(monkeypatch):
    client = ollama(runtime(models=("llama3.2:3b",)))
    monkeypatch.setattr(llm_mod, "default_client", lambda: client)
    rt = TestClient(main.app).get("/api/llm/status").json()["runtime"]
    assert rt["reachable"] and rt["model_present"] is False and f"ollama pull {MODEL}" in rt["error"]
    assert rt["installed_models"] == ["llama3.2:3b"]

    def down(req):
        raise httpx.ConnectError("refused")
    client2 = ollama(down)
    monkeypatch.setattr(llm_mod, "default_client", lambda: client2)
    rt = TestClient(main.app).get("/api/llm/status").json()["runtime"]
    assert rt["reachable"] is False and rt["model_present"] is None and "not reachable" in rt["error"]


def test_status_never_downloads_or_infers(monkeypatch):
    paths = []

    def handler(req):
        paths.append(req.url.path)
        return runtime()(req)
    client = ollama(handler)
    monkeypatch.setattr(llm_mod, "default_client", lambda: client)
    TestClient(main.app).get("/api/llm/status")
    assert set(paths) <= {"/api/version", "/api/tags", "/api/show"}  # no /api/pull, no /api/chat


def test_last_results_are_recorded_and_persisted(monkeypatch):

    def chat(b):
        sys = b["messages"][0]["content"]
        if "testing tool use" in sys:
            tool = [m for m in b["messages"] if m["role"] == "tool"]
            if not tool:
                return reply("", tool_calls=[call("search_evidence", {"query": "trackingUrl"})])
            return reply(json.dumps({"chunk_id": json.loads(tool[0]["content"])["results"][0]["chunk_id"]}))
        if "ping" in b["messages"][-1]["content"]:
            return reply('{"ok": true}')
        return reply(json.dumps({"impact": "unknown", "findings": [], "rationale": "x"}))
    client = ollama(runtime(chat=chat))
    monkeypatch.setattr(llm_mod, "default_client", lambda: client)
    c = TestClient(main.app)
    before = c.get("/api/llm/status").json()["last"]
    assert before == {"check": None, "selftest": None, "investigation": None}  # "not tested" until something runs
    assert c.post("/api/llm/check").json()["ok"]
    st = c.post("/api/llm/selftest").json()
    assert [s["id"] for s in st["steps"]] == ["A", "B", "C"] and all(s["ok"] for s in st["steps"]), st
    assert st["live"] is False and st["provider"] == "ollama"
    assert st["steps"][2]["answer_chunk_id"] in st["steps"][2]["tool_result_ids"]
    a = c.post("/api/analyses/sample/orders-31-review").json()
    inv = c.post(f"/api/analyses/{a['id']}/investigate", data={"use_sample_context": "true"}).json()
    r = c.get("/api/llm/status")
    last = r.json()["last"]
    assert last["check"]["ok"] and last["check"]["live"] is False
    assert [s["ok"] for s in last["selftest"]["steps"]] == [True, True, True]
    assert last["investigation"]["status"] == "completed" and last["investigation"]["id"] == inv["id"]
    assert last["investigation"]["mode"] == inv["llm"]["mode"] and last["investigation"]["live"] is False
    # persisted: the stored investigation and audit chain include the run
    assert c.get(f"/api/analyses/{a['id']}/investigation").json()["id"] == inv["id"]
    actions = [e["action"] for e in c.get("/api/audit").json()["events"]]
    assert {"llm.check", "llm.selftest"} <= set(actions)
    assert c.get("/api/audit/verify").json()["valid"] is True


def test_selftest_stops_after_fatal_unavailable(monkeypatch):
    def down(req):
        raise httpx.ConnectError("refused")
    client = ollama(down)
    monkeypatch.setattr(llm_mod, "default_client", lambda: client)
    st = TestClient(main.app).post("/api/llm/selftest").json()
    assert not st["ok"] and st["steps"][0]["category"] == "unavailable"
    assert st["steps"][1]["skipped"] and st["steps"][2]["skipped"]


# ---------------------------------------------------------------- regression from the first REAL local-model run
def test_model_findings_from_a_consumer_that_never_calls_the_operation_are_rejected():
    """Real qwen3.5:4b run (2026-10-28): the model cited the billing worker, which only calls getOrder(), as broken by
    the createOrder (POST /orders) response changes C001/C002. Verbatim quotes passed, but the rules' scoping (the file
    must call the affected operation) was not applied to model findings, so C001/C002 were wrongly 'confirmed'."""
    def handler(b):
        if any(m["role"] == "tool" for m in b["messages"]) or "tools" not in b:
            return reply(json.dumps({"impact": "unknown", "findings": [], "rationale": "x"}))
        return reply("", tool_calls=[call("search_evidence", {"query": "billing worker state", "source": "consumer"})])

    def answer(b):
        tool = [m for m in b["messages"] if m["role"] == "tool"]
        if not tool:
            return handler(b)
        hit = next(r for r in json.loads(tool[-1]["content"])["results"] if "getOrder" in r["text"])
        return reply(json.dumps({"impact": "consumer_impact", "rationale": "worker enumerates states",
                                 "findings": [{"chunk_id": hit["chunk_id"], "quote": hit["text"],  # whole chunk
                                               "finding": "enumerates_known_values", "explanation": "if/elif"}]}))
    _, inv = investigate("orders-31-review", llm=ollama(chat_only(answer)))
    out = inv["decision"]["outcomes"]
    assert out["C001"] not in ("consumer_break_confirmed", "consumer_usage_found")  # POST /orders: never called
    rec1 = next(r for r in inv["records"] if r["change_id"] == "C001")
    assert not rec1["findings"][0]["verified"] and "never calls POST /orders" in rec1["findings"][0]["verification"]
    rec4 = next(r for r in inv["records"] if r["change_id"] == "C004")  # GET /orders/{orderId}: really called
    assert rec4["findings"][0]["verified"] and "getOrder" in rec4["findings"][0]["operation_call"]["text"]
    assert out["C004"] in ("consumer_break_confirmed", "consumer_usage_found")
    assert inv["decision"]["decision"] != "PROCEED"
