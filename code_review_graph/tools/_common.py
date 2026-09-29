"""Shared utilities for tool sub-modules."""

from __future__ import annotations

import copy
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..constants import env_float
from ..graph import GraphStore
from ..incremental import GitUnavailableError, find_project_root, get_db_path
from ..parser import normalize_file_path

_PROVENANCE_READ_TIMEOUT_SECONDS = 0.05
_PROVENANCE_GIT_TIMEOUT_SECONDS = 1.0
# git status on every tool call; slower than this reads as git unavailable.
_RECEIPT_GIT_TIMEOUT_SECONDS = 5.0
# Rapid successive tool calls reuse one receipt; see _receipt_cache_key.
_RECEIPT_TTL_DEFAULT_SECONDS = 2.0

logger = logging.getLogger(__name__)


def _error_response(
    message: str, status: str = "error", **extra: Any,
) -> dict[str, Any]:
    """Build a standardised error response dict."""
    return {"status": status, "error": message, "summary": message, **extra}


def schema_error_response(exc: Exception) -> dict[str, Any]:
    """Contract error shape for a database newer than this build."""
    message = str(exc)
    return {
        "status": "error", "error_code": "schema_too_new",
        "message": message, "error": message, "summary": message,
    }


def building_response(readiness: dict[str, Any] | None = None) -> dict[str, Any]:
    """A writer holds the graph (build or migration); retry after it finishes."""
    message = (
        "The graph is being built or migrated by another process. "
        "Retry shortly, or check build_or_update_graph(status_only=True)."
    )
    response: dict[str, Any] = {
        "status": "building",
        "reason": "building",
        "summary": message,
        "next_tool_suggestions": ["build_or_update_graph_tool"],
    }
    if readiness is not None:
        response["readiness"] = readiness
    return response


def read_git_head_state(root: Path) -> tuple[str, str | None]:
    """Return ``(git_state, head_sha)`` using the readiness git states.

    ``not_a_repo`` when *root* has no ``.git``; ``unavailable`` when git
    failed, timed out, or has no HEAD yet.
    """
    from ..readiness import GIT_NOT_A_REPO, GIT_OK, GIT_UNAVAILABLE

    if not (root / ".git").exists():
        return GIT_NOT_A_REPO, None
    head = _read_live_git_head(root)
    return (GIT_OK, head) if head else (GIT_UNAVAILABLE, None)


def _read_live_git_head(root: Path) -> str | None:
    """Return the checked-out commit without making provenance mandatory.

    ``head_matches_build`` deliberately compares commits only. It does not
    claim that staged, unstaged, or untracked files are represented by the
    graph, avoiding the misleading ``is_stale=False`` contract from #458.
    """
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            timeout=_PROVENANCE_GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        logger.debug("Could not read live Git HEAD for graph provenance", exc_info=True)
        return None
    if result.returncode != 0:
        logger.debug("git rev-parse failed while reading graph provenance")
        return None
    head_sha = result.stdout.strip()
    return head_sha or None


def _provenance_from_rows(
    rows: dict[str, Any], live_head: Callable[[], str | None],
) -> dict[str, Any]:
    """Legacy receipt fields from metadata rows; *live_head* is read only if needed."""
    provenance: dict[str, Any] = {}
    updated_at = rows.get("last_updated")
    if isinstance(updated_at, str) and updated_at:
        provenance["updated_at"] = updated_at
        try:
            built_at = datetime.fromisoformat(updated_at)
            # Match aware timestamps with an aware ``now`` in the same
            # timezone; None preserves the stored naive/local format.
            now = datetime.now(tz=built_at.tzinfo)
            provenance["age_seconds"] = max(
                0, int((now - built_at).total_seconds()),
            )
        except (OverflowError, TypeError, ValueError):
            # A malformed timestamp only removes the derived age. The raw
            # timestamp and independently valid branch/SHA remain useful.
            pass

    head_sha = rows.get("git_head_sha")
    if isinstance(head_sha, str) and head_sha:
        provenance["built_at_sha"] = head_sha
    if provenance:
        branch = rows.get("git_branch")
        if isinstance(branch, str) and branch:
            provenance["built_on_branch"] = branch
        live_head_sha = live_head()
        if live_head_sha:
            provenance["head_sha"] = live_head_sha
            if isinstance(head_sha, str) and head_sha:
                provenance["head_matches_build"] = live_head_sha == head_sha
            else:
                # A graph in a git repo that never recorded the commit it
                # was built at cannot be compared to HEAD at all. Saying
                # nothing here reads downstream as "current", which is the
                # one answer we know to be unsupported.
                provenance["missing_build_anchor"] = True
    return provenance


def graph_provenance(repo_root: str | None = None) -> dict[str, Any] | None:
    """Return best-effort build metadata for one repository's graph.

    The metadata read is deliberately read-only. Missing, incomplete, or
    unreadable graph databases must never make the enclosing tool call fail.
    """
    try:
        root = _resolve_root(repo_root)
        db_path = get_db_path(root, read_only=True)
        if not db_path.exists():
            return None

        # ``as_uri`` escapes URI-significant path characters before the
        # read-only mode query is appended. It also handles Windows drives.
        database_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        # Provenance is optional and reads only three local metadata rows.
        # Allow a brief commit boundary, but never inherit sqlite3's 5-second
        # default wait when a build or migration holds an exclusive lock.
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=_PROVENANCE_READ_TIMEOUT_SECONDS,
        )
        try:
            rows = dict(connection.execute(
                "SELECT key, value FROM metadata WHERE key IN "
                "('last_updated', 'git_branch', 'git_head_sha')"
            ).fetchall())
        finally:
            connection.close()

        return _provenance_from_rows(rows, lambda: _read_live_git_head(root)) or None
    except Exception:
        return None


def _runtime_matches_source() -> bool:
    """False once the installed package on disk differs from the loaded one.

    A long-lived MCP server keeps running old code after an upgrade; the
    version literal in ``__init__.py`` is the cheapest honest witness.
    """
    import re

    from .. import __version__

    init = Path(__file__).resolve().parents[1] / "__init__.py"
    try:
        match = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            init.read_text(encoding="utf-8"), re.MULTILINE,
        )
    except OSError:
        return False
    return bool(match) and match.group(1) == __version__


def _receipt_etag(parts: list[Any]) -> str:
    import hashlib
    import json as _json

    blob = _json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


_receipt_cache: dict[str, tuple[float, tuple[Any, ...], dict[str, Any]]] = {}
_receipt_cache_lock = threading.Lock()


def clear_receipt_cache() -> None:
    with _receipt_cache_lock:
        _receipt_cache.clear()


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _read_small(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _git_dirs(root: Path) -> tuple[Path, Path] | None:
    """``(git_dir, common_dir)``; a linked worktree's ``.git`` is a pointer file."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git, dot_git
    pointer = _read_small(dot_git)
    if not pointer or not pointer.startswith("gitdir:"):
        return None
    git_dir = Path(pointer[len("gitdir:"):].strip())
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    common = _read_small(git_dir / "commondir")
    common_dir = (git_dir / common) if common else git_dir
    return git_dir, common_dir


_INDEX_KEY_POS = 4


def _receipt_cache_key(root: Path, db_path: Path) -> tuple[Any, ...]:
    """Everything cheap whose change must drop a cached receipt at once.

    Graph writes land in the WAL before a checkpoint touches the database file,
    so both are stat'ed. Edits to untracked files are only seen after the TTL.
    """
    from ..locking import probe

    wal = _stat_key(Path(f"{db_path}-wal"))
    key: list[Any] = [
        # A reader's open creates an empty WAL; empty holds nothing the db lacks.
        str(root), _stat_key(db_path), wal if wal and wal[1] else None,
        probe(db_path).held,
    ]
    dirs = _git_dirs(root)
    if dirs is not None:
        git_dir, common_dir = dirs
        head = _read_small(git_dir / "HEAD")
        ref = None
        if head and head.startswith("ref:"):
            name = head[len("ref:"):].strip()
            ref = _read_small(git_dir / name) or _read_small(common_dir / name)
        key += [_stat_key(git_dir / "index"), head, ref,
                _stat_key(common_dir / "packed-refs")]
    return tuple(key)


def graph_receipt(repo_root: str | None = None) -> dict[str, Any] | None:
    """The contract ``_graph`` receipt: legacy provenance plus readiness.

    Never raises. A database newer than this build yields an error-shaped
    receipt (``status: error``, ``error_code: schema_too_new``). Receipts are
    reused for ``CRG_RECEIPT_TTL`` seconds (default 2, 0 disables) while the
    graph, the writer lock, the git index and HEAD are unchanged.
    """
    try:
        root = _resolve_root(repo_root)
        db_path = get_db_path(root, read_only=True)
    except Exception:
        return None
    ttl = env_float("CRG_RECEIPT_TTL", _RECEIPT_TTL_DEFAULT_SECONDS)
    if ttl <= 0:
        return _healed_or_receipt(root, db_path, _compute_graph_receipt(root, db_path))
    try:
        key = _receipt_cache_key(root, db_path)
    except OSError:
        logger.warning("Could not key the graph receipt cache for %s", root, exc_info=True)
        return _compute_graph_receipt(root, db_path)
    now = time.monotonic()
    with _receipt_cache_lock:
        hit = _receipt_cache.get(str(root))
        if hit is not None and hit[1] == key and now - hit[0] < ttl:
            return copy.deepcopy(hit[2])
    receipt = _compute_graph_receipt(root, db_path)
    if receipt is None:
        return None
    if _self_heal_eligible(receipt):
        # The pre-heal receipt is not cached: the healed one, or the honest
        # stale answer when the heal is debounced, locked out or fails.
        return _run_self_heal(root, db_path) or receipt
    try:
        after = _receipt_cache_key(root, db_path)
    except OSError:
        return receipt
    # git status refreshes the index stat cache itself; anything else moving
    # meanwhile means the receipt may already be old, so it is not kept.
    if _without_index(after) == _without_index(key):
        with _receipt_cache_lock:
            _receipt_cache[str(root)] = (now, after, copy.deepcopy(receipt))
    return receipt


def _without_index(key: tuple[Any, ...]) -> tuple[Any, ...]:
    return key[:_INDEX_KEY_POS] + key[_INDEX_KEY_POS + 1:]


def _healed_or_receipt(
    root: Path, db_path: Path, receipt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if _self_heal_eligible(receipt):
        return _run_self_heal(root, db_path) or receipt
    return receipt


def _compute_graph_receipt(root: Path, db_path: Path) -> dict[str, Any] | None:
    from ..contract import CONTRACT_VERSION
    from ..migrations import SchemaTooNewError
    from ..readiness import GIT_OK, generation_current, head_matches
    from ..readiness_facts import gather_report

    try:
        report = gather_report(root, db_path, git_timeout=_RECEIPT_GIT_TIMEOUT_SECONDS)
    except SchemaTooNewError as exc:
        return {
            "status": "error",
            "error_code": "schema_too_new",
            "message": str(exc),
            "contract_version": CONTRACT_VERSION,
        }
    except Exception:
        logger.warning("Could not compute graph readiness for %s", root, exc_info=True)
        return None

    facts = report.facts
    readiness = report.readiness
    if not facts.graph_exists:
        return {"contract_version": CONTRACT_VERSION, **readiness.to_dict()}
    git_ok = facts.git_state == GIT_OK
    receipt = _provenance_from_rows(
        report.metadata, lambda: facts.head_commit if git_ok else None,
    )
    receipt.update({
        "contract_version": CONTRACT_VERSION,
        **readiness.to_dict(),
        "schema_version": report.schema_version,
        "index_generation": facts.index_generation,
        "built_at_commit": facts.built_at_commit,
        "current_sha": facts.head_commit,
        "failed_files": facts.failed_files,
        "resolver_failures": facts.resolver_failures,
        "source_identity": {
            "source_matches_build": facts.source_matches is True,
            "index_matches_runtime": facts.schema_current and generation_current(facts),
            "runtime_matches_source": _runtime_matches_source(),
            "edited_indexed_count": len((report.drift or {}).get("mismatched", [])),
        },
    })
    if git_ok and facts.built_at_commit:
        receipt["head_matches_build"] = head_matches(facts)
    receipt["etag"] = _receipt_etag([
        report.metadata.get("last_updated"), facts.built_at_commit,
        facts.write_epoch_open, facts.write_epoch_closed, facts.index_generation,
        report.schema_version, facts.head_commit, readiness.status.value,
        facts.failed_files, facts.resolver_failures,
        receipt["source_identity"]["edited_indexed_count"],
    ])
    return receipt


def with_provenance(result: Any, repo_root: str | None = None) -> Any:
    """Attach a ``_graph`` receipt without changing existing fields."""
    if not isinstance(result, dict) or "_graph" in result:
        return result
    receipt = graph_receipt(repo_root)
    if receipt:
        result["_graph"] = receipt
    return result


# --- query-time self-heal -----------------------------------------------------
#
# Every event source that refreshes the graph (hooks, git hooks, schedulers)
# is lossy, so a drifted anchor reaches agents as stale_graph and sends them
# to grep. Before answering stale, run one bounded catch-up update: a missed
# checkout then costs one slower answer instead of a degraded lane.

# Per-root monotonic timestamp of the last heal attempt; parallel tool calls
# share it under one lock, so at most one update runs per debounce window.
_SELF_HEAL_DEBOUNCE_SECONDS = env_float("CRG_SELF_HEAL_DEBOUNCE", 60.0)
_self_heal_lock = threading.Lock()
_self_heal_last: dict[str, float] = {}


def _self_heal_eligible(receipt: dict[str, Any] | None) -> bool:
    """Only staleness an incremental update can fix, and only with an anchor.

    ``git_unavailable`` means git itself failed (an update would too), and a
    missing anchor would full-rebuild inside the budget, which stays the root
    controller's call. ``rebuild_required``/``missing_graph`` never heal.
    """
    if not receipt or receipt.get("status") != "stale_graph":
        return False
    reasons = set(receipt.get("reasons") or [])
    if not reasons & {"head_moved", "git_capture_failed"}:
        return False
    return bool(receipt.get("built_at_commit"))


def _run_self_heal(root: Path, db_path: Path) -> dict[str, Any] | None:
    """One bounded ``update`` for *root*; a fresh receipt, or None."""
    budget = env_float("CRG_SELF_HEAL_BUDGET", 40.0)
    if budget <= 0:
        return None
    now = time.monotonic()
    with _self_heal_lock:
        if now - _self_heal_last.get(str(root), float("-inf")) < _SELF_HEAL_DEBOUNCE_SECONDS:
            return None
        _self_heal_last[str(root)] = now
    logger.info("Self-heal: catching the graph up for %s (budget %.0fs)", root, budget)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "code_review_graph", "update", "--skip-flows",
             "--if-locked=skip", "--repo", str(root)],
            capture_output=True, timeout=budget, stdin=subprocess.DEVNULL, check=False,
        )
        logger.debug("Self-heal rc=%s for %s", completed.returncode, root)
    except subprocess.TimeoutExpired:
        logger.warning("Self-heal timed out after %.0fs for %s", budget, root)
    except OSError as error:
        logger.warning("Self-heal failed to start for %s: %s", root, error)
    with _receipt_cache_lock:
        _receipt_cache.pop(str(root), None)
    try:
        return _compute_graph_receipt(root, db_path)
    except Exception:
        return None

# Common JS/TS builtin method names filtered from callers_of results.
# "Who calls .map()?" returns hundreds of hits and is never useful.
# These are kept in the graph (callees_of still shows them) but excluded
# when doing reverse call tracing to reduce noise.
_BUILTIN_CALL_NAMES: set[str] = {
    "map", "filter", "reduce", "reduceRight", "forEach", "find", "findIndex",
    "some", "every", "includes", "indexOf", "lastIndexOf",
    "push", "pop", "shift", "unshift", "splice", "slice",
    "concat", "join", "flat", "flatMap", "sort", "reverse", "fill",
    "keys", "values", "entries", "from", "isArray", "of", "at",
    "trim", "trimStart", "trimEnd", "split", "replace", "replaceAll",
    "match", "matchAll", "search", "substring", "substr",
    "toLowerCase", "toUpperCase", "startsWith", "endsWith",
    "padStart", "padEnd", "repeat", "charAt", "charCodeAt",
    "assign", "freeze", "defineProperty", "getOwnPropertyNames",
    "hasOwnProperty", "create", "is", "fromEntries",
    "log", "warn", "error", "info", "debug", "trace", "dir", "table",
    "time", "timeEnd", "assert", "clear", "count",
    "then", "catch", "finally", "resolve", "reject", "all", "allSettled", "race", "any",
    "parse", "stringify",
    "floor", "ceil", "round", "random", "max", "min", "abs", "pow", "sqrt",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "createElement", "appendChild", "removeChild",
    "setAttribute", "getAttribute", "preventDefault", "stopPropagation",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "toString", "valueOf", "toJSON", "toISOString",
    "getTime", "getFullYear", "now",
    "isNaN", "parseInt", "parseFloat", "toFixed",
    "encodeURIComponent", "decodeURIComponent",
    "call", "apply", "bind", "next",
    "emit", "on", "off", "once",
    "pipe", "write", "read", "end", "close", "destroy",
    "send", "status", "json", "redirect",
    "set", "get", "delete", "has",
    "findUnique", "findFirst", "findMany", "createMany",
    "update", "updateMany", "deleteMany", "upsert",
    "aggregate", "groupBy", "transaction",
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
    "beforeAll", "afterAll", "mock", "spyOn",
    "require", "fetch",
}

# The names above are JS/TS runtime and library APIs. In other languages a
# ``get``/``update``/``save`` method is ordinary repository code.
_BUILTIN_CALL_LANGUAGES = frozenset({"javascript", "typescript", "tsx", "vue", "svelte"})


def is_builtin_call_name(name: str, defining_languages: set[str]) -> bool:
    """True when *name* is a JS builtin and no non-JS node defines it."""
    return name in _BUILTIN_CALL_NAMES and not (defining_languages - _BUILTIN_CALL_LANGUAGES)


def _validate_repo_root(path: "Path | str") -> Path:
    """Validate that a path is a plausible project root.

    Ensures the path is an existing directory that contains a ``.git``,
    ``.svn``, or ``.code-review-graph`` directory, preventing arbitrary
    file-system traversal via the ``repo_root`` parameter.
    """
    resolved = Path(path).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"repo_root is not an existing directory: {resolved}"
        )
    has_vcs = (
        (resolved / ".git").exists()
        or (resolved / ".svn").exists()
        or (resolved / ".code-review-graph").exists()
    )
    if not has_vcs:
        raise ValueError(
            f"repo_root does not look like a project root "
            f"(no .git, .svn, or .code-review-graph directory found): "
            f"{resolved}"
        )
    return resolved


def _resolve_root(repo_root: str | None = None) -> Path:
    """Resolve and validate the repository root without opening a store."""
    return _validate_repo_root(Path(repo_root)) if repo_root else find_project_root()


def _get_store(repo_root: str | None = None) -> tuple[GraphStore, Path]:
    """Resolve repo root and open the graph store.

    Callers own the returned store and must close it (try/finally or
    context manager) to avoid leaking SQLite file descriptors.
    """
    root = _resolve_root(repo_root)
    db_path = get_db_path(root)
    return GraphStore(db_path), root


def _resolve_graph_file_paths(
    store: GraphStore, root: Path, file_paths: list[str],
) -> list[str]:
    """Resolve user-facing file paths to the paths stored in the graph.

    Graphs may contain absolute paths, repo-relative paths, or cwd-relative
    paths depending on how they were built. Tool inputs are usually relative to
    repo root, so exact matching alone can miss existing graph nodes.
    """
    resolved: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen:
            resolved.append(path)
            seen.add(path)

    for file_path in file_paths:
        raw = file_path.replace("\\", "/")
        candidates = [raw]
        path = Path(file_path)
        if path.is_absolute():
            try:
                candidates.append(str(path.resolve().relative_to(root)).replace("\\", "/"))
            except ValueError:
                pass
        else:
            candidates.append(normalize_file_path(root / path))

        for candidate in candidates:
            if store.get_nodes_by_file(candidate):
                add(candidate)

        suffixes = []
        for candidate in candidates:
            normalized = candidate.replace("\\", "/")
            if normalized not in suffixes:
                suffixes.append(normalized)

        for suffix in suffixes:
            for matched_path in store.get_files_matching(suffix):
                add(matched_path)

    return resolved


# ---------------------------------------------------------------------------
# Result bounding (#849 follow-up)
# ---------------------------------------------------------------------------
#
# Every MCP tool response has to survive a client-side context window. #849
# found get_affected_flows returning 247k tokens inside a workflow documented
# as "5 tool calls, 800 tokens total"; PR #853 capped that one tool. These
# helpers give the remaining tools the same contract:
#
#   * ``total`` always reports the untruncated count,
#   * ``truncated`` marks that the list was cut,
#   * the summary line says how many of how many are shown.
#
# Each tool pairs a caller-facing default with a hard ceiling. The ceiling
# exists so a caller passing ``max_results=1_000_000`` still gets a response
# that fits the ~25k-token budget most MCP clients allow for one tool result.


def _validate_positive_int(value: int, name: str) -> int:
    """Validate a caller-supplied result bound.

    Mirrors the check ``query.py`` applies to ``max_results``: ``bool`` is
    rejected explicitly because ``True`` would otherwise silently mean 1.
    """
    if isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be an integer greater than or equal to 1")
    return value


def _bounded(
    items: "list[Any]", max_results: int, hard_cap: int,
) -> tuple[list[Any], int, bool]:
    """Cap *items* at ``min(max_results, hard_cap)``.

    Returns ``(visible, total, truncated)`` where ``total`` is the
    untruncated length, so callers can always report the real count.
    """
    total = len(items)
    limit = min(max_results, hard_cap)
    return list(items[:limit]), total, total > limit


def _shown_of(shown: int, total: int) -> str:
    """Return the ``", showing N of M"`` fragment used by capped summaries."""
    return f", showing {shown} of {total}" if shown < total else ""


def compact_response(
    summary: str,
    key_entities: list[str] | None = None,
    risk: str = "unknown",
    communities: list[str] | None = None,
    flows_affected: list[str] | None = None,
    next_tool_suggestions: list[str] | None = None,
    data: dict[str, Any] | None = None,
    detail_level: str = "minimal",
) -> dict[str, Any]:
    """Standard compact response format for token efficiency."""
    resp: dict[str, Any] = {
        "status": "ok",
        "summary": summary,
    }
    if key_entities:
        resp["key_entities"] = key_entities[:10]
    if risk != "unknown":
        resp["risk"] = risk
    if communities:
        resp["communities"] = communities[:5]
    if flows_affected:
        resp["flows_affected"] = flows_affected[:5]
    if next_tool_suggestions:
        resp["next_tool_suggestions"] = next_tool_suggestions[:3]
    if detail_level != "minimal" and data:
        resp["data"] = data
    return resp


# Bounds the pathological dirty set (an unignored build directory reaching the
# check through --untracked-files=all). Missing files are a set difference and
# stay exact past the cap; only hashing is capped.
_DRIFT_HASH_CAP = 500


def working_tree_drift(
    root: Path, store: "GraphStore", dirty: list[str] | None = None,
) -> dict[str, Any]:
    """Compare the working tree against what the graph actually indexed.

    Commit identity says nothing about uncommitted work: a file written and not
    yet committed has no node at all, so a graph that is "current" by commit
    answers "this symbol does not exist". This reads the dirty set instead and
    classifies it against ``nodes.file_hash``.

    Returns ``missing`` (on disk, never indexed), ``mismatched`` (indexed under
    different bytes), ``deleted`` (indexed, gone from disk), and a ``check``
    field: ``full``, ``partial`` (hash cap reached) or ``unavailable`` (git
    could not answer, or the graph predates ``indexed_dirty_paths`` so a
    restored file could hide).
    """
    return working_tree_drift_conn(root, store._conn, dirty)


def read_dirty_paths(root: Path, timeout: float | None = None) -> list[str]:
    """Staged, unstaged and untracked paths; raises when git cannot answer.

    ``incremental.get_staged_and_unstaged`` returns None on a git failure;
    freshness checks use this raising variant of the same reader.
    """
    from ..incremental import detect_vcs, get_staged_and_unstaged, read_git_dirty_paths

    vcs = detect_vcs(root)
    if vcs == "svn":
        dirty = get_staged_and_unstaged(root)
        if dirty is None:
            raise GitUnavailableError(f"svn status failed: {root}")
        return dirty
    if vcs != "git":
        raise GitUnavailableError(f"not a git working tree: {root}")
    return read_git_dirty_paths(root, timeout)


def working_tree_drift_conn(
    root: Path, conn: sqlite3.Connection, dirty: list[str] | None = None,
) -> dict[str, Any]:
    """:func:`working_tree_drift` against a raw (possibly read-only) connection."""
    import hashlib
    import json as _json

    from ..incremental import _is_binary, _load_ignore_patterns, _should_ignore
    from ..parser import CodeParser, normalize_file_path

    result: dict[str, Any] = {
        "missing": [], "mismatched": [], "deleted": [], "check": "full",
    }
    try:
        live = list(dirty) if dirty is not None else read_dirty_paths(root)
    except (OSError, subprocess.SubprocessError, GitUnavailableError):
        result["check"] = "unavailable"
        return result

    row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", ("indexed_dirty_paths",),
    ).fetchone()
    indexed_dirty = None if row is None else row[0]
    if indexed_dirty is None:
        # No build-time snapshot: a file restored to HEAD content after the
        # build is invisible here. Say so rather than hashing the whole tree.
        result["check"] = "unavailable"
        candidates = set(live)
    else:
        try:
            candidates = set(live) | set(_json.loads(indexed_dirty))
        except ValueError:
            result["check"] = "unavailable"
            candidates = set(live)

    if not candidates:
        return result

    patterns = _load_ignore_patterns(root)
    parser = CodeParser()
    on_disk: dict[str, Path] = {}
    gone: list[str] = []
    for relative in sorted(candidates):
        path = root / relative
        if _should_ignore(relative, patterns):
            continue
        if not path.is_file():
            gone.append(normalize_file_path(path))
            continue
        if _is_binary(path) or parser.detect_language(path) is None:
            continue
        on_disk[normalize_file_path(path)] = path

    lookup = sorted(set(on_disk) | set(gone))
    if not lookup:
        return result
    placeholders = ", ".join("?" * len(lookup))
    indexed = dict(conn.execute(
        f"SELECT file_path, file_hash FROM nodes WHERE kind = 'File' "
        f"AND file_path IN ({placeholders})",
        lookup,
    ).fetchall())

    result["deleted"] = sorted(p for p in gone if p in indexed)
    hashed = 0
    for absolute, path in sorted(on_disk.items()):
        stored = indexed.get(absolute)
        if stored is None:
            result["missing"].append(absolute)
            continue
        if hashed >= _DRIFT_HASH_CAP:
            result["check"] = "partial"
            continue
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        hashed += 1
        if stored and current != stored:
            result["mismatched"].append(absolute)
    return result


def sibling_graph_root(root: Path) -> Path | None:
    """The main checkout of a linked worktree, when it owns a graph.

    A short-lived worktree rarely justifies its own index (a PMS-sized build is
    ~5 minutes and ~1.7 GB), but the main checkout it was branched from usually
    has one already. That graph is authoritative for every file the branch did
    not touch, which is most of them.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    main_root = Path(completed.stdout.strip()).parent
    if main_root == root or not main_root.is_dir():
        return None
    from ..incremental import get_db_path

    return main_root if get_db_path(main_root, read_only=True).is_file() else None
