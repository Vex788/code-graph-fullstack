"""Schema migration framework for the code-review-graph SQLite database.

Manages incremental schema changes via versioned migration functions.
Each migration is idempotent (uses IF NOT EXISTS / column existence checks)
and runs in its own ``BEGIN IMMEDIATE`` transaction.

Callers that open an existing database must hold the writer lock
(``locking.writer_lock``) before calling :func:`run_migrations`;
``GraphStore`` raises :class:`SchemaMigrationPending` when it cannot.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Graph content generation. Bump when an indexer change needs a full rebuild.
# Replaces the ``cpp_identity_version`` key, which is still read for compat.
INDEX_GENERATION = 1
_LEGACY_GENERATION_KEY = "cpp_identity_version"
_LEGACY_GENERATION_VALUE = "1"

# Oldest schema a reader must understand to read a database written at the
# latest schema. v10 only drops indexes and adds a column and triggers, so a
# v9 reader still reads it.
READER_COMPAT = 9

# Oldest schema version whose code may write to a database at the latest schema.
MIN_WRITER_VERSION = 10

# How long a reader that finds a pending migration waits for the writer lock.
MIGRATION_LOCK_WAIT_SECONDS = 30.0


class SchemaTooNewError(RuntimeError):
    """The database was written by newer code than this build understands."""

    def __init__(self, db_version: int, code_version: int, detail: str = "") -> None:
        self.db_version = db_version
        self.code_version = code_version
        super().__init__(
            f"graph database schema v{db_version} is newer than this build "
            f"supports (v{code_version}){detail}; upgrade code-review-graph"
        )


class SchemaMigrationPending(RuntimeError):  # noqa: N818 - name is part of the contract
    """The database needs a migration but another writer holds the lock.

    Tools report this as ``building`` (a writer is active) or
    ``rebuild_required``; retrying after the writer finishes succeeds.
    """

    def __init__(self, current: int, latest: int, holder_pid: Optional[int] = None) -> None:
        self.current = current
        self.latest = latest
        self.holder_pid = holder_pid
        holder = f" by pid {holder_pid}" if holder_pid else ""
        super().__init__(
            f"graph database schema v{current} needs migration to v{latest}, "
            f"but the writer lock is held{holder}"
        )


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version from the metadata table.

    Returns:
        int: The schema version (0 if metadata table doesn't exist, 1 if not set).
    """
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 1
        return int(row[0] if isinstance(row, (tuple, list)) else row["value"])
    except sqlite3.OperationalError:
        # metadata table doesn't exist
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the schema version in the metadata table."""
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


_KNOWN_TABLES = frozenset({
    "nodes", "edges", "metadata", "communities", "flows", "flow_memberships", "nodes_fts",
    "community_summaries", "flow_snapshots", "risk_index",
})


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    cursor = conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
    columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
    return column in columns


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """``ALTER TABLE ADD COLUMN`` that tolerates a concurrent or earlier add."""
    if _has_column(conn, table, column):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")  # noqa: S608
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
        logger.warning("Column %s.%s already exists", table, column)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name = ?",
        (table,),
    ).fetchone()
    return row[0] > 0


# ---------------------------------------------------------------------------
# Migration functions
# ---------------------------------------------------------------------------


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v2: Add signature column to nodes table."""
    _add_column(conn, "nodes", "signature", "TEXT")
    logger.info("Migration v2: added 'signature' column to nodes")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3: Create flows and flow_memberships tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entry_point_id INTEGER NOT NULL,
            depth INTEGER NOT NULL,
            node_count INTEGER NOT NULL,
            file_count INTEGER NOT NULL,
            criticality REAL NOT NULL DEFAULT 0.0,
            path_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_memberships (
            flow_id INTEGER NOT NULL,
            node_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (flow_id, node_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_criticality ON flows(criticality DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_entry ON flows(entry_point_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_memberships_node ON flow_memberships(node_id)"
    )
    logger.info("Migration v3: created flows and flow_memberships tables")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4: Create communities table, add community_id to nodes."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER,
            cohesion REAL NOT NULL DEFAULT 0.0,
            size INTEGER NOT NULL DEFAULT 0,
            dominant_language TEXT,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    _add_column(conn, "nodes", "community_id", "INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_community ON nodes(community_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_cohesion ON communities(cohesion DESC)"
    )
    logger.info("Migration v4: created communities table")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v5: Create FTS5 virtual table for nodes."""
    if not _table_exists(conn, "nodes_fts"):
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                content='nodes', content_rowid='rowid',
                tokenize='porter unicode61'
            )
        """)
        logger.info("Migration v5: created nodes_fts FTS5 virtual table")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v6: Add pre-computed summary tables for token-efficient queries."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS community_summaries (
            community_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            purpose TEXT DEFAULT '',
            key_symbols TEXT DEFAULT '[]',
            risk TEXT DEFAULT 'unknown',
            size INTEGER DEFAULT 0,
            dominant_language TEXT DEFAULT '',
            FOREIGN KEY (community_id) REFERENCES communities(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_snapshots (
            flow_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            entry_point TEXT NOT NULL,
            critical_path TEXT DEFAULT '[]',
            criticality REAL DEFAULT 0.0,
            node_count INTEGER DEFAULT 0,
            file_count INTEGER DEFAULT 0,
            FOREIGN KEY (flow_id) REFERENCES flows(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_index (
            node_id INTEGER PRIMARY KEY,
            qualified_name TEXT NOT NULL,
            risk_score REAL DEFAULT 0.0,
            caller_count INTEGER DEFAULT 0,
            test_coverage TEXT DEFAULT 'unknown',
            security_relevant INTEGER DEFAULT 0,
            last_computed TEXT DEFAULT '',
            FOREIGN KEY (node_id) REFERENCES nodes(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_risk_index_score "
        "ON risk_index(risk_score DESC)"
    )
    logger.info("Migration v6: created summary tables "
                "(community_summaries, flow_snapshots, risk_index)")


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v7: Add compound edge indexes for summary and risk queries."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_target_kind "
        "ON edges(target_qualified, kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_source_kind "
        "ON edges(source_qualified, kind)"
    )
    logger.info("Migration v7: added compound edge indexes")


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """v8: Add composite index on edges for upsert_edge performance."""
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_edges_composite
        ON edges(kind, source_qualified, target_qualified, file_path, line)
    """)
    logger.info("Migration v8: created composite edge index")


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """v9: Add confidence scoring to edges."""
    _add_column(conn, "edges", "confidence", "REAL DEFAULT 1.0")
    _add_column(conn, "edges", "confidence_tier", "TEXT DEFAULT 'EXTRACTED'")
    logger.info("Migration v9: added edge confidence columns")


# ---------------------------------------------------------------------------
# Name tokens and trigger-maintained FTS (v10)
# ---------------------------------------------------------------------------

_NAME_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+[0-9]*|[A-Z]+[0-9]*|[0-9]+")
NAME_TOKENS_SQL_FUNCTION = "crg_name_tokens"


def split_name_tokens(name: Optional[str]) -> str:
    """Split camelCase, PascalCase and snake_case into lowercase words.

    ``VendorInvoiceActionBean`` -> ``vendor invoice action bean``;
    ``HTTPServer`` -> ``http server``; ``Item2`` -> ``item2 item``.
    """
    if not name:
        return ""
    words: list[str] = []
    for tok in _NAME_TOKEN_RE.findall(name):
        tok = tok.lower()
        words.append(tok)
        stem = tok.rstrip("0123456789")
        # ``item0`` is also findable as ``item``.
        if stem and stem != tok:
            words.append(stem)
    return " ".join(words)


def register_sql_functions(conn: sqlite3.Connection) -> None:
    """Register SQL functions the FTS triggers call. Needed on every writer."""
    conn.create_function(
        NAME_TOKENS_SQL_FUNCTION, 1, split_name_tokens, deterministic=True,
    )


FTS_COLUMNS = ("name", "qualified_name", "file_path", "signature", "name_tokens")
FTS_TRIGGERS = ("nodes_fts_sync_ins", "nodes_fts_sync_del", "nodes_fts_sync_upd")
# Row-level triggers from an older release; they would double-index next to ours.
_LEGACY_FTS_TRIGGERS = ("nodes_fts_ai", "nodes_fts_ad", "nodes_fts_au")

FTS_TABLE_SQL = """
    CREATE VIRTUAL TABLE nodes_fts USING fts5(
        name, qualified_name, file_path, signature, name_tokens,
        content='nodes', content_rowid='rowid',
        tokenize='porter unicode61'
    )
"""

# External-content FTS: a 'delete' must repeat exactly the indexed values.
# name_tokens is always crg_name_tokens(name), so it is recomputed, never
# trusted from the writer.
_FTS_TRIGGER_SQL = (
    """
    CREATE TRIGGER IF NOT EXISTS nodes_fts_sync_ins AFTER INSERT ON nodes BEGIN
        UPDATE nodes SET name_tokens = crg_name_tokens(new.name)
        WHERE id = new.id AND name_tokens IS NOT crg_name_tokens(new.name);
        INSERT INTO nodes_fts(rowid, name, qualified_name, file_path, signature, name_tokens)
        VALUES (new.id, new.name, new.qualified_name, new.file_path, new.signature,
                crg_name_tokens(new.name));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS nodes_fts_sync_del AFTER DELETE ON nodes BEGIN
        INSERT INTO nodes_fts(nodes_fts, rowid, name, qualified_name, file_path,
                              signature, name_tokens)
        VALUES ('delete', old.id, old.name, old.qualified_name, old.file_path,
                old.signature, old.name_tokens);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS nodes_fts_sync_upd AFTER UPDATE ON nodes
    WHEN old.name IS NOT new.name OR old.qualified_name IS NOT new.qualified_name
      OR old.file_path IS NOT new.file_path OR old.signature IS NOT new.signature
    BEGIN
        INSERT INTO nodes_fts(nodes_fts, rowid, name, qualified_name, file_path,
                              signature, name_tokens)
        VALUES ('delete', old.id, old.name, old.qualified_name, old.file_path,
                old.signature, old.name_tokens);
        UPDATE nodes SET name_tokens = crg_name_tokens(new.name)
        WHERE id = new.id AND name_tokens IS NOT crg_name_tokens(new.name);
        INSERT INTO nodes_fts(rowid, name, qualified_name, file_path, signature, name_tokens)
        VALUES (new.id, new.name, new.qualified_name, new.file_path, new.signature,
                crg_name_tokens(new.name));
    END
    """,
)


def drop_fts_triggers(conn: sqlite3.Connection) -> None:
    for name in FTS_TRIGGERS + _LEGACY_FTS_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")  # noqa: S608


def create_fts_triggers(conn: sqlite3.Connection) -> None:
    for name in _LEGACY_FTS_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")  # noqa: S608
    for sql in _FTS_TRIGGER_SQL:
        conn.execute(sql)


def rebuild_fts_in_transaction(conn: sqlite3.Connection) -> int:
    """Recreate ``nodes_fts`` from ``nodes`` inside the caller's transaction.

    Triggers are dropped first so the token backfill does not churn the old
    index, and re-created last. Returns the number of indexed rows.
    """
    register_sql_functions(conn)
    drop_fts_triggers(conn)
    conn.execute(
        "UPDATE nodes SET name_tokens = crg_name_tokens(name) "
        "WHERE name_tokens IS NOT crg_name_tokens(name)"
    )
    conn.execute("DROP TABLE IF EXISTS nodes_fts")
    conn.execute(FTS_TABLE_SQL)
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    create_fts_triggers(conn)
    return int(conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0])


def _unique_index_covers(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():  # noqa: S608
        name, unique = row[1], row[2]
        if not unique:
            continue
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')").fetchall()]
        if cols and cols[0] == column:
            return True
    return False


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def get_index_generation(conn: sqlite3.Connection) -> Optional[int]:
    """Read ``index_generation``, falling back to the legacy identity key."""
    try:
        value = _get_meta(conn, "index_generation")
        if value is None:
            legacy = _get_meta(conn, _LEGACY_GENERATION_KEY)
            return INDEX_GENERATION if legacy == _LEGACY_GENERATION_VALUE else None
        return int(value)
    except (sqlite3.OperationalError, ValueError):
        return None


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """v10: drop redundant indexes, add name_tokens, trigger-maintained FTS,
    compatibility and write-epoch metadata."""
    # (source, kind) / (target, kind) cover single-column lookups by prefix.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_target_kind ON edges(target_qualified, kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_source_kind ON edges(source_qualified, kind)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_edges_source")
    conn.execute("DROP INDEX IF EXISTS idx_edges_target")
    if _unique_index_covers(conn, "nodes", "qualified_name"):
        conn.execute("DROP INDEX IF EXISTS idx_nodes_qualified")
    else:
        logger.warning("Migration v10: no UNIQUE index on nodes.qualified_name; "
                       "keeping idx_nodes_qualified")

    _add_column(conn, "nodes", "name_tokens", "TEXT")
    rebuild_fts_in_transaction(conn)

    legacy = _get_meta(conn, _LEGACY_GENERATION_KEY)
    has_nodes = conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone() is not None
    # A populated graph without the identity key came from an older indexer.
    generation = (
        INDEX_GENERATION
        if legacy == _LEGACY_GENERATION_VALUE or not has_nodes
        else 0
    )
    conn.executemany(
        "INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?)",
        [
            ("index_generation", str(generation)),
            ("write_epoch_open", "0"),
            ("write_epoch_closed", "0"),
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        [
            ("reader_compat", str(READER_COMPAT)),
            ("min_writer_version", str(MIN_WRITER_VERSION)),
        ],
    )
    logger.info("Migration v10: dropped redundant indexes, trigger-maintained FTS "
                "with name_tokens, compat metadata")


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v2,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
    7: _migrate_v7,
    8: _migrate_v8,
    9: _migrate_v9,
    10: _migrate_v10,
}

LATEST_VERSION = max(MIGRATIONS.keys())


def get_reader_compat(conn: sqlite3.Connection) -> Optional[int]:
    try:
        value = _get_meta(conn, "reader_compat")
        return None if value is None else int(value)
    except (sqlite3.OperationalError, ValueError):
        return None


def check_readable(conn: sqlite3.Connection) -> int:
    """Return the schema version, or raise when this build cannot read it."""
    version = get_schema_version(conn)
    if version > LATEST_VERSION:
        compat = get_reader_compat(conn)
        if compat is None or compat > LATEST_VERSION:
            raise SchemaTooNewError(version, LATEST_VERSION, f" (reader_compat {compat})")
    return version


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run all pending migrations in order.

    Each migration runs in its own ``BEGIN IMMEDIATE`` transaction together
    with its schema_version bump, so a failure rolls back completely. The
    version is re-read inside the transaction, so a concurrent migrator that
    got there first turns the step into a no-op.

    Raises:
        SchemaTooNewError: the database is newer than this build.
    """
    current = get_schema_version(conn)
    if current > LATEST_VERSION:
        raise SchemaTooNewError(current, LATEST_VERSION)
    if current >= LATEST_VERSION:
        return

    logger.info("Schema version %d -> %d: running migrations", current, LATEST_VERSION)
    register_sql_functions(conn)
    if conn.in_transaction:
        conn.commit()
    saved_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        for version in sorted(MIGRATIONS.keys()):
            if version <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                if get_schema_version(conn) >= version:
                    conn.execute("COMMIT")
                    continue
                logger.info("Running migration v%d", version)
                MIGRATIONS[version](conn)
                _set_schema_version(conn, version)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                logger.error("Migration v%d failed, rolled back", version, exc_info=True)
                raise
    finally:
        conn.isolation_level = saved_isolation

    logger.info("Migrations complete, now at schema version %d", LATEST_VERSION)
