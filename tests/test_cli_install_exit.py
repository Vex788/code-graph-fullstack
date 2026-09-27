"""install: a settings file left unchanged is reported and fails the exit, not the run."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from code_review_graph.cli import _handle_init
from code_review_graph.skills import SettingsNotWrittenError


def _args(tmp_path: Path, platform: str) -> argparse.Namespace:
    return argparse.Namespace(
        repo=str(tmp_path), dry_run=False, platform=platform, yes=True,
        no_instructions=True, no_skills=True, no_hooks=False,
    )


def _quiet_platform(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("code_review_graph.incremental.find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        "code_review_graph.incremental.ensure_repo_gitignore_excludes_crg",
        lambda repo_root: "present",
    )
    monkeypatch.setattr(
        "code_review_graph.skills.install_platform_configs",
        lambda repo_root, target, dry_run=False: [],
    )
    monkeypatch.setattr("code_review_graph.skills.install_git_hook", lambda repo_root: None)


def test_unwritten_claude_settings_do_not_claim_success(monkeypatch, tmp_path, capsys):
    _quiet_platform(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "code_review_graph.skills.install_hooks", lambda repo_root, platform="claude": 1,
    )
    assert _handle_init(_args(tmp_path, "claude")) == 1
    assert "Installed hooks in" not in capsys.readouterr().out


def test_written_claude_settings_report_success(monkeypatch, tmp_path, capsys):
    _quiet_platform(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "code_review_graph.skills.install_hooks", lambda repo_root, platform="claude": 0,
    )
    assert _handle_init(_args(tmp_path, "claude")) == 0
    assert "Installed hooks in" in capsys.readouterr().out


def test_codex_settings_error_is_rendered_and_install_continues(monkeypatch, tmp_path, capsys):
    _quiet_platform(monkeypatch, tmp_path)
    called = []

    def refuse(repo_root):
        raise SettingsNotWrittenError(tmp_path / "hooks.json", "it contains comments",
                                      {"hooks": {}})

    monkeypatch.setattr("code_review_graph.skills.install_codex_hooks", refuse)
    monkeypatch.setattr(
        "code_review_graph.skills.install_git_hook",
        lambda repo_root: called.append("git") or None,
    )
    assert _handle_init(_args(tmp_path, "codex")) == 1
    captured = capsys.readouterr()
    assert "left" in captured.err and "hooks.json" in captured.err
    assert "Merge this into it by hand" in captured.err
    assert called == ["git"]
    assert "Next steps" in captured.out


def test_cli_install_exit_code_propagates(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{\n  // user comment\n  "hooks": {}\n}\n', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "install", "--platform", "claude",
         "--repo", str(repo), "--yes", "--no-instructions", "--no-skills"],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin",
             "CRG_HOME": str(tmp_path / "crg")},
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "Installed hooks in" not in completed.stdout
    assert settings.read_text(encoding="utf-8").startswith('{\n  // user comment')
