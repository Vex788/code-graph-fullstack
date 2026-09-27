"""MCP builds run as one detached subprocess job per repository root."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from code_review_graph import jobs

_IDENTITY = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    monkeypatch.setattr(jobs, "_jobs", {})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", *_IDENTITY, "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", *_IDENTITY, "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *_IDENTITY, "commit", "-q", "-m", "i"], cwd=root, check=True)
    return root.resolve()


# Stands in for the CLI: reports progress, then a result, like --progress-file.
_FAKE_CHILD = """
import json, os, sys, time
from pathlib import Path
progress, hold, final = Path(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
report = Path(sys.argv[4])
report.write_text(json.dumps({
    "sid_differs": os.getsid(0) != int(sys.argv[5]),
    "stdin": sys.stdin.read(),
    "token": os.environ.get("CRG_WRITER_LOCK_TOKEN"),
}))
def write(**state):
    tmp = progress.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, progress)
write(status="running", phase="parsing", message="Progress: 1/2 files")
time.sleep(hold)
if final == "ok":
    write(status="ok", exit_code=0, result={"status": "ok", "files_parsed": 2})
    sys.exit(0)
if final == "lock_busy":
    write(status="lock_busy", exit_code=75)
    sys.exit(75)
sys.exit(3)  # dies without reporting
"""


def _fake(monkeypatch, tmp_path: Path, hold: float, final: str) -> Path:
    report = tmp_path / "child.json"

    def fake_command(root, progress_path, **_options):
        return [sys.executable, "-c", _FAKE_CHILD, str(progress_path), str(hold), final,
                str(report), str(os.getsid(0))]

    monkeypatch.setattr(jobs, "build_command", fake_command)
    return report


def test_real_build_job_returns_the_build_result(repo):
    result = jobs.run_build_job(repo, full_rebuild=True, postprocess="none", wait_seconds=120)
    assert result["status"] == "ok", result
    assert result["build_type"] == "full"
    assert result["files_parsed"] == 1
    assert result["exit_code"] == 0
    assert Path(result["log_file"]).parent == repo / ".code-review-graph" / "jobs"
    status = jobs.build_job_status(repo)
    assert (status["status"], status["job_id"]) == ("ok", result["job_id"])


def test_second_call_joins_the_running_job(repo, tmp_path, monkeypatch):
    report = _fake(monkeypatch, tmp_path, hold=2.0, final="ok")
    first = jobs.start_build_job(repo, full_rebuild=False, base=None, postprocess="full",
                                 embedding_provider=None, embedding_model=None)
    second = jobs.start_build_job(repo, full_rebuild=True, base=None, postprocess="full",
                                  embedding_provider=None, embedding_model=None)
    assert first["status"] == "building"
    assert second["job_id"] == first["job_id"]
    assert second["already_running"] is True
    deadline = time.monotonic() + 10
    while jobs.build_job_status(repo).get("phase") != "parsing":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    running = jobs.build_job_status(repo)
    assert running["status"] == "building"
    assert running["message"] == "Progress: 1/2 files"
    done = jobs.wait_for_build_job(repo, 30)
    assert (done["status"], done["files_parsed"]) == ("ok", 2)
    child = json.loads(report.read_text())
    assert child == {"sid_differs": True, "stdin": "", "token": None}


def test_wait_timeout_answers_building(repo, tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path, hold=3.0, final="ok")
    result = jobs.run_build_job(repo, wait_seconds=0.3)
    assert result["status"] == "building"
    assert "status_only" in result["summary"]
    assert jobs.wait_for_build_job(repo, 30)["status"] == "ok"


def test_killed_job_is_an_error_never_ok(repo, tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path, hold=30.0, final="ok")
    started = jobs.run_build_job(repo, wait_seconds=0.5)
    os.kill(started["pid"], signal.SIGKILL)
    result = jobs.wait_for_build_job(repo, 10)
    assert result["status"] == "error"
    assert result["error_code"] == "build_failed"
    assert "without reporting" in result["message"]


def test_lock_busy_child_maps_to_lock_busy_error(repo, tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path, hold=0.0, final="lock_busy")
    result = jobs.run_build_job(repo, wait_seconds=10)
    assert (result["status"], result["error_code"], result["exit_code"]) == (
        "error", "lock_busy", 75,
    )


def test_job_from_a_previous_server_is_read_from_disk(repo, tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path, hold=0.0, final="ok")
    job_id = jobs.run_build_job(repo, wait_seconds=10)["job_id"]
    monkeypatch.setattr(jobs, "_jobs", {})
    status = jobs.build_job_status(repo)
    assert (status["status"], status["job_id"]) == ("ok", job_id)


def test_status_without_any_job_is_idle(repo):
    assert jobs.build_job_status(repo)["status"] == "idle"


def test_build_command_maps_options(repo, tmp_path):
    progress = tmp_path / "p.json"
    cmd = jobs.build_command(repo, progress, full_rebuild=False, base="HEAD~2",
                             postprocess="minimal", embedding_provider="local",
                             embedding_model="m")
    assert cmd[3:5] == ["update", "--repo"]
    assert "--skip-flows" in cmd and ["--base", "HEAD~2"] == cmd[cmd.index("--base"):][:2]
    assert cmd[cmd.index("--if-locked") + 1] == "wait"
    assert cmd[cmd.index("--progress-file") + 1] == str(progress)
    with pytest.raises(ValueError):
        jobs.build_command(repo, progress, full_rebuild=True, base=None, postprocess="most",
                           embedding_provider=None, embedding_model=None)
    with pytest.raises(ValueError):
        jobs.build_command(repo, progress, full_rebuild=True, base=None, postprocess="full",
                           embedding_provider="local", embedding_model=None)


@pytest.mark.asyncio
async def test_mcp_tool_runs_the_job_and_status_only_reports_it(repo, monkeypatch):
    from code_review_graph import main as crg_main

    tool = getattr(crg_main.build_or_update_graph_tool, "fn",
                   crg_main.build_or_update_graph_tool)
    result = await tool(repo_root=str(repo), full_rebuild=True, postprocess="none",
                        wait_seconds=120)
    assert result["status"] == "ok", result
    assert result["_graph"]["status"] == "ok"
    status = await tool(repo_root=str(repo), status_only=True)
    assert status["job_id"] == result["job_id"]
    bad = await tool(repo_root=str(repo / "missing"))
    assert bad["error_code"] == "invalid_repo_root"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_factory,expected", [
    (lambda: __import__("code_review_graph.migrations", fromlist=["x"])
        .SchemaMigrationPending(9, 10), ("building", None)),
    (lambda: __import__("code_review_graph.migrations", fromlist=["x"])
        .SchemaTooNewError(99, 10), ("error", "schema_too_new")),
])
async def test_schema_exceptions_map_to_contract_shapes(monkeypatch, exc_factory, expected):
    from fastmcp import Client

    from code_review_graph import main as crg_main

    def boom(**_kwargs):
        raise exc_factory()

    monkeypatch.setattr(crg_main, "list_graph_stats", boom)
    async with Client(crg_main.mcp) as client:
        result = await client.call_tool("list_graph_stats_tool", {}, raise_on_error=False)
    payload = result.structured_content
    assert payload["status"] == expected[0]
    assert payload.get("error_code") == expected[1]
