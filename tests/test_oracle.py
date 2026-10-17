import sqlite3

import pytest

from sieve import forcefield
from sieve.oracle import BudgetExhausted, Oracle, bundled_cif_dir, describe

CIF = bundled_cif_dir() / "IRMOF-1.cif"


@pytest.fixture
def oracle(tmp_path):
    return Oracle(tmp_path / "t.db", "t", labels_path=None, prepared_dir=tmp_path)


def test_timeout_is_a_result_not_an_exception(oracle, tmp_path):
    desc = oracle.tier0(CIF)
    ev = oracle.simulate(desc, 1, mode="direct", timeout_s=2.0)
    assert ev.status == "timeout" and ev.uptake_molkg is None
    assert 1.5 < ev.cpu_s < 4.0  # CPU-time limit, enforced by the kernel
    assert ev.result["phases"][-1]["killed_by"] == "cpu_limit"
    con = sqlite3.connect(tmp_path / "t.db")
    assert con.execute("SELECT phase FROM timeouts").fetchone()[0] == "mc_direct"
    assert con.execute("SELECT status FROM evaluations WHERE tier=1").fetchone()[0] == "timeout"
    assert oracle.spent() > 0  # killed compute is still debited


def test_bad_cif_is_tier0_error(oracle, tmp_path):
    bad = tmp_path / "junk.cif"
    bad.write_text("data_x\n_cell_length_a 10\n")
    d = oracle.tier0(bad)
    assert not d.parse_ok and d.error


def test_budget_refuses_overspend(tmp_path):
    o = Oracle(tmp_path / "t.db", "t", budget_cpu_s=3.0, labels_path=None, prepared_dir=tmp_path)
    desc = o.tier0(CIF)
    with pytest.raises(BudgetExhausted):
        o.simulate(desc, 1, mode="direct")


def test_supercell_rule(tmp_path):
    d = describe(bundled_cif_dir() / "MgMOF-74-VASP.cif", tmp_path)
    assert d.unit_cells[2] >= 4  # c = 6.9 A needs 4 cells for a 12.8 A cutoff


def test_prepared_cif_uses_bare_element_labels(tmp_path):
    d = describe(CIF, tmp_path)
    rows = [l.split() for l in open(d.prepared_path) if l[:1].isupper() and len(l.split()) == 5]
    assert rows and all(r[0] == r[1] and r[0] in forcefield.uff_lj() for r in rows)
    assert set(d.elements) == {"C", "H", "O", "Zn"}


def test_forcefield_has_no_wildcards(tmp_path):
    forcefield.write(tmp_path)
    names = [l.split()[0] for l in open(tmp_path / "force_field_mixing_rules.def")
             if "lennard-jones" in l]
    assert not any(n.endswith("_") for n in names)
    eps, sig = forcefield.uff_lj()["Zn"]
    assert abs(eps - 62.40) < 0.05 and abs(sig - 2.4616) < 1e-3  # UFF Zn3+2
