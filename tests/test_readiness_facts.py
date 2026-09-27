"""gather_facts: metadata, lock probe and git state feed compute_readiness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore, NodeInfo
from code_review_graph.locking import writer_lock
from code_review_graph.migrations import INDEX_GENERATION
from code_review_graph.readiness import GIT_NOT_A_REPO, GIT_OK, GIT_UNAVAILABLE

_IDENTITY = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo(tmp_path: Path, metadata: dict[str, str] | None = None) -> tuple[Path, Path]:
    """A one-commit git repo whose graph indexed app.py at HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD")
    db = repo / ".code-review-graph" / "graph.db"
    import hashlib

    digest = hashlib.sha256((repo / "app.py").read_bytes()).hexdigest()
    store = GraphStore(db)
    try:
        path = str(repo / "app.py")
        store.upsert_node(NodeInfo(
            kind="File", name="app.py", file_path=path, line_start=1, line_end=2,
            language="python",
        ), file_hash=digest)
        store.upsert_node(NodeInfo(
            kind="Function", name="handle", file_path=path, line_start=1, line_end=2,
            language="python",
        ), file_hash=digest)
        values = {
            "git_head_sha": head,
            "indexed_dirty_paths": "[]",
            "last_updated": "2026-01-01T00:00:00",
        }
        values.update(metadata or {})
        for key, value in values.items():
            store.set_metadata(key, value)
        store.commit()
    finally:
        store.close()
    return repo, db


def _gather(repo: Path, db: Path):
    from code_review_graph.readiness_facts import gather_facts

    return gather_facts(repo, db)


def test_clean_graph_is_ok(tmp_path):
    from code_review_graph.readiness import compute_readiness

    repo, db = _repo(tmp_path)
    facts = _gather(repo, db)
    assert facts.git_state == GIT_OK
    assert facts.head_commit == facts.built_at_commit
    assert facts.source_matches is True
    assert facts.index_generation == INDEX_GENERATION
    assert compute_readiness(facts).status.value == "ok"


def test_absent_epoch_and_failure_keys_read_as_closed_and_empty(tmp_path):
    repo, db = _repo(tmp_path)
    facts = _gather(repo, db)
    assert facts.write_epoch_open == facts.write_epoch_closed
    assert (facts.failed_files, facts.resolver_failures) == (0, 0)


def test_open_epoch_and_failures_are_partial(tmp_path):
    from code_review_graph.readiness import compute_readiness

    repo, db = _repo(tmp_path, {
        "write_epoch_open": "3",
        "write_epoch_closed": "2",
        "failed_files": json.dumps(["a.py", "b.py"]),
        "resolver_failures": json.dumps({"jsp": "boom"}),
    })
    facts = _gather(repo, db)
    assert (facts.write_epoch_open, facts.write_epoch_closed) == (3, 2)
    assert (facts.failed_files, facts.resolver_failures) == (2, 1)
    readiness = compute_readiness(facts)
    assert readiness.status.value == "partial_index"
    assert {"write_epoch_open", "failed_files", "resolver_failures"} <= set(readiness.reasons)


def test_built_at_commit_key_wins_over_legacy_sha(tmp_path):
    repo, db = _repo(tmp_path, {"built_at_commit": "f" * 40})
    facts = _gather(repo, db)
    assert facts.built_at_commit == "f" * 40


def test_untracked_file_is_not_a_source_match(tmp_path):
    repo, db = _repo(tmp_path)
    (repo / "new.py").write_text("def fresh():\n    pass\n", encoding="utf-8")
    assert _gather(repo, db).source_matches is False


def test_git_timeout_is_unavailable_never_clean(tmp_path, monkeypatch):
    from code_review_graph.readiness import compute_readiness

    repo, db = _repo(tmp_path)
    real_run = subprocess.run

    def slow_status(cmd, *args, **kwargs):
        if list(cmd[:2]) == ["git", "status"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow_status)
    facts = _gather(repo, db)
    assert facts.git_state == GIT_UNAVAILABLE
    assert facts.source_matches is not True
    assert compute_readiness(facts).status.value == "stale_graph"


def test_head_failure_is_unavailable(tmp_path, monkeypatch):
    repo, db = _repo(tmp_path)

    def broken(cmd, *args, **kwargs):
        raise OSError("git missing")

    monkeypatch.setattr(subprocess, "run", broken)
    assert _gather(repo, db).git_state == GIT_UNAVAILABLE


def test_non_vcs_root_is_not_a_repo(tmp_path):
    root = tmp_path / "plain"
    db = root / ".code-review-graph" / "graph.db"
    GraphStore(db).close()
    assert _gather(root, db).git_state == GIT_NOT_A_REPO


def test_missing_db_is_missing_graph(tmp_path):
    from code_review_graph.readiness import compute_readiness

    facts = _gather(tmp_path, tmp_path / "nope" / "graph.db")
    assert facts.graph_exists is False
    assert compute_readiness(facts).status.value == "missing_graph"
    assert not (tmp_path / "nope").exists()


def test_held_writer_lock_is_building(tmp_path):
    repo, db = _repo(tmp_path)
    with writer_lock(db):
        assert _gather(repo, db).building is True
    assert _gather(repo, db).building is False


def test_stale_index_generation_requires_rebuild(tmp_path):
    from code_review_graph.readiness import compute_readiness

    repo, db = _repo(tmp_path, {"index_generation": str(INDEX_GENERATION + 5)})
    readiness = compute_readiness(_gather(repo, db))
    assert readiness.status.value == "rebuild_required"


def test_older_schema_reads_as_migration_pending(tmp_path):
    from code_review_graph.readiness import compute_readiness

    repo, db = _repo(tmp_path)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE metadata SET value = '9' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    facts = _gather(repo, db)
    assert facts.schema_current is False
    assert "schema_migration_pending" in compute_readiness(facts).reasons


def test_schema_too_new_raises(tmp_path):
    import sqlite3

    from code_review_graph.migrations import SchemaTooNewError

    repo, db = _repo(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    conn.execute("DELETE FROM metadata WHERE key = 'reader_compat'")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaTooNewError):
        _gather(repo, db)


def test_embeddings_state(tmp_path):
    from code_review_graph.readiness import compute_readiness

    repo, db = _repo(tmp_path)
    assert compute_readiness(_gather(repo, db)).embeddings.value == "off"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings (qualified_name TEXT PRIMARY KEY, "
        "vector BLOB NOT NULL, text_hash TEXT NOT NULL, provider TEXT)"
    )
    qn = conn.execute("SELECT qualified_name FROM nodes WHERE kind='Function'").fetchone()[0]
    conn.execute("INSERT INTO embeddings VALUES (?, x'00', 'h', 'p')", (qn,))
    conn.commit()
    conn.close()
    assert compute_readiness(_gather(repo, db)).embeddings.value == "ready"
