from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def test_health():
    assert client.get("/api/health").json()["status"] == "ok"


@pytest.mark.parametrize("name,decision", [("petstore-breaking", "BLOCK"), ("petstore-safe", "PROCEED"),
                                           ("orders-31-review", "HUMAN_REVIEW")])
def test_samples_reach_expected_decision(name, decision):
    r = client.post(f"/api/analyses/sample/{name}")
    assert r.status_code == 200
    body = r.json()
    assert body["recommendation"]["decision"] == decision
    assert client.get(f"/api/analyses/{body['id']}").json()["id"] == body["id"]


def test_upload_flow():
    d = SAMPLES / "petstore-breaking"
    with open(d / "base.yaml", "rb") as b, open(d / "candidate.yaml", "rb") as c:
        r = client.post("/api/analyses", files={"base_spec": ("base.yaml", b), "candidate_spec": ("cand.yaml", c)})
    assert r.status_code == 200 and r.json()["recommendation"]["decision"] == "BLOCK"


def test_invalid_upload_returns_422_with_details():
    r = client.post("/api/analyses", files={"base_spec": ("a.json", b'{"swagger": "2.0"}'),
                                            "candidate_spec": ("b.yaml", b"openapi: 3.0.3\n")})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert not detail["base"]["valid"] and not detail["candidate"]["valid"]


def test_validate_endpoint():
    r = client.post("/api/validate", files={"spec": ("x.yaml", (SAMPLES / "petstore-safe" / "base.yaml").read_bytes())})
    assert r.json()["valid"] is True and r.json()["stats"]["operations"] == 4


def test_unknown_sample_and_analysis():
    assert client.post("/api/analyses/sample/nope").status_code == 404
    assert client.get("/api/analyses/nope").status_code == 404
    assert client.get("/api/not-a-route").status_code == 404
    assert client.get("/api/samples/..%2F..%2Fetc/base").status_code == 404
    assert client.get("/api/samples/petstore-safe/..%2F..%2F..%2Fapp%2Fmain.py").status_code == 404


def test_static_handler_does_not_escape_dist():
    from app.config import settings
    if not settings.frontend_dist.exists():
        return
    r = client.get("/..%2F..%2Fbackend%2Fapp%2Fmain.py")
    assert "FastAPI" not in r.text
