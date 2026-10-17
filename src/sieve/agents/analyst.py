"""The analyst: explains a run's discoveries in plain language. Post-hoc, never steers the run.

For each of the run's top structures it gets the descriptors, where each sits in the candidate
pool (percentiles), every measurement the run made of it, why the allocator picked it, and the
critic's view -- and writes what the structure is, why it scored well, and how sure we are.
It sees nothing but the run's own data (no ground truth). Results are `discovery` events.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np

from sieve.agents import llm
from sieve.oracle import Descriptors, core_descriptors
from sieve.pool import load_candidates
from sieve.store import Store, estimates_from_evaluations

FEATURES = {"LCD": "largest cavity diameter (A)", "PLD": "pore-limiting diameter (A)",
            "AV_VF": "void fraction", "ASA_m2_g": "surface area (m2/g)"}


def pool_percentiles() -> dict[str, np.ndarray]:
    core = core_descriptors()
    names = {c.name for c in load_candidates()}
    return {f: np.sort(np.array([core[n][f] for n in names if core.get(n, {}).get(f) is not None]))
            for f in FEATURES}


def dossier(st: Store, run_id: str, s: str, pct: dict) -> dict:
    """Everything the run knows about one structure."""
    row = st.con.execute("SELECT result_json FROM evaluations WHERE structure=? AND tier=0 AND status='ok' "
                         "ORDER BY run_id=? DESC LIMIT 1", (s, run_id)).fetchone()
    d = Descriptors.from_dict(json.loads(row[0])) if row else None
    core = (d.core if d else None) or core_descriptors().get(s, {})
    feats = {}
    for f, label in FEATURES.items():
        v = core.get(f)
        if v is not None and len(pct[f]):
            feats[f] = {"label": label, "value": round(float(v), 3),
                        "percentile": round(100.0 * float(np.searchsorted(pct[f], v)) / len(pct[f]))}
    meas = [{"tier": t, "uptake": round(u, 3), "err": round(e, 3), "cpu_s": round(c or 0), "mode": m}
            for t, u, e, c, m in st.con.execute(
                "SELECT tier, uptake_molkg, uptake_err, cpu_s, mode FROM evaluations WHERE run_id=? AND "
                "structure=? AND tier>0 AND status='ok' ORDER BY finished_at", (run_id, s))]
    picks = [{"round": r, "tier": t, "why": w} for r, t, w in st.con.execute(
        "SELECT round_no, tier, reason FROM tasks WHERE run_id=? AND structure=? AND tier>0 ORDER BY created_at",
        (run_id, s))]
    vetoes = [json.loads(p)["reason"] for (p,) in st.con.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND type='veto' AND json_extract(payload_json,'$.structure')=?",
        (run_id, s))]
    return {"structure": s, "features": feats,
            "open_metal_sites": (core.get("Has_OMS") or "").lower().startswith("y"),
            "metals": (d.metals if d else []) or [m for m in (core.get("All_Metals") or "").split(",") if m],
            "elements": d.elements if d else [], "density_g_cm3": round(d.density, 3) if d else None,
            "atoms_per_cell": d.n_atoms if d else None, "measurements": meas, "picked": picks,
            "critic_vetoes": vetoes}


async def explain(db_path: str, run_id: str, top: int = 5) -> list[dict]:
    st = Store(db_path)
    est = estimates_from_evaluations(st.con, run_id)
    ranked = sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:top]
    pct = pool_percentiles()
    st.upsert_agent(run_id, "analyst", "analyst", model=llm.MODEL_LARGE)
    out = []
    for rank, e in enumerate(ranked, 1):
        dos = dossier(st, run_id, e.structure, pct)
        dos.update(rank_in_run=rank, best_estimate={"uptake": round(e.uptake, 3), "err": round(e.err, 3), "tier": e.tier})
        st.event(run_id, "agent_start", {"role": "analyst", "model": llm.MODEL_LARGE,
                                         "text": f"explain {e.structure.replace('_clean', '')}"}, agent_id="analyst")
        try:
            r = await llm.chat(st, run_id, "analyst", "analyst", llm.MODEL_LARGE,
                               "You are the analyst of SIEVE, an agent team that screens metal-organic frameworks "
                               "for CO2 uptake at 298 K and 1 bar with GCMC (UFF framework, TraPPE CO2, NO framework "
                               "partial charges, so only van der Waals attraction counts). Explain one discovery to a "
                               "technical audience, grounded ONLY in the data given: what the structure is like, "
                               "which features plausibly drive its uptake (cite the numbers and percentiles), how sure "
                               "the team is (tier, error bars, confirmation), and caveats. Do not invent names, "
                               "synthesis details or experimental values. Reply with one JSON object.",
                               json.dumps({**dos, "answer_schema": {
                                   "headline": "<= 12 words",
                                   "why_it_matters": "2-3 sentences",
                                   "key_features": ["3-4 short bullets, each citing a number"],
                                   "confidence": "one sentence on how certain the ranking is",
                                   "caveats": "one sentence"}}))
            d = r.data or {}
            card = {"structure": e.structure, "rank": rank, "headline": str(d.get("headline", ""))[:120],
                    "why_it_matters": str(d.get("why_it_matters", ""))[:700],
                    "key_features": [str(x)[:160] for x in (d.get("key_features") or [])][:4],
                    "confidence": str(d.get("confidence", ""))[:300], "caveats": str(d.get("caveats", ""))[:300],
                    "reasoning": (r.reasoning or "")[-2000:], "model": llm.MODEL_LARGE}
        except Exception as ex:  # noqa: BLE001 -- the dossier stands on its own without prose
            card = {"structure": e.structure, "rank": rank, "headline": "", "why_it_matters": "",
                    "key_features": [], "confidence": "", "caveats": f"analyst unavailable: {type(ex).__name__}"}
        st.event(run_id, "discovery", {**card, "text": card["headline"] or e.structure, "role": "analyst"},
                 agent_id="analyst")
        st.event(run_id, "agent_finish", {"role": "analyst", "model": llm.MODEL_LARGE,
                                          "text": card["headline"] or "dossier only"}, agent_id="analyst")
        out.append(card)
    return out


def explain_sync(db_path: str, run_id: str, top: int = 5) -> list[dict]:
    return asyncio.run(explain(db_path, run_id, top))


if __name__ == "__main__":
    import sys

    for c in explain_sync(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5):
        print(f"#{c['rank']} {c['structure']}: {c['headline']}")
