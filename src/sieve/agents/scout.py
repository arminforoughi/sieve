"""The scout (MODEL_SMALL) and its sub-scouts: recursive delegation over chemical families.

The pool is split into descriptor families (k-means over cavity size, window size, void
fraction, surface area). The parent scout reads one card per family and decides which deserve
attention. It spawns a sub-scout per chosen family, in parallel; each sub-scout reads its
family's structures and results so far, proposes a scoring rule *within* the family, and rates
the family's promise. A sub-scout that finds its family too mixed may split it and spawn two
sub-sub-scouts. The shortlist then draws from each family in proportion to its promise.
"""

from __future__ import annotations

import asyncio

import numpy as np

from sieve.agents.base import (Ctx, MAX_DEPTH, affordable_t1, delegate, farthest_points, step)
from sieve.agents.world import costs, measured_estimates

N_FAMILIES = 4
RULE_KEYS = ("lcd_target", "lcd_width", "w_lcd", "w_void", "w_area", "w_oms", "w_pld")
DEFAULT_RULE = {"lcd_target": 6.0, "lcd_width": 2.5, "w_lcd": 1.0, "w_void": 0.5}


def kmeans(X: np.ndarray, idx: list[int], k: int, seed: int = 0, iters: int = 25) -> list[list[int]]:
    pts = X[idx]
    rng = np.random.default_rng(seed)
    cent = pts[rng.choice(len(pts), size=k, replace=False)]
    for _ in range(iters):
        lab = np.argmin(np.linalg.norm(pts[:, None, :] - cent[None, :, :], axis=2), axis=1)
        cent = np.array([pts[lab == j].mean(0) if (lab == j).any() else cent[j] for j in range(k)])
    groups = [[idx[i] for i in np.where(lab == j)[0]] for j in range(k)]
    return [g for g in groups if g]


def family_card(ctx: Ctx, fid: str, members: list[int]) -> dict:
    w = ctx.world
    est = measured_estimates(ctx.st, ctx.run_id)
    core = [w.desc[w.names[i]].core for i in members]
    med = lambda k: round(float(np.median([c[k] for c in core])), 2)  # noqa: E731
    metals: dict[str, int] = {}
    for i in members:
        for m in w.metals[i]:
            metals[m] = metals.get(m, 0) + 1
    meas = sorted([(est[w.names[i]].uptake, w.names[i]) for i in members if w.names[i] in est], reverse=True)
    return {"family": fid, "n": len(members), "median_LCD_A": med("LCD"), "median_PLD_A": med("PLD"),
            "median_void": med("AV_VF"), "median_area_m2g": round(med("ASA_m2_g")),
            "open_metal_fraction": round(float(np.mean([w.oms[i] for i in members])), 2),
            "common_metals": [m for m, _ in sorted(metals.items(), key=lambda kv: -kv[1])[:5]],
            "measured": len(meas), "best_measured": [{"structure": s, "uptake": round(u, 2)} for u, s in meas[:3]],
            "worst_measured": [{"structure": s, "uptake": round(u, 2)} for u, s in meas[-2:]] if len(meas) > 3 else [],
            **({"measured_spread": round(float(np.std([u for u, _ in meas])), 2)} if len(meas) >= 2 else {})}


def upside_promise(ctx: Ctx, fid: str, llm_promise: float) -> float:
    """v5: the goal is the single best structure, so a family is judged by its upside, not its
    typical member. Promise = max(the scout's view, an optimistic bound from the family's own
    measurements relative to the run's best), floored so no family's quota ever collapses."""
    pol = ctx.policy
    if not pol.get("upside"):
        return llm_promise
    est = measured_estimates(ctx.st, ctx.run_id)
    w = ctx.world
    up = [est[w.names[i]].uptake for i in ctx.families[fid] if w.names[i] in est]
    best = max((e.uptake for e in est.values() if e.tier > 0), default=0.0)
    if len(up) < 3 or best <= 0:
        opt = 0.6  # barely measured: optimism under uncertainty
    else:
        # optimistic bound on the family's top: its best plus one spread, against the run's best
        opt = min(1.0, (max(up) + float(np.std(up))) / best)
    return max(pol.get("promise_floor", 0.15), llm_promise, opt)


def ensure_families(ctx: Ctx) -> None:
    if not ctx.families:
        groups = kmeans(ctx.world.X, list(range(len(ctx.world.names))), N_FAMILIES)
        ctx.families = {f"f{i + 1}": g for i, g in enumerate(sorted(groups, key=len, reverse=True))}
        ctx.promise = {f: 0.5 for f in ctx.families}


def _rule(d: dict, base: dict) -> dict:
    rule = {k: float(d[k]) for k in RULE_KEYS if isinstance(d.get(k), (int, float))}
    mb = d.get("metal_bonus")
    if isinstance(mb, dict):
        rule["metal_bonus"] = {str(k): float(v) for k, v in mb.items() if isinstance(v, (int, float))}
    if not rule:
        raise ValueError("no usable rule fields")
    rule = {**{k: v for k, v in base.items() if k != "metal_bonus"}, **rule}
    rule["lcd_target"] = min(30.0, max(3.3, rule["lcd_target"]))
    return rule


SUB_EXTRA = ("You are a sub-scout responsible for ONE family of structures. Read the family card: its "
             "descriptor medians, metals and what has been measured in it. Propose a scoring rule for "
             "choosing structures WITHIN this family, and rate how promising the family is (0..1) given "
             "the measurements so far -- data beats textbook priors. If the family looks like two "
             "different kinds of structure mixed together, set split=true.")
UPSIDE_NOTE = (" The team is looking for the single BEST structure, not a good family on average: rate promise by "
               "the chance the family holds a top performer. A family with a poor median but a wide spread "
               "(measured_spread) or few measurements can still be very promising.")
SUB_SCHEMA = {"lcd_target": "A", "lcd_width": "A", "w_lcd": "weight", "w_void": "weight (z)",
              "w_area": "weight (z)", "w_pld": "weight (z)", "w_oms": "bonus", "metal_bonus": "{element: weight}",
              "promise": "0..1", "split": "bool", "headline": "<= 12 words"}


async def sub_scout(ctx: Ctx, fid: str, parent_id: str) -> None:
    """One family's scout. May split its family and delegate to two sub-sub-scouts."""
    members = ctx.families[fid]
    card = family_card(ctx, fid, members)
    aid = f"{parent_id}/{fid}"
    base_rule = ctx.rules.get(fid, DEFAULT_RULE)

    def validate(d):
        rule = _rule(d, base_rule)
        promise = min(1.0, max(0.0, float(d.get("promise", 0.5))))
        return {**rule, "promise": promise, "split": bool(d.get("split")),
                "_text": f"{fid}: {str(d.get('headline', ''))[:100]} (promise {promise:.2f})"}

    def fallback():
        best = card["best_measured"]
        p = 0.5 if not best else min(1.0, best[0]["uptake"] / 6.0)
        return {**base_rule, "promise": p, "split": False, "_text": f"{fid}: rule-based, promise {p:.2f}"}

    extra = SUB_EXTRA + (UPSIDE_NOTE if ctx.policy.get("upside") else "")
    out, _ = await step(ctx, "scout", f"scout family {fid} ({card['n']} structures)", extra,
                        {"family": card, "current_rule": base_rule, "answer_schema": SUB_SCHEMA},
                        fallback, validate, agent_id=aid, parent_id=parent_id, expect=("promise", "lcd_target"))
    ctx.rules[fid] = {k: v for k, v in out.items() if k in RULE_KEYS or k == "metal_bonus"}
    ctx.promise[fid] = upside_promise(ctx, fid, out["promise"])
    if out["split"] and aid.count("/") < MAX_DEPTH and len(members) >= 300:
        halves = kmeans(ctx.world.X, members, 2, seed=ctx.round)
        if len(halves) == 2:
            subs = {f"{fid}{c}": h for c, h in zip("ab", halves)}
            del ctx.families[fid]
            ctx.families.update(subs)
            children = []
            for sid, mem in subs.items():
                scard = family_card(ctx, sid, mem)
                ctx.rules[sid] = dict(ctx.rules[fid])

                def mk(sid=sid, scard=scard):
                    def v(d):
                        r = _rule(d, ctx.rules[sid])
                        p = min(1.0, max(0.0, float(d.get("promise", 0.5))))
                        return {**r, "promise": p, "_text": f"{sid}: {str(d.get('headline', ''))[:90]} (promise {p:.2f})"}
                    return v

                children.append({"id": f"{aid}/{sid}", "role": "scout", "task": f"scout sub-family {sid} ({len(mem)})",
                                 "extra": SUB_EXTRA + (UPSIDE_NOTE if ctx.policy.get("upside") else ""), "user": {"family": scard, "parent_rule": ctx.rules[fid],
                                                              "answer_schema": SUB_SCHEMA},
                                 "fallback": (lambda sid=sid: {**ctx.rules[sid], "promise": ctx.promise.get(fid, 0.5),
                                                               "_text": f"{sid}: inherited parent rule"}),
                                 "validate": mk(), "expect": ("promise", "lcd_target")})
            outs = await delegate(ctx, aid, children)
            for (sid, _), o in zip(subs.items(), outs):
                ctx.rules[sid] = {k: v for k, v in o.items() if k in RULE_KEYS or k == "metal_bonus"}
                ctx.promise[sid] = upside_promise(ctx, sid, o["promise"])
            ctx.rules.pop(fid, None)
            ctx.promise.pop(fid, None)


def _priors() -> dict:
    """Two worked problems from the Nemotron-Science-v1 library on what makes a framework adsorb CO2
    by dispersion alone (this model's physics). Synthetic background reasoning; contains no labels."""
    try:
        from sieve.agents.reviewer import grounding
        hits = grounding("pore size CO2 physisorption dispersion overlapping wall potentials metal-organic framework uptake", 2)
    except Exception:  # noqa: BLE001  library absent: scout without it
        hits = []
    return {"note": "synthetic worked problems (NVIDIA Nemotron-Science-v1), other materials; priors only",
            "items": [{k: h[k] for k in ("citation", "question", "answer")} for h in hits]}


PRIORS = _priors()


async def scout(ctx: Ctx) -> None:
    """Parent scout: choose families to study, spawn a sub-scout per family, merge."""
    ensure_families(ctx)
    cards = [family_card(ctx, f, m) for f, m in ctx.families.items()]

    def validate(d):
        focus = [f for f in (d.get("focus") or []) if f in ctx.families]
        if not focus:
            raise ValueError("no families chosen")
        return {"focus": focus[:5], "_text": str(d.get("headline", ""))[:140] or f"studying {', '.join(focus)}"}

    def fallback():
        # every family gets a look early; later, the promising ones plus the least measured
        order = sorted(ctx.families, key=lambda f: (-ctx.promise.get(f, 0.5), len(ctx.families[f])))
        return {"focus": order[:4], "_text": "rule-based: study the most promising families"}

    plan, _ = await step(ctx, "scout", "split the pool into families and decide where to look",
                         "You lead a group of sub-scouts. The pool has been split into families by pore "
                         "geometry. Choose which families (2-5) deserve a dedicated sub-scout now, favouring "
                         "families with strong results AND families nobody has measured yet.",
                         {"families": cards, "promise_so_far": ctx.promise, "background_reasoning": PRIORS,
                          "answer_schema": {"focus": ["family ids"], "headline": "<= 14 words: the bet"}},
                         fallback, validate, expect=("focus",))
    ctx.st.event(ctx.run_id, "delegate", {"role": "scout", "n": len(plan["focus"]), "depth": 1,
                                          "children": [f"scout/{f}" for f in plan["focus"]],
                                          "text": f"spawning {len(plan['focus'])} sub-scouts: {', '.join(plan['focus'])}"},
                 agent_id="scout")

    async def child(fid):
        await sub_scout(ctx, fid, "scout")
        aid = f"scout/{fid}"
        ctx.st.set_agent_state(aid, "exited")
        ctx.st.event(ctx.run_id, "agent_exit", {"role": "scout"}, agent_id=aid, parent_id="scout")

    await asyncio.gather(*(child(f) for f in plan["focus"]))
    top = sorted(ctx.promise.items(), key=lambda kv: -kv[1])[:2]
    ctx.scout_headline = f"{plan['_text']} — most promising: " + ", ".join(f"{f} ({p:.2f})" for f, p in top)
    ctx.st.event(ctx.run_id, "decision", {"text": f"scout merged {len(plan['focus'])} sub-scout reports; "
                                                  + ", ".join(f"{f} {p:.2f}" for f, p in sorted(ctx.promise.items()))},
                 agent_id="scout")


# --------------------------------------------------------------------------------- shortlist

def shortlist(ctx: Ctx, n: int = 36) -> list[dict]:
    """Candidates for the allocator: per family in proportion to promise; within a family the
    family's rule plus surrogate optimism per unit cost; only affordable structures."""
    ensure_families(ctx)
    w = ctx.world
    est = measured_estimates(ctx.st, ctx.run_id)
    measured = {s: (e.uptake, e.err) for s, e in est.items()}
    mean, spread = w.surrogate(measured)
    mi = [w.idx[s] for s in est if s in w.idx]
    limit = affordable_t1(ctx)
    t1 = {}

    def cost1(i):
        if i not in t1:
            t1[i] = costs(ctx.oracle, w, w.names[i], ctx.cost_model)[1]
        return t1[i]

    def ok(i):
        s = w.names[i]
        return (s not in est and (ctx.cap_atoms is None or w.desc[s].n_atoms_sim <= ctx.cap_atoms)
                and w.desc[s].n_atoms_sim <= 6000 and cost1(i) <= limit)

    total_p = sum(max(0.05, ctx.promise.get(f, 0.5)) for f in ctx.families) or 1.0
    picks: list[tuple[int, str]] = []
    for fid, members in ctx.families.items():
        quota = max(2, round(n * max(0.05, ctx.promise.get(fid, 0.5)) / total_p))
        cand = [i for i in members if ok(i)]
        if not cand:
            continue
        score = w.scout_scores(ctx.rules.get(fid, DEFAULT_RULE))
        in_fam = [i for i in mi if i in set(members)]
        if len(mi) < 10 or len(in_fam) < 3:  # little data: cover the family, test the rule
            chosen = farthest_points(w.X, cand, mi, quota // 2 + 1, ctx.rng)
            chosen += [i for i in sorted(cand, key=lambda i: -score[i]) if i not in chosen][: quota - len(chosen)]
        else:  # evidence first: optimistic surrogate per unit cost, nudged by the family rule
            ucb = (mean + spread) / np.sqrt(np.array([max(10.0, cost1(i)) for i in range(len(w.names))]) / 60.0)
            z = lambda a: (a - np.nanmean(a[cand])) / (np.nanstd(a[cand]) + 1e-9)  # noqa: E731
            blend = z(ucb) + 0.5 * z(score)
            chosen = sorted(cand, key=lambda i: -blend[i])[:quota]
        picks += [(i, fid) for i in chosen]
    seen, out = set(), []
    for i, fid in picks:
        if i in seen:
            continue
        seen.add(i)
        s = w.names[i]
        out.append({**w.card(s), "family": fid,
                    "surrogate": None if np.isnan(mean[i]) else round(float(mean[i]), 2),
                    "surrogate_spread": None if np.isnan(spread[i]) else round(float(spread[i]), 2),
                    "cost_t1_cpu_s": cost1(i)})
    return out[: n + 8]
