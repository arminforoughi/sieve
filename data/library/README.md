# SIEVE science library

Filtered subset of **nvidia/Nemotron-Science-v1** (split file `data/RQA.jsonl`), used to ground
the scout and peer-reviewer agents via `sieve.library.search()`.

- Source: https://huggingface.co/datasets/nvidia/Nemotron-Science-v1/resolve/main/data/RQA.jsonl
- Dataset card: https://huggingface.co/datasets/nvidia/Nemotron-Science-v1
- License: **CC-BY-4.0** (attribution: NVIDIA, Nemotron-Science-v1). Records are
  synthetic, LLM-generated problems and solutions; treat them as background
  reasoning, not as peer-reviewed fact.
- Built: 2026-10-17 by `scripts/build_library.py` (streamed from stdin; the full
  source file was never stored on disk).

## Filter

Keyword topics (word-boundary regexes) are matched against the problem
statement plus final solution (not the chain of thought). A record is kept if
it contains at least one *strong* phrase (e.g. "adsorption isotherm",
"metal-organic framework", "isosteric heat", "open metal site", "kinetic
diameter", "zeolite", "physisorption") **or** at least 2 distinct topics, at
least one of which is adsorption/porous-material related. Eligible records
are then ranked by a relevance score (weighted sum of topics, SIEVE-specific
ones such as open metal sites, kinetic diameter, quadrupole, dispersion and CO2
weighted highest, plus 1.5 per strong phrase up to 3) and the top
2,000 are kept (lowest kept score: 3.5; ties go to earlier lines). Question, reasoning
and answer are each truncated to 4,000 characters. `id` is the 1-based line
number in `RQA.jsonl`; `uuid` is the source record id.

## Counts

- Records streamed: 52,179 (malformed: 0)
- Records eligible under the keyword rule: 3,465
- Records kept (top-2,000 by relevance): 2,000
- File: `nemotron_science_rqa.jsonl`, 14.7 MB

| topic | records |
|---|---|
| adsorption | 1489 |
| isotherm | 1356 |
| porous_material | 1274 |
| co2_capture | 516 |
| gas_separation | 225 |
| physisorption | 208 |
| electrostatics | 180 |
| dispersion | 167 |
| heat_of_adsorption | 164 |
| kinetic_diameter | 35 |
| gcmc | 34 |
| henry | 29 |
| open_metal_sites | 29 |
| quadrupole | 27 |

Most common strong phrases: langmuir isotherm (389), bet surface (333), zeolite (197), langmuir adsorption (156), chemisorption (149), adsorption isotherm (139), heat of adsorption (89), langmuir model (81), physisorption (69), metal‑organic framework (64), isosteric heat (62), adsorption isotherms (54), activated carbon (40), freundlich (38), metal‑organic frameworks (23)
