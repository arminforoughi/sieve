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

from sieve.agents.allocator import allocate
from sieve.agents.analyst import explain
from sieve.agents.base import (Ctx, MIN_ROUND_CPU, budget, confirmation_reserve, counts, default_models,
                               explore_phase, fix_name, leaders, step)
from sieve.agents.critic import critique
from sieve.agents.scout import scout
from sieve.agents.world import World, measured_estimates
from sieve.oracle import CostModel
from sieve.store import Store, resume
from sieve.workers import InlineExecutor, WorkerConfig, WorkerPool, default_workers, make_oracle


async def orchestrate(ctx: Ctx) -> dict:
    b = budget(ctx)

    def validate(d):
        rb = float(d.get("round_budget_cpu_s", 0))
        rb = min(max(rb, MIN_ROUND_CPU), max(MIN_ROUND_CPU, 0.35 * b["remaining_cpu_s"]))
        t1 = min(1.0, max(0.0, float(d.get("tier1_fraction", 0.7))))
        if explore_phase(ctx):  # phase 1: explore only
            t1 = 1.0
        return {"round_budget_cpu_s": rb, "tier1_fraction": t1, "call_scout": bool(d.get("call_scout")),
                "_text": f"round {ctx.round} — {rb:.0f} CPU-s, {round(100 * t1)}% tier 1. {d.get('note', '')}".strip()}

    def fallback():
        return validate({"round_budget_cpu_s": 0.12 * b["remaining_cpu_s"], "tier1_fraction": 0.5,
                         "call_scout": ctx.round % 4 == 1, "note": "rule-based plan"})

    out, _ = await step(ctx, "orchestrator", f"plan round {ctx.round}",
                        "You own the budget. Split it between exploring new structures at tier 1 and confirming "
                        "leaders at tier 2, keeping enough in reserve to confirm the winner. Phase 1 (until 70% of "
                        "the budget is spent) is tier-1 exploration only; phase 2 confirms the leaders. Size rounds "
                        "to keep every worker busy (each worker runs one job at a time).",
                        {"round": ctx.round, "budget": b,
                         "reserve_needed_for_final_confirmation_cpu_s": confirmation_reserve(ctx),
                         "measured_counts": counts(ctx), "leaders": leaders(ctx, 8), "workers": ctx.n_workers,
                         "critic_notes": ctx.critic_notes, "scout_view": ctx.scout_headline,
                         "family_promise": {f: round(p, 2) for f, p in ctx.promise.items()},
                         "answer_schema": {"round_budget_cpu_s": "number, CPU-seconds to spend this round",
                                           "tier1_fraction": "0..1 share of the round on tier-1 exploration",
                                           "call_scout": "bool, ask the scouts to re-assess the families",
                                           "note": "one sentence: the plan and why"}},
                        fallback, validate, expect=("round_budget_cpu_s",))
    return out


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

    out, _ = await step(ctx, "orchestrator", "name the winner",
                        "Name the team's recommended structure. Only tier-2-confirmed candidates are allowed.",
                        {"tier2_confirmed": options, "critic_notes": ctx.critic_notes,
                         "answer_schema": {"structure": "name", "why": "one sentence"}},
                        fallback, validate, expect=("structure",))
    ctx.st.event(ctx.run_id, "recommend", {k: v for k, v in out.items() if not k.startswith("_")}
                 | {"text": out["_text"]}, agent_id="orchestrator")
    return out


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
              policy: dict | None = None) -> str:
    """arm/models/policy: a named variant, e.g. arm='sieve-dsv4' with a different model per role,
    or arm='sieve-v5' with the upside-and-random-slice search policy."""
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
    else:
        cost_model = cost_model or CostModel()
        run_id = st.create_run(arm, budget_cpu_s, {
            "role_models": role_models, "policy": policy or {}, "seed": seed, "n_workers": n_workers, "candidates": candidates,
            "replay": replay, "cost_model": cost_model.__dict__, "agent_design": "v5: upside promise + random slice" if policy else "v4: families, delegation, pipelined rounds",
            "pool": "arena" if candidates else "CoRE-2019-ASR, PLD >= 3.3 A"}, run_id)
    for role, model in role_models.items():
        st.upsert_agent(run_id, role, role, model=model, parent_id=None if role == "orchestrator" else "orchestrator")
    wcfg = WorkerConfig(db_path, run_id, budget_cpu_s, cost_model, replay=replay)
    ex = (InlineExecutor if replay else WorkerPool)(wcfg, n_workers, parent_agent="orchestrator")
    ex.start()
    names = candidates or [c.name for c in pool.load_candidates()]
    rec = None
    tail = 0 if replay else max(1, n_workers // 3)
    try:
        done0 = {r[0] for r in st.con.execute("SELECT DISTINCT structure FROM tasks WHERE run_id=? AND tier=0", (run_id,))}
        todo = [s for s in names if s not in done0]
        if todo:  # round 0: tier 0 on every candidate, paid from the budget
            st.start_round(run_id, 0, {"phase": "tier0", "n": len(todo)})
            st.queue_tasks(run_id, 0, [{"structure": s, "tier": 0} for s in todo])
        ctx = Ctx(st, run_id, make_oracle(wcfg), None, cost_model, n_workers, random.Random(seed), models=role_models,
              policy=policy or {})
        await wait_round(ctx, ex, 0)
        if todo:
            st.finish_round(run_id, 0, {"spent_cpu_s": round(ctx.oracle.spent(), 1), "phase": "tier0"})
        ctx.world = World.load(st, run_id)
        ctx.round = st.con.execute("SELECT COALESCE(MAX(round_no), 0) FROM rounds WHERE run_id=?", (run_id,)).fetchone()[0] or 0
        st.event(run_id, "decision", {"text": f"{len(ctx.world.names):,} structures described; agents take over"},
                 agent_id="orchestrator")
        while True:
            ctx.round += 1
            b = budget(ctx)
            if b["remaining_cpu_s"] <= max(MIN_ROUND_CPU, confirmation_reserve(ctx)) and ctx.round > 2:
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
            await wait_round(ctx, ex, ctx.round, tail)
            await critique(ctx)
            lead = leaders(ctx, 1)
            st.finish_round(run_id, ctx.round, {"spent_cpu_s": round(ctx.oracle.spent(), 1),
                                                "best": {"structure": lead[0]["structure"], "uptake": lead[0]["uptake"],
                                                         "tier": lead[0]["tier"]} if lead else None})
            if ctx.oracle.remaining() < MIN_ROUND_CPU:
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



