"""Durable storage: analyses, investigations, human reviews and a hash-chained audit log in one SQLite file.

Why SQLite (stdlib sqlite3): the audit trail and release decisions must survive restarts (F12/F13). This is a
single-user local tool, so a server database would add install/ops cost with no benefit. One file, transactional,
zero configuration.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  base_filename TEXT, candidate_filename TEXT, title TEXT,
  base_version TEXT, candidate_version TEXT,
  decision TEXT NOT NULL,
  sample TEXT,
  payload TEXT NOT NULL,          -- full analysis JSON
  base_doc TEXT NOT NULL,         -- parsed specs, so the analysis can be re-investigated after a restart
  candidate_doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigations (
  id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES analyses(id),
  created_at TEXT NOT NULL,
  decision TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_id TEXT NOT NULL REFERENCES analyses(id),
  created_at TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
  comment TEXT NOT NULL,
  system_recommendation TEXT NOT NULL,
  agent_recommendation TEXT,
  investigation_id TEXT,
  overrides_recommendation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  analysis_id TEXT,
  details TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inv_analysis ON investigations(analysis_id);
CREATE INDEX IF NOT EXISTS idx_rev_analysis ON reviews(analysis_id);
CREATE INDEX IF NOT EXISTS idx_audit_analysis ON audit_log(analysis_id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""
GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(prev: str, ts: str, actor: str, action: str, analysis_id: str | None, details: str) -> str:
    material = json.dumps([prev, ts, actor, action, analysis_id, details], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ audit
    def _audit(self, actor: str, action: str, analysis_id: str | None, details: dict) -> dict:
        """Caller must hold the lock and be inside a transaction."""
        row = self._conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        ts = _now()
        d = json.dumps(details, sort_keys=True, default=str)
        h = _hash(prev, ts, actor, action, analysis_id, d)
        cur = self._conn.execute(
            "INSERT INTO audit_log (ts, actor, action, analysis_id, details, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (ts, actor, action, analysis_id, d, prev, h))
        return {"seq": cur.lastrowid, "ts": ts, "hash": h}

    def audit(self, actor: str, action: str, analysis_id: str | None, details: dict) -> dict:
        with self._lock, self._conn:
            return self._audit(actor, action, analysis_id, details)

    def audit_events(self, analysis_id: str | None = None, limit: int = 500) -> list[dict]:
        q = "SELECT * FROM audit_log" + (" WHERE analysis_id = ?" if analysis_id else "") + " ORDER BY seq DESC LIMIT ?"
        args = (analysis_id, limit) if analysis_id else (limit,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    def verify_chain(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        prev = GENESIS
        for r in rows:
            expected = _hash(prev, r["ts"], r["actor"], r["action"], r["analysis_id"], r["details"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return {"valid": False, "events": len(rows), "broken_at": r["seq"]}
            prev = r["hash"]
        return {"valid": True, "events": len(rows), "head": prev}

    # ------------------------------------------------------------------ analyses
    def save_analysis(self, a: dict, base_doc: dict, cand_doc: dict, actor: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO analyses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (a["id"], a["created_at"], a["base"]["filename"], a["candidate"]["filename"], a["base"]["title"],
                 a["base"]["api_version"], a["candidate"]["api_version"], a["recommendation"]["decision"],
                 a.get("sample"), json.dumps(a, default=str), json.dumps(base_doc), json.dumps(cand_doc)))
            self._audit(actor, "analysis.created", a["id"], {
                "base": a["base"]["filename"], "candidate": a["candidate"]["filename"],
                "recommendation": a["recommendation"]["decision"], "policy": a["recommendation"]["policy"],
                "counts": a["recommendation"]["counts"], "sample": a.get("sample"),
                "base_sha256": hashlib.sha256(json.dumps(base_doc, sort_keys=True).encode()).hexdigest(),
                "candidate_sha256": hashlib.sha256(json.dumps(cand_doc, sort_keys=True).encode()).hexdigest()})

    def get_analysis(self, analysis_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        if not row:
            return None
        a = json.loads(row["payload"])
        a["investigation"] = self.latest_investigation(analysis_id)
        a["reviews"] = self.reviews(analysis_id)
        return a

    def get_docs(self, analysis_id: str) -> tuple[dict, dict, str | None] | None:
        with self._lock:
            row = self._conn.execute("SELECT base_doc, candidate_doc, sample FROM analyses WHERE id = ?",
                                     (analysis_id,)).fetchone()
        return (json.loads(row["base_doc"]), json.loads(row["candidate_doc"]), row["sample"]) if row else None

    def list_analyses(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT a.id, a.created_at, a.base_filename, a.candidate_filename, a.title, a.base_version,
                          a.candidate_version, a.decision,
                          (SELECT decision FROM investigations i WHERE i.analysis_id = a.id
                             ORDER BY created_at DESC LIMIT 1) AS agent_decision,
                          (SELECT decision FROM reviews r WHERE r.analysis_id = a.id
                             ORDER BY id DESC LIMIT 1) AS review_decision
                   FROM analyses a ORDER BY a.created_at DESC, a.rowid DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ investigations
    def save_investigation(self, inv: dict, actor: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO investigations VALUES (?,?,?,?,?,?)",
                               (inv["id"], inv["analysis_id"], inv["created_at"], inv["decision"]["decision"],
                                inv["reasoning"], json.dumps(inv, default=str)))
            self._audit(actor, "investigation.completed", inv["analysis_id"], {
                "investigation_id": inv["id"], "reasoning": inv["reasoning"],
                "baseline": inv["decision"]["baseline_decision"], "agent_recommendation": inv["decision"]["decision"],
                "adjustments": [{k: x[k] for k in ("code", "change_ids", "test_ids", "evidence_ids")}
                                for x in inv["decision"]["adjustments"]],
                "context_files": inv["context_files"], "target_url": inv["target_url"],
                "trace_steps": len(inv["trace"])})

    def latest_investigation(self, analysis_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM investigations WHERE analysis_id = ? "
                                     "ORDER BY created_at DESC, rowid DESC LIMIT 1", (analysis_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    # ------------------------------------------------------------------ reviews
    def add_review(self, analysis_id: str, reviewer: str, decision: str, comment: str, system_rec: str,
                   agent_rec: str | None, investigation_id: str | None, overrides: bool,
                   acknowledged: list[str]) -> dict:
        with self._lock, self._conn:
            ts = _now()
            cur = self._conn.execute(
                "INSERT INTO reviews (analysis_id, created_at, reviewer, decision, comment, system_recommendation, "
                "agent_recommendation, investigation_id, overrides_recommendation) VALUES (?,?,?,?,?,?,?,?,?)",
                (analysis_id, ts, reviewer, decision, comment, system_rec, agent_rec, investigation_id, int(overrides)))
            ev = self._audit(reviewer, f"review.{decision.lower()}", analysis_id, {
                "review_id": cur.lastrowid, "decision": decision, "comment": comment,
                "system_recommendation": system_rec, "agent_recommendation": agent_rec,
                "investigation_id": investigation_id, "overrides_recommendation": overrides,
                "acknowledged_change_ids": acknowledged})
        return {"id": cur.lastrowid, "created_at": ts, "audit_seq": ev["seq"]}

    def reviews(self, analysis_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM reviews WHERE analysis_id = ? ORDER BY id DESC",
                                      (analysis_id,)).fetchall()
        return [{**dict(r), "overrides_recommendation": bool(r["overrides_recommendation"])} for r in rows]

    def close(self) -> None:
        self._conn.close()
