"""The checks a finalist must pass before it can be recommended, and the phases they define.

  disorder re-measure   CoRE "clean" CIFs keep every alternative position of a disordered site,
                        so a disordered structure (sieve.disorder: atoms closer than any bond)
                        carries doubled Lennard-Jones centres exactly where CO2 binds. Before such
                        a structure can be recommended, it is re-measured at tier 2 on its resolved
                        CIF (<stem>_dres_clean: one atom of every overlapping pair dropped). The
                        recommendation then uses the resolved value; a drop of more than
                        ARTIFACT_DROP is a `disorder_artifact` and vetoes the original.
                        Run config "disorder_check" (on for new real runs; off for replay seeds,
                        which have no ground truth for resolved structures, and for older runs).
  capture check         objective "capture" (sieve/capture.py): a tier-3 capture check on a
                        tier-2-confirmed (and disorder-cleared) structure; the winner is the
                        tier-3-measured structure with the highest APS.

Phases (`phase()`): explore (tier 1) -> confirm (tier 2) -> capture (tier 3, capture objective
only), plus the final funnel the orchestrator runs when the search loop ends. Each phase change
is a `phase` event for the dashboard. Everything here is code: the rules hold when every model
call falls back.
"""

from __future__ import annotations

from functools import lru_cache

from sieve import capture, pool

ARTIFACT_DROP = 0.15     # resolved value more than 15% below the original: an artifact of the doubled atoms
CAPTURE_EXPLORE_UNTIL = 0.60
CAPTURE_FROM = 0.80
FINALISTS = 3

OBJECTIVE_NOTE = (
    "OBJECTIVE: post-combustion CO2 capture from dry flue gas, not 1-bar uptake. The winner is ranked by the "
    "adsorbent performance score APS = S(CO2/N2) x dN (mol/kg; Chung et al., Sci. Adv. 2016), measured by a tier-3 "
    "capture check: binary GCMC of 15:85 CO2:N2 at 1 bar and 298 K (selectivity S and the CO2 loading) plus pure CO2 "
    "at 0.01 bar (vacuum-swing desorption; dN = mixture CO2 loading - N(0.01 bar)). Tiers 1 and 2 (pure CO2, 1 bar) "
    "are the screen that finds candidates; only tier-2-confirmed structures get a tier-3 check, and only "
    "tier-3-measured structures can be recommended. High 1-bar uptake does not guarantee a high APS: weakly "
    "binding pores are not selective, very strongly binding ones do not desorb.")
ZEOLITE_NOTE = (
    "The pool mixes CoRE MOFs and IZA zeolites (material_class). The zeolites are pure-silica SiO2 frameworks: no "
    "metals, no cations, no open metal sites; their pore descriptors were computed by SIEVE on the same geometric "
    "scale as CoRE's. With no charges and no cations this model treats zeolites fairly only GEOMETRICALLY: real "
    "cation-exchanged zeolites (13X, 5A) bind CO2 electrostatically, which nothing here captures.")
DISORDER_NOTE = (
    "A structure flagged disordered=true has atoms closer than any chemical bond (clash_atom_fraction = share of its "
    "atoms involved): the cleaned CIF kept every alternative position of a disordered site. The simulation treats "
    "the overlapped atoms as real, which inflates binding exactly at the overlap sites, so its uptake may be an "
    "artifact. A disordered structure cannot be recommended until it is re-measured at tier 2 on its resolved CIF.")


def objective(ctx) -> str:
    return getattr(ctx, "objective", "uptake")


def notes(ctx, disorder: bool = False) -> str:
    out = []
    if objective(ctx) == "capture":
        out.append(OBJECTIVE_NOTE)
    if getattr(ctx, "mixed", False):
        out.append(ZEOLITE_NOTE)
    if disorder and getattr(ctx, "disorder_check", False):
        out.append(DISORDER_NOTE)
    return " ".join(out)


# --------------------------------------------------------------------------------- disorder

@lru_cache(maxsize=1)
def _scan_table() -> dict:
    try:
        from sieve import disorder
        return disorder.load()
    except Exception:  # noqa: BLE001
        return {}


_lazy: dict[str, dict] = {}


def disorder_info(s: str, lazy: bool = True) -> dict:
    """{disordered, clash_atom_fraction} for a structure: the pool-wide scan, else scanned now."""
    if s in _lazy:
        return _lazy[s]
    r = _scan_table().get(s)
    if r is None and lazy:
        try:
            from sieve import disorder
            r = disorder.scan(pool.cif_path(s))
        except Exception:  # noqa: BLE001
            r = None
        _lazy[s] = r or {}
    r = r or {}
    return {"disordered": bool(r.get("disordered")), "clash_atom_fraction": r.get("clash_atom_fraction")} \
        if r.get("ok", True) and r else {"disordered": None, "clash_atom_fraction": None}


def is_disordered(s: str) -> bool:
    return bool(disorder_info(s).get("disordered"))


def disorder_verdict(ctx, s: str, est: dict) -> dict:
    """status: clean | unchecked (disordered, no resolved tier-2 yet) | passed | artifact."""
    if not getattr(ctx, "disorder_check", False) or not is_disordered(s):
        return {"status": "clean"}
    r = est.get(pool.resolved_name(s))
    if r is None or r.tier < 2:
        return {"status": "unchecked"}
    orig = est[s].uptake if s in est else None
    drop = None if not orig else (orig - r.uptake) / orig
    return {"status": "artifact" if drop is not None and drop > ARTIFACT_DROP else "passed",
            "resolved": r.uptake, "resolved_err": r.err, "original": orig, "drop": drop}


def effective(ctx, s: str, est: dict) -> tuple[float, float] | None:
    """(value, err) the recommendation uses for s's 1-bar uptake: the resolved tier-2 value for a
    disordered structure, else its own estimate."""
    v = disorder_verdict(ctx, s, est)
    if v["status"] in ("passed", "artifact"):
        return v["resolved"], v["resolved_err"]
    e = est.get(s)
    return (e.uptake, e.err) if e else None


def capture_target(ctx, s: str) -> str:
    """The structure a capture check runs on: the resolved CIF for a disordered one."""
    if getattr(ctx, "disorder_check", False) and is_disordered(s):
        return pool.resolved_name(s)
    return s


def capture_of(ctx, s: str, cap: dict) -> dict | None:
    return cap.get(capture_target(ctx, s)) or (cap.get(s) if capture_target(ctx, s) == s else None)


# ------------------------------------------------------------------------------------ phases

def phase(ctx) -> str:
    from sieve.agents.base import EXPLORE_UNTIL, budget

    b = budget(ctx)
    frac = b["spent_cpu_s"] / max(1.0, b["total_cpu_s"])
    if objective(ctx) == "capture":
        return "explore" if frac < CAPTURE_EXPLORE_UNTIL else "confirm" if frac < CAPTURE_FROM else "capture"
    return "explore" if frac < EXPLORE_UNTIL else "confirm"


def note_phase(ctx, name: str | None = None) -> None:
    name = name or phase(ctx)
    if getattr(ctx, "last_phase", None) != name:
        ctx.last_phase = name
        text = {"explore": "phase 1: explore at tier 1", "confirm": "phase 2: confirm leaders at tier 2",
                "capture": "phase 3: capture checks (tier 3) on tier-2-confirmed leaders",
                "final": "final checks: confirmation, disorder re-measure, capture check, then the recommendation"}
        ctx.st.event(ctx.run_id, "phase", {"phase": name, "round": ctx.round, "objective": objective(ctx),
                                           "text": text.get(name, name)}, agent_id="orchestrator")


# ---------------------------------------------------------------------------------- the funnel

def confirmed(ctx, est: dict, n: int = FINALISTS) -> list[str]:
    """Top tier-2-confirmed pool structures by their effective 1-bar value, artifacts excluded."""
    rows = []
    for s, e in est.items():
        if e.tier < 2 or pool.is_resolved(s):
            continue
        if disorder_verdict(ctx, s, est)["status"] == "artifact":
            continue
        v = effective(ctx, s, est)
        rows.append((v[0] if v else e.uptake, s))
    return [s for _, s in sorted(rows, reverse=True)[:n]]


def pending(ctx, est: dict, cap: dict, n: int = FINALISTS) -> list[dict]:
    """Jobs the top confirmed finalists still need before one can be recommended: a disorder
    re-measure (tier 2 on the resolved CIF), then, under the capture objective, a capture check."""
    jobs = []
    for s in confirmed(ctx, est, n):
        v = disorder_verdict(ctx, s, est)
        if v["status"] == "unchecked":
            jobs.append({"structure": pool.resolved_name(s), "tier": 2, "of": s,
                         "why": "disorder check: re-measure on the resolved CIF before it can be recommended"})
            continue
        if objective(ctx) == "capture" and capture_of(ctx, s, cap) is None:
            jobs.append({"structure": capture_target(ctx, s), "tier": 3, "of": s,
                         "why": "capture check: APS before it can be recommended"})
    return jobs


def job_cost(ctx, job: dict) -> float:
    """CPU-s estimate for a funnel job, from the original's descriptors (a resolved variant has
    a few atoms fewer: a slight overestimate)."""
    from sieve.agents.world import costs

    s = job.get("of") or pool.original_name(job["structure"])
    if ctx.world is None or s not in ctx.world.idx:
        return 0.0
    if job["tier"] == 3:
        d = ctx.world.desc[s]
        hint = ctx.oracle.known_uptake(s)
        return float(capture.plan(ctx.oracle, d, ctx.cost_model, hint).est_cpu_s)
    return float(costs(ctx.oracle, ctx.world, s, ctx.cost_model)[job["tier"]])


def reserve(ctx, est: dict, cap: dict) -> float:
    """Budget the final funnel will need: pending checks for today's finalists, plus a capture
    check for every finalist not yet checked (capture objective)."""
    from sieve.agents.base import leaders

    total = sum(job_cost(ctx, j) for j in pending(ctx, est, cap))
    if objective(ctx) == "capture":  # finalists not yet confirmed still need a capture check later
        have = {j["of"] for j in pending(ctx, est, cap)} | set(confirmed(ctx, est))
        for x in leaders(ctx, FINALISTS):
            if x["structure"] not in have and capture_of(ctx, x["structure"], cap) is None:
                total += job_cost(ctx, {"structure": x["structure"], "tier": 3})
    return total


def recommendable(ctx, est: dict, cap: dict) -> list[dict]:
    """Every structure the rules allow as the winner, best first.

    uptake objective: tier-2 confirmed, disorder check passed (or not disordered), ranked by the
    effective (resolved) tier-2 value. capture objective: additionally a tier-3 capture check,
    ranked by APS."""
    out = []
    for s, e in est.items():
        if e.tier < 2 or pool.is_resolved(s):
            continue
        v = disorder_verdict(ctx, s, est)
        if v["status"] in ("unchecked", "artifact"):
            continue
        val, err = effective(ctx, s, est)
        row = {"structure": s, "uptake_t2": round(val, 4), "err": round(err, 4), "tier": 2,
               "disorder": v["status"], **({"uptake_t2_raw": round(e.uptake, 4)} if v["status"] == "passed" else {})}
        if objective(ctx) == "capture":
            c = capture_of(ctx, s, cap)
            if c is None:
                continue
            row.update(capture.brief(c) or {})
            row["capture_on"] = capture_target(ctx, s)
        out.append(row)
    key = (lambda r: r["aps"]) if objective(ctx) == "capture" else (lambda r: r["uptake_t2"])
    return sorted(out, key=key, reverse=True)


def audit(ctx, est: dict, cap: dict) -> list[dict]:
    """The critic's rule-based part (runs whether or not the model answered): flag disorder
    artifacts (veto the original), and require the finalist checks still missing. Returns the
    funnel jobs the critic forces."""
    for s, e in est.items():
        if e.tier < 2 or pool.is_resolved(s) or s in ctx.vetoed:
            continue
        v = disorder_verdict(ctx, s, est)
        if v["status"] == "artifact":
            reason = (f"disorder artifact: resolved CIF measures {v['resolved']:.3f} vs {v['original']:.3f} mol/kg "
                      f"({100 * v['drop']:.0f}% lower)")
            ctx.vetoed[s] = reason
            ctx.st.event(ctx.run_id, "disorder_artifact", {"structure": s, "original": v["original"],
                                                           "resolved": v["resolved"], "drop": v["drop"],
                                                           "text": f"{s.replace('_clean', '')}: {reason}"},
                         agent_id="critic")
            ctx.st.event(ctx.run_id, "veto", {"structure": s, "reason": reason,
                                              "text": f"veto — {s.replace('_clean', '')}: {reason}"}, agent_id="critic")
        elif v["status"] == "passed" and s not in ctx.required:
            ctx.required[s] = "passed"
            ctx.st.event(ctx.run_id, "disorder_passed", {"structure": s, "original": v["original"],
                                                         "resolved": v["resolved"], "drop": v["drop"],
                                                         "text": f"{s.replace('_clean', '')}: disorder check passed "
                                                                 f"(resolved {v['resolved']:.3f} mol/kg)"},
                         agent_id="critic")
    jobs = pending(ctx, est, cap)
    for j in jobs:
        key = (j["of"], j["tier"])
        if ctx.required.get(key):
            continue
        ctx.required[key] = True
        kind = "capture_required" if j["tier"] == 3 else "disorder_check_required"
        what = "a tier-3 capture check" if j["tier"] == 3 else "a tier-2 re-measure on its resolved CIF"
        ctx.st.event(ctx.run_id, kind, {"structure": j["of"], "job": j["structure"], "tier": j["tier"],
                                        "text": f"{j['of'].replace('_clean', '')} needs {what} before it can be recommended"},
                     agent_id="critic")
    return jobs
