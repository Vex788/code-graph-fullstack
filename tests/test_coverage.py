"""Tests for code_review_graph/tools/coverage.py: the disk-vs-graph report.

The coverage tool answers one question — does the graph actually contain
every parseable file on disk? — so these tests pin the three core numbers
(missing / stale / excluded), the exclusion classification order, the list
caps, the text renderer, and the CLI exit-code contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import NodeInfo
from code_review_graph.tools.coverage import (
    _capped,
    _display_path,
    _untracked_files,
    coverage_report,
    format_coverage_text,
)

REPORT_KEYS = {
    "status",
    "summary",
    "repo_root",
    "inventory_count",
    "graph_file_count",
    "indexed_count",
    "missing_from_graph",
    "missing_from_graph_total",
    "missing_from_graph_truncated",
    "stale_in_graph",
    "stale_in_graph_total",
    "stale_in_graph_truncated",
    "excluded",
    "excluded_total",
    "by_language",
    "inventory_by_language",
}

EXCLUSION_REASONS = {"ignored", "binary", "no_language", "untracked"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        # ``core.hookspath=`` disables any developer-wide git hooks (a global
        # post-commit that builds a graph would otherwise materialize
        # .code-review-graph inside the fixture repo and break the read-only
        # and exclusion-count assertions).
        ["git", "-c", "core.hookspath=", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (args, result.stderr)
    return result


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    return path.resolve()


def _commit_all(repo: Path, message: str = "commit") -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)


def _file_node(path: Path, language: str) -> NodeInfo:
    return NodeInfo(
        kind="File",
        name=str(path),
        file_path=str(path),
        line_start=1,
        line_end=1,
        language=language,
    )


# ---------------------------------------------------------------------------
# Unit-level helpers
# ---------------------------------------------------------------------------


def test_capped_lists_first_fifty_sorted_with_total_and_flag():
    files = [f"mod{i:03d}.py" for i in range(51)]
    capped = _capped(files)
    assert capped["count"] == 51
    assert capped["files"] == sorted(files)[:50]
    assert capped["truncated"] is True

    exact = _capped(["a.py", "b.py"])
    assert exact == {"count": 2, "files": ["a.py", "b.py"], "truncated": False}


def test_display_path_prefers_repo_relative_spelling(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _display_path(repo, str(repo / "a" / "b.py")) == "a/b.py"
    # Paths from another root are kept verbatim rather than mangled.
    assert _display_path(repo, "/elsewhere/x.py") == "/elsewhere/x.py"


def test_untracked_files_lists_unignored_others(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "loose.py").write_text("x = 1\n")
    # Not a git repository -> the helper degrades to an empty list.
    assert _untracked_files(plain) == []

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("x = 1\n")
    _commit_all(repo)
    (repo / "fresh.py").write_text("x = 2\n")
    (repo / ".gitignore").write_text("ignored/\n")
    hidden_dir = repo / "ignored"
    hidden_dir.mkdir()
    (hidden_dir / "hidden.py").write_text("x = 3\n")

    untracked = _untracked_files(repo)
    assert "fresh.py" in untracked
    assert all(not name.startswith("ignored/") for name in untracked)


# ---------------------------------------------------------------------------
# Report content
# ---------------------------------------------------------------------------


def test_complete_repo_reports_ok_with_language_breakdown(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 1\n")
    (repo / "page.jsp").write_text("<html></html>\n")
    _commit_all(repo)

    store = GraphStore(tmp_path / "graph.db")
    try:
        store.upsert_node(_file_node(repo / "app.py", "python"))
        store.upsert_node(_file_node(repo / "page.jsp", "jsp"))
        store.commit()

        report = coverage_report(str(repo), store=store)
    finally:
        store.close()

    assert report["status"] == "ok"
    assert report["inventory_count"] == 2
    assert report["graph_file_count"] == 2
    assert report["indexed_count"] == 2
    assert report["missing_from_graph"] == []
    assert report["missing_from_graph_total"] == 0
    assert report["stale_in_graph"] == []
    assert report["stale_in_graph_total"] == 0
    assert report["by_language"] == {"jsp": 1, "python": 1}
    assert report["inventory_by_language"] == {"jsp": 1, "python": 1}
    assert set(report) == REPORT_KEYS
    assert set(report["excluded"]) == EXCLUSION_REASONS


def test_missing_vs_stale_math(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "indexed.py").write_text("x = 1\n")
    (repo / "unindexed.py").write_text("x = 2\n")
    _commit_all(repo)

    store = GraphStore(tmp_path / "graph.db")
    try:
        store.upsert_node(_file_node(repo / "indexed.py", "python"))
        # On disk, in the graph, but deleted since the build.
        store.upsert_node(_file_node(repo / "gone.py", "python"))
        store.commit()

        report = coverage_report(str(repo), store=store)
    finally:
        store.close()

    assert report["status"] == "incomplete"
    assert report["inventory_count"] == 2
    assert report["graph_file_count"] == 2
    assert report["indexed_count"] == 1
    assert report["missing_from_graph"] == ["unindexed.py"]
    assert report["missing_from_graph_total"] == 1
    assert report["stale_in_graph"] == ["gone.py"]
    assert report["stale_in_graph_total"] == 1
    assert "1/2 inventory files indexed" in report["summary"]
    assert "1 stale graph files" in report["summary"]


def test_exclusion_classification_order_and_each_reason(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 1\n")
    (repo / "gone.py").write_text("x = 2\n")
    _commit_all(repo)

    # Untracked candidates outside the inventory, one per exclusion reason.
    # None is gitignored, so `git ls-files --others` reports them all and the
    # first-match order in _EXCLUSION_REASONS decides.
    (repo / "extra.py").write_text("x = 3\n")  # parseable -> untracked
    (repo / "notes.txt").write_text("plain notes\n")  # no language
    (repo / "blob.py").write_bytes(b"data\x00\x01binary")  # binary beats untracked
    build_dir = repo / "build"  # CRG default ignore beats binary
    build_dir.mkdir()
    (build_dir / "out.js").write_bytes(b"out\x00")
    # Tracked-but-deleted: not an exclusion decision (stale detection owns it).
    (repo / "gone.py").unlink()

    store = GraphStore(tmp_path / "graph.db")
    try:
        store.upsert_node(_file_node(repo / "app.py", "python"))
        store.commit()
        report = coverage_report(str(repo), store=store)
    finally:
        store.close()

    excluded = report["excluded"]
    assert excluded["ignored"] == {
        "count": 1, "files": ["build/out.js"], "truncated": False,
    }
    assert excluded["binary"] == {
        "count": 1, "files": ["blob.py"], "truncated": False,
    }
    assert excluded["no_language"] == {
        "count": 1, "files": ["notes.txt"], "truncated": False,
    }
    assert excluded["untracked"] == {
        "count": 1, "files": ["extra.py"], "truncated": False,
    }
    assert report["excluded_total"] == 4
    every_excluded = {
        name for entry in excluded.values() for name in entry["files"]
    }
    assert "gone.py" not in every_excluded


def test_missing_list_is_capped_at_fifty_with_true_total(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    for i in range(60):
        (repo / f"mod{i:03d}.py").write_text(f"x = {i}\n")
    _commit_all(repo)

    # An empty graph: every inventory file is missing.
    store = GraphStore(tmp_path / "graph.db")
    try:
        report = coverage_report(str(repo), store=store)
    finally:
        store.close()

    assert report["missing_from_graph_total"] == 60
    assert report["missing_from_graph_truncated"] is True
    assert len(report["missing_from_graph"]) == 50
    assert report["indexed_count"] == 0
    assert report["inventory_by_language"] == {"python": 60}
    assert report["by_language"] == {}


def test_missing_graph_is_an_error_response_and_never_creates_a_db(
    tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 1\n")
    _commit_all(repo)
    data_dir = tmp_path / "external-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))

    report = coverage_report(str(repo))

    assert report["status"] == "error"
    assert "run `code-review-graph build` first" in report["error"]
    # Read-only: the missing database must not be materialized.
    assert not data_dir.exists()
    assert not (repo / ".code-review-graph").exists()


def test_full_build_with_workflow_yaml_leaves_nothing_missing(tmp_path):
    # Inventory invariant, end to end: a real full build over a repo
    # containing workflow-style YAML must index it (as its yaml File marker),
    # so coverage never reports an accepted file as missing from the graph.
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("def run():\n    return 1\n")
    workflow_dir = repo / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )
    _commit_all(repo)

    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store)
        report = coverage_report(str(repo), store=store)
    finally:
        store.close()

    assert report["status"] == "ok"
    assert report["missing_from_graph"] == []
    assert report["missing_from_graph_total"] == 0
    assert report["indexed_count"] == report["inventory_count"] == 2
    assert report["by_language"].get("yaml") == 1


# ---------------------------------------------------------------------------
# Text renderer
# ---------------------------------------------------------------------------


def test_format_coverage_text_renders_every_section():
    report = {
        "summary": "1/2 inventory files indexed, 1 stale graph files, 2 excluded",
        "by_language": {"python": 1},
        "inventory_by_language": {"python": 2},
        "excluded": {
            "ignored": {"count": 1, "files": ["build/out.js"], "truncated": False},
            "binary": {"count": 1, "files": ["blob.py"], "truncated": False},
            "no_language": {"count": 0, "files": [], "truncated": False},
            "untracked": {"count": 0, "files": [], "truncated": False},
        },
        "missing_from_graph": ["b.py"],
        "missing_from_graph_total": 1,
        "missing_from_graph_truncated": False,
        "stale_in_graph": ["gone.py"],
        "stale_in_graph_total": 1,
        "stale_in_graph_truncated": False,
    }
    text = format_coverage_text(report)
    lines = text.splitlines()

    assert lines[0] == "Coverage: 1/2 inventory files indexed, 1 stale graph files, 2 excluded"
    assert "  Graph File nodes by language: python=1" in lines
    assert "  Inventory by language:        python=2" in lines
    assert "  Excluded: ignored=1, binary=1, no_language=0, untracked=0" in lines
    assert "  Missing from graph (run `code-review-graph build`): (1)" in lines
    assert "    b.py" in lines
    assert "  Stale in graph (deleted from disk): (1)" in lines
    assert "    gone.py" in lines
    assert "... and" not in text


def test_format_coverage_text_reports_the_truncation_remainder():
    text = format_coverage_text({
        "summary": "0/60 inventory files indexed",
        "missing_from_graph": [f"mod{i:03d}.py" for i in range(50)],
        "missing_from_graph_total": 60,
        "missing_from_graph_truncated": True,
    })
    assert "Missing from graph (run `code-review-graph build`): (60)" in text
    assert "... and 10 more" in text


def test_format_coverage_text_survives_an_empty_report():
    text = format_coverage_text({})
    assert text == "Coverage: n/a"


# ---------------------------------------------------------------------------
# CLI (function-level, matching tests/test_cli_reconciliation.py)
# ---------------------------------------------------------------------------


def _build_graph_at(data_dir: Path, repo: Path, *rel_paths: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    store = GraphStore(data_dir / "graph.db")
    try:
        for rel_path in rel_paths:
            store.upsert_node(_file_node(repo / rel_path, "python"))
        store.commit()
    finally:
        store.close()


def _run_cli(argv: list[str], monkeypatch) -> None:
    from code_review_graph import cli

    monkeypatch.setattr(sys, "argv", ["code-review-graph", *argv])
    cli.main()


def test_cli_coverage_complete_graph_exits_zero_with_text(
    tmp_path, monkeypatch, capsys,
):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 1\n")
    _commit_all(repo)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    _build_graph_at(data_dir, repo, "app.py")

    _run_cli(["coverage", str(repo)], monkeypatch)

    out = capsys.readouterr().out
    assert out.startswith("Coverage: 1/1 inventory files indexed")


def test_cli_coverage_missing_files_exit_one_and_json_shape(
    tmp_path, monkeypatch, capsys,
):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 1\n")
    (repo / "unindexed.py").write_text("x = 2\n")
    _commit_all(repo)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    _build_graph_at(data_dir, repo, "app.py")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(["coverage", str(repo), "--json"], monkeypatch)
    assert exc_info.value.code == 1

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == REPORT_KEYS
    assert payload["status"] == "incomplete"
    assert payload["missing_from_graph"] == ["unindexed.py"]


def test_cli_coverage_no_fail_exits_zero_despite_missing(
    tmp_path, monkeypatch, capsys,
):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 1\n")
    (repo / "unindexed.py").write_text("x = 2\n")
    _commit_all(repo)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    _build_graph_at(data_dir, repo, "app.py")

    _run_cli(["coverage", str(repo), "--no-fail"], monkeypatch)

    out = capsys.readouterr().out
    assert "Missing from graph" in out
    assert "unindexed.py" in out


def test_cli_coverage_missing_graph_exits_one_without_creating_data(
    tmp_path, monkeypatch, capsys,
):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 1\n")
    _commit_all(repo)
    data_dir = tmp_path / "missing-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(["coverage", str(repo)], monkeypatch)

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not data_dir.exists()
    assert not (repo / ".code-review-graph").exists()
