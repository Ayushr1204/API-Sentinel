"""Deterministic analysis pipeline: diff → generate tests → run → recommend."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from .diff import diff_specs
from .recommend import recommend
from .runner import run_tests
from .spec_loader import LoadedSpec
from .testgen import generate_tests


def run_analysis(base: LoadedSpec, cand: LoadedSpec) -> dict:
    assert base.valid and cand.valid and base.doc is not None and cand.doc is not None
    timings = {}
    t0 = time.perf_counter()
    changes = diff_specs(base.doc, cand.doc)
    timings["diff_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    t1 = time.perf_counter()
    tests = generate_tests(base.doc, cand.doc, changes)
    timings["testgen_ms"] = round((time.perf_counter() - t1) * 1000, 2)
    t2 = time.perf_counter()
    results = run_tests(base.doc, cand.doc, tests)
    timings["run_ms"] = round((time.perf_counter() - t2) * 1000, 2)
    rec = recommend(changes, tests, results)
    return {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base": base.summary(),
        "candidate": cand.summary(),
        "changes": [c.to_dict() for c in changes],
        "tests": [t.to_dict() for t in tests],
        "results": [r.to_dict() for r in results],
        "recommendation": rec,
        "timings": timings,
        "mode": "deterministic",
    }
