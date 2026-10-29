"""Human release review policy.

The system only recommends; a named reviewer records the release decision. Rules:
* Every decision needs a written rationale (it goes into the append-only audit log).
* The recommendation in force is the agent's latest one if an investigation ran, otherwise the deterministic baseline.
* Approving against a BLOCK, or approving under HUMAN_REVIEW, requires explicitly acknowledging each change the
  recommendation rests on. Overriding a BLOCK also needs a longer rationale.
* A reviewer decides against the recommendation they actually saw: if a newer investigation exists, the review is
  refused (409) until they reload.
"""
from __future__ import annotations

import re

REVIEWER_RE = re.compile(r"^[\w .@'\-]{2,80}$")
MIN_COMMENT = 10
MIN_OVERRIDE_COMMENT = 40
MAX_COMMENT = 2000
BLOCK_CODES_BASELINE = {"CONFIRMED_BREAKING"}
BLOCK_CODES_AGENT = {"CONSUMER_BREAK_CONFIRMED", "LIVE_BREAK_CONFIRMED"}


def requirements(analysis: dict, investigation: dict | None) -> dict:
    rec = analysis["recommendation"]
    if investigation:
        effective, source = investigation["decision"]["decision"], "agent"
    else:
        effective, source = rec["decision"], "baseline"
    must: list[str] = []
    if effective == "BLOCK":
        for r in rec["reasons"]:
            if r["code"] in BLOCK_CODES_BASELINE:
                must += r["change_ids"]
        if investigation:
            for adj in investigation["decision"]["adjustments"]:
                if adj["code"] in BLOCK_CODES_AGENT:
                    must += adj["change_ids"]
    elif effective == "HUMAN_REVIEW":
        for r in rec["reasons"]:
            if r["code"] != "NO_BREAKING":
                must += r["change_ids"]
    must = sorted(set(must))
    return {
        "effective_recommendation": effective,
        "source": source,
        "investigation_id": investigation["id"] if investigation else None,
        "must_acknowledge_to_approve": must,
        "min_comment": MIN_COMMENT,
        "min_override_comment": MIN_OVERRIDE_COMMENT,
    }


def check(req: dict, reviewer: str, decision: str, comment: str, acknowledged: list[str]) -> tuple[list[str], bool]:
    """Returns (problems, overrides_recommendation)."""
    problems = []
    if not REVIEWER_RE.match(reviewer or ""):
        problems.append("Reviewer name must be 2–80 characters (letters, digits, space, . @ ' -).")
    if decision not in ("APPROVED", "REJECTED"):
        problems.append("Decision must be APPROVED or REJECTED.")
    comment = (comment or "").strip()
    eff = req["effective_recommendation"]
    overrides = (decision == "APPROVED" and eff == "BLOCK") or (decision == "REJECTED" and eff == "PROCEED")
    needed = MIN_OVERRIDE_COMMENT if (decision == "APPROVED" and eff == "BLOCK") else MIN_COMMENT
    if len(comment) < needed:
        problems.append(f"Rationale must be at least {needed} characters"
                        + (" when approving against a BLOCK recommendation." if needed > MIN_COMMENT else "."))
    if len(comment) > MAX_COMMENT:
        problems.append(f"Rationale must be at most {MAX_COMMENT} characters.")
    if decision == "APPROVED":
        missing = [c for c in req["must_acknowledge_to_approve"] if c not in set(acknowledged)]
        if missing:
            problems.append(f"Approving requires acknowledging these changes: {', '.join(missing)}.")
    return problems, overrides
