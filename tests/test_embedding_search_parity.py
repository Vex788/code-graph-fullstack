"""Assert-based parity check for EmbeddingStore's vectorized vs pure-Python search.

Plain python3, no pytest fixtures. Run directly:
    python3 tests/test_embedding_search_parity.py
"""

from __future__ import annotations

import math
import os
import random
import tempfile

import pytest

from code_review_graph.embeddings import EmbeddingStore, _encode_vector

_PROVIDER = "test-provider"


def _make_store(tmp_dir: str) -> EmbeddingStore:
    return EmbeddingStore(os.path.join(tmp_dir, "graph.db"), provider=None)


def _insert_row(store: EmbeddingStore, name: str, vector: list[float]) -> None:
    store._conn.execute(
        "INSERT OR REPLACE INTO embeddings (qualified_name, vector, text_hash, provider) "
        "VALUES (?, ?, ?, ?)",
        (name, _encode_vector(vector), "hash", _PROVIDER),
    )
    store._conn.commit()


class _FakeProvider:
    """Minimal stand-in so ``search()`` can run without a real embedding model."""

    def __init__(self, query_vec: list[float]) -> None:
        self._query_vec = query_vec
        self.name = _PROVIDER

    def embed_query(self, text: str) -> list[float]:
        return self._query_vec


def test_ranking_parity_matches_for_random_vectors() -> None:
    random.seed(1234)
    dim, n = 32, 200
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            for i in range(n):
                _insert_row(store, f"node_{i}", [random.uniform(-1, 1) for _ in range(dim)])

            query_vec = [random.uniform(-1, 1) for _ in range(dim)]
            query_norm = math.sqrt(sum(x * x for x in query_vec))

            np = pytest.importorskip("numpy")  # vectorized path needs [embeddings]

            vec_results = store._search_vectorized(np, query_vec, query_norm, _PROVIDER, limit=50)
            py_results = store._search_pure_python(query_vec, _PROVIDER, limit=50)

            vec_names = [name for name, _ in vec_results]
            py_names = [name for name, _ in py_results]
            assert vec_names == py_names, f"ranking order differs:\n{vec_names}\nvs\n{py_names}"
            for (n1, s1), (n2, s2) in zip(vec_results, py_results):
                assert n1 == n2
                assert abs(s1 - s2) < 1e-5, f"{n1}: vectorized={s1} pure-python={s2}"
            print(f"OK: ranking parity across {len(vec_results)} results (dim={dim}, n={n})")
        finally:
            store.close()


def test_zero_norm_query_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            _insert_row(store, "node_a", [1.0, 2.0, 3.0])
            store.provider = _FakeProvider([0.0, 0.0, 0.0])
            results = store.search("anything", limit=10)
            assert results == [], f"expected [], got {results}"
            print("OK: zero-norm query returns []")
        finally:
            store.close()


def test_stored_zero_row_scores_zero_not_nan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            _insert_row(store, "zero_row", [0.0, 0.0, 0.0, 0.0])
            _insert_row(store, "real_row", [1.0, 0.0, 0.0, 0.0])
            query_vec = [1.0, 0.0, 0.0, 0.0]
            query_norm = 1.0

            np = pytest.importorskip("numpy")  # vectorized path needs [embeddings]

            vec_results = dict(
                store._search_vectorized(np, query_vec, query_norm, _PROVIDER, limit=10)
            )
            py_results = dict(store._search_pure_python(query_vec, _PROVIDER, limit=10))

            for results, label in ((vec_results, "vectorized"), (py_results, "pure-python")):
                assert not math.isnan(results["zero_row"]), f"{label}: zero row scored NaN"
                assert results["zero_row"] == 0.0, (
                    f"{label}: expected 0.0, got {results['zero_row']}"
                )
                assert results["real_row"] > results["zero_row"], (
                    f"{label}: zero-norm row won the ranking"
                )
            print("OK: stored zero-norm row scores 0.0 in both paths, never wins")
        finally:
            store.close()


if __name__ == "__main__":
    test_ranking_parity_matches_for_random_vectors()
    test_zero_norm_query_returns_empty()
    test_stored_zero_row_scores_zero_not_nan()
    print("ALL PASSED")
