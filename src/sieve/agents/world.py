"""What the agents can see and the tools they can use. Nothing here touches ground truth.

  World       the candidate pool with tier-0 descriptors (LCD, PLD, void fraction, surface area,
              open metal sites, metals), loaded from this run's own tier-0 rows
  measured    this run's own GCMC estimates (highest tier, inverse-variance mean)
  surrogate   k-nearest-neighbour prediction of uptake for unmeasured structures, fitted only on
              this run's measurements -- the 'learned' signal the allocator weighs
  scout score the scout's rule (a weighted combination of descriptors) applied to every candidate
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from sieve.oracle import CostModel, Descriptors, Oracle
from sieve.store import Store, estimates_from_evaluations

FEATURES = ("LCD", "PLD", "AV_VF", "ASA_m2_g")


@dataclass
class World:
    names: list[str]
    desc: dict[str, Descriptors]
    X: np.ndarray                    # standardized features, row per name
    oms: np.ndarray                  # 1 if open metal sites
    metals: list[list[str]]
    idx: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, st: Store, run_id: str) -> "World":
        desc = {}
        for s, rj in st.con.execute("SELECT structure, result_json FROM evaluations WHERE run_id=? AND "
                                    "tier=0 AND status='ok'", (run_id,)):
            d = Descriptors.from_dict(json.loads(rj))
            if d.parse_ok and all((d.core or {}).get(f) is not None for f in FEATURES):
                desc[s] = d
        names = sorted(desc)
        raw = np.array([[desc[n].core[f] for f in FEATURES] for n in names], dtype=float)
        raw[:, 3] = np.log1p(raw[:, 3])                       # surface area: log scale
        mu, sd = raw.mean(0), raw.std(0) + 1e-9
        X = (raw - mu) / sd
        oms = np.array([1.0 if (desc[n].core.get("Has_OMS") or "").lower().startswith("y") else 0.0
                        for n in names])
        w = cls(names, desc, X, oms, [desc[n].metals for n in names])
        w.idx = {n: i for i, n in enumerate(names)}
        return w

    # -- tools ---------------------------------------------------------------------------

    def scout_scores(self, rule: dict) -> np.ndarray:
        """Apply the scout's rule. Keys (all optional): lcd_target, lcd_width, w_lcd, w_void,
        w_area, w_oms, w_pld, metal_bonus {element: weight}."""
        lcd = np.array([self.desc[n].core["LCD"] for n in self.names])
        target, width = float(rule.get("lcd_target", 6.0)), max(0.5, float(rule.get("lcd_width", 2.5)))
        s = float(rule.get("w_lcd", 1.0)) * np.exp(-((lcd - target) ** 2) / (2 * width ** 2))
        s += float(rule.get("w_void", 0.5)) * self.X[:, 2]
        s += float(rule.get("w_area", 0.0)) * self.X[:, 3]
        s += float(rule.get("w_pld", 0.0)) * self.X[:, 1]
        s += float(rule.get("w_oms", 0.0)) * self.oms
        bonus = rule.get("metal_bonus") or {}
        if bonus:
            s += np.array([max([float(bonus.get(m, 0.0)) for m in ms] or [0.0]) for ms in self.metals])
        return s

    def surrogate(self, measured: dict[str, tuple[float, float]], k: int = 7
                  ) -> tuple[np.ndarray, np.ndarray]:
        """Mean and spread of the k nearest measured neighbours, per candidate."""
        mi = [self.idx[s] for s in measured if s in self.idx]
        if len(mi) < 3:
            return np.full(len(self.names), np.nan), np.full(len(self.names), np.nan)
        y = np.array([measured[self.names[i]][0] for i in mi])
        D = np.linalg.norm(self.X[:, None, :] - self.X[None, mi, :], axis=2)  # (n, m)
        kk = min(k, len(mi))
        nn = np.argpartition(D, kk - 1, axis=1)[:, :kk]
        d = np.take_along_axis(D, nn, axis=1)
        w = 1.0 / (d + 0.15)
        vals = y[nn]
        mean = (w * vals).sum(1) / w.sum(1)
        spread = np.sqrt((w * (vals - mean[:, None]) ** 2).sum(1) / w.sum(1))
        return mean, spread

    def card(self, s: str) -> dict:
        d = self.desc[s]
        c = d.core
        return {"structure": s, "LCD": round(c["LCD"], 2), "PLD": round(c["PLD"], 2),
                "void": round(c["AV_VF"], 3), "area_m2g": round(c["ASA_m2_g"]),
                "open_metal": (c.get("Has_OMS") or "").lower().startswith("y"),
                "metals": d.metals[:3], "atoms_sim": d.n_atoms_sim}


def measured_estimates(st: Store, run_id: str) -> dict:
    return estimates_from_evaluations(st.con, run_id)


def costs(o: Oracle, w: World, s: str, model: CostModel) -> dict:
    d = w.desc[s]
    return {t: round(o.plan(d, t, "auto", model).est_cpu_s) for t in (1, 2)}


def describe_pool(w: World) -> dict:
    lcd = np.array([w.desc[n].core["LCD"] for n in w.names])
    vf = np.array([w.desc[n].core["AV_VF"] for n in w.names])
    metals: dict[str, int] = {}
    for ms in w.metals:
        for m in ms:
            metals[m] = metals.get(m, 0) + 1
    top_metals = sorted(metals.items(), key=lambda kv: -kv[1])[:12]
    q = lambda a: [round(float(np.percentile(a, p)), 2) for p in (10, 50, 90)]  # noqa: E731
    return {"n": len(w.names), "LCD_p10_p50_p90": q(lcd), "void_p10_p50_p90": q(vf),
            "open_metal_fraction": round(float(w.oms.mean()), 3), "common_metals": top_metals}


def jitter_safe(x: float) -> float:
    return 0.0 if (x is None or (isinstance(x, float) and math.isnan(x))) else float(x)
