"""Privacy guarantees and upload security — server-side enforcement tests.

These tests verify the claims made in the UI privacy notice and the
documented upload limits, using the real FastAPI TestClient (not a browser).
"""
import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.agent import llm as llm_mod
from app.store import Store


@pytest.fixture
def client():
    return TestClient(main.app)


def _sample(client, name):
    r = client.post(f"/api/analyses/sample/{name}")
    assert r.status_code == 200, r.text
    return r.json()


# ─────────────────────────────── upload size limits ───────────────────────────

def test_oversized_spec_rejected(client):
    """A file larger than 2 MB must be rejected server-side (not just by the browser)."""
    big = b"openapi: '3.0.3'\n" + b"x" * (2 * 1024 * 1024 + 100)
    r = client.post("/api/analyses", files={
        "base_spec": ("big.yaml", big),
        "candidate_spec": ("big.yaml", big)})
    assert r.status_code == 422
    # The error comes from load_spec detecting the overflow
    assert "size" in r.text.lower() or "large" in r.text.lower() or "limit" in r.text.lower() or "byte" in r.text.lower()


def test_oversized_context_file_rejected(client):
    """A single context file larger than 1 MB must be rejected."""
    a = _sample(client, "petstore-safe")
    big_ctx = b"# huge\n" + b"x" * (1024 * 1024 + 100)
    r = client.post(f"/api/analyses/{a['id']}/investigate", files=[
        ("context", ("huge.py", big_ctx))], data={"target_url": ""})
    assert r.status_code == 413


def test_too_many_context_files_rejected(client):
    """More than MAX_CONTEXT_FILES (25) context files must be rejected."""
    a = _sample(client, "petstore-safe")
    files = [("context", (f"f{i}.py", b"print('hi')")) for i in range(26)]
    r = client.post(f"/api/analyses/{a['id']}/investigate", files=files,
                    data={"target_url": ""})
    assert r.status_code == 413


# ──────────────────────────── format validation ──────────────────────────────

def test_swagger_2_rejected_server_side(client):
    """Swagger 2.0 is explicitly rejected with a useful error."""
    swagger2 = b"swagger: '2.0'\ninfo:\n  title: Old\n  version: '1'\npaths: {}"
    r = client.post("/api/analyses", files={
        "base_spec": ("old.yaml", swagger2),
        "candidate_spec": ("old.yaml", swagger2)})
    assert r.status_code == 422
    body = r.json()
    # The error detail should mention Swagger 2.0
    text = json.dumps(body).lower()
    assert "swagger" in text or "2.0" in text or "openapi" in text


def test_plain_text_rejected(client):
    """A plain text file (not YAML/JSON with OpenAPI structure) is rejected."""
    txt = b"This is just some plain text, not an API spec at all."
    r = client.post("/api/analyses", files={
        "base_spec": ("readme.yaml", txt),
        "candidate_spec": ("readme.yaml", txt)})
    assert r.status_code == 422


def test_binary_file_rejected(client):
    """A binary file is rejected (not valid UTF-8 or not valid OpenAPI)."""
    binary = bytes(range(256)) * 10
    r = client.post("/api/analyses", files={
        "base_spec": ("data.json", binary),
        "candidate_spec": ("data.json", binary)})
    assert r.status_code == 422


# ──────────────────────── privacy: spec persistence ──────────────────────────

def test_specs_persisted_in_database(tmp_path, client):
    """Verify the privacy claim: specs ARE stored (so analyses can be reopened)."""
    db = tmp_path / "priv.db"
    main.set_store(Store(db))
    a = _sample(client, "petstore-safe")
    # Directly inspect SQLite to confirm base_doc and candidate_doc are stored
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT base_doc, candidate_doc FROM analyses WHERE id = ?",
                       (a["id"],)).fetchone()
    conn.close()
    assert row is not None
    base_doc = json.loads(row[0])
    cand_doc = json.loads(row[1])
    assert "openapi" in base_doc
    assert "openapi" in cand_doc


# ──────────── privacy: consumer context NOT persisted ────────────────────────

def test_consumer_context_not_persisted(tmp_path, client):
    """Verify the privacy claim: consumer-code context is NOT saved to the database.

    The investigation table stores filenames (in the payload) but not file contents.
    """
    db = tmp_path / "ctx.db"
    main.set_store(Store(db))
    a = _sample(client, "orders-31-review")
    # Run investigation with sample context (has billing_worker.py etc.)
    inv = client.post(f"/api/analyses/{a['id']}/investigate",
                      data={"use_sample_context": "true"})
    assert inv.status_code == 200

    # Inspect every table in the database for consumer file content
    conn = sqlite3.connect(db)
    # The string "def process_order" appears in billing_worker.py context file
    # It must NOT appear anywhere in the database
    all_tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for (table,) in all_tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        for row in rows:
            for cell in row:
                if isinstance(cell, str) and "def process_order" in cell:
                    conn.close()
                    pytest.fail(
                        f"Consumer code content found in table '{table}': "
                        f"the privacy notice says it should not be persisted")
    conn.close()


# ──────────────────────── audit: no raw file content ─────────────────────────

def test_audit_events_do_not_contain_spec_content(tmp_path, client):
    """Audit events log metadata (filenames, hashes) but never raw spec content."""
    db = tmp_path / "audit.db"
    main.set_store(Store(db))
    a = _sample(client, "petstore-breaking")
    events = client.get("/api/audit", params={"analysis_id": a["id"]}).json()["events"]
    for ev in events:
        details_str = json.dumps(ev["details"])
        # The spec contains "openapi" and paths — that level of detail should not be in audit
        assert "paths" not in details_str or "counts" in details_str, (
            "Audit event appears to contain raw spec content")
        # But metadata IS expected
    # At least one event should exist
    assert len(events) >= 1
    # The creation event should have a SHA-256 hash of the spec (metadata, not content)
    created = [e for e in events if e["action"] == "analysis.created"]
    assert len(created) == 1
    assert "base_sha256" in created[0]["details"]


# ────────────────────── LLM provider: only Ollama ────────────────────────────

def test_only_ollama_provider_accepted(monkeypatch):
    """Hosted providers (openai, anthropic, etc.) must be refused, not used."""
    # The autouse no_real_llm fixture sets SENTINEL_LLM=off, which makes
    # configuration_problem() return None early.  Unset it so we can test
    # the provider-name check.
    monkeypatch.setenv("SENTINEL_LLM", "auto")
    for bad in ("openai", "anthropic", "gemini", "huggingface", "azure"):
        monkeypatch.setenv("SENTINEL_LLM_PROVIDER", bad)
        problem = llm_mod.configuration_problem()
        assert problem is not None, f"Provider '{bad}' was not flagged as a problem"
        assert "not supported" in problem.lower() or "ollama" in problem.lower()
    # Ollama must be accepted
    monkeypatch.setenv("SENTINEL_LLM_PROVIDER", "ollama")
    assert llm_mod.configuration_problem() is None
    # 'local' is an alias for ollama
    monkeypatch.setenv("SENTINEL_LLM_PROVIDER", "local")
    assert llm_mod.configuration_problem() is None


def test_default_client_returns_none_when_llm_off(monkeypatch):
    """SENTINEL_LLM=off must disable the LLM entirely (autouse fixture already sets this)."""
    assert llm_mod.default_client() is None


# ────────────────────── server-side validation independence ───────────────────

def test_server_validation_independent_of_browser(client):
    """Server-side validation runs regardless of client-side checks.

    The TestClient bypasses any browser JS, proving server enforcement.
    """
    # File with wrong extension content but forced through
    invalid = b"not: an: openapi: document: at: all"
    r = client.post("/api/analyses", files={
        "base_spec": ("test.yaml", invalid),
        "candidate_spec": ("test.yaml", invalid)})
    assert r.status_code == 422


def test_validate_endpoint_rejects_invalid(client):
    """The /api/validate endpoint performs server-side checks on a single file."""
    r = client.post("/api/validate", files={
        "spec": ("bad.yaml", b"swagger: '2.0'\ninfo:\n  title: X\n  version: '1'\npaths: {}")})
    assert r.status_code == 200  # endpoint returns 200 with valid=false
    body = r.json()
    assert body["valid"] is False

