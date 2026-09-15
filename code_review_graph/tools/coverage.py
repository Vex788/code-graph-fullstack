"""Disk-vs-graph completeness report.

Compares the repository's parseable inventory (:func:`collect_all_files`)
with the File nodes actually stored in the graph, so a silent indexing gap
(a new extension the parser does not know, an over-eager ignore pattern, a
build that never ran) becomes one visible number instead of an empty query
result.

The walker, ignore matcher, binary heuristic, and language detection are
imported from :mod:`code_review_graph.incremental` / :mod:`parser` — this
module never re-implements them, so the report and the build can never
disagree about what counts as "should be indexed".
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from ..graph import GraphStore
from ..incremental import (
    _is_binary,
    _load_ignore_patterns,
    _should_ignore,
    collect_all_files,
    get_all_tracked_files,
    get_db_path,
)
from ..parser import CodeParser, normalize_file_path
from ._common import _bounded, _error_response, _resolve_root

_GIT_TIMEOUT_SECONDS = 10

# Sample cap per list in the report; ``*_total`` always carries the real count
# (#849 follow-up: every tool response must survive a client context window).
_SAMPLE_CAP = 50

# First-match order is deliberate: a reason that would keep the file out of
# the inventory even after ``git add`` (ignored, binary, no_language) is more
# actionable than "untracked", which the next commit fixes by itself.
_EXCLUSION_REASONS = ("ignored", "binary", "no_language", "untracked")


def _untracked_files(repo_root: Path) -> list[str]:
    """List untracked, not-gitignored files (``git ls-files --others``).

    The inventory is tracked-only, so these files can never appear in the
    graph; they are reported as excluded rather than silently absent.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _display_path(root: Path, path: str) -> str:
    """Prefer a repo-relative spelling; keep absolute paths from other roots."""
    try:
        return str(Path(path).relative_to(root)).replace("\\", "/")
    except ValueError:
        return path


def _capped(files: list[str]) -> dict[str, Any]:
    sample, total, truncated = _bounded(sorted(files), _SAMPLE_CAP, _SAMPLE_CAP)
    return {"count": total, "files": sample, "truncated": truncated}


def coverage_report(
    repo_root: Optional[str] = None,
    *,
    store: Optional[GraphStore] = None,
) -> dict[str, Any]:
    """Build the disk-vs-graph coverage report for one repository.

    Args:
        repo_root: Repository root. Auto-detected from the current directory
            when omitted.
        store: An already-open graph store to read from. When omitted, the
            default database is opened read-only — and a missing database is
            an error response, never a newly created ``graph.db``.

    Returns a dict whose key lists are capped at ``_SAMPLE_CAP`` entries;
    ``missing_from_graph_total`` / ``stale_in_graph_total`` and the per-reason
    ``excluded[reason]["count"]`` always report the true counts.
    """
    try:
        root = _resolve_root(repo_root)
    except ValueError as exc:
        return _error_response(str(exc))

    inventory = collect_all_files(root)
    inventory_abs = {normalize_file_path(root / rel) for rel in inventory}

    owned_store = store is None
    if store is None:
        db_path = get_db_path(root, read_only=True)
        if not db_path.exists():
            return _error_response(
                f"no graph found at {db_path} — run `code-review-graph build` first",
            )
        store = GraphStore(db_path)
    try:
        rows = store._conn.execute(  # noqa: SLF001 - same convention as build/context/navigation tools
            "SELECT file_path, language FROM nodes WHERE kind = 'File'"
        ).fetchall()
    finally:
        if owned_store:
            store.close()

    graph_files: list[tuple[str, str]] = [
        (normalize_file_path(row["file_path"]), row["language"] or "unknown") for row in rows
    ]
    graph_paths = {path for path, _ in graph_files}

    missing_from_graph = sorted(inventory_abs - graph_paths)
    stale_in_graph = sorted(path for path in graph_paths if not Path(path).is_file())

    parser = CodeParser(root)
    ignore_patterns = _load_ignore_patterns(root)

    excluded_files = {reason: [] for reason in _EXCLUSION_REASONS}
    tracked = set(get_all_tracked_files(root))
    candidates = tracked | set(_untracked_files(root))
    for rel_path in candidates:
        if normalize_file_path(root / rel_path) in inventory_abs:
            continue
        full_path = root / rel_path
        # Tracked-but-deleted paths are not an exclusion decision; stale
        # detection on the graph side owns them.
        if not full_path.is_file() or full_path.is_symlink():
            continue
        if _should_ignore(rel_path, ignore_patterns):
            excluded_files["ignored"].append(rel_path)
        elif _is_binary(full_path):
            excluded_files["binary"].append(rel_path)
        elif parser.detect_language(full_path) is None:
            excluded_files["no_language"].append(rel_path)
        elif rel_path not in tracked:
            excluded_files["untracked"].append(rel_path)

    excluded = {reason: _capped(files) for reason, files in excluded_files.items()}
    excluded_total = sum(entry["count"] for entry in excluded.values())

    by_language = Counter(language for _, language in graph_files)
    inventory_by_language = Counter(
        parser.detect_language(root / rel) or "unknown" for rel in inventory
    )

    indexed_count = len(inventory_abs) - len(missing_from_graph)
    return {
        "status": "ok" if not missing_from_graph else "incomplete",
        "summary": (
            f"{indexed_count}/{len(inventory_abs)} inventory files indexed, "
            f"{len(stale_in_graph)} stale graph files, "
            f"{excluded_total} excluded"
        ),
        "repo_root": str(root),
        "inventory_count": len(inventory_abs),
        "graph_file_count": len(graph_paths),
        "indexed_count": indexed_count,
        "missing_from_graph": [
            _display_path(root, path) for path in missing_from_graph[:_SAMPLE_CAP]
        ],
        "missing_from_graph_total": len(missing_from_graph),
        "missing_from_graph_truncated": len(missing_from_graph) > _SAMPLE_CAP,
        "stale_in_graph": [_display_path(root, path) for path in stale_in_graph[:_SAMPLE_CAP]],
        "stale_in_graph_total": len(stale_in_graph),
        "stale_in_graph_truncated": len(stale_in_graph) > _SAMPLE_CAP,
        "excluded": excluded,
        "excluded_total": excluded_total,
        "by_language": dict(sorted(by_language.items())),
        "inventory_by_language": dict(sorted(inventory_by_language.items())),
    }


def format_coverage_text(report: dict[str, Any]) -> str:
    """Render :func:`coverage_report` output as a human-readable block."""
    lines = [f"Coverage: {report.get('summary', 'n/a')}"]

    by_language = report.get("by_language") or {}
    if by_language:
        joined = ", ".join(f"{lang}={count}" for lang, count in by_language.items())
        lines.append(f"  Graph File nodes by language: {joined}")
    inventory_by_language = report.get("inventory_by_language") or {}
    if inventory_by_language:
        joined = ", ".join(f"{lang}={count}" for lang, count in inventory_by_language.items())
        lines.append(f"  Inventory by language:        {joined}")

    excluded = report.get("excluded") or {}
    if excluded:
        joined = ", ".join(
            f"{reason}={entry.get('count', 0)}" for reason, entry in excluded.items()
        )
        lines.append(f"  Excluded: {joined}")

    def _listing(title: str, files: list[str], total: int, truncated: bool) -> None:
        lines.append(f"  {title} ({total})")
        for path in files:
            lines.append(f"    {path}")
        if truncated:
            shown = len(files)
            lines.append(f"    ... and {total - shown} more")

    missing = report.get("missing_from_graph") or []
    if report.get("missing_from_graph_total"):
        _listing(
            "Missing from graph (run `code-review-graph build`):",
            missing,
            report.get("missing_from_graph_total", 0),
            bool(report.get("missing_from_graph_truncated")),
        )
    stale = report.get("stale_in_graph") or []
    if report.get("stale_in_graph_total"):
        _listing(
            "Stale in graph (deleted from disk):",
            stale,
            report.get("stale_in_graph_total", 0),
            bool(report.get("stale_in_graph_truncated")),
        )

    return "\n".join(lines)
