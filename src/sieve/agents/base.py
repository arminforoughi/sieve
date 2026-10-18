"""What every SIEVE agent shares: the run context, one agent step, and delegation.

An agent step (`step`) is: an `agent_start` event, a model call with retries, validation of the
JSON reply in code, an `agent_finish` event carrying the decision and the model's reasoning. If
anything fails, a deterministic fallback runs instead (`agent_fail` -> `agent_restart`); the run
never stops for a model error.

Delegation (`delegate`) is how agents spawn agents: a parent runs several child steps
concurrently, each with its own id (`scout/f2`, `scout/f2/a`), events, tokens and recovery, and
gets their validated outputs back. Children can delegate again, down to MAX_DEPTH.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field

import numpy as np

from sieve.agents import llm
from sieve.agents.world import World, costs, measured_estimates
from sieve.oracle import CostModel, Oracle
from sieve.store import Store

SYSTEM = (
    "You are the {role} of SIEVE, an autonomous agent team screening metal-organic frameworks for "
    "CO2 uptake at 298 K and 1 bar with GCMC simulations under a fixed CPU budget. Tier 1 GCMC has "
    "about 7% noise and is not converged; tier 2 has about 1% noise and costs roughly 3x as much "
    "(less when the structure's energy grid is already cached). The team's goal is to RECOMMEND the "
    "structure with the highest TRUE uptake, and a winner must have tier-2 confirmation. Uptake "
    "values are a ranking signal, not experimental predictions. The force field has no framework "
    "charges, so only van der Waals attraction to the pore walls counts. {extra} Reply with one "
    "JSON object and nothing else.")

MIN_ROUND_CPU = 60.0
EXPLORE_UNTIL = 0.70   # phase 1 (tier-1 exploration) until 70% of the budget is spent; then confirm
MAX_DEPTH = 2          # agents may delegate to children, and children to grandchildren


def default_models() -> dict:
    return {"orchestrator": llm.MODEL_LARGE, "scout": llm.MODEL_SMALL,
            "allocator": llm.MODEL_LARGE, "critic": llm.MODEL_LARGE}


@dataclass
class Ctx:
    st: Store
    run_id: str
    oracle: Oracle
    world: World | None
    cost_model: CostModel
    n_workers: int
    rng: random.Random
    models: dict = field(default_factory=default_models)
    # what the team has learned so far
    rules: dict = field(default_factory=dict)          # family id -> scoring rule from its sub-scout
    promise: dict = field(default_factory=dict)        # family id -> 0..1, the scouts' judgement
    families: dict = field(default_factory=dict)       # family id -> list of world indices
    scout_headline: str = "not yet scouted"
    vetoed: dict = field(default_factory=dict)          # structure -> reason (until tier-2 confirmed)
    force_tier2: list = field(default_factory=list)
    cap_atoms: int | None = None
    critic_notes: str = ""
    round: int = 0
    # search policy knobs (v5): {"upside": bool, "random_frac": float, "promise_floor": float}
    policy: dict = field(default_factory=dict)
    random_debt: float = 0.0
    # run config (stored in runs.config_json, restored on resume)
    objective: str = "uptake"                          # uptake | capture (sieve/capture.py)
    mixed: bool = False                                # pool holds IZA zeolites as well as CoRE MOFs
    disorder_check: bool = False                       # re-measure disordered finalists on resolved CIFs
    force_checks: list = field(default_factory=list)   # critic: funnel jobs (disorder re-measure, tier 3)
    required: dict = field(default_factory=dict)       # checks the critic already announced
    last_phase: str | None = None


# --------------------------------------------------------------------------------- one step

async def step(ctx: Ctx, role: str, task: str, extra: str, user: dict, fallback, validate, *,
               agent_id: str | None = None, parent_id: str | None = None, model: str | None = None,
               expect: tuple[str, ...] = ()) -> tuple[dict, bool]:
    """One agent step. Returns (validated output, used_fallback)."""
    aid = agent_id or role
    model = model or ctx.models.get(role.split("/")[0], llm.MODEL_LARGE)
    parent = parent_id if parent_id is not None else (None if role == "orchestrator" else "orchestrator")
    if aid != role:
        ctx.st.upsert_agent(ctx.run_id, aid, role, model=model, parent_id=parent, state="working")
    ctx.st.set_agent_state(aid, "working")
    from sieve.agents.checks import notes
    extra = f"{extra} {notes(ctx, disorder=role.split('/')[0] in ('critic', 'scout', 'allocator'))}".strip()
    ctx.st.event(ctx.run_id, "agent_start", {"role": role, "model": model, "text": task, "round": ctx.round,
                                             "depth": aid.count("/")}, agent_id=aid, parent_id=parent)
    try:
        r = await llm.chat(ctx.st, ctx.run_id, aid, role, model, SYSTEM.format(role=role, extra=extra),
                           json.dumps(user, default=str), parent_id=parent, expect=expect)
        out = validate(r.data)
        ctx.st.set_agent_state(aid, "idle")
        ctx.st.event(ctx.run_id, "agent_finish", {
            "role": role, "model": model, "round": ctx.round, "text": out.get("_text", ""),
            "decision": {k: v for k, v in out.items() if not k.startswith("_")},
            "reasoning": (r.reasoning or "")[-3000:], "tokens": r.tokens_in + r.tokens_out,
            "depth": aid.count("/")}, agent_id=aid, parent_id=parent)
        return out, False
    except Exception as e:  # noqa: BLE001 -- any failure falls back; the run continues
        ctx.st.set_agent_state(aid, "failed")
        ctx.st.event(ctx.run_id, "agent_fail", {"role": role, "error": f"{type(e).__name__}: {e}"[:300],
                                                "round": ctx.round}, agent_id=aid, parent_id=parent)
        out = fallback()
        ctx.st.set_agent_state(aid, "recovered")
        ctx.st.event(ctx.run_id, "agent_restart", {"role": role, "round": ctx.round,
                                                   "text": f"fell back to the rule-based {role}: {out.get('_text', '')}"},
                     agent_id=aid, parent_id=parent)
        return out, True


async def delegate(ctx: Ctx, parent_id: str, children: list[dict]) -> list[dict]:
    """Spawn child agents concurrently. Each child: {id, role, task, extra, user, fallback, validate,
    expect?, model?}. Returns their outputs in order (fallback outputs when a child fails)."""
    depth = parent_id.count("/") + 1
    if depth > MAX_DEPTH or not children:
        return []
    ctx.st.event(ctx.run_id, "delegate", {"role": parent_id.split("/")[0], "n": len(children),
                                          "children": [c["id"] for c in children], "depth": depth,
                                          "text": f"spawning {len(children)} sub-agents: "
                                                  + ", ".join(c["id"].split("/")[-1] for c in children)},
                 agent_id=parent_id)

    async def run(c):
        out, _ = await step(ctx, c["role"], c["task"], c["extra"], c["user"], c["fallback"], c["validate"],
                            agent_id=c["id"], parent_id=parent_id, model=c.get("model"),
                            expect=c.get("expect", ()))
        ctx.st.set_agent_state(c["id"], "exited")
        ctx.st.event(ctx.run_id, "agent_exit", {"role": c["role"]}, agent_id=c["id"], parent_id=parent_id)
        return out

    return list(await asyncio.gather(*(run(c) for c in children)))


# ------------------------------------------------------------------------------------ state

def leaders(ctx: Ctx, n: int = 12) -> list[dict]:
    """Top pool structures by measured 1-bar uptake (disorder-resolved re-measures are checks of
    their original, not candidates). Adds the disorder flag and capture result when they apply."""
    from sieve import capture, pool
    from sieve.agents import checks

    est = measured_estimates(ctx.st, ctx.run_id)
    top = sorted((e for e in est.values() if not pool.is_resolved(e.structure)), key=lambda e: e.uptake,
                 reverse=True)[:n]
    cap = capture.estimates(ctx.st.con, ctx.run_id) if ctx.objective == "capture" else {}
    out = []
    for e in top:
        row = {"structure": e.structure, "tier": e.tier, "uptake": round(e.uptake, 3), "err": round(e.err, 3),
               "n_meas": e.n_evals, "vetoed": ctx.vetoed.get(e.structure)}
        if ctx.disorder_check and checks.is_disordered(e.structure):
            v = checks.disorder_verdict(ctx, e.structure, est)
            row["disorder"] = v["status"]
            if "resolved" in v:
                row["uptake_resolved"] = round(v["resolved"], 3)
        if ctx.objective == "capture":
            c = checks.capture_of(ctx, e.structure, cap)
            if c:
                row["aps"] = round(c["aps"], 3)
                row["selectivity"] = round(c["S"], 2) if c.get("S") is not None else None
        out.append(row)
    return out


def budget(ctx: Ctx) -> dict:
    spent = ctx.oracle.spent()
    return {"total_cpu_s": round(ctx.oracle.budget_cpu_s), "spent_cpu_s": round(spent),
            "remaining_cpu_s": round(ctx.oracle.budget_cpu_s - spent)}


def explore_phase(ctx: Ctx) -> bool:
    from sieve.agents.checks import phase
    return phase(ctx) == "explore"


def counts(ctx: Ctx) -> dict:
    return {f"tier{t}": n for t, n in ctx.st.con.execute(
        "SELECT tier, COUNT(DISTINCT structure) FROM evaluations WHERE run_id=? AND status='ok' GROUP BY tier",
        (ctx.run_id,))}


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


def already(ctx: Ctx, s: str, tier: int) -> bool:
    return ctx.st.con.execute("SELECT 1 FROM tasks WHERE run_id=? AND structure=? AND tier=? AND "
                              "status IN ('queued','running','done')", (ctx.run_id, s, tier)).fetchone() is not None


def affordable_t1(ctx: Ctx) -> float:
    """CPU-s ceiling for one exploratory tier-1 job: a single structure may not eat more than
    ~1.5% of the whole budget (giant cells at the edges of descriptor space otherwise do)."""
    return max(60.0, 0.015 * ctx.oracle.budget_cpu_s)


def confirmation_reserve(ctx: Ctx) -> float:
    lead = [x for x in leaders(ctx, 3) if x["tier"] < 2]
    return float(sum(costs(ctx.oracle, ctx.world, x["structure"], ctx.cost_model)[2] for x in lead
                     if x["structure"] in ctx.world.idx))


def farthest_points(X: np.ndarray, pool_idx: list[int], have: list[int], k: int, rng: random.Random) -> list[int]:
    """Greedy farthest-point picks from pool_idx, away from `have` and from each other."""
    if not pool_idx or k <= 0:
        return []
    cand = np.array(pool_idx)
    out: list[int] = []
    if have:
        d = np.min(np.linalg.norm(X[cand][:, None, :] - X[np.array(have)][None, :, :], axis=2), axis=1)
    else:
        first = rng.choice(pool_idx)
        out.append(first)
        d = np.linalg.norm(X[cand] - X[first], axis=1)
    while len(out) < k:
        j = int(np.argmax(d))
        if d[j] <= 0:
            break
        out.append(int(cand[j]))
        d = np.minimum(d, np.linalg.norm(X[cand] - X[cand[j]], axis=1))
    return out
