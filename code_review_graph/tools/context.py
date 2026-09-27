"""Tool: get_minimal_context — ultra-compact context for token-efficient workflows."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from ..incremental import get_db_path
from ..migrations import SchemaMigrationPending, SchemaTooNewError
from ..parser import normalize_file_path
from ..readiness import GIT_OK, GIT_UNAVAILABLE, ReadinessStatus
from ..readiness_facts import gather_report
from ._common import (
    _get_store,
    _resolve_root,
    building_response,
    compact_response,
    graph_provenance,
    schema_error_response,
    sibling_graph_root,
)

logger = logging.getLogger(__name__)


def _short(root: Path, absolute: str) -> str:
    """Repo-relative path for a response; absolute paths bloat a 100-token reply."""
    try:
        return str(Path(absolute).relative_to(root))
    except ValueError:
        return absolute


def _not_ready(
    reason: str,
    summary: str,
    next_tool_suggestions: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Return a compact response that directs callers to initialize the graph."""
    response: dict[str, Any] = {
        "status": "not_ready",
        "reason": reason,
        "summary": summary,
        "next_tool_suggestions": next_tool_suggestions or ["build_or_update_graph"],
    }
    response.update(extra)
    return response


def missing_graph_response(root: Path) -> dict[str, Any] | None:
    """``not_ready`` when *root* has no graph database, else None.

    Checked read-only so a query against a cold root never creates an empty DB.
    """
    if get_db_path(root, read_only=True).is_file():
        return None
    sibling = sibling_graph_root(root)
    if sibling is not None:
        return _not_ready(
            "worktree_no_graph",
            f"This worktree has no graph, but its main checkout at {sibling} does. "
            "Query that root: it is authoritative for everything this branch did "
            "not touch, and silent about symbols the branch adds -- take those "
            "from the diff, not from an empty graph result.",
            next_tool_suggestions=["orient", "query_graph", "get_impact_radius"],
            graph_repo_root=str(sibling),
            graph_provenance=graph_provenance(str(sibling)),
        )
    return _not_ready(
        "missing_graph",
        "No graph database found. Build the graph before requesting context.",
    )


def _has_git_changes(root: Path, base: str) -> bool:
    """Quick check for uncommitted or diffed changes."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base, "--"],
            capture_output=True, stdin=subprocess.DEVNULL, text=True,
            cwd=str(root), timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
        # Also check staged/unstaged
        result2 = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, stdin=subprocess.DEVNULL, text=True,
            cwd=str(root), timeout=10,
        )
        return bool(result2.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_minimal_context(
    task: str = "",
    changed_files: list[str] | None = None,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Return minimum context an agent needs to start any task (~100 tokens).

    Combines graph stats, top communities, top flows, risk score,
    and suggested next tools into an ultra-compact response.

    Args:
        task: Natural language description of what the agent is doing
              (e.g. "review PR #42", "debug login timeout").
        changed_files: Explicit changed files. Auto-detected from git if None.
        repo_root: Repository root path. Auto-detected if None.
        base: Git ref for diff comparison.

    Returns:
        Compact graph context, or ``status: not_ready`` when the graph is
        missing, empty, or known to have been built at a different Git commit.
    """
    root = _resolve_root(repo_root)
    missing = missing_graph_response(root)
    if missing is not None:
        return missing

    try:
        report = gather_report(root, get_db_path(root, read_only=True))
    except SchemaTooNewError as exc:
        return schema_error_response(exc)
    readiness = report.readiness
    status_block = readiness.to_dict()
    if readiness.status is ReadinessStatus.BUILDING:
        return building_response(status_block)
    if "index_generation_mismatch" in readiness.reasons:
        return _not_ready(
            "rebuild_required",
            "The graph was indexed by an incompatible build. Run a full rebuild.",
            readiness=status_block,
        )

    try:
        store, root = _get_store(str(root))
    except SchemaMigrationPending:
        return building_response(status_block)
    try:
        # 1. Quick stats
        stats = store.get_stats()
        if stats.total_nodes == 0:
            return _not_ready(
                "empty_graph",
                "The graph database contains no nodes. Build the graph before requesting context.",
                readiness=status_block,
            )

        facts = report.facts
        if facts.git_state == GIT_UNAVAILABLE:
            return _not_ready(
                "git_unavailable",
                "git could not report HEAD or the working tree, so the graph's "
                "freshness cannot be checked. Fix git, then retry.",
                readiness=status_block,
            )
        if facts.git_state == GIT_OK and not facts.built_at_commit:
            return _not_ready(
                "no_build_anchor",
                "The graph never recorded the commit it was built at, so its "
                "freshness cannot be checked. Rebuild it before requesting context.",
                readiness=status_block,
            )
        if "head_moved" in readiness.reasons:
            return _not_ready(
                "stale_graph",
                "The graph was built at a different Git commit. "
                "Update it before requesting context.",
                readiness=status_block,
            )

        # Commit identity says nothing about uncommitted work. A file the graph
        # has never seen, or an indexed one that is gone, makes a query lie about
        # what exists, so it blocks; edited-but-indexed files only warn, because
        # going red there would paint every active editing session red and send
        # agents to grep.
        drift = report.drift or {
            "missing": [], "mismatched": [], "deleted": [], "check": "unavailable",
        }
        drifted = drift["missing"] + drift["deleted"]
        if drifted:
            return _not_ready(
                "stale_worktree",
                f"{len(drifted)} file(s) on disk have no node in the graph or were "
                "deleted after indexing; update it before asking what exists.",
                drifted_files=[_short(root, p) for p in drifted[:10]],
                drifted_file_count=len(drifted),
                readiness=status_block,
            )

        # 2. Risk from changed files
        risk = "unknown"
        risk_score = 0.0
        top_affected: list[str] = []
        test_gap_count = 0
        if changed_files or _has_git_changes(root, base):
            try:
                from ..changes import analyze_changes
                from ..incremental import get_changed_files as _get_changed

                files = changed_files
                if not files:
                    files = _get_changed(root, base)
                if files:
                    abs_files = [normalize_file_path(root / f) for f in files]
                    analysis = analyze_changes(
                        store, abs_files, repo_root=str(root), base=base,
                    )
                    risk_score = analysis.get("risk_score", 0.0)
                    risk = (
                        "high" if risk_score > 0.7
                        else "medium" if risk_score > 0.4
                        else "low"
                    )
                    top_affected = [
                        f.get("name", "")
                        for f in analysis.get("changed_functions", [])[:5]
                    ]
                    test_gap_count = len(analysis.get("test_gaps", []))
            except (
                ImportError, OSError, ValueError,
                sqlite3.Error, subprocess.SubprocessError,
            ):
                logger.debug("Risk analysis failed in get_minimal_context", exc_info=True)

        # 3. Top 3 communities
        communities: list[str] = []
        try:
            rows = store._conn.execute(
                "SELECT name FROM communities ORDER BY size DESC LIMIT 3"
            ).fetchall()
            communities = [r[0] for r in rows]
        except sqlite3.OperationalError:  # nosec B110 — table may not exist yet
            logger.debug("communities table not yet populated")

        # 4. Top 3 critical flows
        flows: list[str] = []
        try:
            rows = store._conn.execute(
                "SELECT name FROM flows ORDER BY criticality DESC LIMIT 3"
            ).fetchall()
            flows = [r[0] for r in rows]
        except sqlite3.OperationalError:  # nosec B110 — table may not exist yet
            logger.debug("flows table not yet populated")

        # 5. Suggest next tools based on task keywords
        task_lower = task.lower()
        if any(w in task_lower for w in ("review", "pr", "merge", "diff")):
            suggestions = ["detect_changes", "get_affected_flows", "get_review_context"]
        elif any(w in task_lower for w in ("debug", "bug", "error", "fix")):
            suggestions = ["semantic_search_nodes", "query_graph", "get_flow"]
        elif any(w in task_lower for w in ("refactor", "rename", "dead", "clean")):
            suggestions = ["refactor", "find_large_functions", "get_architecture_overview"]
        elif any(w in task_lower for w in ("onboard", "understand", "explore", "arch")):
            suggestions = [
                "get_architecture_overview", "list_communities", "list_flows",
            ]
        else:
            suggestions = [
                "detect_changes", "semantic_search_nodes",
                "get_architecture_overview",
            ]

        # Build summary
        summary_parts = [
            f"{stats.total_nodes} nodes, {stats.total_edges} edges"
            f" across {stats.files_count} files.",
        ]
        if risk != "unknown":
            summary_parts.append(f"Risk: {risk} ({risk_score:.2f}).")
        if test_gap_count:
            summary_parts.append(f"{test_gap_count} test gaps.")

        response = compact_response(
            summary=" ".join(summary_parts),
            key_entities=top_affected or None,
            risk=risk,
            communities=communities or None,
            flows_affected=flows or None,
            next_tool_suggestions=suggestions,
        )
        # The graph still finds these symbols; their bodies and line numbers may
        # be behind the working tree. A label, not a refusal.
        edited = drift["mismatched"]
        if edited:
            response["stale_files"] = [_short(root, p) for p in edited[:10]]
            response["stale_file_count"] = len(edited)
        if drift["check"] != "full":
            response["content_check"] = drift["check"]
        if facts.failed_files or facts.resolver_failures:
            response["failed_files"] = facts.failed_files
            response["resolver_failures"] = facts.resolver_failures
        response["readiness"] = status_block
        # ok only when readiness is ok; partial_index is the contract's degraded value.
        response["status"] = readiness.status.value
        return response
    finally:
        store.close()
