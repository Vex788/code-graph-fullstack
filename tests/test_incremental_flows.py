"""Incremental flows and communities equal a full recompute.

Random edit sequences run on the fullstack fixture; after every incremental
update the stored flows and communities must match what a full trace and a
full detection compute on the same graph.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from code_review_graph.communities import detect_communities
from code_review_graph.flows import trace_flows
from code_review_graph.incremental import incremental_update, read_flows_stale

from .witness.conftest import build, copy_fixture, git, open_store

JAVA_ROOT = "src/main/java/com/acme"
_METHOD = re.compile(r"^\s+public\s+[\w<>\[\], ]+\s+(\w+)\s*\(", re.M)


def _flows(store) -> set[tuple]:
    names = dict(store._conn.execute("SELECT id, qualified_name FROM nodes").fetchall())
    stored = set()
    for flow_id, entry, depth, node_count, file_count, criticality in store._conn.execute(
        "SELECT id, entry_point_id, depth, node_count, file_count, criticality FROM flows"
    ).fetchall():
        path = tuple(
            names.get(row[0]) for row in store._conn.execute(
                "SELECT node_id FROM flow_memberships WHERE flow_id = ? ORDER BY position",
                (flow_id,),
            )
        )
        stored.add((names.get(entry), path, depth, node_count, file_count, criticality))
    return stored


def _traced(store) -> set[tuple]:
    names = dict(store._conn.execute("SELECT id, qualified_name FROM nodes").fetchall())
    return {
        (
            flow["entry_point"], tuple(names[i] for i in flow["path"]), flow["depth"],
            flow["node_count"], flow["file_count"], flow["criticality"],
        )
        for flow in trace_flows(store)
    }


def _communities(store) -> set[tuple]:
    members: dict[int, set[str]] = {}
    for qualified, community in store._conn.execute(
        "SELECT qualified_name, community_id FROM nodes WHERE community_id IS NOT NULL"
    ):
        members.setdefault(community, set()).add(qualified)
    return {
        (name, size, cohesion, frozenset(members.get(cid, ())))
        for cid, name, size, cohesion in store._conn.execute(
            "SELECT id, name, size, cohesion FROM communities"
        )
    }


def _detected(store) -> set[tuple]:
    return {
        (c["name"], c["size"], c["cohesion"], frozenset(c["members"]))
        for c in detect_communities(store)
    }


def _assert_matches_full_recompute(repo: Path) -> None:
    store = open_store(repo)
    try:
        assert read_flows_stale(store) is None
        assert _flows(store) == _traced(store)
        assert _communities(store) == _detected(store)
        orphans = store._conn.execute(
            "SELECT COUNT(*) FROM flow_memberships fm "
            "LEFT JOIN nodes n ON n.id = fm.node_id WHERE n.id IS NULL"
        ).fetchone()[0]
        assert orphans == 0
    finally:
        store.close()


class _Editor:
    """Seeded random edits to the fixture's Java sources."""

    def __init__(self, repo: Path, seed: int) -> None:
        self.repo = repo
        self.rng = random.Random(seed)
        self.added: list[tuple[str, str]] = []
        self.count = 0

    def _java_files(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.repo))
            for path in (self.repo / JAVA_ROOT).rglob("*.java")
        )

    def _append(self, relative: str, block: str) -> None:
        path = self.repo / relative
        source = path.read_text(encoding="utf-8").rstrip()
        assert source.endswith("}")
        path.write_text(source[:-1] + block + "}\n", encoding="utf-8")

    def add_method(self) -> str:
        relative = self.rng.choice(self._java_files())
        text = (self.repo / relative).read_text(encoding="utf-8")
        callees = _METHOD.findall(text)
        other = (self.repo / self.rng.choice(self._java_files())).read_text(encoding="utf-8")
        other_class = re.search(r"\b(?:class|interface)\s+(\w+)", other)
        other_methods = _METHOD.findall(other)
        self.count += 1
        name = f"edited{self.count}"
        calls = [f"        {callee}();\n" for callee in self.rng.sample(
            callees, min(len(callees), self.rng.randint(0, 2)))]
        if other_class and other_methods:
            calls.append(f"        {other_class.group(1)}.{self.rng.choice(other_methods)}();\n")
        block = f"\n    public void {name}() {{\n{''.join(calls)}    }}\n"
        self._append(relative, block)
        self.added.append((relative, block))
        return f"add {name} to {relative}"

    def remove_method(self) -> str:
        if not self.added:
            return self.add_method()
        relative, block = self.added.pop(self.rng.randrange(len(self.added)))
        path = self.repo / relative
        path.write_text(path.read_text(encoding="utf-8").replace(block, "\n"), encoding="utf-8")
        return f"remove method from {relative}"

    def comment(self) -> str:
        relative = self.rng.choice(self._java_files())
        self.count += 1
        self._append(relative, f"    // note {self.count}\n")
        return f"comment {relative}"

    def new_file(self) -> str:
        self.count += 1
        name = f"Edited{self.count}Job"
        relative = f"{JAVA_ROOT}/service/{name}.java"
        (self.repo / relative).write_text(
            "package com.acme.service;\n\n"
            f"public class {name} {{\n"
            "    private UserService userService;\n\n"
            "    public void run() {\n        userService.listUsers();\n        step();\n    }\n\n"
            "    public void step() {\n        userService.register(null);\n    }\n}\n",
            encoding="utf-8",
        )
        return f"new {relative}"

    def delete_file(self) -> str:
        candidates = [
            f for f in self._java_files()
            if not any(f == relative for relative, _ in self.added)
        ]
        relative = self.rng.choice(candidates)
        (self.repo / relative).unlink()
        return f"delete {relative}"

    def step(self) -> str:
        edit = self.rng.choices(
            [self.add_method, self.remove_method, self.comment, self.new_file, self.delete_file],
            weights=[5, 3, 2, 1, 1],
        )[0]
        return edit()


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_incremental_postprocess_equals_full_recompute(tmp_path: Path, seed: int) -> None:
    repo = copy_fixture(tmp_path / "app")
    assert build(repo)["status"] == "ok"
    _assert_matches_full_recompute(repo)
    editor = _Editor(repo, seed)
    partial_retraces = 0
    for _ in range(8):
        what = editor.step()
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", what)
        update = build(repo, full=False)
        assert update["build_type"] == "incremental", (what, update["summary"])
        _assert_matches_full_recompute(repo)
        store = open_store(repo)
        try:
            total = store._conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
        finally:
            store.close()
        partial_retraces += update["flows_detected"] < total
    assert partial_retraces, "every update re-traced every flow"

    fresh = copy_fixture(tmp_path / "fresh")
    for relative in {*editor._java_files(), *(
        str(p.relative_to(fresh)) for p in (fresh / JAVA_ROOT).rglob("*.java")
    )}:
        source, target = repo / relative, fresh / relative
        if source.exists():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        elif target.exists():
            target.unlink()
    git(fresh, "add", "-A")
    git(fresh, "commit", "-qm", "edited")
    assert build(fresh)["status"] == "ok"
    incremental, rebuilt = open_store(repo), open_store(fresh)
    try:
        old, new = str(repo), str(fresh)

        def rebase(value):
            if isinstance(value, str):
                return value.replace(old, new)
            if isinstance(value, (tuple, frozenset)):
                return type(value)(rebase(item) for item in value)
            return value

        assert {rebase(row) for row in _flows(incremental)} == _flows(rebuilt)
        assert {rebase(row) for row in _communities(incremental)} == _communities(rebuilt)
    finally:
        incremental.close()
        rebuilt.close()


def test_reparse_keeps_node_ids_memberships_and_communities(tmp_path: Path) -> None:
    repo = copy_fixture(tmp_path / "app")
    build(repo)
    relative = f"{JAVA_ROOT}/service/UserService.java"
    store = open_store(repo)
    try:
        before = store._conn.execute(
            "SELECT id, qualified_name, community_id FROM nodes WHERE file_path = ?",
            (str(repo / relative),),
        ).fetchall()
        memberships = store._conn.execute("SELECT COUNT(*) FROM flow_memberships").fetchone()
    finally:
        store.close()
    path = repo / relative
    path.write_text(
        path.read_text(encoding="utf-8").replace("{\n", "{\n    // moved\n", 1),
        encoding="utf-8",
    )
    git(repo, "commit", "-qam", "shift lines")
    update = build(repo, full=False, postprocess="none")
    assert update["files_updated"] == 1
    store = open_store(repo)
    try:
        after = store._conn.execute(
            "SELECT id, qualified_name, community_id FROM nodes WHERE file_path = ?",
            (str(repo / relative),),
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        assert store._conn.execute(
            "SELECT COUNT(*) FROM flow_memberships"
        ).fetchone() == memberships
        # Only line numbers moved: nothing for flows or communities to redo.
        pending = read_flows_stale(store)
        assert pending is not None and not pending["structural"]
    finally:
        store.close()


def test_skip_flows_marks_stale_and_next_full_run_refreshes(tmp_path: Path) -> None:
    from code_review_graph.tools.context import get_minimal_context

    repo = copy_fixture(tmp_path / "app")
    build(repo)
    editor = _Editor(repo, 7)
    editor.add_method()
    git(repo, "commit", "-qam", "edit")
    minimal = build(repo, full=False, postprocess="minimal")
    assert minimal["flows_stale"] is True
    store = open_store(repo)
    try:
        assert read_flows_stale(store)["structural"]
    finally:
        store.close()
    # Readiness does not wait for flows.
    context = get_minimal_context(task="review", repo_root=str(repo))
    assert context["status"] == "ok", context["readiness"]

    editor.add_method()
    git(repo, "commit", "-qam", "edit again")
    full = build(repo, full=False)
    assert full["build_type"] == "incremental"
    _assert_matches_full_recompute(repo)


def test_no_change_update_refreshes_pending_flows(tmp_path: Path) -> None:
    repo = copy_fixture(tmp_path / "app")
    build(repo)
    _Editor(repo, 3).add_method()
    git(repo, "commit", "-qam", "edit")
    build(repo, full=False, postprocess="minimal")
    again = build(repo, full=False)
    assert again["summary"].startswith("No changes detected")
    assert "flows_detected" in again
    _assert_matches_full_recompute(repo)


def test_watch_batch_postprocesses_the_pending_delta(tmp_path: Path) -> None:
    from code_review_graph.postprocessing import run_pending_post_processing

    repo = copy_fixture(tmp_path / "app")
    build(repo)
    editor = _Editor(repo, 11)
    changed = [editor.add_method().rsplit(" ", 1)[-1] for _ in range(2)]
    store = open_store(repo)
    try:
        update = incremental_update(repo, store, changed_files=changed, reconcile_stale=False)
        assert update["files_updated"] >= 1
        assert read_flows_stale(store) is not None
        result = run_pending_post_processing(store, repo_root=repo)
        assert "warnings" not in result, result
        assert result["flows_detected"] >= 1
    finally:
        store.close()
    _assert_matches_full_recompute(repo)


def test_cli_watch_uses_the_pending_postprocess(tmp_path: Path, monkeypatch) -> None:
    from code_review_graph import cli, incremental
    from code_review_graph.postprocessing import run_pending_post_processing

    repo = copy_fixture(tmp_path / "app")
    build(repo)
    seen = {}

    def fake_watch(root, store, on_files_updated=None, stop_event=None):
        seen["callback"] = on_files_updated

    monkeypatch.setattr(incremental, "watch", fake_watch)
    monkeypatch.setattr("sys.argv", ["code-review-graph", "watch", "--repo", str(repo)])
    cli.main()
    assert seen["callback"].func is run_pending_post_processing

