"""Score runs against arena ground truth. Post-hoc only; agents never import this.

Per run, along the budget axis (every evaluation is a step):
  best_true_found     max TRUE uptake among structures the arm has measured (tier >= 1)
  recommended         the structure the arm would name as its winner at that point: its best
                      own estimate (SIEVE: its tier-2-confirmed pick, see recommendation())
  recommended_true    the TRUE uptake of that recommendation, and its true rank
  decile_recall       share of the true top 10% the arm has measured at all
Across seeds:
  reliability_found   share of seeds that measured a true-best structure
  reliability_picked  share of seeds whose final recommendation IS a true-best structure

"True best" is a set: every structure whose truth is within 2 combined sigma of the top value,
since ground truth itself is a tier-2 measurement with ~3-5% noise.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics as stats
from dataclasses import dataclass, field


@dataclass
class Truth:
    value: dict[str, float]
    err: dict[str, float]
    rank: dict[str, int]
    best_set: set[str]
    top_decile: set[str]

    @classmethod
    def load(cls, arena_db: str) -> "Truth":
        con = sqlite3.connect(f"file:{arena_db}?mode=ro", uri=True)
        rows = con.execute("SELECT structure, uptake, err, rank FROM truth ORDER BY rank").fetchall()
        value = {s: u for s, u, _, _ in rows}
        err = {s: e for s, _, e, _ in rows}
        rank = {s: r for s, _, _, r in rows}
        top_s, top_u, top_e, _ = rows[0]
        best = {s for s, u, e, _ in rows if u >= top_u - 2 * math.hypot(e, top_e)}
        decile = {s for s, _, _, r in rows if r <= max(1, round(0.1 * len(rows)))}
        return cls(value, err, rank, best, decile)


@dataclass
class Point:
    cpu_h: float
    best_true_found: float
    recommended: str | None
    recommended_true: float | None
    recommended_rank: int | None
    decile_recall: float
    found_best: bool
    picked_best: bool


@dataclass
class RunScore:
    run_id: str
    arm: str
    seed: int | None
    budget_h: float
    points: list[Point] = field(default_factory=list)

    def at(self, cpu_h: float) -> Point | None:
        """State of the run once it had spent cpu_h (last point at or before it)."""
        best = None
        for p in self.points:
            if p.cpu_h <= cpu_h + 1e-9:
                best = p
        return best


def recommendation(con: sqlite3.Connection, run_id: str, upto_rowid: int) -> str | None:
    """SIEVE names its winner explicitly (a 'recommend' event); baselines name their best estimate."""
    row = con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' "
                      "AND event_id<=? ORDER BY event_id DESC LIMIT 1",
                      (run_id, upto_rowid)).fetchone()
    return json.loads(row[0]).get("structure") if row else None


def score_run(con: sqlite3.Connection, run_id: str, truth: Truth) -> RunScore:
    run = con.execute("SELECT arm, budget_cpu_s, config_json FROM runs WHERE run_id=?",
                      (run_id,)).fetchone()
    cfg = json.loads(run[2])
    sc = RunScore(run_id, run[0], cfg.get("seed"), run[1] / 3600)
    explicit = con.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND type='recommend'",
                           (run_id,)).fetchone()[0] > 0
    rows = con.execute(
        "SELECT rowid, structure, tier, status, cpu_s, uptake_molkg, uptake_err, finished_at "
        "FROM evaluations WHERE run_id=? AND status!='running' ORDER BY finished_at, rowid",
        (run_id,)).fetchall()
    spent = 0.0
    measured: dict[str, dict[int, list[tuple[float, float]]]] = {}
    for _, s, tier, status, cpu, u, e, fin in rows:
        spent += cpu or 0.0
        if tier == 0 or status != "ok":
            if sc.points:  # spending without new information still moves along the x-axis
                sc.points.append(Point(spent / 3600, *[getattr(sc.points[-1], f) for f in
                                                       ("best_true_found", "recommended",
                                                        "recommended_true", "recommended_rank",
                                                        "decile_recall", "found_best",
                                                        "picked_best")]))
            continue
        measured.setdefault(s, {}).setdefault(tier, []).append((u, e))
        if explicit:
            ev = con.execute("SELECT MAX(event_id) FROM events WHERE run_id=? AND ts<=?",
                             (run_id, fin)).fetchone()[0] or 0
            rec = recommendation(con, run_id, ev)
        else:
            rec = max(measured, key=lambda x: _estimate(measured[x]))
        found = [x for x in measured if x in truth.value]
        sc.points.append(Point(
            cpu_h=spent / 3600,
            best_true_found=max((truth.value[x] for x in found), default=0.0),
            recommended=rec,
            recommended_true=truth.value.get(rec) if rec else None,
            recommended_rank=truth.rank.get(rec) if rec else None,
            decile_recall=len(truth.top_decile & set(found)) / len(truth.top_decile),
            found_best=bool(truth.best_set & set(found)),
            picked_best=rec in truth.best_set if rec else False,
        ))
    final = con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' "
                        "ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
    if final and sc.points:
        rec = json.loads(final[0]).get("structure")
        p = sc.points[-1]
        sc.points.append(Point(p.cpu_h, p.best_true_found, rec, truth.value.get(rec), truth.rank.get(rec),
                               p.decile_recall, p.found_best, rec in truth.best_set))
    return sc


def _estimate(tiers: dict[int, list[tuple[float, float]]]) -> float:
    vals = tiers[max(tiers)]
    w = [1 / max(e, 1e-6) ** 2 for _, e in vals]
    return sum(wi * u for wi, (u, _) in zip(w, vals)) / sum(w)


def summarize(scores: list[RunScore], truth: Truth, budgets_h: list[float]) -> dict:
    """Per arm and budget: medians/IQR across seeds, plus reliability."""
    out: dict = {"truth": {"n": len(truth.value), "best_set": sorted(truth.best_set),
                           "top_value": max(truth.value.values())}, "arms": {}}
    by_arm: dict[str, list[RunScore]] = {}
    for s in scores:
        by_arm.setdefault(s.arm, []).append(s)
    for arm, runs in by_arm.items():
        out["arms"][arm] = {"n_seeds": len(runs), "budgets": {}}
        for b in budgets_h:
            pts = [r.at(b) for r in runs]
            pts = [p for p in pts if p is not None]
            if not pts:
                continue
            q = lambda xs: {"median": stats.median(xs), "p25": _pct(xs, 25),  # noqa: E731
                            "p75": _pct(xs, 75)}
            ranks = [p.recommended_rank or 10 ** 6 for p in pts]
            out["arms"][arm]["budgets"][f"{b:g}"] = {
                "best_true_found": q([p.best_true_found for p in pts]),
                "recommended_true": q([p.recommended_true or 0.0 for p in pts]),
                "decile_recall": q([p.decile_recall for p in pts]),
                "winner_top1": sum(r <= 1 for r in ranks) / len(pts),
                "winner_top5": sum(r <= 5 for r in ranks) / len(pts),
                "winner_top10": sum(r <= 10 for r in ranks) / len(pts),
                "reliability_found": sum(p.found_best for p in pts) / len(pts),
                "reliability_picked": sum(p.picked_best for p in pts) / len(pts),
                "n": len(pts),
            }
    return out


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
