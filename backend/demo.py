"""Reproducible end-to-end demo of API Sentinel (no UI, no network beyond localhost).

    cd backend
    python demo.py

Uses a throw-away database and starts the bundled demo deployment on 127.0.0.1:8100 for live tests.
Every step checks its expected outcome; the script exits non-zero if anything differs.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("SENTINEL_LLM", "off")  # deterministic: rules-mode agent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.store import Store  # noqa: E402

FAILURES: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {what}")
    if not ok:
        FAILURES.append(what)


def start_target(port: int = 8100) -> None:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            print(f"(demo target already running on :{port})")
            return
    from demo_target.petstore_v2 import app as target
    server = uvicorn.Server(uvicorn.Config(target, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("demo target did not start")


def main_demo() -> int:
    db = Path(tempfile.mkdtemp()) / "demo.db"
    main.set_store(Store(db))
    c = TestClient(main.app)
    start_target()

    print("\n1. Deterministic analysis of the three sample pairs")
    ids = {}
    for name, expected in (("petstore-safe", "PROCEED"), ("orders-31-review", "HUMAN_REVIEW"),
                           ("petstore-breaking", "BLOCK"), ("invoices-type-change", "BLOCK"),
                           ("orders-31-noisy-consumers", "HUMAN_REVIEW")):
        a = c.post(f"/api/analyses/sample/{name}").json()
        ids[name] = a["id"]
        n = a["recommendation"]["counts"]
        check(a["recommendation"]["decision"] == expected,
              f"{name}: {n['changes']} changes, {n['tests']} tests ({n['tests_failed']} failed) -> "
              f"{a['recommendation']['decision']} (expected {expected})")

    print("\n2. Validation rejects out-of-scope input")
    r = c.post("/api/analyses", files={"base_spec": ("old.yaml", b"swagger: '2.0'\ninfo: {title: x, version: '1'}"),
                                       "candidate_spec": ("new.yaml", b"asyncapi: 2.6.0")})
    check(r.status_code == 422, f"Swagger 2.0 + AsyncAPI upload refused with {r.status_code}")

    print("\n3. Agent investigation with consumer code (orders-31-review)")
    inv = c.post(f"/api/analyses/{ids['orders-31-review']}/investigate", data={"use_sample_context": "true"}).json()
    d = inv["decision"]
    check(d["baseline_decision"] == "HUMAN_REVIEW" and d["decision"] == "BLOCK",
          f"agent: {d['baseline_decision']} -> {d['decision']} in {len(inv['trace'])} traced steps "
          f"({inv['reasoning']})")
    for adj in d["adjustments"]:
        print(f"      {adj['code']}: changes {adj['change_ids']}, evidence {adj['evidence_ids']}, tests {adj['test_ids']}")
    nodes = [e["node"] for e in inv["trace"]]
    check(nodes[0] == "baseline" and nodes[-1] == "decide" and "verify" in nodes, "trace runs baseline ... verify ... decide")

    print("\n3b. Bounded retry: consumers hidden among unrelated services (orders-31-noisy-consumers)")
    inv_n = c.post(f"/api/analyses/{ids['orders-31-noisy-consumers']}/investigate",
                   data={"use_sample_context": "true"}).json()
    retries = [e for e in inv_n["trace"] if e["node"] == "reflect" and e["detail"]["decision"] == "retry"]
    c005 = next(r for r in inv_n["records"] if r["change_id"] == "C005")
    check(len(retries) >= 1 and [x["strategy"] for x in c005["attempts"]] == ["focused", "broadened"]
          and c005["outcome"] == "consumer_break_confirmed",
          f"{len(retries)} retries; C005 (trackingUrl may be null) found only by the broadened retry -> {c005['outcome']}")
    check(inv_n["decision"]["decision"] == "BLOCK", f"agent: {inv_n['decision']['baseline_decision']} -> "
          f"{inv_n['decision']['decision']}")

    print("\n4. Live HTTP replay against the demo deployment (petstore-breaking)")
    inv2 = c.post(f"/api/analyses/{ids['petstore-breaking']}/investigate",
                  data={"use_sample_context": "true", "target_url": "http://127.0.0.1:8100/v1"}).json()
    live = [r for rec in inv2["records"] for t, r in zip(rec["refined_tests"], rec["refined_results"])
            if t["kind"] == "live_http"]
    failed = [r for r in live if r["status"] == "failed"]
    check(len(live) > 0 and len(failed) > 0, f"{len(live)} live requests sent, {len(failed)} showed a break")
    blocked = c.post(f"/api/analyses/{ids['petstore-breaking']}/investigate",
                     data={"target_url": "http://169.254.169.254/latest"})
    check(blocked.status_code == 422, "SSRF guard refuses a non-allowlisted target (cloud metadata IP)")

    print("\n5. Human release decision")
    a = c.get(f"/api/analyses/{ids['orders-31-review']}").json()
    pol = a["review_policy"]
    body = {"reviewer": "Demo Reviewer", "decision": "APPROVED", "comment": "ok", "seen_investigation_id": inv["id"]}
    r = c.post(f"/api/analyses/{a['id']}/reviews", json=body)
    check(r.status_code == 422, "approving a BLOCK without acknowledgements or rationale is refused")
    r = c.post(f"/api/analyses/{a['id']}/reviews", json={**body, "seen_investigation_id": None,
                                                          "comment": "x" * 50, "acknowledged_change_ids":
                                                          pol["must_acknowledge_to_approve"]})
    check(r.status_code == 409, "a decision made against a stale recommendation is refused (409)")
    r = c.post(f"/api/analyses/{a['id']}/reviews", json={
        **body, "decision": "REJECTED", "comment": "Billing worker cannot handle 'refunded' or a null trackingUrl."})
    rev = r.json()["reviews"][0] if r.status_code == 200 else {}
    check(rev.get("decision") == "REJECTED" and rev.get("agent_recommendation") == "BLOCK",
          "release rejected by a named reviewer, linked to the agent recommendation")

    print("\n6. Audit trail")
    audit = c.get("/api/audit").json()
    actions = [e["action"] for e in reversed(audit["events"])]
    check(audit["chain"]["valid"], f"hash chain intact over {audit['chain']['events']} events")
    check(actions.count("analysis.created") == 5 and "analysis.rejected" in actions and
          actions.count("investigation.completed") == 3 and "investigation.refused" in actions and
          actions[-1] == "review.rejected", "every analysis, investigation, refusal and decision was recorded")

    print("\n7. Evaluation corpus")
    ev = c.get("/api/evaluation").json()
    m, am = ev["detection"]["metrics"], ev["agent"]["metrics"]
    check(m["cases_fully_correct"] == m["cases"], f"detection {m['cases_fully_correct']}/{m['cases']} cases, precision "
          f"{m['precision']:.0%}, recall {m['recall']:.0%}")
    check(am["correct"] == am["cases"] and am["false_escalations"] == 0,
          f"agent {am['correct']}/{am['cases']} cases, {am['false_escalations']} false escalations")

    main.get_store().close()
    print(f"\n{'All checks passed.' if not FAILURES else f'{len(FAILURES)} check(s) FAILED.'}  (database: {db})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main_demo())
