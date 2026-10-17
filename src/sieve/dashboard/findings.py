"""What the screen found, set against standard CO2 capture materials (Compare page, read-only).

  leads       each full-pool arm's top structures, the referee's independent tier-2 value, and
              whether the crystal file is clean (sieve.disorder)
  same_model  every computed structure in the one model and protocol: the only fair comparison
  literature  approximate published values, for orientation only and labelled as such
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sieve.oracle import PROJECT_ROOT, raspa_alias

SCI = PROJECT_ROOT / "data" / "science"
FULL_ARMS = ("sieve-main", "random-main", "descriptor-main", "uniform-main")
SAME_MODEL = [("ZADREE_clean", "ZADREE", "lead · baselines"), ("QIPROY_clean", "QIPROY", "lead · SIEVE"),
              ("Cu-BTC", "HKUST-1", "reference MOF"), ("MgMOF-74-VASP", "Mg-MOF-74", "reference MOF"),
              ("CAXTEC_clean", "CAXTEC", "typical pool MOF")]
# Approximate literature values (298 K). Not produced by this project; verify before citing.
LITERATURE = [
    {"material": "MEA amine (30 wt%)", "type": "liquid · industrial standard", "p015": "~1 (cyclic capacity)",
     "p1": "n/a", "binding": "~85 (chemical)", "weak": "regeneration energy, corrosion, degradation"},
    {"material": "Zeolite 13X", "type": "solid · commercial", "p015": "~2.5–3", "p1": "~4.5–5",
     "binding": "~40–50", "weak": "water ruins its performance"},
    {"material": "Mg-MOF-74", "type": "best-known MOF", "p015": "~5–6", "p1": "~8", "binding": "~40–47",
     "weak": "water-sensitive, costly"},
    {"material": "HKUST-1", "type": "common MOF", "p015": "~0.5–1", "p1": "~4", "binding": "~25–30",
     "weak": "moderate everywhere"},
]


def _science(name: str) -> dict | None:
    try:
        from sieve import science
        return science.cached(name)
    except Exception:  # noqa: BLE001  the science module is being extended; fall back to the files
        f = SCI / f"{raspa_alias(name)}.json"
        if not f.exists():
            return None
        d = json.loads(f.read_text())
        p = SCI / f"{raspa_alias(name)}.props.json"
        if p.exists():
            d["props"] = json.loads(p.read_text())
        return d


def _at(iso: list, p: float):
    return next(((q.get("uptake"), q.get("err")) for q in iso or [] if q.get("p_bar") == p), (None, None))


def _v(d: dict | None):
    return None if not d or d.get("value") is None else [round(d["value"], 3), round(d.get("err") or 0.0, 3)]


def same_model() -> list[dict]:
    rows = []
    for name, label, role in SAME_MODEL:
        d = _science(name)
        if not d:
            continue
        pr, pk = d.get("props") or {}, d.get("pockets") or {}
        wc, q = pr.get("wc") or {}, pr.get("qst") or {}
        rows.append({"name": name, "label": label, "role": role,
                     "p015": _at(d.get("isotherm"), 0.15), "p1": _at(d.get("isotherm"), 1.0),
                     "selectivity": _v(pr.get("selectivity")), "wc_vsa": _v(wc.get("vsa")),
                     "wc_tsa": _v(wc.get("tsa")), "qst": _v(q.get("widom")),
                     "deepest_well": pk.get("min_kjmol"), "pore_blocking": (d.get("pore_blocking") or {}).get("enabled")})
    return rows


def _disorder(names: list[str]) -> dict[str, dict]:
    from sieve import disorder
    known = disorder.load()
    out = {}
    for s in names:
        r = known.get(s)
        if r is None:
            f = PROJECT_ROOT / "data" / "cifs" / f"{s}.cif"
            r = disorder.scan(f) if f.exists() else None
        if r and r.get("ok"):
            out[s] = {"disordered": r["disordered"], "n_clashes": r["n_clashes"], "kinds": r["kinds"],
                      "min_distance_A": r.get("min_distance_A")}
    return out


def leads(db: str, top: int = 10) -> dict:
    from sieve.store import estimates_from_evaluations

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    have = {r[0] for r in con.execute("SELECT run_id FROM runs")}
    found: dict[str, dict] = {}
    for rid in FULL_ARMS:
        if rid not in have:
            continue
        est = sorted(estimates_from_evaluations(con, rid).values(), key=lambda e: -e.uptake)[:top]
        for rank, e in enumerate(est, 1):
            f = found.setdefault(e.structure, {"structure": e.structure, "by": []})
            f["by"].append({"run": rid, "rank": rank, "claimed": round(e.uptake, 3), "tier": e.tier})
    ref = estimates_from_evaluations(con, "referee") if "referee" in have else {}
    rows = []
    for s, f in found.items():
        r = ref.get(s)
        rows.append({**f, "referee": [round(r.uptake, 3), round(r.err, 3)] if r and r.tier >= 2 else None})
    rows.sort(key=lambda r: -(r["referee"] or [max(b["claimed"] for b in r["by"]) * 0.0])[0])
    rows = [r for r in rows if r["referee"]][:8]
    dis = _disorder([r["structure"] for r in rows])
    for r in rows:
        r["disorder"] = dis.get(r["structure"])
    res = PROJECT_ROOT / "data" / "science" / "disorder-resolved.json"  # resolved re-measurements, if any
    return {"rows": rows, "resolved": json.loads(res.read_text()) if res.exists() else {}}


def findings(db: str) -> dict:
    return {"leads": leads(db), "same_model": same_model(), "literature": LITERATURE,
            "model": "SieveUFF: UFF Lennard-Jones + TraPPE CO2, rigid, no framework charges, 298 K"}
