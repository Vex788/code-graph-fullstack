"""Tests for the schema migration framework."""

import sqlite3
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    get_schema_version,
    run_migrations,
)


class TestMigrations:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fresh_db_gets_latest_version(self):
        """A newly created DB should be at the latest schema version."""
        version = get_schema_version(self.store._conn)
        assert version == LATEST_VERSION

    def test_v1_db_migrates_to_latest(self):
        """A v1 database should migrate to latest when GraphStore is opened."""
        # Close the store that was already migrated
        self.store.close()

        # Manually create a v1 database (base schema only, version=1)
        conn = sqlite3.connect(str(self.tmp.name))
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
        # Drop migration artifacts to simulate v1
        conn.execute("DROP TABLE IF EXISTS flows")
        conn.execute("DROP TABLE IF EXISTS flow_memberships")
        conn.execute("DROP TABLE IF EXISTS communities")
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.execute("DROP TABLE IF EXISTS community_summaries")
        conn.execute("DROP TABLE IF EXISTS flow_snapshots")
        conn.execute("DROP TABLE IF EXISTS risk_index")
        conn.commit()
        conn.close()

        # Re-open with GraphStore — should trigger migrations
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

    def test_migration_is_idempotent(self):
        """Opening GraphStore twice should leave schema at latest version."""
        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

    def test_signature_column_exists_after_migration(self):
        """The nodes table should have a 'signature' column after migration."""
        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "signature" in columns

    def test_flows_table_exists_after_migration(self):
        """The flows and flow_memberships tables should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "flows" in tables
        assert "flow_memberships" in tables

    def test_communities_table_exists_after_migration(self):
        """The communities table should exist and nodes should have community_id."""
        tables = _get_table_names(self.store._conn)
        assert "communities" in tables

        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "community_id" in columns

    def test_fts5_table_exists_after_migration(self):
        """The nodes_fts FTS5 virtual table should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "nodes_fts" in tables

    def test_get_schema_version_no_metadata_table(self):
        """get_schema_version returns 0 when metadata table doesn't exist."""
        conn = sqlite3.connect(":memory:")
        assert get_schema_version(conn) == 0
        conn.close()

    def test_get_schema_version_no_key(self):
        """get_schema_version returns 1 when metadata exists but key is missing."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        assert get_schema_version(conn) == 1
        conn.close()

    def test_migrations_dict_covers_all_versions(self):
        """MIGRATIONS should have entries from 2 to LATEST_VERSION."""
        expected = set(range(2, LATEST_VERSION + 1))
        assert set(MIGRATIONS.keys()) == expected

    def test_run_migrations_on_already_current_db(self):
        """run_migrations should be a no-op on an already-current database."""
        version_before = get_schema_version(self.store._conn)
        run_migrations(self.store._conn)
        version_after = get_schema_version(self.store._conn)
        assert version_before == version_after == LATEST_VERSION


    def test_v6_summary_tables_exist(self):
        """v6 summary tables should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "community_summaries" in tables
        assert "flow_snapshots" in tables
        assert "risk_index" in tables

    def test_v6_migration_idempotent(self):
        """Running v6 migration twice should not fail."""
        from code_review_graph.migrations import _migrate_v6

        _migrate_v6(self.store._conn)
        _migrate_v6(self.store._conn)
        tables = _get_table_names(self.store._conn)
        assert "community_summaries" in tables

    def test_v7_compound_edge_indexes_exist(self):
        """v7 compound edge indexes should exist after migration."""
        rows = self.store._conn.execute("PRAGMA index_list(edges)").fetchall()
        indexes = {row[1] if isinstance(row, tuple) else row["name"] for row in rows}

        assert "idx_edges_target_kind" in indexes
        assert "idx_edges_source_kind" in indexes


def _get_table_names(conn: sqlite3.Connection) -> set[str]:
    """Helper: return all table/view names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {row[0] if isinstance(row, (tuple, list)) else row["name"] for row in rows}


# ---------------------------------------------------------------------------
# v10: index cleanup, name_tokens, trigger-maintained FTS, compat metadata
# ---------------------------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import textwrap  # noqa: E402

import pytest  # noqa: E402

from code_review_graph import graph as graph_module  # noqa: E402
from code_review_graph import migrations as migrations_module  # noqa: E402
from code_review_graph.graph import _SCHEMA_SQL  # noqa: E402
from code_review_graph.locking import TOKEN_ENV  # noqa: E402
from code_review_graph.migrations import (  # noqa: E402
    FTS_TRIGGERS,
    INDEX_GENERATION,
    MIN_WRITER_VERSION,
    READER_COMPAT,
    SchemaMigrationPending,
    SchemaTooNewError,
    _add_column,
    get_index_generation,
    split_name_tokens,
)
from code_review_graph.search import (  # noqa: E402
    disable_fts_triggers,
    enable_fts_triggers,
    rebuild_fts,
)

_V9_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_nodes_qualified ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_qualified);
"""


def _make_v9_db(path, *, nodes=(), legacy_generation=None):
    """Build a database exactly as the v9 code left it."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_SQL + _V9_INDEXES)
    conn.execute("INSERT INTO metadata (key, value) VALUES ('schema_version', '1')")
    for version in range(2, 10):
        MIGRATIONS[version](conn)
    conn.execute("UPDATE metadata SET value = '9' WHERE key = 'schema_version'")
    for name, qn in nodes:
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, updated_at) "
            "VALUES ('Class', ?, ?, 'src/A.java', 0)",
            (name, qn),
        )
    if legacy_generation is not None:
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES ('cpp_identity_version', ?)",
            (legacy_generation,),
        )
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


def _indexes(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def _meta(conn, key):
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _fts_hits(conn, query):
    return [
        row[0] for row in conn.execute(
            "SELECT n.qualified_name FROM nodes_fts f JOIN nodes n ON n.rowid = f.rowid "
            "WHERE nodes_fts MATCH ? ORDER BY n.qualified_name",
            (query,),
        ).fetchall()
    ]


def _fts_integrity(conn):
    conn.execute("INSERT INTO nodes_fts(nodes_fts, rank) VALUES('integrity-check', 1)")


def _add_node(store, name, qn):
    store._conn.execute(
        "INSERT INTO nodes (kind, name, qualified_name, file_path, updated_at) "
        "VALUES ('Class', ?, ?, 'src/X.java', 0)",
        (name, qn),
    )


def test_split_name_tokens():
    assert split_name_tokens("VendorInvoiceActionBean") == "vendor invoice action bean"
    assert split_name_tokens("HTTPServer") == "http server"
    assert split_name_tokens("parse_json") == "parse json"
    assert split_name_tokens("BulkItem2") == "bulk item2 item"
    assert split_name_tokens(None) == ""


def test_v9_db_upgrades_to_v10(tmp_path):
    db = tmp_path / "graph.db"
    _make_v9_db(db, nodes=[("VendorInvoiceActionBean", "A.java::VendorInvoiceActionBean")],
                legacy_generation="1")
    with GraphStore(db) as store:
        conn = store._conn
        assert get_schema_version(conn) == LATEST_VERSION == 10
        edge_idx = _indexes(conn, "edges")
        assert {"idx_edges_source", "idx_edges_target"}.isdisjoint(edge_idx)
        assert {"idx_edges_source_kind", "idx_edges_target_kind"} <= edge_idx
        node_idx = _indexes(conn, "nodes")
        assert "idx_nodes_qualified" not in node_idx
        assert any(name.startswith("sqlite_autoindex_nodes") for name in node_idx)
        row = conn.execute("SELECT name_tokens FROM nodes").fetchone()
        assert row[0] == "vendor invoice action bean"
        assert _fts_hits(conn, "vendor invoice") == ["A.java::VendorInvoiceActionBean"]
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}
        assert set(FTS_TRIGGERS) <= triggers
        assert _meta(conn, "index_generation") == str(INDEX_GENERATION)
        assert _meta(conn, "reader_compat") == str(READER_COMPAT)
        assert _meta(conn, "min_writer_version") == str(MIN_WRITER_VERSION)
        assert _meta(conn, "write_epoch_open") == _meta(conn, "write_epoch_closed")
        _fts_integrity(conn)


def test_v10_generation_from_legacy_key(tmp_path):
    populated = [("Foo", "a::Foo")]
    cases = [
        ("legacy", populated, "1", 1),
        ("unknown", populated, None, 0),
        ("empty", (), None, 1),
    ]
    for label, nodes, legacy, expected in cases:
        db = tmp_path / f"{label}.db"
        _make_v9_db(db, nodes=nodes, legacy_generation=legacy)
        with GraphStore(db) as store:
            assert get_index_generation(store._conn) == expected, label


def test_index_generation_falls_back_to_legacy_key():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    assert get_index_generation(conn) is None
    conn.execute("INSERT INTO metadata VALUES ('cpp_identity_version', '1')")
    assert get_index_generation(conn) == INDEX_GENERATION
    conn.close()


def test_fts_triggers_track_insert_update_delete(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        conn = store._conn
        _add_node(store, "VendorInvoiceActionBean", "x::VendorInvoiceActionBean")
        assert _fts_hits(conn, "vendor invoice") == ["x::VendorInvoiceActionBean"]
        conn.execute(
            "UPDATE nodes SET name = 'PurchaseOrderBean', "
            "qualified_name = 'x::PurchaseOrderBean' WHERE qualified_name = ?",
            ("x::VendorInvoiceActionBean",),
        )
        assert _fts_hits(conn, "vendor") == []
        assert _fts_hits(conn, "purchase order") == ["x::PurchaseOrderBean"]
        # Columns outside the index do not churn FTS.
        conn.execute("UPDATE nodes SET community_id = 7")
        conn.execute("DELETE FROM nodes WHERE qualified_name = 'x::PurchaseOrderBean'")
        assert _fts_hits(conn, "purchase") == []
        assert conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0] == 0
        _fts_integrity(conn)


def test_upsert_keeps_fts_in_sync(tmp_path):
    from code_review_graph.parser import NodeInfo

    with GraphStore(tmp_path / "graph.db") as store:
        node = NodeInfo(kind="Function", name="getUserName", file_path="a.py",
                        line_start=1, line_end=2, language="python")
        store.upsert_node(node)
        store.upsert_node(node)  # conflict path fires the update trigger
        store.commit()
        assert len(_fts_hits(store._conn, "user name")) == 1
        _fts_integrity(store._conn)


def test_bulk_load_helpers(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        conn = store._conn
        disable_fts_triggers(conn)
        for i in range(5):
            _add_node(store, f"BulkItem{i}", f"x::BulkItem{i}")
        assert _fts_hits(conn, "bulk") == []
        assert rebuild_fts(conn) == 5
        assert len(_fts_hits(conn, "bulk item")) == 5
        _add_node(store, "AfterRebuild", "x::AfterRebuild")
        assert _fts_hits(conn, "after rebuild") == ["x::AfterRebuild"]
        enable_fts_triggers(conn)  # idempotent
        _fts_integrity(conn)


def test_v10_drops_legacy_relic_triggers(tmp_path):
    db = tmp_path / "graph.db"
    _make_v9_db(db, nodes=[("Foo", "a::Foo")])
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TRIGGER nodes_fts_ai AFTER INSERT ON nodes BEGIN "
        "INSERT INTO nodes_fts(rowid,name,qualified_name,file_path,signature) "
        "VALUES(new.id,new.name,new.qualified_name,new.file_path,new.signature); END"
    )
    conn.commit()
    conn.close()
    with GraphStore(db) as store:
        names = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}
        assert "nodes_fts_ai" not in names
        _add_node(store, "Bar", "a::Bar")
        assert store._conn.execute(
            "SELECT count(*) FROM nodes_fts WHERE rowid = "
            "(SELECT id FROM nodes WHERE qualified_name = 'a::Bar')").fetchone()[0] == 1


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    _make_v9_db(db, nodes=[("Foo", "a::Foo")])

    def broken(conn):
        _add_column(conn, "nodes", "name_tokens", "TEXT")
        conn.execute("DROP INDEX idx_edges_source")
        raise sqlite3.OperationalError("boom")

    monkeypatch.setitem(MIGRATIONS, 10, broken)
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        GraphStore(db)
    conn = sqlite3.connect(str(db))
    assert get_schema_version(conn) == 9
    columns = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "name_tokens" not in columns
    assert "idx_edges_source" in _indexes(conn, "edges")
    conn.close()
    monkeypatch.undo()
    with GraphStore(db) as store:
        assert get_schema_version(store._conn) == LATEST_VERSION


def test_duplicate_column_is_tolerated(tmp_path, monkeypatch):
    with GraphStore(tmp_path / "graph.db") as store:
        monkeypatch.setattr(migrations_module, "_has_column", lambda *a: False)
        _add_column(store._conn, "nodes", "name_tokens", "TEXT")  # already there
        with pytest.raises(sqlite3.OperationalError):
            _add_column(store._conn, "nodes", "strict_col", "TEXT PRIMARY KEY")


def _stamp(db, key, value):
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def test_newer_schema_refused_unless_reader_compatible(tmp_path):
    db = tmp_path / "graph.db"
    GraphStore(db).close()
    _stamp(db, "schema_version", "99")
    _stamp(db, "reader_compat", "99")
    with pytest.raises(SchemaTooNewError, match="v99"):
        GraphStore(db)
    conn = sqlite3.connect(str(db))
    with pytest.raises(SchemaTooNewError):
        run_migrations(conn)
    conn.close()
    _stamp(db, "reader_compat", str(LATEST_VERSION))
    with GraphStore(db) as store:
        assert get_schema_version(store._conn) == 99


_HOLD_LOCK = textwrap.dedent(
    """
    import sys, time
    from code_review_graph.locking import writer_lock
    with writer_lock(sys.argv[1], wait=0):
        print("locked", flush=True)
        time.sleep(float(sys.argv[2]))
    """
)


def _hold_lock(db, seconds):
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(db), str(seconds)],
        stdout=subprocess.PIPE, text=True, env=env,
    )
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_reader_does_not_migrate_while_writer_holds_lock(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    _make_v9_db(db, nodes=[("Foo", "a::Foo")])
    monkeypatch.setattr(graph_module, "MIGRATION_LOCK_WAIT_SECONDS", 0.2)
    holder = _hold_lock(db, 30)
    try:
        with pytest.raises(SchemaMigrationPending) as exc:
            GraphStore(db)
        assert exc.value.current == 9
        assert exc.value.latest == LATEST_VERSION
        assert exc.value.holder_pid == holder.pid
        conn = sqlite3.connect(str(db))
        assert get_schema_version(conn) == 9
        conn.close()
    finally:
        holder.kill()
        holder.wait(timeout=10)
    with GraphStore(db) as store:
        assert get_schema_version(store._conn) == LATEST_VERSION


def test_current_schema_opens_without_the_lock(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    GraphStore(db).close()
    monkeypatch.setattr(graph_module, "MIGRATION_LOCK_WAIT_SECONDS", 0.2)
    holder = _hold_lock(db, 30)
    try:
        with GraphStore(db) as store:
            assert get_schema_version(store._conn) == LATEST_VERSION
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_memory_db_needs_no_lock_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with GraphStore(":memory:") as store:
        assert get_schema_version(store._conn) == LATEST_VERSION
    assert list(tmp_path.iterdir()) == []
