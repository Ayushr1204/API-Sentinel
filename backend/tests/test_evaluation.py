"""The labelled evaluation corpus doubles as a regression suite: every case must keep passing."""
from app.evaluation import run_agent, run_detection, to_markdown


def test_detection_corpus_all_cases_correct():
    r = run_detection()
    failing = [c for c in r["cases"] if not c["pass"]]
    assert not failing, failing
    assert r["metrics"]["precision"] == 1.0 and r["metrics"]["recall"] == 1.0


def test_agent_cases_and_no_false_escalation(monkeypatch):
    monkeypatch.setenv("SENTINEL_LLM", "off")
    r = run_agent()
    assert r["metrics"]["false_escalations"] == 0
    assert all(c["pass"] for c in r["cases"]), r["cases"]
    assert r["metrics"]["impact_recall"] == 1.0 and r["metrics"]["impact_precision"] == 1.0
    cmp = {c["system"]: c for c in r["comparison"]}
    # the measured reasons the agent exists: verification prevents keyword false escalations; retry recovers recall
    assert cmp["keyword_rag"]["false_escalations"] >= 1
    assert cmp["agent_no_retry"]["impact_recall"] < cmp["agent"]["impact_recall"]
    assert "compared with simpler approaches" in to_markdown({"detection": run_detection(), "agent": r})
