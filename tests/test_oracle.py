import sqlite3

from sieve.oracle import Oracle, bundled_cif_dir, describe

CIF = bundled_cif_dir() / "IRMOF-1.cif"


def test_timeout_is_a_result_not_an_exception(tmp_path):
    o = Oracle(tmp_path / "t.db", "t", labels_path=None)
    ev = o.simulate(CIF, 1, timeout_s=2.0)
    assert ev.status == "timeout" and ev.uptake_molkg is None
    assert 1.5 < ev.wall_s < 4.0
    con = sqlite3.connect(tmp_path / "t.db")
    assert con.execute("SELECT COUNT(*) FROM timeouts").fetchone()[0] == 1
    assert con.execute("SELECT status FROM evaluations").fetchone()[0] == "timeout"
    assert o.spent() > 0  # killed compute is still debited


def test_bad_cif_is_tier0_error(tmp_path):
    bad = tmp_path / "junk.cif"
    bad.write_text("data_x\n_cell_length_a 10\n")
    o = Oracle(tmp_path / "t.db", "t", labels_path=None)
    d = o.tier0(bad)
    assert not d.parse_ok and d.error


def test_budget_refuses_overspend(tmp_path):
    import pytest
    from sieve.oracle import BudgetExhausted

    o = Oracle(tmp_path / "t.db", "t", budget_cpu_s=5.0, labels_path=None)
    with pytest.raises(BudgetExhausted):
        o.simulate(CIF, 1)


def test_supercell_rule():
    d = describe(bundled_cif_dir() / "MgMOF-74-VASP.cif")
    assert d.unit_cells[2] >= 4  # c = 6.9 A needs 4 cells for a 12.8 A cutoff
