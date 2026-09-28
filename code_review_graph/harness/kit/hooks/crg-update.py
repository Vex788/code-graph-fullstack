#!/usr/bin/env python3
"""PostToolUse observer: keep the code graph current after edits and HEAD moves.

{{harness_name}} hook protocol: the event JSON arrives on stdin, and this hook never
blocks the tool call and always exits 0. The update itself runs in a detached,
debounced worker, one per repository:

    code-review-graph update --skip-flows --if-locked=skip --repo <root>

Exit codes follow the graph contract: 0 ok and 3 degraded count as success;
75 means another writer holds the graph lock (skipped, a no-op); 4 means the
graph needs a full rebuild, which is logged and never started from here.
Anything else is a failure, retried up to RETRY_LIMIT times before the queue
is poisoned (logged once; the next successful update clears it).

A repository without a graph is skipped: hooks never build.

    crg-update.py --status      state of the worker for the repository at cwd
    crg-update.py --selftest    self-test against a stub binary (CRG_BIN or generated)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no flock, no observer.
    fcntl = None  # type: ignore[assignment]

EXIT_OK = 0
EXIT_DEGRADED = 3
EXIT_REBUILD_REQUIRED = 4
EXIT_LOCK_BUSY = 75

DEBOUNCE_SECONDS = float(os.environ.get("CRG_UPDATE_DEBOUNCE", "2"))
UPDATE_TIMEOUT_SECONDS = 600
STATUS_TIMEOUT_SECONDS = 20
GIT_TIMEOUT_SECONDS = 3
RETRY_LIMIT = 3
NOTICE_INTERVAL_SECONDS = 900
LOG_LIMIT_BYTES = 1_000_000
HEAD_MOVED = "<head-moved>"

# Git subcommands that move HEAD or rewrite the worktree.
HEAD_MOVING = frozenset({"checkout", "switch", "merge", "rebase", "pull", "reset", "stash"})
# Global options placed before the subcommand; these take a separate value.
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                               "--exec-path", "--config-env", "--super-prefix"})
COMMAND_WRAPPERS = frozenset({"sudo", "command", "env", "time", "nohup", "exec", "rtk", "proxy"})
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|&\n()]")
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PATCH_PATH = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
ENV_REFERENCE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")
STATE_HOME = "{{state_home}}"
LOG_HOME = "{{log_home}}"


def expand(spec: str) -> Path:
    """``~`` and ``${VAR:-default}`` expanded; an unset VAR takes its default."""
    text = ENV_REFERENCE.sub(lambda m: os.environ.get(m.group(1)) or m.group(2) or "", spec)
    return Path(text).expanduser()


def state_dir() -> Path:
    return expand(os.environ.get("CRG_UPDATE_STATE_DIR") or STATE_HOME)


def log_dir() -> Path:
    return expand(os.environ.get("CRG_UPDATE_LOG_DIR") or LOG_HOME)


def graph_binary() -> str | None:
    configured = os.environ.get("CRG_BIN")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which("code-review-graph")


# --- which events matter ---------------------------------------------------------


def moves_head(command: str) -> bool:
    """True when some segment of a shell command runs a HEAD-moving git subcommand.

    Only the git subcommand counts, so ``git log checkout`` and
    ``git log --grep=checkout`` do not, while ``git -C repo checkout x`` does.
    """
    for segment in SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        while words and (ENV_ASSIGNMENT.match(words[0]) or words[0] in COMMAND_WRAPPERS):
            words = words[1:]
        if not words or os.path.basename(words[0]) != "git":
            continue
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if option in GIT_VALUE_OPTIONS and rest:
                rest.pop(0)
        if rest and rest[0] in HEAD_MOVING:
            return True
    return False


def edited_paths(tool_input: dict) -> list[str]:
    """File paths an edit tool touched: file_path/notebook_path/path, or an ApplyPatch body."""
    paths = [
        value for key in ("file_path", "notebook_path", "path")
        if isinstance(value := tool_input.get(key), str) and value
    ]
    for key in ("patch", "input", "command"):
        body = tool_input.get(key)
        if isinstance(body, str) and "*** " in body:
            paths += [m.group(1) or m.group(2) for m in PATCH_PATH.finditer(body)]
    return [p.strip() for p in paths if p.strip()]


def repo_root(path: Path) -> Path | None:
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if result.returncode == 0 and top else None


def changes_for(event: dict) -> dict[Path, set[str]]:
    """Repository root -> changed paths (or HEAD_MOVED) for one hook event."""
    tool = str(event.get("tool_name") or "").lower()
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    cwd = Path(str(event.get("cwd") or os.getcwd()))
    changes: dict[Path, set[str]] = {}
    if tool == "bash":
        if moves_head(str(tool_input.get("command") or "")):
            root = repo_root(cwd)
            if root is not None:
                changes[root] = {HEAD_MOVED}
        return changes
    if not any(word in tool for word in ("edit", "write", "patch")):
        return changes
    for raw in edited_paths(tool_input):
        path = Path(raw)
        path = (path if path.is_absolute() else cwd / path).resolve()
        root = repo_root(path)
        if root is None:
            continue
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        if rel.split("/", 1)[0] in {".git", ".code-review-graph"}:
            continue
        changes.setdefault(root, set()).add(rel)
    return changes


# --- per-repository state ----------------------------------------------------------


class State:
    """Files under state_dir() for one repository root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
        base = state_dir()
        base.mkdir(parents=True, exist_ok=True)
        self.prefix = base / digest
        self.pending = self._path("pending.json")
        self.inflight = self._path("inflight.json")
        self.guard = self._path("guard")
        self.worker_lock = self._path("worker.lock")
        self.retries = self._path("retries")
        self.poisoned = self._path("poisoned")
        self.no_graph = self._path("no-graph")
        self.rebuild = self._path("rebuild-required")
        self.last = self._path("last.json")
        self.log = self._path("log")

    def _path(self, name: str) -> Path:
        return Path(f"{self.prefix}.{name}")


def _read_set(path: Path) -> set[str]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(item) for item in loaded} if isinstance(loaded, list) else set()


def _write_set(path: Path, items: set[str]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(sorted(items)), encoding="utf-8")
    tmp.replace(path)


class _Guard:
    """Short exclusive flock around the pending/inflight files."""

    def __init__(self, state: State) -> None:
        self.handle = state.guard.open("a+")

    def __enter__(self) -> _Guard:
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        self.handle.close()


def merge_pending(state: State, paths: set[str]) -> None:
    with _Guard(state):
        _write_set(state.pending, _read_set(state.pending) | paths)


def claim_pending(state: State) -> set[str]:
    """Move pending into inflight; a crashed worker's inflight set is claimed again."""
    with _Guard(state):
        claimed = _read_set(state.inflight) | _read_set(state.pending)
        if claimed:
            _write_set(state.inflight, claimed)
        state.pending.unlink(missing_ok=True)
        return claimed


def ack_inflight(state: State) -> None:
    with _Guard(state):
        state.inflight.unlink(missing_ok=True)


def requeue_inflight(state: State) -> None:
    with _Guard(state):
        queued = _read_set(state.inflight) | _read_set(state.pending)
        if queued:
            _write_set(state.pending, queued)
        state.inflight.unlink(missing_ok=True)


def _read_int(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def _fresh(path: Path, seconds: float = NOTICE_INTERVAL_SECONDS) -> bool:
    try:
        return time.time() - path.stat().st_mtime < seconds
    except OSError:
        return False


def notice(state: State, event: str, marker: Path | None = None, **fields: object) -> None:
    """Append one JSON line to the harness log, at most once per marker interval."""
    if marker is not None:
        if _fresh(marker):
            return
        try:
            marker.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            return
    entry = {"ts": int(time.time()), "event": event, "repo_root": str(state.root), **fields}
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "crg-update.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


# --- the worker --------------------------------------------------------------------


def graph_status(binary: str, root: Path) -> str:
    """Readiness status from ``status --json``; 'missing_graph' or 'unavailable' otherwise."""
    try:
        result = subprocess.run(
            [binary, "status", "--repo", str(root), "--json"],
            capture_output=True, text=True, timeout=STATUS_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        readiness = doc.get("readiness")
        if isinstance(readiness, dict) and isinstance(readiness.get("status"), str):
            return readiness["status"]
        if doc.get("status") == "error":
            return "unavailable"
        return "ok" if doc.get("files") else "missing_graph"
    return "unavailable"


def run_update(binary: str, root: Path) -> int:
    command = [binary, "update", "--skip-flows", "--if-locked=skip", "--repo", str(root)]
    print(f"[{time.strftime('%H:%M:%S')}] {' '.join(command)}", flush=True)
    try:
        return subprocess.run(
            command, cwd=str(root), stdin=subprocess.DEVNULL,
            timeout=UPDATE_TIMEOUT_SECONDS, check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        print(f"update timed out after {UPDATE_TIMEOUT_SECONDS}s", file=sys.stderr, flush=True)
        return -1
    except OSError as error:
        print(f"update failed to start: {error}", file=sys.stderr, flush=True)
        return -1


def _record(state: State, rc: int | None, outcome: str) -> None:
    try:
        state.last.write_text(
            json.dumps({"ts": int(time.time()), "rc": rc, "outcome": outcome}), encoding="utf-8",
        )
    except OSError:
        pass


def worker(root: Path) -> int:
    """Drain the queue for ``root``: debounce, update, retry; one worker per root."""
    state = State(root)
    binary = graph_binary()
    lock = state.worker_lock.open("a+")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another worker owns the queue
        checked = False
        while True:
            time.sleep(DEBOUNCE_SECONDS)
            with _Guard(state):
                if not state.pending.exists() and not state.inflight.exists():
                    # Unlock under the guard: a hook that queues after this sees the
                    # worker lock free and starts a new worker.
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    return 0
            claim_pending(state)
            if binary is None:
                ack_inflight(state)
                _record(state, None, "no-binary")
                continue
            if not checked:
                status = graph_status(binary, root)
                checked = True
                if status in {"missing_graph", "building", "rebuild_required"}:
                    ack_inflight(state)
                    _record(state, None, status)
                    marker = state.no_graph if status == "missing_graph" else state.rebuild
                    notice(state, f"crg_update_skipped_{status}", marker)
                    continue
            poisoned = state.poisoned.exists()
            rc = run_update(binary, root)
            if rc in (EXIT_OK, EXIT_DEGRADED):
                ack_inflight(state)
                state.retries.unlink(missing_ok=True)
                state.poisoned.unlink(missing_ok=True)
                _record(state, rc, "ok" if rc == EXIT_OK else "degraded")
            elif rc == EXIT_LOCK_BUSY:
                ack_inflight(state)
                _record(state, rc, "skipped")
            elif rc == EXIT_REBUILD_REQUIRED:
                ack_inflight(state)
                _record(state, rc, "rebuild_required")
                notice(state, "crg_update_rebuild_required", state.rebuild)
            else:
                retries = _read_int(state.retries) + 1
                state.retries.write_text(str(retries), encoding="utf-8")
                if poisoned or retries >= RETRY_LIMIT:
                    # Drop the queue; a fresh event gets one more attempt.
                    ack_inflight(state)
                    with _Guard(state):
                        state.pending.unlink(missing_ok=True)
                    _record(state, rc, "poisoned")
                    if not poisoned:
                        state.poisoned.write_text(str(int(time.time())), encoding="utf-8")
                        notice(state, "crg_update_poisoned", None, rc=rc, retries=retries)
                else:
                    requeue_inflight(state)
                    _record(state, rc, "failed")
                    time.sleep(DEBOUNCE_SECONDS * retries)
    finally:
        lock.close()


def _worker_running(state: State) -> bool:
    with state.worker_lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


def _open_log(path: Path):
    try:
        mode = "wb" if path.stat().st_size >= LOG_LIMIT_BYTES else "ab"
    except OSError:
        mode = "ab"
    return path.open(mode)


def schedule(root: Path, paths: set[str]) -> None:
    state = State(root)
    if _fresh(state.no_graph):
        return  # no graph here; checked again after the notice interval
    merge_pending(state, paths)
    if _worker_running(state):
        return
    if os.environ.get("CRG_UPDATE_INLINE") == "1":
        worker(root)
        return
    with _open_log(state.log) as log:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(root)],
            cwd=str(root), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )


def status(root: Path) -> dict:
    state = State(root)
    last: object = None
    try:
        last = json.loads(state.last.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {
        "repo_root": str(root),
        "pending": sorted(_read_set(state.pending)),
        "inflight": sorted(_read_set(state.inflight)),
        "running": _worker_running(state),
        "retries": _read_int(state.retries),
        "poisoned": state.poisoned.exists(),
        "last": last,
        "log": str(state.log),
    }


def handle_event(raw: str) -> None:
    try:
        event = json.loads(raw)
    except ValueError:
        return
    if not isinstance(event, dict) or graph_binary() is None:
        return
    for root, paths in changes_for(event).items():
        schedule(root, paths)


# --- self-test ---------------------------------------------------------------------

_STUB = """#!/bin/sh
echo "$*" >> "{log}"
if [ "$1" = "status" ]; then
  printf '%s\\n' '{{"files": 1, "readiness": {{"status": "ok", "reasons": []}}}}'
  exit 0
fi
if [ "$1" = "update" ]; then exit "${{CRG_STUB_RC:-0}}"; fi
exit 0
"""


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, timeout=30)


def selftest() -> dict:
    assert moves_head("git checkout main")
    assert moves_head("git -C repo checkout main")
    assert moves_head("cd x && git pull --rebase")
    assert moves_head("FOO=1 rtk git switch -c topic")
    assert moves_head("git -c core.pager=cat stash pop")
    assert not moves_head("git log checkout")
    assert not moves_head("git log --grep=checkout")
    assert not moves_head("echo git checkout")
    assert not moves_head("ls -la")
    patch = "*** Begin Patch\n*** Update File: src/A.java\n*** Move to: src/B.java\n"
    assert edited_paths({"patch": patch}) == ["src/A.java", "src/B.java"]
    assert edited_paths({"file_path": "a.jsp"}) == ["a.jsp"]

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        os.environ.update({
            "CRG_UPDATE_STATE_DIR": str(base / "state"), "CRG_UPDATE_LOG_DIR": str(base / "logs"),
            "CRG_UPDATE_INLINE": "1",
        })
        global DEBOUNCE_SECONDS
        DEBOUNCE_SECONDS = 0
        calls = base / "calls.log"
        if not os.environ.get("CRG_BIN"):
            stub = base / "code-review-graph"
            stub.write_text(_STUB.format(log=calls), encoding="utf-8")
            stub.chmod(0o755)
            os.environ["CRG_BIN"] = str(stub)
        repo = (base / "repo").resolve()
        repo.mkdir()
        _git("init", "-q", str(repo))
        (repo / "a.jsp").write_text("<p/>\n", encoding="utf-8")

        handle_event("not json")
        handle_event(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git log x"},
                                 "cwd": str(repo)}))
        edit = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "a.jsp")},
                           "cwd": str(repo)})
        handle_event(edit)
        state = State(repo)
        first = status(repo)
        assert not first["pending"] and not first["inflight"] and not first["running"], first
        outcome = (first["last"] or {}).get("outcome")
        rc = (first["last"] or {}).get("rc")
        log_path = base / "logs" / "crg-update.jsonl"
        notices = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
        expected = {EXIT_OK: "ok", EXIT_DEGRADED: "degraded", EXIT_LOCK_BUSY: "skipped",
                    EXIT_REBUILD_REQUIRED: "rebuild_required"}
        if rc in expected:
            assert outcome == expected[rc], first
            assert not first["poisoned"] and not first["retries"], first
        else:
            assert outcome == "poisoned" and first["poisoned"], first
            assert first["retries"] == RETRY_LIMIT, first
            assert sum("crg_update_poisoned" in line for line in notices) == 1, notices
        if rc == EXIT_REBUILD_REQUIRED:
            assert sum("rebuild_required" in line for line in notices) == 1, notices

        before = calls.read_text(encoding="utf-8").count("update ") if calls.exists() else None
        handle_event(edit)
        second = status(repo)
        if rc not in expected:
            # Poisoned: exactly one more attempt, no second notice.
            assert second["last"]["outcome"] == "poisoned" and second["poisoned"], second
            notices = log_path.read_text(encoding="utf-8").splitlines()
            assert sum("crg_update_poisoned" in line for line in notices) == 1, notices
        if before is not None:
            text = calls.read_text(encoding="utf-8")
            assert text.count("update ") == before + 1, text
            assert "build" not in text, text
        return {"selftest": "ok", "update_rc": rc, "outcome": outcome, "state": str(state.prefix)}


def main(argv: list[str]) -> int:
    if fcntl is None:
        return 0
    if argv[:1] == ["--worker"] and len(argv) == 2:
        return worker(Path(argv[1]).resolve())
    if argv[:1] == ["--status"]:
        root = repo_root(Path.cwd())
        if root is not None:
            print(json.dumps(status(root)))
        return 0
    if argv[:1] == ["--selftest"]:
        try:
            print(json.dumps(selftest()))
        except (AssertionError, OSError, subprocess.SubprocessError) as error:
            print(json.dumps({"selftest": "failed", "error": repr(error)}))
            return 1
        return 0
    # Observer contract: never raise, never block the tool call.
    try:
        handle_event(sys.stdin.read())
    except Exception:  # noqa: BLE001, S110 - observer contract: never raise
        pass  # nosec B110 - observer contract: never block the tool call
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
