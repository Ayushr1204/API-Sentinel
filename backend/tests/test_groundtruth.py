"""Ground truth: every bundled sample ships expected.json, and the engine is scored against it."""
import json

import pytest
from fastapi.testclient import TestClient

from app import groundtruth
from app.config import settings
from app.evaluation import run_samples

SAMPLES = sorted(p for p in settings.samples_dir.iterdir() if p.is_dir())


def test_every_sample_is_labelled_and_valid():
    assert len(SAMPLES) >= 12
    for d in SAMPLES:
        assert (d / "expected.json").exists(), d.name
        data = json.loads((d / "expected.json").read_text(encoding="utf-8"))
        assert groundtruth.validate(data) == [], d.name


def test_all_samples_match_their_ground_truth():
    r = run_samples()
    failing = [(c["sample"], c["problems"], c["decision"]) for c in r["cases"] if not c["pass"]]
    assert failing == []
    assert r["metrics"]["investigations_checked"] >= 3


def _change(cid, rule, sev, loc, direction="response"):
    return {"id": cid, "rule": rule, "severity": sev, "location": loc, "direction": direction}


def test_score_reports_missed_unexpected_and_severity_mismatch():
    expected = {"decision": "BLOCK", "changes": [
        {"rule": "PROPERTY_REMOVED", "severity": "BREAKING", "operation": "GET /a", "field": "x"},
        {"rule": "ENUM_VALUE_ADDED", "severity": "POTENTIALLY_BREAKING", "operation": "GET /a", "field": "s"},
        {"rule": "OPERATION_REMOVED", "severity": "BREAKING", "operation": "DELETE /a"}]}
    analysis = {"recommendation": {"decision": "HUMAN_REVIEW"}, "changes": [
        _change("C1", "PROPERTY_REMOVED", "BREAKING", "GET /a › response 200 (application/json) › x"),
        _change("C2", "ENUM_VALUE_ADDED", "NON_BREAKING", "GET /a › response 200 (application/json) › s"),
        _change("C3", "PATH_ADDED", "NON_BREAKING", "/b", "surface")]}
    s = groundtruth.score(analysis, expected)
    assert [r["status"] for r in s["changes"]] == ["matched", "severity_mismatch", "missed"]
    assert [u["change_id"] for u in s["unexpected"]] == ["C3"]
    assert s["counts"] == {"expected": 3, "matched": 1, "severity_mismatch": 1, "missed": 1, "unexpected": 1}
    assert s["decision_match"] is False and s["pass"] is False
    assert s["precision"] == pytest.approx(2 / 3, abs=1e-4) and s["recall"] == pytest.approx(2 / 3, abs=1e-4)


def test_direction_disambiguates_labels_on_the_same_field():
    expected = {"decision": "BLOCK", "changes": [
        {"rule": "ENUM_VALUE_ADDED", "severity": "NON_BREAKING", "operation": "GET /a", "field": "s",
         "direction": "request"},
        {"rule": "ENUM_VALUE_ADDED", "severity": "POTENTIALLY_BREAKING", "operation": "GET /a", "field": "s",
         "direction": "response"}]}
    analysis = {"recommendation": {"decision": "BLOCK"}, "changes": [
        _change("R", "ENUM_VALUE_ADDED", "POTENTIALLY_BREAKING", "GET /a › response 200 › s", "response"),
        _change("Q", "ENUM_VALUE_ADDED", "NON_BREAKING", "GET /a › query parameter 's'", "request")]}
    s = groundtruth.score(analysis, expected)
    assert s["pass"] and [r["change_id"] for r in s["changes"]] == ["Q", "R"]


def test_invalid_labels_are_rejected():
    assert groundtruth.validate({"decision": "MAYBE"})
    assert groundtruth.validate({"decision": "BLOCK", "changes": [{"rule": "X", "severity": "BAD"}]})
    assert groundtruth.validate({"decision": "BLOCK", "decision_after_investigation": "NO"})


def test_api_exposes_ground_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_LLM", "off")
    from app.main import app
    c = TestClient(app)
    listing = {s["name"]: s for s in c.get("/api/samples").json()}
    assert listing["bookings-swagger2-to-oas32-release"]["formats"] == {"base": "Swagger 2.0",
                                                                         "candidate": "OpenAPI 3.2.0"}
    assert listing["orders-31-review"]["expected"]["decision_after_investigation"] == "BLOCK"
    a = c.post("/api/analyses/sample/payments-auth-change").json()
    gt = a["ground_truth"]
    assert gt["pass"] and gt["expected_decision"] == gt["actual_decision"] == "BLOCK"
    inv = c.post(f"/api/analyses/{a['id']}/investigate", data={"use_sample_context": "true"}).json()
    assert "ground_truth" not in inv  # this sample has no post-investigation label
    a2 = c.post("/api/analyses/sample/orders-31-review").json()
    inv2 = c.post(f"/api/analyses/{a2['id']}/investigate", data={"use_sample_context": "true"}).json()
    assert inv2["ground_truth"]["expected_decision"] == "BLOCK"
    assert c.get(f"/api/analyses/{a2['id']}/investigation").json()["ground_truth"]["decision_match"] in (True, False)
    assert c.get(f"/api/analyses/{a2['id']}").json()["ground_truth"]["investigation"] is not None


def test_uploaded_analysis_has_no_ground_truth():
    from app.main import app
    c = TestClient(app)
    d = settings.samples_dir / "petstore-safe"
    files = {"base_spec": ("b.yaml", next(d.glob("base.*")).read_bytes()),
             "candidate_spec": ("c.yaml", next(d.glob("candidate.*")).read_bytes())}
    assert c.post("/api/analyses", files=files).json()["ground_truth"] is None
