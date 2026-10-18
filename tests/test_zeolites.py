"""IZA zeolite references: naming, CIF resolution and the special-position merge (data is local only)."""

import pytest

from sieve.oracle import ZEOLITE_DIR, describe, zeolite_cif, zeolite_code

pytestmark = pytest.mark.skipif(not (ZEOLITE_DIR / "iza" / "CHA.cif").exists(), reason="IZA CIFs not downloaded")


def test_naming():
    assert zeolite_code("IZA-MFI") == "MFI"
    assert zeolite_code("iza-cha") == "CHA"
    assert zeolite_code("MFI") is None and zeolite_code("IZA-XXX") is None and zeolite_code("IZA-../x") is None
    assert zeolite_cif("IZA-MFI").stem == "IZA-MFI"


@pytest.mark.parametrize("code,n", [("CHA", 108), ("DDR", 360), ("MFI", 288), ("LTA", 72)])
def test_describe_merges_special_positions(tmp_path, code, n):
    d = describe(zeolite_cif(f"IZA-{code}"), tmp_path)
    assert d.parse_ok and d.structure == f"IZA-{code}" and d.alias == f"IZA-{code}"
    assert d.n_atoms == n and d.elements == ["O", "Si"] and d.metals == []
    assert d.core == {} or d.core.get("family") == "zeolite"  # sieve.porosity descriptors once built
    assert all(c >= 1 for c in d.unit_cells)
