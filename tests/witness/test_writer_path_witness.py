"""Witnesses for the writer path: readiness stamping, retries, git failure, watcher.

Each xfail(strict) test fails today for the reason in its marker. When the
fix lands it XPASSes, which fails the suite until the marker is removed.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from .conftest import build, git, open_store

USER_SERVICE = "src/main/java/com/acme/service/UserService.java"


def _add_method(repo: Path, relative: str, method: str) -> None:
    path = repo / relative
    source = path.read_text(encoding="utf-8").rstrip()
    assert source.endswith("}")
    body = f"\n    public void {method}() {{\n        listUsers();\n    }}\n}}\n"
    path.write_text(source[:-1] + body, encoding="utf-8")


def _function_names(repo: Path, relative: str) -> set[str]:
    store = open_store(repo)
    try:
        return {node.name for node in store.get_nodes_by_file(str(repo / relative))}
    finally:
        store.close()


@pytest.mark.xfail(strict=True, reason="W2a: readiness is stamped before resolvers run")
def test_resolver_failure_is_not_reported_ok(fixture_repo: Path, monkeypatch):
    from code_review_graph import resolvers
    from code_review_graph.tools.context import get_minimal_context

    def broken(store, repo_root):
        raise RuntimeError("resolver crashed")

    _resolver, label, languages = resolvers.RESOLVERS["jsp"]
    monkeypatch.setitem(resolvers.RESOLVERS, "jsp", (broken, label, languages))
    assert build(fixture_repo)["status"] == "ok"

    context = get_minimal_context(task="review", repo_root=str(fixture_repo))
    assert context["status"] != "ok", "a build whose resolver crashed reads as ok"


@pytest.mark.xfail(strict=True, reason="W2a: a file that failed to parse is never retried")
def test_failed_parse_is_retried_by_next_update(fixture_repo: Path, monkeypatch):
    from code_review_graph.parser import CodeParser

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    original = CodeParser.parse_bytes

    def flaky(self, path, source):
        if Path(path).name == "UserService.java":
            raise RuntimeError("transient parse failure")
        return original(self, path, source)

    monkeypatch.setattr(CodeParser, "parse_bytes", flaky)
    first = build(fixture_repo)
    assert [e["file"] for e in first["errors"]] == [USER_SERVICE]
    monkeypatch.setattr(CodeParser, "parse_bytes", original)

    update = build(fixture_repo, full=False)
    assert update["status"] == "ok"
    assert "listUsers" in _function_names(fixture_repo, USER_SERVICE), update["summary"]


def test_git_status_control_reports_untracked_file(fixture_repo: Path):
    """Control for the timeout witness: with git working, the new file is missing."""
    from code_review_graph.tools._common import working_tree_drift

    build(fixture_repo, postprocess="none")
    (fixture_repo / "src/main/java/com/acme/NewThing.java").write_text(
        "package com.acme;\n\npublic class NewThing {\n}\n", encoding="utf-8",
    )
    store = open_store(fixture_repo)
    try:
        drift = working_tree_drift(fixture_repo, store)
    finally:
        store.close()
    assert drift["check"] == "full"
    assert [Path(p).name for p in drift["missing"]] == ["NewThing.java"]


def test_git_timeout_is_not_a_clean_worktree(fixture_repo: Path, monkeypatch):
    from code_review_graph import incremental
    from code_review_graph.tools._common import working_tree_drift

    build(fixture_repo, postprocess="none")
    (fixture_repo / "src/main/java/com/acme/NewThing.java").write_text(
        "package com.acme;\n\npublic class NewThing {\n}\n", encoding="utf-8",
    )
    real_run = subprocess.run

    def slow_git(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "status"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(incremental.subprocess, "run", slow_git)
    store = open_store(fixture_repo)
    try:
        drift = working_tree_drift(fixture_repo, store)
    finally:
        store.close()
    assert drift["check"] == "unavailable", f"git timed out but drift reads {drift}"


@pytest.mark.xfail(strict=True, reason="W2a: watcher restart does not catch up on edits")
def test_watcher_restart_catches_up_on_edits_made_while_down(fixture_repo: Path):
    from code_review_graph.incremental import watch

    build(fixture_repo, postprocess="none")
    # The watcher is down while this edit lands; no filesystem event will
    # ever be delivered for it.
    _add_method(fixture_repo, USER_SERVICE, "deactivateAll")

    stop = threading.Event()
    stop.set()
    store = open_store(fixture_repo)
    try:
        watch(fixture_repo, store, stop_event=stop)
    finally:
        store.close()
    assert "deactivateAll" in _function_names(fixture_repo, USER_SERVICE)


@pytest.mark.xfail(
    strict=True,
    reason="W4b: incremental flows/communities match 0 rows with repo-relative paths",
)
def test_incremental_postprocess_retraces_changed_flows(fixture_repo: Path):
    assert build(fixture_repo)["flows_detected"] > 0
    _add_method(fixture_repo, USER_SERVICE, "deactivateAll")
    git(fixture_repo, "commit", "-qam", "edit user service")

    update = build(fixture_repo, full=False)
    assert update["build_type"] == "incremental"
    assert update["changed_files"] == [USER_SERVICE]
    counts = {
        "flows_detected": update.get("flows_detected"),
        "communities_detected": update.get("communities_detected"),
    }
    assert counts["flows_detected"] and counts["communities_detected"], counts
