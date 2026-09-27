"""CLI: status --json readiness, contract, lock, --if-locked and --progress-file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from code_review_graph.locking import EXIT_LOCK_BUSY, TOKEN_ENV, lock_path

_IDENTITY = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _cli(*args: str, env: dict[str, str] | None = None, timeout: float = 120):
    return subprocess.run(
        [sys.executable, "-m", "code_review_graph", *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root.resolve()


def _db(root: Path) -> Path:
    return root / ".code-review-graph" / "graph.db"


# A child that holds the writer lock until its marker file is removed.
_HOLDER = """
import sys, time
from pathlib import Path
from code_review_graph.locking import writer_lock
db, marker = sys.argv[1], Path(sys.argv[2])
with writer_lock(db):
    marker.write_text("held")
    while marker.exists():
        time.sleep(0.05)
"""


class _Holder:
    def __init__(self, db: Path, tmp_path: Path) -> None:
        self.marker = tmp_path / "holding"
        self.proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(db), str(self.marker)])
        deadline = time.monotonic() + 30
        while not self.marker.exists():
            assert self.proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)

    def release(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.proc.wait(timeout=30)


def test_status_json_keeps_legacy_keys_and_adds_readiness(repo):
    assert _cli("build", "--repo", str(repo), "-q").returncode == 0
    completed = _cli("status", "--json", "--repo", str(repo))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    head = _git(repo, "rev-parse", "HEAD")
    for key in ("nodes", "files", "last_updated", "built_at_commit"):
        assert key in payload
    assert payload["built_at_commit"] == head
    assert payload["current_sha"] == head
    assert payload["repo_root"] == str(repo)
    assert payload["readiness"]["status"] == "ok", payload["readiness"]
    assert payload["source_identity"]["source_matches_build"] is True
    assert payload["source_identity"]["missing_indexed_paths"] == []
    assert payload["source_identity"]["deleted_indexed_paths"] == []
    jsonschema = pytest.importorskip("jsonschema")
    from code_review_graph.contract import SCHEMAS

    schema = SCHEMAS["status_json"]
    jsonschema.validators.validator_for(schema)(schema).validate(payload)

    (repo / "app.py").write_text("def handle():\n    return 2\n", encoding="utf-8")
    edited = json.loads(_cli("status", "--json", "--repo", str(repo)).stdout)
    assert edited["readiness"]["status"] == "ok", edited["readiness"]
    assert edited["source_identity"]["source_matches_build"] is True
    assert edited["source_identity"]["mismatched_indexed_paths"] == [str(repo / "app.py")]
    assert edited["source_identity"]["edited_indexed_count"] == 1

    (repo / "new.py").write_text("def fresh():\n    pass\n", encoding="utf-8")
    (repo / "app.py").unlink()
    stale = json.loads(_cli("status", "--json", "--repo", str(repo)).stdout)
    assert stale["readiness"]["status"] == "stale_worktree"
    assert stale["source_identity"]["missing_indexed_paths"] == [str(repo / "new.py")]
    assert stale["source_identity"]["deleted_indexed_paths"] == [str(repo / "app.py")]


def test_status_json_schema_too_new_is_error_shape(repo):
    import sqlite3

    assert _cli("build", "--repo", str(repo), "-q").returncode == 0
    conn = sqlite3.connect(_db(repo))
    conn.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    conn.execute("DELETE FROM metadata WHERE key = 'reader_compat'")
    conn.commit()
    conn.close()
    completed = _cli("status", "--json", "--repo", str(repo))
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert (payload["status"], payload["error_code"]) == ("error", "schema_too_new")


def test_contract_command_matches_module(capsys):
    from code_review_graph.contract import render_contract_json

    completed = _cli("contract", "--json")
    assert completed.returncode == 0
    assert completed.stdout == render_contract_json()
    commands = {c["name"]: c for c in json.loads(completed.stdout)["cli_commands"]}
    assert {"lock", "contract", "clone-graph", "status", "build"} <= set(commands)
    assert "--if-locked" in commands["build"]["options"]
    assert "--progress-file" in commands["update"]["options"]


def test_lock_runs_command_with_token_and_lock_held(repo, tmp_path):
    out = tmp_path / "seen.json"
    probe = (
        "import json, os, sys\n"
        "from code_review_graph.locking import holds_writer_lock, probe\n"
        f"db = {str(_db(repo))!r}\n"
        "state = probe(db)\n"
        f"json.dump({{'token': os.environ.get({TOKEN_ENV!r}), 'held': state.held,\n"
        "           'inherited': holds_writer_lock(db)}, open(sys.argv[1], 'w'))\n"
        "sys.exit(7)\n"
    )
    completed = _cli("lock", "--repo", str(repo), "--", sys.executable, "-c", probe, str(out))
    assert completed.returncode == 7, completed.stderr
    seen = json.loads(out.read_text())
    assert seen["token"] and seen["held"] is True and seen["inherited"] is True
    assert lock_path(_db(repo)).read_text() == ""


def test_lock_busy_exits_75(repo, tmp_path):
    holder = _Holder(_db(repo), tmp_path)
    try:
        completed = _cli("lock", "--repo", str(repo), "--wait", "0", "--", "true")
    finally:
        holder.release()
    assert completed.returncode == EXIT_LOCK_BUSY
    assert "lock busy" in completed.stderr


def test_lock_without_command_is_usage_error(repo):
    assert _cli("lock", "--repo", str(repo)).returncode == 2


def test_if_locked_skip_fail_and_wait(repo, tmp_path):
    assert _cli("build", "--repo", str(repo), "-q").returncode == 0
    holder = _Holder(_db(repo), tmp_path)
    try:
        skip = _cli("update", "--repo", str(repo), "--if-locked", "skip")
        fail = _cli("update", "--repo", str(repo), "--if-locked", "fail")
        waited = _cli("update", "--repo", str(repo), "--lock-wait", "0.3")
        pp = _cli("postprocess", "--repo", str(repo), "--if-locked", "skip")
    finally:
        holder.release()
    assert (skip.returncode, skip.stdout, skip.stderr) == (EXIT_LOCK_BUSY, "", "")
    assert fail.returncode == EXIT_LOCK_BUSY and "lock busy" in fail.stderr
    assert waited.returncode == EXIT_LOCK_BUSY
    assert pp.returncode == EXIT_LOCK_BUSY
    after = _cli("update", "--repo", str(repo), "--if-locked", "skip")
    assert after.returncode == 0, after.stderr


def test_build_waits_for_the_lock_then_runs(repo, tmp_path):
    holder = _Holder(_db(repo), tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "code_review_graph", "build", "--repo", str(repo), "-q"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(1.0)
    assert proc.poll() is None, "build did not wait for the held lock"
    holder.release()
    _out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err


def test_progress_file_records_result(repo, tmp_path):
    progress = tmp_path / "p.json"
    completed = _cli("build", "--repo", str(repo), "-q", "--progress-file", str(progress))
    assert completed.returncode == 0, completed.stderr
    state = json.loads(progress.read_text())
    assert state["status"] == "ok"
    assert state["exit_code"] == 0
    assert state["result"]["files_parsed"] >= 1
    assert state["command"] == "build"


def test_progress_file_records_lock_busy(repo, tmp_path):
    assert _cli("build", "--repo", str(repo), "-q").returncode == 0
    progress = tmp_path / "p.json"
    holder = _Holder(_db(repo), tmp_path)
    try:
        completed = _cli(
            "update", "--repo", str(repo), "--if-locked", "fail",
            "--progress-file", str(progress),
        )
    finally:
        holder.release()
    assert completed.returncode == EXIT_LOCK_BUSY
    state = json.loads(progress.read_text())
    assert (state["status"], state["exit_code"]) == ("lock_busy", EXIT_LOCK_BUSY)


def test_nested_lock_command_reenters_for_child_build(repo):
    # `lock -- code-review-graph build` must not deadlock: the child inherits the token.
    completed = _cli(
        "lock", "--repo", str(repo), "--wait", "5", "--",
        sys.executable, "-m", "code_review_graph", "build", "--repo", str(repo), "-q",
        "--if-locked", "fail",
        env={**os.environ},
    )
    assert completed.returncode == 0, completed.stderr


def test_update_rebuild_required_result_exits_4(repo, tmp_path, monkeypatch, capsys):
    from code_review_graph import cli
    from code_review_graph.locking import EXIT_REBUILD_REQUIRED
    from code_review_graph.tools import build as build_module

    assert _cli("build", "--repo", str(repo), "-q").returncode == 0
    monkeypatch.setattr(
        build_module, "build_or_update_graph",
        lambda **kw: {"status": "rebuild_required", "summary": "index generation changed"},
    )
    progress = tmp_path / "p.json"
    monkeypatch.setattr(sys, "argv", [
        "code-review-graph", "update", "--repo", str(repo), "--progress-file", str(progress),
    ])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == EXIT_REBUILD_REQUIRED
    assert "index generation changed" in capsys.readouterr().err
    state = json.loads(progress.read_text())
    assert (state["status"], state["exit_code"]) == ("rebuild_required", EXIT_REBUILD_REQUIRED)


def test_status_json_without_graph_is_missing_graph_json(repo):
    completed = _cli("status", "--json", "--repo", str(repo))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["readiness"]["status"] == "missing_graph"
    assert (payload["nodes"], payload["files"]) == (0, 0)
    assert payload["repo_root"] == str(repo)
    from code_review_graph.contract import CONTRACT_VERSION, SCHEMAS

    assert payload["contract_version"] == CONTRACT_VERSION
    assert not _db(repo).parent.exists()
    jsonschema = pytest.importorskip("jsonschema")
    schema = SCHEMAS["status_json"]
    jsonschema.validators.validator_for(schema)(schema).validate(payload)


def test_status_text_without_graph_still_exits_1(repo):
    completed = _cli("status", "--repo", str(repo))
    assert completed.returncode == 1
    assert "No graph found" in completed.stderr


@pytest.mark.parametrize("command", ["build", "update"])
@pytest.mark.parametrize(
    ("result_status", "expected"), [("partial", 3), ("ok", 0)],
)
def test_build_result_status_maps_to_exit_code(
    repo, tmp_path, monkeypatch, command, result_status, expected,
):
    from code_review_graph import cli
    from code_review_graph.tools import build as build_module

    assert _cli("build", "--repo", str(repo), "-q").returncode == 0
    result = {"status": result_status, "total_nodes": 1, "total_edges": 0}
    monkeypatch.setattr(build_module, "build_or_update_graph", lambda **kw: result)
    progress = tmp_path / "p.json"
    monkeypatch.setattr(sys, "argv", [
        "code-review-graph", command, "--repo", str(repo), "-q",
        "--progress-file", str(progress),
    ])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
        raise SystemExit(0)
    assert exc_info.value.code == expected
    state = json.loads(progress.read_text())
    # The job reader surfaces the result; the exit code carries the degradation.
    assert (state["status"], state["exit_code"]) == ("ok", expected)
    assert state["result"]["status"] == result_status


def test_harness_subcommand_dispatches():
    completed = _cli("harness", "fragment", "--target", "claude", "--json")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["target"] == "claude"
