"""Deterministic release recommendation. A human always makes the final call."""
from __future__ import annotations

from collections import Counter

from .diff import BREAKING, NON_BREAKING, POTENTIAL, Change
from .runner import TestResult
from .testgen import TestCase

PROCEED, BLOCK, HUMAN_REVIEW = "PROCEED", "BLOCK", "HUMAN_REVIEW"


def change_status(changes: list[Change], tests: list[TestCase], results: list[TestResult]) -> dict[str, dict]:
    """Per change: which tests cover it and what they showed."""
    by_test = {r.test_id: r for r in results}
    out: dict[str, dict] = {c.id: {"tests": [], "failed": 0, "passed": 0, "errors": 0} for c in changes}
    for t in tests:
        r = by_test.get(t.id)
        for cid in t.change_ids:
            if cid not in out or r is None:
                continue
            out[cid]["tests"].append(t.id)
            out[cid]["failed" if r.status == "failed" else "passed" if r.status == "passed" else "errors"] += 1
    for cid, s in out.items():
        if not s["tests"]:
            s["evidence"] = "untested"
        elif s["errors"]:
            s["evidence"] = "error"
        elif s["failed"]:
            s["evidence"] = "incompatibility_observed"
        else:
            s["evidence"] = "compatibility_observed"
    return out


def recommend(changes: list[Change], tests: list[TestCase], results: list[TestResult]) -> dict:
    status = change_status(changes, tests, results)
    sev = Counter(c.severity for c in changes)
    reasons: list[dict] = []

    confirmed = [c for c in changes if c.severity == BREAKING and status[c.id]["evidence"] == "incompatibility_observed"]
    unconfirmed = [c for c in changes if c.severity == BREAKING and status[c.id]["evidence"] != "incompatibility_observed"]
    potential = [c for c in changes if c.severity == POTENTIAL]
    errored = [c for c in changes if status[c.id]["evidence"] == "error"]
    surprising = [c for c in changes if c.severity == NON_BREAKING
                  and status[c.id]["evidence"] == "incompatibility_observed"]

    if confirmed:
        reasons.append({"code": "CONFIRMED_BREAKING", "change_ids": [c.id for c in confirmed],
                        "text": f"{len(confirmed)} breaking change(s) confirmed by failing compatibility tests."})
    if unconfirmed:
        reasons.append({"code": "UNCONFIRMED_BREAKING", "change_ids": [c.id for c in unconfirmed],
                        "text": f"{len(unconfirmed)} breaking change(s) detected structurally that the contract "
                                "tests could not confirm (e.g. removal of optional response fields); impact depends "
                                "on how consumers use them."})
    if surprising:
        reasons.append({"code": "UNEXPECTED_INCOMPATIBILITY", "change_ids": [c.id for c in surprising],
                        "text": f"{len(surprising)} change(s) classified non-breaking failed a compatibility test."})
    if potential:
        reasons.append({"code": "POTENTIALLY_BREAKING", "change_ids": [c.id for c in potential],
                        "text": f"{len(potential)} potentially breaking change(s) need a judgement call."})
    if errored:
        reasons.append({"code": "TEST_ERRORS", "change_ids": [c.id for c in errored],
                        "text": f"Tests for {len(errored)} change(s) could not run."})

    if confirmed:
        decision = BLOCK
    elif unconfirmed or surprising or potential or errored:
        decision = HUMAN_REVIEW
    else:
        decision = PROCEED
        reasons.append({"code": "NO_BREAKING", "change_ids": [],
                        "text": "No breaking or potentially breaking changes; all compatibility tests passed."
                        if results else "No breaking or potentially breaking changes detected."})

    return {
        "decision": decision,
        "reasons": reasons,
        "counts": {"changes": len(changes), "breaking": sev[BREAKING], "potentially_breaking": sev[POTENTIAL],
                   "non_breaking": sev[NON_BREAKING], "tests": len(tests),
                   "tests_passed": sum(r.status == "passed" for r in results),
                   "tests_failed": sum(r.status == "failed" for r in results),
                   "tests_error": sum(r.status == "error" for r in results)},
        "change_status": status,
        "policy": "deterministic-v1",
        "final_approval": "required: a human must approve or reject this release",
    }
