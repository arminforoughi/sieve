import json
import random
import sqlite3
from dataclasses import asdict

from sieve.arena import TRUTH_SCHEMA, arena_structures
from sieve.baselines import run_arm
from sieve.evaluate import Truth, score_run, summarize
from sieve.oracle import CostModel, NoiseModel, PROJECT_ROOT


def _fake_truth(path, names):
    con = sqlite3.connect(path)
    con.executescript(TRUTH_SCHEMA)
    rng = random.Random(0)
    vals = sorted(((rng.uniform(0.2, 5.0), n) for n in names), reverse=True)
    for i, (u, n) in enumerate(vals):
        con.execute("INSERT INTO truth VALUES (?,?,?,?,?)", (n, u, 0.04 * u, 1, i + 1))
    con.commit()
    return vals


def test_replay_arms_run_and_score(tmp_path):
    if not (PROJECT_ROOT / "data" / "pool.jsonl").exists():
        import pytest
        pytest.skip("needs data/pool.jsonl (scripts/build_pool.py)")
    names = arena_structures()[:40]
    truth_db = tmp_path / "truth.db"
    _fake_truth(truth_db, names)
    db = str(tmp_path / "replay.db")
    replay = {"truth_db": str(truth_db), "seed": 3, "noise": asdict(NoiseModel())}
    for arm in ("random", "descriptor", "uniform"):
        run_arm(arm, db, 1200.0, n_workers=1, seed=3, run_id=f"{arm}-r", cost_model=CostModel(),
                candidates=names, replay=replay)
    con = sqlite3.connect(db)
    modes = {r[0] for r in con.execute("SELECT DISTINCT mode FROM evaluations WHERE tier>0")}
    assert modes and all(m.startswith("replay-") for m in modes)
    for arm in ("random", "descriptor", "uniform"):
        spent = con.execute("SELECT SUM(cpu_s) FROM evaluations WHERE run_id=?", (f"{arm}-r",)).fetchone()[0]
        assert 0 < spent <= 1200.0 * 1.3
    truth = Truth.load(str(truth_db))
    scores = [score_run(con, f"{a}-r", truth) for a in ("random", "descriptor", "uniform")]
    assert all(s.points for s in scores)
    summ = summarize(scores, truth, [0.1, 0.33])
    assert set(summ["arms"]) == {"random", "descriptor", "uniform"}
    json.dumps(summ)  # serializable for the dashboard
