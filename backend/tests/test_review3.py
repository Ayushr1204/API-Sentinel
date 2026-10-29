"""Review 3: persistence, human review policy, audit trail, security hardening."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.spec_loader import load_spec
from app.store import Store


@pytest.fixture
def client():
    return TestClient(main.app)


def _sample(client, name):
    r = client.post(f"/api/analyses/sample/{name}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- persistence
def test_analysis_survives_restart(tmp_path, client):
    db = tmp_path / "s.db"
    main.set_store(Store(db))
    a = _sample(client, "petstore-breaking")
    main.get_store().close()
    main.set_store(Store(db))  # simulate a process restart
    again = client.get(f"/api/analyses/{a['id']}").json()
    assert again["recommendation"] == a["recommendation"]
    assert [x["id"] for x in client.get("/api/analyses").json()] == [a["id"]]
    # investigation still possible after restart (specs were persisted)
    inv = client.post(f"/api/analyses/{a['id']}/investigate", data={"use_sample_context": "true"})
    assert inv.status_code == 200
    assert client.get(f"/api/analyses/{a['id']}").json()["investigation"]["id"] == inv.json()["id"]


# ---------------------------------------------------------------- review policy
def test_approving_block_requires_ack_and_long_rationale(client):
    a = _sample(client, "petstore-breaking")
    pol = a["review_policy"]
    assert pol["effective_recommendation"] == "BLOCK" and pol["must_acknowledge_to_approve"]
    base = {"reviewer": "Asha Rao", "decision": "APPROVED", "seen_investigation_id": None}
    r = client.post(f"/api/analyses/{a['id']}/reviews", json={**base, "comment": "ship it please"})
    assert r.status_code == 422
    problems = " ".join(r.json()["detail"]["problems"])
    assert "40 characters" in problems and "acknowledging" in problems
    ok = client.post(f"/api/analyses/{a['id']}/reviews", json={
        **base, "comment": "Coordinated with all three consumer teams; they ship v2 clients first.",
        "acknowledged_change_ids": pol["must_acknowledge_to_approve"]})
    assert ok.status_code == 200, ok.text
    rev = ok.json()["reviews"][0]
    assert rev["decision"] == "APPROVED" and rev["overrides_recommendation"] is True
    assert rev["system_recommendation"] == "BLOCK"


def test_reject_needs_only_rationale_and_is_not_override(client):
    a = _sample(client, "petstore-breaking")
    r = client.post(f"/api/analyses/{a['id']}/reviews", json={
        "reviewer": "Asha Rao", "decision": "REJECTED", "comment": "Breaks the mobile app.",
        "seen_investigation_id": None})
    assert r.status_code == 200
    assert r.json()["reviews"][0]["overrides_recommendation"] is False


def test_review_refused_when_recommendation_changed(client):
    a = _sample(client, "orders-31-review")
    inv = client.post(f"/api/analyses/{a['id']}/investigate", data={"use_sample_context": "true"}).json()
    stale = client.post(f"/api/analyses/{a['id']}/reviews", json={
        "reviewer": "Asha Rao", "decision": "REJECTED", "comment": "Not ready for release.",
        "seen_investigation_id": None})
    assert stale.status_code == 409
    fresh = client.post(f"/api/analyses/{a['id']}/reviews", json={
        "reviewer": "Asha Rao", "decision": "REJECTED", "comment": "Not ready for release.",
        "seen_investigation_id": inv["id"]})
    assert fresh.status_code == 200
    assert fresh.json()["reviews"][0]["agent_recommendation"] == inv["decision"]["decision"]


def test_review_rejects_bad_reviewer_and_unknown_ack_ids_are_dropped(client):
    a = _sample(client, "petstore-safe")
    bad = client.post(f"/api/analyses/{a['id']}/reviews", json={
        "reviewer": "<script>", "decision": "APPROVED", "comment": "Looks fine to me.", "seen_investigation_id": None})
    assert bad.status_code == 422
    ok = client.post(f"/api/analyses/{a['id']}/reviews", json={
        "reviewer": "Asha Rao", "decision": "APPROVED", "comment": "Additive changes only.",
        "acknowledged_change_ids": ["C001", "NOPE"], "seen_investigation_id": None})
    assert ok.status_code == 200
    events = client.get("/api/audit", params={"analysis_id": a["id"]}).json()["events"]
    approved = next(e for e in events if e["action"] == "review.approved")
    assert approved["details"]["acknowledged_change_ids"] == ["C001"]
    assert approved["details"]["overrides_recommendation"] is False


# ---------------------------------------------------------------- audit trail
def test_audit_chain_records_lifecycle_and_detects_tampering(tmp_path, client):
    db = tmp_path / "a.db"
    main.set_store(Store(db))
    a = _sample(client, "orders-31-review")
    inv = client.post(f"/api/analyses/{a['id']}/investigate", data={"use_sample_context": "true"}).json()
    client.post(f"/api/analyses/{a['id']}/reviews", json={
        "reviewer": "Asha Rao", "decision": "REJECTED", "comment": "Billing worker would break.",
        "seen_investigation_id": inv["id"]})
    body = client.get("/api/audit").json()
    assert [e["action"] for e in reversed(body["events"])] == [
        "analysis.created", "investigation.completed", "review.rejected"]
    assert body["chain"]["valid"] is True and body["chain"]["events"] == 3

    raw = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE audit_log SET actor='x' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM audit_log WHERE seq=1")
    # someone with direct file access drops the guard and rewrites history: the hash chain exposes it
    raw.execute("DROP TRIGGER audit_no_update")
    raw.execute("UPDATE audit_log SET details=replace(details, 'REJECTED', 'APPROVED') WHERE seq=3")
    raw.commit()
    raw.close()
    assert client.get("/api/audit/verify").json() == {"valid": False, "events": 3, "broken_at": 3}


def test_invalid_upload_is_audited(client):
    r = client.post("/api/analyses", files={"base_spec": ("a.yaml", b"swagger: '2.0'"),
                                            "candidate_spec": ("b.yaml", b"swagger: '2.0'")})
    assert r.status_code == 422
    ev = client.get("/api/audit").json()["events"][0]
    assert ev["action"] == "analysis.rejected" and ev["details"]["base_errors"] == 1


# ---------------------------------------------------------------- hardening
def test_yaml_alias_bomb_rejected_quickly():
    lines = ["a0: &a0 [x,x,x,x,x,x,x,x,x,x]"] + [
        f"a{i}: &a{i} [" + ",".join([f"*a{i-1}"] * 10) + "]" for i in range(1, 8)]
    doc = "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths: {}\nx-bomb:\n" + "\n".join("  " + x for x in lines)
    r = load_spec(doc, "bomb.yaml")
    assert not r.valid and "nodes" in r.errors[0].message


def test_many_aliases_rejected():
    anchors = "\n".join(f"  k{i}: *a" for i in range(150))
    r = load_spec(f"openapi: 3.0.3\ninfo: {{title: t, version: '1'}}\npaths: {{}}\nx-a: &a 1\nx-b:\n{anchors}", "x.yaml")
    assert not r.valid and "aliases" in r.errors[0].message


def test_deep_nesting_rejected_not_crashing():
    deep = '{"openapi":"3.0.3","info":{"title":"t","version":"1"},"paths":{},"x":' + "[" * 50000 + "]" * 50000 + "}"
    r = load_spec(deep, "deep.json")
    assert not r.valid and "nest" in r.errors[0].message


def test_security_headers(client):
    api = client.get("/api/health")
    assert api.headers["x-content-type-options"] == "nosniff"
    assert api.headers["x-frame-options"] == "DENY"
    assert api.headers["cache-control"] == "no-store"


def test_upload_filename_paths_are_stripped(client, tmp_path):
    from pathlib import Path
    sample = Path(main.settings.samples_dir) / "petstore-safe"
    r = client.post("/api/analyses", files={
        "base_spec": ("../../etc/base.yaml", (sample / "base.yaml").read_bytes()),
        "candidate_spec": ("..\\..\\cand.yaml", (sample / "candidate.yaml").read_bytes())})
    assert r.status_code == 200
    assert r.json()["base"]["filename"] == "base.yaml"
    assert r.json()["candidate"]["filename"] == "cand.yaml"
