#!/usr/bin/env python3
"""Side-effect-free code graph readiness probe for a Git repository.

Reads only the code-review-graph CLI JSON (``status --json``, optionally
``coverage --json``); never opens the graph database. Exit 0 when the graph is
ready or degraded with data (``partial_index``, ``stale_graph``, ``stale_worktree``:
usable, gaps named), 2 when the status is blocking (``missing_graph``, ``building``,
``rebuild_required``, error): GRAPH_PREP_REQUIRED now means run ``crg-heal`` once,
then probe again; or the graph lane is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

PREP_MARKER = "GRAPH_PREP_REQUIRED"
READY = {"ok"}
DEGRADED = {"partial_index", "stale_graph", "stale_worktree"}
STATUS_TIMEOUT_SECONDS = 60
COVERAGE_TIMEOUT_SECONDS = 180
RULES_FILE = Path(__file__).resolve().parents[3] / "hooks" / "crg_rules.json"


def graph_binary() -> str | None:
    return os.environ.get("CRG_BIN") or shutil.which("code-review-graph")


def indexed_extensions() -> set[str] | None:
    """Extensions the graph indexes, from the kit's crg_rules.json; None when absent."""
    try:
        rules = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    extensions = rules.get("indexed_extensions") if isinstance(rules, dict) else None
    return set(extensions) if isinstance(extensions, list) else None


def run_json(args: list[str], timeout: float) -> tuple[dict | None, str]:
    """(parsed JSON object or None, diagnostic text) for one CLI call."""
    binary = graph_binary()
    if binary is None:
        return None, "code-review-graph is not on PATH"
    try:
        result = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"code-review-graph {args[0]} timed out after {timeout:g}s"
    except OSError as error:
        return None, f"code-review-graph {args[0]} failed to start: {error}"
    try:
        loaded = json.loads(result.stdout)
    except ValueError:
        loaded = None
    text = (result.stderr or result.stdout).strip()[-300:]
    return (loaded if isinstance(loaded, dict) else None), text


def read_status(repo: Path) -> tuple[str, dict, str]:
    """(readiness status, status JSON, diagnostic) from ``status --json``."""
    doc, text = run_json(["status", "--repo", str(repo), "--json"], STATUS_TIMEOUT_SECONDS)
    if doc is None:
        return "unavailable", {}, text
    if doc.get("status") == "error":
        return "unavailable", doc, str(doc.get("error_code") or text)
    readiness = doc.get("readiness")
    if isinstance(readiness, dict) and isinstance(readiness.get("status"), str):
        return readiness["status"], doc, ""
    return "unavailable", doc, "status --json has no readiness block (graph older than fs.6)"


def verdict_for(status: str) -> str:
    if status in READY:
        return "ready"
    if status in DEGRADED:
        return "degraded"
    return "unavailable" if status == "unavailable" else "prep_required"


def git_commit(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", ref + "^{commit}"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()


def changed_dispositions(
    changed: list[str], usable: bool, status: str, identity: dict, missing: set[str],
) -> list[dict]:
    extensions = indexed_extensions()
    unavailable = set(identity.get("missing_indexed_paths") or [])
    unavailable |= set(identity.get("deleted_indexed_paths") or [])
    unavailable |= set(identity.get("mismatched_indexed_paths") or [])
    excluded = tuple(identity.get("excluded_path_prefixes") or [])
    out = []
    for path in sorted(dict.fromkeys(changed)):
        if extensions is not None and Path(path).suffix.lower() not in extensions:
            continue
        if not usable:
            reason = f"graph-{status}"
        elif excluded and path.startswith(excluded):
            reason = "excluded-by-graph-policy"
        elif path in unavailable or path in missing:
            reason = "not-indexed"
        else:
            reason = "live-graph-receipt"
        disposition = "indexed" if reason == "live-graph-receipt" else "fallback"
        out.append({"path": path, "disposition": disposition, "reason": reason})
    return out


def probe(
    repo: Path, changed: list[str] | None = None, head: str | None = None,
    coverage: bool = False,
) -> dict:
    repo = repo.resolve()
    status, doc, diagnostic = read_status(repo)
    verdict = verdict_for(status)
    reasons = list((doc.get("readiness") or {}).get("reasons") or [])
    if diagnostic:
        reasons.append(diagnostic)
    head_sha = git_commit(repo, head) if head else ""
    if head_sha and verdict in {"ready", "degraded"}:
        if doc.get("current_sha") != head_sha:
            verdict = "prep_required"
            reasons.append("the checkout is not at the review head")
        elif doc.get("built_at_commit") != head_sha:
            verdict = "degraded"
            reasons.append("the graph was built at another commit than the review head")
    identity = doc.get("source_identity") if isinstance(doc.get("source_identity"), dict) else {}
    report: dict = {
        "repo": str(repo),
        "graph_status": status,
        "verdict": verdict,
        "marker": PREP_MARKER if verdict == "prep_required" else "",
        "reasons": reasons,
        "receipt": {
            key: doc.get(key)
            for key in ("repo_root", "built_at_commit", "current_sha", "nodes", "files",
                        "readiness", "source_identity", "contract_version", "index_generation")
            if key in doc
        },
    }
    missing: set[str] = set()
    if coverage and verdict in {"ready", "degraded"}:
        cov, text = run_json(["coverage", str(repo), "--json", "--no-fail"],
                             COVERAGE_TIMEOUT_SECONDS)
        if cov is None:
            report["coverage"] = {"status": "unavailable", "error": text}
        else:
            missing = set(cov.get("missing_from_graph") or [])
            report["coverage"] = {
                key: cov.get(key)
                for key in ("status", "inventory_count", "indexed_count",
                            "missing_from_graph_total", "missing_from_graph_truncated",
                            "excluded_total")
            }
            if cov.get("missing_from_graph_total"):
                report["verdict"] = "degraded"
                reasons.append(f"{cov['missing_from_graph_total']} files missing from the graph")
    usable = report["verdict"] in {"ready", "degraded"}
    report["changed_files"] = changed_dispositions(changed or [], usable, status, identity,
                                                   missing)
    report["changed_complete"] = all(
        item["disposition"] == "indexed" for item in report["changed_files"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--changed", action="append", default=[])
    parser.add_argument("--head", help="commit the graph must be built at")
    parser.add_argument("--coverage", action="store_true",
                        help="also run the coverage gate (slower)")
    args = parser.parse_args()
    if not args.repo.is_dir():
        parser.error(f"repository does not exist: {args.repo}")
    try:
        report = probe(args.repo, args.changed, args.head, args.coverage)
    except subprocess.CalledProcessError as error:
        parser.exit(2, f"graph_health.py: not a commit: {error.cmd[-1]}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["verdict"] in {"ready", "degraded"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
