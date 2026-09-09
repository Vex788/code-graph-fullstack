"""Assert-based check that hybrid_search pins an exact simple-name match ahead
of fuzzy neighbours, without disturbing relative order elsewhere.

Plain python3, no pytest fixtures. Run directly:
    python3 tests/test_hybrid_exact_pin.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.search import hybrid_search


def _seed(store: GraphStore) -> dict[str, int]:
    """Three Function nodes sharing the "processOrder" prefix.

    ``processOrder`` is the exact match target; ``processOrderExtra`` and
    ``processOrderUtil`` are same-prefix fuzzy neighbours (mirrors "a class
    full of getters").
    """
    nodes = [
        NodeInfo(
            kind="Function", name="processOrder", file_path="orders.py",
            line_start=1, line_end=10, language="python",
        ),
        NodeInfo(
            kind="Function", name="processOrderExtra", file_path="extra.py",
            line_start=1, line_end=10, language="python",
        ),
        NodeInfo(
            kind="Function", name="processOrderUtil", file_path="util.py",
            line_start=1, line_end=10, language="python",
        ),
    ]
    ids: dict[str, int] = {}
    for node in nodes:
        ids[node.name] = store.upsert_node(node, file_hash="abc123")
    store._conn.commit()
    return ids


def _run(query: str, ids: dict[str, int], store: GraphStore):
    """Call the real hybrid_search with FTS/embedding ranking pinned by mock.

    Fixes the pre-boost ranking so the fuzzy neighbours ("_extra", "_util")
    outrank the exact match ("processOrder") purely on rank — reproducing
    "an FTS lane crowded with same-prefix symbols burying the exact match" —
    then lets hybrid_search's real boosting and (new) exact-name pin run
    unmodified on top of that.
    """
    fts_order = [
        (ids["processOrderExtra"], 10.0),
        (ids["processOrderUtil"], 8.0),
        (ids["processOrder"], 5.0),
    ]
    with patch("code_review_graph.search._fts_search", return_value=fts_order), \
         patch("code_review_graph.search._embedding_search", return_value=[]):
        return hybrid_search(store, query, limit=10)


def test_exact_name_match_pinned_ahead_of_fuzzy_neighbours() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store = GraphStore(tmp.name)
    try:
        ids = _seed(store)
        results = _run("processOrder", ids, store)

        names = [r["qualified_name"] for r in results]
        assert len(results) == 3, f"expected 3 results, got {len(results)}"

        exact = next(r for r in results if r["name"] == "processOrder")
        extra = next(r for r in results if r["name"] == "processOrderExtra")
        util = next(r for r in results if r["name"] == "processOrderUtil")

        # The exact match leads despite a lower raw score than its fuzzy
        # neighbours — proof the reordering, not the score, put it first.
        assert results[0]["name"] == "processOrder", f"exact match not first: {names}"
        assert exact["score"] < extra["score"], (
            f"expected exact match's own score below its fuzzy neighbour's "
            f"(exact={exact['score']}, extra={extra['score']}) to prove the "
            f"ordering was overridden, not naturally highest-scored"
        )

        # Stability: relative order within the non-exact ("rest") group is
        # unchanged from the pre-pin score order (extra outscored util).
        rest_names = [r["name"] for r in results if r["name"] != "processOrder"]
        assert rest_names == ["processOrderExtra", "processOrderUtil"], (
            f"rest-group order not preserved: {rest_names}"
        )
        assert extra["score"] > util["score"]

        # Same objects structurally: no keys added/removed/changed by the pin.
        expected_keys = {
            "name", "qualified_name", "kind", "file_path", "line_start",
            "line_end", "language", "params", "return_type", "signature",
            "score",
        }
        for r in results:
            assert set(r.keys()) == expected_keys, f"keys changed: {r.keys()}"

        print(f"OK: exact match pinned first ahead of higher-scored neighbours: {names}")
    finally:
        store.close()
        Path(tmp.name).unlink(missing_ok=True)


def test_short_token_does_not_pin() -> None:
    """A query token under 6 chars must not trigger pinning."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store = GraphStore(tmp.name)
    try:
        short_node = NodeInfo(
            kind="Function", name="asdf", file_path="short.py",
            line_start=1, line_end=5, language="python",
        )
        other_node = NodeInfo(
            kind="Function", name="asdf_longer_neighbor", file_path="long.py",
            line_start=1, line_end=5, language="python",
        )
        ids = {
            "asdf": store.upsert_node(short_node, file_hash="x"),
            "asdf_longer_neighbor": store.upsert_node(other_node, file_hash="x"),
        }
        store._conn.commit()

        # "asdf" is 4 chars: below the 6-char pin threshold, even though it
        # exactly equals a node's simple name and query token.
        fts_order = [(ids["asdf_longer_neighbor"], 10.0), (ids["asdf"], 5.0)]
        with patch("code_review_graph.search._fts_search", return_value=fts_order), \
             patch("code_review_graph.search._embedding_search", return_value=[]):
            results = hybrid_search(store, "asdf", limit=10)

        names = [r["name"] for r in results]
        # Unpinned score order preserved: the higher-scored neighbour stays first.
        assert names == ["asdf_longer_neighbor", "asdf"], (
            f"short token incorrectly pinned exact match: {names}"
        )
        print(f"OK: short (<6 char) token does not pin: {names}")
    finally:
        store.close()
        Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    test_exact_name_match_pinned_ahead_of_fuzzy_neighbours()
    test_short_token_does_not_pin()
    print("ALL PASSED")
