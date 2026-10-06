#!/usr/bin/env python3
"""Heal a code graph so the caller can use it, then report what it may claim.

Self-heal covers a graph that exists. This covers what it refuses: a missing graph
or ``rebuild_required`` on a PMS worktree is cloned from a validated seed graph
(``clone-graph --no-update``, then ``update`` as its own timed stage: a timeout leaves the
clone usable, ``stale_graph``, exit 3), a ``building`` graph is polled, never rebuilt, and so
is a ``building`` seed before a clone. The seed itself, when it has no graph, is cloned
once from the main PMS checkout if that is ``ok``. ``--clone-only`` (crg-reconcile) skips every
other heal. Scope is an allowlist (the PMS checkout, ``sp_api_library``, their
worktrees and the seed); every other path exits 4 with ``out_of_scope``. A per-repo
lock serialises healers and one machine-wide lock caps clones at one. A
``stale_worktree`` update runs once per fingerprint of the worktree and the tool
(``--version`` and contract version), so a tool upgrade re-arms it. An update that fails
with "built with a different repository root" (a graph copied raw from another checkout) is
not retried: the root is force-cloned from the seed instead, under the same locks and budget.

Prints one JSON object with ``--json``. Exit 0 ready, 3 usable but degraded
(``partial_index``/``stale_graph``/``stale_worktree``), 4 not usable, 75 busy past
the budget. Embeddings stay off; nothing here opens the graph database.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_bootstrap as gb  # noqa: E402

STATE_HOME = "{{state_home}}"
ENV_REFERENCE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")
PMS_ROOT = "~/IdeaProjects/pms"
SPAPI_ROOT = "~/IdeaProjects/sp_api_library"
SEED_DIR = "~/IdeaProjects/.crg-seed-pms"

READY = {"ok"}
DEGRADED = {"partial_index", "stale_graph", "stale_worktree"}
EXIT_OK, EXIT_DEGRADED, EXIT_BLOCKED, EXIT_BUSY = 0, 3, 4, gb.LOCK_BUSY
BUDGET_SECONDS = 240
UPDATE_SECONDS = 180
POLL_SECONDS = 120
LOCK_WAIT_SECONDS = 30
WRONG_ROOT = "built with a different repository root"
MAX_GAPS = 50


class BusyError(Exception):
    """A lock stayed held past the budget."""


def expand(spec: str) -> Path:
    """``~`` and ``${VAR:-default}`` expanded; an unset VAR takes its default."""
    text = ENV_REFERENCE.sub(lambda m: os.environ.get(m.group(1)) or m.group(2) or "", spec)
    return Path(text).expanduser()


def state_dir() -> Path:
    configured = os.environ.get("CRG_HEAL_STATE_DIR")
    return expand(configured) if configured else expand(STATE_HOME).parent / "crg-heal"


def reconcile_state_dir() -> Path:
    configured = os.environ.get("CRG_RECONCILE_STATE_DIR")
    return expand(configured) if configured else expand(STATE_HOME).parent / "crg-reconcile"


def real(path: str | Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def sha12(root: Path) -> str:
    return hashlib.sha1(str(root).encode()).hexdigest()[:12]


# --- scope ---------------------------------------------------------------------------


def worktrees(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    done = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"],
                          capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        return set()
    return {real(line[9:]) for line in done.stdout.splitlines() if line.startswith("worktree ")}


def classify(root: Path) -> tuple[str | None, Path]:
    """(``pms``/``spapi``/None for out of scope, the seed checkout)."""
    pms, spapi, seed_dir = real(PMS_ROOT), real(SPAPI_ROOT), real(SEED_DIR)
    seed = seed_dir if seed_dir.is_dir() else pms
    if root in {pms, seed_dir} | worktrees(pms):
        return "pms", seed
    if root in {spapi} | worktrees(spapi):
        return "spapi", seed
    return None, seed


# --- locks and attempt state ---------------------------------------------------------


@contextmanager
def locked(path: Path, deadline: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BusyError(f"{path.name} is held past the budget; retry later") from None
                time.sleep(min(0.2, max(deadline - time.monotonic(), 0.01)))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def tool_version() -> str:
    """First line of ``code-review-graph --version``; ``unknown`` when it prints nothing."""
    try:
        done = gb.run("version", ["--version"], gb.STATUS_SECONDS)
    except gb.StageError:
        return "unknown"
    return done.stdout.strip().partition("\n")[0].strip() or "unknown"


def fingerprint(root: Path, tool_id: str) -> str:
    """crg-reconcile's fingerprint, 16 hex: sha1 of HEAD, status --porcelain, diff HEAD and
    ``<tool_id>\\n``. The tool id makes a tool upgrade re-arm an update that already ran."""
    digest = hashlib.sha1()
    for args in (("rev-parse", "HEAD"), ("status", "--porcelain"), ("diff", "HEAD")):
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              timeout=60, check=False)
        digest.update(done.stdout)
    digest.update(f"{tool_id}\n".encode())
    return digest.hexdigest()[:16]


def attempt_file(root: Path) -> Path:
    return reconcile_state_dir() / sha12(root)


def attempted(root: Path, status: str, fp: str) -> bool:
    try:
        record = attempt_file(root).read_text(encoding="utf-8").split()
    except OSError:
        return False
    return len(record) >= 2 and record[0] == status and record[1] == fp


def remember(root: Path, status: str, fp: str) -> None:
    path = attempt_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{status} {fp} {int(time.time())}\n", encoding="utf-8")


def forget(root: Path) -> None:
    attempt_file(root).unlink(missing_ok=True)


# --- graph reads ---------------------------------------------------------------------


def read(root: Path) -> tuple[str, dict]:
    try:
        status, doc = gb.readiness("heal-status", root, gb.STATUS_SECONDS)
    except gb.StageError:
        return "unavailable", {}
    if status == "unavailable" and doc.get("status") == "error":
        status = str(doc.get("error_code") or "error")
    return status, doc


def receipt(doc: dict) -> dict:
    return {key: doc.get(key) for key in ("built_at_commit", "current_sha", "nodes", "files")}


def gaps(doc: dict) -> tuple[list[dict], int]:
    identity = doc.get("source_identity") or {}
    found = [{"path": path, "kind": kind}
             for key, kind in (("missing_indexed_paths", "missing"),
                               ("deleted_indexed_paths", "deleted"),
                               ("mismatched_indexed_paths", "mismatched"),
                               ("skipped_oversize_paths", "skipped_oversize"))
             for path in identity.get(key) or []]
    failed = doc.get("failed_files")
    if isinstance(failed, list):
        found += [{"path": path, "kind": "failed_files"} for path in failed]
    elif isinstance(failed, int) and failed > 0:
        found.append({"path": None, "kind": "failed_files", "count": failed})
    return found[:MAX_GAPS], len(found)


def exit_for(status: str) -> int:
    if status in READY:
        return EXIT_OK
    return EXIT_DEGRADED if status in DEGRADED else EXIT_BLOCKED


# --- the heal ------------------------------------------------------------------------


def update(root: Path, remaining: float) -> str:
    seconds = min(UPDATE_SECONDS, max(remaining, 1))
    try:
        done = gb.run("update", ["update", "--skip-flows", "--if-locked=wait",
                                 "--lock-wait", "60", "--repo", str(root)], seconds)
    except gb.StageError as error:
        return str(error)
    if done.returncode in (0, 3):
        return ""
    if WRONG_ROOT in done.stderr + done.stdout:
        return WRONG_ROOT
    return f"update exited {done.returncode}"


def refresh(root: Path, family: str, seed: Path, deadline: float,
            no_clone: bool) -> tuple[str, int, str]:
    """(action, forced exit code or -1, note): one update, or a forced re-clone from the seed
    when the update says the graph was built with another repository root (it never heals)."""
    note = update(root, deadline - time.monotonic())
    if note != WRONG_ROOT:
        return "update", -1, note
    blocked = blocked_next(root, family, seed, "rebuild_required", no_clone)
    if blocked:
        return "update", -1, f"{WRONG_ROOT}; {blocked}"
    action, forced, note = clone(root, seed, "rebuild_required", deadline)
    return action, forced, note or f"re-cloned from the seed: the graph was {WRONG_ROOT}"


def poll(root: Path, deadline: float) -> tuple[str, dict]:
    end = min(time.monotonic() + POLL_SECONDS, deadline)
    interval = float(os.environ.get("CRG_HEAL_POLL_SECONDS") or 5)
    while True:
        time.sleep(max(min(interval, end - time.monotonic()), 0))
        status, doc = read(root)
        if status != "building" or time.monotonic() >= end:
            return status, doc


def clone(root: Path, source: Path, status: str, deadline: float,
          bootstrap: bool = False) -> tuple[str, int, str]:
    """(action, forced exit code or -1, note) for one clone from the validated *source*.

    *bootstrap* means the seed itself is being created, so *source* is the main checkout.
    """
    source_status, _ = read(source)
    if source_status == "building":
        source_status, _ = poll(source, deadline)
    if source_status not in READY:
        return "none", EXIT_BLOCKED, ("main checkout is not ok: heal it first" if bootstrap
                                      else f"seed graph is {source_status}; "
                                           "the seed must be ok to clone")
    with locked(state_dir() / "clone.lock", deadline):
        seconds = deadline - time.monotonic() - gb.STATUS_SECONDS
        if seconds < 30:
            raise BusyError("too little budget left for a clone; retry later")
        try:
            update_seconds = float(os.environ.get("CRG_HEAL_UPDATE_SECONDS") or UPDATE_SECONDS)
            result = gb.bootstrap(root, source, 0, seconds, force=status == "rebuild_required",
                                  lock_wait=LOCK_WAIT_SECONDS, update_seconds=update_seconds)
        except gb.StageError as error:
            code = EXIT_BUSY if "timed out" in str(error) else EXIT_BLOCKED
            return "clone", code, str(error)
    if result["status"] == "skip":
        return "none", EXIT_BLOCKED, str(result["reason"])
    return "clone", -1, str(result.get("update_note") or "")


def blocked_next(root: Path, family: str, seed: Path, status: str, no_clone: bool) -> str:
    """Why this blocking status cannot be cloned, or '' when a clone may run."""
    if family != "pms":
        return "no seed graph for sp_api_library: nightly crg-postprocess-all"
    if root == seed:
        if status == "rebuild_required":
            return "nightly crg-postprocess-all"
        if root == real(PMS_ROOT):
            return "the seed has no graph: the owner builds it once; agents never build"
    return "clone disabled by --no-clone" if no_clone else ""


def heal(repo: str, budget: float, no_clone: bool, clone_only: bool = False) -> tuple[dict, int]:
    started = time.monotonic()
    deadline = started + budget
    root = real(repo)
    report: dict = {"repo_root": str(root), "before": None, "after": None, "action": "none",
                    "usable": False, "claim_scope": "none", "healed": False, "seconds": 0.0,
                    "fingerprint": "", "receipt": {}, "gaps": [], "gaps_total": 0, "next": ""}
    family, seed = classify(root)
    if family is None:
        report.update(action="out_of_scope", next="repo is outside the crg-heal allowlist")
        return report, EXIT_BLOCKED
    try:
        with locked(state_dir() / f"{sha12(root)}.lock", deadline):
            code = _heal_locked(root, family, seed, report, deadline, no_clone, clone_only)
    except BusyError as busy:
        report.update(action="busy", next=str(busy))
        return _finish(report, started), EXIT_BUSY
    return _finish(report, started), code


def _heal_locked(root: Path, family: str, seed: Path, report: dict, deadline: float,
                 no_clone: bool, clone_only: bool) -> int:
    before, doc = read(root)
    report["before"] = before
    after, forced, note, action = before, -1, "", "none"
    if before in ("missing_graph", "rebuild_required"):
        note = blocked_next(root, family, seed, before, no_clone)
        if not note:
            bootstrap = root == seed and root != real(PMS_ROOT)
            action, forced, note = clone(root, real(PMS_ROOT) if bootstrap else seed, before,
                                         deadline, bootstrap)
            after, doc = read(root)
    elif clone_only:
        note = "--clone-only: status is not missing_graph or rebuild_required; nothing to clone"
    elif before == "stale_graph":
        action, forced, note = refresh(root, family, seed, deadline, no_clone)
        after, doc = read(root)
    elif before == "stale_worktree":
        tool_id = f"{tool_version()}|{doc.get('contract_version') or 'unknown'}"
        report["fingerprint"] = fp = fingerprint(root, tool_id)
        if attempted(root, before, fp):
            note = "update already attempted at this fingerprint (HEAD and worktree unchanged)"
        else:
            action, forced, note = refresh(root, family, seed, deadline, no_clone)
            after, doc = read(root)
            if after in READY:
                forget(root)
            else:
                remember(root, after, fp)
    elif before == "building":
        action = "poll"
        after, doc = poll(root, deadline)
        if after == "building":
            forced, note = EXIT_BUSY, "a build is still running; retry later"
    elif before not in READY | DEGRADED:
        note = f"graph status is {before}: heal never updates or clones it"
    report.update(
        after=after, action=action, usable=after in READY | DEGRADED,
        claim_scope="full" if after in READY else "degraded" if after in DEGRADED else "none",
        healed=action != "none" and exit_for(after) < exit_for(before),
        receipt=receipt(doc), next=note,
    )
    report["gaps"], report["gaps_total"] = gaps(doc)
    return forced if forced >= 0 else exit_for(after)


def _finish(report: dict, started: float) -> dict:
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="graph root to heal (allowlisted paths only)")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument("--budget", type=float, default=BUDGET_SECONDS,
                        help="seconds for locks, polling and the clone (default 240)")
    parser.add_argument("--no-clone", action="store_true",
                        help="never clone from the seed; report instead")
    parser.add_argument("--clone-only", action="store_true",
                        help="skip update/poll healing: only clone a missing_graph or "
                             "rebuild_required root from the validated seed")
    args = parser.parse_args()
    try:
        report, code = heal(args.repo, args.budget, args.no_clone, args.clone_only)
    except OSError as error:
        report = {"repo_root": args.repo, "before": None, "after": None, "action": "none",
                  "usable": False, "claim_scope": "none", "healed": False, "seconds": 0.0,
                  "fingerprint": "", "receipt": {}, "gaps": [], "gaps_total": 0,
                  "next": f"crg-heal failed: {error}"}
        code = EXIT_BLOCKED
    if args.json:
        print(json.dumps(report))
    else:
        print(f"crg-heal: {report['action']} {report['before']} -> {report['after']} "
              f"usable={report['usable']} exit={code} {report['next']}".rstrip())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
