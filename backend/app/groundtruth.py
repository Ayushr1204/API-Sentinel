"""Ground truth for the bundled samples: what each sample *should* produce, and how close the engine got.

Each sample folder may hold an ``expected.json``:

    {
      "decision": "BLOCK",                         # expected baseline recommendation
      "decision_after_investigation": "BLOCK",     # optional: expected result of a rules-mode investigation
      "rationale": "why a reviewer would decide this",
      "changes": [
        {"rule": "ENUM_VALUE_ADDED", "severity": "POTENTIALLY_BREAKING",
         "operation": "GET /orders/{orderId}", "field": "state", "direction": "response", "why": "..."}
      ]
    }

Labels are written from what each edit means for existing clients (for the newer samples, before the engine was
run on them). Scoring never hides a disagreement: missed, unexpected and wrongly-graded changes are all listed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VALID_DECISIONS = {"PROCEED", "HUMAN_REVIEW", "BLOCK"}
VALID_SEVERITIES = {"BREAKING", "POTENTIALLY_BREAKING", "NON_BREAKING"}


def load_expected(sample_dir: Path) -> dict | None:
    path = sample_dir / "expected.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = validate(data)
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return data


def validate(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["expected.json must be an object"]
    out = []
    if data.get("decision") not in VALID_DECISIONS:
        out.append(f"decision must be one of {sorted(VALID_DECISIONS)}")
    after = data.get("decision_after_investigation")
    if after is not None and after not in VALID_DECISIONS:
        out.append("decision_after_investigation must be a decision or absent")
    for i, ch in enumerate(data.get("changes") or []):
        if not isinstance(ch, dict) or not ch.get("rule"):
            out.append(f"changes[{i}] needs a rule")
        elif ch.get("severity") not in VALID_SEVERITIES:
            out.append(f"changes[{i}].severity must be one of {sorted(VALID_SEVERITIES)}")
    return out


def matches(expected: dict, change: dict) -> bool:
    """Same rule; the change's location starts with the expected operation; the expected field is in the last
    location segment. Mirrors the evaluation corpus's matching (evaluation._matches)."""
    if change.get("rule") != expected.get("rule"):
        return False
    side = expected.get("direction")  # optional: request | response | surface
    if side and change.get("direction") != side:
        return False
    location = change.get("location") or ""
    op = expected.get("operation")
    if op and not location.startswith(op):
        return False
    fld = expected.get("field")
    if fld and fld not in location.rsplit(" › ", 1)[-1]:
        return False
    return True


def score(analysis: dict, expected: dict) -> dict:
    """Compare one analysis with its ground truth."""
    changes = analysis.get("changes") or []
    used: set[str] = set()
    rows = []
    for exp in expected.get("changes") or []:
        candidates = [c for c in changes if c["id"] not in used and matches(exp, c)]
        # prefer the change graded as expected, so two labels on one field cannot swap partners
        hit = next((c for c in candidates if c["severity"] == exp["severity"]), candidates[0] if candidates else None)
        if hit is None:
            rows.append({"expected": exp, "status": "missed", "change_id": None, "actual_severity": None})
            continue
        used.add(hit["id"])
        status = "matched" if hit["severity"] == exp["severity"] else "severity_mismatch"
        rows.append({"expected": exp, "status": status, "change_id": hit["id"], "actual_severity": hit["severity"]})
    unexpected = [{"change_id": c["id"], "rule": c["rule"], "severity": c["severity"], "location": c["location"]}
                  for c in changes if c["id"] not in used]
    tp = sum(r["status"] != "missed" for r in rows)
    fn = sum(r["status"] == "missed" for r in rows)
    fp = len(unexpected)
    actual = (analysis.get("recommendation") or {}).get("decision")
    decision_ok = actual == expected["decision"]
    severity_ok = sum(r["status"] == "matched" for r in rows)
    return {
        "expected_decision": expected["decision"],
        "actual_decision": actual,
        "decision_match": decision_ok,
        "decision_after_investigation": expected.get("decision_after_investigation"),
        "rationale": expected.get("rationale", ""),
        "changes": rows,
        "unexpected": unexpected,
        "counts": {"expected": len(rows), "matched": severity_ok, "severity_mismatch": tp - severity_ok,
                   "missed": fn, "unexpected": fp},
        "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "pass": decision_ok and fn == 0 and fp == 0 and severity_ok == tp,
    }


def score_investigation(investigation: dict, expected: dict | None) -> dict | None:
    """Expected vs actual decision after an investigation (None when the sample has no such label)."""
    if not expected or not expected.get("decision_after_investigation"):
        return None
    actual = (investigation.get("decision") or {}).get("decision")
    exp = expected["decision_after_investigation"]
    return {"expected_decision": exp, "actual_decision": actual, "decision_match": actual == exp,
            "note": "Labelled for a rules-mode investigation with the sample's consumer files; an LLM-assisted run "
                    "may differ and is shown, not hidden."}
