"""The SIEVE agent team and the orchestrator's round loop.

Each round:
  orchestrator (MODEL_LARGE)  sets the round's CPU budget and tier split, decides whether to ask
                              the scout again
  scout        (MODEL_SMALL)  turns descriptors + results so far into a scoring rule; the system
                              applies it to every unmeasured structure to build a shortlist
  allocator    (MODEL_LARGE)  picks the (structure, tier) jobs from the shortlist, the kNN
                              surrogate's predictions and the measured leaders
  critic       (MODEL_LARGE)  audits after the round: tier-1-only leaders, uncertainty that does
                              not support the ranking, timeout patterns; vetoes and forces tier 2
At the end the orchestrator recommends a winner; code refuses any pick without tier-2 data.

Guardrails live in code, not prompts: budget caps, JSON validation, known structures only, and a
deterministic fallback for every role. A failed model call is an `agent_fail`, the fallback runs,
and the agent is marked `recovered` -- the run never stops for a model error.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from dataclasses import dataclass, field

import numpy as np

from sieve import pool
from sieve.agents import llm
from sieve.agents.world import World, costs, describe_pool, measured_estimates
from sieve.oracle import CostModel, Oracle
from sieve.store import Store, resume
from sieve.workers import InlineExecutor, WorkerConfig, WorkerPool, default_workers, make_oracle

SYSTEM = (
    "You are the {role} of SIEVE, an autonomous agent team screening metal-organic frameworks for "
    "CO2 uptake at 298 K and 1 bar with GCMC simulations under a fixed CPU budget. Tier 1 GCMC has "
    "about 7% noise and is not converged; tier 2 has about 1% noise and costs roughly 3x as much "
    "(less when the structure's energy grid is already cached). The team's goal is to RECOMMEND the "
    "structure with the highest TRUE uptake, and a winner must have tier-2 confirmation. Uptake "
    "values are a ranking signal, not experimental predictions. {extra} Reply with one JSON object "
    "and nothing else.")

MIN_ROUND_CPU = 60.0
EXPLORE_UNTIL = 0.70  # phase 1 (tier-1 exploration) until 70% of the budget is spent; then confirm


@dataclass
class Ctx:
    st: Store
    run_id: str
    oracle: Oracle
    world: World
    cost_model: CostModel
    n_workers: int
    rng: random.Random
    rule: dict = field(default_factory=lambda: {"lcd_target": 6.0, "lcd_width": 2.5, "w_lcd": 1.0,
                                                "w_void": 0.5})
    rule_headline: str = "default: ~6 A cavities, high void fraction"
    vetoed: dict = field(default_factory=dict)        # structure -> reason (until tier-2 confirmed)
    force_tier2: list = field(default_factory=list)
    cap_atoms: int | None = None
    critic_notes: str = ""
    round: int = 0


# ------------------------------------------------------------------------------- agent wrapper

async def agent(ctx: Ctx, role: str, model: str, task: str, extra: str, user: dict, fallback,
                validate) -> tuple[dict, bool]:
    """Run one agent step: events, LLM call with retries, validation, deterministic fallback."""
    aid = role
    ctx.st.set_agent_state(aid, "working")
    ctx.st.event(ctx.run_id, "agent_start", {"role": role, "model": model, "text": task,
                                             "round": ctx.round}, agent_id=aid, parent_id="orchestrator"
                 if role != "orchestrator" else None)
    try:
        r = await llm.chat(ctx.st, ctx.run_id, aid, role, model, SYSTEM.format(role=role, extra=extra),
                           json.dumps(user, default=str), parent_id="orchestrator" if role != "orchestrator" else None)
        out = validate(r.data)
        ctx.st.set_agent_state(aid, "idle")
        ctx.st.event(ctx.run_id, "agent_finish", {
            "role": role, "model": model, "round": ctx.round, "text": out.get("_text", ""),
            "decision": {k: v for k, v in out.items() if not k.startswith("_")},
            "reasoning": (r.reasoning or "")[-3000:], "tokens": r.tokens_in + r.tokens_out},
            agent_id=aid, parent_id="orchestrator" if role != "orchestrator" else None)
        return out, False
    except Exception as e:  # noqa: BLE001 -- any failure falls back; the run continues
        ctx.st.set_agent_state(aid, "failed")
        ctx.st.event(ctx.run_id, "agent_fail", {"role": role, "error": f"{type(e).__name__}: {e}"[:300],
                                                "round": ctx.round}, agent_id=aid)
        out = fallback()
        ctx.st.set_agent_state(aid, "recovered")
        ctx.st.event(ctx.run_id, "agent_restart", {"role": role, "round": ctx.round,
                                                   "text": f"fell back to the rule-based {role}: {out.get('_text', '')}"},
                     agent_id=aid)
        return out, True


# ------------------------------------------------------------------------------------- state

def leaders(ctx: Ctx, n: int = 12) -> list[dict]:
    est = measured_estimates(ctx.st, ctx.run_id)
    top = sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:n]
    return [{"structure": e.structure, "tier": e.tier, "uptake": round(e.uptake, 3),
             "err": round(e.err, 3), "n_meas": e.n_evals,
             "vetoed": ctx.vetoed.get(e.structure)} for e in top]


def budget(ctx: Ctx) -> dict:
    spent = ctx.oracle.spent()
    return {"total_cpu_s": round(ctx.oracle.budget_cpu_s), "spent_cpu_s": round(spent),
            "remaining_cpu_s": round(ctx.oracle.budget_cpu_s - spent)}


def counts(ctx: Ctx) -> dict:
    return {f"tier{t}": n for t, n in ctx.st.con.execute(
        "SELECT tier, COUNT(DISTINCT structure) FROM evaluations WHERE run_id=? AND status='ok' "
        "GROUP BY tier", (ctx.run_id,))}


def recent_timeouts(ctx: Ctx) -> list[dict]:
    return [{"structure": s, "tier": t, "atoms_sim": n, "phase": ph} for s, t, n, ph in ctx.st.con.execute(
        "SELECT structure, tier, n_atoms_sim, phase FROM timeouts WHERE run_id=? ORDER BY killed_at DESC LIMIT 8",
        (ctx.run_id,))]


def fix_name(ctx: Ctx, s: str) -> str | None:
    if s in ctx.world.idx:
        return s
    if f"{s}_clean" in ctx.world.idx:
        return f"{s}_clean"
    return None


# ------------------------------------------------------------------------------- orchestrator

async def orchestrate(ctx: Ctx) -> dict:
    b = budget(ctx)
    lead = leaders(ctx, 8)
    reserve = confirmation_reserve(ctx)

    def validate(d):
        rb = float(d.get("round_budget_cpu_s", 0))
        lo, hi = MIN_ROUND_CPU, max(MIN_ROUND_CPU, 0.35 * b["remaining_cpu_s"])
        rb = min(max(rb, lo), hi)
        t1 = min(1.0, max(0.0, float(d.get("tier1_fraction", 0.7))))
        if b["spent_cpu_s"] < EXPLORE_UNTIL * b["total_cpu_s"]:  # phase 1: explore only
            t1 = 1.0
        return {"round_budget_cpu_s": rb, "tier1_fraction": t1, "call_scout": bool(d.get("call_scout")),
                "_text": f"round {ctx.round} — {rb:.0f} CPU-s, {round(100 * t1)}% tier 1. {d.get('note', '')}".strip()}

    def fallback():
        return validate({"round_budget_cpu_s": 0.12 * b["remaining_cpu_s"],
                         "tier1_fraction": 0.8 if ctx.round < 4 else 0.5,
                         "call_scout": ctx.round % 4 == 1, "note": "rule-based plan"})

    out, _ = await agent(ctx, "orchestrator", llm.MODEL_LARGE,
                         f"plan round {ctx.round}",
                         "You own the budget. Split it between exploring new structures at tier 1 and "
                         "confirming leaders at tier 2, keeping enough in reserve to confirm the winner. "
                         "Phase 1 (until 70% of the budget is spent) is tier-1 exploration only; phase 2 "
                         "confirms the leaders at tier 2. Size rounds to keep every worker busy.",
                         {"round": ctx.round, "budget": b, "reserve_needed_for_final_confirmation_cpu_s": reserve,
                          "measured_counts": counts(ctx), "leaders": lead, "workers": ctx.n_workers,
                          "critic_notes": ctx.critic_notes, "scout_rule_now": ctx.rule_headline,
                          "answer_schema": {"round_budget_cpu_s": "number, CPU-seconds to spend this round",
                                            "tier1_fraction": "0..1 share of the round on tier-1 exploration",
                                            "call_scout": "bool, ask the scout to revise its rule",
                                            "note": "one sentence: the plan and why"}},
                         fallback, validate)
    return out


def confirmation_reserve(ctx: Ctx) -> float:
    lead = [x for x in leaders(ctx, 3) if x["tier"] < 2]
    return float(sum(costs(ctx.oracle, ctx.world, x["structure"], ctx.cost_model)[2] for x in lead
                     if x["structure"] in ctx.world.idx))


# -------------------------------------------------------------------------------------- scout

async def scout(ctx: Ctx) -> None:
    w = ctx.world
    est = measured_estimates(ctx.st, ctx.run_id)
    ranked = sorted(est.values(), key=lambda e: e.uptake, reverse=True)
    best = [{**w.card(e.structure), "uptake": round(e.uptake, 2), "tier": e.tier} for e in ranked[:10]
            if e.structure in w.idx]
    worst = [{**w.card(e.structure), "uptake": round(e.uptake, 2)} for e in ranked[-5:] if e.structure in w.idx]

    def validate(d):
        rule = {k: float(d[k]) for k in ("lcd_target", "lcd_width", "w_lcd", "w_void", "w_area", "w_oms", "w_pld")
                if isinstance(d.get(k), (int, float))}
        mb = d.get("metal_bonus") or {}
        if isinstance(mb, dict):
            rule["metal_bonus"] = {str(k): float(v) for k, v in mb.items() if isinstance(v, (int, float))}
        if not rule:
            raise ValueError("no usable rule fields")
        rule = {**{k: v for k, v in ctx.rule.items() if k != "metal_bonus"}, **rule}
        rule["lcd_target"] = min(30.0, max(3.3, rule["lcd_target"]))
        return {**rule, "_text": str(d.get("headline") or d.get("reasoning") or "revised rule")[:160]}

    def fallback():
        if best:  # move the cavity target toward the measured leaders
            t = float(np.median([b["LCD"] for b in best[:5]]))
            return {**ctx.rule, "lcd_target": t, "_text": f"leaders cluster near {t:.1f} A cavities"}
        return {**ctx.rule, "_text": ctx.rule_headline}

    out, _ = await agent(ctx, "scout", llm.MODEL_SMALL, "revise the screening rule",
                         "You read structural descriptors and chemistry. CO2 at 1 bar binds best where pore "
                         "walls overlap (cavities ~1.5-2x CO2's 3.3 A kinetic diameter), at open metal sites "
                         "and in polar or functionalized pores; capacity needs void volume. Note: this force "
                         "field has no framework charges, so open metal sites and polarity matter less "
                         "than geometry. Propose a scoring rule; the system applies it to every candidate.",
                         {"pool": describe_pool(w), "best_measured_so_far": best, "worst_measured": worst,
                          "current_rule": ctx.rule,
                          "answer_schema": {"lcd_target": "A, ideal largest-cavity diameter",
                                            "lcd_width": "A, tolerance around it",
                                            "w_lcd": "weight of the cavity match", "w_void": "weight of void fraction (z)",
                                            "w_area": "weight of surface area (z)", "w_pld": "weight of pore-limiting diameter (z)",
                                            "w_oms": "bonus for open metal sites", "metal_bonus": "{element: weight}",
                                            "headline": "<= 14 words, what you are betting on"}},
                         fallback, validate)
    ctx.rule = {k: v for k, v in out.items() if not k.startswith("_")}
    ctx.rule_headline = out.get("_text", ctx.rule_headline)


def farthest_points(X: np.ndarray, pool_idx: list[int], have: list[int], k: int, rng: random.Random) -> list[int]:
    """Greedy farthest-point picks from pool_idx, away from `have` and from each other."""
    if not pool_idx:
        return []
    cand = np.array(pool_idx)
    if have:
        d = np.min(np.linalg.norm(X[cand][:, None, :] - X[np.array(have)][None, :, :], axis=2), axis=1)
    else:
        first = rng.choice(pool_idx)
        d = np.linalg.norm(X[cand] - X[first], axis=1)
        out = [first]
        d = np.minimum(d, np.linalg.norm(X[cand] - X[first], axis=1))
        k -= 1
    out = [] if have else out
    for _ in range(max(0, k)):
        j = int(np.argmax(d))
        out.append(int(cand[j]))
        d = np.minimum(d, np.linalg.norm(X[cand] - X[cand[j]], axis=1))
    return out


def shortlist(ctx: Ctx, n: int = 36) -> list[dict]:
    w = ctx.world
    est = measured_estimates(ctx.st, ctx.run_id)
    measured = {s: (e.uptake, e.err) for s, e in est.items()}
    score = w.scout_scores(ctx.rule)
    mean, spread = w.surrogate(measured)
    unmeasured = [i for i, s in enumerate(w.names) if s not in est
                  and (ctx.cap_atoms is None or w.desc[s].n_atoms_sim <= ctx.cap_atoms)]
    if not unmeasured:
        return []
    mi = [w.idx[s] for s in est if s in w.idx]
    if len(mi) < 10:
        # too little data to trust anything: cover descriptor space (farthest-point sampling),
        # plus a few of the scout's favourites so its prior gets tested, not obeyed
        picks = farthest_points(w.X, unmeasured, mi, 20, ctx.rng) + sorted(unmeasured, key=lambda i: -score[i])[:6]
    else:
        # evidence first: the surrogate's optimistic estimate leads, the scout's rule is one input
        ucb = mean + 1.0 * spread
        picks = sorted(unmeasured, key=lambda i: -ucb[i])[:20]
        picks += [i for i in sorted(unmeasured, key=lambda i: -score[i])[:8] if i not in picks]
        picks += [i for i in farthest_points(w.X, unmeasured, mi + picks, 6, ctx.rng) if i not in picks]
    picks = list(dict.fromkeys(picks))
    srank = {i: r + 1 for r, i in enumerate(sorted(unmeasured, key=lambda i: -score[i]))}
    out = []
    for i in picks[:n]:
        s = w.names[i]
        out.append({**w.card(s), "scout_rank": srank[i],
                    "surrogate": None if np.isnan(mean[i]) else round(float(mean[i]), 2),
                    "surrogate_spread": None if np.isnan(spread[i]) else round(float(spread[i]), 2),
                    "cost_t1_cpu_s": costs(ctx.oracle, w, s, ctx.cost_model)[1]})
    return out


# ---------------------------------------------------------------------------------- allocator

async def allocate(ctx: Ctx, plan: dict) -> list[dict]:
    cands = shortlist(ctx)
    lead = [{**x, "cost_t2_cpu_s": costs(ctx.oracle, ctx.world, x["structure"], ctx.cost_model)[2]}
            for x in leaders(ctx, 12) if x["tier"] < 2 and x["structure"] in ctx.world.idx]
    rb = plan["round_budget_cpu_s"]

    def validate(d):
        jobs, spent, seen = [], 0.0, set()
        b = budget(ctx)
        late = b["spent_cpu_s"] >= EXPLORE_UNTIL * b["total_cpu_s"]
        forced = [{"structure": s, "tier": 2, "why": "critic: confirm before it can be called a winner"}
                  for s in ctx.force_tier2 if s in ctx.world.idx] if late else []
        t2_budget = (1 - plan["tier1_fraction"]) * rb + (0 if late else 0.0)
        spent2 = 0.0
        for j in forced + list(d.get("jobs") or []):
            s = fix_name(ctx, str(j.get("structure", "")))
            tier = 2 if int(j.get("tier", 1)) == 2 else 1
            if not s or (s, tier) in seen or already(ctx, s, tier):
                continue
            c = costs(ctx.oracle, ctx.world, s, ctx.cost_model)[tier]
            if jobs and spent + c > rb:
                continue
            if tier == 2 and not late and spent2 + c > max(t2_budget, 0.0):
                continue  # early rounds: tier 2 only within the planned share
            spent2 += c if tier == 2 else 0.0
            seen.add((s, tier))
            jobs.append({"structure": s, "tier": tier, "why": str(j.get("why", ""))[:200], "est_cpu_s": c})
            spent += c
            if len(jobs) >= 3 * ctx.n_workers:
                break
        topped = 0
        for c in cands:  # an under-filled round wastes workers: top up from the shortlist
            if spent >= 0.8 * rb or len(jobs) >= 3 * ctx.n_workers:
                break
            s2 = c["structure"]
            if (s2, 1) in seen or already(ctx, s2, 1):
                continue
            seen.add((s2, 1))
            jobs.append({"structure": s2, "tier": 1, "why": "top-up from shortlist", "est_cpu_s": c["cost_t1_cpu_s"]})
            spent += c["cost_t1_cpu_s"]
            topped += 1
        if not jobs:
            raise ValueError("no valid jobs")
        n2 = sum(j["tier"] == 2 for j in jobs)
        if topped:
            d = {**d, "summary": f"{d.get('summary', '')} (+{topped} topped up from the shortlist)"}
        return {"jobs": jobs, "_text": f"{len(jobs)} jobs, {n2} at tier 2 — {d.get('summary', '')}".strip(" —")}

    def fallback():
        jobs = [{"structure": x["structure"], "tier": 2, "why": "leader without tier-2 data"}
                for x in lead[:max(1, int((1 - plan["tier1_fraction"]) * 6))]]
        jobs += [{"structure": c["structure"], "tier": 1, "why": "shortlist"} for c in cands[:3 * ctx.n_workers]]
        return validate({"jobs": jobs, "summary": "rule-based batch"})

    out, _ = await agent(ctx, "allocator", llm.MODEL_LARGE, f"choose round {ctx.round}'s jobs",
                         "Choose the next batch of GCMC jobs. Tier 1 explores unmeasured structures; tier 2 "
                         "confirms measured leaders whose tier-1 error bars overlap. Weight measured evidence "
                         "over priors: once the surrogate exists (fitted on this run's own measurements; its "
                         "spread is its uncertainty), prefer high surrogate + spread; the scout's rank is a "
                         "hypothesis to test, not an order. Keep some picks in unexplored regions. Stay inside "
                         "the round budget.",
                         {"round_budget_cpu_s": round(rb), "tier1_fraction": plan["tier1_fraction"],
                          "workers": ctx.n_workers, "unmeasured_candidates": cands,
                          "measured_leaders_without_tier2": lead, "critic_forced_tier2": ctx.force_tier2,
                          "answer_schema": {"jobs": [{"structure": "name", "tier": "1 or 2", "why": "short"}],
                                            "summary": "one sentence"}},
                         fallback, validate)
    n2 = [j for j in out["jobs"] if j["tier"] == 2]
    if n2:
        ctx.st.event(ctx.run_id, "promote", {"n": len(n2), "tier": 2, "structures": [j["structure"] for j in n2],
                                             "text": f"promoting {len(n2)} to tier 2: "
                                                     + ", ".join(j["structure"].replace("_clean", "") for j in n2[:4])},
                     agent_id="allocator")
    ctx.force_tier2 = [s for s in ctx.force_tier2 if s not in {j["structure"] for j in n2}]
    return out["jobs"]


def already(ctx: Ctx, s: str, tier: int) -> bool:
    return ctx.st.con.execute("SELECT 1 FROM tasks WHERE run_id=? AND structure=? AND tier=? AND "
                              "status IN ('queued','running','done')", (ctx.run_id, s, tier)).fetchone() is not None


# ------------------------------------------------------------------------------------- critic

async def critique(ctx: Ctx) -> None:
    lead = leaders(ctx, 10)
    auto = [x["structure"] for x in lead if x["tier"] < 2]  # brief: top-10 on tier-1 data alone

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
                         "force_tier2": top3, "notes": "rule-based audit"})

    out, _ = await agent(ctx, "critic", llm.MODEL_LARGE, f"audit round {ctx.round}",
                         "Audit the ranking. Flag any structure in the top 10 on tier-1 data alone, check "
                         "whether the error bars actually separate the leaders, and veto calling anything a "
                         "winner without tier-2 confirmation. Confirmation is scheduled in the second half "
                         "of the budget, so early on flag rather than force. Look for timeout patterns (e.g. very large "
                         "simulation cells) and set a cap if they waste budget.",
                         {"top10": lead, "tier1_only_in_top10": auto, "recent_timeouts": recent_timeouts(ctx),
                          "budget": budget(ctx), "current_cap_atoms_sim": ctx.cap_atoms,
                          "answer_schema": {"vetoes": [{"structure": "name", "reason": "short"}],
                                            "force_tier2": ["names to confirm next round"],
                                            "cap_atoms_sim": "int or null", "notes": "one sentence"}},
                         fallback, validate)
    for v in out["vetoes"]:
        if v["structure"] not in ctx.vetoed:
            ctx.vetoed[v["structure"]] = v["reason"]
            ctx.st.event(ctx.run_id, "veto", {"structure": v["structure"], "reason": v["reason"],
                                              "text": f"veto — {v['structure'].replace('_clean', '')}: {v['reason']}"},
                         agent_id="critic")
    # code-level rule: the top 3 on tier-1 alone are always queued for confirmation
    ctx.force_tier2 = list(dict.fromkeys(out["force_tier2"] + [s for s in auto[:3]]))
    n_timeouts = len(recent_timeouts(ctx))
    if out["cap_atoms_sim"] and out["cap_atoms_sim"] != ctx.cap_atoms and n_timeouts >= 3:
        ctx.cap_atoms = out["cap_atoms_sim"]
        ctx.st.event(ctx.run_id, "decision", {"text": f"critic caps simulated cell size at {ctx.cap_atoms} atoms "
                                                      f"after timeouts"}, agent_id="critic")
    ctx.critic_notes = out.get("_text", "")


# --------------------------------------------------------------------------- recommendation

async def recommend(ctx: Ctx) -> dict | None:
    est = measured_estimates(ctx.st, ctx.run_id)
    confirmed = sorted([e for e in est.values() if e.tier == 2], key=lambda e: e.uptake, reverse=True)[:6]
    if not confirmed:
        return None
    options = [{"structure": e.structure, "uptake_t2": round(e.uptake, 3), "err": round(e.err, 3),
                "n_meas": e.n_evals} for e in confirmed]

    def validate(d):
        s = fix_name(ctx, str(d.get("structure", "")))
        e = est.get(s) if s else None
        if not e or e.tier < 2:
            raise ValueError("pick has no tier-2 confirmation")
        return {"structure": s, "uptake": e.uptake, "err": e.err, "tier": e.tier,
                "_text": f"recommends {s.replace('_clean', '')} — {e.uptake:.3f} mol/kg, tier 2. {d.get('why', '')}"}

    def fallback():
        return validate({"structure": confirmed[0].structure, "why": "highest tier-2 estimate"})

    out, _ = await agent(ctx, "orchestrator", llm.MODEL_LARGE, "name the winner",
                         "Name the team's recommended structure. Only tier-2-confirmed candidates are allowed.",
                         {"tier2_confirmed": options, "critic_notes": ctx.critic_notes,
                          "answer_schema": {"structure": "name", "why": "one sentence"}}, fallback, validate)
    ctx.st.event(ctx.run_id, "recommend", {k: v for k, v in out.items() if not k.startswith("_")}
                 | {"text": out["_text"]}, agent_id="orchestrator")
    return out


# ----------------------------------------------------------------------------------- the run

async def _wait_round(ctx: Ctx, ex, round_no: int) -> None:
    while True:
        ex.supervise()
        n = ctx.st.con.execute("SELECT COUNT(*) FROM tasks WHERE run_id=? AND round_no=? AND "
                               "status IN ('queued','running')", (ctx.run_id, round_no)).fetchone()[0]
        if n == 0:
            return
        await asyncio.sleep(0.5)


async def run(db_path: str, budget_cpu_s: float = 4 * 3600.0, n_workers: int | None = None,
              seed: int = 0, run_id: str | None = None, cost_model: CostModel | None = None,
              candidates: list[str] | None = None, replay: dict | None = None) -> str:
    st = Store(db_path)
    n_workers = n_workers or default_workers()
    resuming = bool(run_id and st.con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone())
    if resuming:
        state = resume(db_path, run_id)
        cfg = state.config
        budget_cpu_s, seed, n_workers = state.budget_cpu_s, cfg["seed"], cfg.get("n_workers") or n_workers
        cost_model, candidates, replay = CostModel(**cfg["cost_model"]), cfg.get("candidates"), cfg.get("replay")
    else:
        cost_model = cost_model or CostModel()
        run_id = st.create_run("sieve", budget_cpu_s, {
            "seed": seed, "n_workers": n_workers, "candidates": candidates, "replay": replay,
            "cost_model": cost_model.__dict__, "models": {"large": llm.MODEL_LARGE, "small": llm.MODEL_SMALL},
            "pool": "arena" if candidates else "CoRE-2019-ASR, PLD >= 3.3 A"}, run_id)
    for role, model in (("orchestrator", llm.MODEL_LARGE), ("scout", llm.MODEL_SMALL),
                        ("allocator", llm.MODEL_LARGE), ("critic", llm.MODEL_LARGE)):
        st.upsert_agent(run_id, role, role, model=model, parent_id=None if role == "orchestrator" else "orchestrator")
    cfg = WorkerConfig(db_path, run_id, budget_cpu_s, cost_model, replay=replay)
    ex = (InlineExecutor if replay else WorkerPool)(cfg, n_workers, parent_agent="orchestrator")
    ex.start()
    names = candidates or [c.name for c in pool.load_candidates()]
    try:
        # round 0: tier 0 on every candidate (descriptors + RASPA-ready CIFs), paid from the budget
        done0 = {r[0] for r in st.con.execute("SELECT DISTINCT structure FROM tasks WHERE run_id=? AND tier=0", (run_id,))}
        todo = [s for s in names if s not in done0]
        if todo:
            st.start_round(run_id, 0, {"phase": "tier0", "n": len(todo)})
            st.queue_tasks(run_id, 0, [{"structure": s, "tier": 0} for s in todo])
        ctx = Ctx(st, run_id, make_oracle(cfg), None, cost_model, n_workers, random.Random(seed))
        await _wait_round(ctx, ex, 0)
        if todo:
            st.finish_round(run_id, 0, {"spent_cpu_s": round(ctx.oracle.spent(), 1), "phase": "tier0"})
        ctx.world = World.load(st, run_id)
        ctx.round = (st.con.execute("SELECT COALESCE(MAX(round_no), 0) FROM rounds WHERE run_id=?",
                                    (run_id,)).fetchone()[0] or 0)
        st.event(run_id, "decision", {"text": f"{len(ctx.world.names):,} structures described; agents take over"},
                 agent_id="orchestrator")
        while True:
            ctx.round += 1
            b = budget(ctx)
            reserve = confirmation_reserve(ctx)
            if b["remaining_cpu_s"] <= max(MIN_ROUND_CPU, reserve) and ctx.round > 2:
                break
            plan = await orchestrate(ctx)
            if plan["call_scout"] or ctx.round == 1 or ctx.round % 4 == 1:
                await scout(ctx)
            jobs = await allocate(ctx, plan)
            st.start_round(run_id, ctx.round, {"n": len(jobs), "split": f"{sum(j['tier'] == 1 for j in jobs)} tier 1 / "
                                                                        f"{sum(j['tier'] == 2 for j in jobs)} tier 2"})
            st.queue_tasks(run_id, ctx.round, [{"structure": j["structure"], "tier": j["tier"],
                                                "priority": 1.0 if j["tier"] == 2 else 0.0, "reason": j["why"]}
                                               for j in jobs])
            await _wait_round(ctx, ex, ctx.round)
            await critique(ctx)
            lead = leaders(ctx, 1)
            st.finish_round(run_id, ctx.round, {"spent_cpu_s": round(ctx.oracle.spent(), 1),
                                                "best": {"structure": lead[0]["structure"], "uptake": lead[0]["uptake"],
                                                         "tier": lead[0]["tier"]} if lead else None})
            if ctx.oracle.remaining() < MIN_ROUND_CPU:
                break
        # final confirmation of tier-1-only leaders, then the recommendation
        need = [x["structure"] for x in leaders(ctx, 3) if x["tier"] < 2]
        if need and ctx.oracle.remaining() > MIN_ROUND_CPU:
            ctx.round += 1
            st.start_round(run_id, ctx.round, {"phase": "confirm", "n": len(need), "tier": 2})
            st.queue_tasks(run_id, ctx.round, [{"structure": s, "tier": 2, "priority": 2.0,
                                                "reason": "final confirmation"} for s in need])
            await _wait_round(ctx, ex, ctx.round)
            st.finish_round(run_id, ctx.round, {"spent_cpu_s": round(ctx.oracle.spent(), 1), "phase": "confirm"})
        rec = await recommend(ctx)
        from sieve.agents.explain import explain
        await explain(db_path, run_id, top=5)  # the analyst writes up the discoveries
    finally:
        ex.stop()
    for role in ("orchestrator", "scout", "allocator", "critic"):
        st.set_agent_state(role, "exited")
    st.finish_run(run_id, "finished", {"n_structures": len(measured_estimates(st, run_id)),
                                       "recommended": rec and {k: v for k, v in rec.items() if not k.startswith("_")}})
    return run_id


def run_sync(*a, **k) -> str:
    return asyncio.run(run(*a, **k))
