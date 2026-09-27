"""Incremental graph update logic.

Detects changed files via git diff, re-parses only changed + impacted files,
and updates the graph accordingly. Also supports CLI invocation for hooks.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NamedTuple, Optional

from .constants import env_float, env_int
from .graph import STORE_BATCH_FILES, GraphStore
from .locking import writer_lock
from .migrations import INDEX_GENERATION, get_index_generation
from .parser import CodeParser, normalize_file_path, probe_grammars, seed_parser_probes
from .resolvers import RESOLVERS, run_resolver

_MAX_PARSE_WORKERS = int(os.environ.get("CRG_PARSE_WORKERS", str(min(os.cpu_count() or 4, 8))))

# Set only while the in-process FastMCP server is using stdio transport.
# This is deliberately separate from ``sys.stdin.isatty()``: CI, cron, and
# redirected CLI builds also have non-TTY stdin, but do not share the MCP
# transport's file-descriptor lifetime problem.
_MCP_STDIO_ACTIVE = False

# Each process-pool worker runs this module in its own process, while each
# thread-pool worker needs isolated parser state.  A thread-local cache covers
# both cases and avoids rebuilding CodeParser (including its grammar probes and
# parser caches) for every file in a parallel build.
_PARSE_WORKER_STATE = threading.local()


def _select_executor_kind() -> str:
    """Return 'process' or 'thread' for parallel parsing.

    Defaults to ``process`` (the original behavior, fastest on Linux/macOS).
    Auto-switches to ``thread`` for an active MCP stdio server on every
    platform, where ``ProcessPoolExecutor`` workers can inherit the transport
    pipe/socket and prevent EOF shutdown. The older Windows non-TTY fallback
    remains for direct integrations that predate the explicit transport flag
    (issues #46, #136, PR #615).

    Override explicitly with ``CRG_PARSE_EXECUTOR={process,thread}``.

    Tree-sitter parsing in the worker releases the GIL during native
    parsing, so the speedup loss for falling back to threads is small
    (typically <30% on the full-build path) and the trade is worth it
    to avoid the deadlock + zombie process accumulation.
    """
    explicit = os.environ.get("CRG_PARSE_EXECUTOR", "").strip().lower()
    if explicit in ("process", "thread"):
        return explicit
    if _MCP_STDIO_ACTIVE:
        return "thread"
    if sys.platform == "win32" and not sys.stdin.isatty():
        return "thread"
    return "process"


def _make_executor(max_workers: int, probes: Optional[dict[str, bool]] = None):
    """Construct the parallel-parse executor selected by [_select_executor_kind].

    Process workers start with the parent's grammar *probes* instead of each
    spawning its own probe per grammar.
    """
    if _select_executor_kind() == "thread":
        return concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    return concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers, initializer=seed_parser_probes, initargs=(probes or {},),
    )

logger = logging.getLogger(__name__)

# Seconds a library writer (MCP build, watcher) waits for the writer lock.
# The CLI takes the lock itself first, per ``--if-locked``.
_WRITER_LOCK_WAIT = env_float("CRG_WRITER_LOCK_WAIT", 120.0)

# Write-epoch metadata read by readiness_facts.gather_report.
_EPOCH_OPEN_KEY = "write_epoch_open"
_EPOCH_CLOSED_KEY = "write_epoch_closed"
_FAILED_FILES_KEY = "failed_files"
_RESOLVER_FAILURES_KEY = "resolver_failures"
_INDEX_GENERATION_KEY = "index_generation"

FAULT_STAGES = ("parse", "store", "resolvers", "postprocess", "stamp")


def fault_point(stage: str) -> None:
    """Test hook: ``CRG_FAULT_AT=<stage>`` fails the write at that stage.

    ``CRG_FAULT_MODE=kill`` SIGKILLs the process instead of raising.
    """
    if os.environ.get("CRG_FAULT_AT") != stage:
        return
    if os.environ.get("CRG_FAULT_MODE") == "kill" and hasattr(signal, "SIGKILL"):
        os.kill(os.getpid(), signal.SIGKILL)
    raise RuntimeError(f"injected fault at stage {stage!r}")


@contextlib.contextmanager
def store_writer_lock(store: GraphStore, wait: Optional[float] = None):
    """Hold the writer lock for *store*'s database (re-entrant)."""
    if str(store.db_path) in ("", ":memory:"):
        yield None
        return
    with writer_lock(store.db_path, wait=_WRITER_LOCK_WAIT if wait is None else wait) as token:
        yield token


def _meta_int(store: GraphStore, key: str) -> int:
    try:
        return int(store.get_metadata(key) or 0)
    except ValueError:
        return 0


def write_epoch_is_open(store: GraphStore) -> bool:
    """True when a write opened an epoch that no stamp has closed."""
    return _meta_int(store, _EPOCH_OPEN_KEY) > _meta_int(store, _EPOCH_CLOSED_KEY)


def open_write_epoch(store: GraphStore) -> int:
    """Commit ``write_epoch_open = n + 1`` before any graph write; returns it."""
    epoch = max(_meta_int(store, _EPOCH_OPEN_KEY), _meta_int(store, _EPOCH_CLOSED_KEY)) + 1
    store.set_metadata(_EPOCH_OPEN_KEY, str(epoch))
    return epoch


def read_failed_files(store: GraphStore) -> list[str]:
    raw = store.get_metadata(_FAILED_FILES_KEY)
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def read_resolver_failures(store: GraphStore) -> dict[str, str]:
    raw = store.get_metadata(_RESOLVER_FAILURES_KEY)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


class _VcsState(NamedTuple):
    vcs: str
    branch: str
    revision: str
    # None: git could not list dirty paths.
    dirty: Optional[list[str]]


def _capture_vcs_state(repo_root: Path) -> _VcsState:
    """VCS anchor taken when a write starts, stamped when it ends."""
    vcs = detect_vcs(repo_root)
    if vcs == "git":
        branch, sha = _git_branch_info(repo_root)
        dirty = get_staged_and_unstaged(repo_root)
        return _VcsState(vcs, branch, sha, sorted(dirty) if dirty is not None else None)
    if vcs == "svn":
        branch, rev = _svn_revision_info(repo_root)
        return _VcsState(vcs, branch, rev, None)
    return _VcsState(vcs, "", "", None)


def stamp_write_epoch(
    store: GraphStore,
    epoch: int,
    *,
    started_at: str,
    build_type: str,
    vcs: _VcsState,
    ignore_policy: str,
    failed_files: list[str],
    resolver_failures: dict[str, str],
) -> None:
    """Close *epoch* in one transaction with everything readiness reads.

    Runs last, after parse, store, resolvers and post-processing.
    """
    with store.transaction():
        fault_point("stamp")
        store.set_metadata("last_updated", started_at)
        store.set_metadata("last_build_type", build_type)
        store.set_metadata(_IGNORE_POLICY_METADATA_KEY, ignore_policy)
        if vcs.vcs == "git":
            if vcs.branch:
                store.set_metadata("git_branch", vcs.branch)
            if vcs.revision:
                # The build anchor readiness compares with HEAD.
                store.set_metadata("git_head_sha", vcs.revision)
            if vcs.dirty is None:
                # Without a snapshot the drift check reports "unavailable".
                store.delete_metadata("indexed_dirty_paths")
            else:
                store.set_metadata(
                    "indexed_dirty_paths", json.dumps(vcs.dirty, separators=(",", ":")),
                )
        elif vcs.vcs == "svn":
            if vcs.branch:
                store.set_metadata("svn_branch", vcs.branch)
            if vcs.revision:
                store.set_metadata("svn_revision", vcs.revision)
        store.set_metadata(_FAILED_FILES_KEY, json.dumps(sorted(set(failed_files))))
        store.set_metadata(
            _RESOLVER_FAILURES_KEY, json.dumps(resolver_failures, sort_keys=True),
        )
        store.set_metadata(_INDEX_GENERATION_KEY, str(INDEX_GENERATION))
        store.set_metadata(_EPOCH_CLOSED_KEY, str(epoch))


def finish_write(store: GraphStore, result: dict[str, Any]) -> None:
    """Stamp the epoch a ``stamp=False`` build or update left open.

    Folds post-processing failures (``result["postprocess_failures"]``) into
    ``resolver_failures`` and sets ``status`` to ``partial`` on any failure.
    """
    pending = result.pop("_pending_stamp", None)
    if pending is None:
        return
    failures = dict(result.get("resolver_failures") or {})
    for key in [k for k in failures if k.startswith("postprocess.")]:
        if key in (result.get("postprocess_ran") or ()):
            failures.pop(key)
    failures.update(result.get("postprocess_failures") or {})
    result["resolver_failures"] = failures
    stamp_write_epoch(
        store,
        result["write_epoch"],
        failed_files=result.get("failed_files") or [],
        resolver_failures=failures,
        **pending,
    )
    store.checkpoint()
    result["status"] = "partial" if failures or result.get("failed_files") else "ok"


# -- Graph delta: what flows and communities must re-derive -----------------
#
# An incremental write journals every node and edge change through temporary
# triggers on its own connection, so resolver rewrites in unchanged files are
# seen too. The net change is merged into ``flows_stale`` metadata; the next
# full post-processing consumes it. {"full": true} asks for a full recompute.

_FLOWS_STALE_KEY = "flows_stale"
_DELTA_LISTS = ("nodes", "sources", "targets", "deleted_ids")
_MAX_DELTA_ENTRIES = env_int("CRG_MAX_DELTA_ENTRIES", 50_000, minimum=1)
# Columns flows, communities and embedding text read; line moves are not changes.
_NODE_COLUMNS = (
    "kind", "name", "qualified_name", "file_path", "language", "parent_name",
    "params", "return_type", "modifiers", "is_test", "extra",
)
_JOURNAL_TRIGGERS = {
    "crg_delta_node_insert": (
        "AFTER INSERT ON main.nodes BEGIN INSERT INTO crg_delta VALUES "
        "('n', NEW.kind, NEW.qualified_name, NULL, NEW.id, NULL); END"
    ),
    "crg_delta_node_delete": (
        "AFTER DELETE ON main.nodes BEGIN INSERT INTO crg_delta VALUES "
        "('d', OLD.kind, OLD.qualified_name, NULL, OLD.id, NULL); END"
    ),
    "crg_delta_node_update": (
        "AFTER UPDATE ON main.nodes WHEN "
        + " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in _NODE_COLUMNS)
        + " BEGIN INSERT INTO crg_delta VALUES "
        "('n', NEW.kind, NEW.qualified_name, NULL, NEW.id, NULL); "
        "INSERT INTO crg_delta VALUES ('n', OLD.kind, OLD.qualified_name, NULL, NULL, NULL); END"
    ),
    "crg_delta_edge_insert": (
        "AFTER INSERT ON main.edges BEGIN INSERT INTO crg_delta VALUES "
        "('+', NEW.kind, NEW.source_qualified, NEW.target_qualified, NULL, NEW.file_path); END"
    ),
    "crg_delta_edge_delete": (
        "AFTER DELETE ON main.edges BEGIN INSERT INTO crg_delta VALUES "
        "('-', OLD.kind, OLD.source_qualified, OLD.target_qualified, NULL, OLD.file_path); END"
    ),
    "crg_delta_edge_update": (
        "AFTER UPDATE ON main.edges WHEN OLD.kind IS NOT NEW.kind "
        "OR OLD.source_qualified IS NOT NEW.source_qualified "
        "OR OLD.target_qualified IS NOT NEW.target_qualified BEGIN "
        "INSERT INTO crg_delta VALUES "
        "('-', OLD.kind, OLD.source_qualified, OLD.target_qualified, NULL, OLD.file_path); "
        "INSERT INTO crg_delta VALUES "
        "('+', NEW.kind, NEW.source_qualified, NEW.target_qualified, NULL, NEW.file_path); END"
    ),
}


def _start_delta_journal(store: GraphStore) -> None:
    conn = store._conn
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS crg_delta "
        "(op TEXT, kind TEXT, a TEXT, b TEXT, node_id INTEGER, file_path TEXT)"
    )
    conn.execute("DELETE FROM temp.crg_delta")
    for name, body in _JOURNAL_TRIGGERS.items():
        conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {name} {body}")
    conn.commit()


def _stop_delta_journal(store: GraphStore) -> dict[str, Any]:
    """Drop the triggers and return the net change they recorded."""
    conn = store._conn
    for name in _JOURNAL_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS temp.{name}")
    nodes: set[str] = set()
    deleted_ids: set[int] = set()
    edge_net: dict[tuple[str, str, str], int] = {}
    for op, kind, a, b, node_id in conn.execute(
        "SELECT op, kind, a, b, node_id FROM temp.crg_delta"
    ):
        if op in ("n", "d"):
            nodes.add(a)
            if op == "d":
                deleted_ids.add(node_id)
        else:
            key = (kind, a, b)
            edge_net[key] = edge_net.get(key, 0) + (1 if op == "+" else -1)
    conn.execute("DELETE FROM temp.crg_delta")
    conn.commit()
    changed = [key for key, count in edge_net.items() if count]
    return {
        "nodes": sorted(nodes),
        "sources": sorted({s for k, s, _t in changed if k in ("CALLS", "TESTED_BY")}),
        "targets": sorted({t for k, _s, t in changed if k == "CALLS"}),
        "deleted_ids": sorted(deleted_ids),
        "inherits": any(k in ("INHERITS", "IMPLEMENTS") for k, _s, _t in changed),
        "structural": bool(nodes or changed),
    }


def _journal_mark(store: GraphStore) -> int:
    row = store._conn.execute("SELECT MAX(rowid) FROM temp.crg_delta").fetchone()
    return int(row[0] or 0)


def _files_that_lost_edges(store: GraphStore, since: int) -> set[str]:
    """Files whose edges were deleted after journal position *since*."""
    return {
        row[0] for row in store._conn.execute(
            "SELECT DISTINCT file_path FROM temp.crg_delta "
            "WHERE rowid > ? AND op = '-' AND file_path IS NOT NULL",
            (since,),
        )
    }


def read_flows_stale(store: GraphStore) -> Optional[dict[str, Any]]:
    """The graph change flows and communities have not absorbed yet, or None."""
    raw = store.get_metadata(_FLOWS_STALE_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return {"full": True}
    return value if isinstance(value, dict) else {"full": True}


def mark_flows_stale(store: GraphStore, delta: dict[str, Any]) -> None:
    """Merge *delta* into the pending change; too large a change becomes full."""
    pending = read_flows_stale(store)
    if delta.get("full") or (pending is not None and pending.get("full")):
        merged: dict[str, Any] = {"full": True}
    else:
        previous = pending or {}
        merged = {
            key: sorted(set(previous.get(key, ())) | set(delta.get(key, ())))
            for key in _DELTA_LISTS
        }
        for flag in ("inherits", "structural"):
            merged[flag] = bool(previous.get(flag) or delta.get(flag))
        if sum(len(merged[key]) for key in _DELTA_LISTS) > _MAX_DELTA_ENTRIES:
            merged = {"full": True}
    store.set_metadata(_FLOWS_STALE_KEY, json.dumps(merged, separators=(",", ":")))


def clear_flows_stale(store: GraphStore) -> None:
    store.delete_metadata(_FLOWS_STALE_KEY)


# Extension -> language tag, matching the RESOLVERS frozensets in
# code_review_graph/resolvers/__init__.py. Turns a set of changed file paths
# into the set of languages that changed, for incremental resolver gating.
_EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".res": "rescript",
    ".resi": "rescript",
    ".java": "java",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".php": "php",
    ".rs": "rust",
    ".cs": "csharp",
    ".jsp": "jsp",
    ".jspf": "jsp",
    ".tag": "jsp",
    # Frontend assets: the JSP resolver's edges also bind to these File nodes.
    ".js": "javascript",
    ".mjs": "javascript",
    ".jsx": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
}


def _changed_languages(paths) -> frozenset[str]:
    """Map changed file paths to the resolver-trigger language tags."""
    return frozenset(
        lang
        for path in paths
        for ext, lang in _EXTENSION_LANGUAGES.items()
        if path.endswith(ext)
    )


# Result-dict keys every incremental_update return path must carry, mapped
# from the RESOLVERS registry names (``spring_event`` predates the registry
# naming and reports as ``event_resolution``). None means the resolver did
# not run on that path; a stats dict (even all-zero) means it ran.
_RESOLVER_RESULT_KEYS: dict[str, str] = {
    "python": "python_resolution",
    "rescript": "rescript_resolution",
    "spring": "spring_resolution",
    "spring_event": "event_resolution",
    "temporal": "temporal_resolution",
    "jsp": "jsp_resolution",
    "hcl": "hcl_resolution",
    "scoped": "scoped_resolution",
}


def _resolver_results_section(
    results: dict[str, Optional[dict]],
) -> dict[str, Optional[dict]]:
    """Project registry-named resolver results onto the ``*_resolution`` keys."""
    return {key: results.get(name) for name, key in _RESOLVER_RESULT_KEYS.items()}


# Resolvers that must also re-run on a deletion-only change (a stale or
# missing path, not just a freshly changed one), because they maintain
# derived/virtual graph state (e.g. Spring Event nodes — issue #474; JSP
# link edges derived from live templates). Every other resolver only looks
# at newly changed files, as before this refactor.
_RECONCILE_ON_DELETE = frozenset({"python", "spring", "spring_event", "temporal", "jsp"})


# Default ignore patterns (in addition to .gitignore).
#
# ``**/<dir>/**`` patterns are safe-anywhere directory exclusions.  A leading
# slash anchors a pattern to the repository root, which prevents ambiguous
# output names such as ``build`` and ``dist`` from hiding nested source
# directories.  See: #91 and PR #92.
DEFAULT_IGNORE_PATTERNS = [
    "**/.code-review-graph/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/.svn/**",
    "**/__pycache__/**",
    "*.pyc",
    "**/.venv/**",
    "**/venv/**",
    "/dist/**",
    "/build/**",
    "/.next/**",
    "/.nuxt/**",
    "/target/**",
    "/bin/**",
    "/obj/**",
    # PHP / Laravel / Composer. Deliberately depth-matching: in a PHP monorepo a
    # Composer vendor/ dir legitimately sits under each package (issue #91).
    # Applied only to Composer projects -- `_load_ignore_patterns` drops this
    # pattern when the repository has no `composer.json`, because outside PHP a
    # directory named `vendor` is ordinary source (a Java `…core.vendor`
    # package, a `vendor/` of first-party integrations) and silently losing it
    # makes the graph answer "no such symbol" for code that exists. This
    # supersedes 6a517f8, which deleted the pattern outright: that also fixed
    # the Java case but left PHP repositories indexing their whole
    # dependency tree.
    "**/vendor/**",
    "/storage/**",
    "/bootstrap/cache/**",
    "/public/build/**",
    # Ruby / Bundler
    "**/.bundle/**",
    # Java / Kotlin / Gradle
    "**/.gradle/**",
    "*.jar",
    # Dart / Flutter
    "**/.dart_tool/**",
    "**/.pub-cache/**",
    # AWS CDK
    "**/cdk.out/**",
    # General
    "/coverage/**",
    "**/.cache/**",
    "/.tmp/**",
    "/tmp/**",  # nosec B108 -- repo-relative ignore glob, not a temp-file path
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "*.db",
    "*.sqlite",
    "*.db-journal",
    "*.db-wal",
]

# Build-output directories that ``DEFAULT_IGNORE_PATTERNS`` only anchors at the
# repository root.  A nested copy is ignored as well, but only when a sibling
# manifest proves the directory is that module's build output — ``moduleA/pom.xml``
# next to ``moduleA/target/``.  Without that evidence the nested directory keeps
# being parsed and watched, so the root anchoring from #91/#92 still protects
# everyone whose nested ``build/`` or ``dist/`` holds real sources.  See: #811.
NESTED_OUTPUT_DIR_MARKERS: dict[str, frozenset[str]] = {
    "target": frozenset({"pom.xml", "Cargo.toml", "build.sbt"}),
    "build": frozenset({
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
    }),
    ".next": frozenset({
        "next.config.js",
        "next.config.mjs",
        "next.config.cjs",
        "next.config.ts",
    }),
    ".nuxt": frozenset({"nuxt.config.js", "nuxt.config.mjs", "nuxt.config.ts"}),
}

# Bounds for the nested build-output scan.  The scan only lists directories
# (no file stats), stops at ``CRG_MODULE_SCAN_DEPTH`` levels, never descends
# into an already-ignored tree, and its result is cached per repository so
# incremental updates never pay for it twice inside the TTL.
_MODULE_SCAN_DEPTH = int(os.environ.get("CRG_MODULE_SCAN_DEPTH", "3"))
_MODULE_SCAN_MAX_DIRS = int(os.environ.get("CRG_MODULE_SCAN_MAX_DIRS", "2000"))
_MAX_NESTED_OUTPUT_PATTERNS = 200
_NESTED_IGNORE_TTL_SECONDS = float(os.environ.get("CRG_NESTED_IGNORE_TTL", "300"))

_nested_ignore_cache: dict[tuple[str, tuple[str, ...]], tuple[float, list[str]]] = {}
_nested_ignore_lock = threading.Lock()


def find_svn_root(start: Path | None = None) -> Optional[Path]:
    """Walk up from start to find the SVN working copy root.

    For SVN 1.7+, there is a single ``.svn`` at the WC root.
    For older SVN, every directory has ``.svn`` — we return the topmost one
    found so that the WC root is correctly identified.
    """
    current = start or Path.cwd()
    candidate: Optional[Path] = None
    while current != current.parent:
        if (current / ".svn").exists():
            candidate = current
        current = current.parent
    if (current / ".svn").exists():
        candidate = current
    return candidate


def find_repo_root(
    start: Path | None = None,
    stop_at: Path | None = None,
) -> Optional[Path]:
    """Walk up from ``start`` to find the nearest ``.git`` directory or SVN working copy root.

    Args:
        start: Starting directory.  Defaults to ``Path.cwd()``.
        stop_at: Optional boundary — if provided, the walk examines
            ``stop_at`` for a ``.git`` directory and then stops without
            crossing above it.  Useful for tests that create a synthetic
            repo under ``tmp_path`` (so the walk does not accidentally
            climb into a developer's home-directory dotfiles repo) and
            for any production caller that wants to bound the ancestor
            walk — e.g. multi-repo orchestrators, CI containers with
            bind-mounted volumes, embedded sandboxes.  See #241.

    Returns:
        The first ancestor containing ``.git`` or an SVN working copy,
        or ``None`` if no ancestor up to and including ``stop_at`` (when
        set) or the filesystem root (when ``stop_at is None``) contains one.
    """
    current = start or Path.cwd()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        if stop_at is not None and current == stop_at:
            return None
        current = current.parent
    if (current / ".git").exists():
        return current
    # No Git root found — try SVN
    return find_svn_root(start)


def detect_vcs(root: Path) -> str:
    """Return ``'git'``, ``'svn'``, or ``'none'`` based on VCS markers at *root*."""
    if (root / ".git").exists():
        return "git"
    if (root / ".svn").exists():
        return "svn"
    return "none"


def find_project_root(
    start: Path | None = None,
    stop_at: Path | None = None,
) -> Path:
    """Find the project root.

    Resolution order (highest precedence first):

    1. ``CRG_REPO_ROOT`` environment variable — explicit override for
       anyone scripting the CLI from outside the repo (CI jobs, daemons,
       multi-repo orchestrators). See: #155
    2. Git repository root via :func:`find_repo_root` from ``start``,
       honoring ``stop_at`` if provided.
    3. ``start`` itself (or cwd if no start given).

    ``stop_at`` is forwarded to :func:`find_repo_root` so callers that
    want to bound the ancestor walk (typically tests; see #241) can do so
    without having to call ``find_repo_root`` directly.
    """
    env_override = os.environ.get("CRG_REPO_ROOT", "").strip()
    if env_override:
        p = Path(env_override).expanduser().resolve()
        if p.exists():
            return p
    root = find_repo_root(start, stop_at=stop_at)
    if root:
        return root
    return start or Path.cwd()


def _write_data_dir_gitignore(data_dir: Path) -> None:
    """Write .gitignore file in data directory if it doesn't exist.

    The gitignore contains a single '*' to prevent accidental commits.
    """
    inner_gitignore = data_dir / ".gitignore"
    if not inner_gitignore.exists():
        try:
            # `encoding="utf-8"` is REQUIRED — the em-dash in the header is
            # U+2014 which falls outside cp1252.  On Windows, calling
            # write_text without an encoding silently uses the system default
            # codepage, producing a file that subsequently fails to decode as
            # UTF-8 (see issue #239).
            inner_gitignore.write_text(
                "# Auto-generated by code-review-graph — do not commit database files.\n"
                "# The graph.db contains absolute paths and code structure metadata.\n"
                "*\n",
                encoding="utf-8",
            )
        except OSError:
            # Data dir might be read-only (rare); that's OK, it's a best-effort guard.
            pass


def get_data_dir(repo_root: Path, *, create: bool = True) -> Path:
    """Return the directory where this project's graph data lives.

    Resolution priority:
    1. Registry entry for this repo (set via --data-dir)
    2. CRG_DATA_DIR environment variable (global override)
    3. Default: <repo>/.code-review-graph/

    By default, ``<repo_root>/.code-review-graph``. If the
    ``CRG_DATA_DIR`` environment variable is set, it is used verbatim
    instead — letting you keep graphs outside the working tree (useful
    for ephemeral workspaces, Docker volumes, or shared caches). See: #155

    By default the directory is created if it does not already exist; an
    inner ``.gitignore`` (with ``*``) is written so any accidentally-nested
    files never get committed. Both are idempotent. Pass ``create=False``
    when resolving the path for a read-only existence check.

    A registry entry pointing at a directory that no longer exists is
    ignored when the repository still has its own ``.code-review-graph``
    database, so a swept temp directory cannot silently strand a graph.
    """
    # Check registry first
    try:
        from .registry import Registry, default_registry_path

        # Registry construction creates its parent directory. A read-only
        # lookup must skip it entirely when no registry file exists.
        if create or default_registry_path().is_file():
            registry_data_dir = Registry().get_data_dir_for_repo(str(repo_root))
            if registry_data_dir:
                data_dir = Path(registry_data_dir).resolve()
                # A registry entry whose directory has vanished must not be
                # recreated empty while the repository still holds its own
                # graph: ``create=True`` would build a second, empty database
                # there and the real one would report ``missing_graph``
                # forever. Relocating to a fresh directory stays supported --
                # the guard only fires when a local graph already exists.
                local_graph = repo_root / ".code-review-graph" / "graph.db"
                if create and not data_dir.exists() and local_graph.is_file():
                    logger.warning(
                        "Registry maps %s to %s, which does not exist, while %s "
                        "holds a graph. Ignoring the registry entry; clear it with: "
                        "code-review-graph unregister %s",
                        repo_root,
                        data_dir,
                        local_graph,
                        repo_root,
                    )
                else:
                    if create:
                        data_dir.mkdir(parents=True, exist_ok=True)
                        _write_data_dir_gitignore(data_dir)
                    return data_dir
    except Exception as exc:
        # If registry lookup fails, log and fall through to other methods
        logger.debug("Registry lookup failed for %s: %s", repo_root, exc)

    # Check environment variable
    env_override = os.environ.get("CRG_DATA_DIR", "").strip()
    if env_override:
        data_dir = Path(env_override).expanduser().resolve()
    else:
        data_dir = repo_root / ".code-review-graph"

    if create:
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_data_dir_gitignore(data_dir)

    return data_dir


def get_db_path(repo_root: Path, *, read_only: bool = False) -> Path:
    """Determine the database path for a repository.

    Respects ``CRG_DATA_DIR`` (see :func:`get_data_dir`). Migrates a
    legacy top-level ``.code-review-graph.db`` file into the new
    directory when it exists (WAL/SHM side-files are discarded). Pass
    ``read_only=True`` to resolve the current path without creating a data
    directory, migrating a legacy database, or deleting side-files.
    """
    crg_dir = get_data_dir(repo_root, create=not read_only)
    new_db = crg_dir / "graph.db"

    if read_only:
        return new_db

    # Migrate legacy database if present (only meaningful when the
    # legacy file sits at the repo root — if CRG_DATA_DIR is set we
    # skip the migration because there's no relationship between the
    # legacy location and the new one).
    legacy_db = repo_root / ".code-review-graph.db"
    if legacy_db.exists() and not new_db.exists():
        legacy_db.rename(new_db)
    # Discard stale WAL/SHM side-files from the old location
    for suffix in ("-wal", "-shm", "-journal"):
        side = repo_root / f".code-review-graph.db{suffix}"
        if side.exists():
            side.unlink()

    return new_db


def ensure_repo_gitignore_excludes_crg(repo_root: Path) -> str:
    """Ensure repo-level .gitignore excludes ``.code-review-graph/``.

    Returns one of:
    - ``created``: .gitignore was created with the entry
    - ``updated``: entry was appended to existing .gitignore
    - ``already-present``: no changes were needed
    """
    gitignore_path = repo_root / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""

    for raw_line in existing.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == ".code-review-graph" or line.startswith(".code-review-graph/"):
            return "already-present"

    block = "# Added by code-review-graph\n.code-review-graph/\n"
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    gitignore_path.write_text(existing + prefix + block, encoding="utf-8")

    if existing:
        return "updated"
    return "created"


def _load_ignore_patterns(repo_root: Path) -> list[str]:
    """Load ignore patterns from .code-review-graphignore file.

    A line starting with ``!`` keeps a path out of the automatic nested
    build-output detection (see :data:`NESTED_OUTPUT_DIR_MARKERS`); it does not
    negate the explicit patterns, which keep their existing meaning.

    ``**/vendor/**`` is a Composer default and applies only when the repository
    has a ``composer.json``. Elsewhere a ``vendor`` directory is ordinary
    source, and there is no syntax for a repository to take a default back.
    """
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    if "**/vendor/**" in patterns and not (repo_root / "composer.json").is_file():
        patterns.remove("**/vendor/**")
    keep: list[str] = []
    ignore_file = repo_root / ".code-review-graphignore"
    if ignore_file.exists():
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if line.startswith("!"):
                    keep.append(line[1:].strip().strip("/"))
                    continue
                # Directory names without a slash match at any depth, as in
                # .gitignore. A leading slash remains an explicit root anchor.
                if line.endswith("/"):
                    prefix = line[:-1]
                    if prefix.startswith("/") or "/" in prefix:
                        line = f"{prefix}/**"
                    else:
                        line = f"**/{prefix}/**"
                elif line.endswith("/**") and not line.startswith(("/", "**/")):
                    prefix = line[:-3]
                    if "/" in prefix:
                        line = f"/{line}"
                    else:
                        line = f"**/{line}"
                if line:
                    patterns.append(line)
    patterns.extend(_nested_output_ignore_patterns(repo_root, patterns, tuple(keep)))
    return patterns


_IGNORE_POLICY_METADATA_KEY = "ignore_policy_fingerprint"


def ignore_policy_fingerprint(patterns: list[str]) -> str:
    """Fingerprint the effective ignore policy an index was built under.

    Takes the already-loaded list rather than a repository root, so a caller
    physically cannot record a policy other than the one it is using -- the
    seven independent ``_load_ignore_patterns`` call sites would otherwise have
    to agree by convention. An adapter that rebinds the loader is fingerprinted
    correctly for free.
    """
    return hashlib.sha256("\n".join(sorted(patterns)).encode()).hexdigest()[:16]


def _should_ignore(path: str, patterns: list[str]) -> bool:
    """Check if a path matches any ignore pattern.

    ``**/<dir>/**`` and unanchored single-directory patterns match at any
    depth. A leading slash anchors a pattern to the repository root.
    """
    normalized = path.replace("\\", "/").lstrip("/")
    parts = PurePosixPath(normalized).parts
    for pattern in patterns:
        anchored = pattern.startswith("/")
        candidate = pattern[1:] if anchored else pattern

        if candidate.startswith("**/") and candidate.endswith("/**"):
            segment = candidate[3:-3]
            if segment and segment in parts:
                return True
            continue

        if candidate.endswith("/**"):
            prefix = tuple(part for part in candidate[:-3].split("/") if part)
            if not prefix:
                continue
            if anchored or len(prefix) > 1:
                if parts[: len(prefix)] == prefix:
                    return True
            elif prefix[0] in parts:
                return True
            continue

        if fnmatch.fnmatch(normalized, candidate):
            return True
    return False


def _child_directories(directory: Path) -> list[tuple[str, bool]]:
    """List ``directory`` once, returning ``(name, is_dir)`` for its entries.

    Symlinked directories are reported as non-directories so no walk follows
    them out of the repository.  Returns an empty list for anything that
    cannot be listed — an unreadable directory is not worth a failed watch.
    """
    try:
        with os.scandir(directory) as entries:
            listing: list[tuple[str, bool]] = []
            for entry in entries:
                try:
                    listing.append((entry.name, entry.is_dir(follow_symlinks=False)))
                except OSError:  # pragma: no cover - vanished mid-scan
                    continue
    except OSError as exc:
        logger.debug("Cannot list %s: %s", directory, exc)
        return []
    listing.sort()
    return listing


def _scan_nested_output_dirs(
    repo_root: Path,
    base_patterns: list[str],
    max_depth: int = _MODULE_SCAN_DEPTH,
    max_dirs: int = _MODULE_SCAN_MAX_DIRS,
) -> list[str]:
    """Find nested build-output directories that a sibling manifest confirms.

    Returns root-anchored ignore patterns such as ``/moduleA/target/**``.  The
    walk lists directories only, skips trees the base patterns already ignore,
    and is bounded by *max_depth* and *max_dirs*.
    """
    patterns: list[str] = []
    queue: list[tuple[Path, int]] = [(repo_root, 0)]
    visited = 0
    while queue:
        directory, depth = queue.pop()
        visited += 1
        if visited > max_dirs:
            logger.debug("Nested output scan hit the %d directory cap", max_dirs)
            break
        listing = _child_directories(directory)
        subdirectories = {name for name, is_dir in listing if is_dir}
        file_names = {name for name, is_dir in listing if not is_dir}
        flagged: set[str] = set()
        if depth > 0:
            for output_dir, markers in NESTED_OUTPUT_DIR_MARKERS.items():
                if output_dir in subdirectories and file_names & markers:
                    relative = (directory / output_dir).relative_to(repo_root).as_posix()
                    patterns.append(f"/{relative}/**")
                    flagged.add(output_dir)
                    if len(patterns) >= _MAX_NESTED_OUTPUT_PATTERNS:
                        logger.debug("Nested output scan hit the pattern cap")
                        return patterns
        if depth >= max_depth:
            continue
        for name in subdirectories:
            if name in flagged:
                continue
            child = directory / name
            if _should_ignore(child.relative_to(repo_root).as_posix(), base_patterns):
                continue
            queue.append((child, depth + 1))
    return patterns


def _nested_output_ignore_patterns(
    repo_root: Path,
    base_patterns: list[str],
    keep: tuple[str, ...] = (),
) -> list[str]:
    """Cached wrapper around :func:`_scan_nested_output_dirs`.

    The result is reused for ``_NESTED_IGNORE_TTL_SECONDS`` so a long-running
    watch pays for the scan once, not on every incremental update, while a
    module added later is still picked up without a restart.  Set
    ``CRG_NESTED_OUTPUT_SCAN=0`` to turn the whole thing off, or list
    ``!some/path`` in ``.code-review-graphignore`` to spare one directory.

    Excluding a directory removes its files from the graph, so the result is
    logged at info level: an unexpected exclusion has to be discoverable from
    a normal build, not only by diffing file counts.
    """
    if os.environ.get("CRG_NESTED_OUTPUT_SCAN", "1").strip().lower() in ("0", "false", "no"):
        return []
    key = (str(repo_root), keep)
    now = time.monotonic()
    with _nested_ignore_lock:
        cached = _nested_ignore_cache.get(key)
        if cached is not None and now - cached[0] < _NESTED_IGNORE_TTL_SECONDS:
            return list(cached[1])
    patterns = _scan_nested_output_dirs(repo_root, base_patterns)
    if keep:
        spared = {entry.replace("\\", "/").strip("/") for entry in keep}
        patterns = [
            pattern for pattern in patterns if pattern[1:-3] not in spared
        ]
    if patterns:
        logger.info(
            "Excluding %d nested build-output director%s (a sibling manifest marks "
            "them as build output; keep one with '!<path>' in "
            ".code-review-graphignore): %s",
            len(patterns),
            "y" if len(patterns) == 1 else "ies",
            ", ".join(pattern[1:-3] for pattern in patterns[:10])
            + (" …" if len(patterns) > 10 else ""),
        )
    with _nested_ignore_lock:
        _nested_ignore_cache[key] = (time.monotonic(), patterns)
    return list(patterns)


def clear_nested_ignore_cache() -> None:
    """Drop the cached nested build-output patterns (used by tests)."""
    with _nested_ignore_lock:
        _nested_ignore_cache.clear()


def _is_binary(path: Path) -> bool:
    """Quick heuristic: check if file appears to be binary."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(8192)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True


_GIT_TIMEOUT = env_int("CRG_GIT_TIMEOUT", 30, minimum=1)  # seconds, configurable

# When True, `git ls-files --recurse-submodules` is used so that files
# inside git submodules are included in the graph.  Opt-in via env var;
# can also be overridden per-call through function parameters.
_RECURSE_SUBMODULES = os.environ.get("CRG_RECURSE_SUBMODULES", "").lower() in ("1", "true", "yes")


def _git_branch_info(repo_root: Path) -> tuple[str, str]:
    """Return (branch_name, head_sha) for the current repo state."""
    branch = ""
    sha = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, UnicodeDecodeError):
        pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, UnicodeDecodeError):
        pass
    return branch, sha


def _svn_revision_info(repo_root: Path) -> tuple[str, str]:
    """Return (branch_path, revision_str) for the current SVN working copy."""
    branch = ""
    rev = ""
    try:
        result = subprocess.run(
            ["svn", "info", "--non-interactive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("URL: "):
                    url = line[5:].strip()
                    # Extract trunk/branches/tags segment from SVN URL
                    for marker in ("/branches/", "/tags/", "/trunk"):
                        if marker in url:
                            idx = url.index(marker)
                            branch = url[idx:].lstrip("/")
                            break
                    if not branch and url:
                        branch = url.rstrip("/").split("/")[-1]
                elif line.startswith("Revision: "):
                    rev = line[10:].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return branch, rev


_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9_.~^/@{}\-]+$")
_SAFE_SVN_REV = re.compile(r"^r?\d+(:r?\d+|:HEAD|:BASE|:COMMITTED)?$", re.IGNORECASE)


def _decode_name_status_paths(output: bytes) -> list[str]:
    """Decode ``git diff --name-status -z`` output into a list of paths.

    Renames and copies (``R<score>``/``C<score>`` records) carry two paths —
    the old and the new one.  Both are emitted so the old path flows through
    the purge loop in :func:`incremental_update`; otherwise a rename leaves
    the old path's nodes and edges in the graph and the incremental result
    diverges from a full rebuild.
    """
    fields = [os.fsdecode(f) for f in output.split(b"\0") if f]
    paths: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(fields):
        status = fields[i]
        takes_two = status[:1] in ("R", "C")
        entry = fields[i + 1 : i + (3 if takes_two else 2)]
        i += 3 if takes_two else 2
        for path in entry:
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _commit_object_exists(repo_root: Path, ref: str) -> bool:
    """Return True if *ref* resolves to a commit object present in the repo.

    This is an object-existence check, not an ancestry check: a commit that is
    only reachable from a branch we have since switched away from is still a
    valid ``git diff`` base, so we must accept it. Any git failure (missing
    binary, timeout, unknown ref) is treated as "not usable".
    """
    if not ref or ref.startswith("-") or not _SAFE_GIT_REF.fullmatch(ref):
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve_incremental_base(repo_root: Path, store: "GraphStore") -> str | None:
    """Resolve the automatic diff base for a default incremental update.

    The graph records the commit it was last built at (``git_head_sha``). Using
    that as the diff base lets a single ``update`` reconcile every change since
    the graph was last in sync, instead of only the most recent commit, which
    is what a fixed ``HEAD~1`` base does. That fixed base silently misses work
    that arrived through a multi-commit pull, rebase, or branch switch.

    Returns:
        - the stored commit SHA when it is still a usable diff base;
        - ``"HEAD~1"`` for SVN or non-git working copies, whose change
          discovery ignores or reinterprets the base anyway;
        - ``None`` for a git repo with no usable anchor (a fresh or legacy
          database, or a stored commit lost to a history rewrite or shallow
          clone), signalling the caller to do a full rebuild rather than
          diff against a wrong base.
    """
    if detect_vcs(repo_root) != "git":
        return "HEAD~1"
    stored = store.get_metadata("git_head_sha")
    if stored and _commit_object_exists(repo_root, stored):
        return stored
    return None


def get_changed_files(repo_root: Path, base: str = "HEAD~1") -> list[str]:
    """Get list of changed files via git diff or svn status.

    For SVN working copies the *base* parameter is ignored; modified/added/
    deleted files are detected from ``svn status``.  Pass an SVN revision
    range (e.g. ``"r100:HEAD"``) as *base* to compare against a specific
    revision instead.
    """
    if detect_vcs(repo_root) == "svn":
        return _get_svn_changed_files(repo_root, base if _SAFE_SVN_REV.match(base) else None)
    # Git path
    if base.startswith("-") or not _SAFE_GIT_REF.fullmatch(base):
        logger.warning("Invalid git ref rejected: %s", base)
        return []
    try:
        # --name-status (not --name-only): renames/copies must report BOTH
        # paths, or the old path never reaches the purge loop (issue #684).
        result = subprocess.run(
            ["git", "diff", "--name-status", "-z", base, "--"],
            capture_output=True,
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            # Fallback: try diff against empty tree (initial commit)
            result = subprocess.run(
                ["git", "diff", "--name-status", "-z", "--cached"],
                capture_output=True,
                cwd=str(repo_root),
                timeout=_GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        if result.returncode != 0:
            logger.warning("git diff failed while discovering changed files")
            return []
        return _decode_name_status_paths(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

def _get_svn_changed_files(repo_root: Path, rev_range: str | None = None) -> list[str]:
    """Return changed files in an SVN working copy.

    When *rev_range* is given (e.g. ``"r100:HEAD"``), ``svn diff --summarize``
    is used to list files changed between those revisions.  Otherwise
    ``svn status`` reports working-copy modifications.
    """
    try:
        if rev_range:
            result = subprocess.run(
                ["svn", "diff", "--summarize", "--non-interactive", "-r", rev_range],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(repo_root), timeout=_GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                logger.warning("svn diff --summarize failed (rc=%d): %s",
                               result.returncode, result.stderr[:200])
                return []
            files = []
            for line in result.stdout.splitlines():
                # Format: "M       path/to/file"  (first char is status)
                if len(line) >= 2 and line[0] in ("M", "A", "D"):
                    files.append(line[1:].strip())
            return files
        else:
            result = subprocess.run(
                ["svn", "status", "--non-interactive"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(repo_root), timeout=_GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
            files = []
            for line in result.stdout.splitlines():
                if len(line) < 2:
                    continue
                status_char = line[0]
                # M=modified, A=added, D=deleted, R=replaced, C=conflicted
                if status_char in ("M", "A", "D", "R", "C"):
                    # SVN status: 8 fixed-width columns then the path
                    path = line[8:].strip() if len(line) > 8 else line[1:].strip()
                    files.append(path)
            return files
    except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return []

class GitUnavailableError(RuntimeError):
    """git could not answer; callers must not read this as a clean tree."""


def read_git_dirty_paths(repo_root: Path, timeout: Optional[float] = None) -> list[str]:
    """Staged, unstaged and untracked paths from ``git status``.

    Raises :class:`GitUnavailableError` when git fails, times out or is
    missing. With porcelain ``-z`` a rename/copy record lists its
    destination first and its source in the following record.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT if timeout is None else timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitUnavailableError(f"git status failed: {exc}") from exc
    if result.returncode != 0:
        raise GitUnavailableError(f"git status exited {result.returncode}")
    files: list[str] = []
    records = result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) > 3:
            files.append(os.fsdecode(record[3:]))
            if b"R" in record[:2] or b"C" in record[:2]:
                index += 1
        index += 1
    return files


def get_staged_and_unstaged(repo_root: Path) -> Optional[list[str]]:
    """Get all modified files (staged + unstaged + untracked).

    Returns None when git cannot answer (non-zero exit, timeout, missing
    binary): that is "unavailable", never a clean tree.
    """
    if detect_vcs(repo_root) == "svn":
        return _get_svn_changed_files(repo_root)
    try:
        return read_git_dirty_paths(repo_root)
    except GitUnavailableError as exc:
        logger.warning("git status unavailable for %s: %s", repo_root, exc)
        return None


def get_all_tracked_files(
    repo_root: Path,
    recurse_submodules: bool | None = None,
) -> list[str]:
    """Get all files tracked by git or svn.

    Args:
        repo_root: Repository root directory.
        recurse_submodules: If True, pass ``--recurse-submodules`` to
            ``git ls-files`` so that files inside git submodules are
            included.  When *None* (default), falls back to the
            ``CRG_RECURSE_SUBMODULES`` environment variable.
            (Ignored for SVN working copies.)
    """
    if detect_vcs(repo_root) == "svn":
        return _get_svn_all_tracked_files(repo_root)

    if recurse_submodules is None:
        recurse_submodules = _RECURSE_SUBMODULES

    cmd = ["git", "ls-files"]
    if recurse_submodules:
        cmd.append("--recurse-submodules")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return []

def _get_svn_all_tracked_files(repo_root: Path) -> list[str]:
    """Return SVN-versioned files by walking the working copy.

    Uses ``svn list -R`` to get the server-side file list, falling back to
    a filesystem walk (which is also the fallback in :func:`collect_all_files`).
    """
    try:
        result = subprocess.run(
            ["svn", "list", "--recursive", "--non-interactive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=60,  # svn list queries the server
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            # svn list returns paths relative to the WC URL; directories end with "/"
            files = [
                f.strip()
                for f in result.stdout.splitlines()
                if f.strip() and not f.strip().endswith("/")
            ]
            if files:
                return files
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: let collect_all_files do a filesystem walk
    return []


def collect_all_files(
    repo_root: Path,
    recurse_submodules: bool | None = None,
) -> list[str]:
    """Collect all parseable files in the repo, respecting ignore patterns.

    Args:
        repo_root: Repository root directory.
        recurse_submodules: If True, include files from git submodules.
            When *None*, falls back to ``CRG_RECURSE_SUBMODULES`` env var.
    """
    ignore_patterns = _load_ignore_patterns(repo_root)
    parser = CodeParser(repo_root)
    files = []

    # Prefer git ls-files for tracked files
    tracked = get_all_tracked_files(repo_root, recurse_submodules)
    if tracked:
        candidates = tracked
    else:
        # Fallback: walk directory
        candidates = [str(p.relative_to(repo_root)) for p in repo_root.rglob("*") if p.is_file()]

    for rel_path in candidates:
        if _should_ignore(rel_path, ignore_patterns):
            continue
        # Skip paths that would exceed OS filename limits (macOS: 255 bytes
        # per component, ~1024 total; Windows: 260 total).
        try:
            full_path = repo_root / rel_path
        except (OSError, ValueError):
            logger.debug("Skipping path that cannot be constructed: %s", rel_path)
            continue
        if len(str(full_path)) > 1000 or any(len(p.encode()) > 255 for p in full_path.parts):
            logger.debug("Skipping overlong path: %s", rel_path[:120])
            continue
        if not full_path.is_file():
            continue
        if full_path.is_symlink():
            continue
        if parser.detect_language(full_path) is None:
            continue
        if _is_binary(full_path):
            continue
        files.append(rel_path)

    return files


def _reconcile_stale_files(
    repo_root: Path,
    store: GraphStore,
    current_files: list[str] | None = None,
    stored: list[str] | None = None,
) -> list[str]:
    """Remove graph files absent from the current parseable repository inventory."""
    stored_files = set(store.get_all_files() if stored is None else stored)
    current_paths: set[str]
    if current_files is not None:
        current_paths = {
            normalize_file_path(repo_root / file_path) for file_path in current_files
        }
    else:
        ignore_patterns = _load_ignore_patterns(repo_root)
        parser = CodeParser(repo_root)
        current_paths = set()
        for stored_file in stored_files:
            path = Path(stored_file)
            try:
                relative = str(path.relative_to(repo_root))
            except ValueError:
                continue
            if (
                path.is_file()
                and not path.is_symlink()
                and not _should_ignore(relative, ignore_patterns)
                and parser.detect_language(path) is not None
                and not _is_binary(path)
            ):
                current_paths.add(stored_file)
    stale_files = sorted(stored_files - current_paths)
    if stale_files:
        store.remove_files_permanently(stale_files)
    return stale_files


# Clock tolerance for the content-drift sweep. ``last_updated`` is written by
# ``time.strftime`` with one-second resolution, so a file saved in the same
# instant an update recorded its own finish can stat as "newer" without any
# real change. The hash comparison decides drift; this window only keeps such
# same-instant files from being re-hashed on every later update.
_DRIFT_MTIME_TOLERANCE_SECONDS = float(
    os.environ.get("CRG_DRIFT_MTIME_TOLERANCE", "0.25")
)


def _detect_content_drift(
    repo_root: Path, store: GraphStore, stored: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Find indexed files whose on-disk content no longer matches the graph.

    Git cannot see this divergence: the watcher applies an edit while HEAD
    stands still, the edit is later reverted (``git checkout -- <file>``), and
    every diff against the stored base comes back empty even though the graph
    still describes the reverted content. The sweep is stat-first — only files
    whose mtime is newer than the graph's ``last_updated`` record are hashed —
    so a quiet repository pays one ``stat`` per indexed file and no hashing.

    Returns ``(drifted, vanished)``: repo-relative paths ready to enter the
    changed-file pipeline, and stored paths whose file no longer exists.
    """
    built_at_raw = store.get_metadata("last_updated")
    if not built_at_raw:
        return [], []
    try:
        # ``last_updated`` is written by time.strftime (naive local time);
        # .timestamp() reads it back in that same local-time frame.
        threshold = datetime.fromisoformat(built_at_raw).timestamp()
    except ValueError:
        return [], []
    threshold += _DRIFT_MTIME_TOLERANCE_SECONDS

    drifted: list[str] = []
    vanished: list[str] = []
    for stored_path in store.get_all_files() if stored is None else stored:
        path = Path(stored_path)
        try:
            if path.stat().st_mtime <= threshold:
                continue
            raw = path.read_bytes()
        except FileNotFoundError:
            try:
                path.relative_to(repo_root)
            except ValueError:
                continue
            vanished.append(stored_path)
            continue
        except OSError:
            continue
        stored_hash = store.get_file_hash(stored_path)
        if not stored_hash:
            continue
        if hashlib.sha256(raw).hexdigest() != stored_hash:
            try:
                drifted.append(str(path.relative_to(repo_root)))
            except ValueError:
                drifted.append(str(path))
    return sorted(drifted), sorted(vanished)


def _assert_graph_matches_root(repo_root: Path, store: GraphStore) -> None:
    """Refuse an incremental reconciliation anchored to a different root.

    Authoritative File nodes identify the root a graph was built with. If none
    of them are under the requested root, treating every row as stale is more
    likely to destroy a usable graph than to clean up orphan rows. Orphan-only
    databases have no File markers and retain the purge behavior from #861.
    """
    file_paths = store.get_file_marker_paths()
    if not file_paths:
        return
    prefix = normalize_file_path(repo_root)
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    if any(normalize_file_path(path).startswith(prefix) for path in file_paths):
        return
    sample = normalize_file_path(sorted(file_paths)[0])
    raise RuntimeError(
        f"the graph holds {len(file_paths)} file(s) such as {sample!r}, none of "
        f"them under {str(repo_root)!r}; it was built with a different "
        "repository root. Rebuild it, or retry with the root it was built "
        "with, instead of reconciling every file away."
    )


_MAX_DEPENDENT_HOPS = int(os.environ.get("CRG_DEPENDENT_HOPS", "2"))
_MAX_DEPENDENT_FILES = 500


def _single_hop_dependents(store: GraphStore, file_path: str) -> set[str]:
    """Find files that directly depend on *file_path* (single hop)."""
    dependents: set[str] = set()
    edges = store.get_edges_by_target(file_path)
    for e in edges:
        if e.kind == "IMPORTS_FROM":
            dependents.add(e.file_path)

    nodes = store.get_nodes_by_file(file_path)
    for node in nodes:
        for e in store.get_edges_by_target(node.qualified_name):
            if e.kind in ("CALLS", "IMPORTS_FROM", "INHERITS", "IMPLEMENTS"):
                dependents.add(e.file_path)

    dependents.discard(file_path)
    return dependents


class DependentList(list):
    """A ``list[str]`` with a ``.truncated`` flag.

    When :func:`find_dependents` hits ``_MAX_DEPENDENT_FILES`` it truncates
    the result and sets ``truncated = True`` so callers can distinguish a
    complete expansion from a capped one.  See issue #261.

    This is a transparent ``list`` subclass — existing callers that iterate,
    ``len()``, or slice continue to work unchanged; only callers that
    specifically check ``.truncated`` benefit from the signal.
    """

    truncated: bool

    def __init__(self, items: list, *, truncated: bool = False) -> None:
        super().__init__(items)
        self.truncated = truncated


def find_dependents(
    store: GraphStore,
    file_path: str,
    max_hops: int = _MAX_DEPENDENT_HOPS,
) -> DependentList:
    """Find files that import from or depend on the given file.

    Performs up to *max_hops* iterations of expansion (default 2).
    Stops early if the total exceeds 500 files.

    Returns a :class:`DependentList` — a regular ``list[str]`` that also
    carries a ``.truncated`` flag.  When ``truncated is True`` the
    returned list is capped at ``_MAX_DEPENDENT_FILES`` and the full
    set of dependents was not explored.  See issue #261.
    """
    all_dependents: set[str] = set()
    visited: set[str] = {file_path}
    frontier: set[str] = {file_path}
    for _hop in range(max_hops):
        next_frontier: set[str] = set()
        for fp in frontier:
            deps = _single_hop_dependents(store, fp)
            new_deps = deps - visited
            all_dependents.update(new_deps)
            next_frontier.update(new_deps)
        visited.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
        if len(all_dependents) > _MAX_DEPENDENT_FILES:
            logger.warning(
                "Dependent expansion capped at %d files for %s",
                len(all_dependents),
                file_path,
            )
            return DependentList(
                list(all_dependents)[:_MAX_DEPENDENT_FILES],
                truncated=True,
            )
    return DependentList(list(all_dependents))


def _parse_single_file(
    args: tuple[str, str],
) -> tuple[str, list, list, str | None, str]:
    """Parse one file in a process- or thread-pool worker.

    Returns ``(rel_path, nodes, edges, error_or_none, file_hash)``.
    Must be a module-level function so ``ProcessPoolExecutor`` can
    serialise it across processes.
    """
    rel_path, repo_root_str = args
    abs_path = Path(repo_root_str) / rel_path
    try:
        raw = abs_path.read_bytes()
        fhash = hashlib.sha256(raw).hexdigest()
        parser = getattr(_PARSE_WORKER_STATE, "parser", None)
        parser_repo_root = getattr(_PARSE_WORKER_STATE, "repo_root", None)
        if parser is None or parser_repo_root != repo_root_str:
            parser = CodeParser(Path(repo_root_str))
            _PARSE_WORKER_STATE.parser = parser
            _PARSE_WORKER_STATE.repo_root = repo_root_str
        nodes, edges = parser.parse_bytes(abs_path, raw)
        return (rel_path, nodes, edges, None, fhash)
    except FileNotFoundError:
        return (rel_path, [], [], _VANISHED, "")
    except Exception as e:
        return (rel_path, [], [], str(e), "")


def _parse_chunk(
    chunk: list[tuple[str, str]],
) -> list[tuple[str, list, list, str | None, str]]:
    return [_parse_single_file(args) for args in chunk]


# Error marker for a file deleted between discovery and parsing.
_VANISHED = "\0vanished"
_PARSE_CHUNK_FILES = 20


class _ParseOutcome(NamedTuple):
    parsed: int
    total_nodes: int
    total_edges: int
    errors: list[dict[str, str]]
    vanished: list[str]


def _parse_and_store(
    repo_root: Path,
    store: GraphStore,
    parser: CodeParser,
    rel_paths: list[str],
) -> _ParseOutcome:
    """Parse *rel_paths* and store them STORE_BATCH_FILES files per transaction.

    Parse failures become ``errors``; storage failures (including a database
    that stays locked) propagate, because they are not the file's fault.
    """
    batch: list[tuple[str, list, list, str]] = []
    errors: list[dict[str, str]] = []
    vanished: list[str] = []
    counts = [0, 0, 0]  # parsed files, nodes, edges
    stored_once = False

    def flush() -> None:
        nonlocal stored_once
        if not batch:
            return
        store.store_file_batch(batch)
        batch.clear()
        if not stored_once:
            stored_once = True
            fault_point("store")

    def accept(rel_path: str, nodes: list, edges: list, error: str | None, fhash: str) -> None:
        if error == _VANISHED:
            vanished.append(rel_path)
            return
        if error is not None:
            logger.warning("Error parsing %s: %s", rel_path, error)
            errors.append({"file": rel_path, "error": error})
            return
        batch.append((str(repo_root / rel_path), nodes, edges, fhash))
        counts[0] += 1
        counts[1] += len(nodes)
        counts[2] += len(edges)
        if counts[0] == 1:
            fault_point("parse")
        if len(batch) >= STORE_BATCH_FILES:
            flush()

    file_count = len(rel_paths)
    use_serial = os.environ.get("CRG_SERIAL_PARSE", "") == "1"
    if use_serial or file_count < 8:
        for i, rel_path in enumerate(rel_paths, 1):
            full_path = repo_root / rel_path
            try:
                source = full_path.read_bytes()
                fhash = hashlib.sha256(source).hexdigest()
                nodes, edges = parser.parse_bytes(full_path, source)
            except FileNotFoundError:
                accept(rel_path, [], [], _VANISHED, "")
                continue
            except Exception as e:
                accept(rel_path, [], [], str(e), "")
                continue
            accept(rel_path, nodes, edges, None, fhash)
            if i % 50 == 0 or i == file_count:
                logger.info("Progress: %d/%d files parsed", i, file_count)
    else:
        # Parallel parsing; storing stays in this thread (SQLite single-writer).
        # Executor kind auto-selected: process for normal CLI/automation;
        # thread for MCP stdio to avoid pipe-handle inheritance deadlocks and
        # orphan workers (issues #46, #136, PR #615). Override via
        # CRG_PARSE_EXECUTOR env. Chunks are stored in completion order.
        root = str(repo_root)
        chunks = [
            [(rel_path, root) for rel_path in rel_paths[i:i + _PARSE_CHUNK_FILES]]
            for i in range(0, file_count, _PARSE_CHUNK_FILES)
        ]
        done = 0
        probes = probe_grammars(sorted(parser.grammars_for(repo_root / rel for rel in rel_paths)))
        with _make_executor(_MAX_PARSE_WORKERS, probes) as executor:
            futures = [executor.submit(_parse_chunk, chunk) for chunk in chunks]
            try:
                for future in concurrent.futures.as_completed(futures):
                    for rel_path, nodes, edges, error, fhash in future.result():
                        accept(rel_path, nodes, edges, error, fhash)
                    done += _PARSE_CHUNK_FILES
                    if done % 200 == 0 or done >= file_count:
                        logger.info(
                            "Progress: %d/%d files parsed", min(done, file_count), file_count,
                        )
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    flush()
    return _ParseOutcome(counts[0], counts[1], counts[2], errors, vanished)


def _run_resolvers(
    store: GraphStore,
    repo_root: Path,
    names: list[str],
    failures: dict[str, str],
) -> dict[str, Optional[dict]]:
    """Run *names* in registry order; each one's failure replaces its old entry."""
    results: dict[str, Optional[dict]] = dict.fromkeys(RESOLVERS)
    for index, name in enumerate(names):
        failures.pop(name, None)
        results[name] = run_resolver(name, store, repo_root, failures)
        if index == 0:
            fault_point("resolvers")
    return results


def _canonical_repo_root(repo_root: Path) -> Path:
    """Return one stable identity for a repository root.

    Graph paths are anchored to the root supplied by the caller. Resolving the
    root once prevents equivalent spellings (notably ``.`` and an absolute
    path) from looking like two different repositories during reconciliation.
    """
    return Path(repo_root).expanduser().resolve()


def _begin_write(
    repo_root: Path, store: GraphStore, build_type: str, ignore_patterns: list[str],
) -> tuple[int, dict[str, Any]]:
    """Open a write epoch; returns it with the stamp arguments captured now."""
    pending = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "build_type": build_type,
        "vcs": _capture_vcs_state(repo_root),
        "ignore_policy": ignore_policy_fingerprint(ignore_patterns),
    }
    return open_write_epoch(store), pending


def _end_write(
    store: GraphStore, result: dict[str, Any], pending: dict[str, Any], stamp: bool,
) -> dict[str, Any]:
    result["status"] = (
        "partial" if result["failed_files"] or result["resolver_failures"] else "ok"
    )
    result["_pending_stamp"] = pending
    if stamp:
        finish_write(store, result)
    return result


def full_build(
    repo_root: Path,
    store: GraphStore,
    recurse_submodules: bool | None = None,
    *,
    stamp: bool = True,
    lock_wait: Optional[float] = None,
) -> dict:
    """Full rebuild of the entire graph.

    Runs under the writer lock inside a write epoch. With ``stamp=False`` the
    epoch stays open for the caller to close with :func:`finish_write` after
    its post-processing.

    Args:
        repo_root: Repository root directory.
        store: Graph database store.
        recurse_submodules: If True, include files from git submodules.
            When *None*, falls back to ``CRG_RECURSE_SUBMODULES`` env var.
    """
    repo_root = _canonical_repo_root(repo_root)
    with store_writer_lock(store, lock_wait):
        ignore_patterns = _load_ignore_patterns(repo_root)
        epoch, pending = _begin_write(repo_root, store, "full", ignore_patterns)
        parser = CodeParser(repo_root)
        files = collect_all_files(repo_root, recurse_submodules)
        stale_files = _reconcile_stale_files(repo_root, store, files)
        if store.has_nodes():
            outcome = _parse_and_store(repo_root, store, parser, files)
        else:
            # Empty graph: one transaction with FTS triggers off, index rebuilt at the end.
            with store.bulk_load():
                outcome = _parse_and_store(repo_root, store, parser, files)
        if outcome.vanished:
            store.remove_files_permanently(
                [normalize_file_path(repo_root / rel) for rel in outcome.vanished]
            )

        failures: dict[str, str] = {}
        resolver_results = _run_resolvers(store, repo_root, list(RESOLVERS), failures)

        result = {
            "files_parsed": len(files),
            "stale_files_removed": len(stale_files),
            "total_nodes": outcome.total_nodes,
            "total_edges": outcome.total_edges,
            "errors": outcome.errors,
            "failed_files": sorted(error["file"] for error in outcome.errors),
            "resolver_failures": failures,
            "write_epoch": epoch,
            **_resolver_results_section(resolver_results),
        }
        mark_flows_stale(store, {"full": True})
        return _end_write(store, result, pending, stamp)


def _inheritance_dependents(store: GraphStore, file_path: str) -> set[str]:
    """Files whose classes extend or implement a class in *file_path*."""
    dependents: set[str] = set()
    for node in store.get_nodes_by_file(file_path):
        if node.kind not in ("Class", "Type"):
            continue
        for edge in store.iter_edges_by_target(node.qualified_name):
            if edge.kind in ("INHERITS", "IMPLEMENTS"):
                dependents.add(edge.file_path)
    dependents.discard(file_path)
    return dependents


def _noop_update_result(changed_files: list[str] | None) -> dict[str, Any]:
    return {
        "status": "ok",
        "files_updated": 0,
        "total_nodes": 0,
        "total_edges": 0,
        "changed_files": list(changed_files or []),
        "dependent_files": [],
        "stale_files_removed": 0,
        "content_drift_detected": 0,
        "errors": [],
        **_resolver_results_section(dict.fromkeys(RESOLVERS)),
    }


def incremental_update(
    repo_root: Path,
    store: GraphStore,
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
    reconcile_stale: bool = True,
    *,
    startup: bool = False,
    stamp: bool = True,
    lock_wait: Optional[float] = None,
) -> dict:
    """Incremental update: re-parse changed files and retry failed ones.

    Runs under the writer lock. Returns ``status``:

    - ``ok`` / ``partial`` (parse or resolver failures were recorded);
    - ``rebuild_required`` when the graph's index generation is not this
      build's: nothing is written and a full build is needed.

    ``startup=True`` (a watcher (re)start) also sweeps for stale files and
    content drift. A result without ``write_epoch`` wrote nothing.
    """
    repo_root = _canonical_repo_root(repo_root)
    with store_writer_lock(store, lock_wait):
        return _incremental_update_locked(
            repo_root, store, base, changed_files, reconcile_stale, startup, stamp,
        )


def _incremental_update_locked(
    repo_root: Path,
    store: GraphStore,
    base: str,
    changed_files: list[str] | None,
    reconcile_stale: bool,
    startup: bool,
    stamp: bool,
) -> dict:
    if reconcile_stale:
        _assert_graph_matches_root(repo_root, store)

    generation = get_index_generation(store._conn)
    if generation != INDEX_GENERATION and store.has_nodes():
        logger.warning(
            "Graph index generation %s differs from %s; a full rebuild is required",
            generation, INDEX_GENERATION,
        )
        return {
            **_noop_update_result(changed_files),
            "status": "rebuild_required",
            "rebuild_required": True,
            "reason": "index_generation_mismatch",
            "index_generation": generation,
            "expected_index_generation": INDEX_GENERATION,
        }

    _start_delta_journal(store)
    try:
        return _incremental_update_journaled(
            repo_root, store, base, changed_files, reconcile_stale, startup, stamp,
        )
    finally:
        _stop_delta_journal(store)


def _incremental_update_journaled(
    repo_root: Path,
    store: GraphStore,
    base: str,
    changed_files: list[str] | None,
    reconcile_stale: bool,
    startup: bool,
    stamp: bool,
) -> dict:
    journal_start = _journal_mark(store)
    parser = CodeParser(repo_root)
    ignore_patterns = _load_ignore_patterns(repo_root)
    # A previous write that never stamped left the graph in an unknown state.
    recovering = write_epoch_is_open(store)
    # An explicit empty change list asks for reconciliation only.
    startup = startup or recovering or (reconcile_stale and changed_files == [])
    previous_failed = read_failed_files(store)
    resolver_failures = read_resolver_failures(store)
    stored_cache: list[list[str]] = []

    def stored_files() -> list[str]:
        if not stored_cache:
            stored_cache.append(store.get_all_files())
        return stored_cache[0]

    # Determine changed files
    auto_discovery = changed_files is None
    changed: list[str] = (
        get_changed_files(repo_root, base) if changed_files is None else list(changed_files)
    )
    # A changed ignore policy is invisible to a diff: files it newly admits were
    # never in the graph and never appear in `git diff`, so without this they
    # stay missing until a full rebuild. Comparison happens before any inventory
    # work -- on the common path this costs one metadata read and one sha256.
    policy = ignore_policy_fingerprint(ignore_patterns)
    # A missing key means the index predates fingerprinting, which is exactly
    # the population that needs reconciling; treat it as a mismatch.
    # ``reconcile_stale=False`` is the watch path: it carries an explicit file
    # list and must never walk the repository (a batch that inventoried 6000
    # files on every save would make watching unusable). A policy change is
    # picked up by the next ordinary update instead.
    policy_changed = reconcile_stale and (
        store.get_metadata(_IGNORE_POLICY_METADATA_KEY) != policy
    )
    inventory = collect_all_files(repo_root) if policy_changed else None
    deletions_seen = any(not (repo_root / rel).exists() for rel in changed)
    # The full stale sweep stats and sniffs every indexed file, so it only
    # runs when something can have gone stale behind the diff's back.
    stale_files: list[str] = []
    if reconcile_stale and (policy_changed or startup or deletions_seen):
        stale_files = _reconcile_stale_files(repo_root, store, inventory, stored_files())
        if stale_files:
            stored_cache.clear()

    # An empty diff does not prove the graph matches disk: edits the watcher
    # applied and a later ``git checkout`` reverted leave HEAD unchanged while
    # the graph still describes the reverted content. Sweep mtimes (stat-first)
    # and let any drifted file enter the normal changed-file pipeline below.
    # Explicit changed_files lists (the watcher's own batches) skip the sweep —
    # they already know what changed.
    drifted_files: list[str] = []
    vanished: list[str] = []
    if auto_discovery and (not changed or startup):
        drifted_files, vanished = _detect_content_drift(repo_root, store, stored_files())
        vanished = [path for path in vanished if path not in stale_files]
    if drifted_files:
        changed = list(dict.fromkeys(changed + drifted_files))

    if inventory is not None:
        # Read after the reconcile above so removed rows are already gone.
        known = {normalize_file_path(path) for path in stored_files()}
        added = [
            rel
            for rel in inventory
            if normalize_file_path(repo_root / rel) not in known
        ]
        if added:
            logger.info(
                "Ignore policy changed: %d file(s) newly indexable", len(added)
            )
            changed = list(dict.fromkeys(changed + added))

    # Files whose last parse failed are retried until they parse.
    retried = [rel for rel in previous_failed if rel not in changed]
    changed = changed + retried
    retry_resolvers = [name for name in RESOLVERS if name in resolver_failures]

    if (
        not changed and not stale_files and not vanished
        and not recovering and not retry_resolvers
    ):
        if policy_changed:
            # Nothing was added and nothing removed, so the new policy really
            # did produce no work -- record it, or every later update pays for
            # the inventory again.
            store.set_metadata(_IGNORE_POLICY_METADATA_KEY, policy)
        return _noop_update_result([])

    epoch, pending = _begin_write(repo_root, store, "incremental", ignore_patterns)

    # Subclasses re-resolve against a changed parent; other dependents are
    # unchanged on disk and would only be hash-skipped.
    dependent_files: set[str] = set()
    for rel_path in changed:
        for dep in _inheritance_dependents(store, normalize_file_path(repo_root / rel_path)):
            try:
                dependent_files.add(str(Path(dep).relative_to(repo_root)))
            except ValueError:
                dependent_files.add(dep)
        if len(dependent_files) > _MAX_DEPENDENT_FILES:
            break

    # Combine changed + dependent
    all_files = set(changed) | dependent_files
    missing_paths: set[str] = set(vanished)

    # Separate deleted/unparseable files from files that need re-parsing
    to_parse: list[str] = []
    for rel_path in sorted(all_files):
        if _should_ignore(rel_path, ignore_patterns):
            continue
        abs_path = repo_root / rel_path
        if not abs_path.is_file():
            if normalize_file_path(abs_path) not in stale_files:
                missing_paths.add(normalize_file_path(abs_path))
            continue
        if parser.detect_language(abs_path) is None:
            continue
        # Quick hash check to skip unchanged files
        try:
            raw = abs_path.read_bytes()
            stored_hash = store.get_file_hash(str(abs_path))
            if stored_hash and stored_hash == hashlib.sha256(raw).hexdigest():
                continue
        except OSError:
            pass
        to_parse.append(rel_path)

    outcome = _parse_and_store(repo_root, store, parser, to_parse)
    missing_paths.update(normalize_file_path(repo_root / rel) for rel in outcome.vanished)

    removed_files = store.remove_files_permanently(sorted(missing_paths)) if missing_paths else 0

    # Removing a file drops other files' edges into it, while a fresh build
    # keeps them as unresolved references: parse those files again.
    settled = missing_paths | set(stale_files) | {
        normalize_file_path(repo_root / rel) for rel in to_parse
    }
    referrers: list[str] = []
    for lost in sorted(_files_that_lost_edges(store, journal_start) - settled):
        try:
            rel = str(Path(lost).relative_to(repo_root))
        except ValueError:
            continue
        if (repo_root / rel).is_file() and not _should_ignore(rel, ignore_patterns):
            referrers.append(rel)
    if referrers:
        again = _parse_and_store(repo_root, store, parser, referrers)
        outcome = _ParseOutcome(
            outcome.parsed + again.parsed,
            outcome.total_nodes + again.total_nodes,
            outcome.total_edges + again.total_edges,
            outcome.errors + again.errors,
            outcome.vanished + again.vanished,
        )
        dependent_files.update(referrers)
        all_files.update(referrers)
    files_updated = outcome.parsed + len(stale_files) + removed_files

    # Only re-run a resolver when a file in one of its declared languages
    # changed. python/spring/spring_event/temporal/jsp are in _RECONCILE_ON_DELETE
    # and also look at stale/missing paths, so a deletion that only surfaces
    # through reconciliation still clears derived state (e.g. virtual Spring
    # Event nodes — issue #474); every other resolver only looks at newly
    # changed files. Failed resolvers, and all of them after an unstamped
    # write, run again.
    reconciled_languages = _changed_languages(
        set(all_files) | set(stale_files) | missing_paths
    )
    changed_languages = _changed_languages(all_files)
    to_run = [
        name
        for name, (_resolver, _label, languages) in RESOLVERS.items()
        if recovering
        or name in retry_resolvers
        or languages & (
            reconciled_languages if name in _RECONCILE_ON_DELETE else changed_languages
        )
    ]
    resolver_results = _run_resolvers(store, repo_root, to_run, resolver_failures)

    result = {
        "files_updated": files_updated,
        "total_nodes": outcome.total_nodes,
        "total_edges": outcome.total_edges,
        "changed_files": list(changed),
        "dependent_files": sorted(dependent_files),
        "stale_files_removed": len(stale_files),
        "content_drift_detected": len(drifted_files),
        "retried_failed_files": len(retried),
        "errors": outcome.errors,
        "failed_files": sorted(error["file"] for error in outcome.errors),
        "resolver_failures": resolver_failures,
        "write_epoch": epoch,
        **_resolver_results_section(resolver_results),
    }
    # An unstamped earlier write may have changed anything.
    mark_flows_stale(store, {"full": True} if recovering else _stop_delta_journal(store))
    return _end_write(store, result, pending, stamp)


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------


_DEBOUNCE_SECONDS = 1
# A steady stream of events (a build writing sources) must not postpone the
# update forever: a batch is processed at most this long after its first event.
_DEBOUNCE_MAX_WAIT_SECONDS = env_float("CRG_WATCH_MAX_WAIT", 10.0, minimum=0.1)
_AUTO_WATCH_RESTART_MAX_DELAY = 60.0


def _raise_watch_update_errors(result: dict, context: str) -> None:
    """Fail the watch boundary when an incremental update reports errors."""
    if result.get("status") == "rebuild_required":
        raise RuntimeError(f"{context}: the graph needs a full rebuild")
    errors = result.get("errors") or []
    if not errors:
        return
    details = "; ".join(
        f"{error.get('file', 'unknown')}: {error.get('error', 'unknown error')}"
        for error in errors
    )
    raise RuntimeError(f"{context} reported errors: {details}")


def _raise_watch_postprocess_warnings(result: object) -> None:
    """Treat structured post-processing warnings as a failed watch update."""
    if not isinstance(result, dict):
        return
    warnings = result.get("warnings") or []
    if warnings:
        details = "; ".join(str(warning) for warning in warnings)
        raise RuntimeError(f"post-processing reported warnings: {details}")


# ---------------------------------------------------------------------------
# Watch scheduling and supervision
# ---------------------------------------------------------------------------

# A single recursive watch on the repository root makes the OS register one
# watch per directory in the tree — including every temp directory a build tool
# churns through inside ``target/`` or ``node_modules/``.  Planning the watches
# ourselves keeps ignored trees off the OS watch list entirely.  See: #811.
_WATCH_PLAN_DEPTH = int(os.environ.get("CRG_WATCH_PLAN_DEPTH", "3"))
_MAX_WATCH_SCHEDULES = int(os.environ.get("CRG_MAX_WATCH_SCHEDULES", "24"))
# Splitting a watch costs one watchdog emitter, so it has to buy more than it
# costs: an ignored tree is only worth excluding once it holds this many
# directories.  A lone ``__pycache__`` is not worth a thread; ``target/`` is.
_WATCH_SPLIT_MIN_DIRS = int(os.environ.get("CRG_WATCH_SPLIT_MIN_DIRS", "4"))
_WATCH_HEALTH_INTERVAL = float(os.environ.get("CRG_WATCH_HEALTH_INTERVAL", "10"))
_WATCH_STOP_TIMEOUT = 10.0
_WATCH_TICK_SECONDS = 1.0


def _watch_child_dirs(
    directory: Path,
    cache: dict[Path, list[Path]] | None = None,
) -> list[Path]:
    """Return the real (non-symlink) subdirectories of *directory*."""
    if cache is not None and directory in cache:
        return cache[directory]
    children = [directory / name for name, is_dir in _child_directories(directory) if is_dir]
    if cache is not None:
        cache[directory] = children
    return children


def _ignored_tree_weight(directory: Path, cap: int) -> int:
    """Count the directories inside an ignored tree, stopping at *cap*.

    Used to decide whether excluding the tree is worth a separate watch.  The
    count stops as soon as the cap is reached, so probing ``node_modules`` is
    barely more expensive than probing an empty ``__pycache__``.
    """
    if cap <= 0:
        return 0
    total = 1
    queue = [directory]
    while queue and total < cap:
        current = queue.pop()
        for name, is_dir in _child_directories(current):
            if not is_dir:
                continue
            total += 1
            if total >= cap:
                return total
            queue.append(current / name)
    return total


def _plan_watch_subtree(
    directory: Path,
    repo_root: Path,
    ignore_patterns: list[str],
    depth: int,
    max_depth: int,
    cache: dict[Path, list[Path]],
    split_threshold: int = _WATCH_SPLIT_MIN_DIRS,
) -> tuple[bool, list[tuple[Path, bool]]]:
    """Plan the watches covering *directory*.

    Returns ``(clean, plan)``.  ``clean`` means nothing below *directory*
    within *max_depth* is worth excluding, in which case a single recursive
    watch covers it.  Otherwise the directory is watched non-recursively and
    each surviving child is planned in turn, so the ignored subtree is never
    handed to the OS at all.
    """
    ignored_weight = 0
    kept: list[Path] = []
    for child in _watch_child_dirs(directory, cache):
        if _should_ignore(child.relative_to(repo_root).as_posix(), ignore_patterns):
            if ignored_weight < split_threshold:
                ignored_weight += _ignored_tree_weight(child, split_threshold - ignored_weight)
        else:
            kept.append(child)
    clean = ignored_weight < split_threshold
    if depth >= max_depth:
        if clean:
            return True, [(directory, True)]
        return False, [(directory, False)] + [(child, True) for child in kept]
    child_plans: list[tuple[Path, bool]] = []
    for child in kept:
        child_clean, child_plan = _plan_watch_subtree(
            child, repo_root, ignore_patterns, depth + 1, max_depth, cache, split_threshold
        )
        clean = clean and child_clean
        child_plans.extend(child_plan)
    if clean:
        return True, [(directory, True)]
    return False, [(directory, False)] + child_plans


def _plan_watch_paths(
    repo_root: Path,
    ignore_patterns: list[str],
    max_depth: int = _WATCH_PLAN_DEPTH,
    max_schedules: int = _MAX_WATCH_SCHEDULES,
    split_threshold: int = _WATCH_SPLIT_MIN_DIRS,
) -> list[tuple[Path, bool]]:
    """Return the ``(path, recursive)`` watches that cover the repo.

    Deeper plans exclude more ignored trees but cost one watchdog emitter each,
    so an over-budget plan is retried at a shallower depth before falling back
    to the single recursive root watch.
    """
    cache: dict[Path, list[Path]] = {}
    for depth in range(max(1, max_depth), 0, -1):
        _, plan = _plan_watch_subtree(
            repo_root, repo_root, ignore_patterns, 0, depth, cache, split_threshold
        )
        if len(plan) <= max(1, max_schedules):
            return plan
    logger.warning(
        "%s needs more than %d watches to skip its ignored trees; "
        "falling back to one recursive watch (raise CRG_MAX_WATCH_SCHEDULES to split it)",
        repo_root,
        max_schedules,
    )
    return [(repo_root, True)]


def _run_time_boxed(operation: Callable[[], Any], description: str, timeout: float = 10.0) -> None:
    """Run *operation* on a throwaway thread so a wedged watcher cannot hang exit.

    Watchdog's teardown joins its emitter threads, and the whole point of this
    module's health check is that one of those threads may be stuck.  Every
    watchdog thread is a daemon thread, so abandoning the join is safe.
    """

    def _call() -> None:
        try:
            operation()
        except Exception as exc:  # noqa: BLE001 - teardown must not mask the real error
            logger.debug("%s failed: %s", description, exc)

    thread = threading.Thread(target=_call, name="crg-watch-teardown", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        logger.warning(
            "%s did not finish in %.0fs; leaving it to process exit", description, timeout
        )


def _watch_health_path(repo_root: Path) -> Path | None:
    """Where this watcher publishes its health, or None if that is unavailable."""
    try:
        from .daemon import watch_health_path

        return watch_health_path(repo_root)
    except Exception as exc:  # noqa: BLE001 - health reporting is best-effort
        logger.debug("Watcher health reporting disabled: %s", exc)
        return None


def _watch_identity(path: str | Path) -> tuple[int, int, float] | None:
    """Identify a directory by inode, not by name.

    ``rm -rf src && mkdir src`` leaves the path spelled exactly as before while
    the watch on it is dead, so a name is not an identity.  ``st_birthtime``
    joins the tuple where the platform has it (macOS, Windows), which catches
    the recreated directory that happens to reuse an inode.
    """
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino, float(getattr(status, "st_birthtime", 0.0)))


class _WatchEntry(NamedTuple):
    """One scheduled watch: the watchdog handle and the directory it covers."""

    handle: Any
    identity: tuple[int, int, float] | None


class _WatchSupervisor:
    """Owns the observer's watches and reports whether they still work.

    Three jobs, all deliberately cheap:

    * schedule only the directories that survive the ignore patterns, so the OS
      never registers a watch inside an ignored tree;
    * adopt directories created later underneath a non-recursive watch, and
      notice when a watched directory has been replaced by a new one wearing
      the same name;
    * notice a dead watchdog thread and publish watcher health, so a stalled
      watcher stops looking healthy to ``crg-daemon status``.
    """

    def __init__(
        self,
        observer: Any,
        repo_root: Path,
        ignore_patterns: list[str],
        health_path: Path | None = None,
        max_schedules: int = _MAX_WATCH_SCHEDULES,
    ) -> None:
        self._observer = observer
        # One boundary resolves the path.  ``--repo .`` stays relative all the
        # way down from the CLI, and a relative root would make every
        # ``relative_to`` on an absolute event path raise.
        self._repo_root = Path(os.path.abspath(repo_root))
        self._ignore_patterns = ignore_patterns
        self._health_path = health_path
        self._max_schedules = max(1, max_schedules)
        self._handler: Any = None
        self._watches: dict[str, _WatchEntry] = {}
        self._shallow: set[str] = set()
        self._live_threads: dict[int, threading.Thread] = {}
        self._repaired_roots: set[str] = set()
        self._degraded = False
        self._last_health_write = 0.0
        self._last_health_state: tuple[bool, bool, tuple[str, ...]] | None = None
        self._started_at = time.time()
        self._token = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"

    # -- scheduling -----------------------------------------------------

    @property
    def watched_paths(self) -> list[str]:
        return sorted(self._watches)

    @property
    def degraded(self) -> bool:
        """True once the watch budget forced a coarser, recursive watch."""
        return self._degraded

    def attach(self, observer: Any) -> None:
        """Bind the observer, once the initial build has earned one."""
        self._observer = observer

    def schedule_initial(self, handler: Any) -> None:
        """Schedule the planned watches for *handler*."""
        self._handler = handler
        plan = _plan_watch_paths(
            self._repo_root,
            self._ignore_patterns,
            max_schedules=self._max_schedules,
        )
        for path, recursive in plan:
            self._schedule(path, recursive=recursive)
        logger.info(
            "Watching %d path(s) under %s; ignored trees are never registered",
            len(self._watches),
            self._repo_root,
        )

    def _schedule(self, path: Path, *, recursive: bool) -> None:
        key = str(path)
        if key in self._watches:
            return
        try:
            handle = self._observer.schedule(self._handler, key, recursive=recursive)
        except OSError as exc:
            logger.warning("Could not watch %s: %s", key, exc)
            return
        self._watches[key] = _WatchEntry(handle, _watch_identity(key))
        if recursive:
            self._shallow.discard(key)
        else:
            self._shallow.add(key)

    def sync_watches(self) -> tuple[list[str], list[str]]:
        """Reconcile the watches under every non-recursive watch.

        Returns ``(adopted, vanished)`` as absolute paths, so the caller can
        index a directory that appeared and reconcile one that disappeared.

        Directory events cannot be used for this.  macOS drops every directory
        event for a child of a non-recursive watch (``FSEventsEmitter.
        _is_recursive_event``), so a brand-new top-level directory — or one
        recreated by ``rm -rf src && mkdir src`` — would never be noticed and
        would stay unindexed forever.  One ``scandir`` per non-recursive watch
        per tick (typically one or two) is nothing next to the thousands of
        kernel watches this planning saves, and it cannot go blind.
        """
        adopted: list[str] = []
        vanished: list[str] = []
        for parent in sorted(self._shallow):
            present = {
                name for name, is_dir in _child_directories(Path(parent)) if is_dir
            }
            for child in sorted(self._children_of(parent)):
                if os.path.basename(child) not in present:
                    self._release_directory(child)
                    vanished.append(child)
                elif self._watches[child].identity != _watch_identity(child):
                    # Same name, different directory: the watch on it died with
                    # the old inode.  Release it now so the loop below adopts
                    # the replacement, instead of mistaking it for a corpse.
                    logger.info("Directory %s was replaced; re-watching it", child)
                    self._release_directory(child)
                    vanished.append(child)
            for name in sorted(present):
                if parent not in self._shallow:
                    # A promotion replaced this parent with one recursive
                    # watch, which already covers everything below it.
                    break
                candidate = os.path.join(parent, name)
                if candidate in self._watches:
                    continue
                if self._adopt_directory(candidate):
                    adopted.append(candidate)
        return adopted, vanished

    def _children_of(self, parent: str) -> list[str]:
        """Watched paths directly underneath *parent*."""
        return [path for path in self._watches if os.path.dirname(path) == parent]

    def _descendants_of(self, parent: str) -> list[str]:
        """Watched paths anywhere underneath *parent*, at any depth."""
        prefix = parent.rstrip(os.sep) + os.sep
        return [path for path in self._watches if path.startswith(prefix)]

    def _adopt_directory(self, candidate: str) -> bool:
        """Watch a directory that appeared under a non-recursive watch.

        Planned the same way startup plans the repository: a module arriving
        from a branch switch must not hand its ``node_modules`` and ``target``
        straight back to the OS, which is the exposure #811 is about.
        """
        try:
            relative = Path(candidate).relative_to(self._repo_root).as_posix()
        except ValueError:
            return False
        if _should_ignore(relative, self._ignore_patterns):
            logger.debug("Not watching ignored directory %s", relative)
            return False
        plan = self._affordable_plan(Path(candidate))
        if plan is None:
            # Promoting the parent — often the repository root — hands every
            # ignored tree under it back to the OS, which is the condition
            # #811 is about.  It is the last resort, never the first.
            self._promote_to_recursive(os.path.dirname(candidate))
            return True
        for path, recursive in plan:
            self._schedule(path, recursive=recursive)
        logger.info("Watching new directory %s (%d watch(es))", relative, len(plan))
        return True

    def _affordable_plan(self, directory: Path) -> list[tuple[Path, bool]] | None:
        """The most selective plan for *directory* that fits the budget.

        Mirrors :func:`_plan_watch_paths`: try the deepest split first, then
        shallower ones, then a single recursive watch on the directory itself.
        Only when even one slot is unavailable does the caller fall back to
        promoting the parent.
        """
        cache: dict[Path, list[Path]] = {}
        available = self._max_schedules - len(self._watches)
        if available <= 0:
            return None
        for depth in range(max(1, _WATCH_PLAN_DEPTH), 0, -1):
            _, plan = _plan_watch_subtree(
                directory, self._repo_root, self._ignore_patterns, 0, depth, cache
            )
            if len(plan) <= available:
                return plan
        # One recursive watch still filters every other directory in the repo.
        return [(directory, True)]

    def _promote_to_recursive(self, parent: str) -> None:
        """Trade filtering for coverage when the watch budget runs out."""
        for path in [parent, *self._descendants_of(parent)]:
            self._release_directory(path)
        self._schedule(Path(parent), recursive=True)
        self._degraded = True
        logger.warning(
            "Watch budget of %d reached; watching %s recursively instead — ignored "
            "trees under it are watched again. Raise CRG_MAX_WATCH_SCHEDULES to "
            "keep filtering.",
            self._max_schedules,
            parent,
        )

    def _release_directory(self, path: str) -> None:
        entry = self._watches.pop(path, None)
        self._shallow.discard(path)
        self._repaired_roots.discard(path)
        if entry is None:
            return
        # unschedule() joins the emitter thread with no timeout, and a wedged
        # emitter is the very thing this class exists to survive.
        _run_time_boxed(
            lambda: self._observer.unschedule(entry.handle),
            f"unschedule {path}",
            timeout=_WATCH_STOP_TIMEOUT,
        )

    # -- liveness -------------------------------------------------------

    def _watchdog_threads(self) -> list[tuple[threading.Thread, str | None]]:
        """Every thread the observer depends on, with the root it watches.

        The dispatcher, each emitter, and any reader thread an emitter owns
        (inotify keeps its buffer thread there) can die on their own; the
        process survives all three, which is what makes the failure silent.
        """
        threads: list[tuple[threading.Thread, str | None]] = []
        observer = self._observer
        if isinstance(observer, threading.Thread):
            threads.append((observer, None))
        try:
            emitters = list(getattr(observer, "emitters", ()) or ())
        except TypeError:  # a stub or mock observer — nothing to inspect
            return threads
        for emitter in emitters:
            root = getattr(getattr(emitter, "watch", None), "path", None)
            root = root if isinstance(root, str) else None
            if isinstance(emitter, threading.Thread):
                threads.append((emitter, root))
            try:
                members = list(vars(emitter).values())
            except TypeError:
                continue
            threads.extend(
                (member, root)
                for member in members
                if isinstance(member, threading.Thread) and member is not emitter
            )
        return threads

    def check_liveness(self) -> tuple[list[str], list[str]]:
        """Return ``(dead_thread_names, repaired_roots)``.

        A thread that stopped is only a death if the watch it belonged to is
        still the live watch for a directory that is still the same directory.
        Three things are deliberately not deaths:

        * a thread never seen alive — an emitter caught between construction
          and start is not a corpse;
        * a thread whose watch we have already released, or whose root is gone.
          Both backends stop an emitter when its own root disappears, so a
          plain ``rm -rf lib/`` would otherwise exit the watcher, and the
          daemon would restart it every 30s forever;
        * a thread whose root has been replaced since we scheduled it.
          ``rm -rf src && mkdir src`` inside one tick leaves the name in place
          but kills the watch, and calling that a death both exits the process
          and loses the recreated directory's contents.

        The remaining case — the watch is current, the directory is the same
        one, and its thread died anyway — is repaired once per root by
        rescheduling it, so an inode the filesystem handed straight back does
        not cost a restart.  A second death of the same root is reported, and
        the caller exits: that is #811's crash, and it must stay loud.
        """
        dead: list[str] = []
        repaired: list[str] = []
        still_present: dict[int, threading.Thread] = {}
        for thread, root in self._watchdog_threads():
            key = id(thread)
            if thread.is_alive():
                still_present[key] = thread
                continue
            if key not in self._live_threads:
                continue
            if root is None:
                dead.append(thread.name)
                continue
            entry = self._watches.get(root)
            if entry is None:
                logger.debug("Watch on %s was already released; not a death", root)
                continue
            if entry.identity != _watch_identity(root):
                logger.info(
                    "Watch root %s is gone or replaced; not a death — sync_watches "
                    "releases the stale watch and adopts the replacement",
                    root,
                )
                continue
            if root in self._repaired_roots:
                dead.append(thread.name)
                continue
            logger.warning(
                "Watch on %s stopped while the directory is still there; "
                "rescheduling it once before giving up",
                root,
            )
            recursive = root not in self._shallow
            self._release_directory(root)  # also clears the repair mark
            self._schedule(Path(root), recursive=recursive)
            if root not in self._watches:
                # Rescheduling failed — ENOSPC from inotify is the very trigger
                # behind #811 — so the directory is now unwatched.  That is a
                # loss of coverage, and the one thing it must not do is pass
                # quietly as a repair.
                logger.error("Could not reschedule the watch on %s", root)
                dead.append(thread.name)
                continue
            self._repaired_roots.add(root)
            repaired.append(root)
        self._live_threads = still_present
        return dead, repaired

    # -- health reporting ------------------------------------------------

    def report_health(
        self,
        *,
        observer_alive: bool,
        last_event_at: float | None = None,
        events_seen: int = 0,
        dead_threads: tuple[str, ...] = (),
        phase: str = "watching",
        force: bool = False,
    ) -> None:
        """Publish watcher health, rate-limited to one write per interval."""
        if self._health_path is None:
            return
        now = time.time()
        state = (observer_alive, self._degraded, tuple(dead_threads))
        if (
            not force
            and state == self._last_health_state
            and now - self._last_health_write < _WATCH_HEALTH_INTERVAL
        ):
            return
        payload = {
            "repo": str(self._repo_root),
            "pid": os.getpid(),
            "started_at": self._started_at,
            "updated_at": now,
            "observer_alive": observer_alive,
            "last_event_at": last_event_at,
            "events_seen": events_seen,
            "watched_paths": len(self._watches),
            "dead_threads": list(dead_threads),
            "degraded": self._degraded,
            "phase": phase,
        }
        try:
            self._health_path.parent.mkdir(parents=True, exist_ok=True)
            # Unique per writer, not per process: two supervisors in one
            # process would otherwise race on the same temp file and hand a
            # reader a torn document.
            temporary = self._health_path.with_name(f"{self._health_path.name}.{self._token}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._health_path)
        except OSError as exc:
            logger.debug("Could not write watcher health to %s: %s", self._health_path, exc)
            return
        self._last_health_write = now
        self._last_health_state = state

    def clear_health(self) -> None:
        """Remove the health file on a clean shutdown."""
        if self._health_path is None:
            return
        try:
            self._health_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def _make_debouncer(
    callback: Callable[[list[Any]], None],
    interval: float = _DEBOUNCE_SECONDS,
    max_wait: Optional[float] = None,
) -> Any:
    """An ``EventDebouncer`` whose quiet-period wait is capped at *max_wait*."""
    from watchdog.utils.event_debouncer import EventDebouncer

    cap = _DEBOUNCE_MAX_WAIT_SECONDS if max_wait is None else max_wait

    class CappedDebouncer(EventDebouncer):
        def run(self) -> None:
            with self._cond:
                while True:
                    while not self._events and self.should_keep_running():
                        self._cond.wait()
                    deadline = time.monotonic() + cap
                    while self.should_keep_running():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        if not self._cond.wait(timeout=min(interval, remaining)):
                            break
                    if not self.should_keep_running():
                        break
                    events = self._events
                    self._events = []
                    self.events_callback(events)

    return CappedDebouncer(interval, callback)  # type: ignore[arg-type]


def _create_watch_handler(
    repo_root: Path,
    store: GraphStore,
    on_files_updated: Optional[Callable],
):
    """Create the debounced watchdog handler for one repository."""
    from watchdog.events import FileSystemEvent, FileSystemEventHandler

    ignore_patterns = _load_ignore_patterns(repo_root)
    parser = CodeParser(repo_root)
    lexical_root = Path(os.path.abspath(repo_root))
    resolved_root = lexical_root.resolve()

    class WatchBatchProcessor:
        def __init__(self) -> None:
            self.failure: BaseException | None = None
            self.last_event_at: float | None = None
            self.events_seen: int = 0

        def _relative_path(self, path: str) -> str | None:
            candidate = Path(os.path.abspath(path))
            try:
                relative = candidate.relative_to(lexical_root)
            except ValueError:
                return None
            existing = candidate
            while not existing.exists() and existing != lexical_root:
                existing = existing.parent
            try:
                existing.resolve().relative_to(resolved_root)
            except ValueError:
                return None
            if any(
                component.is_symlink()
                for component in [
                    lexical_root / Path(*relative.parts[:index])
                    for index in range(1, len(relative.parts) + 1)
                ]
            ):
                return None
            if _should_ignore(str(relative), ignore_patterns):
                return None
            return str(relative)

        def _stored_descendants(self, relative_directory: str) -> set[str]:
            # Stored file paths use POSIX separators (#774).
            directory = normalize_file_path(repo_root / relative_directory) + "/"
            return {
                str(Path(file_path).relative_to(repo_root))
                for file_path in store.get_all_files()
                if file_path.startswith(directory)
            }

        def _parseable_file(self, relative_path: str) -> bool:
            absolute_path = repo_root / relative_path
            resolved_path = absolute_path.resolve()
            try:
                resolved_path.relative_to(resolved_root)
            except ValueError:
                return False
            return (
                absolute_path.is_file()
                and not absolute_path.is_symlink()
                and parser.detect_language(absolute_path) is not None
                and not _is_binary(absolute_path)
            )

        def _parseable_descendants(self, relative_directory: str) -> set[str]:
            directory = repo_root / relative_directory
            if not directory.is_dir() or directory.is_symlink():
                return set()
            return {
                str(path.relative_to(repo_root))
                for path in directory.rglob("*")
                if self._parseable_file(str(path.relative_to(repo_root)))
                and not _should_ignore(str(path.relative_to(repo_root)), ignore_patterns)
            }

        def _event_paths(self, event: FileSystemEvent) -> set[str]:
            paths: set[str] = set()
            source = self._relative_path(os.fsdecode(event.src_path))
            destination_path = getattr(event, "dest_path", "")
            destination = (
                self._relative_path(os.fsdecode(destination_path))
                if destination_path
                else None
            )
            if event.is_directory:
                if source is not None and event.event_type in {"deleted", "moved"}:
                    paths.update(self._stored_descendants(source))
                if destination is not None:
                    paths.update(self._parseable_descendants(destination))
                elif source is not None and event.event_type == "created":
                    paths.update(self._parseable_descendants(source))
            else:
                if source is not None and event.event_type in {"deleted", "moved"}:
                    paths.add(source)
                elif source is not None and self._parseable_file(source):
                    paths.add(source)
                if destination is not None and self._parseable_file(destination):
                    paths.add(destination)
            return paths

        def process(self, events: list[FileSystemEvent]) -> None:
            # Recorded before the work so a stalled watcher is distinguishable
            # from a watcher whose repository is simply quiet.
            self.last_event_at = time.time()
            self.events_seen += len(events)
            try:
                changed_files = sorted(
                    {path for event in events for path in self._event_paths(event)}
                )
                if not changed_files:
                    return
                result = incremental_update(
                    repo_root,
                    store,
                    changed_files=changed_files,
                    reconcile_stale=False,
                )
                _raise_watch_update_errors(result, "incremental update")
                if result["files_updated"] > 0 and on_files_updated is not None:
                    postprocess_result = on_files_updated(store)
                    _raise_watch_postprocess_warnings(postprocess_result)
            except BaseException as exc:
                self.failure = exc

        def raise_if_failed(self) -> None:
            if self.failure is not None:
                raise RuntimeError("watch update failed") from self.failure

    processor = WatchBatchProcessor()
    debouncer = _make_debouncer(processor.process)

    class GraphUpdateHandler(FileSystemEventHandler):
        def dispatch(self, event: FileSystemEvent) -> None:
            if event.event_type not in {"created", "modified", "deleted", "moved"}:
                return
            if event.is_directory and event.event_type == "modified":
                return
            debouncer.handle_event(event)

        def start(self) -> None:
            debouncer.start()

        def stop(self) -> None:
            debouncer.stop()
            debouncer.join()

        def process(self, events: list[FileSystemEvent]) -> None:
            processor.process(events)

        def raise_if_failed(self) -> None:
            processor.raise_if_failed()

        @property
        def last_event_at(self) -> float | None:
            return processor.last_event_at

        @property
        def events_seen(self) -> int:
            return processor.events_seen

    return GraphUpdateHandler()


def _sync_watch_tree(supervisor: _WatchSupervisor, handler: Any) -> None:
    """Reconcile watches, then bring the graph in line with what changed.

    A directory adopted this tick may already hold files, and one that
    vanished may still have nodes in the graph — on macOS neither produces a
    single event, so the sync has to do the bookkeeping itself.  The work goes
    through the debouncer, exactly as a real event would, so indexing a large
    new directory never blocks the loop that publishes the heartbeat.
    """
    from watchdog.events import DirCreatedEvent, DirDeletedEvent

    adopted, vanished = supervisor.sync_watches()
    for path in adopted:
        handler.dispatch(DirCreatedEvent(path))
    for path in vanished:
        handler.dispatch(DirDeletedEvent(path))


# Coarse filesystem timestamps can trail time.time() slightly.
_STARTUP_WINDOW_SLACK_SECONDS = 1.0


def _paths_changed_since(
    repo_root: Path, ignore_patterns: list[str], since: float,
) -> list[Path]:
    """Files modified after *since*, plus every file of a directory changed after it.

    A directory's mtime covers files moved in with their old timestamps.
    Ignored trees and symlinked directories are not walked.
    """
    threshold = since - _STARTUP_WINDOW_SLACK_SECONDS
    changed: list[Path] = []
    for directory, dirnames, filenames in os.walk(repo_root):
        base = Path(directory)
        relative = base.relative_to(repo_root)
        dirnames[:] = [
            name for name in dirnames
            if name != ".git"
            and not (base / name).is_symlink()
            and not _should_ignore((relative / name).as_posix(), ignore_patterns)
        ]
        try:
            fresh_directory = base.stat().st_mtime >= threshold
        except OSError:
            continue
        for name in filenames:
            path = base / name
            if _should_ignore((relative / name).as_posix(), ignore_patterns):
                continue
            try:
                if fresh_directory or path.lstat().st_mtime >= threshold:
                    changed.append(path)
            except OSError:
                continue
    return changed


def _install_sigterm_interrupt() -> Callable[[], None]:
    """Make SIGTERM unwind like Ctrl+C, and return an undo callable.

    ``crg-daemon stop`` terminates its children, so without this the watcher
    dies at 143 and leaves its health file behind, which then reads as a
    stalled watcher forever.  Only the main thread may install handlers.
    """
    def _raise_interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    except (ValueError, OSError, AttributeError):  # not the main thread, or no SIGTERM
        return lambda: None

    def _restore() -> None:
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError):  # pragma: no cover - shutdown race
            pass

    return _restore


def watch(
    repo_root: Path,
    store: GraphStore,
    on_files_updated: Optional[Callable] = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Watch for file changes and auto-update the graph.

    Uses a one-second debounce to batch rapid-fire saves into a single update.

    Ignored trees are never handed to the OS watcher, and every tick checks
    that watchdog's own threads are still running: a dead one raises, so the
    process exits non-zero and the daemon restarts it instead of the graph
    going quietly stale.  See: #811.

    Args:
        repo_root: Repository root to watch.
        store: Graph database to update.
        on_files_updated: Optional callback invoked after each debounced
            batch of file updates completes.  Receives the store as its
            only argument.  Used by the CLI to run post-processing
            (FTS, flows, communities) after watch updates.
        stop_event: Optional event that ends the loop cleanly, for callers
            that run ``watch`` on a thread they need to shut down.

    Raises:
        RuntimeError: if a watch update fails, or if the filesystem observer
            stops running.
    """
    from watchdog.events import DirCreatedEvent, DirDeletedEvent, FileModifiedEvent
    from watchdog.observers import Observer

    # One boundary, once: ``--repo .`` reaches here relative, and every path
    # comparison below — stored file paths, watch keys, event paths — assumes
    # they are all spelled the same way.
    repo_root = _canonical_repo_root(repo_root)
    ignore_patterns = _load_ignore_patterns(repo_root)
    supervisor = _WatchSupervisor(
        None,
        repo_root,
        ignore_patterns,
        health_path=_watch_health_path(repo_root),
    )
    # The first build of a large repository takes minutes.  Without a
    # heartbeat up front, ``crg-daemon status`` calls that healthy watcher
    # stalled for the whole build.
    supervisor.report_health(observer_alive=True, phase="initial-build", force=True)

    # Edits made while no watcher ran produce no events: reconcile against
    # the last stamped commit plus a drift and stale-file sweep.
    reconcile_started = time.time()
    initial = incremental_update(
        repo_root,
        store,
        base=resolve_incremental_base(repo_root, store) or "HEAD",
        startup=True,
    )
    _raise_watch_update_errors(initial, "initial watch reconciliation")
    if initial["files_updated"] > 0 and on_files_updated is not None:
        postprocess_result = on_files_updated(store)
        _raise_watch_postprocess_warnings(postprocess_result)
    observer = Observer()
    supervisor.attach(observer)
    handler = _create_watch_handler(repo_root, store, on_files_updated)
    supervisor.schedule_initial(handler)
    handler.start()
    observer.start()
    # What changed after the reconciliation looked and before the observer
    # listened produced no event; replay it through the debouncer.
    for path in _paths_changed_since(repo_root, ignore_patterns, reconcile_started):
        handler.dispatch(FileModifiedEvent(str(path)))
    supervisor.report_health(observer_alive=True, force=True)

    logger.info("Watching %s for changes... (Ctrl+C to stop)", repo_root)
    restore_sigterm = _install_sigterm_interrupt()
    try:
        import time as _time

        while True:
            if stop_event is not None:
                if stop_event.wait(_WATCH_TICK_SECONDS):
                    break
            else:
                _time.sleep(_WATCH_TICK_SECONDS)
            handler.raise_if_failed()
            _sync_watch_tree(supervisor, handler)
            dead, repaired = supervisor.check_liveness()
            for path in repaired:
                # A rescheduled watch missed whatever happened while it was
                # down, so re-read the directory rather than trust the gap.
                # Both halves are needed: the deletion contributes the stored
                # descendants, without which a file removed during the outage
                # keeps its rows, and the creation contributes what is on disk
                # now.  Watch batches run with reconcile_stale=False, so
                # nothing else would ever catch the stale side.
                handler.dispatch(DirDeletedEvent(path))
                handler.dispatch(DirCreatedEvent(path))
            if dead:
                names = ", ".join(dead)
                supervisor.report_health(
                    observer_alive=False,
                    last_event_at=handler.last_event_at,
                    events_seen=handler.events_seen,
                    dead_threads=tuple(dead),
                    force=True,
                )
                logger.error(
                    "Filesystem watcher thread(s) died (%s); %s would stop updating "
                    "silently, so this watcher is exiting for the daemon to restart it",
                    names,
                    repo_root,
                )
                raise RuntimeError(f"watch observer stopped: dead thread(s) {names}")
            supervisor.report_health(
                observer_alive=True,
                last_event_at=handler.last_event_at,
                events_seen=handler.events_seen,
            )
        supervisor.clear_health()
    except KeyboardInterrupt:
        supervisor.clear_health()
        _run_time_boxed(observer.stop, "observer stop")
    finally:
        restore_sigterm()
        _run_time_boxed(observer.stop, "observer stop")
        observer.join(timeout=_WATCH_STOP_TIMEOUT)
        handler.stop()
    logger.info("Watch stopped.")


def start_watch_thread(
    repo_root: Path,
    store: GraphStore,
    daemon: bool = True,
    stop_event: threading.Event | None = None,
) -> threading.Thread | None:
    """Start watch mode in a background thread that restarts after failures.

    Every restart begins with a catch-up reconciliation, so edits made while
    the watcher was down are indexed. Returns the started thread, or None if
    watchdog is unavailable.
    """
    try:
        import watchdog  # noqa: F401
    except ImportError:
        logger.warning("watchdog not installed; auto-watch disabled")
        return None

    stop = stop_event or threading.Event()

    def _run() -> None:
        # A thread cannot take the process down, so the one thing it must not
        # do is die quietly: the server would keep serving a frozen graph.
        delay = 1.0
        while not stop.is_set():
            started = time.monotonic()
            try:
                watch(repo_root, store, stop_event=stop)
                return
            except Exception as exc:  # noqa: BLE001 - restart on anything
                logger.error("Auto-watch for %s failed, restarting: %s", repo_root, exc)
            if time.monotonic() - started > _AUTO_WATCH_RESTART_MAX_DELAY:
                delay = 1.0
            if stop.wait(delay):
                return
            delay = min(delay * 2, _AUTO_WATCH_RESTART_MAX_DELAY)

    thread = threading.Thread(
        target=_run,
        daemon=daemon,
        name="crg-watch",
    )
    thread.start()
    logger.info("Auto-watch started for %s", repo_root)
    return thread
