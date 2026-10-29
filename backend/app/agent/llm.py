"""Optional LLM reasoning for the `assess` node.

The LLM never decides alone: every finding it returns must cite a retrieved chunk AND quote text that appears
verbatim in that chunk; anything else is discarded by the verifier. The only model runtime is a local Ollama server
(SENTINEL_LLM_PROVIDER=ollama). Without it this module is unused and the agent runs its rule-based assessor (and the
trace says so).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Protocol

ALLOWED_FINDINGS = ["reads_field", "reads_without_null_check", "reads_with_null_check", "enumerates_known_values",
                    "handles_new_value", "calls_sdk_method", "reads_header", "sends_removed_value", "sends_field",
                    "calls_operation", "not_used"]

SYSTEM = (
    "You are the impact assessor inside API Sentinel, a release gate for REST APIs. You receive ONE structural "
    "change between two OpenAPI specs, the deterministic contract-test result for it, and evidence chunks retrieved "
    "from consumer code, logs and the specs. Decide whether the evidence shows existing consumers depend on the "
    "changed element in a way the change breaks. Use ONLY the evidence provided. Every finding must cite a chunk_id "
    "and include an exact quote copied verbatim from that chunk. If the evidence is insufficient, say impact "
    "'unknown'. Reply with a single JSON object and nothing else."
)

SCHEMA_HINT = {
    "impact": "consumer_impact | no_consumer_impact | unknown",
    "confidence": "low | medium | high",
    "findings": [{"chunk_id": "E001", "quote": "exact text from the chunk", "finding": "|".join(ALLOWED_FINDINGS),
                  "explanation": "one sentence"}],
    "handled_enum_values": ["only if the consumer enumerates specific values, else null"],
    "rationale": "two or three sentences",
}


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


_SECRET_RE = re.compile(r"(Bearer\s+\S+|\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+)", re.I)


def redact(text: str) -> str:
    """Defence in depth for messages that are shown and stored: mask anything that looks like a credential."""
    return _SECRET_RE.sub("[redacted]", text)


class LLMError(Exception):
    """A model-runtime failure, classified. The message is safe to show and store."""

    def __init__(self, category: str, message: str, *, retryable: bool = False, fatal: bool = False,
                 status: int | None = None):
        super().__init__(redact(message))
        self.category, self.retryable, self.fatal, self.status = category, retryable, fatal, status

    def to_dict(self) -> dict:
        return {"category": self.category, "message": str(self), "status": self.status, "fatal": self.fatal}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class OllamaClient:
    """Local inference through Ollama's native API (POST /api/chat on http://localhost:11434 by default).

    Why the native API and not Ollama's OpenAI-compatible /v1 endpoint: /api/chat lets us set options.num_ctx (Ollama
    defaults small GPUs to a 4K context, which would silently truncate API Sentinel's evidence-heavy prompts), `format`
    for JSON, and `think`. No key, no token, and no model download: the model must be pulled by the user."""
    provider = "ollama"
    label = "Ollama"
    default_model = "qwen3.5:4b"
    supports_tools = True

    def __init__(self, model: str | None = None, transport: Any = None):
        import httpx
        self._httpx = httpx
        self.mocked = transport is not None
        # OLLAMA_MODEL wins over SENTINEL_LLM_MODEL (the generic, lower-priority name)
        self.model = model or os.environ.get("OLLAMA_MODEL") or os.environ.get("SENTINEL_LLM_MODEL") or self.default_model
        self.name = f"ollama:{self.model}"
        self.base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self.timeout = _env_float("OLLAMA_TIMEOUT", 180.0)
        self.num_ctx = _env_int("OLLAMA_NUM_CTX", 8192)
        self.temperature = _env_float("SENTINEL_LLM_TEMPERATURE", 0.0)
        think = os.environ.get("OLLAMA_THINK", "false").strip().lower()
        self.think: Any = {"false": False, "true": True, "": None}.get(think, think)
        self.keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "10m")
        self.dropped_params: set[str] = set()
        self.note: str | None = None
        self.last_stats: dict | None = None
        self._http = httpx.Client(base_url=self.base_url, transport=transport, follow_redirects=False,
                                  timeout=httpx.Timeout(self.timeout, connect=5.0))

    # ------------------------------------------------------------ transport + errors
    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        httpx = self._httpx
        try:
            kw = {"timeout": httpx.Timeout(timeout, connect=5.0)} if timeout else {}
            return self._http.request(method, path, json=body, **kw)
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise LLMError("unavailable", f"Ollama is not reachable at {self.base_url} (connection timed out). "
                               "Start the Ollama app (or run `ollama serve`).", fatal=True) from None
            raise LLMError("timeout", f"The local model did not answer within {self.timeout:.0f} s. It may be too "
                           "large for this machine or still loading; try a smaller model, a smaller OLLAMA_NUM_CTX, or "
                           "a larger OLLAMA_TIMEOUT.", fatal=True) from None
        except httpx.HTTPError as exc:
            raise LLMError("unavailable", f"Ollama is not reachable at {self.base_url} ({type(exc).__name__}). "
                           "Install it from https://ollama.com/download/windows and start the Ollama app "
                           "(or run `ollama serve`).", fatal=True) from None

    @staticmethod
    def _error_text(r) -> str:
        try:
            body = r.json()
            return str(body.get("error") or body) if isinstance(body, dict) else str(body)
        except ValueError:
            return r.text[:300]

    def classify(self, r) -> LLMError:
        msg = redact(self._error_text(r))[:300]
        low = msg.lower()
        if r.status_code == 404 or ("not found" in low and "model" in low) or "try pulling" in low:
            return LLMError("model_missing", f"Model '{self.model}' is not installed in Ollama. Download it once with: "
                            f"ollama pull {self.model}", fatal=True, status=r.status_code)
        if any(w in low for w in ("more system memory", "out of memory", "cuda error", "insufficient memory",
                                  "unable to allocate", "failed to load model")):
            return LLMError("resources", f"Ollama could not run '{self.model}' on this machine ({msg}). Try a smaller "
                            "model or a smaller OLLAMA_NUM_CTX.", fatal=True, status=r.status_code)
        if r.status_code >= 500:
            return LLMError("model_error", f"Ollama reported an inference error (HTTP {r.status_code}): {msg}",
                            fatal=True, status=r.status_code)
        return LLMError("bad_request", f"Ollama refused the request (HTTP {r.status_code}): {msg}", fatal=True,
                        status=r.status_code)

    # ------------------------------------------------------------ chat
    @staticmethod
    def _to_ollama(messages: list[dict]) -> list[dict]:
        """The shared tool-loop history (JSON-string arguments, tool_call_id) -> Ollama's format: arguments are objects,
        tool results carry tool_name instead of tool_call_id."""
        out = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                calls = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function", tc)
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except ValueError:
                            args = {}
                    calls.append({"function": {"name": fn.get("name", ""), "arguments": args if isinstance(args, dict) else {}}})
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            elif m.get("role") == "tool":
                out.append({"role": "tool", "content": str(m.get("content", "")), "tool_name": m.get("name", "")})
            else:
                out.append({"role": m["role"], "content": str(m.get("content") or "")})
        return out

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str | None = None,
             json_mode: bool = False) -> dict:
        body: dict[str, Any] = {"model": self.model, "messages": self._to_ollama(messages), "stream": False,
                                "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
                                "keep_alive": self.keep_alive}
        if self.think is not None and "think" not in self.dropped_params:
            body["think"] = self.think
        if tools and tool_choice != "none":  # Ollama has no tool_choice; "none" = do not offer tools
            body["tools"] = tools  # no `format` here: constraining the output to JSON would suppress tool calls
        elif json_mode or tool_choice == "none":  # "none" is only used to ask for the final JSON answer
            body["format"] = "json"
        r = self._request("POST", "/api/chat", body)
        if r.status_code == 400 and "think" in self._error_text(r).lower() and "think" in body:
            body.pop("think")  # model without a thinking mode: drop the flag once and remember
            self.dropped_params.add("think")
            r = self._request("POST", "/api/chat", body)
        if r.status_code == 400 and "tools" in body and "tool" in self._error_text(r).lower():
            raise LLMError("unsupported_tools", f"Ollama model '{self.model}' does not support tool calling.",
                           status=400)
        if r.status_code >= 400:
            raise self.classify(r)
        try:
            data = r.json()
            msg = data.get("message") or {}
        except (ValueError, AttributeError):
            raise LLMError("bad_response", "Ollama returned a reply that is not JSON.") from None
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            calls.append({"id": str(tc.get("id") or f"call_{i}"), "name": str(fn.get("name") or ""),
                          "arguments": args if isinstance(args, str) else json.dumps(args if args is not None else {})})
        content = msg.get("content") or ""
        self.last_stats = {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
                           "total_ms": round((data.get("total_duration") or 0) / 1e6), "done_reason": data.get("done_reason")}
        if data.get("prompt_eval_count") and data["prompt_eval_count"] >= self.num_ctx:
            self.note = (f"The prompt filled the whole {self.num_ctx}-token context, so Ollama may have truncated it; "
                         "raise OLLAMA_NUM_CTX if memory allows.")
        if not content.strip() and not calls:
            why = data.get("done_reason")
            raise LLMError("bad_response", f"The local model returned an empty answer (done_reason={why})."
                           + (" It ran out of tokens; raise OLLAMA_NUM_CTX." if why == "length" else ""))
        return {"content": content, "tool_calls": calls, "finish_reason": data.get("done_reason"),
                "has_reasoning": bool(msg.get("thinking"))}

    def complete(self, system: str, user: str) -> str:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         json_mode=True)["content"]

    # ------------------------------------------------------------ diagnostics (no inference, no download)
    def status(self) -> dict:
        out: dict[str, Any] = {"provider": "ollama", "endpoint": self.base_url, "model": self.model,
                               "reachable": False, "version": None, "model_present": None, "installed_models": [],
                               "capabilities": None, "num_ctx": self.num_ctx, "think": self.think,
                               "temperature": self.temperature, "timeout_s": self.timeout, "error": None}
        try:
            v = self._request("GET", "/api/version", timeout=5.0)
            out["reachable"] = v.status_code < 400
            out["version"] = (v.json() or {}).get("version") if out["reachable"] else None
            tags = self._request("GET", "/api/tags", timeout=10.0)
            models = [m.get("name") or m.get("model") for m in (tags.json() or {}).get("models", [])] \
                if tags.status_code < 400 else []
            out["installed_models"] = sorted(n for n in models if n)
            wanted = self.model if ":" in self.model else f"{self.model}:latest"
            out["model_present"] = any(n in (self.model, wanted) for n in out["installed_models"])
            if out["model_present"]:
                show = self._request("POST", "/api/show", {"model": self.model}, timeout=10.0)
                if show.status_code < 400:
                    caps = (show.json() or {}).get("capabilities")
                    out["capabilities"] = caps if isinstance(caps, list) else None
            else:
                out["error"] = f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"
        except LLMError as exc:
            out["error"] = str(exc)
        except ValueError:
            out["error"] = "Ollama answered with something that is not JSON."
        return out


PROVIDER_ALIASES = {"ollama": "ollama", "local": "ollama"}


def _named_provider() -> str:
    """SENTINEL_LLM_PROVIDER (project convention); LLM_PROVIDER is accepted as an alias."""
    return (os.environ.get("SENTINEL_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER") or "").strip().lower()


def configuration_problem() -> str | None:
    """A human-readable reason the configured LLM cannot be used."""
    if os.environ.get("SENTINEL_LLM", "auto").lower() == "off":
        return None
    named = _named_provider()
    if named and named not in PROVIDER_ALIASES:
        return (f"SENTINEL_LLM_PROVIDER='{named}' is not supported. The only LLM provider is 'ollama' (local); "
                "remove the line (or set SENTINEL_LLM=off) for rules-only mode.")
    return None


def default_client() -> LLMClient | None:
    """The local Ollama client when SENTINEL_LLM_PROVIDER=ollama, else None (rules-only mode). Never assumed: the app
    cannot know whether Ollama is installed."""
    if os.environ.get("SENTINEL_LLM", "auto").lower() == "off":
        return None
    if PROVIDER_ALIASES.get(_named_provider()) != "ollama":
        return None
    return OllamaClient()


# ---------------------------------------------------------------- most recent results, for the diagnostics panel
LAST: dict[str, dict | None] = {"check": None, "selftest": None, "investigation": None}


def remember(kind: str, value: dict) -> None:
    LAST[kind] = {**value, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def check(client: LLMClient) -> dict:
    """One minimal real request, to prove inference works end to end."""
    started = time.perf_counter()
    try:
        raw = client.complete('Reply with the JSON object {"ok": true} and nothing else.', '{"ping": true}')
        ok = '"ok"' in raw.replace(" ", "")
        extra = {"model_note": client.note} if getattr(client, "note", None) else {}
        if getattr(client, "last_stats", None):
            extra["stats"] = client.last_stats
        return {"ok": ok, "category": None if ok else "bad_response", **extra, "resolved_model": client.model,
                "message": "The model answered." if ok else "The model answered, but not with the expected JSON.",
                "latency_ms": round((time.perf_counter() - started) * 1000), "reply_excerpt": redact(raw[:120])}
    except LLMError as exc:
        return {"ok": False, **exc.to_dict(), "latency_ms": round((time.perf_counter() - started) * 1000)}


def build_prompt(change: dict, test_summary: list[dict], evidence: list[dict]) -> str:
    return json.dumps({
        "change": {k: change.get(k) for k in ("id", "rule", "severity", "direction", "location", "message", "before",
                                              "after", "component")},
        "contract_tests": test_summary,
        "evidence": [{"chunk_id": e["id"], "citation": e["citation"], "text": e["text"]} for e in evidence],
        "reply_format": SCHEMA_HINT,
    }, indent=1, default=str)


IMPACTS = ("consumer_impact", "no_consumer_impact", "unknown")


def validate_assessment(data: Any) -> tuple[dict, list[str]]:
    """Check a model reply against the assessment schema. Returns (clean, problems). Malformed findings are dropped,
    never repaired; an invalid 'impact' raises (the caller falls back to rules)."""
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    if data.get("impact") not in IMPACTS:
        raise ValueError(f"invalid impact {data.get('impact')!r}")
    problems: list[str] = []
    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        raise ValueError("findings must be a list")
    findings = []
    for i, f in enumerate(raw_findings):
        if not isinstance(f, dict):
            problems.append(f"finding {i} is not an object")
            continue
        if not isinstance(f.get("chunk_id"), str) or not f["chunk_id"]:
            problems.append(f"finding {i} has no chunk_id")
            continue
        if f.get("finding") not in ALLOWED_FINDINGS:
            problems.append(f"finding {i} has unknown type {f.get('finding')!r}")
            continue
        if not isinstance(f.get("quote"), str) or not f["quote"].strip():
            problems.append(f"finding {i} has no quote")
            continue
        findings.append({"chunk_id": f["chunk_id"], "quote": f["quote"], "finding": f["finding"],
                         "explanation": str(f.get("explanation") or "")[:500]})
    clean = {"impact": data["impact"], "findings": findings,
             "confidence": data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else None,
             "rationale": str(data.get("rationale") or "")[:1500]}
    hev = data.get("handled_enum_values")
    if isinstance(hev, list) and all(isinstance(x, (str, int, float)) for x in hev):
        clean["handled_enum_values"] = hev
    return clean, problems


def parse_reply(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("LLM reply contained no JSON object")
    clean, problems = validate_assessment(json.loads(m.group(0)))
    clean["_problems"] = problems
    return clean


# ---------------------------------------------------------------- model-driven tool use
TOOL_GUIDE = (
    " You can call tools to gather more evidence before answering: search_evidence (BM25 search over the consumer "
    "files and both specs) and get_chunk (full text of one chunk). Call them only if the provided evidence is not "
    "enough. You may cite only chunk_ids you have seen in the evidence or in tool results. When done, reply with the "
    "single JSON object described in reply_format and nothing else."
)
TOOLS = [
    {"type": "function", "function": {
        "name": "search_evidence",
        "description": "BM25 search over the uploaded consumer code/logs/docs and the two OpenAPI specs. Returns "
                       "chunk ids, citations (file:lines) and text.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Identifiers or words to look for, e.g. 'trackingUrl getOrder'"},
            "source": {"type": "string", "enum": ["consumer", "spec", "any"], "description": "Where to search"},
            "k": {"type": "integer", "minimum": 1, "maximum": 8, "description": "Number of results (default 5)"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_chunk",
        "description": "Full text of one evidence chunk by id (e.g. 'E003').",
        "parameters": {"type": "object", "properties": {"chunk_id": {"type": "string"}}, "required": ["chunk_id"]}}},
]
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 6


def assess_with_tools(client: Any, system: str, user: str, execute: Any) -> tuple[str, list[dict], str | None]:
    """Let the model call the safe evidence tools (bounded), then return its final JSON text.

    `execute(name, arguments_json) -> (result_dict, log_dict)` runs a tool; it never executes model-provided code.
    Returns (final_text, tool_log, note)."""
    messages: list[dict] = [{"role": "system", "content": system + TOOL_GUIDE}, {"role": "user", "content": user}]
    log: list[dict] = []
    note = None
    for rnd in range(1, MAX_TOOL_ROUNDS + 1):
        try:
            resp = client.chat(messages, tools=TOOLS, tool_choice="auto")
        except LLMError as exc:
            if exc.category != "unsupported_tools":
                raise
            return client.complete(system, user), log, f"{exc} Assessed without tools."
        if not resp["tool_calls"]:
            if not resp["content"].strip():
                raise LLMError("bad_response", "The model returned neither tool calls nor an answer.")
            return resp["content"], log, note
        messages.append({"role": "assistant", "content": resp["content"] or "",
                         "tool_calls": [{"id": tc["id"], "type": "function",
                                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                        for tc in resp["tool_calls"]]})
        for tc in resp["tool_calls"]:
            if len(log) >= MAX_TOOL_CALLS:
                result, entry = {"error": "tool call budget exhausted; answer now"}, {
                    "tool": tc["name"], "arguments": tc["arguments"][:300], "error": "budget exhausted",
                    "result_ids": []}
            else:
                result, entry = execute(tc["name"], tc["arguments"])
            log.append({"round": rnd, "call_id": tc["id"], **entry})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                             "content": json.dumps(result)[:12000]})
    # tool budget spent: ask for the answer without further tools
    messages.append({"role": "user", "content": "Tool budget used. Reply now with the final JSON object only."})
    try:
        resp = client.chat(messages, tools=TOOLS, tool_choice="none")
    except LLMError as exc:
        if exc.category != "unsupported_tools":
            raise
        resp = client.chat(messages)
    if resp["tool_calls"] and not resp["content"].strip():
        raise LLMError("bad_response", f"The model kept calling tools after {MAX_TOOL_ROUNDS} rounds.")
    return resp["content"], log, note


# ---------------------------------------------------------------- live self-test (Tests A, B, C)
def selftest(client: Any, index: Any, change: dict, tests: list[dict], evidence: list[dict]) -> dict:
    """Three REAL requests through the configured client. Nothing here is scripted: `live` is False only when a test
    injected a fake transport, so a mocked run can never be reported as real inference."""
    from ..evidence import tokenize
    live = not getattr(client, "mocked", False)
    steps: list[dict] = []

    def norm(t: str) -> str:
        return " ".join(str(t).split())

    a = check(client)
    steps.append({"id": "A", "name": "Basic inference", "ok": a["ok"], "latency_ms": a.get("latency_ms"),
                  "detail": a.get("message"), "category": a.get("category")})
    fatal = not a["ok"] and a.get("fatal")

    if fatal:
        steps.append({"id": "B", "name": "Structured assessment", "ok": False, "skipped": True,
                      "detail": "skipped: basic inference failed with a fatal error"})
    else:
        t = time.perf_counter()
        try:
            raw = client.complete(SYSTEM, build_prompt(change, tests, evidence))
            data = parse_reply(raw)
            verified = sum(1 for f in data["findings"]
                           if index.get(f["chunk_id"]) is not None and norm(f["quote"]) in norm(index.get(f["chunk_id"]).text))
            steps.append({"id": "B", "name": "Structured assessment", "ok": True,
                          "latency_ms": round((time.perf_counter() - t) * 1000),
                          "detail": f"schema valid; impact={data['impact']}; {len(data['findings'])} finding(s), "
                                    f"{verified} with a verbatim quote; {len(data['_problems'])} malformed dropped",
                          "impact": data["impact"], "findings": len(data["findings"]), "verified_quotes": verified,
                          "problems": data["_problems"][:5]})
        except LLMError as exc:
            steps.append({"id": "B", "name": "Structured assessment", "ok": False, **exc.to_dict(),
                          "detail": str(exc)})
            fatal = exc.fatal
        except Exception as exc:  # schema / JSON failure: reported, rejected safely
            steps.append({"id": "B", "name": "Structured assessment", "ok": False, "category": "bad_response",
                          "detail": f"reply rejected: {redact(str(exc))[:200]}"})

    if fatal:
        steps.append({"id": "C", "name": "Tool calling", "ok": False, "skipped": True,
                      "detail": "skipped: an earlier step failed with a fatal error"})
    elif not getattr(client, "supports_tools", False):
        steps.append({"id": "C", "name": "Tool calling", "ok": False, "skipped": True,
                      "detail": f"the {getattr(client, 'provider', '?')} adapter uses JSON-only mode (no tool calls)"})
    else:
        t = time.perf_counter()
        msgs = [{"role": "system", "content": "You are testing tool use. Call the search_evidence tool to find "
                 "consumer code that reads the field trackingUrl. After you receive the tool result, reply with the "
                 'JSON object {"chunk_id": "<an id from the tool result>"} and nothing else.'},
                {"role": "user", "content": "Find the consumer code that reads trackingUrl."}]
        try:
            r1 = client.chat(msgs, tools=[TOOLS[0]], tool_choice="auto")
            calls = [c for c in r1["tool_calls"] if c["name"] == "search_evidence"]
            if not calls:
                steps.append({"id": "C", "name": "Tool calling", "ok": False, "category": "no_tool_call",
                              "detail": "the model answered without calling search_evidence"})
            else:
                call = calls[0]
                try:
                    args = json.loads(call["arguments"] or "{}")
                    query = str(args.get("query") or "")
                except ValueError:
                    args, query = {}, ""
                hits = index.search(tokenize(query or "trackingUrl"), k=5, kinds=("context",))
                ids = [ch.id for ch, _ in hits]
                msgs.append({"role": "assistant", "content": r1["content"] or "",
                             "tool_calls": [{"id": call["id"], "type": "function",
                                             "function": {"name": call["name"], "arguments": call["arguments"]}}]})
                msgs.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": json.dumps(
                    {"results": [{"chunk_id": ch.id, "citation": ch.public()["citation"], "text": ch.text[:800]}
                                 for ch, _ in hits]})})
                try:
                    r2 = client.chat(msgs, tools=[TOOLS[0]], tool_choice="none")
                except LLMError as exc:
                    if exc.category != "unsupported_tools":
                        raise
                    r2 = client.chat(msgs)
                m = re.search(r"\{.*\}", r2["content"] or "", re.S)
                answer = json.loads(m.group(0)).get("chunk_id") if m else None
                ok = answer in ids
                steps.append({"id": "C", "name": "Tool calling", "ok": ok,
                              "latency_ms": round((time.perf_counter() - t) * 1000),
                              "detail": (f"model called search_evidence(query={query!r}); the app returned "
                                         f"{', '.join(ids) or 'no chunks'}; the model's answer cites "
                                         f"{answer!r}" + ("" if ok else " (not one of the tool's results)")),
                              "tool_arguments": args, "tool_result_ids": ids, "answer_chunk_id": answer})
        except LLMError as exc:
            steps.append({"id": "C", "name": "Tool calling", "ok": False, **exc.to_dict(), "detail": str(exc)})
        except Exception as exc:
            steps.append({"id": "C", "name": "Tool calling", "ok": False, "category": "bad_response",
                          "detail": f"tool-call exchange failed: {redact(str(exc))[:200]}"})
    return {"live": live, "provider": getattr(client, "provider", None), "model": getattr(client, "model", None),
            "ok": all(s["ok"] for s in steps),
            "steps": steps}
