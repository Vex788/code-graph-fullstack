"""clone-graph: seed a worktree graph from another checkout, re-rooting every path."""

from __future__ import annotations

import os
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
    assert result["status"] == "ok" and result["temp_replaced"] is True
    assert _text_hits(target_db, str(seed) + "/") == {}
    assert _fts_integrity_error(target_db) is None

    conn = sqlite3.connect(target_db)
    try:
        # The private copy runs journal_mode=OFF; the published graph must not.
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] != "off"
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


@pytest.mark.parametrize("failing", ["rewrite_root", "rebuild_fts"])
def test_failure_mid_rewrite_publishes_no_graph(seed: Path, tmp_path: Path, monkeypatch, failing):
    def killed(*_args, **_kwargs):
        raise RuntimeError("killed mid-rewrite")

    worktree = _worktree(tmp_path)
    target_db = get_db_path(worktree)
    monkeypatch.setattr(f"code_review_graph.clone_graph.{failing}", killed)
    with pytest.raises(RuntimeError, match="killed"):
        clone_graph(seed, worktree, update=False)
    assert [p.name for p in target_db.parent.glob("graph.db*")] == ["graph.db.lock"]

    monkeypatch.undo()
    clone_graph(seed, worktree, update=False)
    published = target_db.read_bytes()
    monkeypatch.setattr(f"code_review_graph.clone_graph.{failing}", killed)
    with pytest.raises(RuntimeError, match="killed"):
        clone_graph(seed, worktree, update=False, force=True)
    assert target_db.read_bytes() == published
    assert sorted(p.name for p in target_db.parent.glob("graph.db*")) == [
        "graph.db", "graph.db.lock",
    ]


def test_failed_publish_does_not_drop_the_old_wal(seed: Path, tmp_path: Path, monkeypatch):
    worktree = _worktree(tmp_path)
    clone_graph(seed, worktree, update=False)
    target_db = get_db_path(worktree)

    # Commit a row and die without a clean close: the frame lives only in -wal.
    probe = (
        "import os, sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1])\n"
        "conn.execute('PRAGMA journal_mode=WAL')\n"
        "conn.execute('PRAGMA wal_autocheckpoint=0')\n"
        "conn.execute('CREATE TABLE IF NOT EXISTS probe (v INTEGER)')\n"
        "conn.execute('INSERT INTO probe VALUES (7)')\n"
        "conn.commit()\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", probe, str(target_db)], check=True)
    assert Path(f"{target_db}-wal").exists()

    real_replace = os.replace

    def failing_replace(source, destination):
        if Path(destination) == target_db:
            raise OSError("simulated publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated"):
        clone_graph(seed, worktree, update=False, force=True)
    monkeypatch.undo()

    assert not list(target_db.parent.glob("*.replacing")), "parked sidecars must be restored"
    conn = sqlite3.connect(target_db)
    try:
        assert conn.execute("SELECT v FROM probe").fetchone() == (7,)
    finally:
        conn.close()


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


@pytest.mark.parametrize(
    ("old", "new", "updates"),
    [("/r", "/w", 1), ("/r", "/r/w/x", 2), ("/r/w/x", "/r", 2)],
)
def test_rewrite_passes_depend_on_root_nesting(old: str, new: str, updates: int):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE risk_index (qualified_name TEXT UNIQUE)")
    conn.execute("INSERT INTO risk_index VALUES (?)", (old + "/a.py::f",))
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    rewrite_root(conn, old, new)
    assert sum(s.startswith("UPDATE") for s in seen) == updates
    assert conn.execute("SELECT qualified_name FROM risk_index").fetchone()[0] == new + "/a.py::f"


@pytest.mark.parametrize("new", ["/w", "/r/w/x"])
def test_edges_are_copied_with_indexes_and_sequence(new: str):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
        "source_qualified TEXT NOT NULL, target_qualified TEXT NOT NULL, "
        "file_path TEXT NOT NULL, line INTEGER DEFAULT 0, extra TEXT DEFAULT '{}')"
    )
    conn.execute("CREATE INDEX idx_edges_file ON edges(file_path)")
    insert = (
        "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, extra) "
        "VALUES ('CALLS', ?, ?, ?, ?)"
    )
    conn.execute(insert, ("/r/a.py::f", "/r/b.py::g", "/r/a.py", '{"p": "/r/c.py"}'))
    conn.execute(insert, ("x", "/other/y", "/r/w/x/a.py", None))
    conn.execute(insert, ("gone", "gone", "gone", "{}"))
    conn.execute("DELETE FROM edges WHERE id = 3")

    changed = rewrite_root(conn, "/r", new)

    assert changed == {"edges.source_qualified": 1, "edges.target_qualified": 1,
                       "edges.file_path": 2, "edges.extra": 1}
    assert conn.execute(
        "SELECT id, source_qualified, target_qualified, file_path, extra FROM edges ORDER BY id"
    ).fetchall() == [
        (1, f"{new}/a.py::f", f"{new}/b.py::g", f"{new}/a.py", f'{{"p": "{new}/c.py"}}'),
        (2, "x", "/other/y", f"{new}/w/x/a.py", None),
    ]
    assert [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'edges'"
    )] == ["idx_edges_file"]
    conn.execute(insert, ("n", "n", "n", "{}"))
    assert conn.execute("SELECT max(id) FROM edges").fetchone()[0] == 4


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
