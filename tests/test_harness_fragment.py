"""Graph kit config fragment, the harness CLI, bump-pin and the kit update hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.contract import SCHEMAS, WRITE_TOOLS
from code_review_graph.harness import cli as harness_cli
from code_review_graph.harness.fragment import fragment, validate_fragment


@pytest.fixture(scope="module")
def fragments() -> dict[str, dict]:
    return {t: fragment(t) for t in ("claude", "zcode", "bug-hunter")}


@pytest.mark.parametrize("target", ["claude", "zcode", "bug-hunter"])
def test_fragment_validates_against_contract_schema(fragments, target):
    jsonschema = pytest.importorskip("jsonschema")
    doc = fragments[target]
    jsonschema.validate(doc, SCHEMAS["harness_fragment"])
    assert validate_fragment(doc) == []
    assert doc["owner"] == "code-review-graph" and doc["target"] == target


def test_fragment_hook_paths_follow_target_root(fragments):
    commands = {t: f["hooks"][0]["command"] for t, f in fragments.items()}
    assert commands["claude"] == 'python3 "$HOME/.claude/hooks/crg-update.py"'
    assert commands["zcode"] == 'python3 "$HOME/.zcode/harness/hooks/crg-update.py"'
    assert commands["bug-hunter"] == 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/crg-update.py"'
    for doc in fragments.values():
        hook = doc["hooks"][0]
        assert hook["event"] == "PostToolUse"
        assert set(hook["matcher"].split("|")) >= {"Edit", "Write", "MultiEdit", "Bash"}
    assert "ApplyPatch" in fragments["zcode"]["hooks"][0]["matcher"]


def test_fragment_permissions_are_read_only(fragments):
    allow = fragments["claude"]["permissions"]["allow"]
    tools = [a for a in allow if a.startswith("mcp__")]
    assert "mcp__code-review-graph__query_graph_tool" in tools
    assert "mcp__plugin_bug-hunter_code-review-graph__query_graph_tool" in tools
    assert not {f"mcp__code-review-graph__{t}" for t in WRITE_TOOLS} & set(tools)
    assert "Bash(code-review-graph status:*)" in allow
    assert not any("build" in a or "update" in a for a in allow if a.startswith("Bash("))
    server = fragments["claude"]["mcpServers"]["code-review-graph"]
    assert server == {"command": "code-review-graph", "args": ["serve", "--tools", "agent"]}


def test_region_only_target_has_no_fragment():
    with pytest.raises(ValueError):
        fragment("laya")


def test_validate_fragment_flags_bad_shape():
    pytest.importorskip("jsonschema")
    assert validate_fragment({"owner": "x"})


def _seed_claude_md(root: Path) -> None:
    # A real claude root carries the graph-routing markers; a bare one is degraded (exit 3).
    (root / "CLAUDE.md").write_text(
        "<!-- crg-kit:begin graph-routing -->\n<!-- crg-kit:end graph-routing -->\n",
        encoding="utf-8",
    )


def test_cli_fragment_and_apply(tmp_path: Path, capsys):
    assert harness_cli.main(["fragment", "--target", "claude", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["target"] == "claude"
    _seed_claude_md(tmp_path)
    root = str(tmp_path)
    assert harness_cli.main(["apply", "--target", "claude", "--root", root, "--check"]) == 1
    assert harness_cli.main(
        ["apply", "--target", "claude", "--root", root, "--files-only"]
    ) == 0
    assert harness_cli.main(["apply", "--target", "claude", "--root", root, "--check"]) == 0
    assert "clean" in capsys.readouterr().out


def test_register_adds_harness_subcommand():
    import argparse

    parser = argparse.ArgumentParser()
    harness_cli.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["harness", "fragment", "--target", "zcode", "--json"])
    assert args.command == "harness" and args.harness_run is harness_cli.run


def test_module_entry_point(tmp_path: Path):
    _seed_claude_md(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "code_review_graph.harness", "apply", "--target", "claude",
         "--root", str(tmp_path), "--dry-run"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "MISSING" in result.stdout


def test_bump_pin_rewrites_literals(tmp_path: Path, capsys):
    readme = tmp_path / "README.md"
    readme.write_text(
        'uv tool install "git+https://github.com/Vex788/code-graph-fullstack@391609a"\n'
        "GRAPH_SOURCE = \"https://github.com/Vex788/code-graph-fullstack.git@v2.3.8-fs.5\"\n"
        "unrelated other-repo@391609a\n",
        encoding="utf-8",
    )
    pin = tmp_path / "crg.pin"
    pin.write_text("v2.3.8-fs.5\n", encoding="utf-8")
    none = tmp_path / "none.txt"
    none.write_text("nothing\n", encoding="utf-8")

    rc = harness_cli.main(["bump-pin", "--tag", "v2.3.8-fs.6", "--files", str(readme), str(pin)])
    assert rc == 0
    text = readme.read_text(encoding="utf-8")
    assert text.count("code-graph-fullstack@v2.3.8-fs.6") == 1
    assert "code-graph-fullstack.git@v2.3.8-fs.6" in text
    assert "other-repo@391609a" in text
    assert pin.read_text(encoding="utf-8") == "v2.3.8-fs.6\n"

    assert harness_cli.main(["bump-pin", "--tag", "v2.3.8-fs.6", "--files", str(none)]) == 1
    assert harness_cli.main(["bump-pin", "--tag", "bad tag", "--files", str(pin)]) == 1
    assert "NO PIN" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake binary")
@pytest.mark.parametrize(
    ("tool", "command", "rc", "called"),
    [
        ("Edit", "", 0, True),
        ("Bash", "ls -la", 0, False),
        ("Bash", "git log --grep=checkout", 0, False),
        ("Bash", "git -C repo checkout main", 0, True),
        ("Bash", "git pull --rebase", 75, True),
        ("Write", "", 1, True),
    ],
)
def test_kit_update_hook(tmp_path: Path, tool, command, rc, called):
    from code_review_graph.harness.apply import render_kit
    from code_review_graph.harness.targets import get_target

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    hook = tmp_path / "crg-update.py"
    hook.write_text(render_kit(get_target("claude"))["hooks/crg-update.py"], encoding="utf-8")
    log = tmp_path / "calls.log"
    fake = tmp_path / "code-review-graph"
    fake.write_text(
        f'#!/bin/sh\necho "$*" >> "{log}"\n'
        'if [ "$1" = status ]; then echo \'{"files": 1, "readiness": {"status": "ok"}}\'; '
        f"exit 0; fi\nexit {rc}\n"
    )
    fake.chmod(0o755)
    event = {"tool_name": tool, "cwd": str(repo),
             "tool_input": {"command": command, "file_path": str(repo / "a.py")}}
    env = {**os.environ, "CRG_BIN": str(fake), "CRG_UPDATE_INLINE": "1",
           "CRG_UPDATE_DEBOUNCE": "0", "CRG_UPDATE_STATE_DIR": str(tmp_path / "state"),
           "CRG_UPDATE_LOG_DIR": str(tmp_path / "logs")}
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(event), capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr  # PostToolUse observers never block
    calls = log.read_text() if log.exists() else ""
    assert ("update --skip-flows --if-locked=skip" in calls) is called
