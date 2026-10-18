"""The peer reviewer: checks the science claims SIEVE makes about a structure, recursively.

  lead reviewer (MODEL_LARGE)   collects every claim made about the structure (the analyst's
                                write-up and the Science page's own statements), splits them into
                                individual claims
  sub-reviewers (SUB_MODEL)     one per claim, in parallel: check the claim against an evidence
                                pack of real numbers and return supported / partly / refuted /
                                unverifiable, with the evidence used
  recursion                     a sub-reviewer that finds a compound claim splits it, and spawns a
                                sub-sub-reviewer per part (MAX_DEPTH). Verdicts roll up in code:
                                refuted if any part is refuted, supported only if all parts are.
  lead reviewer, again          overall assessment and what would change the conclusion

The evidence pack is the run's own simulations (energy field, isotherms, references, descriptors,
measurements). The model has no framework charges, so claims that rest on electrostatics cannot
be supported by this evidence -- the reviewer is told so explicitly.

Each sub-reviewer also gets the 3 closest worked problems from the Nemotron-Science-v1 library
(sieve.library, BM25). They are background reasoning, not evidence: synthetic and LLM-written,
so they can explain a mechanism but never support a number. The ones a reviewer leans on are
cited on the claim node.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
from pathlib import Path

from sieve.agents import llm
from sieve.agents.base import MAX_DEPTH, Ctx, step
from sieve.oracle import PROJECT_ROOT, CostModel
from sieve.store import Store

REVIEW_DB = PROJECT_ROOT / "data" / "science" / "reviews.db"
POOL_BEST = "ZADREE_clean"  # best confirmed on the full pool (the Science page's first entry)
# the Science page's own caveat, stated for the reviewer so it can judge the references fairly
CONTEXT = {
    "MgMOF-74-VASP": "Experimentally Mg-MOF-74 is one of the strongest low-pressure CO2 adsorbents known, because CO2 binds "
                     "to its open Mg2+ sites electrostatically. This model has no framework charges, so it omits that.",
    "Cu-BTC": "HKUST-1 has open Cu2+ paddlewheel sites and small tetrahedral pockets; experimentally ~4 mmol/g CO2 at 1 bar, "
              "298 K, partly from electrostatics, which this charge-free model omits.",
}
# sub-reviewers on the large model: the small one accepted causal claims from bare comparisons
SUB_MODEL = llm.MODEL_LARGE
VERDICTS = ("supported", "partly", "refuted", "unverifiable")
_busy: set[str] = set()
_lock = threading.Lock()

EXTRA_SUB = ("You are a sub-reviewer checking ONE scientific claim about a metal-organic framework against "
             "the evidence pack (numbers from the team's own GCMC simulations and descriptors). Be a skeptical "
             "referee, not a proof-reader. Verdicts: 'supported' only if the numbers directly show the claim, "
             "INCLUDING any causal part ('drives', 'enhances', 'balances', 'explains'); a causal claim needs a "
             "comparison in the evidence (references, the typical structure, the energy field) -- a number "
             "being high is not evidence that it causes the uptake. 'partly' if only part holds or the cause is "
             "untested, 'refuted' if the numbers contradict it, 'unverifiable' if the evidence cannot test it. "
             "This model has NO framework partial charges: any effect of open metal sites, polarity, quadrupole "
             "or other electrostatics is NOT in the simulation, so such a claim is at best unverifiable here "
             "(and a claim that open metal sites drive the simulated uptake is refuted by the model itself). "
             "If the claim joins several independent assertions (e.g. two causes, or a number and an "
             "interpretation), put 2-3 short self-contained sub-claims in `split` instead of judging it whole. "
             "`background_reasoning` holds worked problems from the Nemotron-Science-v1 library: synthetic, "
             "LLM-written, about other materials. Use them only to judge whether a mechanism is physically "
             "plausible; they are never evidence for this structure's numbers. List in `cites` the ids you used.")


def grounding(claim: str, k: int = 3) -> list[dict]:
    """Closest Nemotron-Science-v1 passages for a claim; [] if the library is not built."""
    try:
        from sieve import library
        hits = library.search(claim, k=k)
    except (FileNotFoundError, OSError):
        return []
    return [{"id": h["id"], "citation": h["citation"], "score": round(float(h["score"]), 2),
             "question": h["question"][:300], "excerpt": h["excerpt"][:500], "answer": str(h.get("answer") or "")[:200],
             "topics": h.get("topics", [])} for h in hits]

# phrases that mark a claim resting on physics this force field does not contain
ELECTROSTATIC = re.compile(r"\bopen[- ](metal|cu|mg|zn|co|ni|fe|mn)\b|\bunsaturated metal|\boms\b|electrostat|"
                           r"\bquadrupol|\blewis[- ]acid|\bcoordinatively\b|\bpolar (site|group|surface)", re.I)


# ...unless the claim is about the model's own limitation ("the force field neglects framework charges"),
# which is a fact about the setup, not a claim that needs electrostatics to test
MODEL_CAVEAT = re.compile(r"\b(force[- ]field|model|simulation)s?\b[^.]*\b(omit|neglect|lack|ignor|exclud|without|no)\w*", re.I)


def electrostatic(claim: str) -> bool:
    return bool(ELECTROSTATIC.search(claim)) and not (MODEL_CAVEAT.search(claim) and not re.search(
        r"\b(could|would|may|might|drive|cause|enhanc|explain|alter|over|under)", claim, re.I))


def evidence_pack(name: str, analyst: dict | None) -> dict:
    from sieve import science
    from sieve.agents.analyst import dossier, pool_percentiles

    # cached() only: science.get() would start a RASPA computation in this process
    sci = science.cached(name) or {}
    brief = lambda d: {"isotherm": d["isotherm"], "pockets": {k: v for k, v in d["pockets"].items() if k != "hist"},  # noqa: E731
                       **({"capture": capture(d["props"])} if d.get("props") else {})}
    refs = {science.REFERENCES[r]: brief(d) for r in science.REFERENCES if r != name and (d := science.cached(r))}
    typ_f = PROJECT_ROOT / "data" / "science-typical.txt"
    typ_name = typ_f.read_text().strip() if typ_f.exists() else None
    typ = brief(d) if typ_name and typ_name != name and (d := science.cached(typ_name)) else None
    best = brief(d) if name != POOL_BEST and (d := science.cached(POOL_BEST)) else None
    runs = Store(PROJECT_ROOT / "data" / "runs.db")
    row = runs.con.execute("SELECT run_id FROM evaluations WHERE structure=? AND status='ok' AND tier IN (1, 2) "
                           "ORDER BY tier DESC LIMIT 1", (name,)).fetchone()
    dos = dossier(runs, row[0], name, pool_percentiles()) if row else {}
    trail = [json.loads(p).get("text", "")[:200] for (p,) in runs.con.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND type IN ('veto','promote','recommend') "
        "AND payload_json LIKE ? ORDER BY ts", (row[0], f"%{name}%"))][:8] if row else []
    return {"structure": name, "descriptors_and_percentiles": dos.get("features"),
            "metals": dos.get("metals") or sci.get("metals"), "atoms_per_cell": sci.get("n_atoms"),
            "open_metal_sites": dos.get("open_metal_sites"), "screening_measurements": dos.get("measurements"),
            "screening_decisions": trail or None,
            "energy_field": {k: v for k, v in (sci.get("pockets") or {}).items() if k != "hist"},
            "energy_field_meaning": ("RASPA van der Waals grid for the oxygen atom of a CO2 molecule (TraPPE O_co2 probe), "
                                     "kJ/mol; accessible_fraction = share of the cell where it is attractive; "
                                     "strong_fraction = share of that attractive pore volume below -10 kJ/mol; "
                                     "min_kjmol = the deepest well. The same numbers apply to the references."),
            "isotherm_298K": sci.get("isotherm"),
            "isotherm_meaning": ("GCMC uptake (mol/kg = mmol/g) at 298 K vs CO2 pressure in bar, 500 production cycles per "
                                 "point (error bars given), run for this page; NOT a screening tier. 0.15 bar = flue gas"),
            "capture_performance": capture(sci.get("props")),
            "capture_meaning": ("flue-gas capture metrics from separate GCMC runs (same force field): selectivity = "
                                "CO2/N2 adsorption selectivity from a 15:85 mixture at 1 bar, 298 K; wc_* = working "
                                "capacity in mol/kg for a vacuum swing (0.15 -> 0.01 bar), the same from the mixture, and "
                                "a temperature swing (298 -> 393 K at 0.15 bar); qst = heat of adsorption at infinite "
                                "dilution (Widom), kJ/mol; k_h = Henry constant mol/kg/bar. No charges: selectivity and "
                                "qst of open-metal-site frameworks are underestimated."),
            "context": CONTEXT.get(name), "typical_structure": typ, "references": refs,
            "best_pool_structure": {"name": POOL_BEST, **best} if best else None,
            "model": "UFF framework LJ + TraPPE CO2, rigid, NO framework charges; uptake = absolute loading",
            "analyst_writeup": analyst}


def pct(x: float) -> str:
    """A percentage that never rounds a small non-zero share to '0%' (the reviewer caught that)."""
    return "0%" if x == 0 else f"{x:.1f}%" if x < 10 else f"{x:.0f}%"


def capture(props: dict | None) -> dict | None:
    """The capture metrics, compact, for the evidence pack (None until computed)."""
    if not props:
        return None
    v = lambda d: None if not d or d.get("value") is None else (round(d["value"], 3), round(d.get("err") or 0, 3))  # noqa: E731
    wc, q = props.get("wc") or {}, props.get("qst") or {}
    return {"selectivity": v(props.get("selectivity")), "wc_vsa": v(wc.get("vsa")), "wc_mixture": v(wc.get("mixture")),
            "wc_tsa": v(wc.get("tsa")), "qst_kjmol": v(q.get("widom")),
            "k_h_co2": v((props.get("k_h") or {}).get("CO2")), "note": "(value, error)"}


def page_claims(name: str, ev: dict) -> list[str]:
    """The claims the Science page itself makes, so the reviewer checks our captions too."""
    out = []
    p, iso = ev.get("energy_field") or {}, ev.get("isotherm_298K") or []
    at = lambda iso, pb: next((q["uptake"] for q in iso if q["p_bar"] == pb), None)  # noqa: E731
    if p:
        sf = 100 * p.get("strong_fraction", 0)
        out.append(f"{pct(sf)} of {name}'s pore binds a CO2 oxygen by more than 10 kJ/mol, "
                   f"and the deepest well is {p.get('min_kjmol')} kJ/mol.")
    u, t = at(iso, 0.15), at((ev.get("typical_structure") or {}).get("isotherm") or [], 0.15)
    if u and t:
        out.append(f"At flue-gas pressure (0.15 bar) {name} holds {u:.2f} mol/kg, {u / t:.1f}x the typical pool structure "
                   f"at the same pressure ({t:.2f} mol/kg).")
    for ref, d in (ev.get("references") or {}).items():
        r = at(d["isotherm"], 0.15)
        if u and r:
            out.append(f"{name} adsorbs {'more' if u > r else 'less'} CO2 than {ref} at 0.15 bar ({u:.2f} vs {r:.2f} mol/kg).")
    # the capture tiles on the Science page make claims too
    cap = ev.get("capture_performance") or {}
    for ref, d in (ev.get("references") or {}).items():
        rc = d.get("capture") or {}
        if cap.get("selectivity") and rc.get("selectivity"):
            a, b = cap["selectivity"][0], rc["selectivity"][0]
            out.append(f"{name} separates CO2 from N2 {'better' if a > b else 'worse'} than {ref} "
                       f"(CO2/N2 selectivity {a:.1f} vs {b:.1f}).")
        if cap.get("wc_tsa") and rc.get("wc_tsa"):
            a, b = cap["wc_tsa"][0], rc["wc_tsa"][0]
            out.append(f"In a temperature-swing cycle {name} delivers {a / b:.1f}x the CO2 of {ref} per kg "
                       f"({a:.2f} vs {b:.2f} mol/kg).")
    if cap.get("qst_kjmol") and cap.get("selectivity"):
        out.append(f"{name}'s heat of adsorption ({cap['qst_kjmol'][0]:.1f} kJ/mol) is low enough for cheap "
                   f"regeneration while still giving a CO2/N2 selectivity of {cap['selectivity'][0]:.0f}.")
    return out


async def _step(*a, tries: int = 3, **kw) -> tuple[dict, bool]:
    """base.step, retried when it fell back: llm.chat does not retry transport errors such as
    ConnectionResetError, which this endpoint throws now and then under parallel load."""
    for i in range(tries):
        out, fell = await step(*a, **kw)
        if not fell or i == tries - 1:
            return out, fell
        await asyncio.sleep(2.0 * (i + 1))


def _rollup(verdicts: list[str]) -> str:
    if not verdicts:
        return "unverifiable"
    if "refuted" in verdicts:
        return "refuted" if all(v == "refuted" for v in verdicts) else "partly"
    if all(v == "supported" for v in verdicts):
        return "supported"
    if all(v == "unverifiable" for v in verdicts):
        return "unverifiable"
    return "partly"


async def check(ctx: Ctx, claim: str, ev: dict, aid: str, parent: str) -> dict:
    """Check one claim; split and recurse if it is compound. Returns a claim-tree node."""
    depth = aid.count("/")

    def validate(d):
        v = str(d.get("verdict", "")).lower()
        # models sometimes return sub-claims as {"claim": ...} objects instead of strings
        split = [str(x.get("claim", "") if isinstance(x, dict) else x)[:220] for x in (d.get("split") or [])]
        split = [x for x in split if x.strip()][:3] if not leaf else []
        if len(split) < 2:  # a "split" into one part is just the claim again
            split = []
        if v not in VERDICTS and not split:
            raise ValueError("no verdict")
        cites = {str(x).split("#")[-1].strip() for x in (d.get("cites") or [])}
        return {"verdict": v if v in VERDICTS else "partly", "evidence": str(d.get("evidence", ""))[:500],
                "split": split, "cites": sorted(cites),
                "_text": f"{v or 'split'}: {claim[:90]}"}

    def fallback():
        return {"verdict": "unverifiable", "evidence": "reviewer unavailable; not checked", "split": [], "cites": [],
                "_text": f"unverifiable (no reviewer): {claim[:80]}"}

    leaf = depth >= MAX_DEPTH
    lib = grounding(claim)
    schema = {"verdict": "|".join(VERDICTS), "evidence": "the numbers you used, one or two sentences",
              "cites": ["ids of background_reasoning items you relied on, or []"]}
    if not leaf:
        schema["split"] = ["only if compound: 2-3 independent sub-claims"]
    out, _ = await _step(ctx, "reviewer", f"check: {claim[:80]}",
                        EXTRA_SUB + (" This claim is already a part of a split claim: do NOT split it, judge it." if leaf else ""),
                        {"claim": claim, "evidence": ev, "background_reasoning": lib, "answer_schema": schema},
                        fallback, validate, agent_id=aid, parent_id=parent, model=SUB_MODEL,
                        expect=("verdict", "split"))
    node = {"id": aid, "claim": claim, "verdict": out["verdict"], "evidence": out["evidence"], "children": [],
            "grounding": [{**g, "used": str(g["id"]) in out["cites"]} for g in lib]}
    if not out["split"] and node["verdict"] == "supported" and electrostatic(claim):
        # guard in code: the model has no framework charges, so it cannot support an electrostatic claim
        node["verdict"] = "unverifiable"
        node["evidence"] = ("capped from 'supported': the claim rests on electrostatics (open metal sites / charges), "
                            "which this charge-free model does not contain. Reviewer's note: " + out["evidence"])[:600]
    if out["split"]:
        ctx.st.event(ctx.run_id, "delegate", {"role": "reviewer", "n": len(out["split"]), "depth": depth + 1,
                                              "text": f"splitting a compound claim into {len(out['split'])} parts"},
                     agent_id=aid)
        kids = await asyncio.gather(*(check(ctx, c, ev, f"{aid}/{chr(97 + i)}", aid) for i, c in enumerate(out["split"])))
        node["children"] = kids
        node["verdict"] = _rollup([k["verdict"] for k in kids])
        node["evidence"] = f"rolled up from {len(kids)} sub-claims"
        if node["verdict"] == "supported" and electrostatic(claim):
            # the split checked only the parts this model can test; the electrostatic part stays untested
            node["verdict"] = "partly"
            node["evidence"] += ("; capped at 'partly': the claim also invokes electrostatics (open metal sites / charges), "
                                 "which this charge-free model cannot test")
    ctx.st.set_agent_state(aid, "exited")
    ctx.st.event(ctx.run_id, "agent_exit", {"role": "reviewer"}, agent_id=aid, parent_id=parent)
    return node


async def draft_writeup(ctx: Ctx, name: str, ev: dict) -> dict | None:
    """The analyst's explanation of one structure, written from the evidence pack, with the same
    brief the run's analyst gets."""
    def validate(d):
        card = {"headline": str(d.get("headline", ""))[:120], "why_it_matters": str(d.get("why_it_matters", ""))[:700],
                "key_features": [str(x)[:160] for x in (d.get("key_features") or [])][:4],
                "confidence": str(d.get("confidence", ""))[:300], "caveats": str(d.get("caveats", ""))[:300]}
        if not card["why_it_matters"]:
            raise ValueError("empty write-up")
        return {**card, "_text": card["headline"]}

    out, fell = await _step(ctx, "analyst", f"explain {name.replace('_clean', '')}",
                           "You are acting as the analyst: explain why this structure captures as much (or as little) "
                           "CO2 as it does, for a technical audience, grounded ONLY in the evidence given (cite numbers). "
                           "Interpret: which features (metals, binding pockets, pore size) plausibly drive its uptake, "
                           "how it compares with the references, the typical and the best pool structure, how sure we are. "
                           "The isotherm is not a screening tier: mention tier-1/tier-2 only if screening_measurements "
                           "are present, otherwise base confidence on the isotherm error bars.",
                           {"evidence": {k: v for k, v in ev.items() if k != "analyst_writeup"},
                            "answer_schema": {"headline": "<= 12 words", "why_it_matters": "2-3 sentences",
                                              "key_features": ["3-4 short bullets, each citing a number"],
                                              "confidence": "one sentence", "caveats": "one sentence"}},
                           lambda: {"_text": "no write-up"}, validate, agent_id="analyst", parent_id="reviewer",
                           model=llm.MODEL_LARGE, expect=("why_it_matters",))
    if fell:
        return None
    return {k: v for k, v in out.items() if not k.startswith("_")}


async def review(name: str, analyst: dict | None = None) -> dict:
    REVIEW_DB.parent.mkdir(parents=True, exist_ok=True)
    st = Store(REVIEW_DB)
    run_id = f"review-{name}"
    if st.con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
        st.con.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        st.con.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
    st.create_run("review", 0.0, {"structure": name}, run_id)
    st.upsert_agent(run_id, "reviewer", "reviewer", model=llm.MODEL_LARGE)
    ctx = Ctx(st, run_id, None, None, CostModel(), 0, random.Random(0),
              models={"reviewer": llm.MODEL_LARGE})
    ev = evidence_pack(name, analyst)
    if not analyst:  # no run wrote one (e.g. the references): have the analyst write it now, then review it
        analyst = await draft_writeup(ctx, name, ev)
        ev["analyst_writeup"] = analyst
    if analyst:  # keep the reviewed write-up with the review
        st.event(run_id, "discovery", {"structure": name, **analyst, "text": analyst.get("headline") or name,
                                       "role": "analyst"}, agent_id="analyst")
    from sieve import science
    ours = page_claims(science.REFERENCES.get(name, name.replace("_clean", "")), ev)

    def v_claims(d):
        cl = [str(c)[:260] for c in (d.get("claims") or []) if str(c).strip()][:6]
        if not cl:
            raise ValueError("no claims")
        return {"claims": cl, "_text": f"{len(cl)} claims to check"}

    plan, _ = await _step(ctx, "reviewer", f"collect the claims made about {name}",
                         "You lead a peer review of claims an agent team made about one structure. List the distinct, "
                         "checkable scientific claims in the analyst write-up (max 4, verbatim or lightly condensed). "
                         "Skip claims that only restate a number without interpretation.",
                         {"analyst_writeup": analyst, "answer_schema": {"claims": ["claim text"]}},
                         lambda: {"claims": [c for c in [(analyst or {}).get("why_it_matters"), *((analyst or {}).get("key_features") or [])]
                                             if c][:4] or ["(no analyst claims)"],
                                  "_text": "rule-based claim list"},
                         v_claims, agent_id="reviewer", parent_id=None, expect=("claims",))
    claims = [c for c in plan["claims"] if c != "(no analyst claims)"] + ours
    st.event(run_id, "delegate", {"role": "reviewer", "n": len(claims), "depth": 1,
                                  "text": f"spawning {len(claims)} sub-reviewers, one per claim"}, agent_id="reviewer")
    tree = await asyncio.gather(*(check(ctx, c, ev, f"reviewer/c{i + 1}", "reviewer") for i, c in enumerate(claims)))

    tally = {v: sum(n["verdict"] == v for n in tree) for v in VERDICTS}

    def v_final(d):
        return {"assessment": str(d.get("assessment", ""))[:700], "would_change_our_mind": str(d.get("would_change_our_mind", ""))[:400],
                "_text": str(d.get("assessment", ""))[:140]}

    final, _ = await _step(ctx, "reviewer", "write the overall assessment",
                          "Summarize the peer review for a scientific audience: which claims hold, which do not, and "
                          "what experiment or simulation would change the conclusion.",
                          {"claims_checked": [{"claim": n["claim"], "verdict": n["verdict"], "evidence": n["evidence"],
                                               "parts": [{"claim": k["claim"], "verdict": k["verdict"]} for k in n["children"]]}
                                              for n in tree],
                           "screening_measurements": ev.get("screening_measurements"), "model": ev.get("model"),
                           "answer_schema": {"assessment": "2-3 sentences", "would_change_our_mind": "one sentence"}},
                          lambda: {"assessment": f"{tally['supported']} supported, {tally['partly']} partly, "
                                                 f"{tally['refuted']} refuted, {tally['unverifiable']} unverifiable.",
                                   "would_change_our_mind": "a simulation with framework charges", "_text": "rule-based summary"},
                          v_final, agent_id="reviewer", parent_id=None, expect=("assessment",))
    out = {"structure": name, "analyst_writeup": analyst, "claims": tree, "tally": tally, "assessment": final["assessment"],
           "would_change_our_mind": final["would_change_our_mind"], "models": {"lead": llm.MODEL_LARGE, "sub": SUB_MODEL}}
    (REVIEW_DB.parent / f"review-{name}.json").write_text(json.dumps(out))
    st.finish_run(run_id, "finished", {"tally": tally})
    return out


def latest_analyst_note(name: str) -> dict | None:
    for db in (PROJECT_ROOT / "data" / "runs.db", PROJECT_ROOT / "data" / "agent-v2.db"):
        if not db.exists():
            continue
        row = Store(db).con.execute("SELECT payload_json FROM events WHERE type='discovery' AND "
                                    "json_extract(payload_json,'$.structure')=? ORDER BY event_id DESC LIMIT 1",
                                    (name,)).fetchone()
        if row:
            d = json.loads(row[0])
            return {k: d.get(k) for k in ("headline", "why_it_matters", "key_features", "confidence", "caveats")}
    return None


def get(name: str) -> dict:
    f = REVIEW_DB.parent / f"review-{name}.json"
    if f.exists():
        return {"status": "ready", **json.loads(f.read_text())}
    from sieve import science
    if science.cached(name) is None:
        return {"status": "waiting", "reason": "science data still computing"}
    with _lock:
        if name not in _busy:
            _busy.add(name)

            def work():
                try:
                    asyncio.run(review(name, latest_analyst_note(name)))
                except Exception as e:  # noqa: BLE001
                    (REVIEW_DB.parent / f"review-{name}.err").write_text(f"{type(e).__name__}: {e}")
                finally:
                    _busy.discard(name)
            threading.Thread(target=work, daemon=True).start()
    return {"status": "reviewing"}


if __name__ == "__main__":
    import sys

    r = asyncio.run(review(sys.argv[1], latest_analyst_note(sys.argv[1])))
    def show(n, ind=0):
        print(" " * ind + f"[{n['verdict']}] {n['claim'][:110]}")
        for k in n["children"]:
            show(k, ind + 2)
    for n in r["claims"]:
        show(n)
    print("ASSESSMENT:", r["assessment"])
