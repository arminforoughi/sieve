"""Build SIEVE's local science library from Nemotron-Science-v1 (RQA split).

Reads the dataset's JSONL from **stdin** (so the 816 MB source file never has
to touch disk), keeps records relevant to CO2 adsorption in porous materials,
and writes a compact JSONL plus a provenance README.

Usage (from the repo root)::

    curl -sL https://huggingface.co/datasets/nvidia/Nemotron-Science-v1/resolve/main/data/RQA.jsonl \
      | uv run --no-sync python -I scripts/build_library.py

Dataset text is treated purely as data: it is parsed with ``json.loads`` and
matched with regular expressions, nothing in it is ever executed.

Source record schema (one JSON object per line)::

    {"uuid": str, "license": "cc-by-4.0", "used_in": [str], "tools": [],
     "messages": [{"role": "user", "content": <prompt + problem>},
                  {"role": "assistant", "content": <final solution, \\boxed{}>,
                   "reasoning_content": <long chain of thought>}]}
"""

from __future__ import annotations

import argparse
import heapq
import datetime as _dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "library"
OUT_FILE = OUT_DIR / "nemotron_science_rqa.jsonl"

DATASET = "nvidia/Nemotron-Science-v1"
SPLIT_FILE = "data/RQA.jsonl"
URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main/{SPLIT_FILE}"

MAX_CHARS = 4000

# topic -> list of regex fragments (matched case-insensitively with \b...\b).
TOPICS: dict[str, list[str]] = {
    "adsorption": [r"adsorption", r"adsorb(?:s|ed|ing|ent|ents|ate)?", r"desorption", r"desorb(?:s|ed|ing)?",
                   r"sorbent", r"uptake"],
    "isotherm": [r"isotherms?", r"langmuir", r"freundlich", r"BET", r"brunauer", r"toth", r"sips"],
    "physisorption": [r"physisorption", r"physisorbed", r"chemisorption", r"chemisorbed"],
    "porous_material": [r"metal[\s\-‑]organic frameworks?", r"MOFs?", r"zeolites?", r"COFs?",
                        r"covalent organic frameworks?", r"activated carbons?", r"porous", r"micropor\w*",
                        r"mesopor\w*", r"nanopor\w*", r"pore (?:size|volume|diameter|width)s?",
                        r"surface areas?", r"cavit(?:y|ies)", r"zif-?\d*", r"hkust-?1", r"uio-?66"],
    "co2_capture": [r"CO2", r"CO₂", r"carbon dioxide", r"carbon capture", r"flue gas", r"CCS", r"DAC",
                    r"direct air capture"],
    "dispersion": [r"van der waals", r"vdw", r"dispersion (?:forces?|interactions?|energy)", r"london (?:forces?|dispersion)",
                   r"lennard[\s\-‑]jones", r"LJ potential", r"\d+-6 potential"],
    "quadrupole": [r"quadrupole(?: moments?)?", r"quadrupolar"],
    "heat_of_adsorption": [r"heats? of adsorption", r"isosteric heats?", r"enthalp(?:y|ies) of adsorption",
                           r"adsorption enthalp(?:y|ies)", r"clausius[\s\-‑]clapeyron", r"Q_?st"],
    "henry": [r"henry'?s? (?:law )?constants?", r"henry'?s law", r"henry regime"],
    "kinetic_diameter": [r"kinetic diameters?", r"molecular sieving", r"molecular sieves?"],
    "open_metal_sites": [r"open metal sites?", r"coordinatively unsaturated", r"unsaturated metal (?:sites?|centers?|centres?)"],
    "electrostatics": [r"framework charges?", r"partial charges?", r"electrostatic(?:s| interactions?)?",
                       r"point charges?", r"ewald"],
    "gas_separation": [r"gas separation", r"selectivity", r"breakthrough", r"pressure swing", r"PSA", r"TSA",
                       r"IAST"],
    "gcmc": [r"grand canonical", r"GCMC", r"monte carlo"],
}

# A single hit on any of these is enough to keep a record.
STRONG = [r"adsorption isotherms?", r"metal[\s\-‑]organic frameworks?", r"isosteric heats?",
          r"heats? of adsorption", r"langmuir (?:isotherm|adsorption|model|equation)", r"BET (?:isotherm|surface|equation|theory)",
          r"freundlich", r"physisorption", r"chemisorption", r"open metal sites?", r"kinetic diameters?",
          r"zeolites?", r"covalent organic frameworks?", r"carbon capture", r"grand canonical monte carlo",
          r"activated carbons?", r"CO2 (?:adsorption|uptake|capture)"]

# Case-sensitive tokens (acronyms that collide with ordinary words when lowercased).
CASE_SENSITIVE = {r"MOFs?", r"COFs?", r"BET", r"CCS", r"DAC", r"PSA", r"TSA", r"IAST", r"GCMC", r"LJ potential", r"Q_?st",
                  r"CO2", r"CO₂"}


def _compile(frag: str) -> re.Pattern[str]:
    flags = 0 if frag in CASE_SENSITIVE else re.IGNORECASE
    return re.compile(rf"(?<![\w]){frag}(?![\w])", flags)


TOPIC_RX = {t: [_compile(f) for f in frags] for t, frags in TOPICS.items()}
STRONG_RX = [_compile(f) for f in STRONG]

PREAMBLE = re.compile(
    r"^\s*Solve the following problem\.\s*Make sure to put the answer \(and only answer\) inside .{0,3}oxed\{\}\.?\s*",
    re.IGNORECASE,
)
BOXED = re.compile(r"\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}")


def clean_question(text: str) -> str:
    q = PREAMBLE.sub("", text or "").strip()
    # Several prompts contain the problem statement twice verbatim; keep one copy.
    half = len(q) // 2
    if len(q) > 200:
        a, b = q[:half].strip(), q[half:].strip()
        if a and a == b:
            q = a
    return q


def truncate(text: str, n: int = MAX_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def match_topics(text: str) -> tuple[list[str], list[str]]:
    topics = [t for t, rxs in TOPIC_RX.items() if any(rx.search(text) for rx in rxs)]
    strong = sorted({m.group(0).lower() for rx in STRONG_RX for m in [rx.search(text)] if m})
    return topics, strong


def keep(topics: list[str], strong: list[str], min_topics: int) -> bool:
    # Adsorption-related context must be present: CO2 + electrostatics alone is just general chemistry.
    core = {"adsorption", "isotherm", "physisorption", "porous_material", "heat_of_adsorption", "henry",
            "kinetic_diameter", "open_metal_sites", "gcmc"}
    if strong:
        return True
    return len(topics) >= min_topics and bool(core & set(topics))


# Relevance weights: SIEVE-specific mechanisms count more than generic adsorption vocabulary.
WEIGHTS = {
    "adsorption": 1.0, "isotherm": 1.0, "physisorption": 1.5, "porous_material": 1.5, "co2_capture": 2.0,
    "dispersion": 2.0, "quadrupole": 2.0, "heat_of_adsorption": 2.0, "henry": 1.5, "kinetic_diameter": 2.5,
    "open_metal_sites": 3.0, "electrostatics": 1.0, "gas_separation": 1.0, "gcmc": 2.0,
}


def score(topics: list[str], strong: list[str]) -> float:
    return sum(WEIGHTS[t] for t in topics) + 1.5 * min(len(strong), 3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--min-topics", type=int, default=2, help="distinct topic hits needed without a strong hit")
    ap.add_argument("--max-records", type=int, default=2000,
                    help="keep only the top-N eligible records by relevance score (size control)")
    ap.add_argument("--out", type=Path, default=OUT_FILE)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".jsonl.tmp")
    seen = eligible = bad = 0
    heap: list[tuple[float, int, str]] = []  # min-heap of (score, -id, json): ties favour earlier lines
    for lineno, line in enumerate(sys.stdin.buffer, start=1):
        seen += 1
        if seen % 20000 == 0:
            print(f"  streamed {seen:,} records, eligible {eligible:,}", file=sys.stderr, flush=True)
        try:
            rec = json.loads(line)
            msgs = rec["messages"]
            user = next(m for m in msgs if m.get("role") == "user")
            asst = next(m for m in msgs if m.get("role") == "assistant")
        except (ValueError, KeyError, StopIteration, TypeError, AttributeError):
            bad += 1
            continue
        question = clean_question(str(user.get("content") or ""))
        answer_full = str(asst.get("content") or "")
        # Score on the problem statement and final solution; the long chain of thought
        # would match nearly every keyword list by accident.
        topics, strong = match_topics(question + "\n" + answer_full)
        if not keep(topics, strong, args.min_topics):
            continue
        eligible += 1
        sc = score(topics, strong)
        if len(heap) >= args.max_records and (sc, -lineno) <= heap[0][:2]:
            continue
        boxed = BOXED.findall(answer_full)
        row = {
            "id": lineno,
            "uuid": rec.get("uuid"),
            "relevance": round(sc, 2),
            "topics": topics,
            "strong": strong,
            "question": truncate(question),
            "reasoning": truncate(str(asst.get("reasoning_content") or "")),
            "answer": truncate(answer_full),
            "final": truncate(boxed[-1], 500) if boxed else "",
        }
        item = (sc, -lineno, json.dumps(row, ensure_ascii=False))
        if len(heap) < args.max_records:
            heapq.heappush(heap, item)
        else:
            heapq.heapreplace(heap, item)

    rows = sorted(heap, key=lambda it: -it[1])  # write in source-line order
    kept = len(rows)
    topic_counts: Counter[str] = Counter()
    strong_counts: Counter[str] = Counter()
    with tmp.open("w", encoding="utf-8") as out:
        for _, _, js in rows:
            r = json.loads(js)
            topic_counts.update(r["topics"])
            strong_counts.update(r["strong"])
            out.write(js + "\n")
    tmp.replace(args.out)
    min_score = min((it[0] for it in rows), default=0.0)

    size = args.out.stat().st_size
    print(f"done: streamed {seen:,}, malformed {bad:,}, eligible {eligible:,}, kept {kept:,} (min score {min_score}), {size / 1e6:.1f} MB -> {args.out}",
          file=sys.stderr)
    for t, c in topic_counts.most_common():
        print(f"  {t:20s} {c}", file=sys.stderr)
    if kept < 50:
        print("WARNING: fewer than 50 records kept; consider --min-topics 1", file=sys.stderr)

    write_readme(args.out.parent / "README.md", seen, bad, eligible, kept, size, topic_counts, strong_counts,
                 args.min_topics, args.max_records, min_score)
    return 0


def write_readme(readme: Path, seen, bad, eligible, kept, size, topic_counts, strong_counts, min_topics,
                 max_records, min_score) -> None:
    today = _dt.date.today().isoformat()
    topic_lines = "\n".join(f"| {t} | {c} |" for t, c in topic_counts.most_common())
    strong_lines = ", ".join(f"{s} ({c})" for s, c in strong_counts.most_common(15))
    readme.write_text(
        f"""# SIEVE science library

Filtered subset of **{DATASET}** (split file `{SPLIT_FILE}`), used to ground
the scout and peer-reviewer agents via `sieve.library.search()`.

- Source: {URL}
- Dataset card: https://huggingface.co/datasets/{DATASET}
- License: **CC-BY-4.0** (attribution: NVIDIA, Nemotron-Science-v1). Records are
  synthetic, LLM-generated problems and solutions; treat them as background
  reasoning, not as peer-reviewed fact.
- Built: {today} by `scripts/build_library.py` (streamed from stdin; the full
  source file was never stored on disk).

## Filter

Keyword topics (word-boundary regexes) are matched against the problem
statement plus final solution (not the chain of thought). A record is kept if
it contains at least one *strong* phrase (e.g. "adsorption isotherm",
"metal-organic framework", "isosteric heat", "open metal site", "kinetic
diameter", "zeolite", "physisorption") **or** at least {min_topics} distinct topics, at
least one of which is adsorption/porous-material related. Eligible records
are then ranked by a relevance score (weighted sum of topics, SIEVE-specific
ones such as open metal sites, kinetic diameter, quadrupole, dispersion and CO2
weighted highest, plus 1.5 per strong phrase up to 3) and the top
{max_records:,} are kept (lowest kept score: {min_score}; ties go to earlier lines). Question, reasoning
and answer are each truncated to {MAX_CHARS:,} characters. `id` is the 1-based line
number in `RQA.jsonl`; `uuid` is the source record id.

## Counts

- Records streamed: {seen:,} (malformed: {bad:,})
- Records eligible under the keyword rule: {eligible:,}
- Records kept (top-{max_records:,} by relevance): {kept:,}
- File: `nemotron_science_rqa.jsonl`, {size / 1e6:.1f} MB

| topic | records |
|---|---|
{topic_lines}

Most common strong phrases: {strong_lines}
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
