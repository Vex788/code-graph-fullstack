"""Query-time self-heal: a stale_graph receipt triggers one bounded catch-up."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.tools import _common as common

_IDENTITY = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _receipt(status: str, reasons: list[str], **extra) -> dict:
    return {"status": status, "reasons": reasons, **extra}


def test_eligibility_lets_only_incremental_staleness_heal():
    assert common._self_heal_eligible(
        _receipt("stale_graph", ["head_moved"], built_at_commit="abc"))
    assert common._self_heal_eligible(
        _receipt("stale_graph", ["git_capture_failed", "head_moved"], built_at_commit="abc"))
    assert not common._self_heal_eligible(_receipt("ok", []))
    assert not common._self_heal_eligible(_receipt("stale_graph", ["git_unavailable"]))
    # No anchor: an update would full-rebuild inside the budget.
    assert not common._self_heal_eligible(_receipt("stale_graph", ["head_moved"]))
    assert not common._self_heal_eligible(
        _receipt("rebuild_required", ["index_generation_mismatch"]))
    assert not common._self_heal_eligible(_receipt("missing_graph", ["missing_graph"]))
    assert not common._self_heal_eligible(None)


def test_eligibility_lets_untracked_missing_files_heal_a_stale_worktree():
    def worktree(**identity):
        return _receipt(
            "stale_worktree", ["worktree_changed"], built_at_commit="abc",
            source_identity={"check": "full", **identity},
        )

    assert common._self_heal_eligible(worktree(missing_indexed_count=1))
    assert not common._self_heal_eligible(worktree(missing_indexed_count=0))
    assert not common._self_heal_eligible(worktree())
    assert not common._self_heal_eligible(
        worktree(missing_indexed_count=1, check="unavailable"))
    assert not common._self_heal_eligible(
        _receipt("stale_worktree", ["worktree_changed"],
                 source_identity={"check": "full", "missing_indexed_count": 1}))


def test_graph_receipt_heals_an_untracked_file(tmp_path: Path, monkeypatch):
    """A new untracked source file is indexed by the query-time catch-up."""
    monkeypatch.setenv("CRG_RECEIPT_TTL", "0")
    common._self_heal_last.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "core.hooksPath", str(repo / ".no-hooks"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    from code_review_graph.tools.build import build_or_update_graph

    build_or_update_graph(repo_root=str(repo), postprocess="none")
    (repo / "fresh.py").write_text("def fresh():\n    return 2\n", encoding="utf-8")

    monkeypatch.setenv("CRG_SELF_HEAL_BUDGET", "0")
    stale = common.graph_receipt(str(repo))
    assert stale["status"] == "stale_worktree", stale
    assert stale["source_identity"]["missing_indexed_count"] == 1

    monkeypatch.setenv("CRG_SELF_HEAL_BUDGET", "120")
    healed = common.graph_receipt(str(repo))
    assert healed["status"] == "ok", healed


def test_oversize_untracked_file_never_triggers_a_catch_up(tmp_path: Path, monkeypatch):
    """The loop: an unindexable big file read as 'missing' forever and re-ran every update."""
    monkeypatch.setenv("CRG_RECEIPT_TTL", "0")
    monkeypatch.setenv("CRG_MAX_FILE_BYTES", "400")
    common._self_heal_last.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "core.hooksPath", str(repo / ".no-hooks"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    from code_review_graph.tools.build import build_or_update_graph

    build_or_update_graph(repo_root=str(repo), postprocess="none")
    (repo / "big.py").write_text("def big():\n" + "    x = 1\n" * 200, encoding="utf-8")

    def _must_not_run(*args, **kwargs):
        pytest.fail("an oversize untracked file must not start a catch-up update")

    monkeypatch.setattr(common, "_run_self_heal", _must_not_run)
    monkeypatch.setenv("CRG_SELF_HEAL_BUDGET", "120")
    receipt = common.graph_receipt(str(repo))
    assert receipt["status"] == "ok", receipt
    assert receipt["source_identity"]["skipped_oversize_count"] == 1
    assert "missing_indexed_count" not in receipt["source_identity"]


class _Rc0:
    returncode = 0
    stdout = b""
    stderr = b""


def test_heal_runs_once_per_debounce_window(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CRG_SELF_HEAL_DEBOUNCE", "60")
    monkeypatch.setenv("CRG_SELF_HEAL_BUDGET", "10")
    started = []
    monkeypatch.setattr(
        common.subprocess, "run", lambda *args, **kwargs: started.append(args) or _Rc0())
    monkeypatch.setattr(common, "_compute_graph_receipt", lambda root, db: {"status": "ok"})
    common._self_heal_last.clear()

    first = common._run_self_heal(tmp_path, tmp_path / "graph.db")
    second = common._run_self_heal(tmp_path, tmp_path / "graph.db")

    assert first == {"status": "ok"}
    assert second is None  # debounced: the honest stale answer stays
    assert len(started) == 1


def test_heal_disabled_by_zero_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CRG_SELF_HEAL_BUDGET", "0")

    def _must_not_run(*args, **kwargs):
        pytest.fail("budget 0 must not start an update")

    monkeypatch.setattr(common.subprocess, "run", _must_not_run)
    common._self_heal_last.clear()
    assert common._run_self_heal(tmp_path, tmp_path / "graph.db") is None


def test_graph_receipt_heals_a_moved_head(tmp_path: Path, monkeypatch):
    """The receipt seam catches a HEAD move no hook observed."""
    monkeypatch.setenv("CRG_RECEIPT_TTL", "0")
    common._self_heal_last.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "core.hooksPath", str(repo / ".no-hooks"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    from code_review_graph.tools.build import build_or_update_graph

    build_or_update_graph(repo_root=str(repo), postprocess="none")

    (repo / "app.py").write_text("def handle():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "move head")

    monkeypatch.setenv("CRG_SELF_HEAL_BUDGET", "0")
    stale = common.graph_receipt(str(repo))
    assert stale is not None
    assert stale["status"] == "stale_graph", stale
    assert "head_moved" in stale["reasons"]

    monkeypatch.setenv("CRG_SELF_HEAL_BUDGET", "120")
    healed = common.graph_receipt(str(repo))
    assert healed is not None
    assert healed["status"] == "ok", healed
    assert healed["built_at_commit"] == _git(repo, "rev-parse", "HEAD")
