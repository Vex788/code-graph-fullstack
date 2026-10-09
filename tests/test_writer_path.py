"""Writer path: epoch protocol, failure retries, atomic resolvers, storage, faults."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import incremental
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    FAULT_STAGES,
    full_build,
    get_db_path,
    incremental_update,
)
from code_review_graph.migrations import FTS_TRIGGERS, INDEX_GENERATION
from code_review_graph.readiness import compute_readiness
from code_review_graph.readiness_facts import gather_facts

from .witness.conftest import build, copy_fixture, git, open_store

USER_SERVICE = "src/main/java/com/acme/service/UserService.java"


def _meta(repo: Path) -> dict[str, str]:
    conn = sqlite3.connect(get_db_path(repo))
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM metadata")}
    finally:
        conn.close()


def _fts_integrity_error(db: Path) -> str | None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO nodes_fts(nodes_fts, rank) VALUES('integrity-check', 1)")
        return None
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()


def _append_method(repo: Path, relative: str, method: str) -> None:
    path = repo / relative
    source = path.read_text(encoding="utf-8").rstrip()
    path.write_text(
        source[:-1] + f"\n    public void {method}() {{\n    }}\n}}\n", encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path / "app")


# ---------------------------------------------------------------------------
# Epoch protocol and stamp
# ---------------------------------------------------------------------------


def test_build_closes_its_epoch_with_one_stamp(repo: Path):
    result = build(repo)
    meta = _meta(repo)

    assert result["status"] == "ok"
    assert meta["write_epoch_open"] == meta["write_epoch_closed"] == "1"
    assert json.loads(meta["failed_files"]) == []
    assert json.loads(meta["resolver_failures"]) == {}
    assert meta["index_generation"] == str(INDEX_GENERATION)
    assert meta["git_head_sha"] == git(repo, "rev-parse", "HEAD").strip()
    assert "cpp_identity_version" not in meta
    assert compute_readiness(gather_facts(repo, get_db_path(repo))).status.value == "ok"

    _append_method(repo, USER_SERVICE, "archive")
    assert build(repo, full=False)["status"] == "ok"
    meta = _meta(repo)
    assert meta["write_epoch_open"] == meta["write_epoch_closed"] == "2"


def test_resolver_failure_keeps_the_epoch_stamp_partial(repo: Path, monkeypatch):
    from code_review_graph import resolvers

    def broken(store, repo_root):
        raise RuntimeError("resolver crashed")

    original = resolvers.RESOLVERS["jsp"]
    monkeypatch.setitem(resolvers.RESOLVERS, "jsp", (broken, *original[1:]))
    result = build(repo)

    assert result["status"] == "partial"
    assert json.loads(_meta(repo)["resolver_failures"]) == {
        "jsp": "RuntimeError: resolver crashed",
    }
    facts = gather_facts(repo, get_db_path(repo))
    assert compute_readiness(facts).status.value == "partial_index"

    # The failed resolver is retried by the next update even with no changes.
    monkeypatch.setitem(resolvers.RESOLVERS, "jsp", original)
    retried = build(repo, full=False)
    assert retried["status"] == "ok"
    assert retried["jsp_resolution"] is not None
    assert json.loads(_meta(repo)["resolver_failures"]) == {}


def test_parse_failure_is_recorded_and_retried(repo: Path, monkeypatch):
    from code_review_graph.parser import CodeParser

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    original = CodeParser.parse_bytes

    def flaky(self, path, source):
        if Path(path).name == "UserService.java":
            raise RuntimeError("transient parse failure")
        return original(self, path, source)

    monkeypatch.setattr(CodeParser, "parse_bytes", flaky)
    first = build(repo)
    assert first["status"] == "partial"
    assert json.loads(_meta(repo)["failed_files"]) == [USER_SERVICE]
    facts = gather_facts(repo, get_db_path(repo))
    assert facts.failed_files == 1
    assert compute_readiness(facts).status.value == "partial_index"

    # Still failing: stays recorded, nothing else changes.
    again = build(repo, full=False)
    assert again["status"] == "partial"
    assert again["retried_failed_files"] == 1

    monkeypatch.setattr(CodeParser, "parse_bytes", original)
    fixed = build(repo, full=False)
    assert fixed["status"] == "ok"
    assert json.loads(_meta(repo)["failed_files"]) == []


def test_injected_stage_failure_leaves_the_epoch_open(repo: Path, monkeypatch):
    assert build(repo)["status"] == "ok"
    _append_method(repo, USER_SERVICE, "archive")
    git(repo, "commit", "-qam", "edit")
    monkeypatch.setenv("CRG_FAULT_AT", "resolvers")

    with pytest.raises(RuntimeError, match="injected fault"):
        build(repo, full=False)

    meta = _meta(repo)
    assert int(meta["write_epoch_open"]) > int(meta["write_epoch_closed"])
    facts = gather_facts(repo, get_db_path(repo))
    assert compute_readiness(facts).status.value == "partial_index"

    # Recovery: the next update sees the open epoch and re-runs everything.
    monkeypatch.delenv("CRG_FAULT_AT")
    recovered = build(repo, full=False)
    assert recovered["status"] == "ok"
    assert recovered["jsp_resolution"] is not None
    assert compute_readiness(gather_facts(repo, get_db_path(repo))).status.value == "ok"


def test_generation_mismatch_requires_a_rebuild_without_writing(repo: Path):
    build(repo, postprocess="none")
    store = GraphStore(get_db_path(repo))
    try:
        store.set_metadata("index_generation", str(INDEX_GENERATION + 1))
        before = _meta(repo)
        result = incremental_update(repo, store, changed_files=[USER_SERVICE])
    finally:
        store.close()

    assert result["status"] == "rebuild_required"
    assert result["rebuild_required"] is True
    assert result["reason"] == "index_generation_mismatch"
    assert _meta(repo) == before
    tool = build(repo, full=False)
    assert tool["status"] == "rebuild_required"


# ---------------------------------------------------------------------------
# Atomic resolvers
# ---------------------------------------------------------------------------


def test_failing_resolver_rolls_back_to_its_previous_edges(repo: Path, monkeypatch):
    from code_review_graph import resolvers
    from code_review_graph.resolvers import run_resolver

    build(repo, postprocess="none")
    store = GraphStore(get_db_path(repo))
    try:
        def count() -> int:
            return store._conn.execute(
                "SELECT count(*) FROM edges WHERE kind IN ('RENDERS', 'REQUESTS', 'INCLUDES')"
            ).fetchone()[0]

        before = count()
        assert before > 0

        def half_done(store, repo_root):
            store._conn.execute("DELETE FROM edges WHERE kind = 'RENDERS'")
            store.commit()  # a resolver's own commit must not escape
            raise RuntimeError("died after deleting")

        monkeypatch.setitem(
            resolvers.RESOLVERS, "jsp", (half_done, *resolvers.RESOLVERS["jsp"][1:]),
        )
        failures: dict[str, str] = {}
        assert run_resolver("jsp", store, repo, failures) is None
        assert failures == {"jsp": "RuntimeError: died after deleting"}
        assert count() == before
        assert not store._conn.in_transaction
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_reparse_keeps_node_ids_and_communities(repo: Path):
    build(repo, postprocess="none")
    store = GraphStore(get_db_path(repo))
    try:
        path = str(repo / USER_SERVICE)

        def rows() -> dict[str, tuple]:
            return {
                r[0]: (r[1], r[2]) for r in store._conn.execute(
                    "SELECT qualified_name, id, community_id FROM nodes WHERE file_path = ?",
                    (path,),
                )
            }

        store._conn.execute("UPDATE nodes SET community_id = 42 WHERE file_path = ?", (path,))
        before = rows()
        _append_method(repo, USER_SERVICE, "archive")
        result = incremental_update(repo, store, changed_files=[USER_SERVICE])
        after = rows()
    finally:
        store.close()

    assert result["files_updated"] == 1
    added = set(after) - set(before)
    assert [qn.rsplit(".", 1)[-1] for qn in added] == ["archive"]
    assert {qn: after[qn] for qn in before} == before
    assert _fts_integrity_error(get_db_path(repo)) is None


def test_locked_database_is_retried_not_a_parse_error(repo: Path, monkeypatch):
    monkeypatch.setattr("code_review_graph.graph._LOCKED_RETRY_DELAYS", (0.3, 0.3, 0.3))
    store = GraphStore(get_db_path(repo))
    store._conn.execute("PRAGMA busy_timeout=50")
    blocker = sqlite3.connect(get_db_path(repo), timeout=1, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.5, blocker.rollback)
    release.start()
    try:
        result = full_build(repo, store)
    finally:
        release.join()
        blocker.close()
        store.close()
    assert result["status"] == "ok"
    assert result["errors"] == []


def test_database_that_stays_locked_fails_the_build(repo: Path, monkeypatch):
    monkeypatch.setattr("code_review_graph.graph._LOCKED_RETRY_DELAYS", (0.05,))
    build(repo, postprocess="none")
    store = GraphStore(get_db_path(repo))
    store._conn.execute("PRAGMA busy_timeout=50")
    blocker = sqlite3.connect(get_db_path(repo), timeout=1)
    original = GraphStore.store_file_batch

    def store_after_foreign_lock(self, batch):
        # A writer that bypasses the writer lock grabs SQLite between batches.
        if not blocker.in_transaction:
            blocker.execute("BEGIN IMMEDIATE")
        return original(self, batch)

    monkeypatch.setattr(GraphStore, "store_file_batch", store_after_foreign_lock)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            full_build(repo, store)
    finally:
        blocker.rollback()
        blocker.close()
        store.close()
    meta = _meta(repo)
    assert int(meta["write_epoch_open"]) > int(meta["write_epoch_closed"])


# ---------------------------------------------------------------------------
# Update cost
# ---------------------------------------------------------------------------


def test_noop_update_skips_the_stale_file_sweep(repo: Path, monkeypatch):
    build(repo, postprocess="none")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("stale sweep ran on a no-op update")

    monkeypatch.setattr(incremental, "_reconcile_stale_files", forbidden)
    result = build(repo, full=False)
    assert result["status"] == "ok"
    assert "write_epoch" not in result


def test_update_removes_a_file_deleted_outside_git(repo: Path):
    build(repo, postprocess="none")
    scratch = repo / "src/main/java/com/acme/Scratch.java"
    scratch.write_text("package com.acme;\npublic class Scratch {}\n", encoding="utf-8")
    store = GraphStore(get_db_path(repo))
    try:
        # Indexed through an explicit list, as the watcher does; git never saw it.
        incremental_update(repo, store, changed_files=[str(scratch.relative_to(repo))])
    finally:
        store.close()
    assert _count_file_nodes(repo, scratch) == 1

    scratch.unlink()
    result = build(repo, full=False, postprocess="none")
    assert result["status"] == "ok"
    assert _count_file_nodes(repo, scratch) == 0


def _count_file_nodes(repo: Path, path: Path) -> int:
    conn = sqlite3.connect(get_db_path(repo))
    try:
        return conn.execute(
            "SELECT count(*) FROM nodes WHERE kind = 'File' AND file_path = ?",
            (path.as_posix(),),
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


def test_debounce_flushes_a_steady_event_stream_by_max_wait():
    pytest.importorskip("watchdog")
    batches: list[tuple[float, int]] = []
    debouncer = incremental._make_debouncer(
        lambda events: batches.append((time.monotonic(), len(events))),
        interval=0.3,
        max_wait=0.8,
    )
    debouncer.start()
    started = time.monotonic()
    try:
        while time.monotonic() - started < 2.0:
            debouncer.handle_event(object())
            time.sleep(0.1)
    finally:
        debouncer.stop()
        debouncer.join()
    assert batches, "no batch was flushed while events kept arriving"
    assert batches[0][0] - started < 1.5


def test_auto_watch_thread_restarts_after_a_failure(tmp_path: Path, monkeypatch):
    pytest.importorskip("watchdog")
    calls: list[int] = []
    stop = threading.Event()

    def flaky_watch(repo_root, store, on_files_updated=None, stop_event=None):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        stop.set()

    monkeypatch.setattr(incremental, "watch", flaky_watch)
    thread = incremental.start_watch_thread(tmp_path, None, stop_event=stop)  # type: ignore[arg-type]
    assert thread is not None
    thread.join(timeout=10)
    assert len(calls) == 2


def test_watch_batch_skips_a_file_that_vanished_before_parsing(repo: Path, monkeypatch):
    build(repo, postprocess="none")
    ghost = "src/main/java/com/acme/Ghost.java"
    real_read = Path.read_bytes

    def vanishing(self: Path) -> bytes:
        if self.name == "Ghost.java":
            raise FileNotFoundError(self)
        return real_read(self)

    (repo / ghost).write_text("package com.acme;\npublic class Ghost {}\n", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", vanishing)
    store = GraphStore(get_db_path(repo))
    try:
        result = incremental_update(repo, store, changed_files=[ghost], reconcile_stale=False)
    finally:
        store.close()
    assert result["errors"] == []
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Fault injection: SIGKILL at every stage never leaves an "ok" graph
# ---------------------------------------------------------------------------


def _run_cli(repo: Path, command: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "code_review_graph", command, "--repo", str(repo)],
        env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
@pytest.mark.parametrize("command", ["build", "update"])
@pytest.mark.parametrize("stage", FAULT_STAGES)
def test_sigkill_at_any_stage_never_reads_ok(repo: Path, stage: str, command: str):
    env = dict(os.environ)
    env["CRG_SERIAL_PARSE"] = "1"
    if command == "update":
        assert _run_cli(repo, "build", env).returncode == 0
        _append_method(repo, USER_SERVICE, "archive")
        git(repo, "commit", "-qam", "edit")

    killed = _run_cli(repo, command, {**env, "CRG_FAULT_AT": stage, "CRG_FAULT_MODE": "kill"})
    assert killed.returncode == -9, killed.stdout + killed.stderr

    db = get_db_path(repo)
    readiness = compute_readiness(gather_facts(repo, db))
    assert readiness.status.value in ("partial_index", "building"), readiness
    assert _fts_integrity_error(db) is None

    recovered = _run_cli(repo, "update", env)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert compute_readiness(gather_facts(repo, db)).status.value == "ok"
    assert _fts_integrity_error(db) is None


# ---------------------------------------------------------------------------
# Anchor capture: a failed capture must never read as fresh
# ---------------------------------------------------------------------------


def _set_meta(repo: Path, key: str, value: str) -> None:
    conn = sqlite3.connect(get_db_path(repo))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)", (key, value)
        )
        conn.commit()
    finally:
        conn.close()


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def test_update_after_commit_restamps_anchor(repo: Path):
    build(repo)

    _append_method(repo, USER_SERVICE, "anchorProbe")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "move head")

    result = build(repo, full=False)
    meta = _meta(repo)

    assert result["status"] == "ok", result
    assert meta["git_head_sha"] == _head(repo)
    assert "git_capture_failed" not in meta
    assert compute_readiness(gather_facts(repo, get_db_path(repo))).status.value == "ok"


def test_failed_capture_keeps_old_anchor_and_flags(repo: Path):
    build(repo)
    _append_method(repo, USER_SERVICE, "captureProbe")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "move head")
    stale_anchor = _meta(repo)["git_head_sha"]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(incremental, "_git_branch_info", lambda root: ("", ""))
        result = build(repo, full=False)
    meta = _meta(repo)

    assert result["status"] == "ok", result
    assert meta["git_head_sha"] == stale_anchor  # not silently kept looking fresh
    assert meta["git_capture_failed"] == "1"
    readiness = compute_readiness(gather_facts(repo, get_db_path(repo)))
    assert readiness.status.value == "stale_graph"
    assert {"git_capture_failed", "head_moved"} <= set(readiness.reasons)

    # Healthy git on the next update restamps and clears the flag.
    recovered = build(repo, full=False)
    meta = _meta(repo)
    assert recovered["status"] == "ok", recovered
    assert meta["git_head_sha"] == _head(repo)
    assert "git_capture_failed" not in meta


def test_capture_flag_forces_restamp_on_clean_diff(repo: Path):
    """Flagged graph, clean tree: the no-op exit must not keep the flag."""
    build(repo)
    _set_meta(repo, "git_capture_failed", "1")

    result = build(repo, full=False)
    meta = _meta(repo)

    assert result["status"] == "ok", result
    assert "No changes detected" not in result.get("summary", "")
    assert "git_capture_failed" not in meta
    assert meta["git_head_sha"] == _head(repo)


def test_git_diff_failure_is_an_error_not_up_to_date(repo: Path, monkeypatch):
    build(repo)
    anchor = _meta(repo)["git_head_sha"]
    _append_method(repo, USER_SERVICE, "diffProbe")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "move head")

    monkeypatch.setattr(incremental, "_git_diff_output", lambda *args, **kwargs: None)
    result = build(repo, full=False)

    assert result["status"] == "error", result
    assert result["reason"] == "git_diff_failed"
    assert "No changes detected" not in result.get("summary", "")
    assert _meta(repo)["git_head_sha"] == anchor  # untouched, honestly stale


def test_changed_files_strict_separates_failure_from_empty(repo: Path, monkeypatch):
    from code_review_graph.incremental import get_changed_files, get_changed_files_strict

    assert get_changed_files_strict(repo, "HEAD~1") == []  # healthy git, empty diff

    monkeypatch.setattr(incremental, "_git_diff_output", lambda *args, **kwargs: None)
    assert get_changed_files_strict(repo, "HEAD~1") is None  # git failed
    assert get_changed_files(repo, "HEAD~1") == []  # legacy wrapper keeps its shape


def test_capture_flag_is_inert_on_a_non_git_root(tmp_path: Path):
    """A flag left by a repo that stopped being git never reads as stale."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    # The registered-root shape: a graph without any VCS marker.
    (root / ".code-review-graph").mkdir()
    build(root)
    _set_meta(root, "git_capture_failed", "1")

    result = build(root, full=False)
    facts = gather_facts(root, get_db_path(root))

    assert result["status"] == "ok", result
    assert facts.git_capture_failed is False
    readiness = compute_readiness(facts)
    # A non-git root stays drift-unverifiable (pre-existing stale_worktree);
    # the flag itself must contribute nothing.
    assert "git_capture_failed" not in readiness.reasons
    assert readiness.status.value != "stale_graph"


def test_symbolic_base_at_head_keeps_the_noop_fast_path(repo: Path):
    from code_review_graph.tools.build import build_or_update_graph

    build(repo)

    result = build_or_update_graph(repo_root=str(repo), base="HEAD", postprocess="none")

    assert result["status"] == "ok", result
    assert "No changes detected" in result["summary"]


# ---------------------------------------------------------------------------
# FTS drift on a no-op update (#1104)
# ---------------------------------------------------------------------------


def _drop_fts_triggers(repo: Path) -> None:
    """Leave the graph in the state an interrupted bulk load leaves behind.

    The row triggers are what keep ``nodes_fts`` in step with ``nodes``; without
    them every write after this point is invisible to search.
    """
    conn = sqlite3.connect(get_db_path(repo))
    try:
        for name in FTS_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")  # nosec B608 - fixed names
        conn.commit()
    finally:
        conn.close()


def _unindexed_symbols(repo: Path) -> set[str]:
    """Graph symbols that a valid FTS lookup cannot find."""
    store = open_store(repo)
    try:
        missing = {
            str(row[0])
            for row in store._conn.execute(
                "SELECT name FROM nodes WHERE id NOT IN (SELECT id FROM nodes_fts_docsize)"
            )
        }
        for name in missing:
            found = store._conn.execute(
                "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH ?",
                (f'"{name}"',),
            ).fetchone()[0]
            assert not found, f"{name} is indexed but does not match its own row"
        return missing
    finally:
        store.close()


def test_noop_update_repairs_unsynced_fts(repo: Path):
    """A no-op update must repair FTS drift, not certify the stale index."""
    build(repo)
    _drop_fts_triggers(repo)
    _append_method(repo, USER_SERVICE, "driftProbe")
    git(repo, "commit", "-qam", "edit")
    assert build(repo, full=False, postprocess="none")["files_updated"] == 1
    assert _unindexed_symbols(repo), "precondition: the new symbol is not searchable"

    result = build(repo, full=False, postprocess="minimal")

    assert result["status"] == "ok", result
    assert "No changes detected" in result["summary"]
    assert result.get("fts_repaired") is True, result
    assert not _unindexed_symbols(repo)


def test_noop_update_reports_skipped_fts_repair(repo: Path):
    """--skip-postprocess stays cheap but says the index is not maintained."""
    build(repo)
    _drop_fts_triggers(repo)

    result = build(repo, full=False, postprocess="none")

    assert result["status"] == "ok", result
    assert result.get("fts_stale") is True, result
    assert "fts_repaired" not in result


def test_skip_postprocess_update_reports_the_drift_it_creates(repo: Path):
    """The update that leaves the index unsynced says so, not just the next one."""
    build(repo)
    _drop_fts_triggers(repo)
    _append_method(repo, USER_SERVICE, "skipProbe")
    git(repo, "commit", "-qam", "edit")

    result = build(repo, full=False, postprocess="none")

    assert result["status"] == "ok", result
    assert result.get("fts_stale") is True, result
    assert _unindexed_symbols(repo), "the reported drift must be the real one"


def test_noop_update_with_pending_flows_repairs_unsynced_fts(repo: Path):
    """The other no-op branch repairs drift through the post-processing gate.

    ``postprocess="full"`` with a pending flows delta diverts into
    ``_run_postprocess``, which owns its own FTS gate. That coupling is what
    keeps this sub-path covered, so pin it rather than trust it.
    """
    from code_review_graph.incremental import read_flows_stale

    build(repo)
    _drop_fts_triggers(repo)
    _append_method(repo, USER_SERVICE, "flowsProbe")
    git(repo, "commit", "-qam", "edit")
    # --skip-postprocess writes the nodes, leaves the index unsynced, and keeps
    # the flows delta pending: both properties the no-op branch keys on.
    assert build(repo, full=False, postprocess="none")["files_updated"] == 1
    store = open_store(repo)
    try:
        assert read_flows_stale(store) is not None, "precondition: flows are pending"
    finally:
        store.close()
    assert _unindexed_symbols(repo), "precondition: the new symbol is not searchable"

    result = build(repo, full=False, postprocess="full")

    assert result["status"] == "ok", result
    assert "No changes detected" in result["summary"]
    assert result.get("fts_rebuilt") is True, result
    assert not _unindexed_symbols(repo)


def test_failed_fts_rebuild_on_noop_update_never_reports_success(repo: Path):
    """A rebuild that raises is drift, not a warning nobody reads."""
    build(repo)
    _append_method(repo, USER_SERVICE, "boomProbe")
    git(repo, "commit", "-qam", "edit")
    assert build(repo, full=False, postprocess="none")["files_updated"] == 1
    _drop_fts_triggers(repo)

    with patch(
        "code_review_graph.search.rebuild_fts_index",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        result = build(repo, full=False, postprocess="minimal")

    assert result["status"] == "ok", result  # the build itself is not the failure
    assert "No changes detected" in result["summary"]
    assert result.get("fts_stale") is True, result
    assert "fts_repaired" not in result
    assert any("FTS" in w for w in result["warnings"]), result


def test_failed_fts_rebuild_in_postprocess_pipeline_never_reports_success(repo: Path):
    """The same holds inside _run_postprocess, which owns the other branch."""
    build(repo)
    _drop_fts_triggers(repo)
    _append_method(repo, USER_SERVICE, "boomProbe2")
    git(repo, "commit", "-qam", "edit")

    with patch(
        "code_review_graph.search.rebuild_fts_index",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        result = build(repo, full=False, postprocess="minimal")

    assert result.get("fts_stale") is True, result
    assert result.get("fts_repaired") is not True, result
    assert result.get("fts_rebuilt") is not True, result


def test_healthy_noop_update_does_not_touch_fts(repo: Path):
    """The drift check must stay free on a graph whose index is in sync."""
    build(repo)
    _append_method(repo, USER_SERVICE, "cheapProbe")
    git(repo, "commit", "-qam", "edit")
    assert build(repo, full=False, postprocess="minimal")["files_updated"] == 1
    assert not _unindexed_symbols(repo)

    with patch(
        "code_review_graph.search.rebuild_fts_index",
        side_effect=AssertionError("healthy no-op update rebuilt FTS"),
    ) as rebuild:
        result = build(repo, full=False, postprocess="minimal")

    assert result["status"] == "ok", result
    assert "fts_repaired" not in result
    assert result.get("fts_stale") is not True
    rebuild.assert_not_called()
