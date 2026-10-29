"""API Sentinel — FastAPI application."""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from . import review as review_policy
from .agent import llm as llm_mod
from .agent.graph import run_investigation
from .agent.tools import allowed_hosts, check_target
from .config import settings
from .evidence import build_index
from .pipeline import run_analysis
from .spec_loader import load_spec
from .store import Store

app = FastAPI(title="API Sentinel", version=__version__,
              description="Breaking-change detection and targeted test generation for OpenAPI 3.0/3.1")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"])

MAX_CONTEXT_FILES = 25
MAX_CONTEXT_TOTAL = 5 * 1024 * 1024
SYSTEM_ACTOR = "api-sentinel"

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    path = request.url.path
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif not path.startswith(("/docs", "/redoc", "/openapi.json")):  # Swagger UI loads its assets from a CDN
        response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


# ---------------------------------------------------------------------- storage
_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_path)
    return _store


def set_store(store: Store) -> None:
    """Used by tests to swap in an isolated database."""
    global _store
    _store = store


# ---------------------------------------------------------------------- helpers
def _safe_name(name: str | None, default: str, limit: int = 200) -> str:
    """Basename only, whatever the client's OS separator: never trust client-supplied paths."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (base or default)[:limit]


async def _read_upload(f: UploadFile) -> bytes:
    return await f.read(settings.max_spec_bytes + 1)  # one extra byte lets load_spec detect overflow


def _samples() -> dict[str, dict]:
    out = {}
    for d in sorted(p for p in settings.samples_dir.iterdir() if p.is_dir()):
        base = next(iter(sorted(d.glob("base.*"))), None)
        cand = next(iter(sorted(d.glob("candidate.*"))), None)
        if base and cand:
            about = (d / "ABOUT.txt").read_text(encoding="utf-8").strip() if (d / "ABOUT.txt").exists() else ""
            context = sorted(p for p in (d / "context").iterdir() if p.is_file()) if (d / "context").is_dir() else []
            out[d.name] = {"name": d.name, "description": about, "base": base, "candidate": cand, "context": context}
    return out


def _analyse(base_raw: bytes | str, base_name: str, cand_raw: bytes | str, cand_name: str,
             sample: str | None = None) -> dict:
    base = load_spec(base_raw, base_name)
    cand = load_spec(cand_raw, cand_name)
    if not (base.valid and cand.valid):
        get_store().audit(SYSTEM_ACTOR, "analysis.rejected", None, {
            "base": base_name[:200], "candidate": cand_name[:200],
            "base_errors": len(base.errors), "candidate_errors": len(cand.errors)})
        raise HTTPException(status_code=422, detail={"message": "One or both specifications are invalid",
                                                     "base": base.summary(), "candidate": cand.summary()})
    analysis = run_analysis(base, cand)
    analysis["sample"] = sample
    get_store().save_analysis(analysis, base.doc, cand.doc, SYSTEM_ACTOR)
    return _full(analysis["id"])


def _full(analysis_id: str) -> dict:
    a = get_store().get_analysis(analysis_id)
    if a is None:
        raise HTTPException(404, "Analysis not found")
    a["review_policy"] = review_policy.requirements(a, a.get("investigation"))
    return a


# ---------------------------------------------------------------------- routes
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/config")
def config() -> dict:
    client = llm_mod.default_client()
    return {"reasoning": client.name if client else None,
            "llm": {"provider": getattr(client, "provider", None), "model": getattr(client, "model", None),
                    "disabled_by_setting": os.environ.get("SENTINEL_LLM", "auto").lower() == "off",
                    "configuration_problem": llm_mod.configuration_problem(),
                    "tool_calling": bool(getattr(client, "supports_tools", False)),
                    "endpoint": getattr(client, "base_url", None)},
            "version": __version__, "live_allowed_hosts": sorted(allowed_hosts()),
            "limits": {"spec_bytes": settings.max_spec_bytes, "context_files": MAX_CONTEXT_FILES,
                       "context_bytes": settings.max_context_bytes}}


@app.post("/api/validate")
async def validate(spec: UploadFile = File(...)) -> dict:
    return load_spec(await _read_upload(spec), _safe_name(spec.filename, "spec")).summary()


@app.post("/api/analyses")
async def create_analysis(base_spec: UploadFile = File(...), candidate_spec: UploadFile = File(...)) -> dict:
    base_raw, cand_raw = await _read_upload(base_spec), await _read_upload(candidate_spec)
    return await run_in_threadpool(_analyse, base_raw, _safe_name(base_spec.filename, "base"),
                                   cand_raw, _safe_name(candidate_spec.filename, "candidate"))


@app.get("/api/samples")
def list_samples() -> list[dict]:
    return [{"name": s["name"], "description": s["description"], "context_files": [p.name for p in s["context"]]}
            for s in _samples().values()]


@app.get("/api/samples/{name}/{which}", response_class=PlainTextResponse)
def sample_file(name: str, which: str) -> str:
    s = _samples().get(name)
    if not s or which not in ("base", "candidate"):
        raise HTTPException(404, "Unknown sample")
    return s[which].read_text(encoding="utf-8")


@app.post("/api/analyses/sample/{name}")
def analyse_sample(name: str) -> dict:
    s = _samples().get(name)
    if not s:
        raise HTTPException(404, "Unknown sample")
    return _analyse(s["base"].read_bytes(), s["base"].name, s["candidate"].read_bytes(), s["candidate"].name,
                    sample=name)


@app.get("/api/analyses")
def list_analyses(limit: int = 50) -> list[dict]:
    return get_store().list_analyses(max(1, min(limit, 200)))


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str) -> dict:
    return _full(analysis_id)


async def _prepare_investigation(analysis_id: str, context: list[UploadFile], target_url: str,
                                 use_sample_context: bool):
    store = get_store()
    analysis = store.get_analysis(analysis_id)
    docs = store.get_docs(analysis_id)
    if analysis is None or docs is None:
        raise HTTPException(404, "Analysis not found")
    base_doc, cand_doc, sample = docs
    files: list[tuple[str, str]] = []
    total = 0
    if len(context) > MAX_CONTEXT_FILES:
        raise HTTPException(413, f"At most {MAX_CONTEXT_FILES} context files")
    for f in context:
        if not f.filename:
            continue
        data = await f.read(settings.max_context_bytes + 1)
        if len(data) > settings.max_context_bytes:
            raise HTTPException(413, f"{f.filename} exceeds {settings.max_context_bytes} bytes")
        total += len(data)
        if total > MAX_CONTEXT_TOTAL:
            raise HTTPException(413, f"Context files exceed {MAX_CONTEXT_TOTAL} bytes in total")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, f"{f.filename} is not UTF-8 text")
        files.append((_safe_name(f.filename, "context.txt", 120), text))
    if not files and use_sample_context and sample:
        files = [(p.name, p.read_text(encoding="utf-8")) for p in _samples().get(sample, {}).get("context", [])]
    target = target_url.strip() or None
    if target:
        err = check_target(target)
        if err:
            store.audit(SYSTEM_ACTOR, "investigation.refused", analysis_id, {"reason": err[:300]})
            raise HTTPException(422, err)
    return analysis, base_doc, cand_doc, files, target


@app.post("/api/analyses/{analysis_id}/investigate")
async def investigate(analysis_id: str, context: list[UploadFile] = File(default=[]),
                      target_url: str = Form(default=""), use_sample_context: bool = Form(default=True)) -> dict:
    """Synchronous investigation (used by scripts and tests). The UI uses the job API below to show progress."""
    analysis, base_doc, cand_doc, files, target = await _prepare_investigation(
        analysis_id, context, target_url, use_sample_context)
    index = build_index(base_doc, cand_doc, files)
    # the graph is synchronous (LLM / live HTTP calls block), so keep it off the event loop
    result = await run_in_threadpool(run_investigation, base_doc, cand_doc, analysis, index,
                                     [n for n, _ in files], target)
    get_store().save_investigation(result, SYSTEM_ACTOR)
    _remember_investigation(result)
    return result


def _remember_investigation(result: dict) -> None:
    info = result.get("llm") or {}
    llm_mod.remember("investigation", {"id": result.get("id"), "analysis_id": result.get("analysis_id"),
                                       "status": "completed", "mode": info.get("mode"), "live": info.get("live"),
                                       "model": info.get("configured"), "assessments_by_llm": info.get("assessments_by_llm"),
                                       "tool_calls": info.get("tool_calls"), "failed_calls": info.get("failed_calls"),
                                       "reasoning": result.get("reasoning"),
                                       "recommendation": (result.get("decision") or {}).get("decision")})


# ---------------------------------------------------------------------- investigation jobs (live progress)
MAX_RUNNING_JOBS = 2
MAX_KEPT_JOBS = 50
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_job(job: dict, base_doc: dict, cand_doc: dict, analysis: dict, files: list, target: str | None) -> None:
    try:
        index = build_index(base_doc, cand_doc, files)
        result = run_investigation(base_doc, cand_doc, analysis, index, [n for n, _ in files], target,
                                   on_event=lambda ev: job["events"].append(ev))
        get_store().save_investigation(result, SYSTEM_ACTOR)
        _remember_investigation(result)
        job.update(status="done", investigation_id=result["id"])
    except Exception as exc:  # report, never hide; message is redacted (it may come from the model runtime)
        job.update(status="failed", error=llm_mod.redact(f"{type(exc).__name__}: {exc}")[:500])
        llm_mod.remember("investigation", {"analysis_id": analysis.get("id"), "status": "failed", "mode": None,
                                           "error": job["error"]})
    finally:
        job["finished_at"] = time.time()


@app.post("/api/analyses/{analysis_id}/investigations", status_code=202)
async def start_investigation(analysis_id: str, context: list[UploadFile] = File(default=[]),
                              target_url: str = Form(default=""),
                              use_sample_context: bool = Form(default=True)) -> dict:
    analysis, base_doc, cand_doc, files, target = await _prepare_investigation(
        analysis_id, context, target_url, use_sample_context)
    with _jobs_lock:
        if sum(j["status"] == "running" for j in _jobs.values()) >= MAX_RUNNING_JOBS:
            raise HTTPException(429, "Two investigations are already running; wait for one to finish.")
        job = {"id": uuid.uuid4().hex[:12], "analysis_id": analysis_id, "status": "running", "events": [],
               "error": None, "investigation_id": None, "started_at": time.time(), "finished_at": None,
               "context_files": [n for n, _ in files], "target_url": target}
        _jobs[job["id"]] = job
        for old in sorted(_jobs.values(), key=lambda j: j["started_at"])[:-MAX_KEPT_JOBS]:
            if old["status"] != "running":
                _jobs.pop(old["id"], None)
    threading.Thread(target=_run_job, args=(job, base_doc, cand_doc, analysis, files, target), daemon=True).start()
    return {"job_id": job["id"], "status": "running"}


@app.get("/api/investigation-jobs/{job_id}")
def investigation_job(job_id: str, since: int = 0) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown investigation job (jobs are kept in memory until the server restarts)")
    events = list(job["events"])
    out = {k: job[k] for k in ("id", "analysis_id", "status", "error", "investigation_id", "context_files",
                               "target_url")}
    out["elapsed_ms"] = round(((job["finished_at"] or time.time()) - job["started_at"]) * 1000)
    out["events"] = events[max(0, since):]
    out["event_count"] = len(events)
    if job["status"] == "done":
        out["investigation"] = get_store().latest_investigation(job["analysis_id"])
    return out


@app.post("/api/llm/check")
def llm_check() -> dict:
    """Makes ONE minimal real request to the configured model (a few tokens) to prove inference works."""
    client = llm_mod.default_client()
    if client is None:
        problem = llm_mod.configuration_problem()
        return {"configured": False, "ok": False, "category": "not_configured" if not problem else "configuration",
                "message": problem or "No LLM is configured. For local inference put SENTINEL_LLM_PROVIDER=ollama in "
                                      "backend/.env and restart the server (see README: local model with Ollama)."}
    result = llm_mod.check(client)
    llm_mod.remember("check", {"ok": result["ok"], "category": result.get("category"), "model": client.name,
                               "live": not getattr(client, "mocked", False), "message": result.get("message"),
                               "latency_ms": result.get("latency_ms")})
    get_store().audit(SYSTEM_ACTOR, "llm.check", None, {"model": client.name, "ok": result["ok"],
                                                        "category": result.get("category")})
    return {"configured": True, "provider": client.provider, **result,
            "model": client.model}


@app.post("/api/llm/selftest")
async def llm_selftest() -> dict:
    """Tests A-C against the configured model with REAL requests (a handful of small calls):
    A basic inference, B structured assessment of a real sample change, C a real tool-call round trip."""
    client = llm_mod.default_client()
    if client is None:
        return {"configured": False, "ok": False, "steps": [],
                "message": llm_mod.configuration_problem() or "No LLM is configured; the agent runs in rules mode."}
    from .agent.tools import query_terms
    s = _samples()["orders-31-review"]
    base, cand = load_spec(s["base"].read_bytes(), "base"), load_spec(s["candidate"].read_bytes(), "candidate")
    analysis = run_analysis(base, cand)
    index = build_index(base.doc, cand.doc, [(p.name, p.read_text(encoding="utf-8")) for p in s["context"]])
    change = next(c for c in analysis["changes"] if c["rule"] == "ENUM_VALUE_ADDED" and "orderId" in (c["path"] or ""))
    results = {r["test_id"]: r for r in analysis["results"]}
    tests = [{"id": t["id"], "kind": t["kind"], "predicted": t["predicted"], "status": results[t["id"]]["status"],
              "verdict": results[t["id"]]["verdict"]} for t in analysis["tests"] if change["id"] in t["change_ids"]]
    terms = query_terms(change)
    evidence = [{**ch.public(), "score": sc} for ch, sc in index.search(terms, k=6, kinds=("context",))
                + index.search(terms, k=2, kinds=("spec",))]
    out = await run_in_threadpool(llm_mod.selftest, client, index, change, tests, evidence)
    llm_mod.remember("selftest", {"ok": out["ok"], "live": out["live"], "model": client.name,
                                  "steps": [{k: st.get(k) for k in ("id", "name", "ok", "skipped", "category", "detail")}
                                            for st in out["steps"]]})
    get_store().audit(SYSTEM_ACTOR, "llm.selftest", None, {"model": client.name, "live": out["live"], "ok": out["ok"],
                                                           "steps": {x["id"]: x["ok"] for x in out["steps"]}})
    return {"configured": True, "change_id": change["id"], **out}


@app.get("/api/llm/status")
async def llm_status() -> dict:
    """Diagnostics without inference: is the local runtime reachable, is the model installed, and the most recent
    check / self-test / investigation results (in memory, since the server started). Never returns a secret."""
    client = llm_mod.default_client()
    out: dict = {"configured": client is not None, "provider": getattr(client, "provider", None),
                 "model": getattr(client, "model", None),
                 "disabled_by_setting": os.environ.get("SENTINEL_LLM", "auto").lower() == "off",
                 "configuration_problem": llm_mod.configuration_problem(), "runtime": None,
                 "last": dict(llm_mod.LAST)}
    if client is not None and hasattr(client, "status"):
        out["runtime"] = await run_in_threadpool(client.status)
    return out


@app.get("/api/analyses/{analysis_id}/investigation")
def get_investigation(analysis_id: str) -> dict:
    inv = get_store().latest_investigation(analysis_id)
    if not inv:
        raise HTTPException(404, "No investigation for this analysis")
    return inv


class ReviewIn(BaseModel):
    reviewer: str = Field(max_length=80)
    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    comment: str = Field(max_length=review_policy.MAX_COMMENT)
    acknowledged_change_ids: list[str] = Field(default_factory=list, max_length=500)
    seen_investigation_id: str | None = None


@app.post("/api/analyses/{analysis_id}/reviews")
def create_review(analysis_id: str, body: ReviewIn) -> dict:
    store = get_store()
    a = store.get_analysis(analysis_id)
    if a is None:
        raise HTTPException(404, "Analysis not found")
    inv = a.get("investigation")
    req = review_policy.requirements(a, inv)
    if (inv["id"] if inv else None) != body.seen_investigation_id:
        raise HTTPException(409, {"message": "The recommendation changed since you loaded it (a newer investigation "
                                             "exists). Reload and review again.", "requirements": req})
    known = {c["id"] for c in a["changes"]}
    ack = sorted({c for c in body.acknowledged_change_ids if c in known})
    problems, overrides = review_policy.check(req, body.reviewer.strip(), body.decision, body.comment, ack)
    if problems:
        raise HTTPException(422, {"message": " ".join(problems), "problems": problems, "requirements": req})
    store.add_review(analysis_id, body.reviewer.strip(), body.decision, body.comment.strip(),
                     a["recommendation"]["decision"], inv["decision"]["decision"] if inv else None,
                     inv["id"] if inv else None, overrides, ack)
    return _full(analysis_id)


@app.get("/api/audit")
def audit_log(analysis_id: str | None = None, limit: int = 200) -> dict:
    store = get_store()
    return {"events": store.audit_events(analysis_id, max(1, min(limit, 1000))), "chain": store.verify_chain()}


_eval_cache: dict | None = None


@app.get("/api/evaluation")
async def evaluation(refresh: bool = False) -> dict:
    """Runs the labelled evaluation corpus (rules mode, no network). Cached per process."""
    global _eval_cache
    if _eval_cache is None or refresh:
        from .evaluation import run_all
        _eval_cache = await run_in_threadpool(run_all)
    return _eval_cache


@app.get("/api/audit/verify")
def audit_verify() -> dict:
    return get_store().verify_chain()


# Serve the built frontend (single-process demo). In development Vite serves it instead.
_dist: Path = settings.frontend_dist
if _dist.exists():
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404, "Not found")  # unknown API routes must not fall through to the SPA
        target = (_dist / full_path).resolve()
        if full_path and target.is_file() and _dist.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(_dist / "index.html")
