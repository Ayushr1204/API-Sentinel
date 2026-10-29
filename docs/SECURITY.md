# Security notes

API Sentinel is a **single-user tool that runs locally**. It parses untrusted files (the specs and consumer context
someone hands you) and, if asked, sends HTTP requests to a deployment. Those two things are the attack surface.
Each control below was tested. The test that covers it is named in brackets.

## Untrusted input

| Threat | Control |
|--------|---------|
| Code execution while parsing YAML | `yaml.safe_load` only. Uploaded files are never executed or imported. |
| YAML "billion laughs" alias expansion (a file under 1 KB made the analysis spend 19 s of CPU before this fix) | At most 100 aliases, and the *expanded* tree is capped at 300,000 nodes, checked iteratively before any further processing [`test_yaml_alias_bomb_rejected_quickly`, `test_many_aliases_rejected`] |
| Deeply nested JSON/YAML causing `RecursionError` (previously an unhandled 500 error) | Nesting is capped at 64 levels, and recursion errors become validation errors [`test_deep_nesting_rejected_not_crashing`] |
| Oversized uploads | Spec ≤ 2 MB; context files ≤ 1 MB each, ≤ 25 files, ≤ 5 MB in total. The upload is read with a one-byte overflow probe, never fully buffered beyond the limit. |
| `$ref` to remote URLs or files (SSRF, local file read) | External `$ref`s are rejected before validation and never fetched. |
| Path traversal through upload filenames | Only the basename is kept, whatever the client OS separator (`/` or `\`) [`test_upload_filename_paths_are_stripped`] |
| Path traversal through the static file route | Resolved paths must stay inside `frontend/dist`, and unknown `/api/*` routes return 404 rather than the SPA. |
| XSS from spec or consumer content shown in the UI | React escapes all text, and nothing uses `dangerouslySetInnerHTML`. The CSP (`script-src 'self'`) blocks inline and third-party scripts. |

## Live HTTP tests (outbound requests)

* Only hosts in `SENTINEL_LIVE_ALLOWED_HOSTS` can be targeted. The default is `127.0.0.1,localhost`. The cloud metadata IP and every other host are refused, and the refusal is audited.
* Redirects are not followed, so an allowed host cannot bounce a request somewhere else. Each request has a 5 s timeout.
* No credentials are ever sent, even when the spec declares security schemes.

## LLM path (optional)

* The only model runtime is a local Ollama server. No API key, token or cloud account is used, and no request leaves
  the machine for inference (unless `OLLAMA_BASE_URL` is pointed elsewhere). Settings live in `backend/.env`
  (git-ignored), which takes priority over machine-wide environment variables.
* Model errors are classified (`unavailable`, `model_missing`, `timeout`, `resources`, `model_error`, `bad_response`).
  A redactor masks anything shaped like `Bearer …` or `token=…` in messages before they are shown or stored
  [`test_error_messages_are_redacted`].
* After a fatal model error, the model is not called again in that run (circuit breaker), so a stopped Ollama or a
  missing model costs one failed request, not one per change [`test_fatal_llm_error_stops_further_calls`].
* **Model tool calls are sandboxed by construction:** the model can only call `search_evidence` and `get_chunk`, which
  are read-only lookups in the in-memory evidence index of the current run. Arguments are parsed as JSON and validated.
  Unknown tools and bad arguments are rejected and logged. Calls are capped (3 rounds, 6 calls). No model output is ever
  executed as code or as a command [`test_unknown_tools_and_invalid_arguments_are_refused`].
* Model output is untrusted. A finding counts only if its quote appears verbatim in the cited chunk. Malformed
  replies fall back to rules, and the trace says so. The model cannot relax a test-confirmed BLOCK
  [`test_llm_cannot_overturn_a_verified_block`].
* Consumer code sent as context goes only to the local
  Ollama server (`OLLAMA_BASE_URL`, default `http://localhost:11434`); no key or token is used. Point `OLLAMA_BASE_URL`
  only at a machine you control. Set `SENTINEL_LLM=off` for code that must not reach any model.
* The app never downloads or starts a model: `/api/llm/status` reads only `/api/version`, `/api/tags` and `/api/show`
  [`test_status_never_downloads_or_infers`].

## Integrity of decisions

* Release decisions are made only by a named reviewer. Approving against BLOCK requires acknowledging each change behind it and giving a rationale of at least 40 characters. A decision made against a stale recommendation is refused with 409 [`test_review3.py`].
* The audit log is **append-only**: SQLite triggers abort any UPDATE or DELETE. It is also **hash-chained**: each event stores SHA-256(previous hash, timestamp, actor, action, analysis, details). `GET /api/audit/verify` recomputes the whole chain. An edit made by someone who bypasses the triggers with direct file access is detected, along with the event where the break occurs [`test_audit_chain_records_lifecycle_and_detects_tampering`]
* The audit log stores SHA-256 hashes of both specs for each analysis, so a later dispute can prove which documents were compared.

## HTTP hardening

`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` with `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`,
a strict CSP on the UI (`default-src 'self'`; no inline scripts; no `data:` fonts, because Vite is configured not to inline assets),
and `Cache-Control: no-store` on API responses. CORS allows only the Vite dev origin. Only GET and POST are allowed.

## Known limits (deliberate, given the scope)

* **No authentication.** Reviewer identity is *self-asserted*: the audit log records who people say they are, not who they proved they are. Run the tool bound to `127.0.0.1` (uvicorn's default). Before sharing it on a network, put it behind an authenticating reverse proxy and take the reviewer name from that identity.
* **No rate limiting.** Analysis is CPU-bound and bounded by the input limits above. A shared deployment would need a proxy-level limit.
* **The hash chain is not signed.** Someone with write access to the database file could rewrite *all* events and recompute every hash. To prevent that, periodically export the chain head (`/api/audit/verify` → `head`) to a separate system.
* **Evidence files are kept in memory only for the duration of an investigation.** Analyses (including both parsed specs) persist in `backend/data/sentinel.db`. Delete that file to purge everything.
