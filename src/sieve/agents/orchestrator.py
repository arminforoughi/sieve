"""The orchestrator (MODEL_LARGE): owns the budget and the round loop, names the winner.

Round loop: plan the round (budget, tier split, whether to re-scout) -> scout (which delegates to
sub-scouts) -> allocator -> queue jobs on the worker pool -> critic (which delegates to
sub-critics) -> commit the round. The next round is planned as soon as the queue is empty and only
the slowest few jobs are still running, so idle workers pick up new work instead of waiting on a
round barrier. At the end: confirm tier-1-only leaders at tier 2, recommend a tier-2-confirmed
winner, and have the analyst write up the discoveries.
"""

from __future__ import annotations

import asyncio
import random

from sieve import pool

from sieve.agents import checks
from sieve.agents.allocator import allocate
from sieve.agents.analyst import explain
from sieve.agents.base import (Ctx, MIN_ROUND_CPU, budget, confirmation_reserve, counts, default_models,
                               already, explore_phase, fix_name, leaders, step)
from sieve.agents.critic import critique
from sieve.agents.scout import scout
from sieve.agents.world import World, measured_estimates
from sieve.oracle import CostModel
from sieve.store import Store, resume
from sieve.workers import InlineExecutor, WorkerConfig, WorkerPool, default_workers, make_oracle


async def orchestrate(ctx: Ctx) -> dict:
    b = budget(ctx)
    held = (confirmation_reserve(ctx) + funnel_reserve(ctx)) if (ctx.disorder_check or ctx.objective == "capture") else 0.0

    def validate(d):
        rb = float(d.get("round_budget_cpu_s", 0))
        rb = min(max(rb, MIN_ROUND_CPU), max(MIN_ROUND_CPU, 0.35 * b["remaining_cpu_s"]))
        if held:  # finalist checks ahead: a round may not eat the budget they need
            rb = min(rb, max(MIN_ROUND_CPU / 2, b["remaining_cpu_s"] - held))
        t1 = min(1.0, max(0.0, float(d.get("tier1_fraction", 0.7))))
        if explore_phase(ctx):  # phase 1: explore only
            t1 = 1.0
        return {"round_budget_cpu_s": rb, "tier1_fraction": t1, "call_scout": bool(d.get("call_scout")),
                "_text": f"round {ctx.round} — {rb:.0f} CPU-s, {round(100 * t1)}% tier 1. {d.get('note', '')}".strip()}

    def fallback():
        return validate({"round_budget_cpu_s": 0.12 * b["remaining_cpu_s"], "tier1_fraction": 0.5,
                         "call_scout": ctx.round % 4 == 1, "note": "rule-based plan"})

    out, _ = await step(ctx, "orchestrator", f"plan round {ctx.round}",
                        ("You own the budget. Split it between exploring new structures at tier 1 and confirming "
                         "leaders at tier 2, keeping enough in reserve to confirm the winner. Phase 1 (until 70% of "
                         "the budget is spent) is tier-1 exploration only; phase 2 confirms the leaders. Size rounds "
                         "to keep every worker busy (each worker runs one job at a time)." if ctx.objective != "capture" else
                         "You own the budget. Split it between exploring new structures at tier 1, confirming leaders "
                         "at tier 2 and capture checks at tier 3, keeping the reserve the final checks need. Phase 1 "
                         "(until 60% of the budget is spent) is tier-1 exploration only; phase 2 (to 80%) confirms "
                         "leaders at tier 2; phase 3 runs tier-3 capture checks on tier-2-confirmed leaders. Size "
                         "rounds to keep every worker busy (each worker runs one job at a time)."),
                        {"round": ctx.round, "budget": b,
                         "reserve_needed_for_final_confirmation_cpu_s": confirmation_reserve(ctx),
                         **({"phase": checks.phase(ctx),
                             "reserve_needed_for_final_checks_cpu_s": round(funnel_reserve(ctx))}
                            if (ctx.disorder_check or ctx.objective == "capture") else {}),
                         "measured_counts": counts(ctx), "leaders": leaders(ctx, 8), "workers": ctx.n_workers,
                         "critic_notes": ctx.critic_notes, "scout_view": ctx.scout_headline,
                         "family_promise": {f: round(p, 2) for f, p in ctx.promise.items()},
                         "answer_schema": {"round_budget_cpu_s": "number, CPU-seconds to spend this round",
                                           "tier1_fraction": "0..1 share of the round on tier-1 exploration",
                                           "call_scout": "bool, ask the scouts to re-assess the families",
                                           "note": "one sentence: the plan and why"}},
                        fallback, validate, expect=("round_budget_cpu_s",))
    return out


def run_options(cfg: dict) -> dict:
    """The run options a resumed run restores from its stored config. Older runs lack the keys:
    objective 'uptake', MOF-only pool, no limit, no disorder check -- exactly how they ran."""
    from sieve.capture import objective as _objective
    return {"objective": _objective(cfg), "pool_kind": pool.pool_kind(cfg), "limit": cfg.get("limit"),
            "disorder_check": bool(cfg.get("disorder_check", False))}


async def recommend_checked(ctx: Ctx) -> dict | None:
    """The recommendation when finalist checks apply (disorder re-measure and/or the capture
    objective): only structures that passed every check are allowed (checks.recommendable)."""
    from sieve import capture
    from sieve.agents import checks

    est = measured_estimates(ctx.st, ctx.run_id)
    cap = capture.estimates(ctx.st.con, ctx.run_id) if ctx.objective == "capture" else {}
    options = checks.recommendable(ctx, est, cap)[:6]
    if not options:
        ctx.st.event(ctx.run_id, "decision", {"text": "no structure passed every finalist check "
                                                      + ("(tier 2, disorder, tier-3 capture check)" if ctx.objective == "capture"
                                                         else "(tier 2, disorder)") + ": no recommendation"},
                     agent_id="orchestrator")
        return None
    by = {o["structure"]: o for o in options}
    capture_obj = ctx.objective == "capture"

    def validate(d):
        s = fix_name(ctx, str(d.get("structure", ""))) or str(d.get("structure", ""))
        o = by.get(s)
        if not o:
            raise ValueError("pick did not pass the finalist checks")
        what = (f"APS {o['aps']:.3f} mol/kg (S {o['S']:.1f}, dN {o['dn']:.3f} mol/kg), tier 3" if capture_obj
                else f"{o['uptake_t2']:.3f} mol/kg, tier 2")
        dis = " (disorder-resolved value)" if o.get("disorder") == "passed" else ""
        return {"structure": s, "uptake": o["uptake_t2"], "err": o["err"], "tier": 3 if capture_obj else 2,
                "objective": ctx.objective, "disorder": o.get("disorder"),
                **({k: o.get(k) for k in ("aps", "aps_err", "S", "S_err", "dn", "dn_err", "capture_on")} if capture_obj else {}),
                **({"uptake_raw": o["uptake_t2_raw"]} if "uptake_t2_raw" in o else {}),
                "_text": f"recommends {s.replace('_clean', '')} — {what}{dis}. {d.get('why', '')}"}

    def fallback():
        return validate({"structure": options[0]["structure"],
                         "why": "highest APS among capture-checked structures" if capture_obj else "highest checked tier-2 estimate"})

    out, _ = await step(ctx, "orchestrator", "name the winner",
                        "Name the team's recommended structure. Only structures that passed every finalist check "
                        "are offered" + (" and they are ranked by APS" if capture_obj else "") + ".",
                        {"recommendable": options, "critic_notes": ctx.critic_notes,
                         "answer_schema": {"structure": "name", "why": "one sentence"}},
                        fallback, validate, expect=("structure",))
    ctx.st.event(ctx.run_id, "recommend", {k: v for k, v in out.items() if not k.startswith("_")}
                 | {"text": out["_text"]}, agent_id="orchestrator")
    return out


async def recommend(ctx: Ctx) -> dict | None:
    if ctx.disorder_check or ctx.objective == "capture":
        return await recommend_checked(ctx)
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

    out, _ = await step(ctx, "orchestrator", "name the winner",
                        "Name the team's recommended structure. Only tier-2-confirmed candidates are allowed.",
                        {"tier2_confirmed": options, "critic_notes": ctx.critic_notes,
                         "answer_schema": {"structure": "name", "why": "one sentence"}},
                        fallback, validate, expect=("structure",))
    ctx.st.event(ctx.run_id, "recommend", {k: v for k, v in out.items() if not k.startswith("_")}
                 | {"text": out["_text"]}, agent_id="orchestrator")
    return out


def funnel_reserve(ctx: Ctx) -> float:
    """Budget the final checks will need (disorder re-measures, capture checks); 0 when neither applies."""
    if not (ctx.disorder_check or ctx.objective == "capture"):
        return 0.0
    from sieve import capture
    est = measured_estimates(ctx.st, ctx.run_id)
    cap = capture.estimates(ctx.st.con, ctx.run_id) if ctx.objective == "capture" else {}
    return checks.reserve(ctx, est, cap)


async def final_checks(ctx: Ctx, ex, max_rounds: int = 5) -> None:
    """The final funnel, after the last confirmation: run the checks the top finalists still
    need (a disorder re-measure, then a capture check), let the critic's rules judge the results,
    and repeat while a finalist was knocked out and another needs checking. Each pass is a round;
    a pass that spends nothing (every job refused by the budget) ends the funnel."""
    if not (ctx.disorder_check or ctx.objective == "capture"):
        return
    from sieve import capture
    checks.note_phase(ctx, "final")
    for _ in range(max_rounds):
        est = measured_estimates(ctx.st, ctx.run_id)
        cap = capture.estimates(ctx.st.con, ctx.run_id) if ctx.objective == "capture" else {}
        checks.audit(ctx, est, cap)
        jobs = [j for j in checks.pending(ctx, est, cap) if not already(ctx, j["structure"], j["tier"])]
        if not jobs or ctx.oracle.remaining() < MIN_ROUND_CPU / 4:
            break
        before = ctx.oracle.spent()
        ctx.round += 1
        n3, nd = sum(j["tier"] == 3 for j in jobs), sum(j["tier"] == 2 for j in jobs)
        ctx.st.start_round(ctx.run_id, ctx.round, {"phase": "final-checks", "n": len(jobs),
                                                   "split": " / ".join(x for x in (f"{nd} disorder re-measure" if nd else "",
                                                                                   f"{n3} tier 3" if n3 else "") if x),
                                                   "jobs": [f"{j['structure']} t{j['tier']}" for j in jobs]})
        ctx.st.queue_tasks(ctx.run_id, ctx.round, [{"structure": j["structure"], "tier": j["tier"], "priority": 3.0,
                                                    "reason": j["why"]} for j in jobs])
        await wait_round(ctx, ex, ctx.round)
        ctx.st.finish_round(ctx.run_id, ctx.round, {"spent_cpu_s": round(ctx.oracle.spent(), 1), "phase": "final-checks"})
        if ctx.oracle.spent() - before < 1.0:  # nothing affordable: same no-progress exit as the main loop
            ctx.st.event(ctx.run_id, "decision", {"text": "final checks: no affordable job left"}, agent_id="orchestrator")
            break
    est = measured_estimates(ctx.st, ctx.run_id)
    checks.audit(ctx, est, capture.estimates(ctx.st.con, ctx.run_id) if ctx.objective == "capture" else {})


async def wait_round(ctx: Ctx, ex, round_no: int, tail: int = 0) -> None:
    """Return when the round's queue is empty and at most `tail` of its jobs are still running."""
    while True:
        ex.supervise()
        q, r = ctx.st.con.execute(
            "SELECT SUM(status='queued'), SUM(status='running') FROM tasks WHERE run_id=? AND round_no=?",
            (ctx.run_id, round_no)).fetchone()
        if not (q or 0) and (r or 0) <= tail:
            return
        await asyncio.sleep(0.5)


async def run(db_path: str, budget_cpu_s: float = 4 * 3600.0, n_workers: int | None = None, seed: int = 0,
              run_id: str | None = None, cost_model: CostModel | None = None, candidates: list[str] | None = None,
              replay: dict | None = None, arm: str = "sieve", models: dict | None = None,
              policy: dict | None = None, objective: str = "uptake", pool_kind: str = "mof",
              limit: int | None = None, disorder_check: bool | None = None) -> str:
    """arm/models/policy: a named variant, e.g. arm='sieve-dsv4' with a different model per role,
    or arm='sieve-v5' with the upside-and-random-slice search policy.
    objective: 'uptake' (default) | 'capture' (tier-3 capture check, rank by APS; sieve/capture.py).
    pool_kind: 'mof' (default) | 'mixed' (CoRE MOFs + IZA zeolites; sieve/pool.py).
    limit: screen a random (family-stratified) subset of the pool, for smoke runs.
    disorder_check: re-measure disordered finalists on resolved CIFs (default: on, except replay).
    All of these are stored in the run config, so resume restores them."""
    st = Store(db_path)
    role_models = {**default_models(), **(models or {})}
    n_workers = n_workers or default_workers()
    if run_id and st.con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
        cfg = resume(db_path, run_id).config
        budget_cpu_s = st.run(run_id)["budget_cpu_s"]
        seed, n_workers = cfg["seed"], cfg.get("n_workers") or n_workers
        cost_model, candidates, replay = CostModel(**cfg["cost_model"]), cfg.get("candidates"), cfg.get("replay")
        role_models = {**role_models, **(cfg.get("role_models") or {})}
        policy = cfg.get("policy") or policy
        o = run_options(cfg)
        objective, pool_kind, limit, disorder_check = o["objective"], o["pool_kind"], o["limit"], o["disorder_check"]
        sample = cfg.get("sample")
    else:
        if objective == "capture" and replay:
            raise ValueError("the capture objective has no replay model (arena truth is 1-bar uptake only)")
        if disorder_check is None:
            disorder_check = not replay
        # a limited pool is sampled once and stored, so a resume screens the same structures even
        # if the pool itself changed meanwhile (e.g. more zeolite descriptors were computed)
        sample = pool.candidate_names(pool_kind, limit, seed) if limit and not candidates else None
        cost_model = cost_model or CostModel()
        run_id = st.create_run(arm, budget_cpu_s, {
            "objective": objective, "disorder_check": bool(disorder_check), "limit": limit, "sample": sample,
            **({"pool_label": "CoRE-2019-ASR + IZA-SC zeolites, PLD >= 3.3 A"} if pool_kind == "mixed" else {}),
            "role_models": role_models, "policy": policy or {}, "seed": seed, "n_workers": n_workers, "candidates": candidates,
            "replay": replay, "cost_model": cost_model.__dict__, "agent_design": "v5: upside promise + random slice" if policy else "v4: families, delegation, pipelined rounds",
            "pool": "arena" if candidates else "mixed" if pool_kind == "mixed" else "CoRE-2019-ASR, PLD >= 3.3 A"},
            run_id)
    for role, model in role_models.items():
        st.upsert_agent(run_id, role, role, model=model, parent_id=None if role == "orchestrator" else "orchestrator")
    wcfg = WorkerConfig(db_path, run_id, budget_cpu_s, cost_model, replay=replay)
    ex = (InlineExecutor if replay else WorkerPool)(wcfg, n_workers, parent_agent="orchestrator")
    ex.start()
    names = candidates or sample or pool.candidate_names(pool_kind, None, seed)
    rec = None
    tail = 0 if replay else max(1, n_workers // 3)
    try:
        done0 = {r[0] for r in st.con.execute("SELECT DISTINCT structure FROM tasks WHERE run_id=? AND tier=0", (run_id,))}
        todo = [s for s in names if s not in done0]
        if todo:  # round 0: tier 0 on every candidate, paid from the budget
            st.start_round(run_id, 0, {"phase": "tier0", "n": len(todo)})
            st.queue_tasks(run_id, 0, [{"structure": s, "tier": 0} for s in todo])
        ctx = Ctx(st, run_id, make_oracle(wcfg), None, cost_model, n_workers, random.Random(seed), models=role_models,
              policy=policy or {}, objective=objective, mixed=pool_kind == "mixed",
              disorder_check=bool(disorder_check))
        await wait_round(ctx, ex, 0)
        if todo:
            st.finish_round(run_id, 0, {"spent_cpu_s": round(ctx.oracle.spent(), 1), "phase": "tier0"})
        ctx.world = World.load(st, run_id)
        ctx.round = st.con.execute("SELECT COALESCE(MAX(round_no), 0) FROM rounds WHERE run_id=?", (run_id,)).fetchone()[0] or 0
        st.event(run_id, "decision", {"text": f"{len(ctx.world.names):,} structures described; agents take over"},
                 agent_id="orchestrator")
        stalled = 0
        while True:
            ctx.round += 1
            b = budget(ctx)
            if b["remaining_cpu_s"] <= max(MIN_ROUND_CPU, confirmation_reserve(ctx) + funnel_reserve(ctx)) and ctx.round > 2:
                break
            checks.note_phase(ctx)
            spent_before = ctx.oracle.spent()
            plan = await orchestrate(ctx)
            if plan["call_scout"] or ctx.round == 1 or ctx.round % 4 == 1:
                await scout(ctx)
            jobs = await allocate(ctx, plan)
            n3 = sum(j["tier"] == 3 for j in jobs)
            st.start_round(run_id, ctx.round, {"n": len(jobs), "phase": checks.phase(ctx),
                                               "split": f"{sum(j['tier'] == 1 for j in jobs)} tier 1 / "
                                                        f"{sum(j['tier'] == 2 for j in jobs)} tier 2"
                                                        + (f" / {n3} tier 3" if n3 else "")})
            st.queue_tasks(run_id, ctx.round, [{"structure": j["structure"], "tier": j["tier"],
                                                "priority": {2: 1.0, 3: 1.5}.get(j["tier"], 0.0)
                                                + (0.5 if pool.is_resolved(j["structure"]) else 0.0),
                                                "reason": j["why"]} for j in jobs])
            await wait_round(ctx, ex, ctx.round, tail)
            await critique(ctx)
            lead = leaders(ctx, 1)
            st.finish_round(run_id, ctx.round, {"spent_cpu_s": round(ctx.oracle.spent(), 1),
                                                "best": {"structure": lead[0]["structure"], "uptake": lead[0]["uptake"],
                                                         "tier": lead[0]["tier"]} if lead else None})
            if ctx.oracle.remaining() < MIN_ROUND_CPU:
                break
            # End-of-budget livelock guard: when every job the agents still want is refused by the
            # budget (e.g. a tier-2 needing 190 s with 76 s left), a round spends nothing and the
            # next one asks for the same job. Two rounds in a row without progress end the loop.
            stalled = stalled + 1 if ctx.oracle.spent() - spent_before < 1.0 else 0
            if stalled >= 2:
                st.event(run_id, "decision", {"text": f"no affordable work left ({ctx.oracle.remaining():.0f} CPU-s "
                                                      f"remaining, two rounds without progress): ending the search"},
                         agent_id="orchestrator")
                break
        for r in range(1, ctx.round + 1):  # let every straggler finish before the final decision
            await wait_round(ctx, ex, r)
        need = [x["structure"] for x in leaders(ctx, 3) if x["tier"] < 2]
        if need and ctx.oracle.remaining() > MIN_ROUND_CPU:
            ctx.round += 1
            st.start_round(run_id, ctx.round, {"phase": "confirm", "n": len(need), "tier": 2})
            st.queue_tasks(run_id, ctx.round, [{"structure": s, "tier": 2, "priority": 2.0, "reason": "final confirmation"}
                                               for s in need])
            await wait_round(ctx, ex, ctx.round)
            st.finish_round(run_id, ctx.round, {"spent_cpu_s": round(ctx.oracle.spent(), 1), "phase": "confirm"})
        await final_checks(ctx, ex)
        rec = await recommend(ctx)
        await explain(db_path, run_id, top=5)
    finally:
        ex.stop()
    for role in role_models:
        st.set_agent_state(role, "exited")
    st.finish_run(run_id, "finished", {"n_structures": len(measured_estimates(st, run_id)),
                                       "recommended": rec and {k: v for k, v in rec.items() if not k.startswith("_")}})
    return run_id


def run_sync(*a, **k) -> str:
    return asyncio.run(run(*a, **k))



