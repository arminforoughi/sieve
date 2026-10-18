"""Grid pore descriptors (sieve.porosity): exact synthetic geometry, percolation, one zeolite."""

import math

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure

from sieve import porosity as P
from sieve.oracle import ZEOLITE_DIR, zeolite_cif


def test_simple_cubic_channel_exact():
    # one C atom (r = 1.70) per 10 A cubic cell: the cavity is centred at (1/2, 1/2, 1/2), the
    # channels run along the axes and are narrowest at the face centres
    s = Structure(Lattice.cubic(10.0), ["C"], [[0, 0, 0]])
    d = P.compute(s)
    assert d.LCD == pytest.approx(2 * (5 * math.sqrt(3) - 1.70), abs=0.01)
    pld = 2 * (5 * math.sqrt(2) - 1.70)        # the face centre lies on the grid: no grid error
    assert d.PLD_grid == pytest.approx(pld, abs=0.01)
    assert d.PLD == pytest.approx(pld + P.PLD_BIAS_A, abs=0.01)
    assert d.AV_VF == pytest.approx(1 - 4 / 3 * math.pi * 1.7 ** 3 / 1000, abs=0.01)
    # ASA: the whole probe sphere around the single atom, per gram
    asa = 4 * math.pi * (1.70 + P.ASA_PROBE_A) ** 2 * 1e-20 / (12.011 / 6.02214e23)
    assert d.ASA_m2_g == pytest.approx(asa, rel=0.02)


def test_closed_cages_do_not_percolate():
    # radius 4.4 atoms on a 6 A cubic lattice: the face-centre windows (3*sqrt(2) = 4.24 A from the
    # atoms) are shut, the body-centre cavities (5.20 A) are not: cages without channels
    s = Structure(Lattice.cubic(6.0), ["U"], [[0, 0, 0]])
    d = P.compute(s, radii={"U": 4.4})
    assert d.LCD == pytest.approx(2 * (3 * math.sqrt(3) - 4.4), abs=0.02)
    assert d.PLD == 0.0 and d.AV_VF == 0.0 and d.ASA_m2_g == 0.0


def test_percolation_union_find_offsets():
    m = np.zeros((6, 6, 6), dtype=bool)
    m[:, 2, 2] = True                     # a straight channel along x wraps the cell
    assert P.percolates(m)
    m2 = np.zeros((6, 6, 6), dtype=bool)
    m2[1:5, 2, 2] = True                  # a closed pocket
    assert not P.percolates(m2)
    assert P.accessible(m).sum() == 6 and P.accessible(m2).sum() == 0


@pytest.mark.skipif(not (ZEOLITE_DIR / "iza" / "LTA.cif").exists(), reason="IZA CIFs not downloaded")
def test_lta_alpha_cage_and_window():
    # IZA's own convention (O = 1.35 A): alpha cage ~11 A, 8-ring window ~4.1 A
    iza = P.compute_cif(zeolite_cif("IZA-LTA"), radii={"O": 1.35})
    assert iza.LCD == pytest.approx(11.0, abs=0.2) and iza.PLD == pytest.approx(4.1, abs=0.2)
    # the pool's CCDC radii (O = 1.52, Si = 2.10), on CoRE's scale: both a little smaller
    ccdc = P.compute_cif(zeolite_cif("IZA-LTA"))
    assert 10.3 < ccdc.LCD < iza.LCD and 3.6 < ccdc.PLD < iza.PLD
    assert ccdc.density == pytest.approx(1.414, abs=0.01)
