"""Witnesses for storage safety: concurrent writers, installer merge, FTS after a path rewrite."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import build, copy_fixture

# Child writer A: a slow first file store keeps SQLite's write lock for longer
# than the store's busy_timeout (5 s today), then the build finishes normally.
_SLOW_WRITER = """
import sys, time
from pathlib import Path
from code_review_graph.graph import GraphStore

repo, marker, hold = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3])
original = GraphStore.upsert_node
state = {"slept": False}

def slow_upsert(self, node, file_hash=""):
    if not state["slept"]:
        state["slept"] = True
        marker.write_text("holding")
        time.sleep(hold)
    return original(self, node, file_hash=file_hash)

GraphStore.upsert_node = slow_upsert
sys.argv = ["code-review-graph", "build", "--repo", repo]
from code_review_graph.cli import main
main()
"""


def _child_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CRG_HOME"] = str(tmp_path / "crg-home")
    env["CRG_SERIAL_PARSE"] = "1"
    return env


@pytest.mark.xfail(strict=True, reason="W1/W2a: two writers collide on 'database is locked'")
def test_two_writer_processes_do_not_hit_database_locked(fixture_repo: Path, tmp_path: Path):
    build(fixture_repo, postprocess="none")
    marker = tmp_path / "writer-a-holding"
    env = _child_env(tmp_path)
    writer_a = subprocess.Popen(
        [sys.executable, "-c", _SLOW_WRITER, str(fixture_repo), str(marker), "7"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert writer_a.poll() is None, writer_a.communicate()
            assert time.monotonic() < deadline, "writer A never took the write lock"
            time.sleep(0.05)
        writer_b = subprocess.run(
            [sys.executable, "-m", "code_review_graph", "build", "--repo", str(fixture_repo)],
            env=env, capture_output=True, text=True, timeout=180,
        )
    finally:
        out_a, err_a = writer_a.communicate(timeout=180)
    assert writer_a.returncode == 0, err_a[-2000:]
    output_b = writer_b.stdout + writer_b.stderr
    assert "database is locked" not in output_b, output_b[-2000:]
    # 75 = lock busy, skipped (the plan's exit code for a contended writer).
    assert writer_b.returncode in (0, 75), output_b[-2000:]


def test_hook_install_keeps_jsonc_settings(tmp_path: Path):
    from code_review_graph.skills import _merge_hooks_into_settings

    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    original = (
        "{\n"
        "  // allow read-only shell commands\n"
        '  "permissions": {"allow": ["Bash(ls:*)"]},\n'
        '  "model": "opus"\n'
        "}\n"
    )
    (settings_dir / "settings.json").write_text(original, encoding="utf-8")
    hooks = {"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": []}]}}

    _merge_hooks_into_settings(settings_dir, hooks)

    text = (settings_dir / "settings.json").read_text(encoding="utf-8")
    assert "Bash(ls:*)" in text and '"model"' in text, text


def _naive_path_rewrite(db: Path, old_prefix: str, new_prefix: str) -> list[str]:
    """Rewrite every text column the way bug-hunter's graph_bootstrap does today.

    Returns the per-column errors graph_bootstrap would collect, limited to
    the tables that hold paths.
    """
    errors: list[str] = []
    conn = sqlite3.connect(db)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            for column in [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]:
                try:
                    conn.execute(
                        f"UPDATE {table} SET {column} = replace({column}, ?, ?) "  # nosec B608
                        f"WHERE {column} LIKE ?",
                        (old_prefix, new_prefix, "%" + old_prefix + "%"),
                    )
                except sqlite3.OperationalError as exc:
                    if table in ("nodes", "edges"):
                        errors.append(f"{table}.{column}: {exc}")
        conn.commit()
    finally:
        conn.close()
    return errors


def _fts_integrity_error(db: Path) -> str | None:
    conn = sqlite3.connect(db)
    try:
        # rank=1 also compares an external-content index with its content table.
        conn.execute("INSERT INTO nodes_fts(nodes_fts, rank) VALUES('integrity-check', 1)")
        return None
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()


def test_fts_integrity_control_on_fresh_build(built_fixture: Path):
    from code_review_graph.incremental import get_db_path

    assert _fts_integrity_error(get_db_path(built_fixture)) is None


@pytest.mark.xfail(
    strict=True,
    reason="W1/W2b: a raw path rewrite of a copied graph desyncs nodes_fts (wave0); "
    "since v10 the FTS triggers need crg_name_tokens, so the raw UPDATE fails",
)
def test_fts_integrity_survives_path_rewrite_of_copied_graph(tmp_path: Path):
    from code_review_graph.incremental import get_db_path

    seed = copy_fixture(tmp_path / "seed")
    build(seed, postprocess="minimal")
    worktree = copy_fixture(tmp_path / "worktree")
    seed_db = get_db_path(seed)
    target_db = get_db_path(worktree)
    source = sqlite3.connect(seed_db)
    destination = sqlite3.connect(target_db)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    errors = _naive_path_rewrite(target_db, str(seed), str(worktree))
    assert errors == []

    assert _fts_integrity_error(target_db) is None
    conn = sqlite3.connect(target_db)
    try:
        def hits(token: str) -> int:
            return conn.execute(
                "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH ?",
                (f"file_path : {token}",),
            ).fetchone()[0]

        assert (hits("worktree") > 0, hits("seed")) == (True, 0)
    finally:
        conn.close()
