"""clone-graph: seed a worktree graph from another checkout, re-rooting every path."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.clone_graph import clone_graph, resolve_seed
from code_review_graph.incremental import get_db_path
from code_review_graph.path_columns import PATH_COLUMNS, is_registered, rewrite_root
from tests.witness.conftest import build, copy_fixture

# FTS shadow tables hold tokenised copies of nodes; they are rebuilt, not rewritten.
_FTS_TABLES = {"nodes_fts", "nodes_fts_data", "nodes_fts_idx", "nodes_fts_docsize",
               "nodes_fts_config"}


@pytest.fixture(scope="module")
def seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    home = tmp_path_factory.mktemp("crg-home-clone")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CRG_HOME", str(home))
        repo = copy_fixture(tmp_path_factory.mktemp("seed") / "app")
        assert build(repo)["status"] == "ok"
        db = get_db_path(repo)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS embeddings (qualified_name TEXT PRIMARY KEY, "
                "vector BLOB NOT NULL, text_hash TEXT NOT NULL, provider TEXT NOT NULL "
                "DEFAULT 'unknown')"
            )
            rows = conn.execute(
                "SELECT qualified_name FROM nodes WHERE kind = 'Function' LIMIT 5"
            ).fetchall()
            conn.executemany(
                "INSERT INTO embeddings VALUES (?, ?, 'h', 'test')",
                [(qn, str(repo).encode()) for (qn,) in rows],
            )
            conn.commit()
        finally:
            conn.close()
    return repo


def _worktree(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path / "worktree")


def _fts_integrity_error(db: Path) -> str | None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO nodes_fts(nodes_fts, rank) VALUES('integrity-check', 1)")
        return None
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()


def _text_hits(db: Path, needle: str) -> dict[str, int]:
    """Every non-FTS text column still containing *needle*."""
    conn = sqlite3.connect(db)
    hits: dict[str, int] = {}
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            if table in _FTS_TABLES:
                continue
            for column in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:
                count = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE typeof({column}) = 'text' "  # nosec B608
                    f"AND instr({column}, ?) > 0",
                    (needle,),
                ).fetchone()[0]
                if count:
                    hits[f"{table}.{column}"] = count
    finally:
        conn.close()
    return hits


def test_seed_paths_live_only_in_registered_columns(seed: Path):
    hits = _text_hits(get_db_path(seed), str(seed))
    assert hits, "fixture graph stores no absolute paths; the scan proves nothing"
    unregistered = [key for key in hits if not is_registered(*key.split("."))]
    assert unregistered == []


def test_clone_rewrites_every_path_and_keeps_fts_consistent(seed: Path, tmp_path: Path):
    worktree = _worktree(tmp_path)
    result = clone_graph(seed, worktree, update=False)
    target_db = get_db_path(worktree)

    assert result["target_db"] == str(target_db)
    assert _text_hits(target_db, str(seed) + "/") == {}
    assert _fts_integrity_error(target_db) is None

    conn = sqlite3.connect(target_db)
    try:
        def fts_hits(token: str) -> int:
            return conn.execute(
                "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH ?",
                (f"file_path : {token}",),
            ).fetchone()[0]

        assert (fts_hits("worktree") > 0, fts_hits("seed")) == (True, 0)
        qualified = [r[0] for r in conn.execute("SELECT qualified_name FROM nodes")]
        assert qualified and all(
            q.startswith(str(worktree) + "/") for q in qualified if "/" in q
        )
        embedded = [r[0] for r in conn.execute("SELECT qualified_name FROM embeddings")]
        assert len(embedded) == 5
        assert all(q.startswith(str(worktree) + "/") for q in embedded)
        # Vectors are BLOBs and are never rewritten.
        assert {r[0] for r in conn.execute("SELECT vector FROM embeddings")} == {
            str(seed).encode()
        }
        meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
    finally:
        conn.close()
    seed_conn = sqlite3.connect(get_db_path(seed))
    seed_sha = seed_conn.execute(
        "SELECT value FROM metadata WHERE key = 'git_head_sha'"
    ).fetchone()[0]
    seed_conn.close()
    assert meta["git_head_sha"] == seed_sha
    assert meta["write_epoch_open"] == meta["write_epoch_closed"]
    assert meta["cloned_from"] == str(seed)


def test_query_by_new_path_works(seed: Path, tmp_path: Path):
    from code_review_graph.graph import GraphStore

    worktree = _worktree(tmp_path)
    clone_graph(seed, worktree, update=False)
    relative = "src/main/java/com/acme/service/UserService.java"
    store = GraphStore(get_db_path(worktree))
    try:
        names = {n.name for n in store.get_nodes_by_file(str(worktree / relative))}
        assert "UserService" in names
        assert store.get_nodes_by_file(str(seed / relative)) == []
        found = store._conn.execute(
            "SELECT n.file_path FROM nodes_fts f JOIN nodes n ON n.id = f.rowid "
            "WHERE nodes_fts MATCH ?", ("name : UserService",),
        ).fetchall()
        assert found and all(str(worktree) in row[0] for row in found)
    finally:
        store.close()


def test_clone_then_update_is_ready(seed: Path, tmp_path: Path, monkeypatch):
    from code_review_graph.readiness_facts import gather_report

    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    worktree = _worktree(tmp_path)
    result = clone_graph(seed, worktree)
    assert result["update"]["status"] == "ok"
    report = gather_report(worktree, get_db_path(worktree))
    assert report.facts.head_commit == report.facts.built_at_commit
    assert report.facts.source_matches is True


def test_existing_target_needs_force(seed: Path, tmp_path: Path):
    worktree = _worktree(tmp_path)
    clone_graph(seed, worktree, update=False)
    with pytest.raises(FileExistsError):
        clone_graph(seed, worktree, update=False)
    assert clone_graph(seed, worktree, update=False, force=True)["status"] == "ok"


def test_seed_db_path_and_seed_root(seed: Path, tmp_path: Path):
    root, db = resolve_seed(get_db_path(seed))
    assert (root, db) == (seed, get_db_path(seed))
    loose = tmp_path / "loose.db"
    loose.write_bytes(get_db_path(seed).read_bytes())
    with pytest.raises(ValueError, match="seed-root"):
        resolve_seed(loose)
    assert resolve_seed(loose, seed_root=seed)[0] == seed
    with pytest.raises(FileNotFoundError):
        resolve_seed(tmp_path / "absent")


def test_nested_target_does_not_collide(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (qualified_name TEXT UNIQUE, file_path TEXT, name TEXT, "
                 "signature TEXT, parent_name TEXT, extra TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?, ?, 'n', NULL, NULL, '{}')", [
        ("/r/a.py::f", "/r/a.py"),
        ("/r/w/x/a.py::f", "/r/w/x/a.py"),
    ])
    changed = rewrite_root(conn, "/r", "/r/w/x")
    assert changed["nodes.qualified_name"] == 2
    assert sorted(r[0] for r in conn.execute("SELECT qualified_name FROM nodes")) == [
        "/r/w/x/a.py::f", "/r/w/x/w/x/a.py::f",
    ]


def test_sibling_prefix_is_not_rewritten():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE edges (source_qualified TEXT, target_qualified TEXT, "
                 "file_path TEXT, extra TEXT)")
    conn.execute(
        "INSERT INTO edges VALUES ('/r/app2/a.py::f', '/r/app/b.py::g', '/r/app/b.py', "
        "'{\"path\": \"/r/app/c.py\", \"other\": \"/r/app2/d.py\"}')"
    )
    rewrite_root(conn, "/r/app", "/w")
    assert conn.execute("SELECT * FROM edges").fetchone() == (
        "/r/app2/a.py::f", "/w/b.py::g", "/w/b.py",
        '{"path": "/w/c.py", "other": "/r/app2/d.py"}',
    )


def test_registry_has_no_duplicates_and_known_modes():
    keys = [(c.table, c.column) for c in PATH_COLUMNS]
    assert len(keys) == len(set(keys))
    assert {c.mode for c in PATH_COLUMNS} <= {"path", "json"}
    for required in ("embeddings.qualified_name", "flows.path_json", "risk_index.qualified_name"):
        assert is_registered(*required.split("."))


def test_cli_clone_graph(seed: Path, tmp_path: Path):
    worktree = _worktree(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "clone-graph",
         "--from", str(seed), "--to", str(worktree), "--no-update"],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Cloned graph" in completed.stdout
    assert _fts_integrity_error(get_db_path(worktree)) is None
    again = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "clone-graph",
         "--from", str(seed), "--to", str(worktree), "--no-update"],
        capture_output=True, text=True, timeout=120,
    )
    assert again.returncode == 1
    assert "--force" in again.stderr
