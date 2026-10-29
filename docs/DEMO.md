# Review demo script (about 10 minutes)

Everything below runs offline on the bundled samples. The expected observations are from a verified run. Numbers
such as step counts or milliseconds may vary slightly, while decisions, change IDs and outcomes do not.

## 0. Before the review (2 minutes, once)
```bat
cd C:\Users\<you>\Downloads\API_Sentinel\api-sentinel\backend
.venv\Scripts\activate.bat
python demo.py
```
**Expect** `All checks passed.` This proves the whole pipeline works before you open the UI.

Start the app, plus the demo deployment for step 5, in two terminals with the venv active:
```bat
uvicorn app.main:app --port 8000
```
```bat
uvicorn demo_target.petstore_v2:app --port 8100
```
Open http://127.0.0.1:8000. To start from an empty history, stop the server, delete `backend\data\sentinel.db`, and start it again.

## 1. Validation and the comparison page (1 minute)
* Point out the sidebar **Status**: backend online, reasoning mode (LLM model or "Rules only"), audit chain intact.
* Drop any `.txt` file on **Baseline**. **Expect** an instant client-side refusal: "is not a .yaml, .yml or .json file".
* Drop a Swagger 2.0 YAML file. **Expect** a server-side refusal: "Swagger 2.0 is not supported. Convert to OpenAPI 3.0/3.1 first."

## 2. Deterministic detection with confirmed breaks: `invoices-type-change` (2 minutes)
Click **Compare** on the `invoices-type-change` card.
* **Expect** recommendation **Block**: 9 changes, 6 breaking (6 confirmed), 6 of 9 tests failed.
* Changes tab, row **C002 Type changed** (`GET /invoices › … › [].amount`). The detail pane shows:
  * **Confirmed.** a contract test reproduced what an existing client would experience
  * baseline `["number"]` against candidate `["string"]`, side by side
  * *Why it matters*: values arrive in a form clients cannot parse
  * the executed test, its generated input, and the validator error
* Switch **Group by → Category** to show Parameters, Request body, Response body and Endpoints (C006, the removed PDF endpoint).
* Message: **tool use**. Diff engine, test generator and runner are deterministic tools whose outputs drive the decision.

## 3. Insufficient evidence, then agentic RAG: `orders-31-review` (2 minutes)
**New comparison** → **Compare** on `orders-31-review`.
* **Expect** **Review**: 5 potentially breaking changes (a new enum value, a nullable field, an operationId rename). Tests
  show risk, but no hard break. This is the "insufficient evidence → human review" case.
* Open **Investigation**, then **Run investigation**, using the bundled consumer files. **Expect**:
  * a live view first (the stages light up, changes go from queued to investigating to settled)
  * then **Investigation result: Block**, changed from Review, citing C003, C004 and C005, evidence E001/E002, tests R001/R002/T003
  * in **Per-change investigation**, expanding **C004** shows the cited lines of `billing_worker.py` (the `if state == "placed" … else: raise` chain) marked *verified*, and a refined test **Failed**
  * C001/C002 (the `POST /orders` response) stay at *No usage found*, because no consumer calls `createOrder`
* Message: **agentic RAG with verification**. Retrieval is BM25 over consumer code and the specs. A finding counts only
  when its lines exist in the chunk *and* the file calls the affected operation.

## 4. Planning, bounded retry and observability: `orders-31-noisy-consumers` (2 minutes)
**Compare** `orders-31-noisy-consumers` → **Investigation** → **Run investigation**.
* **Expect** Block with **Retries · re-plans: 3 · 0**.
* In **Per-change investigation**, row **C005** shows retrieval `focused → broadened`. Expanding it shows
  "Retrieval attempts: focused (8 hits, 0 verified) → broadened (11 hits, 1 verified)". The first query was crowded out by
  six unrelated services, and the retry with operation-level terms found the billing worker.
* **Show all steps** → filter **Planning & control**. Every `reflect` decision is listed: "Retry C001 (attempt 2 of 2) …",
  "C003 settled as consumer_break_confirmed …", and so on. Expand one to show the decision, reason and attempts, with
  the raw event available.
* Message: **planning and bounded retries**. `reflect` decides to retry (at most one per change), move on, re-plan or stop
  (step budget 28). The evaluation measures that disabling retries drops impact recall from 100% to 83%.

## 5. Live replay and SSRF guard: `petstore-breaking` (1 minute, optional)
**Compare** `petstore-breaking` → Investigation → deployment URL `http://127.0.0.1:8100/v1` → Run.
* **Expect** Block, and refined tests of kind *live http* with real requests and replies (expand a row, then **Request sent**).
  Statuses distinguish **Failed** (incompatibility) from **Timed out** and **Infrastructure failure**, which are *not*
  compatibility verdicts.
* Try `http://169.254.169.254/latest` as the URL. **Expect** a refusal: host not in `SENTINEL_LIVE_ALLOWED_HOSTS`.
* After a confirmed break on an operation, `reflect` **re-plans**: other changes on the same operation move forward
  (look for "moved forward" in the trace).

## 6. Human decision and audit (1 minute)
Back on `orders-31-review` → **Release decision**.
* Choose **Approve release**. **Expect** an override warning (approving against Block), a demand to acknowledge
  C003/C004/C005, and a rationale of at least 40 characters. **Record approval** stays disabled until all three are met.
* Instead, choose **Reject release**, enter your name and a reason, and record it. **Expect** the decision banner, and step 3 of the
  workflow strip ticked.
* **Audit trail** tab shows *Analysis created → Investigation completed → Release rejected*, and **Hash chain intact**.
  **Audit log** page → **Verify chain now**.

## 7. Evaluation (1 minute)
**Evaluation** page.
* Release decision: naive gate 15/21, severity without tests 20/21, API Sentinel 21/21.
* Investigation: keyword RAG falsely escalates the decoy (1 false escalation, 43% impact precision). The agent has
  0 false escalations and 100% impact precision and recall. Without retries, impact recall is 83%.
* Read the **Limits** box aloud: small corpus written alongside the engine, rules mode only.

## Demonstrating key features

### Upload and format validation
- The **Baseline** slot is the current API spec; the **Candidate** is the one you plan to ship.
- Accepted: OpenAPI 3.0.x / 3.1.x in JSON or YAML, max 2 MB, single file (no external `$ref`).
- Drop a `.txt` → client-side refusal. Drop Swagger 2.0 YAML → server-side 422 with "Swagger 2.0 is not supported".
- The info callout below the action bar explains processing, privacy, and accepted inputs.

### Real tool calling
- In step 3, after investigation, expand an `assess` step marked **LLM** in the trace.
- The detail pane shows the tool name (`search_evidence` or `get_chunk`), the arguments the model chose, and the
  chunks API Sentinel returned. The model had to call the tool; the app did not pre-fill results.
- Self-test step C (sidebar → **Run self-test**) also demonstrates a single tool-call round trip with live inference.

### BM25 evidence retrieval
- Expand any `retrieve` step in the trace. It shows the BM25 query terms, the number of hits, and the chunk IDs.
- Chunks are slices of consumer-code files; hover a chunk ID to see the file and line range.

### Genuine retry / feedback loop
- `orders-31-noisy-consumers` (step 4): the first focused retrieval gets 8 hits but 0 verified (the billing worker is
  buried among 6 unrelated services). `reflect` triggers a bounded retry with broadened terms. The second retrieval
  finds the billing worker. Expand the **Planning & control** filter to see each `reflect` decision.

### Traces and verification
- **Show all steps** in the trace. Filter by **All**, **Tool calls**, **Reasoning**, or **Planning & control**.
- Each event shows mode (LLM/rules/tool), wall-clock time, and a summary. Expand for the full detail JSON.
- Verification: each finding must quote the cited chunk verbatim, or it is discarded. Check the `verify` step.

### Final decision and audit
- The recommendation banner on the results page. Investigation may change it (e.g. Review → Block).
- **Audit trail** tab → events in order → **Verify chain now** confirms hash integrity.
- **Release decision** → approve or reject, with review policy enforcement.

### Fallback plan (if Ollama is slow or unavailable)
Set `SENTINEL_LLM=off` in `backend\.env` and restart. Every feature works; investigations run in **rules mode**
(no LLM, deterministic only). The banner says "LLM investigation unavailable" and the decision is rules-based.

### Server-side processing and privacy
- Specifications and results are stored locally in SQLite (`backend/data/sentinel.db`).
- Consumer code is processed in memory and never written to the database.
- Ollama runs locally; no hosted LLM API is contacted.
- The privacy notice is visible in the upload page info callout and in `README.md`.

---

## 8. LLM mode with the local model (only if Ollama is running and `qwen3.5:4b` is pulled)
Before the review: `ollama list` shows `qwen3.5:4b`, and `backend\.env` has `SENTINEL_LLM_PROVIDER=ollama`.
* Sidebar → the diagnostics rows: **Ollama reachable: passed**, **Model installed: passed** (with `tools` among the
  capabilities). Click **Test connection** once early: the first request loads the model into VRAM and can take a minute.
* **Run self-test**. **Expect** A, B and C ✓ and "Real requests (not a test transport)". C shows the query the model chose,
  the chunk IDs API Sentinel returned, and the chunk the model cited.
* Run the `orders-31-review` investigation. `assess` steps show **LLM**, the banner counts model assessments and tool calls,
  and the sidebar's **Last investigation** says *completed, LLM-assisted*. The decision is still verified by the rules:
  a model claim without a verbatim quote is discarded.
* To show the fallback: quit Ollama (tray icon → Quit) and run the investigation again. **Expect** "LLM investigation
  unavailable (unavailable)", the same rules-based decision, and the model called only once.
