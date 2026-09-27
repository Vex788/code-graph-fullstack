"""EmbeddingStore storage and search: float16 rows, legacy float32 rows, the
matrix cache, provider/dimension isolation, and the background embed helper.

Every provider here is a stub; nothing downloads a model.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import numpy as np
import pytest

from code_review_graph import embeddings
from code_review_graph.embeddings import (
    EmbeddingProvider,
    EmbeddingStore,
    _encode_stored_vector,
    _encode_vector,
    embed_changed,
    embeddings_status,
    open_search_store,
    read_embeddings_meta,
)
from code_review_graph.graph import GraphNode, GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.repo_settings import EmbeddingSettings, clear_cache


class StubProvider(EmbeddingProvider):
    """Deterministic 4-d vectors from keyword counts; records every call."""

    VOCAB = ("invoice", "vendor", "order", "user")

    def __init__(self, name: str = "stub:test:f32:d4") -> None:
        self._name = name
        self.doc_batches: list[list[str]] = []
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        low = text.lower()
        vec = [float(low.count(word)) for word in self.VOCAB]
        return vec if any(vec) else [0.0, 0.0, 0.0, 0.1]

    def embed(self, texts):
        self.doc_batches.append(list(texts))
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return self._vector(text)

    @property
    def dimension(self):
        return 4

    @property
    def name(self):
        return self._name


@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch):
    embeddings.clear_matrix_cache()
    clear_cache()
    monkeypatch.delenv("CRG_EMBEDDINGS", raising=False)
    yield
    embeddings.clear_matrix_cache()
    clear_cache()


def _node(name: str, parent: str = "", kind: str = "Function") -> GraphNode:
    return GraphNode(
        id=0, kind=kind, name=name, qualified_name=f"src/app.py::{name}",
        file_path="src/app.py", line_start=1, line_end=2, language="python",
        parent_name=parent or None, params=None, return_type=None, is_test=False,
        file_hash=None, extra={},
    )


def _store(tmp_path: Path, provider: EmbeddingProvider | None = None, **kw) -> EmbeddingStore:
    return EmbeddingStore(tmp_path / "graph.db", embedding_provider=provider or StubProvider(),
                          **kw)


def _raw_insert(store: EmbeddingStore, name: str, blob: bytes, provider: str) -> None:
    store._conn.execute(
        "INSERT OR REPLACE INTO embeddings (qualified_name, vector, text_hash, provider) "
        "VALUES (?, ?, 'h', ?)", (name, blob, provider),
    )


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def test_stored_vectors_are_normalized_float16(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.embed_nodes([_node("VendorInvoice"), _node("order_user")]) == 2
        blobs = [row[0] for row in store._conn.execute("SELECT vector FROM embeddings")]
        assert {len(b) for b in blobs} == {2 * 4}
        for blob in blobs:
            vec = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
            assert np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-3)
    finally:
        store.close()


def test_float32_dtype_setting_keeps_full_precision(tmp_path):
    store = _store(tmp_path, dtype="float32")
    try:
        store.embed_nodes([_node("VendorInvoice")])
        blob = store._conn.execute("SELECT vector FROM embeddings").fetchone()[0]
        assert len(blob) == 16
    finally:
        store.close()
    with pytest.raises(ValueError):
        _store(tmp_path, dtype="int8")


def test_encode_stored_vector_round_trip_and_zero_vector():
    blob = _encode_stored_vector([3.0, 4.0])
    assert struct.unpack("2e", blob) == pytest.approx((0.6, 0.8), abs=1e-3)
    assert embeddings._decode_stored(blob, 2) == pytest.approx([0.6, 0.8], abs=1e-3)
    assert embeddings._decode_stored(_encode_vector([1.0, 2.0]), 2) == [1.0, 2.0]
    assert embeddings._decode_stored(blob, 3) is None
    assert struct.unpack("2e", _encode_stored_vector([0.0, 0.0])) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Search: legacy rows, isolation, cache
# ---------------------------------------------------------------------------

def test_legacy_float32_rows_are_searched_next_to_float16_rows(tmp_path):
    provider = StubProvider()
    store = _store(tmp_path, provider)
    try:
        store.embed_nodes([_node("order_thing")])  # float16, normalized
        _raw_insert(store, "legacy::vendor_invoice", _encode_vector([10.0, 10.0, 0.0, 0.0]),
                    provider.name)  # float32, unnormalized
        results = dict(store.search("vendor invoice"))
        assert results["legacy::vendor_invoice"] == pytest.approx(1.0, abs=1e-5)
        assert results["src/app.py::order_thing"] == pytest.approx(0.0, abs=1e-3)
        py = dict(store._search_pure_python(provider._vector("vendor invoice"), provider.name, 10))
        assert py.keys() == results.keys()
    finally:
        store.close()


def test_mixed_dimensions_under_one_provider_do_not_crash_or_mix(tmp_path):
    """Witness: a dimension change left old rows behind; the reshape used to raise."""
    provider = StubProvider()
    store = _store(tmp_path, provider)
    try:
        _raw_insert(store, "new::a", _encode_vector([1.0, 1.0, 0.0, 0.0]), provider.name)
        _raw_insert(store, "old::b", _encode_vector([1.0, 1.0, 0.0]), provider.name)
        _raw_insert(store, "old::c", _encode_vector([1.0, 0.0, 0.0]), provider.name)
        results = store.search("vendor invoice", limit=10)
        assert [name for name, _ in results] == ["new::a"]
        assert store._search_pure_python([1.0, 1.0, 0.0, 0.0], provider.name, 10) == [
            ("new::a", pytest.approx(1.0))
        ]
    finally:
        store.close()


def test_search_only_sees_the_current_provider(tmp_path):
    store = _store(tmp_path, StubProvider("stub:a"))
    try:
        _raw_insert(store, "a::x", _encode_stored_vector([1.0, 1.0, 0.0, 0.0]), "stub:a")
        _raw_insert(store, "b::x", _encode_stored_vector([1.0, 1.0, 0.0, 0.0]), "stub:b")
        assert [n for n, _ in store.search("vendor invoice")] == ["a::x"]
    finally:
        store.close()


def test_search_without_vectors_never_embeds_the_query(tmp_path):
    provider = StubProvider()
    store = _store(tmp_path, provider)
    try:
        _raw_insert(store, "other::x", _encode_stored_vector([1.0, 0, 0, 0]), "stub:other")
        assert store.search("vendor") == []
        assert provider.queries == []
    finally:
        store.close()


def test_top_k_matches_full_sort(tmp_path):
    rng = np.random.default_rng(7)
    provider = StubProvider()
    store = _store(tmp_path, provider)
    try:
        for i in range(300):
            _raw_insert(store, f"n::{i}", _encode_stored_vector(rng.normal(size=4)),
                        provider.name)
        query = [0.3, -0.2, 0.9, 0.1]
        norm = float(np.linalg.norm(query))
        fast = store._search_vectorized(np, query, norm, provider.name, 25)
        # argpartition top-k equals a full sort of the same matrix...
        names, mat = store._matrix(np, provider.name, 4)
        sims = mat.astype(np.float32) @ (np.asarray(query, dtype=np.float32) / norm)
        assert [n for n, _ in fast] == [names[i] for i in np.argsort(-sims, kind="stable")[:25]]
        # ...and the pure-Python path agrees within float16 storage tolerance.
        slow = store._search_pure_python(query, provider.name, 25)
        for (_, a), (_, b) in zip(fast, slow):
            assert a == pytest.approx(b, abs=2e-3)
    finally:
        store.close()


def test_matrix_cache_is_reused_and_invalidated_by_writes(tmp_path, monkeypatch):
    provider = StubProvider()
    store = _store(tmp_path, provider)
    builds = {"n": 0}
    original = np.frombuffer

    def counting(*args, **kwargs):
        builds["n"] += 1
        return original(*args, **kwargs)

    try:
        store.embed_nodes([_node("VendorInvoice")])
        monkeypatch.setattr(np, "frombuffer", counting)
        store.search("vendor")
        store.search("invoice")
        assert builds["n"] == 1  # second search served from the cache

        store.embed_nodes([_node("order_user")])
        assert "src/app.py::order_user" in dict(store.search("order"))
        assert builds["n"] == 2

        store.remove_node("src/app.py::order_user")
        assert "src/app.py::order_user" not in dict(store.search("order"))

        # A write from another connection (another process) also invalidates.
        other = sqlite3.connect(str(tmp_path / "graph.db"))
        other.execute(
            "INSERT INTO embeddings VALUES ('ext::vendor', ?, 'h', ?)",
            (_encode_stored_vector([0.0, 1.0, 0.0, 0.0]), provider.name),
        )
        other.commit()
        other.close()
        assert "ext::vendor" in dict(store.search("vendor"))
    finally:
        store.close()


def test_generation_metadata_bumps_on_writes(tmp_path):
    graph = GraphStore(tmp_path / "graph.db")
    graph.close()
    store = _store(tmp_path)
    try:
        store.embed_nodes([_node("VendorInvoice")])
        store.remove_node("src/app.py::VendorInvoice")
        value = store._conn.execute(
            "SELECT value FROM metadata WHERE key = 'embeddings_generation'",
        ).fetchone()[0]
        assert value == "2"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Writes: batching
# ---------------------------------------------------------------------------

def test_hash_lookup_is_batched(tmp_path):
    store = _store(tmp_path)
    statements: list[str] = []
    try:
        nodes = [_node(f"fn_{i}") for i in range(1200)]
        store.embed_nodes(nodes, batch_size=500)
        store._conn.set_trace_callback(statements.append)
        assert store.embed_nodes(nodes) == 0  # unchanged text: nothing re-embedded
        lookups = [s for s in statements if s.startswith("SELECT qualified_name, text_hash")]
        assert len(lookups) == 3  # 1200 names in chunks of 500
    finally:
        store.close()


def test_batches_are_length_sorted(tmp_path):
    provider = StubProvider()
    store = _store(tmp_path, provider)
    try:
        names = ["a_really_long_function_name_x", "b", "mid_name", "zz_long_function_name"]
        store.embed_nodes([_node(n) for n in names], batch_size=2)
        lengths = [len(t) for batch in provider.doc_batches for t in batch]
        assert lengths == sorted(lengths)
    finally:
        store.close()


def test_documents_go_through_embed_documents(tmp_path):
    class DocProvider(StubProvider):
        def embed(self, texts):
            raise AssertionError("embed() must not be used when embed_documents exists")

        def embed_documents(self, texts):
            return [self._vector(t) for t in texts]

    store = _store(tmp_path, DocProvider())
    try:
        assert store.embed_nodes([_node("VendorInvoice")]) == 1
    finally:
        store.close()


def test_provider_switch_re_embeds(tmp_path):
    store = _store(tmp_path, StubProvider("stub:a"))
    try:
        assert store.embed_nodes([_node("VendorInvoice")]) == 1
        store.provider = StubProvider("stub:b")
        assert store.embed_nodes([_node("VendorInvoice")]) == 1
        assert store.count_for_provider("stub:b") == 1
    finally:
        store.close()


def test_legacy_local_provider_uses_model_query_prompt():
    class Model:
        prompts = {"query": "Represent this: "}

        def __init__(self):
            self.calls = []

        def encode(self, texts, prompt_name=None, show_progress_bar=False):
            self.calls.append(prompt_name)
            return [np.array([1.0, 0.0])]

    provider = embeddings.LocalEmbeddingProvider("m")
    provider._model = Model()
    provider.embed_query("q")
    provider.embed(["d"])
    assert provider._model.calls == ["query", None]


# ---------------------------------------------------------------------------
# embed_changed, state metadata, status, search store
# ---------------------------------------------------------------------------

def _graph(tmp_path: Path) -> tuple[Path, GraphStore]:
    repo = tmp_path / "repo"
    (repo / ".code-review-graph").mkdir(parents=True)
    graph = GraphStore(repo / ".code-review-graph" / "graph.db")
    for name in ("VendorInvoice", "OrderService", "UserDao"):
        graph.upsert_node(NodeInfo(kind="Class", name=name, file_path=str(repo / "A.java"),
                                   line_start=1, line_end=2, language="java"))
    graph.commit()
    return repo, graph


@pytest.fixture
def stub_profile(monkeypatch):
    provider = StubProvider()

    def fake(settings: EmbeddingSettings, *, mlx_parity=None):
        from code_review_graph.embedding_providers.profiles import FAST, Resolution

        res = Resolution(settings.profile, settings.profile, FAST, "stub-model", 4, True)
        return provider, res

    monkeypatch.setattr(embeddings, "provider_for_settings", fake)
    return provider


def test_embed_changed_off_by_default(tmp_path, stub_profile):
    repo, graph = _graph(tmp_path)
    try:
        result = embed_changed(graph, None, repo_root=repo)
        assert result["state"] == "off" and result["embedded"] == 0
        assert stub_profile.doc_batches == []
        assert read_embeddings_meta(graph._conn)["embeddings_state"] == "off"
    finally:
        graph.close()


def test_first_enable_creates_vectors_and_state(tmp_path, stub_profile):
    """Witness: enabling on a graph with zero vectors must embed (refresh returned early)."""
    repo, graph = _graph(tmp_path)
    try:
        result = embed_changed(graph, None, repo_root=repo, env={"CRG_EMBEDDINGS": "fast"})
        assert result["state"] == "ready" and result["embedded"] == 3
        assert result["priority"].startswith(("nice+", "qos", "unchanged"))
        meta = read_embeddings_meta(graph._conn)
        assert meta["embeddings_state"] == "ready"
        assert meta["embeddings_provider"] == stub_profile.name
        assert meta["embeddings_model"] == "stub-model"
        assert meta["embeddings_dim"] == "4"
        assert meta["embeddings_stale_count"] == "0"
    finally:
        graph.close()


def test_embed_changed_only_embeds_changed_nodes(tmp_path, stub_profile):
    repo, graph = _graph(tmp_path)
    env = {"CRG_EMBEDDINGS": "fast"}
    try:
        embed_changed(graph, None, repo_root=repo, env=env)
        stub_profile.doc_batches.clear()
        graph.upsert_node(NodeInfo(kind="Class", name="InvoiceLine",
                                   file_path=str(repo / "B.java"), line_start=1, line_end=2,
                                   language="java"))
        graph.commit()
        qn = graph._conn.execute(
            "SELECT qualified_name FROM nodes WHERE name = 'InvoiceLine'").fetchone()[0]
        result = embed_changed(graph, [qn], repo_root=repo, env=env)
        assert result["embedded"] == 1
        assert [len(b) for b in stub_profile.doc_batches] == [1]
    finally:
        graph.close()


def test_embed_changed_marks_stale_when_nodes_are_left_out(tmp_path, stub_profile):
    repo, graph = _graph(tmp_path)
    try:
        qn = graph._conn.execute(
            "SELECT qualified_name FROM nodes WHERE name = 'UserDao'").fetchone()[0]
        result = embed_changed(graph, [qn], repo_root=repo, env={"CRG_EMBEDDINGS": "fast"})
        assert result["state"] == "stale" and result["stale_count"] == 2
    finally:
        graph.close()


def test_embed_changed_in_background_thread(tmp_path, stub_profile):
    repo, graph = _graph(tmp_path)
    try:
        thread = embed_changed(graph, None, repo_root=repo, wait=False,
                               env={"CRG_EMBEDDINGS": "fast"})
        thread.join(10)
        assert thread.result["state"] == "ready"
    finally:
        graph.close()


def test_embed_changed_unavailable_backend_records_state(tmp_path, monkeypatch):
    from code_review_graph.embedding_providers import profiles

    monkeypatch.setattr(profiles, "module_available", lambda name: False)
    repo, graph = _graph(tmp_path)
    try:
        result = embed_changed(graph, None, repo_root=repo, env={"CRG_EMBEDDINGS": "fast"})
        assert result["state"] == "unavailable" and "embeddings-fast" in result["warning"]
        assert read_embeddings_meta(graph._conn)["embeddings_state"] == "unavailable"
    finally:
        graph.close()


def test_embed_changed_provider_failure_never_raises(tmp_path, stub_profile):
    repo, graph = _graph(tmp_path)

    def boom(texts):
        raise RuntimeError("model crashed")

    stub_profile.embed = boom
    try:
        result = embed_changed(graph, None, repo_root=repo, env={"CRG_EMBEDDINGS": "fast"})
        assert result["state"] == "stale" and "model crashed" in result["error"]
    finally:
        graph.close()


def test_open_search_store_states(tmp_path, stub_profile, monkeypatch):
    repo, graph = _graph(tmp_path)
    db = graph.db_path
    try:
        store, info = open_search_store(db, repo_root=repo, env={})
        assert store is None and info == {"state": "off", "provider": None, "warning": None}

        env = {"CRG_EMBEDDINGS": "fast"}
        store, info = open_search_store(db, repo_root=repo, env=env)
        assert store is None and info["state"] == "stale" and "enable" in info["warning"]

        embed_changed(graph, None, repo_root=repo, env=env)
        store, info = open_search_store(db, repo_root=repo, env=env)
        assert store is not None and info["state"] == "ready"
        store.close()
    finally:
        graph.close()


def test_embeddings_status_reports_counts_and_state(tmp_path, stub_profile, monkeypatch):
    from code_review_graph.embedding_providers import profiles

    monkeypatch.setattr(profiles, "module_available", lambda name: name == "model2vec")
    repo, graph = _graph(tmp_path)
    try:
        status = embeddings_status(graph.db_path, repo)
        assert status["state"] == "off" and status["vectors"] == 0
        (repo / ".code-review-graph.toml").write_text(
            "[embeddings]\nenabled = true\nprofile = \"fast\"\n")
        clear_cache()
        status = embeddings_status(graph.db_path, repo)
        assert status["state"] == "stale" and status["stale_count"] == 3
        assert status["backend"] == "model2vec" and status["dim"] == 256
        assert status["provider"] == "model2vec:minishlab/potion-code-16M-v2:f32:d256"
    finally:
        graph.close()


def test_default_store_follows_enabled_settings(tmp_path, stub_profile, monkeypatch):
    repo, graph = _graph(tmp_path)
    graph.close()
    db = repo / ".code-review-graph" / "graph.db"
    sentinel = StubProvider("legacy:default")
    monkeypatch.setattr(embeddings, "get_provider", lambda *a, **k: sentinel)

    store = EmbeddingStore(db)  # embeddings off: historic default provider
    assert store.provider is sentinel and store.dtype == "float16"
    store.close()

    monkeypatch.setenv("CRG_EMBEDDINGS", "fast")
    store = EmbeddingStore(db)  # e.g. embed_graph_tool with no arguments
    assert store.provider is stub_profile
    store.close()
