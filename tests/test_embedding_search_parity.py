"""Assert-based parity check for EmbeddingStore's vectorized vs pure-Python search.

Plain python3, no pytest fixtures. Run directly:
    python3 tests/test_embedding_search_parity.py
"""

from __future__ import annotations

import math
import os
import random
import struct
import tempfile

import pytest

from code_review_graph.embeddings import (
    EmbeddingStore,
    _encode_stored_vector,
    _encode_vector,
)

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


def _insert_stored_row(store: EmbeddingStore, name: str, blob: bytes) -> None:
    """Insert a row exactly as production writes it (already-encoded blob)."""
    store._conn.execute(
        "INSERT OR REPLACE INTO embeddings (qualified_name, vector, text_hash, provider) "
        "VALUES (?, ?, ?, ?)",
        (name, blob, "hash", _PROVIDER),
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


def test_ranking_parity_on_production_float16_rows() -> None:
    """Parity on the representation production actually writes.

    ``_encode_stored_vector`` L2-normalizes and rounds to float16, so a stored
    row is unit-length only to within the format's error (~6e-5). The ranking
    test above inserts raw float32, which is the legacy branch and never
    catches a missing per-row renormalization in the float16 branch.
    """
    random.seed(2024)
    dim, n = 384, 300
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            for i in range(n):
                vec = [random.gauss(0, 1) for _ in range(dim)]
                _insert_stored_row(store, f"node_{i}", _encode_stored_vector(vec))

            query_vec = [random.gauss(0, 1) for _ in range(dim)]
            query_norm = math.sqrt(sum(x * x for x in query_vec))

            np = pytest.importorskip("numpy")

            vec_results = store._search_vectorized(np, query_vec, query_norm, _PROVIDER, limit=50)
            py_results = store._search_pure_python(query_vec, _PROVIDER, limit=50)

            vec_names = [name for name, _ in vec_results]
            py_names = [name for name, _ in py_results]
            assert vec_names == py_names, f"ranking order differs:\n{vec_names}\nvs\n{py_names}"
            worst = max(
                (abs(s1 - s2) for (_, s1), (_, s2) in zip(vec_results, py_results)),
                default=0.0,
            )
            assert worst < 1e-5, f"float16 row divergence {worst:.3e} exceeds tolerance"
            print(f"OK: float16 production rows rank identically (worst delta {worst:.2e})")
        finally:
            store.close()


def test_nonpositive_limit_matches_on_both_paths() -> None:
    """A negative limit must return nothing, not ``scored[:-1]``.

    ``list[:-1]`` silently returns every row but the last, so the two paths
    disagreed for any ``limit <= -1``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            for i in range(4):
                _insert_row(store, f"node_{i}", [1.0, float(i), 0.0, 0.0])

            query_vec = [1.0, 0.0, 0.0, 0.0]
            query_norm = 1.0
            np = pytest.importorskip("numpy")

            for limit in (-5, -1, 0):
                py = store._search_pure_python(query_vec, _PROVIDER, limit=limit)
                vec = store._search_vectorized(np, query_vec, query_norm, _PROVIDER, limit=limit)
                assert py == [], f"pure-python limit={limit} returned {len(py)} rows, expected []"
                assert vec == [], f"vectorized limit={limit} returned {len(vec)} rows, expected []"
            print("OK: limit <= 0 returns [] on both paths")
        finally:
            store.close()


def test_non_finite_row_scores_zero_on_both_paths() -> None:
    """A stored NaN row must score 0.0, never sort to the top as NaN."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            _insert_row(store, "good_row", [1.0, 0.0, 0.0, 0.0])
            _insert_stored_row(
                store, "nan_row", _encode_stored_vector([float("nan")] * 4)
            )

            query_vec = [1.0, 0.0, 0.0, 0.0]
            query_norm = 1.0
            np = pytest.importorskip("numpy")

            py = dict(store._search_pure_python(query_vec, _PROVIDER, limit=10))
            vec = dict(store._search_vectorized(np, query_vec, query_norm, _PROVIDER, limit=10))

            for results, label in ((py, "pure-python"), (vec, "vectorized")):
                assert results["nan_row"] == 0.0, (
                    f"{label}: NaN row scored {results['nan_row']!r}, expected 0.0"
                )
                assert results["good_row"] > results["nan_row"], (
                    f"{label}: NaN row outranked a real match"
                )
            print("OK: stored NaN row scores 0.0 on both paths, never wins")
        finally:
            store.close()


def test_non_finite_query_returns_empty() -> None:
    """A provider emitting NaN must not rank the whole index."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            _insert_row(store, "good_row", [1.0, 0.0, 0.0, 0.0])
            store.provider = _FakeProvider([float("nan")] * 4)
            assert store.search("anything", limit=10) == []
            print("OK: non-finite query vector returns []")
        finally:
            store.close()


def test_search_disagrees_no_longer_on_limit() -> None:
    """The public entry point honours a negative limit the same way either path does."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            for i in range(3):
                _insert_row(store, f"node_{i}", [1.0, float(i), 0.0, 0.0])
            store.provider = _FakeProvider([1.0, 0.0, 0.0, 0.0])
            assert store.search("anything", limit=-1) == []
            assert len(store.search("anything", limit=2)) == 2
            print("OK: search() honours limit <= 0")
        finally:
            store.close()


def test_float16_row_scores_one_against_itself() -> None:
    """A vector's similarity with itself is 1.0 regardless of storage dtype.

    Deterministic witness for the missing per-row renormalization: a stored
    float16 row is unit-length only to ~6e-5, so an un-normalized matrix
    returns that residual (0.99996...) instead of 1.0. Ranking parity alone can
    miss this on a lucky seed; the identity probe cannot.
    """
    dim = 384
    rng = random.Random(2024)  # float16 norm error ~1.4e-5, above the 1e-5 tolerance
    vector = [rng.gauss(0, 1) for _ in range(dim)]
    blob = _encode_stored_vector(vector)
    query = list(struct.unpack(f"{dim}e", blob))  # the stored row, exactly

    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        try:
            _insert_stored_row(store, "self", blob)
            query_norm = math.sqrt(sum(x * x for x in query))

            np = pytest.importorskip("numpy")

            vec = dict(store._search_vectorized(np, query, query_norm, _PROVIDER, limit=1))
            py = dict(store._search_pure_python(query, _PROVIDER, limit=1))

            assert abs(vec["self"] - 1.0) < 1e-5, (
                f"vectorized self-similarity {vec['self']!r}: the matrix row was not renormalized"
            )
            assert abs(py["self"] - 1.0) < 1e-6, f"pure-python self-similarity {py['self']!r}"
            print(f"OK: float16 row self-similarity is 1.0 (vectorized={vec['self']:.7f})")
        finally:
            store.close()


if __name__ == "__main__":
    test_ranking_parity_matches_for_random_vectors()
    test_ranking_parity_on_production_float16_rows()
    test_zero_norm_query_returns_empty()
    test_stored_zero_row_scores_zero_not_nan()
    test_float16_row_scores_one_against_itself()
    test_nonpositive_limit_matches_on_both_paths()
    test_non_finite_row_scores_zero_on_both_paths()
    test_non_finite_query_returns_empty()
    test_search_disagrees_no_longer_on_limit()
    print("ALL PASSED")
