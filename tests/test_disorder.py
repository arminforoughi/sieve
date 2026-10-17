from pathlib import Path

from pymatgen.core import Lattice, Structure

from sieve import disorder


def _write(tmp: Path, species, coords) -> Path:
    s = Structure(Lattice.cubic(10.0), species, coords)
    p = tmp / "toy_clean.cif"
    s.to(filename=str(p), fmt="cif")
    return p


def test_clean_structure_has_no_clashes(tmp_path):
    p = _write(tmp_path, ["C", "O"], [[0, 0, 0], [0.12, 0, 0]])  # 1.2 A: a real bond
    r = disorder.scan(p)
    assert r["ok"] and not r["disordered"]


def test_overlapping_pair_found_and_resolved(tmp_path):
    # an N/O site recorded twice, 0.45 A apart, next to a real carbon
    p = _write(tmp_path, ["N", "O", "C"], [[0, 0, 0], [0.045, 0, 0], [0.5, 0.5, 0.5]])
    r = disorder.scan(p)
    assert r["disordered"] and r["kinds"] == {"N-O": 1}
    path, summ = disorder.resolve(p, out_dir=tmp_path)
    assert summ["dropped"] == 1 and summ["clashes_left"] == 0
    kept = {site.specie.symbol for site in Structure.from_file(str(path))}
    assert kept == {"O", "C"}  # O has the smaller UFF epsilon, so N is the copy dropped


def test_periodic_clash_across_boundary(tmp_path):
    p = _write(tmp_path, ["C", "C"], [[0.001, 0, 0], [0.995, 0, 0]])  # 0.06 A through the boundary
    assert disorder.scan(p)["n_clashes"] == 1
