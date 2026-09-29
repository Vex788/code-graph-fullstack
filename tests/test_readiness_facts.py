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


def test_capture_failure_flag_is_stale_even_with_matching_head(tmp_path):
    """A write that could not capture HEAD never reads as fresh."""
    from code_review_graph.readiness import compute_readiness

    repo, db = _repo(tmp_path, {"git_capture_failed": "1"})
    facts = _gather(repo, db)
    assert facts.git_capture_failed is True
    assert facts.head_commit == facts.built_at_commit  # anchor happens to match
    readiness = compute_readiness(facts)
    assert readiness.status.value == "stale_graph"
    assert "git_capture_failed" in readiness.reasons


def test_untracked_file_is_not_a_source_match(tmp_path):
    repo, db = _repo(tmp_path)
    (repo / "new.py").write_text("def fresh():\n    pass\n", encoding="utf-8")
    assert _gather(repo, db).source_matches is False


def test_edited_indexed_file_is_ok_and_reported(tmp_path):
    """Edited bytes keep every symbol findable: ok, with the file listed."""
    from code_review_graph.readiness_facts import gather_report

    repo, db = _repo(tmp_path)
    (repo / "app.py").write_text("def handle():\n    return 2\n", encoding="utf-8")
    report = gather_report(repo, db)
    assert report.facts.source_matches is True
    assert report.readiness.status.value == "ok"
    identity = report.source_identity
    assert identity["source_matches_build"] is True
    assert identity["mismatched_indexed_paths"] == [str(repo / "app.py")]
    assert identity["edited_indexed_count"] == 1


def test_new_untracked_java_file_is_stale_worktree(tmp_path):
    from code_review_graph.readiness_facts import gather_report

    repo, db = _repo(tmp_path)
    (repo / "Fresh.java").write_text("class Fresh {}\n", encoding="utf-8")
    report = gather_report(repo, db)
    assert report.readiness.status.value == "stale_worktree"
    assert report.source_identity["missing_indexed_paths"] == [str(repo / "Fresh.java")]


def test_deleted_indexed_file_is_stale_worktree(tmp_path):
    from code_review_graph.readiness_facts import gather_report

    repo, db = _repo(tmp_path)
    (repo / "app.py").unlink()
    report = gather_report(repo, db)
    assert report.readiness.status.value == "stale_worktree"
    assert report.source_identity["deleted_indexed_paths"] == [str(repo / "app.py")]
    assert report.source_identity["source_matches_build"] is False


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


def _embed_rows(db: Path, provider: str, meta: dict[str, str]) -> None:
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings (qualified_name TEXT PRIMARY KEY, "
        "vector BLOB NOT NULL, text_hash TEXT NOT NULL, provider TEXT)"
    )
    qn = conn.execute("SELECT qualified_name FROM nodes WHERE kind='Function'").fetchone()[0]
    conn.execute("INSERT INTO embeddings VALUES (?, x'00', 'h', ?)", (qn, provider))
    conn.executemany(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", list(meta.items()),
    )
    conn.commit()
    conn.close()


def _embeddings_value(repo: Path, db: Path) -> str:
    from code_review_graph.readiness import compute_readiness

    return compute_readiness(_gather(repo, db)).embeddings.value


def test_disabled_embeddings_with_kept_vectors_read_off(tmp_path):
    repo, db = _repo(tmp_path)
    _embed_rows(db, "p", {"embeddings_state": "off", "embeddings_provider": "p"})
    assert _embeddings_value(repo, db) == "off"


def test_unavailable_embeddings_state_is_reported(tmp_path):
    repo, db = _repo(tmp_path)
    _embed_rows(db, "p", {"embeddings_state": "unavailable"})
    assert _embeddings_value(repo, db) == "unavailable"


def test_only_current_provider_vectors_count(tmp_path):
    repo, db = _repo(tmp_path)
    _embed_rows(db, "old", {"embeddings_state": "ready", "embeddings_provider": "new"})
    assert _embeddings_value(repo, db) == "stale"


def test_current_provider_vectors_are_ready(tmp_path):
    repo, db = _repo(tmp_path)
    _embed_rows(db, "new", {"embeddings_state": "ready", "embeddings_provider": "new",
                            "embeddings_stale_count": "0"})
    assert _embeddings_value(repo, db) == "ready"


def test_recorded_stale_count_wins_over_row_count(tmp_path):
    repo, db = _repo(tmp_path)
    _embed_rows(db, "new", {"embeddings_state": "stale", "embeddings_provider": "new",
                            "embeddings_stale_count": "3"})
    assert _embeddings_value(repo, db) == "stale"


def test_recorded_state_without_vectors_is_stale(tmp_path):
    repo, db = _repo(tmp_path, {"embeddings_state": "ready", "embeddings_provider": "p"})
    assert _embeddings_value(repo, db) == "stale"
