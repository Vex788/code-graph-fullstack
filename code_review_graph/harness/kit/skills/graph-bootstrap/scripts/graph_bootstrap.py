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

SEED_REFRESH_SECONDS = 180
CLONE_SECONDS = 600
STATUS_SECONDS = 30
# Two status reads plus the refresh and the clone; callers must wait longer.
TOTAL_BUDGET_SECONDS = 840
assert SEED_REFRESH_SECONDS + CLONE_SECONDS + 2 * STATUS_SECONDS <= TOTAL_BUDGET_SECONDS

LOCK_BUSY = 75
USABLE_SEED = {"ok", "partial_index", "stale_graph", "stale_worktree"}
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
        text = (result.stderr or result.stdout).strip()
        return ("missing_graph" if "No graph found" in text else "unavailable"), {}
    status = (doc.get("readiness") or {}).get("status")
    return (status if isinstance(status, str) else "unavailable"), doc


def git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def bootstrap(worktree: Path, seed: Path, refresh_seconds: float, clone_seconds: float) -> dict:
    head = git_head(worktree)
    if not head:
        return {"status": "skip", "reason": "worktree is not a git checkout"}
    seed_status, _ = readiness("seed-status", seed, STATUS_SECONDS)
    if seed_status not in USABLE_SEED:
        return {"status": "skip", "reason": f"seed graph is {seed_status}",
                "seed_readiness": seed_status}
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
    clone = run("clone-graph", ["clone-graph", "--from", str(seed), "--to", str(worktree),
                                "--json", "--force"], clone_seconds)
    if clone.returncode != 0:
        raise StageError("clone-graph", f"exit {clone.returncode}: "
                         + (clone.stderr or clone.stdout).strip()[-300:])
    try:
        cloned = json.loads(clone.stdout)
    except ValueError:
        cloned = {}
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
        "reasons": (doc.get("readiness") or {}).get("reasons", []),
        "seconds": round(time.time() - started, 1),
        "rows_rewritten": cloned.get("rows_rewritten") if isinstance(cloned, dict) else None,
        "fts_rows": cloned.get("fts_rows") if isinstance(cloned, dict) else None,
        "nodes": doc.get("nodes"),
        "files": doc.get("files"),
        "built_at_commit": str(doc.get("built_at_commit") or "")[:12],
        "worktree_head": head[:12],
        "seed_readiness": seed_status,
        "seed_refresh_note": refresh_note,
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
    parser.add_argument("--bootstrap-graph", action="store_true",
                        help="accepted from the pipeline's prepare step")
    args = parser.parse_args()
    budget = args.refresh_seed_seconds + args.clone_seconds + 2 * STATUS_SECONDS
    try:
        result = bootstrap(args.worktree.resolve(), args.seed.resolve(),
                           args.refresh_seed_seconds, args.clone_seconds)
    except StageError as error:
        result = {"status": "failed", "stage": error.stage, "error": str(error)}
    result["budget_seconds"] = budget
    print(json.dumps(result))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
