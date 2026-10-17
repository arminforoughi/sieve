"""Capture properties: selectivity, working capacity, Qst arithmetic and RASPA output parsing."""

import math
from pathlib import Path

import pytest

from sieve import _raspa_job as job
from sieve import forcefield, science

FIX = Path(__file__).parent / "fixtures"


def test_selectivity_formula():
    # x = 2.0 / 0.2 mol/kg in a 15:85 gas -> (2/0.2)/(0.15/0.85) = 56.667
    s = science.selectivity(2.0, 0.1, 0.2, 0.02)
    assert s["value"] == pytest.approx(10 / (0.15 / 0.85))
    assert s["err"] == pytest.approx(s["value"] * math.hypot(0.05, 0.1))
    # equal mole fractions in gas and pore -> no selectivity
    assert science.selectivity(0.15, 0.0, 0.85, 0.01)["value"] == pytest.approx(1.0)
    assert science.selectivity(1.0, 0.1, 0.0, 0.0)["value"] is None


def test_working_capacity_arithmetic():
    wc = science.working_capacity(1.5, 0.3, 0.2, 0.4)
    assert wc["value"] == pytest.approx(1.3) and wc["err"] == pytest.approx(0.5)
    assert (wc["ads"], wc["des"]) == (1.5, 0.2)


def test_qst_sign_convention():
    # <U_gh>_1-<U_h>_0 = -20 kJ/mol at 298 K -> dH = -22.48, Q_st = +22.48 (exothermic adsorption)
    q = science.qst_widom(-20.0, 0.5)
    assert q["value"] == pytest.approx(20.0 + 0.0083144626 * 298.0) and q["err"] == 0.5


def test_derive_props_end_to_end():
    iso = [{"p_bar": 0.05, "uptake": 0.5, "err": 0.05}, {"p_bar": 0.15, "uptake": 1.2, "err": 0.1}]
    pts = {"mixture": {"loadings": {"CO2": {"molkg": 1.0, "err": 0.1}, "N2": {"molkg": 0.1, "err": 0.01}},
                       "enthalpy": {"CO2": {"kjmol": -25.0, "err": 1.0}}},
           "vsa_des": {"uptake_molkg": 0.1, "uptake_err": 0.01, "enthalpy": {"_total": {"kjmol": -24.0, "err": 2.0}}},
           "tsa_des": {"uptake_molkg": 0.2, "uptake_err": 0.02},
           "widom": {"widom": {"CO2": {"kh_molkgpa": 2e-5, "kh_err": 2e-6, "u_kjmol": -22.0, "u_err": 0.3},
                               "N2": {"kh_molkgpa": 1e-6, "kh_err": 1e-7, "u_kjmol": -10.0, "u_err": 0.1}}}}
    p = science.derive_props(iso, pts, {"min_kjmol": -12.0})
    assert p["selectivity"]["value"] == pytest.approx(10 * 0.85 / 0.15)
    assert p["selectivity_0"]["value"] == pytest.approx(20.0)
    assert p["wc"]["vsa"]["value"] == pytest.approx(1.1)
    assert p["wc"]["mixture"]["value"] == pytest.approx(0.9)
    assert p["wc"]["tsa"]["value"] == pytest.approx(1.0)
    assert p["k_h"]["CO2"]["value"] == pytest.approx(2.0)  # mol/kg/Pa -> mol/(kg bar)
    assert p["qst"]["fluct_low"]["value"] == 24.0 and p["qst"]["fluct_mix"]["value"] == 25.0
    assert p["qst"]["widom"]["value"] > 22.0
    assert p["deepest_well_kjmol"] == -12.0
    csv = science.props_csv(p)
    assert csv.startswith("quantity,value,err,unit,definition") and "wc_tsa_298to393K_0.15bar,1," in csv


def test_parse_mixture_enthalpy():
    h = job.parse_enthalpy((FIX / "raspa_mixture_enthalpy.txt").read_text())
    assert h["CO2"] == {"kjmol": -35.65614, "err": 4.723375}
    assert h["N2"] == {"kjmol": -21.55841, "err": 10.049598}
    assert h["_total"]["kjmol"] == -34.05669


def test_parse_pure_enthalpy():
    h = job.parse_enthalpy((FIX / "raspa_pure_enthalpy.txt").read_text())
    assert h == {"_total": {"kjmol": -32.1827, "err": 10.725549}}
    assert job.parse_enthalpy("no such block") == {}


def test_parse_widom():
    w = job.parse_widom((FIX / "raspa_widom.txt").read_text())
    assert w["CO2"]["kh_molkgpa"] == pytest.approx(0.00884124) and w["CO2"]["kh_err"] == pytest.approx(0.00789362)
    assert w["N2"]["kh_molkgpa"] == pytest.approx(8.25457e-05)
    assert w["CO2"]["u_kjmol"] == pytest.approx(-38.2022857679)
    assert w["N2"]["u_err"] == pytest.approx(1.1540265918)


def test_mixture_script_and_forcefield(tmp_path):
    base = {"unit_cells": [1, 1, 1], "forcefield": forcefield.MIX_NAME, "alias": "x", "grid_types": ["C_co2"],
            "spacing": 0.5, "cycles": 10, "init_cycles": 5, "helium_void_fraction": 0.7, "temperature": 298.0,
            "pressure": 1e5, "molecule": "CO2"}
    s = job._script({**base, "kind": "mc_mixture", "components": [{"molecule": "CO2", "mol_fraction": 0.15},
                                                                     {"molecule": "N2", "mol_fraction": 0.85}]})
    assert "Component 1 MoleculeName             N2" in s and "IdentityChangesList      0 1" in s
    assert "MolFraction              0.85" in s and "ChargeMethod                  None" in s
    w = job._script({**base, "kind": "widom", "components": [{"molecule": "CO2"}, {"molecule": "N2"}]})
    assert w.count("WidomProbability") == 2 and "SwapProbability" not in w
    # the screen's single-component script is unchanged
    assert "IdentityChange" not in job._script({**base, "kind": "mc_direct"})
    forcefield.write(tmp_path / "mix", mixture=True)
    forcefield.write(tmp_path / "plain")
    mix = (tmp_path / "mix" / "force_field_mixing_rules.def").read_text()
    assert "N_n2     lennard-jones   36.00000  3.31000" in mix and "N_com    none" in mix
    assert "N_n2" not in (tmp_path / "plain" / "pseudo_atoms.def").read_text()
