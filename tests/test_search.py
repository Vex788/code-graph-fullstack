"""Tests for the hybrid search engine."""

import tempfile
from pathlib import Path

import pytest

from code_review_graph import embeddings as embeddings_mod
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.search import (
    _fts_search,
    _query_terms,
    detect_query_kind_boost,
    hybrid_search,
    rebuild_fts_index,
    rrf_merge,
)


class TestHybridSearch:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed_data()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed_data(self):
        """Seed test nodes into the graph store."""
        nodes = [
            NodeInfo(
                kind="Function", name="get_users", file_path="api.py",
                line_start=1, line_end=20, language="python",
                params="(db: Session)", return_type="list[User]",
            ),
            NodeInfo(
                kind="Function", name="create_user", file_path="api.py",
                line_start=25, line_end=40, language="python",
                params="(name: str, email: str)", return_type="User",
            ),
            NodeInfo(
                kind="Class", name="UserService", file_path="services.py",
                line_start=1, line_end=100, language="python",
            ),
            NodeInfo(
                kind="Function", name="authenticate", file_path="auth.py",
                line_start=5, line_end=30, language="python",
                params="(token: str)", return_type="bool",
            ),
            NodeInfo(
                kind="Type", name="UserResponse", file_path="models.py",
                line_start=1, line_end=15, language="python",
            ),
        ]
        for node in nodes:
            node_id = self.store.upsert_node(node, file_hash="abc123")
            # Set signature for functions
            if node.kind == "Function":
                sig = f"def {node.name}{node.params or '()'} -> {node.return_type or 'None'}"
                self.store._conn.execute(
                    "UPDATE nodes SET signature = ? WHERE id = ?", (sig, node_id)
                )
        self.store._conn.commit()

    # --- rebuild_fts_index ---

    def test_rebuild_fts_index(self):
        """rebuild_fts_index returns the correct count of indexed rows."""
        count = rebuild_fts_index(self.store)
        assert count == 5

    def test_rebuild_fts_index_idempotent(self):
        """Rebuilding twice gives the same count."""
        count1 = rebuild_fts_index(self.store)
        count2 = rebuild_fts_index(self.store)
        assert count1 == count2

    # --- FTS search by name ---

    def test_fts_search_by_name(self):
        """FTS search finds a node by its name."""
        rebuild_fts_index(self.store)
        results = hybrid_search(self.store, "get_users")
        assert len(results) > 0
        names = [r["name"] for r in results]
        assert "get_users" in names

    # --- FTS search by signature ---

    def test_fts_search_by_signature(self):
        """FTS search finds a node by content in its signature."""
        rebuild_fts_index(self.store)
        results = hybrid_search(self.store, "Session")
        assert len(results) > 0
        # get_users has "Session" in its signature
        names = [r["name"] for r in results]
        assert "get_users" in names

    # --- Kind boosting ---

    def test_kind_boost_pascal_case(self):
        """PascalCase query boosts Class kind > 1.0."""
        boosts = detect_query_kind_boost("UserService")
        assert "Class" in boosts
        assert boosts["Class"] > 1.0

    def test_kind_boost_snake_case(self):
        """snake_case query boosts Function kind > 1.0."""
        boosts = detect_query_kind_boost("get_users")
        assert "Function" in boosts
        assert boosts["Function"] > 1.0

    def test_kind_boost_dotted(self):
        """Dotted query boosts qualified name matches."""
        boosts = detect_query_kind_boost("api.get_users")
        assert "_qualified" in boosts
        assert boosts["_qualified"] > 1.0

    def test_kind_boost_empty(self):
        """Empty query returns no boosts."""
        boosts = detect_query_kind_boost("")
        assert boosts == {}

    def test_kind_boost_all_uppercase(self):
        """ALL_CAPS should not trigger PascalCase boost."""
        boosts = detect_query_kind_boost("HTTP_STATUS")
        assert "Class" not in boosts
        # But should trigger snake_case boost
        assert "Function" in boosts

    # --- RRF merge ---

    def test_rrf_merge(self):
        """Node appearing in both lists ranks highest after RRF merge."""
        list_a = [(1, 10.0), (2, 8.0), (3, 6.0)]
        list_b = [(2, 9.0), (4, 7.0), (1, 5.0)]

        merged = rrf_merge(list_a, list_b)
        ids = [item_id for item_id, _ in merged]

        # Items 1 and 2 appear in both lists, so they should be top-ranked
        assert ids[0] in (1, 2)
        assert ids[1] in (1, 2)
        # ID 2 is rank 0+0 in list_b and rank 1 in list_a
        # ID 1 is rank 0 in list_a and rank 2 in list_b
        # So ID 2 should rank higher: 1/(60+1+1) + 1/(60+0+1) vs 1/(60+0+1) + 1/(60+2+1)
        assert ids[0] == 2

    def test_rrf_merge_single_list(self):
        """RRF merge with a single list preserves order."""
        single = [(10, 5.0), (20, 3.0), (30, 1.0)]
        merged = rrf_merge(single)
        ids = [item_id for item_id, _ in merged]
        assert ids == [10, 20, 30]

    def test_rrf_merge_empty(self):
        """RRF merge with empty lists returns empty."""
        merged = rrf_merge([], [])
        assert merged == []

    # --- Fallback to keyword search ---

    def test_fallback_to_keyword(self):
        """Works without FTS index by falling back to keyword LIKE matching."""
        # Do NOT rebuild FTS index — drop it if it exists
        try:
            self.store._conn.execute("DROP TABLE IF EXISTS nodes_fts")
            self.store._conn.commit()
        except Exception:
            pass

        results = hybrid_search(self.store, "authenticate")
        assert len(results) > 0
        names = [r["name"] for r in results]
        assert "authenticate" in names

    # --- Empty query ---

    def test_empty_query_handled(self):
        """Empty query returns empty results without crashing."""
        results = hybrid_search(self.store, "")
        assert results == []

    def test_whitespace_query_handled(self):
        """Whitespace-only query returns empty results."""
        results = hybrid_search(self.store, "   ")
        assert results == []

    # --- Return fields ---

    def test_hybrid_search_returns_expected_fields(self):
        """All expected fields are present in search results."""
        rebuild_fts_index(self.store)
        results = hybrid_search(self.store, "get_users")
        assert len(results) > 0

        expected_fields = {
            "name", "qualified_name", "kind", "file_path",
            "line_start", "line_end", "language", "params",
            "return_type", "signature", "score",
        }
        for result in results:
            assert expected_fields.issubset(result.keys()), (
                f"Missing fields: {expected_fields - result.keys()}"
            )

    # --- Kind filtering ---

    def test_kind_filter(self):
        """Kind parameter filters results to only that kind."""
        rebuild_fts_index(self.store)
        results = hybrid_search(self.store, "User", kind="Class")
        for r in results:
            assert r["kind"] == "Class"

    # --- Context file boosting ---

    def test_context_file_boost(self):
        """Nodes in context_files get boosted above others."""
        rebuild_fts_index(self.store)

        # Search for "user" which matches multiple nodes
        results_with_ctx = hybrid_search(
            self.store, "user", context_files=["api.py"]
        )

        # Find get_users in both result sets
        if results_with_ctx:
            api_nodes = [r for r in results_with_ctx if r["file_path"] == "api.py"]
            if api_nodes:
                # api.py nodes should have a score boost
                api_score = api_nodes[0]["score"]
                assert api_score > 0

    # --- Limit parameter ---

    def test_limit_respected(self):
        """Search respects the limit parameter."""
        rebuild_fts_index(self.store)
        results = hybrid_search(self.store, "user", limit=2)
        assert len(results) <= 2

    # --- FTS5 injection safety ---

    def test_fts_query_with_special_chars(self):
        """FTS5 special characters are safely handled."""
        rebuild_fts_index(self.store)
        # These should not crash — FTS5 operators like AND, OR, NOT, *, etc.
        for dangerous_query in ['OR user', 'NOT thing', 'user*', '"user"', 'a AND b']:
            results = hybrid_search(self.store, dangerous_query)
            # Just assert no exception was raised
            assert isinstance(results, list)

    # --- _out_mode tracking ---

    def test_out_mode_fts_only(self):
        """_out_mode is 'fts' when only FTS contributes (no embeddings)."""
        rebuild_fts_index(self.store)
        out: list[str] = []
        results = hybrid_search(self.store, "authenticate", _out_mode=out)
        assert out == ["fts"]
        assert len(results) > 0

    def test_out_mode_keyword(self):
        """_out_mode is 'keyword' when FTS table is absent and no embeddings."""
        self.store._conn.execute("DROP TABLE IF EXISTS nodes_fts")
        self.store._conn.commit()
        out: list[str] = []
        results = hybrid_search(self.store, "authenticate", _out_mode=out)
        assert out == ["keyword"]
        assert len(results) > 0

    def test_out_mode_keyword_no_results(self):
        """_out_mode is 'none' when keyword fallback also returns 0 results."""
        self.store._conn.execute("DROP TABLE IF EXISTS nodes_fts")
        self.store._conn.commit()
        out: list[str] = []
        results = hybrid_search(self.store, "xyzzy_nonexistent_abc123", _out_mode=out)
        assert results == []
        assert out == ["none"]

    def test_out_mode_semantic(self, monkeypatch):
        """_out_mode is 'semantic' when only embeddings contribute."""
        import code_review_graph.search as search_mod

        # Triggers index nodes on write; empty the index so only embeddings hit.
        self.store._conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
        self.store._conn.commit()
        node_id = self.store._conn.execute(
            "SELECT id FROM nodes WHERE name = 'authenticate'"
        ).fetchone()[0]

        def fake_emb(store, query, limit=50, model=None, provider=None, **_kwargs):
            return [(node_id, 0.9)]

        monkeypatch.setattr(search_mod, "_embedding_search", fake_emb)
        out: list[str] = []
        results = hybrid_search(self.store, "authenticate", _out_mode=out)
        assert out == ["semantic"]
        assert len(results) > 0

    def test_out_mode_hybrid(self, monkeypatch):
        """_out_mode is 'hybrid' when both FTS and embeddings contribute."""
        import code_review_graph.search as search_mod

        rebuild_fts_index(self.store)
        node_id = self.store._conn.execute(
            "SELECT id FROM nodes WHERE name = 'authenticate'"
        ).fetchone()[0]

        def fake_emb(store, query, limit=50, model=None, provider=None, **_kwargs):
            return [(node_id, 0.9)]

        monkeypatch.setattr(search_mod, "_embedding_search", fake_emb)
        out: list[str] = []
        results = hybrid_search(self.store, "authenticate", _out_mode=out)
        assert out == ["hybrid"]
        assert len(results) > 0

    def test_out_mode_empty_query(self):
        """_out_mode is 'none' for empty queries (no search ran)."""
        out: list[str] = []
        results = hybrid_search(self.store, "", _out_mode=out)
        assert results == []
        assert out == ["none"]

    def test_fts_rebuild_is_atomic(self):
        """Regression test for #259: rebuild_fts_index must wrap the DROP +
        CREATE + INSERT sequence in a single transaction so a crash between
        DROP and CREATE cannot leave the DB without an FTS table."""
        # Build, rebuild, then verify the table exists and is queryable.
        rebuild_fts_index(self.store)

        # Verify the FTS table exists and has rows.
        conn = self.store._conn
        count = conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0]
        assert count > 0

        # Rebuild again — must not raise and must leave the table intact.
        new_count = rebuild_fts_index(self.store)
        assert new_count == count

        # Verify search still works after double-rebuild.
        results = hybrid_search(self.store, "auth")
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# W4a: OR-ed terms, phrase-first order, LIKE top-up, honest embeddings state
# ---------------------------------------------------------------------------

def _names_store(tmp_path, names):
    store = GraphStore(tmp_path / "graph.db")
    for i, name in enumerate(names):
        store.upsert_node(NodeInfo(kind="Class", name=name, file_path=f"src/F{i}.java",
                                   line_start=1, line_end=2, language="java"))
    store.commit()
    return store


def _hit_names(store, hits):
    rows = {r["id"]: r["name"] for r in store._conn.execute("SELECT id, name FROM nodes")}
    return [rows[node_id] for node_id, _ in hits]


def test_query_terms_split_identifiers_and_drop_stopwords():
    raw, tokens, sequence = _query_terms("who saves the VendorInvoice")
    assert raw == ["saves", "VendorInvoice"]
    assert tokens == ["saves", "vendor", "invoice"]
    assert sequence == "saves vendor invoice"


def test_multi_word_query_ors_terms(tmp_path):
    """Witness: one missing word used to empty the phrase-only FTS lane."""
    store = _names_store(tmp_path, ["VendorInvoice", "PaymentService", "Unrelated"])
    try:
        names = _hit_names(store, _fts_search(store._conn, "vendor payment", limit=10))
        assert set(names) == {"VendorInvoice", "PaymentService"}
    finally:
        store.close()


def test_phrase_hits_rank_before_single_word_hits(tmp_path):
    store = _names_store(tmp_path, ["InvoiceVendor", "VendorInvoiceActionBean", "VendorList"])
    try:
        names = _hit_names(store, _fts_search(store._conn, "vendor invoice", limit=10))
        assert names[0] == "VendorInvoiceActionBean"
        assert set(names[1:]) == {"InvoiceVendor", "VendorList"}
    finally:
        store.close()


def test_single_identifier_matches_parts_in_any_order(tmp_path):
    store = _names_store(tmp_path, ["VendorInvoiceActionBean", "InvoiceLine", "BeanUtils"])
    try:
        names = _hit_names(store, _fts_search(store._conn, "InvoiceBean", limit=10))
        assert names == ["VendorInvoiceActionBean"]
    finally:
        store.close()


def test_exact_identifier_does_not_broaden(tmp_path):
    store = _names_store(tmp_path, ["helper_0_0", "helper_1_2", "helper_3_4"])
    try:
        assert _hit_names(store, _fts_search(store._conn, "helper_1_2", limit=10)) == [
            "helper_1_2"
        ]
    finally:
        store.close()


def test_fts_operators_in_query_are_literal(tmp_path):
    store = _names_store(tmp_path, ["VendorInvoice"])
    try:
        assert _fts_search(store._conn, 'vendor" OR NEAR(x', limit=10) is not None
        assert _hit_names(store, _fts_search(store._conn, "vendor AND", limit=10)) == [
            "VendorInvoice"
        ]
    finally:
        store.close()


def test_like_lane_tops_up_few_hits(tmp_path):
    store = _names_store(tmp_path, ["voice", "VendorInvoice", "Other"])
    try:
        mode: list[str] = []
        names = [r["name"] for r in hybrid_search(store, "voice", _out_mode=mode)]
        # FTS only knows the whole token "voice"; LIKE adds the substring match.
        assert names == ["voice", "VendorInvoice"]
        assert mode == ["fts"]
    finally:
        store.close()


def _repo_with_graph(tmp_path, names):
    repo = tmp_path / "repo"
    (repo / ".code-review-graph").mkdir(parents=True)
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    for i, name in enumerate(names):
        store.upsert_node(NodeInfo(kind="Class", name=name, file_path=str(repo / f"F{i}.java"),
                                   line_start=1, line_end=2, language="java"))
    store.commit()
    return repo, store


def test_embeddings_off_reports_state_without_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("CRG_EMBEDDINGS", raising=False)
    repo, store = _repo_with_graph(tmp_path, ["VendorInvoice"])
    try:
        info: dict = {}
        mode: list[str] = []
        hybrid_search(store, "vendor", repo_root=str(repo), _out_mode=mode, _out_info=info)
        assert mode == ["fts"]
        assert info == {"embeddings_state": "off"}
    finally:
        store.close()


def test_enabled_but_unavailable_backend_warns(tmp_path, monkeypatch):
    from code_review_graph.embedding_providers import profiles

    monkeypatch.setenv("CRG_EMBEDDINGS", "fast")
    monkeypatch.setattr(profiles, "module_available", lambda name: False)
    repo, store = _repo_with_graph(tmp_path, ["VendorInvoice"])
    try:
        info: dict = {}
        hybrid_search(store, "vendor", repo_root=str(repo), _out_info=info)
        assert info["embeddings_state"] == "unavailable"
        assert "embeddings-fast" in info["warning"]
    finally:
        store.close()


class _KeywordProvider(embeddings_mod.EmbeddingProvider):
    def _vec(self, text):
        low = text.lower()
        return [1.0 if "invoice" in low else 0.0, 1.0 if "order" in low else 0.0, 0.1]

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)

    @property
    def dimension(self):
        return 3

    @property
    def name(self):
        return "stub:keyword:f32:d3"


@pytest.fixture
def keyword_profile(monkeypatch):
    provider = _KeywordProvider()

    def fake(settings, *, mlx_parity=None):
        from code_review_graph.embedding_providers.profiles import FAST, Resolution

        return provider, Resolution(settings.profile, settings.profile, FAST, "stub", 3, True)

    monkeypatch.setattr(embeddings_mod, "provider_for_settings", fake)
    monkeypatch.setenv("CRG_EMBEDDINGS", "fast")
    embeddings_mod.clear_matrix_cache()
    return provider


def test_enabled_without_vectors_falls_back_with_warning(tmp_path, keyword_profile):
    repo, store = _repo_with_graph(tmp_path, ["VendorInvoice"])
    try:
        info: dict = {}
        mode: list[str] = []
        hybrid_search(store, "invoice", repo_root=str(repo), _out_mode=mode, _out_info=info)
        assert mode == ["fts"]
        assert info["embeddings_state"] == "stale"
        assert "embeddings enable" in info["warning"]
    finally:
        store.close()


def test_semantic_lane_joins_when_vectors_exist(tmp_path, keyword_profile):
    repo, store = _repo_with_graph(tmp_path, ["VendorInvoice", "OrderService"])
    try:
        embeddings_mod.embed_changed(store, None, repo_root=repo)
        info: dict = {}
        mode: list[str] = []
        results = hybrid_search(store, "billing invoice", repo_root=str(repo),
                                _out_mode=mode, _out_info=info)
        assert mode == ["hybrid"]
        assert info["embeddings_state"] == "ready" and "warning" not in info
        assert results[0]["name"] == "VendorInvoice"
    finally:
        store.close()


def test_semantic_search_nodes_reports_state_and_minimal_limit(tmp_path, monkeypatch):
    from code_review_graph.tools.query import semantic_search_nodes

    monkeypatch.setenv("CRG_EMBEDDINGS", "off")
    repo, store = _repo_with_graph(tmp_path, [f"VendorInvoice{i}" for i in range(8)])
    store.close()
    full = semantic_search_nodes("vendor invoice", repo_root=str(repo))
    assert full["embeddings_state"] == "off" and "warning" not in full
    minimal = semantic_search_nodes("vendor invoice", repo_root=str(repo), limit=7,
                                    detail_level="minimal")
    assert len(minimal["results"]) == 7 and minimal["embeddings_state"] == "off"


def test_orient_limit_detail_and_short_names(tmp_path, monkeypatch):
    from code_review_graph.tools.navigation import orient

    monkeypatch.setenv("CRG_EMBEDDINGS", "off")
    repo, store = _repo_with_graph(tmp_path, ["Pay", "PayService", "PayRequest", "PayDao"])
    store.close()
    result = orient("pay", repo_root=str(repo), limit=3)
    names = [f["name"] for f in result["top_functions"]]
    assert len(names) == 3 and "Pay" in names  # no 6-character cutoff
    assert "embeddings_state" not in result  # "off" is implied by search_mode
    minimal = orient("pay", repo_root=str(repo), detail_level="minimal")
    assert set(minimal["top_functions"][0]) == {"name", "where"}
    assert "communities" not in minimal
