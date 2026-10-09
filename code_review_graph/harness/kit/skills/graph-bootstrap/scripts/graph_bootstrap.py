#!/usr/bin/env python3
"""Seed a fresh worktree's code graph from a seed checkout's graph.

A fresh worktree would otherwise need a full build (hours on a large repo).
``code-review-graph clone-graph`` copies the seed's graph with the SQLite
backup API, re-roots every stored path, rebuilds the search index and runs an
incremental update to the worktree's HEAD. This script only drives the CLI
and checks the resulting readiness; it never opens the graph database.

Prints one JSON object. Exit 0 only when the worktree graph is ``ok``; a
``partial_index`` or stale result is ``degraded`` and, like every failure,
exits 2 so the caller marks the graph lane degraded instead of building.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

GIT_HEAD_SECONDS = 30
SEED_REFRESH_SECONDS = 120
CLONE_SECONDS = 520
STATUS_SECONDS = 30
SEED_BUILD_WAIT_SECONDS = 120
# Worst case: the git probe, the seed status, the building-seed poll (wait plus
# one status read), a stale-seed refresh, the clone plus its catch-up, and the
# verify read; callers must wait longer than this total.
TOTAL_BUDGET_SECONDS = (GIT_HEAD_SECONDS + STATUS_SECONDS
                        + SEED_BUILD_WAIT_SECONDS + STATUS_SECONDS
                        + SEED_REFRESH_SECONDS + CLONE_SECONDS + STATUS_SECONDS)
assert TOTAL_BUDGET_SECONDS <= 880, TOTAL_BUDGET_SECONDS

LOCK_BUSY = 75
UPDATE_LOCK_WAIT_SECONDS = 60
SEED_POLL_SECONDS = 5
TEMPORARY_ROOTS = (Path("/tmp"), Path("/private/tmp"), Path("/var/folders"))  # nosec B108
# Compared resolved: on macOS /tmp is a symlink to /private/tmp and scratch
# checkouts resolve to /private/var/folders, so the raw spellings above never
# match a resolved worktree. Compared only, never created under.
_TEMPORARY_ROOTS_RESOLVED = tuple(p.resolve() for p in TEMPORARY_ROOTS)
# partial_index is not a seed: its gaps would be copied into every clone.
USABLE_SEED = {"ok", "stale_graph", "stale_worktree"}
STALE = {"stale_graph", "stale_worktree"}


class StageError(RuntimeError):
    def __init__(self, stage: str, error: str) -> None:
        super().__init__(error)
        self.stage = stage


def graph_binary() -> str:
    binary = os.environ.get("CRG_BIN") or shutil.which("code-review-graph")
    if not binary:
        raise StageError("setup", "code-review-graph is not on PATH")
    return binary


def run(stage: str, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    # Embeddings stay off: a bootstrap must not start model downloads or re-embedding.
    env = {**os.environ, "CRG_EMBEDDINGS": "off"}
    try:
        return subprocess.run(
            [graph_binary(), *args], capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        raise StageError(stage, f"{args[0]} timed out after {timeout:g}s") from None
    except OSError as error:
        raise StageError(stage, f"{args[0]} failed to start: {error}") from None


def readiness(stage: str, root: Path, timeout: float) -> tuple[str, dict]:
    result = run(stage, ["status", "--repo", str(root), "--json"], timeout)
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return "unavailable", {}
    block = doc.get("readiness")
    status = block.get("status") if isinstance(block, dict) else None
    return (status if isinstance(status, str) else "unavailable"), doc


def git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=GIT_HEAD_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def is_temporary_root(root: Path) -> bool:
    """Whether *root* is a WORKTREE that must not receive a graph: scratch tmp or an in-flight
    worktree. Only the worktree target is ever checked; the seed may live anywhere.
    ``CRG_ALLOW_TEMPORARY_ROOT=1`` opts out (tests: their hermetic HOME lives
    under pytest's tmp dir by construction)."""
    if os.environ.get("CRG_ALLOW_TEMPORARY_ROOT"):
        return False
    resolved = root.resolve()
    return (".orca-preparing" in resolved.parts
            or any(root == tmp or root.is_relative_to(tmp) for tmp in TEMPORARY_ROOTS)
            or any(resolved == tmp or resolved.is_relative_to(tmp)
                   for tmp in _TEMPORARY_ROOTS_RESOLVED))


def wait_out_build(seed: Path, wait_seconds: float) -> str:
    """Poll a ``building`` seed until it leaves that state or *wait_seconds* run out."""
    deadline = time.monotonic() + wait_seconds
    interval = min(SEED_POLL_SECONDS, wait_seconds)
    while True:
        time.sleep(max(min(interval, deadline - time.monotonic()), 0))
        status, _ = readiness("seed-status", seed, STATUS_SECONDS)
        if status != "building" or time.monotonic() >= deadline:
            return status


def catch_up(worktree: Path, seconds: float) -> str:
    """One ``update`` after a ``--no-update`` clone; '' when it finished, else why it did not.

    A timeout leaves the re-rooted clone readable: ``stale_graph``, or ``partial_index`` when
    the killed update had opened its write epoch (the next update recovers it).
    """
    if seconds <= 0:
        return "no budget left for the post-clone update"
    try:
        done = run("update", ["update", "--skip-flows", "--if-locked=wait", "--lock-wait",
                              str(UPDATE_LOCK_WAIT_SECONDS), "--repo", str(worktree)], seconds)
    except StageError as error:
        return str(error)
    return "" if done.returncode in (0, 3) else f"update exited {done.returncode}"


def bootstrap(worktree: Path, seed: Path, refresh_seconds: float, clone_seconds: float,
              force: bool = True, lock_wait: float | None = None,
              update_seconds: float | None = None,
              seed_build_wait_seconds: float = SEED_BUILD_WAIT_SECONDS) -> dict:
    """Clone *seed* into *worktree*. Only *worktree* passes ``is_temporary_root``, never the
    seed. With *update_seconds* the clone runs ``--no-update`` and the catch-up update is its
    own stage, cut at that timeout or the end of *clone_seconds*."""
    worktree = Path(worktree)
    if is_temporary_root(worktree):
        return {"status": "skip", "reason": "temporary root"}
    worktree = worktree.resolve()
    head = git_head(worktree)
    if not head:
        return {"status": "skip", "reason": "worktree is not a git checkout"}
    seed_status, _ = readiness("seed-status", seed, STATUS_SECONDS)
    if seed_status == "building":
        seed_status = wait_out_build(seed, seed_build_wait_seconds)
    if seed_status not in USABLE_SEED:
        reason = (f"seed graph is still building after {seed_build_wait_seconds:g}s"
                  if seed_status == "building" else f"seed graph is {seed_status}")
        return {"status": "skip", "reason": reason, "seed_readiness": seed_status}
    refresh_note = ""
    if seed_status in STALE and refresh_seconds > 0:
        refresh = run("seed-refresh", ["update", "--skip-flows", "--if-locked=skip",
                                       "--repo", str(seed)], refresh_seconds)
        if refresh.returncode == LOCK_BUSY:
            refresh_note = "seed is being updated by another writer; cloned its last snapshot"
        elif refresh.returncode not in (0, 3):
            refresh_note = f"seed refresh exited {refresh.returncode}: " + (
                refresh.stderr.strip()[-200:]
            )
    started = time.time()
    clone_args = ["clone-graph", "--from", str(seed), "--to", str(worktree), "--json"]
    if force:
        clone_args.append("--force")
    if update_seconds is not None:
        clone_args.append("--no-update")
    if lock_wait is not None:
        clone_args += ["--lock-wait", f"{lock_wait:g}"]
    clone = run("clone-graph", clone_args, clone_seconds)
    if clone.returncode != 0:
        raise StageError("clone-graph", f"exit {clone.returncode}: "
                         + (clone.stderr or clone.stdout).strip()[-300:])
    try:
        cloned = json.loads(clone.stdout)
    except ValueError:
        cloned = {}
    update_note = ""
    if update_seconds is not None:
        left = clone_seconds - (time.time() - started)
        update_note = catch_up(worktree, min(update_seconds, left))
    status, doc = readiness("verify", worktree, STATUS_SECONDS)
    if status == "ok":
        verdict = "ok"
    elif status == "partial_index" or status in STALE:
        verdict = "degraded"
    else:
        verdict = "failed"
    return {
        "status": verdict,
        "readiness": status,
        "reasons": (doc["readiness"].get("reasons", [])
                    if isinstance(doc.get("readiness"), dict) else []),
        "seconds": round(time.time() - started, 1),
        "rows_rewritten": cloned.get("rows_rewritten") if isinstance(cloned, dict) else None,
        "fts_rows": cloned.get("fts_rows") if isinstance(cloned, dict) else None,
        "nodes": doc.get("nodes"),
        "files": doc.get("files"),
        "built_at_commit": str(doc.get("built_at_commit") or "")[:12],
        "worktree_head": head[:12],
        "seed_readiness": seed_status,
        "seed_refresh_note": refresh_note,
        "update_note": update_note,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worktree", required=True, type=Path,
                        help="the freshly created worktree to seed")
    parser.add_argument("--seed", required=True, type=Path,
                        help="a checkout holding a current graph (e.g. a develop-pinned worktree)")
    parser.add_argument("--refresh-seed-seconds", type=float, default=SEED_REFRESH_SECONDS,
                        help="update a stale seed first, for at most this long (0 skips)")
    parser.add_argument("--clone-seconds", type=float, default=CLONE_SECONDS,
                        help="time allowed for clone-graph including its update")
    parser.add_argument("--seed-build-wait-seconds", type=float,
                        default=SEED_BUILD_WAIT_SECONDS,
                        help="poll a building seed for at most this long before skipping")
    parser.add_argument("--bootstrap-graph", action="store_true",
                        help="accepted from the pipeline's prepare step")
    args = parser.parse_args()
    budget = (GIT_HEAD_SECONDS + SEED_BUILD_WAIT_SECONDS + args.refresh_seed_seconds
              + args.clone_seconds + 3 * STATUS_SECONDS)
    try:
        result = bootstrap(args.worktree, args.seed.resolve(),
                           args.refresh_seed_seconds, args.clone_seconds,
                           seed_build_wait_seconds=args.seed_build_wait_seconds)
    except StageError as error:
        result = {"status": "failed", "stage": error.stage, "error": str(error)}
    result["budget_seconds"] = budget
    print(json.dumps(result))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
