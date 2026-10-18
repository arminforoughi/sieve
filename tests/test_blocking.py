"""Pore blocking: periodic percolation, sphere covering, RASPA .block files, and a RASPA smoke run."""

import math

import numpy as np
import pytest

from sieve import blocking as B

HIGH, LOW = 1e5, -1000.0  # K: wall / attractive


def _grid(cell: np.ndarray, field, spacing: float = 0.5) -> dict:
    """A read_grid()-shaped dict: RASPA's axis-aligned Cartesian box around the cell (grids.c),
    energy from field(frac) evaluated periodically at every box node."""
    size = np.abs(cell).sum(axis=0)
    shift = np.where(cell < 0, cell, 0).sum(axis=0)
    n = (size / spacing).astype(int)
    ax = [shift[i] + np.arange(n[i] + 1) * size[i] / n[i] for i in range(3)]
    r = np.stack(np.meshgrid(*ax, indexing="ij"), axis=-1)
    frac = np.linalg.solve(cell.T, r.reshape(-1, 3).T).T % 1.0
    return {"spacing": spacing, "n": n, "size": size, "shift": shift,
            "energy_K": field(frac).reshape(tuple(n + 1)).astype(np.float32)}


def _dist(frac, c, cell):
    d = frac - np.asarray(c)
    d -= np.rint(d)
    return np.linalg.norm(d @ cell, axis=1)


def _channel_and_cage(cell, chan_xy, cage, r_chan=1.5, r_cage=1.2):
    """A channel along c through fractional (x, y) = chan_xy, and a closed spherical cage."""
    def field(frac):
        e = np.full(len(frac), HIGH)
        line = np.c_[frac[:, :2], np.zeros(len(frac))]
        e[_dist(line, [*chan_xy, 0.0], cell * np.array([[1], [1], [0]])) < r_chan] = LOW
        e[_dist(frac, cage, cell) < r_cage] = LOW
        return e
    return field


def test_channel_percolates_cage_is_one_pocket():
    cell = np.diag([10.0, 10.0, 10.0])
    s = B.analyse(_grid(cell, _channel_and_cage(cell, (0.5, 0.5), (0.15, 0.15, 0.5))), cell)
    assert s["n_pockets"] == 1 and s["enabled"] and s["clean_cover"]
    assert s["channel_dims"] == [1]
    p = s["pockets"][0]
    assert np.allclose(p["centre_frac"], [0.15, 0.15, 0.5], atol=0.03)
    sph = s["spheres"]
    assert len(sph) == 1 and 1.2 <= sph[0]["r"] < 2.5
    # the sphere covers every cage point and no channel point
    pts = np.indices((20, 20, 20)).reshape(3, -1).T / 20
    in_s = _dist(pts, sph[0]["frac"], cell) < sph[0]["r"]
    assert in_s[_dist(pts, (0.15, 0.15, 0.5), cell) < 1.2].all()
    chan = _dist(np.c_[pts[:, :2], np.zeros(len(pts))], (0.5, 0.5, 0), cell * np.array([[1], [1], [0]])) < 1.5
    assert not in_s[chan].any()


def test_no_pockets_means_no_file(tmp_path):
    cell = np.diag([10.0, 10.0, 10.0])
    def field(frac):
        e = np.full(len(frac), HIGH)
        e[_dist(np.c_[frac[:, :2], np.zeros(len(frac))], (0.5, 0.5, 0), cell * np.array([[1], [1], [0]])) < 1.5] = LOW
        return e
    s = B.analyse(_grid(cell, field), cell)
    assert s["n_pockets"] == 0 and not s["enabled"] and s["spheres"] == []
    assert B.write("X", s, "ff", root=tmp_path) is None
    summary, blk = B.cached("X", "ff", root=tmp_path)
    assert summary["n_pockets"] == 0 and blk is None


def test_shift_vectors_across_the_boundary():
    acc = np.zeros((10, 10, 10), bool)
    acc[:, 0, 0] = True           # a full line along x: closes on its own image (+1, 0, 0)
    acc[0:2, 5, 5] = True         # a blob straddling the x boundary: one component, no net shift
    acc[8:10, 5, 5] = True
    acc[2:8, 8, 8] = True         # a segment that never reaches a face
    lab, meta = B.periodic_components(acc)
    comps = meta["components"]
    assert len(comps) == 3
    k_line, k_blob, k_seg = lab[0, 0, 0], lab[0, 5, 5], lab[3, 8, 8]
    assert lab[9, 5, 5] == k_blob  # merged across the face
    assert comps[k_line]["percolating"] and comps[k_line]["dims"] == 1
    assert not comps[k_blob]["percolating"] and not comps[k_seg]["percolating"]
    # a loop that wraps in y AND z through the boundary (a staircase): 1 direction, two axes
    acc = np.zeros((6, 6, 6), bool)
    for i in range(6):
        acc[0, i, i] = True
        acc[0, i, (i + 1) % 6] = True
    lab, meta = B.periodic_components(acc)
    (d,) = meta["components"].values()
    assert d["percolating"] and d["dims"] == 1
    # a 2D sheet percolates in two directions
    acc = np.zeros((6, 6, 6), bool)
    acc[:, :, 3] = True
    (d,) = B.periodic_components(acc)[1]["components"].values()
    assert d["dims"] == 2


def test_pocket_straddling_the_corner_is_unwrapped():
    cell = np.diag([10.0, 10.0, 10.0])
    s = B.analyse(_grid(cell, _channel_and_cage(cell, (0.5, 0.5), (0.0, 0.0, 0.0))), cell)
    assert s["n_pockets"] == 1
    c = np.array(s["spheres"][0]["frac"])
    assert np.all(np.minimum(c, 1 - c) < 0.03)  # centred on the corner, not at the cell middle
    assert s["spheres"][0]["r"] < 2.5


def test_non_orthogonal_hexagonal_cell():
    cell = B.raspa_cell(12.0, 12.0, 8.0, 90.0, 90.0, 120.0)
    assert cell[1, 0] < 0  # b leans back: RASPA's box is shifted, wider than the cell
    g = _grid(cell, _channel_and_cage(cell, (0.0, 0.0), (1 / 3, 2 / 3, 0.5)))
    assert g["shift"][0] < 0
    s = B.analyse(g, cell)
    assert not s["orthogonal"] and s["n_pockets"] == 1 and s["clean_cover"]
    assert np.allclose(s["pockets"][0]["centre_frac"], [1 / 3, 2 / 3, 0.5], atol=0.03)
    sph = s["spheres"][0]
    assert np.allclose(sph["cart"], np.array(sph["frac"]) @ cell, atol=1e-2)
    assert 1.2 <= sph["r"] < 2.5
    # the fractional resampling reproduces the field
    e = B.frac_energy(g, cell)
    assert e.shape == tuple((np.linalg.norm(cell, axis=1) / 0.5).astype(int))


def test_raspa_cell_matches_lattice_parameters():
    cell = B.raspa_cell(10.0, 11.0, 12.0, 80.0, 95.0, 110.0)
    a, b, c = cell
    ang = lambda u, v: math.degrees(math.acos(np.dot(u, v) / np.linalg.norm(u) / np.linalg.norm(v)))  # noqa: E731
    assert np.allclose(np.linalg.norm(cell, axis=1), [10, 11, 12])
    assert abs(ang(b, c) - 80) < 1e-9 and abs(ang(a, c) - 95) < 1e-9 and abs(ang(a, b) - 110) < 1e-9
    assert a[1] == a[2] == b[2] == 0.0


def test_block_file_format(tmp_path):
    sph = [{"frac": [0.0, 0.5, 0.25], "r": 3.14159}, {"frac": [0.123456789, 0.9, 1e-7], "r": 1.0}]
    txt = B.format_block(sph)
    assert txt == "2\n0.000000 0.500000 0.250000 3.1416\n0.123457 0.900000 0.000000 1.0000\n"
    back = B.parse_block(txt)
    assert len(back) == 2 and back[0]["r"] == pytest.approx(3.1416) and back[1]["frac"][0] == pytest.approx(0.123457)
    s = {"spheres": sph, "n_pockets": 2, "enabled": True}
    p = B.write("Y", s, "ff", root=tmp_path)
    assert p.name == "Y.block" and p.read_text() == txt
    assert B.cached("Y", "ff", root=tmp_path)[1] == p


def test_blocked_mask_min_image():
    cell = np.diag([10.0, 10.0, 10.0])
    m = B.blocked_mask(np.array([[9.8, 0.1, 0.1], [5.0, 5.0, 5.0]]), cell, [{"frac": [0, 0, 0], "r": 1.0}])
    assert m.tolist() == [True, False]


def test_screening_default_is_unblocked(tmp_path):
    from sieve.oracle import Oracle
    from sieve.store import Store

    st = Store(str(tmp_path / "r.db"))
    rid = st.create_run("random", 10.0, {"seed": 0})
    rid2 = st.create_run("random", 10.0, {"seed": 0, "block_pockets": True})
    assert Oracle(tmp_path / "r.db", rid, labels_path=None).block_pockets is False
    assert Oracle(tmp_path / "r.db", rid2, labels_path=None).block_pockets is True
    assert Oracle(tmp_path / "x.db", "nope", labels_path=None).block_pockets is False


def test_job_script_unchanged_without_block_file():
    from sieve._raspa_job import _script

    job = {"alias": "X", "unit_cells": [1, 1, 1], "forcefield": "F", "grid_types": ["O_co2"], "spacing": 0.5,
           "molecule": "CO2", "cycles": 10, "init_cycles": 5, "helium_void_fraction": 0.75,
           "temperature": 298, "pressure": 1e5}
    for kind in ("mc_direct", "mc_grid", "movie"):
        assert "BlockPockets" not in _script({**job, "kind": kind})
        blk = _script({**job, "kind": kind, "block_file": "/x.block"})
        assert "BlockPockets             yes" in blk and "BlockPocketsFileName     X_sieve" in blk
    comps = {"components": [{"molecule": "CO2", "mol_fraction": 0.15}, {"molecule": "N2", "mol_fraction": 0.85}]}
    for kind in ("mc_mixture", "widom"):
        assert _script({**job, **comps, "kind": kind, "block_file": "/x"}).count("BlockPockets             yes") == 2
    assert "BlockPockets" not in _script({**job, "kind": "makegrid", "block_file": "/x"})


# ------------------------------------------------------------------------------- RASPA smoke run

def test_raspa_lta_blocking_lowers_loading(tmp_path):
    from sieve import forcefield, science
    from sieve.oracle import ZEOLITE_DIR, Oracle, describe

    if not (ZEOLITE_DIR / "iza" / "LTA.cif").exists():
        pytest.skip("IZA CIFs not downloaded")
    s, blk = science.pore_blocking("IZA-LTA")
    assert s["n_pockets"] == 1 and blk is not None  # the sodalite cage
    c = np.array(s["spheres"][0]["frac"])
    assert np.all(np.minimum(c, 1 - c) < 0.02)       # at the origin of Pm-3m LTA
    desc = describe(science._cif("IZA-LTA"), tmp_path)
    o = Oracle(tmp_path / "t.db", "t", budget_cpu_s=1e9, labels_path=None, prepared_dir=tmp_path)
    job = {"alias": desc.alias, "cif_path": desc.prepared_path, "elements": desc.elements,
           "unit_cells": list(desc.unit_cells), "forcefield": forcefield.NAME, "grid_types": [], "spacing": 0.5,
           "molecule": "CO2", "temperature": 298.0, "pressure": 15000.0, "helium_void_fraction": 0.75,
           "raspa_root": str(o.env.root), "kind": "mc_direct", "cycles": 50, "init_cycles": 50}
    free = o._run_child(job, 600, "t", 0.0)
    held = o._run_child({**job, "block_file": str(blk)}, 600, "t", 0.0)
    assert free["status"] == "ok" and held["status"] == "ok", (free.get("stderr"), held.get("stderr"))
    assert held["result"]["blocked_centers"] == math.prod(desc.unit_cells)
    assert "blocked_centers" not in free["result"]
    assert held["result"]["uptake_molkg"] < free["result"]["uptake_molkg"]
