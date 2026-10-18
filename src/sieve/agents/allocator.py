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


def capture_candidates(ctx: Ctx) -> list[dict]:
    """Capture objective, phase 3: tier-2-confirmed, disorder-cleared structures without a capture
    check yet, with what a tier-3 job on them would cost."""
    from sieve import capture
    from sieve.agents import checks
    from sieve.agents.world import measured_estimates

    if ctx.objective != "capture" or checks.phase(ctx) != "capture":
        return []
    est = measured_estimates(ctx.st, ctx.run_id)
    cap = capture.estimates(ctx.st.con, ctx.run_id)
    out = []
    for s in checks.confirmed(ctx, est, 8):
        if checks.disorder_verdict(ctx, s, est)["status"] == "unchecked" or checks.capture_of(ctx, s, cap):
            continue
        t = checks.capture_target(ctx, s)
        if already(ctx, t, 3):
            continue
        out.append({"structure": s, "uptake_t2": round(est[s].uptake, 3),
                    "cost_t3_cpu_s": round(checks.job_cost(ctx, {"structure": t, "tier": 3, "of": s}))})
    return out


async def allocate(ctx: Ctx, plan: dict) -> list[dict]:
    from sieve.agents import checks

    cands = shortlist(ctx)
    lead = [{**x, "cost_t2_cpu_s": costs(ctx.oracle, ctx.world, x["structure"], ctx.cost_model)[2]}
            for x in leaders(ctx, 12) if x["tier"] < 2 and x["structure"] in ctx.world.idx]
    cap3 = capture_candidates(ctx)
    cap3_ok = {c["structure"] for c in cap3}
    rb = plan["round_budget_cpu_s"]

    def validate(d):
        late = not explore_phase(ctx)
        forced = [{"structure": s, "tier": 2, "why": "critic: confirm before it can be called a winner"}
                  for s in ctx.force_tier2 if s in ctx.world.idx] if late else []
        # the critic's funnel checks (disorder re-measure on the resolved CIF, capture check) go first
        funnel = [j for j in ctx.force_checks if late and (j["tier"] == 2 or checks.phase(ctx) == "capture")]
        t2_budget = (1 - plan["tier1_fraction"]) * rb
        jobs, spent, spent2, seen = [], 0.0, 0.0, set()
        for j in funnel:
            s, tier = j["structure"], j["tier"]
            if (s, tier) in seen or already(ctx, s, tier):
                continue
            c = checks.job_cost(ctx, j)
            if jobs and spent + c > rb:
                continue
            seen.add((s, tier))
            spent += c
            jobs.append({"structure": s, "tier": tier, "why": j["why"], "est_cpu_s": c})
        for j in forced + list(d.get("jobs") or []):
            s = fix_name(ctx, str(j.get("structure", "")))
            try:
                tier = int(j.get("tier", 1))
            except (TypeError, ValueError):
                tier = 1
            if tier == 3:  # only on confirmed, disorder-cleared structures, in the capture phase
                if not s or s not in cap3_ok:
                    continue
                t = checks.capture_target(ctx, s)
                if (t, 3) in seen or already(ctx, t, 3):
                    continue
                c = checks.job_cost(ctx, {"structure": t, "tier": 3, "of": s})
                if jobs and spent + c > rb:
                    continue
                seen.add((t, 3))
                spent += c
                jobs.append({"structure": t, "tier": 3, "why": "capture check: " + str(j.get("why", ""))[:180],
                             "est_cpu_s": c})
                continue
            tier = 2 if tier == 2 else 1
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
        n3 = sum(j["tier"] == 3 for j in jobs)
        topped = len(jobs) - chosen
        return {"jobs": jobs, "_text": f"{len(jobs)} jobs, {n2} at tier 2" + (f", {n3} at tier 3" if n3 else "")
                + f" — {d.get('summary', '')}".rstrip(" —")
                + (f" (+{topped} topped up)" if topped else "")}

    def fallback():
        jobs = [{"structure": x["structure"], "tier": 2, "why": "leader without tier-2 data"}
                for x in lead[:max(1, int((1 - plan["tier1_fraction"]) * 6))]]
        jobs += [{"structure": c["structure"], "tier": 3, "why": "rule-based: confirmed leader without APS"}
                 for c in cap3[:2]]
        jobs += [{"structure": c["structure"], "tier": 1, "why": f"shortlist ({c['family']})"}
                 for c in cands[:3 * ctx.n_workers]]
        return validate({"jobs": jobs, "summary": "rule-based batch"})

    out, _ = await step(ctx, "allocator", f"choose round {ctx.round}'s jobs",
                        "Choose the next batch of GCMC jobs. Tier 1 explores unmeasured structures; tier 2 "
                        "confirms measured leaders whose tier-1 error bars overlap"
                        + ("; tier 3 (capture objective, phase 3 only) runs the capture check on a structure from "
                           "tier2_confirmed_without_capture_check" if cap3 else "") + ". Weight measured evidence "
                        "over priors: the surrogate is fitted on this run's own measurements (its spread is "
                        "its uncertainty). Spread picks across the scouts' families according to their "
                        "promise, and stay inside the round budget.",
                        {"round_budget_cpu_s": round(rb), "tier1_fraction": plan["tier1_fraction"],
                         "budget": budget(ctx), "workers": ctx.n_workers,
                         "family_promise": {f: round(p, 2) for f, p in ctx.promise.items()},
                         "unmeasured_candidates": cands, "measured_leaders_without_tier2": lead,
                         "critic_forced_tier2": ctx.force_tier2,
                         **({"tier2_confirmed_without_capture_check": cap3} if cap3 else {}),
                         **({"critic_forced_checks": [j["structure"] for j in ctx.force_checks]} if ctx.force_checks else {}),
                         "answer_schema": {"jobs": [{"structure": "name", "tier": "1 or 2" + (" or 3" if cap3 else ""),
                                                     "why": "short"}],
                                           "summary": "one sentence"}},
                        fallback, validate, expect=("jobs",))
    out["jobs"] = random_slice(ctx, out["jobs"])
    if any(j["why"].startswith("random slice") for j in out["jobs"]):
        rs = [j["structure"] for j in out["jobs"] if j["why"].startswith("random slice")]
        ctx.st.event(ctx.run_id, "decision", {"text": f"random slice: {len(rs)} uniform pick(s) — "
                                                      + ", ".join(s.replace("_clean", "") for s in rs),
                                              "kind": "random_slice", "n": len(rs)}, agent_id="allocator")
    checked = [j for j in out["jobs"] if j["tier"] == 3 or (j["tier"] == 2 and "_dres" in j["structure"])]
    if checked:
        ctx.st.event(ctx.run_id, "promote", {"n": len(checked), "tier": 3 if checked[0]["tier"] == 3 else 2,
                                             "kind": "checks", "structures": [j["structure"] for j in checked],
                                             "text": f"scheduling {len(checked)} finalist check(s): "
                                                     + ", ".join(f"{j['structure'].replace('_clean', '')} "
                                                                 f"({'capture' if j['tier'] == 3 else 'disorder'})"
                                                                 for j in checked[:4])},
                     agent_id="allocator")
    done = {(j["structure"], j["tier"]) for j in out["jobs"]}
    ctx.force_checks = [j for j in ctx.force_checks if (j["structure"], j["tier"]) not in done]
    promoted = [j for j in out["jobs"] if j["tier"] == 2 and "_dres" not in j["structure"]]
    if promoted:
        ctx.st.event(ctx.run_id, "promote", {"n": len(promoted), "tier": 2, "structures": [j["structure"] for j in promoted],
                                             "text": f"promoting {len(promoted)} to tier 2: "
                                                     + ", ".join(j["structure"].replace("_clean", "") for j in promoted[:4])},
                     agent_id="allocator")
    ctx.force_tier2 = [s for s in ctx.force_tier2 if s not in {j["structure"] for j in promoted}]
    return out["jobs"]
