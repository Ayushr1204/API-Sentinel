"""Evidence corpus + BM25 retrieval, in-process.

Why BM25 and not embeddings: per analysis the corpus is tiny (two specs plus a few context files), and the
questions are about exact identifiers (`trackingUrl`, `/pets/{petId}`, `X-Tenant-Id`, enum literals), where
lexical matching is precise and explainable. No model download, no vector store, no extra service.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from .oas import operations

_SPLIT = re.compile(r"[A-Za-z][a-z0-9]+|[A-Z]+(?![a-z])|\d+")
_WORD = re.compile(r"[A-Za-z0-9_\-]+")
STOP = {"the", "a", "an", "of", "to", "and", "or", "is", "in", "for", "on", "with", "this", "that", "be", "it",
        "if", "as", "at", "by", "from", "return", "const", "let", "var", "def", "self", "import", "function"}
CHUNK_LINES = 14
CHUNK_OVERLAP = 4
MAX_FILES = 25


def tokenize(text: str) -> list[str]:
    """Whole identifiers AND their camelCase/snake_case parts, lower-cased."""
    out: list[str] = []
    for word in _WORD.findall(text):
        lw = word.lower()
        if lw not in STOP and len(lw) > 1:
            out.append(lw)
        parts = [p.lower() for p in _SPLIT.findall(word)]
        if len(parts) > 1:
            out.extend(p for p in parts if p not in STOP and len(p) > 1)
    return out


@dataclass
class Chunk:
    id: str
    source: str          # file name or "base spec" / "candidate spec"
    kind: str            # context | spec
    start_line: int
    end_line: int
    text: str
    tokens: list[str] = field(default_factory=list, repr=False)

    def cite(self) -> str:
        return f"{self.source}:{self.start_line}-{self.end_line}" if self.kind == "context" else self.source

    def public(self) -> dict:
        d = asdict(self)
        d.pop("tokens")
        d["citation"] = self.cite()
        return d


class EvidenceIndex:
    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self._df: Counter = Counter()
        self._avgdl = 0.0

    # ------------------------------------------------------------------ building
    def add_context_file(self, name: str, text: str) -> None:
        lines = text.splitlines() or [""]
        step = CHUNK_LINES - CHUNK_OVERLAP
        for start in range(0, len(lines), step):
            seg = lines[start:start + CHUNK_LINES]
            if not any(s.strip() for s in seg):
                continue
            self._add(name, "context", start + 1, start + len(seg), "\n".join(seg))
            if start + CHUNK_LINES >= len(lines):
                break

    def add_spec(self, label: str, doc: dict) -> None:
        for path, method, op, params in operations(doc):
            summary = {k: op.get(k) for k in ("operationId", "summary", "description", "deprecated") if op.get(k)}
            text = f"{method.upper()} {path}\n" + json.dumps(summary) + "\nparameters: " + \
                ", ".join(f"{p.get('in')}:{p.get('name')}" for p in params)
            self._add(f"{label} spec › {method.upper()} {path}", "spec", 0, 0, text)
        info = doc.get("info") or {}
        if info.get("description"):
            self._add(f"{label} spec › info.description", "spec", 0, 0, str(info["description"]))

    def _add(self, source: str, kind: str, start: int, end: int, text: str) -> None:
        c = Chunk(id=f"E{len(self.chunks) + 1:03d}", source=source, kind=kind, start_line=start, end_line=end,
                  text=text[:4000], tokens=tokenize(text))
        self.chunks.append(c)

    def finalize(self) -> "EvidenceIndex":
        self._df = Counter()
        for c in self.chunks:
            self._df.update(set(c.tokens))
        self._avgdl = (sum(len(c.tokens) for c in self.chunks) / len(self.chunks)) if self.chunks else 0.0
        return self

    # ------------------------------------------------------------------ search
    def search(self, query_terms: list[str], k: int = 5, kinds: tuple[str, ...] = ("context", "spec"),
               k1: float = 1.4, b: float = 0.75) -> list[tuple[Chunk, float]]:
        q = [t for t in tokenize(" ".join(query_terms))]
        if not q or not self.chunks:
            return []
        n = len(self.chunks)
        scored = []
        for c in self.chunks:
            if c.kind not in kinds:
                continue
            tf = Counter(c.tokens)
            dl = len(c.tokens) or 1
            s = 0.0
            for t in set(q):
                if tf[t] == 0:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * dl / (self._avgdl or 1)))
            if s > 0:
                scored.append((c, round(s, 3)))
        scored.sort(key=lambda x: (-x[1], x[0].id))
        return scored[:k]

    def get(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.id == chunk_id), None)


def build_index(base: dict, cand: dict, context_files: list[tuple[str, str]]) -> EvidenceIndex:
    idx = EvidenceIndex()
    for name, text in context_files[:MAX_FILES]:
        idx.add_context_file(name, text)
    idx.add_spec("base", base)
    idx.add_spec("candidate", cand)
    return idx.finalize()
