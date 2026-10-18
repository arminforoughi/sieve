"""The critic (MODEL_LARGE) and its sub-critics.

After every round the critic audits the ranking: structures in the top 10 on tier-1 data alone,
error bars that do not separate the leaders, timeout patterns. It vetoes calling anything a
winner without tier-2 confirmation and can cap the simulated cell size.

In the confirmation phase it delegates: one sub-critic (MODEL_SMALL) per contested tier-1
leader examines that structure's evidence -- its measurement against what similar structures
measured (the surrogate) -- and says whether the lead is credible and worth a tier-2 run. The
critic rules with those verdicts in hand.
"""

from __future__ import annotations

import numpy as np

from sieve.agents.base import Ctx, budget, delegate, explore_phase, fix_name, leaders, recent_timeouts, step
from sieve.agents import llm
from sieve.agents.world import measured_estimates


async def sub_critics(ctx: Ctx, contested: list[dict]) -> list[dict]:
    w = ctx.world
    est = measured_estimates(ctx.st, ctx.run_id)
    mean, spread = w.surrogate({s: (e.uptake, e.err) for s, e in est.items() if s not in {c["structure"] for c in contested}})
    children = []
    for c in contested:
        s = c["structure"]
        i = w.idx.get(s)
        evidence = {**c, **(w.card(s) if i is not None else {}),
                    "neighbours_predict": None if i is None or np.isnan(mean[i]) else round(float(mean[i]), 2),
                    "neighbours_spread": None if i is None or np.isnan(spread[i]) else round(float(spread[i]), 2)}

        def validate(d, s=s):
            cred = min(1.0, max(0.0, float(d.get("credibility", 0.5))))
            return {"structure": s, "confirm": bool(d.get("confirm", True)), "credibility": cred,
                    "_text": f"{s.replace('_clean', '')}: {'confirm' if d.get('confirm', True) else 'skip'}, "
                             f"credibility {cred:.2f} — {str(d.get('reason', ''))[:100]}"}

        def fallback(s=s, ev=evidence):
            pred = ev.get("neighbours_predict")
            cred = 0.5 if pred is None else max(0.0, min(1.0, 1 - abs(ev["uptake"] - pred) / max(ev["uptake"], 1e-6)))
            return {"structure": s, "confirm": True, "credibility": cred, "_text": f"{s}: rule-based, confirm"}

        children.append({"id": f"critic/{s.replace('_clean', '')[:16]}", "role": "critic",
                         "model": ctx.models.get("subcritic", llm.MODEL_SMALL),
                         "task": f"examine the lead of {s.replace('_clean', '')}",
                         "extra": "You are a sub-critic examining ONE leading structure measured only at tier 1. "
                                  "Compare its measurement (and error bar) with what similar structures measured "
                                  "(neighbours_predict, with its spread). An uptake far above its neighbours may be "
                                  "noise or a real outlier; either way only tier 2 can tell. Say whether it deserves "
                                  "a tier-2 confirmation and how credible its lead is.",
                         "user": {"evidence": evidence, "answer_schema": {"confirm": "bool", "credibility": "0..1",
                                                                          "reason": "one sentence"}},
                         "fallback": fallback, "validate": validate, "expect": ("confirm", "credibility")})
    return await delegate(ctx, "critic", children)


async def critique(ctx: Ctx) -> None:
    lead = leaders(ctx, 10)
    auto = [x["structure"] for x in lead if x["tier"] < 2]  # brief: top-10 on tier-1 data alone
    verdicts = []
    if not explore_phase(ctx) and auto:
        verdicts = await sub_critics(ctx, [x for x in lead if x["tier"] < 2][:3])

    def validate(d):
        vetoes = []
        for v in d.get("vetoes") or []:
            s = fix_name(ctx, str(v.get("structure", "")))
            if s:
                vetoes.append({"structure": s, "reason": str(v.get("reason", ""))[:200]})
        force = [s for s in (fix_name(ctx, str(x)) for x in d.get("force_tier2") or []) if s]
        cap = d.get("cap_atoms_sim")
        cap = int(cap) if isinstance(cap, (int, float)) and cap > 200 else None
        return {"vetoes": vetoes, "force_tier2": force, "cap_atoms_sim": cap,
                "_text": str(d.get("notes", ""))[:200] or f"{len(vetoes)} vetoes"}

    def fallback():
        top3 = [x["structure"] for x in lead[:3] if x["tier"] < 2]
        return validate({"vetoes": [{"structure": s, "reason": "top-3 on tier-1 data only"} for s in top3],
                         "force_tier2": [v["structure"] for v in verdicts if v["confirm"]] or top3,
                         "notes": "rule-based audit"})

    out, _ = await step(ctx, "critic", f"audit round {ctx.round}",
                        "Audit the ranking. Flag any structure in the top 10 on tier-1 data alone, check whether "
                        "the error bars actually separate the leaders, and veto calling anything a winner without "
                        "tier-2 confirmation. Confirmation is scheduled after the exploration phase, so early on "
                        "flag rather than force. If sub-critic verdicts are given, use them to choose what to "
                        "confirm. Look for timeout patterns (very large cells) and set a cap if they waste budget."
                        + (" Finalist checks are enforced in code after your audit: a disordered leader "
                           "(disordered=true) is re-measured on its resolved CIF before it can be recommended."
                           if ctx.disorder_check else "")
                        + (" Under the capture objective nothing can be recommended without a tier-3 capture check;"
                           " say in notes which confirmed leaders most deserve one." if ctx.objective == "capture" else ""),
                        {"top10": lead, "tier1_only_in_top10": auto, "sub_critic_verdicts": verdicts,
                         "recent_timeouts": recent_timeouts(ctx), "budget": budget(ctx),
                         "current_cap_atoms_sim": ctx.cap_atoms,
                         "answer_schema": {"vetoes": [{"structure": "name", "reason": "short"}],
                                           "force_tier2": ["names to confirm next"], "cap_atoms_sim": "int or null",
                                           "notes": "one sentence"}},
                        fallback, validate, expect=("vetoes", "notes", "force_tier2"))
    for v in out["vetoes"]:
        if v["structure"] not in ctx.vetoed:
            ctx.vetoed[v["structure"]] = v["reason"]
            ctx.st.event(ctx.run_id, "veto", {"structure": v["structure"], "reason": v["reason"],
                                              "text": f"veto — {v['structure'].replace('_clean', '')}: {v['reason']}"},
                         agent_id="critic")
    credible = [v["structure"] for v in sorted(verdicts, key=lambda v: -v["credibility"]) if v["confirm"]]
    ctx.force_tier2 = list(dict.fromkeys(out["force_tier2"] + credible + auto[:3]))
    if out["cap_atoms_sim"] and out["cap_atoms_sim"] != ctx.cap_atoms and len(recent_timeouts(ctx)) >= 3:
        ctx.cap_atoms = out["cap_atoms_sim"]
        ctx.st.event(ctx.run_id, "decision", {"text": f"critic caps simulated cell size at {ctx.cap_atoms} atoms "
                                                      f"after repeated timeouts"}, agent_id="critic")
    # finalist checks, enforced in code: disorder re-measure, capture check (capture objective)
    from sieve import capture
    from sieve.agents import checks
    if ctx.disorder_check or ctx.objective == "capture":
        est = measured_estimates(ctx.st, ctx.run_id)
        cap = capture.estimates(ctx.st.con, ctx.run_id) if ctx.objective == "capture" else {}
        jobs = checks.audit(ctx, est, cap) if not explore_phase(ctx) else []
        have = {(j["structure"], j["tier"]) for j in ctx.force_checks}
        ctx.force_checks += [j for j in jobs if (j["structure"], j["tier"]) not in have]
    ctx.critic_notes = out.get("_text", "")
