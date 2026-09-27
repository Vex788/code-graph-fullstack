"""Daemon: data-dir aware builds, exclusive PID file, rotating logs, build timeout."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import daemon as daemon_module
from code_review_graph.daemon import DaemonConfig, WatchDaemon, WatchRepo


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def test_start_skips_initial_build_when_graph_lives_in_the_data_dir(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo")
    data_dir = tmp_path / "external"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    config = DaemonConfig(log_dir=tmp_path / "logs", repos=[WatchRepo(str(repo), "r")])
    daemon = WatchDaemon(config=config)
    with (
        patch.object(daemon, "_initial_build") as initial_build,
        patch.object(daemon, "_start_watcher"),
        patch.object(daemon, "start_config_watcher"),
        patch.object(daemon, "start_health_checker"),
        patch.object(daemon, "_save_state"),
    ):
        daemon.start()
    initial_build.assert_not_called()
    assert not (repo / ".code-review-graph" / "graph.db").exists()


def test_reconcile_skips_initial_build_when_graph_lives_in_the_data_dir(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo")
    data_dir = tmp_path / "external"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    daemon = WatchDaemon(config=DaemonConfig(log_dir=tmp_path / "logs"))
    new_config = DaemonConfig(log_dir=tmp_path / "logs", repos=[WatchRepo(str(repo), "r")])
    with (
        patch.object(daemon, "_initial_build") as initial_build,
        patch.object(daemon, "_start_watcher"),
        patch.object(daemon, "_save_state"),
    ):
        daemon.reconcile(new_config)
    initial_build.assert_not_called()


def test_pid_file_is_exclusive_while_the_owner_lives(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    daemon_module.write_pid(os.getppid(), pid_path)
    with pytest.raises(daemon_module.DaemonAlreadyRunningError):
        daemon_module.write_pid(os.getpid(), pid_path)
    assert daemon_module.read_pid(pid_path) == os.getppid()


def test_stale_pid_file_is_replaced(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("999999999", encoding="utf-8")
    with patch.object(daemon_module, "pid_alive", return_value=False):
        daemon_module.write_pid(os.getpid(), pid_path)
    assert daemon_module.read_pid(pid_path) == os.getpid()


def test_rewriting_own_pid_is_allowed(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    daemon_module.write_pid(os.getpid(), pid_path)
    daemon_module.write_pid(os.getpid(), pid_path)
    assert daemon_module.read_pid(pid_path) == os.getpid()


def test_handle_start_reports_a_lost_pid_race(capsys):
    from code_review_graph.daemon_cli import _handle_start

    class Args:
        foreground = True

    def taken():
        raise daemon_module.DaemonAlreadyRunningError(4242)

    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=False),
        patch("code_review_graph.daemon.load_config", return_value=DaemonConfig()),
        patch("code_review_graph.daemon.WatchDaemon") as daemon_cls,
        patch("code_review_graph.daemon.write_pid", side_effect=taken),
        pytest.raises(SystemExit) as exc_info,
    ):
        _handle_start(Args())
    assert exc_info.value.code == 1
    assert "4242" in capsys.readouterr().out
    daemon_cls.return_value.start.assert_not_called()


def test_daemon_log_handler_rotates(tmp_path):
    log_file = tmp_path / "daemon.log"
    handler = daemon_module.configure_daemon_logging(log_file, max_bytes=300, backups=2)
    try:
        log = logging.getLogger("code_review_graph.test_rotation")
        for i in range(60):
            log.warning("line %03d %s", i, "x" * 40)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert log_file.exists()
    assert (tmp_path / "daemon.log.1").exists()
    assert not (tmp_path / "daemon.log.3").exists()


def test_watcher_log_rotates_at_spawn(tmp_path):
    log_path = tmp_path / "alias.log"
    log_path.write_bytes(b"x" * 100)
    daemon_module.rotate_if_large(log_path, max_bytes=50, backups=2)
    assert not log_path.exists()
    assert (tmp_path / "alias.log.1").read_bytes() == b"x" * 100
    log_path.write_bytes(b"y" * 10)
    daemon_module.rotate_if_large(log_path, max_bytes=50, backups=2)
    assert log_path.read_bytes() == b"y" * 10


def test_initial_build_has_a_timeout(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CRG_DAEMON_BUILD_TIMEOUT", "7")
    daemon = WatchDaemon(config=DaemonConfig(log_dir=tmp_path / "logs"))
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    with patch.object(daemon_module.subprocess, "run", fake_run):
        daemon._initial_build(WatchRepo(str(_repo(tmp_path, "r")), "r"))
    assert seen["timeout"] == 7
    assert seen["stdin"] is subprocess.DEVNULL
    assert "timed out" in caplog.text
