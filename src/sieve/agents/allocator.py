"""The allocator (MODEL_LARGE): picks each round's (structure, tier) jobs.

Input: the scouts' shortlist (with family, surrogate prediction and spread, CPU cost), the
measured leaders without tier-2 data, and the round's budget and tier split. Code enforces the
rest: only known structures, no repeats, inside the round budget, tier 2 only within its share
during the exploration phase, and an under-filled round is topped up from the shortlist so no
worker idles.
"""

from __future__ import annotations

from sieve.agents.base import Ctx, affordable_t1, already, budget, explore_phase, fix_name, leaders, step
from sieve.agents.scout import shortlist
from sieve.agents.world import costs


def random_slice(ctx: Ctx, jobs: list[dict]) -> list[dict]:
    """v5 exploration floor: a fixed share of each round's tier-1 jobs goes to structures drawn
    uniformly from the whole unmeasured pool, replacing the lowest-priority picks (top-ups first,
    then the allocator's last). Whatever the agents believe, SIEVE keeps random search's luck.
    A running fraction (random_debt) keeps the share exact even when rounds are small."""
    frac = ctx.policy.get("random_frac", 0.0)
    t1 = [j for j in jobs if j["tier"] == 1]
    if frac <= 0 or not t1:
        return jobs
    ctx.random_debt += frac * len(t1)
    k = int(ctx.random_debt)
    if not k:
        return jobs
    w, limit = ctx.world, affordable_t1(ctx)
    taken = {j["structure"] for j in jobs}
    pool = [s for s in w.names if s not in taken and not already(ctx, s, 1)
            and w.desc[s].n_atoms_sim <= min(6000, ctx.cap_atoms or 6000)]
    picks = []
    for s in ctx.rng.sample(pool, min(len(pool), 8 * k)):
        if len(picks) == k:
            break
        c = costs(ctx.oracle, w, s, ctx.cost_model)[1]
        if c <= limit:
            picks.append({"structure": s, "tier": 1, "why": "random slice: exploration floor (v5)", "est_cpu_s": c})
    ctx.random_debt -= len(picks)
    drop = sorted(range(len(jobs)), key=lambda i: (jobs[i]["tier"] != 1, not jobs[i]["why"].startswith("top-up"), -i))
    drop = set([i for i in drop if jobs[i]["tier"] == 1][:len(picks)])
    return [j for i, j in enumerate(jobs) if i not in drop] + picks


async def allocate(ctx: Ctx, plan: dict) -> list[dict]:
    cands = shortlist(ctx)
    lead = [{**x, "cost_t2_cpu_s": costs(ctx.oracle, ctx.world, x["structure"], ctx.cost_model)[2]}
            for x in leaders(ctx, 12) if x["tier"] < 2 and x["structure"] in ctx.world.idx]
    rb = plan["round_budget_cpu_s"]

    def validate(d):
        late = not explore_phase(ctx)
        forced = [{"structure": s, "tier": 2, "why": "critic: confirm before it can be called a winner"}
                  for s in ctx.force_tier2 if s in ctx.world.idx] if late else []
        t2_budget = (1 - plan["tier1_fraction"]) * rb
        jobs, spent, spent2, seen = [], 0.0, 0.0, set()
        for j in forced + list(d.get("jobs") or []):
            s = fix_name(ctx, str(j.get("structure", "")))
            tier = 2 if int(j.get("tier", 1)) == 2 else 1
            if not s or (s, tier) in seen or already(ctx, s, tier):
                continue
            c = costs(ctx.oracle, ctx.world, s, ctx.cost_model)[tier]
            if jobs and spent + c > rb:
                continue
            if tier == 2 and not late and spent2 + c > t2_budget:
                continue
            seen.add((s, tier))
            spent += c
            spent2 += c if tier == 2 else 0.0
            jobs.append({"structure": s, "tier": tier, "why": str(j.get("why", ""))[:200], "est_cpu_s": c})
            if len(jobs) >= 3 * ctx.n_workers:
                break
        chosen = len(jobs)
        for c in cands:  # an under-filled round wastes workers: top up from the shortlist
            if spent >= 0.8 * rb or len(jobs) >= 3 * ctx.n_workers:
                break
            s = c["structure"]
            if (s, 1) in seen or already(ctx, s, 1):
                continue
            seen.add((s, 1))
            jobs.append({"structure": s, "tier": 1, "why": f"top-up from shortlist ({c['family']})",
                         "est_cpu_s": c["cost_t1_cpu_s"]})
            spent += c["cost_t1_cpu_s"]
        if not jobs:
            raise ValueError("no valid jobs")
        n2 = sum(j["tier"] == 2 for j in jobs)
        topped = len(jobs) - chosen
        return {"jobs": jobs, "_text": f"{len(jobs)} jobs, {n2} at tier 2 — {d.get('summary', '')}".strip(" —")
                + (f" (+{topped} topped up)" if topped else "")}

    def fallback():
        jobs = [{"structure": x["structure"], "tier": 2, "why": "leader without tier-2 data"}
                for x in lead[:max(1, int((1 - plan["tier1_fraction"]) * 6))]]
        jobs += [{"structure": c["structure"], "tier": 1, "why": f"shortlist ({c['family']})"}
                 for c in cands[:3 * ctx.n_workers]]
        return validate({"jobs": jobs, "summary": "rule-based batch"})

    out, _ = await step(ctx, "allocator", f"choose round {ctx.round}'s jobs",
                        "Choose the next batch of GCMC jobs. Tier 1 explores unmeasured structures; tier 2 "
                        "confirms measured leaders whose tier-1 error bars overlap. Weight measured evidence "
                        "over priors: the surrogate is fitted on this run's own measurements (its spread is "
                        "its uncertainty). Spread picks across the scouts' families according to their "
                        "promise, and stay inside the round budget.",
                        {"round_budget_cpu_s": round(rb), "tier1_fraction": plan["tier1_fraction"],
                         "budget": budget(ctx), "workers": ctx.n_workers,
                         "family_promise": {f: round(p, 2) for f, p in ctx.promise.items()},
                         "unmeasured_candidates": cands, "measured_leaders_without_tier2": lead,
                         "critic_forced_tier2": ctx.force_tier2,
                         "answer_schema": {"jobs": [{"structure": "name", "tier": "1 or 2", "why": "short"}],
                                           "summary": "one sentence"}},
                        fallback, validate, expect=("jobs",))
    out["jobs"] = random_slice(ctx, out["jobs"])
    if any(j["why"].startswith("random slice") for j in out["jobs"]):
        rs = [j["structure"] for j in out["jobs"] if j["why"].startswith("random slice")]
        ctx.st.event(ctx.run_id, "decision", {"text": f"random slice: {len(rs)} uniform pick(s) — "
                                                      + ", ".join(s.replace("_clean", "") for s in rs)}, agent_id="allocator")
    promoted = [j for j in out["jobs"] if j["tier"] == 2]
    if promoted:
        ctx.st.event(ctx.run_id, "promote", {"n": len(promoted), "tier": 2, "structures": [j["structure"] for j in promoted],
                                             "text": f"promoting {len(promoted)} to tier 2: "
                                                     + ", ".join(j["structure"].replace("_clean", "") for j in promoted[:4])},
                     agent_id="allocator")
    ctx.force_tier2 = [s for s in ctx.force_tier2 if s not in {j["structure"] for j in promoted}]
    return out["jobs"]
