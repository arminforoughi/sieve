"""Local science library: BM25 retrieval over a filtered Nemotron-Science-v1 subset.

The library file is built by ``scripts/build_library.py`` (see
``data/library/README.md`` for provenance; CC-BY-4.0, NVIDIA). It holds
synthetic LLM-written problems with reasoning and solutions about adsorption,
porous materials and CO2 capture. Use it as background reasoning to ground
agents, not as an authority: cite records with :func:`cite`.

Dependency-free. The index is built lazily on first :func:`search` and cached.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "library" / "nemotron_science_rqa.jsonl"
LIBRARY_PATH = Path(os.environ.get("SIEVE_LIBRARY", _DEFAULT_PATH))

SOURCE = "Nemotron-Science-v1 RQA"

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    """a an and are as at be been but by can do does for from has have how if in into is it its of on or
    so such than that the their then there these this those to was we were what when which while who will
    with would you your our not no also may i e g let s us given use using find thus therefore""".split()
)
# Common spellings normalised so "CO₂" and "co2", "metal-organic" and "metal organic" meet.
_NORMALISE = (("₂", "2"), ("₄", "4"), ("‑", "-"), ("–", "-"), ("—", "-"))

K1 = 1.5
B = 0.75
EXCERPT_CHARS = 600
QUESTION_CHARS = 400


def tokenize(text: str) -> list[str]:
    text = text.lower()
    for a, b in _NORMALISE:
        text = text.replace(a, b)
    return [t for t in _TOKEN.findall(text) if t not in _STOP and len(t) > 1]


@dataclass(frozen=True)
class _Index:
    docs: list[dict]
    tfs: list[Counter]
    lengths: list[int]
    idf: dict[str, float]
    avgdl: float


@lru_cache(maxsize=4)
def _load(path: str) -> _Index:
    docs: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    tfs, lengths, df = [], [], Counter()
    for d in docs:
        toks = tokenize(d.get("question", "") + "\n" + d.get("reasoning", ""))
        tf = Counter(toks)
        tfs.append(tf)
        lengths.append(len(toks))
        df.update(tf.keys())
    n = len(docs)
    idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
    avgdl = (sum(lengths) / n) if n else 0.0
    return _Index(docs, tfs, lengths, idf, avgdl)


def records(path: Path | str | None = None) -> list[dict]:
    """All library records (cached)."""
    return _load(str(path or LIBRARY_PATH)).docs


def _shorten(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _excerpt(text: str, qterms: set[str], width: int = EXCERPT_CHARS) -> str:
    """The ~width-char window of ``text`` containing the most distinct query terms."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    spans = [(m.start(), tokenize(m.group(0))[0]) for m in re.finditer(r"[A-Za-z0-9₂]+", text)
             if tokenize(m.group(0)) and tokenize(m.group(0))[0] in qterms]
    if not spans:
        return text[: width - 1].rstrip() + "…"
    best_start, best_hits, j = spans[0][0], -1, 0
    for i, (start, _) in enumerate(spans):
        while j < len(spans) and spans[j][0] < start + width:
            j += 1
        hits = len({t for _, t in spans[i:j]}) * 1000 + (j - i)
        if hits > best_hits:
            best_hits, best_start = hits, start
    lo = max(0, best_start - width // 6)
    lo = text.rfind(" ", 0, lo) + 1 if lo else 0
    hi = min(len(text), lo + width)
    return ("…" if lo else "") + text[lo:hi].strip() + ("…" if hi < len(text) else "")


def search(query: str, k: int = 3, path: Path | str | None = None) -> list[dict]:
    """Top-``k`` BM25 matches for ``query`` over question + reasoning.

    Each result: ``id``, ``score``, ``question`` (shortened), ``excerpt`` (~600
    chars of reasoning around the densest query-term span), ``answer`` (boxed
    final answer if present, else shortened solution), ``topics``, ``citation``.
    Deterministic: ties broken by record id.
    """
    idx = _load(str(path or LIBRARY_PATH))
    q = tokenize(query)
    if not q or not idx.docs:
        return []
    qtf = Counter(q)
    scored: list[tuple[float, int]] = []
    for i, tf in enumerate(idx.tfs):
        s = 0.0
        dl = idx.lengths[i]
        for term, qn in qtf.items():
            f = tf.get(term)
            if f:
                s += qn * idx.idf[term] * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / idx.avgdl))
        if s > 0:
            scored.append((s, i))
    scored.sort(key=lambda x: (-x[0], idx.docs[x[1]]["id"]))
    qterms = set(qtf)
    out = []
    for s, i in scored[:k]:
        d = idx.docs[i]
        r = {
            "id": d["id"],
            "score": round(s, 3),
            "question": _shorten(d.get("question", ""), QUESTION_CHARS),
            "excerpt": _excerpt(d.get("reasoning", ""), qterms),
            "answer": _shorten(d.get("final") or d.get("answer", ""), 400),
            "topics": list(d.get("topics", [])),
        }
        r["citation"] = cite(r)
        out.append(r)
    return out


def cite(r: dict) -> str:
    """Short citation string for a search result or record."""
    return f"{SOURCE} #{r['id']}"
