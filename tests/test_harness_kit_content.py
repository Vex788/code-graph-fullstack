"""Graph kit content: every kit file renders for every target and agrees with the contract.

``crg_rules.json`` is generated from the contract; regenerate it with
``uv run python tests/test_harness_kit_content.py --write-rules``.
"""

from __future__ import annotations

import json
import os
import py_compile
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from code_review_graph.harness.apply import render_kit, render_regions
from code_review_graph.harness.renderer import load_blocks, render_text
from code_review_graph.harness.targets import KIT_DIR, get_target

REPO = Path(__file__).resolve().parents[1]
CONTRACT_FILE = REPO / "docs" / "spec" / "contract.json"
RULES_FILE = KIT_DIR / "data" / "crg_rules.json"
TARGETS = ("claude", "zcode", "bug-hunter")
MCP_SERVER = "code-review-graph"
TOOL_PREFIXES = ["mcp__code-review-graph__", "mcp__plugin_bug-hunter_code-review-graph__"]
# Receipt statuses an agent may act on; everything else means GRAPH_PREP_REQUIRED.
READY = ["ok"]
DEGRADED = ["partial_index"]
PREP_MARKER = "GRAPH_PREP_REQUIRED"
# CLI subcommands the kit names before their wave lands: command -> wave.
PENDING_CLI = {"embeddings": "W4a"}


@lru_cache(maxsize=1)
def contract() -> dict:
    return json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))


def indexed_extensions() -> list[str]:
    from code_review_graph.parser import EXTENSION_TO_LANGUAGE

    return sorted(EXTENSION_TO_LANGUAGE)


def expected_rules() -> dict:
    doc = contract()
    tools = sorted(t["name"] for t in doc["tools"])
    blocking = [s for s in doc["statuses"] if s not in READY + DEGRADED]
    cross_stack = [
        {"name": k["name"], "since": k["since"]}
        for k in doc["kinds"]["edge_kinds"] if k["cross_stack"]
    ]
    alternation = "|".join(blocking + ["error"])
    return {
        "version": 1,
        "generated_from": "docs/spec/contract.json",
        "contract_version": doc["contract_version"],
        "compat_epoch": doc["compat_epoch"],
        "mcp_server": MCP_SERVER,
        "tool_prefixes": TOOL_PREFIXES,
        "tools": tools,
        "read_only_tools": sorted(t["name"] for t in doc["tools"] if t["read_only"]),
        "write_tools": sorted(t["name"] for t in doc["tools"] if not t["read_only"]),
        "statuses": doc["statuses"],
        "ready_statuses": READY,
        "degraded_statuses": DEGRADED,
        "blocking_statuses": blocking,
        "prep_required_marker": PREP_MARKER,
        # Matched against json.dumps(tool_response): quotes may arrive escaped.
        "blocking_response_regex": (
            r'\\?"status\\?"\s*:\s*\\?"(' + alternation + r')\\?"|' + PREP_MARKER
        ),
        "exit_codes": doc["exit_codes"],
        "hook_noop_exit_codes": [doc["exit_codes"]["lock_busy"]],
        "indexed_extensions": indexed_extensions(),
        "cross_stack_edge_kinds": cross_stack,
    }


def rules_text() -> str:
    return json.dumps(expected_rules(), indent=2) + "\n"


@lru_cache(maxsize=None)
def rendered(target: str) -> dict[str, str]:
    return render_kit(get_target(target))


def _all_rendered() -> list[tuple[str, str, str]]:
    out = [(t, rel, text) for t in TARGETS for rel, text in rendered(t).items()]
    for t in (*TARGETS, "laya"):
        for (rel, rid), body in render_regions(get_target(t)).items():
            out.append((t, f"{rel}#{rid}", body))
    return out


skip_windows = pytest.mark.skipif(sys.platform == "win32", reason="POSIX hook")


# --- rendering ----------------------------------------------------------------------


EXPECTED_FILES = {
    "hooks/crg-update.py",
    "hooks/crg_rules.json",
    "skills/code-search-routing/SKILL.md",
    "skills/context-efficient-code-research/SKILL.md",
    "skills/graph-bootstrap/SKILL.md",
    "skills/graph-bootstrap/scripts/graph_bootstrap.py",
    "skills/pr-context-pack/SKILL.md",
    "skills/pr-context-pack/references/context-pack.schema.json",
    "skills/pr-context-pack/scripts/build_context_pack.py",
    "skills/pr-context-pack/scripts/graph_health.py",
    "skills/pr-context-pack/scripts/read_context_pack.py",
}


@pytest.mark.parametrize("target", TARGETS)
def test_every_kit_file_renders(target: str):
    files = rendered(target)
    assert set(files) == EXPECTED_FILES
    for rel, text in files.items():
        assert "{{" not in text.replace("{{impact_for}}", "") or rel.endswith(".py"), rel
        assert "<!-- if:" not in text and "<!-- endif" not in text, rel


def test_every_block_renders_for_every_target():
    blocks = load_blocks(KIT_DIR / "blocks")
    assert {"graph-search", "impact-claim", "graph-routing"} <= set(blocks)
    for target in (*TARGETS, "laya"):
        spec = get_target(target)
        for name, text in blocks.items():
            assert render_text(text, target, spec.vars.get, blocks, name).strip()


def test_no_user_home_literals():
    for target, rel, text in _all_rendered():
        assert "/Users/" not in text, f"{target}:{rel}"
        assert "vex788" not in text.lower(), f"{target}:{rel}"


def test_rendered_python_compiles(tmp_path: Path):
    for target in TARGETS:
        for rel, text in rendered(target).items():
            if rel.endswith(".py"):
                path = tmp_path / target / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                py_compile.compile(str(path), doraise=True)


@pytest.mark.parametrize("target", TARGETS)
def test_rendered_json_parses(target: str):
    for rel, text in rendered(target).items():
        if rel.endswith(".json"):
            json.loads(text)


def test_targets_differ_where_they_should():
    claude, zcode, hunter = (rendered(t) for t in TARGETS)
    schema = "skills/pr-context-pack/references/context-pack.schema.json"
    assert '"pms/pr-context-pack/1"' in claude[schema]
    assert '"bughunter/pr-context-pack/1"' in hunter[schema]
    routing = "skills/code-search-routing/SKILL.md"
    assert "PMS" in claude[routing] and "PMS" in zcode[routing] and "PMS" not in hunter[routing]
    assert "mcp__plugin_bug-hunter_code-review-graph__" in hunter[routing]
    assert "mcp__plugin_bug-hunter" not in claude[routing]
    research = "skills/context-efficient-code-research/SKILL.md"
    assert "~/.zcode/harness/bin/pms-test" in zcode[research]
    assert "~/.claude/bin/pms-test" in claude[research] and "pms-test" not in hunter[research]
    hook = "hooks/crg-update.py"
    assert "CLAUDE_PLUGIN_DATA" in hunter[hook] and "CLAUDE_PLUGIN_DATA" not in claude[hook]
    assert '"~/.zcode/harness/state/crg-update"' in zcode[hook]
    assert '"~/.claude/state/crg-update"' in claude[hook]


@skip_windows
def test_hook_state_dir_expands_target_paths(tmp_path: Path):
    probe = "import runpy, sys; m = runpy.run_path(sys.argv[1]); print(m['state_dir']())"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CRG_", "CLAUDE_PLUGIN"))}
    env["HOME"] = str(tmp_path)
    hook = _hook(tmp_path, "bug-hunter")

    def state_dir(extra: dict[str, str]) -> str:
        return subprocess.run(
            [sys.executable, "-c", probe, str(hook)], capture_output=True, text=True,
            timeout=30, env={**env, **extra}, check=True,
        ).stdout.strip()

    assert state_dir({}) == f"{tmp_path}/.claude/plugins/data/bug-hunter/state/crg-update"
    assert state_dir({"CLAUDE_PLUGIN_DATA": "/data"}) == "/data/state/crg-update"


def test_zcode_impact_claim_region_keeps_role_variable():
    regions = render_regions(get_target("zcode"))
    body = regions[("agent-src/blocks/impact-claim.md", "impact-claim")]
    assert "{{impact_for}}" in body


def test_one_build_rule_everywhere():
    for target, rel, text in _all_rendered():
        if rel.endswith(".md"):
            lowered = text.lower()
            assert "may run `build_or_update_graph_tool`" not in lowered, f"{target}:{rel}"
            assert "read-only means no source edits" not in lowered, f"{target}:{rel}"
    for target in TARGETS:
        text = rendered(target)["skills/context-efficient-code-research/SKILL.md"]
        assert "never builds" in text or "never build" in text


# --- the contract -------------------------------------------------------------------


_TOOL_NAME = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+_tool)\b")


def test_every_mentioned_tool_exists_in_contract():
    known = {t["name"] for t in contract()["tools"]}
    unknown = {
        f"{target}:{rel}: {name}"
        for target, rel, text in _all_rendered()
        for name in _TOOL_NAME.findall(text)
        if name not in known
    }
    assert not unknown, sorted(unknown)


# Inline code, shell lines in code fences, and argv lists in the kit scripts.
_CLI_PROSE = re.compile(r"(?:`|^\s*)code-review-graph\s+([a-z][a-z-]*)", re.M)
_CLI_ARGV = re.compile(r'(?:run_json\(\s*|\brun\(\s*"[\w-]+",\s*)\[\s*"([a-z][a-z-]*)"')
_CLI_BINARY = re.compile(r'\[\s*(?:binary|graph_binary\(\))\s*,\s*"([a-z][a-z-]*)"')


def _mentioned_cli() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for target, rel, text in _all_rendered():
        names = _CLI_PROSE.findall(text)
        if rel.endswith(".py"):
            names += _CLI_ARGV.findall(text) + _CLI_BINARY.findall(text)
        for name in names:
            found.setdefault(name, set()).add(f"{target}:{rel}")
    return found


def test_cli_mentions_are_detected():
    found = _mentioned_cli()
    assert {"status", "update", "clone-graph", "impact", "coverage"} <= set(found)


HAS_CLI = "cli_commands" in contract()


@pytest.mark.xfail(
    not HAS_CLI, strict=True,
    reason="docs/spec/contract.json has no cli_commands yet (W2b)",
)
def test_every_mentioned_cli_command_exists_in_contract():
    known = {c["name"] for c in contract().get("cli_commands", [])}
    missing = {name: sorted(where) for name, where in _mentioned_cli().items()
               if name not in known and name not in PENDING_CLI}
    assert HAS_CLI and not missing, missing


def test_pending_cli_commands_have_landed():
    known = {c["name"] for c in contract().get("cli_commands", [])}
    assert set(PENDING_CLI) <= known


def test_script_cli_calls_use_contract_options():
    """Scripts pass only options the contract lists for that subcommand."""
    if not HAS_CLI:
        pytest.skip("no cli_commands in the contract yet (W2b)")
    options = {c["name"]: set(c["options"]) for c in contract()["cli_commands"]}
    call = re.compile(r'\[\s*(?:(?:binary|graph_binary\(\)),\s*)?"(%s)"([^\]]*)\]'
                      % "|".join(re.escape(name) for name in options))
    bad = []
    for target in TARGETS:
        for rel, text in rendered(target).items():
            if not rel.endswith(".py"):
                continue
            for m in call.finditer(text):
                command = m.group(1)
                flags = {f.split("=")[0] for f in re.findall(r'"(--[a-z-]+[^"]*)"', m.group(2))}
                if not flags and "--" not in m.group(2):
                    continue
                if command not in options or not flags <= options[command]:
                    bad.append((target, rel, command, sorted(flags - options.get(command, set()))))
    assert not bad, bad


def test_crg_rules_match_contract():
    committed = RULES_FILE.read_text(encoding="utf-8")
    assert committed == rules_text(), (
        "crg_rules.json is stale; run: uv run python tests/test_harness_kit_content.py "
        "--write-rules"
    )
    for target in TARGETS:
        rules = json.loads(rendered(target)["hooks/crg_rules.json"])
        assert rules == expected_rules()
        assert {".jsp", ".jspf", ".tag", ".js", ".css", ".xml", ".java", ".sql"} <= set(
            rules["indexed_extensions"]
        )
        assert set(rules["read_only_tools"]).isdisjoint(rules["write_tools"])
        assert "build_or_update_graph_tool" in rules["write_tools"]
        assert rules["blocking_statuses"] == [
            "missing_graph", "building", "rebuild_required", "stale_graph", "stale_worktree",
        ]
        rx = re.compile(rules["blocking_response_regex"])
        assert rx.search(json.dumps(json.dumps({"_graph": {"status": "stale_graph"}})))
        assert rx.search('{"status": "error", "error_code": "x"}')
        assert not rx.search('{"_graph": {"status": "partial_index"}}')
        assert rules["tool_prefixes"][1] == "mcp__plugin_bug-hunter_code-review-graph__"


# --- the update hook ----------------------------------------------------------------


def _hook(tmp_path: Path, target: str = "claude") -> Path:
    hook = tmp_path / target / "crg-update.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(rendered(target)["hooks/crg-update.py"], encoding="utf-8")
    return hook


def _stub(tmp_path: Path, update_rc: int, status_json: str | None) -> tuple[Path, Path]:
    log = tmp_path / f"calls-{update_rc}.log"
    status = (
        f"printf '%s\\n' '{status_json}'; exit 0" if status_json is not None
        else 'echo "No graph found at x. Run `code-review-graph build` first." >&2; exit 1'
    )
    stub = tmp_path / f"stub-{update_rc}"
    stub.write_text(
        f'#!/bin/sh\necho "$*" >> "{log}"\n'
        f'if [ "$1" = status ]; then {status}; fi\nexit {update_rc}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, log


def _env(tmp_path: Path, stub: Path | None) -> dict[str, str]:
    env = {
        **os.environ,
        "CRG_UPDATE_STATE_DIR": str(tmp_path / "state"),
        "CRG_UPDATE_LOG_DIR": str(tmp_path / "logs"),
        "CRG_UPDATE_DEBOUNCE": "0",
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "plugin-data"),
    }
    env.pop("CRG_BIN", None)
    if stub is not None:
        env["CRG_BIN"] = str(stub)
    return env




@skip_windows
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize(
    ("rc", "outcome"),
    [(0, "ok"), (75, "skipped"), (4, "rebuild_required"), (1, "poisoned")],
)
def test_hook_selftest_with_stub_binary(tmp_path: Path, target: str, rc: int, outcome: str):
    stub, log = _stub(tmp_path, rc, '{"files": 2, "readiness": {"status": "ok"}}')
    result = subprocess.run(
        [sys.executable, str(_hook(tmp_path, target)), "--selftest"],
        capture_output=True, text=True, timeout=120, env=_env(tmp_path, stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["selftest"] == "ok" and report["update_rc"] == rc
    assert report["outcome"] == outcome
    calls = log.read_text(encoding="utf-8")
    assert "update --skip-flows --if-locked=skip --repo" in calls
    assert " build" not in calls and not calls.startswith("build")


@skip_windows
def test_hook_selftest_without_crg_bin(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(_hook(tmp_path)), "--selftest"],
        capture_output=True, text=True, timeout=120, env=_env(tmp_path, None),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["outcome"] == "ok"


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=30)
    (path / "page.jsp").write_text("<p/>\n", encoding="utf-8")
    return path.resolve()


def _run_hook(tmp_path: Path, event: dict | str, stub: Path | None, inline: bool = True):
    env = _env(tmp_path, stub)
    if inline:
        env["CRG_UPDATE_INLINE"] = "1"
    raw = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(
        [sys.executable, str(_hook(tmp_path))], input=raw,
        capture_output=True, text=True, timeout=60, env=env,
    )


@skip_windows
@pytest.mark.parametrize(
    ("tool", "command", "called"),
    [
        ("Bash", "ls -la", False),
        ("Bash", "git log checkout", False),
        ("Bash", "git log --grep=checkout", False),
        ("Bash", "git -C repo checkout main", True),
        ("Bash", "git pull --rebase", True),
        ("Read", "", False),
        ("Edit", "", True),
    ],
)
def test_hook_triggers(tmp_path: Path, tool: str, command: str, called: bool):
    repo = _git_repo(tmp_path / "repo")
    stub, log = _stub(tmp_path, 0, '{"files": 1, "readiness": {"status": "ok"}}')
    event = {"tool_name": tool, "cwd": str(repo),
             "tool_input": {"command": command, "file_path": str(repo / "page.jsp")}}
    result = _run_hook(tmp_path, event, stub)
    assert result.returncode == 0, result.stderr
    assert ("update " in log.read_text(encoding="utf-8") if log.exists() else False) is called


@skip_windows
def test_hook_always_exits_zero(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    stub, _ = _stub(tmp_path, 1, '{"files": 1, "readiness": {"status": "ok"}}')
    for event in ("", "not json", "[]", {"tool_name": "Write",
                                          "tool_input": {"file_path": str(repo / "page.jsp")}}):
        assert _run_hook(tmp_path, event, stub).returncode == 0
    assert _run_hook(tmp_path, {"tool_name": "Edit"}, None).returncode == 0


@skip_windows
def test_hook_never_builds_a_missing_graph(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    stub, log = _stub(tmp_path, 0, None)
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "page.jsp")}}
    assert _run_hook(tmp_path, event, stub).returncode == 0
    assert _run_hook(tmp_path, event, stub).returncode == 0
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls == [f"status --repo {repo} --json"]  # the second event is short-circuited
    notices = (tmp_path / "logs" / "crg-update.jsonl").read_text(encoding="utf-8")
    assert "crg_update_skipped_missing_graph" in notices


@skip_windows
def test_hook_detached_worker_drains_queue(tmp_path: Path):
    import time

    repo = _git_repo(tmp_path / "repo")
    stub, log = _stub(tmp_path, 0, '{"files": 1, "readiness": {"status": "ok"}}')
    event = {"tool_name": "Write", "tool_input": {"file_path": str(repo / "page.jsp")}}
    assert _run_hook(tmp_path, event, stub, inline=False).returncode == 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if log.exists() and "update " in log.read_text(encoding="utf-8"):
            break
        time.sleep(0.1)
    assert "update --skip-flows --if-locked=skip --repo" in log.read_text(encoding="utf-8")


# --- scripts ------------------------------------------------------------------------


def _script(tmp_path: Path, target: str, rel: str) -> Path:
    files = rendered(target)
    skill_dir = Path(rel).parent.parent
    for other, text in files.items():
        if Path(other).is_relative_to(skill_dir) or other == "hooks/crg_rules.json":
            path = tmp_path / target / other
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return tmp_path / target / rel


def _commit(repo: Path, message: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True, env=env,
                   timeout=30)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def _impact_json(repo: Path) -> str:
    return json.dumps({
        "status": "ok",
        "changed_nodes": [{"name": "page.jsp", "kind": "File", "qualified_name": "page.jsp",
                           "file_path": str(repo / "page.jsp"), "line_start": 1, "line_end": 1}],
        "impacted_nodes": [{"name": "Bean", "kind": "Class", "qualified_name": "app.Bean",
                            "file_path": str(repo / "Bean.java"), "line_start": 3}],
        "edges": [{"kind": "RENDERS", "source": "page.jsp", "target": "app.Bean"}],
        "truncated": False,
    })


def _graph_stub(tmp_path: Path, status: dict | None, extra: str = "") -> tuple[Path, Path]:
    log = tmp_path / "graph-calls.log"
    status_line = (
        f"printf '%s\\n' '{json.dumps(status)}'; exit 0" if status is not None
        else "echo 'Traceback: boom' >&2; exit 1"
    )
    stub = tmp_path / "code-review-graph"
    stub.write_text(
        f'#!/bin/sh\necho "$*" >> "{log}"\n'
        f'if [ "$1" = status ]; then {status_line}; fi\n{extra}'
        "if [ \"$1\" = query ]; then printf '%s\\n' "
        "'{\"status\": \"ok\", \"results\": [], \"result_count\": 0}'; exit 0; fi\n"
        "if [ \"$1\" = coverage ]; then printf '%s\\n' "
        "'{\"status\": \"ok\", \"missing_from_graph\": [], \"missing_from_graph_total\": 0}'; "
        "exit 0; fi\nexit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, log


def _status(repo: Path, head: str, readiness: str = "ok") -> dict:
    return {
        "nodes": 10, "files": 2, "last_updated": "x", "repo_root": str(repo),
        "built_at_commit": head, "current_sha": head,
        "readiness": {"status": readiness, "embeddings": "off", "reasons": []},
        "source_identity": {"source_matches_build": True, "missing_indexed_paths": [],
                            "deleted_indexed_paths": [], "mismatched_indexed_paths": []},
    }


@skip_windows
@pytest.mark.parametrize(("readiness", "rc", "verdict"), [
    ("ok", 0, "ready"), ("partial_index", 0, "degraded"), ("stale_graph", 2, "prep_required"),
])
def test_graph_health_reads_only_cli_json(tmp_path: Path, readiness, rc, verdict):
    repo = _git_repo(tmp_path / "repo")
    head = _commit(repo, "init")
    stub, log = _graph_stub(tmp_path, _status(repo, head, readiness))
    script = _script(tmp_path, "bug-hunter", "skills/pr-context-pack/scripts/graph_health.py")
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(repo), "--changed", "page.jsp",
         "--head", head],
        capture_output=True, text=True, timeout=60, env={**os.environ, "CRG_BIN": str(stub)},
    )
    assert result.returncode == rc, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["verdict"] == verdict
    assert report["graph_status"] == readiness
    assert "sqlite" not in script.read_text(encoding="utf-8")
    assert log.read_text(encoding="utf-8").startswith(f"status --repo {repo} --json")


def _real_status_json(repo: Path) -> dict:
    """What this build's ``status --json`` prints for *repo*."""
    completed = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "status", "--json", "--repo", str(repo)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@skip_windows
def test_graph_health_missing_graph_is_prep_required(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    missing = _real_status_json(repo)
    assert missing["readiness"]["status"] == "missing_graph"
    stub, _ = _graph_stub(tmp_path, missing)
    script = _script(tmp_path, "claude", "skills/pr-context-pack/scripts/graph_health.py")
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(repo)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "CRG_BIN": str(stub)},
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["graph_status"] == "missing_graph" and report["verdict"] == "prep_required"
    assert report["marker"] == PREP_MARKER


@skip_windows
def test_graph_health_cli_crash_is_unavailable_not_missing(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    stub, _ = _graph_stub(tmp_path, None)
    script = _script(tmp_path, "claude", "skills/pr-context-pack/scripts/graph_health.py")
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(repo)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "CRG_BIN": str(stub)},
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["graph_status"] == "unavailable" and report["verdict"] == "unavailable"


@skip_windows
@pytest.mark.parametrize("target", ["claude", "bug-hunter"])
def test_build_context_pack_uses_cli_receipt(tmp_path: Path, target: str):
    pytest.importorskip("jsonschema")
    repo = _git_repo(tmp_path / "repo")
    base = _commit(repo, "base")
    (repo / "page.jsp").write_text("<p>changed</p>\n", encoding="utf-8")
    head = _commit(repo, "head")
    impact = f"if [ \"$1\" = impact ]; then printf '%s\\n' '{_impact_json(repo)}'; exit 0; fi\n"
    stub, log = _graph_stub(tmp_path, _status(repo, head), impact)
    script = _script(tmp_path, target, "skills/pr-context-pack/scripts/build_context_pack.py")
    receipt = tmp_path / "status.json"
    receipt.write_text(json.dumps(_status(repo, head)), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(repo), "--base", base, "--head", head,
         "--pr", "7", "--graph-receipt", str(receipt), "--output-root", str(tmp_path / "out")],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "CRG_BIN": str(stub), "HOME": str(tmp_path / "home")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    pack = json.loads((Path(result.stdout.strip()) / "context-pack.json").read_text("utf-8"))
    prefix = "bughunter" if target == "bug-hunter" else "pms"
    assert pack["schema"] == f"{prefix}/pr-context-pack/1"
    assert pack["graph"]["status"] == "receipt"
    assert pack["graph"]["changed_files"] == [
        {"path": "page.jsp", "disposition": "indexed", "reason": "live-graph-receipt"},
    ]
    assert pack["capabilities"]["domain"] is False
    assert [n["qualified_name"] for n in pack["graph"]["nodes"]] == ["page.jsp"]
    assert pack["graph"]["callees"] == [
        {"qualified_name": "app.Bean", "file_path": "Bean.java", "start_line": 3,
         "kind": "RENDERS"},
    ]
    assert f"impact --repo {repo} --depth 1" in log.read_text(encoding="utf-8")

    reader = script.with_name("read_context_pack.py")
    summary = subprocess.run(
        [sys.executable, str(reader), "summary", "--context",
         str(Path(result.stdout.strip()) / "context-pack.json")],
        capture_output=True, text=True, timeout=60,
    )
    assert summary.returncode == 0, summary.stderr
    assert json.loads(summary.stdout)["schema"] == f"{prefix}/pr-context-summary/1"


@skip_windows
def test_build_context_pack_rejects_stale_receipt(tmp_path: Path):
    pytest.importorskip("jsonschema")
    repo = _git_repo(tmp_path / "repo")
    base = _commit(repo, "base")
    (repo / "page.jsp").write_text("<p>changed</p>\n", encoding="utf-8")
    head = _commit(repo, "head")
    stub, _ = _graph_stub(tmp_path, _status(repo, head, "stale_graph"))
    script = _script(tmp_path, "zcode", "skills/pr-context-pack/scripts/build_context_pack.py")
    receipt = tmp_path / "status.json"
    receipt.write_text(json.dumps(_status(repo, head, "stale_graph")), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(repo), "--base", base, "--head", head,
         "--pr", "7", "--graph-receipt", str(receipt), "--output-root", str(tmp_path / "out")],
        capture_output=True, text=True, timeout=120, env={**os.environ, "CRG_BIN": str(stub)},
    )
    assert result.returncode == 2
    assert PREP_MARKER in result.stderr


def _bootstrap_stub(tmp_path: Path, readiness: str, clone_rc: int = 0, sleep: int = 0) -> Path:
    log = tmp_path / "boot-calls.log"
    status = json.dumps({"files": 3, "nodes": 9, "built_at_commit": "a", "current_sha": "a",
                         "readiness": {"status": readiness, "embeddings": "off", "reasons": []}})
    stub = tmp_path / "code-review-graph"
    stub.write_text(
        f'#!/bin/sh\necho "$* CRG_EMBEDDINGS=$CRG_EMBEDDINGS" >> "{log}"\n'
        f"if [ \"$1\" = status ]; then printf '%s\\n' '{status}'; exit 0; fi\n"
        f'if [ "$1" = clone-graph ]; then sleep {sleep}; '
        f"printf '%s\\n' '{{\"status\": \"ok\", \"rows_rewritten\": 5}}'; exit {clone_rc}; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


@skip_windows
@pytest.mark.parametrize(("readiness", "rc", "status"), [
    ("ok", 0, "ok"), ("partial_index", 2, "degraded"), ("stale_graph", 2, "degraded"),
])
def test_graph_bootstrap_clones_then_checks_readiness(tmp_path, readiness, rc, status):
    seed = _git_repo(tmp_path / "seed")
    _commit(seed, "seed")
    worktree = _git_repo(tmp_path / "wt")
    _commit(worktree, "wt")
    stub = _bootstrap_stub(tmp_path, readiness)
    script = _script(tmp_path, "bug-hunter", "skills/graph-bootstrap/scripts/graph_bootstrap.py")
    result = subprocess.run(
        [sys.executable, str(script), "--bootstrap-graph", "--worktree", str(worktree),
         "--seed", str(seed)],
        capture_output=True, text=True, timeout=120, env={**os.environ, "CRG_BIN": str(stub)},
    )
    assert result.returncode == rc, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == status and report["readiness"] == readiness
    calls = (tmp_path / "boot-calls.log").read_text(encoding="utf-8")
    assert f"clone-graph --from {seed} --to {worktree} --json" in calls
    assert "CRG_EMBEDDINGS=off" in calls
    assert "sqlite" not in script.read_text(encoding="utf-8")


@skip_windows
def test_graph_bootstrap_skips_a_seed_without_graph(tmp_path: Path):
    seed = _git_repo(tmp_path / "seed")
    _commit(seed, "seed")
    worktree = _git_repo(tmp_path / "wt")
    _commit(worktree, "wt")
    missing = json.dumps(_real_status_json(seed))
    stub = tmp_path / "code-review-graph"
    stub.write_text(
        f"#!/bin/sh\nif [ \"$1\" = status ]; then printf '%s\\n' '{missing}'; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    script = _script(tmp_path, "claude", "skills/graph-bootstrap/scripts/graph_bootstrap.py")
    result = subprocess.run(
        [sys.executable, str(script), "--worktree", str(worktree), "--seed", str(seed)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "CRG_BIN": str(stub)},
    )
    report = json.loads(result.stdout)
    assert (report["status"], report["seed_readiness"]) == ("skip", "missing_graph")
    assert result.returncode == 2


@skip_windows
def test_graph_bootstrap_timeout_is_reported(tmp_path: Path):
    seed = _git_repo(tmp_path / "seed")
    worktree = _git_repo(tmp_path / "wt")
    _commit(worktree, "wt")
    stub = _bootstrap_stub(tmp_path, "ok", sleep=5)
    script = _script(tmp_path, "claude", "skills/graph-bootstrap/scripts/graph_bootstrap.py")
    result = subprocess.run(
        [sys.executable, str(script), "--worktree", str(worktree), "--seed", str(seed),
         "--clone-seconds", "1"],
        capture_output=True, text=True, timeout=60, env={**os.environ, "CRG_BIN": str(stub)},
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "failed" and report["stage"] == "clone-graph"
    assert "timed out" in report["error"]


def test_bootstrap_budget_fits_caller_timeout():
    text = rendered("bug-hunter")["skills/graph-bootstrap/scripts/graph_bootstrap.py"]
    budget = int(re.search(r"^TOTAL_BUDGET_SECONDS = (\d+)", text, re.M).group(1))
    assert budget <= 900  # manage_review_worktrees.py waits 900 s for the script


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-rules"]:
        RULES_FILE.write_text(rules_text(), encoding="utf-8")
        print(f"wrote {RULES_FILE}")
