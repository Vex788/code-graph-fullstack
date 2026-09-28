"""Per-stage indexing timings: full build, no-op update, one-file update.

Every target is copied into a temporary directory first, so a run never
touches a repository's own ``.code-review-graph`` data. Stages are timed with
``time.perf_counter`` around public calls; nothing in the build path is
patched. Where a stage cannot be separated without editing production code
the result says so in ``notes`` instead of guessing.

Usage::

    python -m code_review_graph.eval.benchmarks.stage_timing --out docs/perf/baseline.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable

_GIT_IDENTITY = ["-c", "user.email=bench@example.invalid", "-c", "user.name=bench"]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_GENERATOR = _REPO_ROOT / "tests" / "fixtures" / "fullstack_stripes_gen.py"
_NOTES = [
    "full_build_s covers parse, store and the post-parse resolvers; full_build() runs them "
    "together, so resolvers_s is measured by re-running each resolver on the built graph "
    "and parse_store_s is full_build_s minus that sum (an estimate).",
    "postprocess_s wraps the whole post-build step; postprocess_timing is its own per-stage "
    "breakdown. The difference is bare/C++ call-target resolution, which it does not time.",
    "noop_update and one_file_update time build_or_update_graph(full_rebuild=False), the "
    "path hooks and the MCP tool take; each one-file edit is committed first.",
]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *_GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return completed.stdout.strip()


def _timed(call: Callable[[], Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    value = call()
    return round(time.perf_counter() - started, 4), value


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 4) if values else 0.0


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("fullstack_stripes_gen", _GENERATOR)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(_GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_fork_copy(source: Path, dest: Path, ref: str = "HEAD") -> Path:
    """Clone *source* at *ref*; the checkout's own graph data stays untouched."""
    subprocess.run(
        ["git", "clone", "-q", str(source), str(dest)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    _git(dest, "checkout", "-q", "--detach", ref)
    return dest.resolve()


def prepare_fixture(dest: Path, scale: int) -> Path:
    """Generate the fullstack fixture at *scale* files as a one-commit repository."""
    _load_generator().generate(dest, scale=scale)
    _git(dest, "init", "-q")
    # Benchmark repos must not fire machine-level git hooks (a global
    # core.hooksPath can post-commit a background graph refresh, racing the
    # one-file update this benchmark times). A nonexistent hooks path runs
    # no hooks.
    _git(dest, "config", "core.hooksPath", str(dest / ".githooks-disabled"))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture")
    return dest.resolve()


def _append_edit(repo: Path, relative: str, index: int) -> None:
    path = repo / relative
    text = path.read_text(encoding="utf-8")
    if relative.endswith(".py"):
        text += f"\n\ndef _bench_edit_{index}():\n    return {index}\n"
    else:
        text += f"\n// bench edit {index}\n"
    path.write_text(text, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", f"bench edit {index}")


def _count_files(store: Any) -> int:
    return len(store.get_all_files())


def time_repo(repo: Path, edit_file: str, repeat: int = 3) -> dict[str, Any]:
    """Time a full build, *repeat* no-op updates and *repeat* one-file updates of *repo*."""
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build, get_db_path
    from code_review_graph.resolvers import RESOLVERS, run_resolver
    from code_review_graph.tools.build import _run_postprocess, build_or_update_graph

    result: dict[str, Any] = {"edit_file": edit_file}
    store = GraphStore(get_db_path(repo))
    try:
        full_s, built = _timed(lambda: full_build(repo, store))
        resolver_s: dict[str, float] = {}
        for name in RESOLVERS:
            resolver_s[name], _ = _timed(partial(run_resolver, name, store, repo))
        build_result: dict[str, Any] = {}
        post_s, warnings = _timed(
            lambda: _run_postprocess(store, build_result, "full", full_rebuild=True),
        )
        stats = store.get_stats()
        result["graph"] = {
            "files": _count_files(store),
            "nodes": stats.total_nodes,
            "edges": stats.total_edges,
            "parse_errors": len(built["errors"]),
        }
        result["full_build"] = {
            "full_build_s": full_s,
            "resolvers_s": resolver_s,
            "parse_store_s": round(max(0.0, full_s - sum(resolver_s.values())), 4),
            "postprocess_s": post_s,
            "postprocess_timing": build_result.get("postprocess_timing", {}),
            "total_s": round(full_s + post_s, 4),
            "warnings": warnings,
        }
    finally:
        store.close()

    noop: list[float] = []
    for _ in range(repeat):
        seconds, outcome = _timed(
            lambda: build_or_update_graph(full_rebuild=False, repo_root=str(repo)),
        )
        if outcome.get("files_updated"):
            raise RuntimeError(f"no-op update re-parsed files: {outcome.get('summary')}")
        noop.append(seconds)
    result["noop_update"] = {"median_s": _median(noop), "runs_s": noop}

    one_file: list[float] = []
    last: dict[str, Any] = {}
    for index in range(repeat):
        _append_edit(repo, edit_file, index)
        seconds, last = _timed(
            lambda: build_or_update_graph(full_rebuild=False, repo_root=str(repo)),
        )
        if not last.get("files_updated"):
            raise RuntimeError(f"one-file update parsed nothing: {last.get('summary')}")
        one_file.append(seconds)
    result["one_file_update"] = {
        "median_s": _median(one_file),
        "runs_s": one_file,
        "files_updated": last.get("files_updated"),
        "dependent_files": len(last.get("dependent_files") or []),
        "postprocess_timing": last.get("postprocess_timing", {}),
    }
    return result


def machine_info(fork: Path | None = _REPO_ROOT, ref: str = "HEAD") -> dict[str, Any]:
    from code_review_graph import __version__

    try:
        sha = _git(fork, "rev-parse", ref) if fork is not None else None
    except (OSError, subprocess.CalledProcessError):
        sha = None
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_review_graph_version": __version__,
        "fork_git_head": sha,
        "package_path": str(Path(__file__).resolve().parents[2]),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "parse_executor": os.environ.get("CRG_PARSE_EXECUTOR", "auto"),
    }


def run(
    fork: Path | None = _REPO_ROOT,
    scale: int = 2000,
    repeat: int = 3,
    fork_ref: str = "HEAD",
) -> dict[str, Any]:
    """Time the fork checkout and the fixture at *scale*; either may be skipped."""
    report: dict[str, Any] = {
        "machine": machine_info(fork, fork_ref),
        "notes": _NOTES,
        "targets": {},
    }
    with tempfile.TemporaryDirectory(prefix="crg-stage-timing-") as tmp:
        tmp_path = Path(tmp)
        os.environ.setdefault("CRG_HOME", str(tmp_path / "crg-home"))
        if fork is not None:
            print("stage timing: fork copy (full build, no-op, one-file)", flush=True)
            repo = prepare_fork_copy(fork, tmp_path / "fork", fork_ref)
            report["targets"]["fork"] = time_repo(repo, "code_review_graph/hints.py", repeat)
            shutil.rmtree(repo, ignore_errors=True)
        if scale:
            print(f"stage timing: fixture at --scale {scale}", flush=True)
            repo = prepare_fixture(tmp_path / "fixture", scale)
            report["targets"][f"fixture_scale_{scale}"] = time_repo(
                repo, "src/main/java/com/acme/service/UserService.java", repeat,
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-stage indexing timings.")
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--fork", type=Path, default=_REPO_ROOT, help="git checkout to time")
    parser.add_argument("--fork-ref", default="HEAD", help="commit of --fork to time")
    parser.add_argument("--no-fork", action="store_true", help="skip the fork target")
    parser.add_argument("--scale", type=int, default=2000, help="fixture files (0 skips)")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    report = run(None if args.no_fork else args.fork, args.scale, args.repeat, args.fork_ref)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
