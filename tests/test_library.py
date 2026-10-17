from pathlib import Path

import pytest

from sieve import library

pytestmark = pytest.mark.skipif(
    not Path(library.LIBRARY_PATH).exists(),
    reason="science library not built (run scripts/build_library.py)",
)


def test_library_file_non_empty():
    path = Path(library.LIBRARY_PATH)
    assert path.stat().st_size > 0
    recs = library.records()
    assert len(recs) > 50
    assert {"id", "question", "reasoning", "answer", "topics"} <= set(recs[0])


@pytest.mark.parametrize(
    "query",
    [
        "open metal sites CO2 binding electrostatic",
        "pore size overlapping van der Waals potential adsorption",
    ],
)
def test_search_returns_scored_results(query):
    res = library.search(query, k=3)
    assert len(res) == 3
    for r in res:
        assert r["score"] > 0
        assert r["question"] and r["excerpt"]
        assert library.cite(r) == f"Nemotron-Science-v1 RQA #{r['id']}"
    scores = [r["score"] for r in res]
    assert scores == sorted(scores, reverse=True)


def test_search_deterministic():
    q = "Langmuir isotherm saturation low pressure Henry regime"
    assert library.search(q, k=5) == library.search(q, k=5)


def test_empty_query():
    assert library.search("the of and", k=3) == []
