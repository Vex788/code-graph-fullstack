"""MCP server entry point for Code Review Graph.

Run as: code-review-graph serve
Communicates via stdio (standard MCP transport), or use
``code-review-graph serve --http`` for Streamable HTTP on localhost (port 5555
by default). The HTTP transport validates ``Host`` and ``Origin`` so the loopback
endpoint cannot be driven cross-origin (e.g. via DNS rebinding); see
``code_review_graph.http_origin_guard``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

import anyio

# ToolResult lives at fastmcp.tools.tool on 3.x but is only re-exported from
# fastmcp.tools on 4.x; the package namespace works on both (pip installs 4.x
# on Windows CI while uv.lock pins 3.x).
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from mcp.types import TextContent
from typing_extensions import NotRequired, TypedDict  # pydantic rejects typing.TypedDict below 3.12

from . import incremental as _incremental
from .cli import _get_version
from .constants import env_float, env_int
from .graph import GraphStore
from .incremental import find_project_root, get_db_path, start_watch_thread
from .jobs import DEFAULT_WAIT_SECONDS, build_job_status, run_build_job
from .migrations import SchemaMigrationPending, SchemaTooNewError
from .prompts import (
    Message,
    architecture_map_prompt,
    debug_issue_prompt,
    onboard_developer_prompt,
    pre_merge_check_prompt,
    review_changes_prompt,
)
from .repo_settings import load_embedding_settings
from .tools import (
    apply_refactor_func,
    batch_query,
    coverage_report,
    cross_repo_search_func,
    detect_changes_func,
    embed_graph,
    find_large_functions,
    generate_wiki_func,
    get_affected_flows_func,
    get_architecture_overview_func,
    get_bridge_nodes_func,
    get_community_func,
    get_docs_section,
    get_flow,
    get_hub_nodes_func,
    get_impact_radius,
    get_knowledge_gaps_func,
    get_minimal_context,
    get_review_context,
    get_suggested_questions_func,
    get_surprising_connections_func,
    get_wiki_page_func,
    list_communities_func,
    list_flows,
    list_graph_stats,
    list_repos_func,
    query_graph,
    refactor_func,
    run_postprocess,
    semantic_search_nodes,
    traverse_graph_func,
    with_provenance,
)
from .tools._common import _resolve_root, building_response, schema_error_response
from .tools.navigation import common_callers_of, orient, shortest_path_between

logger = logging.getLogger(__name__)

# NOTE: Thread-safe for stdio MCP (single-threaded). If adding HTTP/SSE
# transport with concurrent requests, replace with contextvars.ContextVar.
_default_repo_root: str | None = None


def _resolve_repo_root(repo_root: Optional[str]) -> Optional[str]:
    """Resolve repo_root for a tool call.

    Order of precedence:
    1. Explicit ``repo_root`` passed by the MCP client (highest).
    2. ``--repo`` CLI flag passed to ``code-review-graph serve``
       (captured in ``_default_repo_root``).
    3. None — the underlying impl will fall back to the server's cwd.

    All MCP tools that accept ``repo_root`` should use this helper so
    ``serve --repo <X>`` applies consistently, including
    ``get_docs_section_tool``. See: #222.

    None, empty, whitespace and ``"/"`` count as unset: clients send them
    when they mean "the default repository".
    """
    if repo_root is None or repo_root.strip() in ("", "/"):
        return _default_repo_root
    return repo_root


mcp = FastMCP(
    "code-review-graph",
    # Same derivation the CLI prints for --version (dist-info first, then
    # __version__ fallback); without this FastMCP falls back to its own
    # library version in the MCP initialize handshake. See: df3ee7e.
    version=_get_version(),
    instructions=(
        "Persistent incremental knowledge graph for token-efficient, "
        "context-aware code reviews. Parses your codebase with Tree-sitter, "
        "builds a structural graph, and provides smart impact analysis."
    ),
)


def _schema_error_payload(exc: BaseException) -> Optional[dict]:
    """Contract shape for a schema exception anywhere in *exc*'s chain."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SchemaTooNewError):
            return schema_error_response(current)
        if isinstance(current, SchemaMigrationPending):
            return building_response()
        current = current.__cause__ or current.__context__
    return None


def _error_payload(code: str, message: str) -> dict:
    """The contract error shape; ``error`` repeats the message for old readers."""
    return {"status": "error", "error_code": code, "message": message, "error": message}


def _value_error_payload(exc: BaseException) -> Optional[dict]:
    """Error shape for a ValueError anywhere in *exc*'s chain (FastMCP wraps it)."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValueError):
            message = str(current)
            code = (
                "invalid_repo_root" if message.startswith("repo_root")
                else "invalid_argument"
            )
            return _error_payload(code, message)
        current = current.__cause__ or current.__context__
    return None


def _tool_result(payload: dict) -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
    )


class _SchemaStateMiddleware(Middleware):
    """Tools that open the graph answer ``building`` or ``schema_too_new``
    instead of failing when the schema is mid-migration or too new.

    It also keeps every tool error in the contract shape ``{status: error,
    error_code, message}``: a rejected argument (``ValueError``, including a
    bad ``repo_root``) becomes ``invalid_argument``/``invalid_repo_root``,
    and an error result without ``error_code`` gets ``tool_error``.
    """

    async def on_call_tool(self, context, call_next):
        try:
            result = await call_next(context)
        except Exception as exc:
            payload = _schema_error_payload(exc) or _value_error_payload(exc)
            if payload is None:
                raise
            return _tool_result(payload)
        payload = getattr(result, "structured_content", None)
        if (
            isinstance(payload, dict)
            and payload.get("status") == "error"
            and "error_code" not in payload
        ):
            message = str(payload.get("message") or payload.get("error")
                          or payload.get("summary") or "tool error")
            return _tool_result({**payload, **_error_payload("tool_error", message)})
        return result


mcp.add_middleware(_SchemaStateMiddleware())


#: Appended to the message a tool returns when ``CRG_TOOL_TIMEOUT`` cuts it
#: short. ``detect_changes_tool`` keeps its own, which names the two knobs
#: that actually bound change analysis.
#:
#: Deliberately hedged about argument names: this one message is shared by
#: every bounded tool, and several of them (``list_graph_stats_tool``,
#: ``get_architecture_overview_tool``) accept none of the three. Advice that
#: names a parameter the caller cannot pass is worse than no advice.
_TIMEOUT_HINT = (
    "Narrow the request with whichever of changed_files, max_depth or "
    "max_results this tool accepts, or increase CRG_TOOL_TIMEOUT."
)
_DETECT_CHANGES_TIMEOUT_HINT = (
    "Reduce scope with CRG_MAX_CHANGED_FUNCS / CRG_MAX_TRANSITIVE_FRONTIER, "
    "or increase CRG_TOOL_TIMEOUT."
)


async def _run_off_loop(work: Callable[[], dict]) -> dict:
    """Run *work* on a worker thread, on the same limiter FastMCP uses.

    ``anyio.to_thread.run_sync``, not ``asyncio.to_thread``, and the
    difference is not cosmetic. A plain ``def`` tool body is dispatched by
    FastMCP through anyio's default thread limiter (40 slots). Rewriting these
    tools as ``async def`` takes them off that limiter and onto
    ``asyncio.to_thread``'s default executor, which is capped at
    ``min(32, os.cpu_count() + 4)`` -- 8 slots on a 4-core Windows box. Making
    the server *more* likely to queue was the opposite of the point, so the
    offload stays on the limiter it came from.

    ``abandon_on_cancel=True`` keeps the semantics ``asyncio.wait_for`` needs:
    without it, cancelling the await blocks until the thread finishes anyway
    and the timeout below could never fire.
    """
    return await anyio.to_thread.run_sync(work, abandon_on_cancel=True)


async def _offload(
    tool_name: str,
    work: Callable[[], dict],
    root: str | None,
    *,
    provenance: bool = True,
    bounded: bool = True,
    timeout_hint: str = _TIMEOUT_HINT,
) -> dict:
    """Run one blocking tool body off the event loop, optionally bounded.

    Every MCP tool that reaches Git discovery, a BFS traversal, FTS, an
    embedding provider or any other multi-second work goes through here. Two
    things happen, and both matter for #262, where a tool call that is quick
    from the CLI comes back to an MCP client as error -32001:

    1. The work stays off the stdio event loop, so the server can still answer
       other requests while it runs. FastMCP's own ``run_in_thread`` default
       does this for sync tool functions today, but that is a library default
       inside a ``>=3.2.4,<5`` range, and it is not a promise this server's
       responsiveness should rest on. See #46, #136.
    2. ``CRG_TOOL_TIMEOUT``, when set above 0, bounds the call. A bounded call
       answers -- with ``status: error`` and a message naming the tool and the
       budget -- where an unbounded one just stops responding until the client
       gives up. That is the difference between a diagnosable result and
       -32001.

    Unset or 0 leaves every call unbounded, which is the historical behaviour
    and stays the default.

    Args:
        tool_name: The registered tool name, used in the timeout message.
        work: Zero-argument callable returning the tool's result dict. It runs
            on a worker thread.
        root: Resolved repository root, passed to ``with_provenance``.
        provenance: Whether to stamp the result with graph provenance. The
            stamp reads SQLite and spawns ``git rev-parse``, so it runs on the
            worker thread too, never on the event loop.
        bounded: Whether ``CRG_TOOL_TIMEOUT`` applies. **Default True, and the
            four long-running tools plus ``apply_refactor_tool`` pass False.**

            A timeout here cancels the *await*, never the thread: there is no
            way to interrupt a running parse, embed, or file rewrite from
            outside. For a read-only query that is harmless -- the abandoned
            worker computes a result nobody reads. For a tool that writes, it
            is not: ``build_or_update_graph_tool`` would report failure to the
            client while its worker kept writing ``graph.db``, and the natural
            retry would run a second concurrent update against the same
            database. ``apply_refactor_tool`` would report failure with the
            rename already applied to disk, and its retry would fail again
            with "not found or expired", leaving a modified tree and two
            errors.

            These tools were also unbounded before this helper existed, and
            ``CRG_TOOL_TIMEOUT`` is exactly what #262 tells a user to set to
            keep review calls responsive. Letting that knob quietly abort
            their builds would make the documented remedy a new bug.
        timeout_hint: Advice appended to the timeout message.
    """
    def _run() -> dict:
        result = work()
        return with_provenance(result, root) if provenance else result

    tool_timeout = env_int("CRG_TOOL_TIMEOUT", 0)
    if not bounded or tool_timeout <= 0:
        return await _run_off_loop(_run)

    try:
        return await asyncio.wait_for(_run_off_loop(_run), timeout=tool_timeout)
    except asyncio.TimeoutError:
        message = f"{tool_name} timed out after {tool_timeout}s. {timeout_hint}"
        error_response = {
            "status": "error",
            "error": message,
            "summary": message,
        }
        if not provenance:
            return error_response
        return await _run_off_loop(
            lambda: with_provenance(error_response, root)
        )


@mcp.tool()
async def build_or_update_graph_tool(
    full_rebuild: bool = False,
    repo_root: Optional[str] = None,
    base: Optional[str] = None,
    postprocess: str = "full",
    recurse_submodules: Optional[bool] = None,
    embedding_provider: Optional[str] = None,
    embedding_model: Optional[str] = None,
    status_only: bool = False,
    wait_seconds: Optional[float] = None,
) -> dict:
    """Build or incrementally update the code knowledge graph.

    Call this first to initialize the graph, or after making changes.
    By default performs an incremental update (only changed files).
    Set full_rebuild=True to re-parse every file.

    The build runs as a background job (a ``code-review-graph build|update``
    subprocess, one per repository), never inside the server. The call waits
    up to ``wait_seconds`` for it; a longer build answers ``status:
    building`` with a ``job_id``, and a repeat call joins the running job.
    The wait runs off the stdio event loop via ``_offload`` (unbounded: a
    cancelled await would report failure while the build job keeps writing),
    so the loop stays responsive (#46, #136).

    Args:
        full_rebuild: If True, re-parse all files. Default: False (incremental).
        repo_root: Repository root path. Auto-detected from current directory if omitted.
        base: Git ref to diff against for incremental updates. When omitted,
            resolves automatically to the commit the graph was last built at,
            so one update catches everything since the last sync (not just the
            latest commit). Pass an explicit ref to override.
        postprocess: Post-processing level: "full" (default), "minimal" (signatures+FTS only),
                     or "none" (skip all post-processing). Use "minimal" for faster builds.
        recurse_submodules: If True, include files from git submodules.
            When None (default), falls back to CRG_RECURSE_SUBMODULES env var.
        embedding_provider: Exact provider for an explicit post-build embedding
            refresh. Must be supplied with embedding_model. Default: disabled.
        embedding_model: Exact model for an explicit post-build embedding
            refresh. Must be supplied with embedding_provider. Default: disabled.
        status_only: If True, report the build job for this root (progress
            while running, the result or error once finished) without
            starting one. Default: False.
        wait_seconds: How long to wait for the job before answering
            ``building``. Default: CRG_BUILD_WAIT_SECONDS, else 25.
    """
    root = _resolve_repo_root(repo_root)

    def _run() -> dict:
        try:
            resolved = str(_resolve_root(root))
        except ValueError as exc:
            return {
                "status": "error", "error_code": "invalid_repo_root",
                "message": str(exc), "error": str(exc), "summary": str(exc),
            }
        if status_only:
            return with_provenance(build_job_status(resolved), resolved)
        wait = (
            wait_seconds if wait_seconds is not None
            else env_float("CRG_BUILD_WAIT_SECONDS", DEFAULT_WAIT_SECONDS)
        )
        try:
            result = run_build_job(
                resolved, full_rebuild=full_rebuild, base=base,
                postprocess=postprocess, recurse_submodules=recurse_submodules,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model, wait_seconds=wait,
            )
        except ValueError as exc:
            return {
                "status": "error", "error_code": "invalid_argument",
                "message": str(exc), "error": str(exc), "summary": str(exc),
            }
        return with_provenance(result, resolved)

    # bounded=False: a timeout cancels the await, never the build job, and
    # reporting failure while the job keeps writing graph.db invites a retry
    # that runs a second concurrent update against the same database.
    return await _offload(
        "build_or_update_graph_tool", _run, root,
        provenance=False, bounded=False,
    )


@mcp.tool()
async def coverage_report_tool(repo_root: Optional[str] = None) -> dict:
    """Disk-vs-graph completeness report for the code knowledge graph.

    Compares the repository's parseable inventory (collect_all_files) with
    the File nodes stored in the graph:

    * ``missing_from_graph`` — inventory files with no File node (exit-relevant
      signal; the MCP response always reports them, it never fails the call),
    * ``stale_in_graph`` — File nodes whose path no longer exists on disk,
    * ``excluded`` — files outside the inventory classified as ignored /
      binary / no_language / untracked, with counts and capped samples,
    * ``by_language`` / ``inventory_by_language`` — File-node and disk-side
      language counts.

    Read-only: never builds, migrates, or writes the graph. Runs off the
    stdio event loop via ``_offload`` so it stays responsive.

    Args:
        repo_root: Repository root path. Auto-detected from current directory if omitted.
    """
    root = _resolve_repo_root(repo_root)

    return await _offload(
        "coverage_report_tool",
        lambda: coverage_report(root),
        root,
    )


@mcp.tool()
async def run_postprocess_tool(
    flows: bool = True,
    communities: bool = True,
    fts: bool = True,
    repo_root: Optional[str] = None,
    embedding_provider: Optional[str] = None,
    embedding_model: Optional[str] = None,
) -> dict:
    """Run post-processing on existing graph (flows, communities, FTS index).

    Use after building with postprocess="none" or "minimal", or to re-run
    expensive steps independently. Signatures are always computed.

    Offloaded via ``_offload`` (unbounded: post-processing writes) so
    community detection on large graphs doesn't block the MCP event loop.
    See: #46, #136.

    Args:
        flows: Run flow detection. Default: True.
        communities: Run community detection. Default: True.
        fts: Rebuild FTS index. Default: True.
        repo_root: Repository root path. Auto-detected if omitted.
        embedding_provider: Exact provider for an explicit embedding refresh.
            Must be supplied with embedding_model. Default: disabled.
        embedding_model: Exact model for an explicit embedding refresh.
            Must be supplied with embedding_provider. Default: disabled.
    """
    root = _resolve_repo_root(repo_root)

    # bounded=False: post-processing writes flows, communities and the FTS
    # index; a cancelled await would report failure with the write still
    # running.
    return await _offload(
        "run_postprocess_tool",
        lambda: run_postprocess(
            flows=flows, communities=communities, fts=fts, repo_root=root,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
        ),
        root,
        bounded=False,
    )


@mcp.tool()
async def get_minimal_context_tool(
    task: str = "",
    changed_files: Optional[list[str]] = None,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
) -> dict:
    """Get ultra-compact context for any task (~100 tokens).

    Diagnostic readiness and risk context. Call it when a response's
    `_graph` is missing or not ready, or when you need risk/community context.
    Returns graph stats, risk score, top communities/flows, and suggested
    next tools in a single compact response. Returns
    ``status: not_ready`` with a build suggestion when the graph is missing,
    empty, or known to have been built at a different Git commit.

    Args:
        task: What you are doing (e.g. "review PR #42", "debug login timeout").
        changed_files: Explicit list of changed files. Auto-detected if omitted.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for diff comparison. Default: HEAD~1.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_minimal_context_tool",
        lambda: get_minimal_context(
            task=task, changed_files=changed_files,
            repo_root=root, base=base,
        ),
        root,
    )


@mcp.tool()
async def get_impact_radius_tool(
    changed_files: Optional[list[str]] = None,
    max_depth: int = 2,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
    max_results: int = 100,
    offset: int = 0,
) -> dict:
    """Analyze the blast radius of changed files in the codebase.

    Shows which functions, classes, and files are impacted by changes.
    Auto-detects changed files from git if not specified.

    Args:
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        max_depth: Number of hops to traverse in the dependency graph. Default: 2.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes. Default: HEAD~1.
        detail_level: "standard" for full output, "minimal" for compact summary. Default: standard.
        max_results: Maximum impacted nodes per page, highest impact first. Default: 100.
        offset: Impacted nodes to skip; pass the previous ``next_offset``. Default: 0.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_impact_radius_tool",
        lambda: get_impact_radius(
            changed_files=changed_files, max_depth=max_depth,
            repo_root=root, base=base, detail_level=detail_level,
            max_results=max_results, offset=offset,
        ),
        root,
    )


@mcp.tool()
async def query_graph_tool(
    pattern: str,
    target: str,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
    max_results: int = 100,
    offset: int = 0,
) -> dict:
    """Run a predefined graph query to explore code relationships.

    Available patterns:
    - callers_of: Find functions that call the target
    - references_to: Find nodes that reference the target
    - callees_of: Find functions called by the target
    - imports_of: Find what the target imports
    - importers_of: Find files that import the target
    - children_of: Find nodes contained in a file or class
    - tests_for: Find tests for the target
    - inheritors_of: Find classes inheriting from the target
    - triggers_of: Find methods invoked by a scheduler or other trigger
    - triggered_by: Find schedulers or other triggers that invoke the target
    - publishers_of: Find methods that publish an event
    - listeners_of: Find methods that listen for an event
    - handlers_of: Find methods that handle an endpoint
    - endpoints_for: Find endpoints handled by a method
    - consumers_of: Find classes that consume a Spring configuration property
    - file_summary: Get all nodes in a file

    Cross-stack patterns (results carry ``via`` edge kind and ``direction``):
    - pages_for: Pages that render or request a class/endpoint, or that it forwards to
    - requests_to: Pages and scripts requesting an endpoint, class or URL (``/x.action``)
    - included_by: Pages that include a page or script
    - views_of: Pages an action forwards or redirects to
    - forwards_to: Actions that forward or redirect to a page
    - maps_to: Tables an entity maps to, or entities mapped to a table
    - binds_to: Bean properties a form binds, or forms bound to a property
    - styles_of: Selectors a page uses, or pages using a selector or stylesheet

    Args:
        pattern: Query pattern name (see above).
        target: Node name, qualified name, file path, or URL to query.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full output, "minimal" for compact summary. Default: standard.
        max_results: Maximum results to return. Default: 100.
        offset: Results to skip; pass the previous ``next_offset``. Default: 0.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "query_graph_tool",
        lambda: query_graph(
            pattern=pattern, target=target, repo_root=root,
            detail_level=detail_level, max_results=max_results, offset=offset,
        ),
        root,
    )


class QuerySpec(TypedDict):
    """One ``query_graph`` lookup inside ``batch_query_tool``."""

    pattern: str
    target: str
    repo_root: NotRequired[str]


@mcp.tool()
async def batch_query_tool(
    queries: list[QuerySpec],
    repo_root: Optional[str] = None,
    max_results_per_query: int = 10,
) -> dict:
    """Run several query_graph lookups in one call over one open graph.

    Use this instead of query_graph_tool whenever you have more than one
    target or pattern (e.g. callers_of for 3 methods plus tests_for one).
    Accepts the same patterns as query_graph_tool. Up to 25 unique
    (pattern, target) pairs; duplicates run once, extras are counted in
    ``queries_dropped``. Each item has its own status (ok, not_found,
    ambiguous, error) and never fails the batch. Items are compact:
    ``resolved``, ``prod: ["Class.method:line"]``, ``prod_count``,
    ``tests`` (count), ``self_call``; tests_for gives ``tests`` and
    ``test_names``; same-named candidates may be merged (``resolution``).
    An item may carry its own ``repo_root``; an invalid or unbuilt root is
    reported on that item only.

    Args:
        queries: List of {"pattern": ..., "target": ..., "repo_root"?: ...} objects.
        repo_root: Repository root path. Auto-detected if omitted.
        max_results_per_query: Names listed per item. Default: 10.
    """
    root = _resolve_repo_root(repo_root)

    def _run() -> dict:
        result = batch_query(
            queries=[dict(q) for q in queries], repo_root=root,
            max_results_per_query=max_results_per_query,
        )
        try:
            return with_provenance(result, root)
        except ValueError:
            # An invalid repo_root is already reported per item.
            return result

    return await _offload("batch_query_tool", _run, root, provenance=False)


@mcp.tool()
async def get_review_context_tool(
    changed_files: Optional[list[str]] = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
    max_results: int = 50,
    max_files: int = 25,
) -> dict:
    """Generate a focused, token-efficient review context for code changes.

    Combines impact analysis with source snippets and review guidance.
    Use this for comprehensive code reviews.

    Args:
        changed_files: Files to review. Auto-detected from git diff if omitted.
        max_depth: Impact radius depth. Default: 2.
        include_source: Include source code snippets. Default: True.
        max_lines_per_file: Max source lines per file. Default: 200.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection. Default: HEAD~1.
        detail_level: "standard" for full output, "minimal" for
            token-efficient summary. Default: standard.
        max_results: Maximum graph nodes per list and edges to return.
            Default: 50. Each list reports its untruncated ``*_total``.
        max_files: Maximum files listed and given source snippets.
            Default: 25. Snippets share an 800-line budget.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_review_context_tool",
        lambda: get_review_context(
            changed_files=changed_files, max_depth=max_depth,
            include_source=include_source, max_lines_per_file=max_lines_per_file,
            repo_root=root, base=base, detail_level=detail_level,
            max_results=max_results, max_files=max_files,
        ),
        root,
    )


@mcp.tool()
async def semantic_search_nodes_tool(
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
    repo_root: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    detail_level: str = "standard",
    offset: int = 0,
) -> dict:
    """Search for code entities by name, keyword, or semantic similarity.

    Embeddings are off by default, so this is FTS5 / keyword search until a
    repository enables them (`code-review-graph embeddings enable --profile
    balanced`; cloud providers "openai" / "google" / "minimax" / "voyage"
    need their env vars). The response's ``search_mode`` says which search
    produced the results, ``embeddings_state`` is off, ready, stale or
    unavailable, and ``warning`` explains any fallback to keyword search.

    Args:
        query: Search string to match against node names.
        kind: Optional filter: File, Class, Function, Type, or Test.
        limit: Maximum results. Default: 20.
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model for query vectors. Must match the model used
               during embed_graph. Falls back to CRG_EMBEDDING_MODEL env var
               (local), CRG_OPENAI_MODEL (openai), or CRG_VOYAGE_MODEL (voyage).
        provider: Embedding provider for this call: "local", "openai",
                  "google", "minimax", or "voyage". The repository's embedding
                  settings decide when omitted.
        detail_level: "standard" for full output, "minimal" for compact summary. Default: standard.
        offset: Ranked results to skip; pass the previous ``next_offset``. Default: 0.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "semantic_search_nodes_tool",
        lambda: semantic_search_nodes(
            query=query, kind=kind, limit=limit, repo_root=root,
            model=model, provider=provider, detail_level=detail_level,
            offset=offset,
        ),
        root,
    )


@mcp.tool()
async def embed_graph_tool(
    repo_root: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict:
    """Compute vector embeddings for all graph nodes to enable semantic search.

    Embeddings are off by default; `code-review-graph embeddings enable
    --profile balanced` turns them on for the repository and keeps them
    current on updates. This tool is a one-off embed with an explicit provider.

    Requires: pip install code-review-graph[embeddings] (local provider only;
    cloud providers use stdlib urllib).
    Default provider: local. Default model: all-MiniLM-L6-v2.
    Override provider via `provider` param, model via `model` param or
    CRG_EMBEDDING_MODEL / CRG_OPENAI_MODEL / CRG_VOYAGE_MODEL env vars.
    Changing the model or provider re-embeds all nodes automatically.

    After running this, semantic_search_nodes_tool will use vector similarity
    instead of keyword matching for much better results.

    Runs the blocking sentence-transformers / Gemini / HTTP inference off
    the stdio event loop via ``_offload`` (unbounded: embedding writes) so
    it stays responsive — without this wrapper, embedding a large graph
    would silently hang the MCP server on Windows. See: #46, #136.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model. For local: HuggingFace ID/path; for openai:
               model ID (e.g. "text-embedding-3-small"); for google: Gemini
               model ID; for voyage: Voyage model ID (e.g. "voyage-code-3").
               Falls back to CRG_EMBEDDING_MODEL / CRG_OPENAI_MODEL /
               CRG_VOYAGE_MODEL env vars as appropriate.
        provider: "local" (default), "openai", "google", "minimax", or "voyage".
                  "openai" requires CRG_OPENAI_BASE_URL + CRG_OPENAI_API_KEY +
                  CRG_OPENAI_MODEL env vars and accepts any OpenAI-compatible
                  endpoint (real OpenAI, Azure, new-api, LiteLLM, vLLM, etc.).
                  "voyage" requires VOYAGE_API_KEY and defaults to voyage-code-3
                  unless a model arg or CRG_VOYAGE_MODEL is supplied.
    """
    root = _resolve_repo_root(repo_root)

    return await _offload(
        "embed_graph_tool",
        lambda: embed_graph(repo_root=root, model=model, provider=provider),
        root,
        # Writes embeddings; minutes is normal, and a cancelled await would
        # report failure while the embedding run continues.
        bounded=False,
    )


@mcp.tool()
async def list_graph_stats_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Get aggregate statistics about the code knowledge graph.

    Shows total nodes, edges, languages, files, and last update time.
    Useful for checking if the graph is built and up to date.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "list_graph_stats_tool",
        lambda: list_graph_stats(repo_root=root),
        root,
    )


@mcp.tool()
def get_docs_section_tool(
    section_name: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Get a specific section from the LLM-optimized documentation reference.

    Returns only the requested section content for minimal token usage.
    Use this before answering any user question about the plugin.

    Available sections: usage, review-delta, review-pr, commands, legal,
    watch, embeddings, languages, troubleshooting.

    Args:
        section_name: The section to retrieve (e.g. "review-delta", "usage").
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_docs_section(
        section_name=section_name,
        repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
async def find_large_functions_tool(
    min_lines: int = 50,
    kind: Optional[str] = None,
    file_path_pattern: Optional[str] = None,
    limit: int = 50,
    repo_root: Optional[str] = None,
) -> dict:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for decomposition audits, code quality checks, and enforcing
    size limits during code review. Results are ordered by line count.

    Args:
        min_lines: Minimum line count to flag. Default: 50.
        kind: Optional filter: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results. Default: 50.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "find_large_functions_tool",
        lambda: find_large_functions(
            min_lines=min_lines, kind=kind, file_path_pattern=file_path_pattern,
            limit=limit, repo_root=root,
        ),
        root,
    )


@mcp.tool()
async def list_flows_tool(
    sort_by: str = "criticality",
    limit: int = 50,
    kind: Optional[str] = None,
    detail_level: str = "standard",
    repo_root: Optional[str] = None,
    offset: int = 0,
) -> dict:
    """List execution flows in the codebase, sorted by criticality.

    Each flow represents a call chain starting from an entry point
    (HTTP handler, CLI command, test function, etc.). Use this to
    understand the main execution paths through the codebase.

    Args:
        sort_by: Sort column: criticality, depth, node_count, file_count, or name.
        limit: Maximum flows to return. Default: 50.
        kind: Optional filter by entry point kind (e.g. "Test", "Function").
        detail_level: "standard" (default) returns full flow data; "minimal"
                      returns only name, criticality, and node_count per flow.
        repo_root: Repository root path. Auto-detected if omitted.
        offset: Flows to skip; pass the previous ``next_offset``. Default: 0.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "list_flows_tool",
        lambda: list_flows(
            repo_root=root, sort_by=sort_by, limit=limit, kind=kind,
            detail_level=detail_level, offset=offset,
        ),
        root,
    )


@mcp.tool()
async def get_flow_tool(
    flow_id: Optional[int] = None,
    flow_name: Optional[str] = None,
    include_source: bool = False,
    repo_root: Optional[str] = None,
    max_steps: int = 50,
    max_source_lines: int = 400,
) -> dict:
    """Get detailed information about a single execution flow.

    Returns the full call path with each step's function name, file, and
    line numbers. Optionally includes source code snippets for each step.

    Provide either flow_id (from list_flows_tool) or flow_name to search by name.

    Args:
        flow_id: Database ID of the flow.
        flow_name: Name to search for (partial match). Ignored if flow_id given.
        include_source: Include source code snippets for each step. Default: False.
        repo_root: Repository root path. Auto-detected if omitted.
        max_steps: Maximum steps to return; flow.total_steps reports the
            full count. Default: 50.
        max_source_lines: Total source lines across all steps when
            include_source is set. Default: 400.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_flow_tool",
        lambda: get_flow(
            flow_id=flow_id, flow_name=flow_name,
            include_source=include_source, repo_root=root,
            max_steps=max_steps, max_source_lines=max_source_lines,
        ),
        root,
    )


@mcp.tool()
async def get_affected_flows_tool(
    changed_files: Optional[list[str]] = None,
    base: str = "HEAD~1",
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
    max_flows: int = 50,
) -> dict:
    """Find execution flows affected by changed files.

    Identifies which execution flows pass through nodes in the changed files.
    Useful during code review to understand which user-facing or critical paths
    are impacted by a change. Auto-detects changed files from git if not specified.

    Args:
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        base: Git ref for auto-detecting changes. Default: HEAD~1.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full step details, "minimal" for per-flow
            metadata only. Default: standard.
        max_flows: Maximum flows to return; total reports the full count.
            Default: 50. Pass 0 for no caller limit. Standard mode
            additionally caps visible flows at 25 and minimal mode at 500,
            because a standard flow costs ~980 tokens against ~18 minimal.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_affected_flows_tool",
        lambda: get_affected_flows_func(
            changed_files=changed_files, base=base, repo_root=root,
            detail_level=detail_level, max_flows=max_flows,
        ),
        root,
    )


@mcp.tool()
async def list_communities_tool(
    sort_by: str = "size",
    min_size: int = 0,
    detail_level: str = "standard",
    repo_root: Optional[str] = None,
    max_results: int = 50,
    max_members: int = 10,
    offset: int = 0,
) -> dict:
    """List detected code communities in the codebase.

    Each community represents a cluster of related code entities (functions,
    classes) detected via the Leiden algorithm or file-based grouping.
    Use this to understand the high-level structure of the codebase.

    Args:
        sort_by: Sort column: size, cohesion, or name.
        min_size: Minimum community size to include. Default: 0.
        detail_level: "standard" (default) returns full community data;
                      "minimal" returns only name, size, and cohesion
                      per community.
        repo_root: Repository root path. Auto-detected if omitted.
        max_results: Maximum communities to return; total reports the full
            count. Default: 50.
        max_members: Maximum member names listed per community in standard
            mode. Each community's size still reports its true member
            count. Default: 10.
        offset: Communities to skip; pass the previous ``next_offset``. Default: 0.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "list_communities_tool",
        lambda: list_communities_func(
            repo_root=root, sort_by=sort_by, min_size=min_size,
            detail_level=detail_level, max_results=max_results,
            max_members=max_members, offset=offset,
        ),
        root,
    )


@mcp.tool()
async def get_community_tool(
    community_name: Optional[str] = None,
    community_id: Optional[int] = None,
    include_members: bool = False,
    repo_root: Optional[str] = None,
    max_members: int = 25,
) -> dict:
    """Get detailed information about a single code community.

    Returns community metadata including size, cohesion, dominant language,
    and member list. Optionally includes full node details for each member.

    Provide either community_id (from list_communities_tool) or community_name
    to search by name.

    Args:
        community_name: Name to search for (partial match). Ignored if community_id given.
        community_id: Database ID of the community.
        include_members: Include full member node details. Default: False.
        repo_root: Repository root path. Auto-detected if omitted.
        max_members: Maximum member entries to include; the community's
            size still reports its true member count and
            members_truncated marks the cut. Default: 25.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_community_tool",
        lambda: get_community_func(
            community_name=community_name, community_id=community_id,
            include_members=include_members, repo_root=root,
            max_members=max_members,
        ),
        root,
    )


@mcp.tool()
async def get_architecture_overview_tool(
    repo_root: Optional[str] = None,
    detail_level: str = "minimal",
    max_results: int = 100,
    max_members: int = 10,
) -> dict:
    """Generate an architecture overview based on community structure.

    Builds a high-level view of the codebase architecture by analyzing
    community boundaries and cross-community coupling. Includes warnings
    for high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "minimal" (default) drops community member lists
                      and aggregates cross-community edges to one row per
                      community pair (typical reduction: 600KB -> <5KB);
                      "standard" returns full per-edge detail.
        max_results: Maximum cross-community rows and warnings to return;
            cross_community_edges_total reports the full count. Default: 100.
        max_members: Maximum member names per community in standard mode.
            Default: 10.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_architecture_overview_tool",
        lambda: get_architecture_overview_func(
            repo_root=root,
            detail_level=detail_level,
            max_results=max_results,
            max_members=max_members,
        ),
        root,
    )


@mcp.tool()
async def detect_changes_tool(
    base: str = "HEAD~1",
    changed_files: Optional[list[str]] = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
    max_results: int = 25,
    max_flows: int = 20,
) -> dict:
    """Detect changes and produce risk-scored, priority-ordered review guidance.

    Primary tool for code review. Maps git diffs to affected functions,
    flows, communities, and test coverage gaps. Returns risk scores and
    prioritized review items. Replaces get_review_context for change-aware reviews.

    Runs off the stdio event loop via ``_offload``: this tool runs `git diff`
    subprocesses and BFS traversals that can take several seconds on large
    repos, and ``CRG_TOOL_TIMEOUT`` bounds the call when set. See: #46, #136,
    #262.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        include_source: Include source code snippets for changed functions. Default: False.
        max_depth: Impact radius depth for BFS traversal. Default: 2.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full output, "minimal" for
            token-efficient summary. Default: standard.
        max_results: Maximum changed functions, test gaps, and changed files
            to return; the matching *_total fields report the full counts.
            Default: 25.
        max_flows: Maximum affected flows to embed. Embedded flows carry
            per-flow metadata only — use get_affected_flows_tool for step
            detail. Default: 20.
    """
    root = _resolve_repo_root(repo_root)

    return await _offload(
        "detect_changes_tool",
        lambda: detect_changes_func(
            base=base, changed_files=changed_files,
            include_source=include_source, max_depth=max_depth,
            repo_root=root, detail_level=detail_level,
            max_results=max_results, max_flows=max_flows,
        ),
        root,
        timeout_hint=_DETECT_CHANGES_TIMEOUT_HINT,
    )


@mcp.tool()
async def refactor_tool(
    mode: str = "rename",
    old_name: Optional[str] = None,
    new_name: Optional[str] = None,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
    repo_root: Optional[str] = None,
    max_results: int = 50,
    detail_level: str = "standard",
) -> dict:
    """Graph-powered refactoring operations.

    Unified entry point for rename previews, dead code detection, and
    refactoring suggestions.

    Modes:
    - rename: Preview renaming a symbol. Returns an edit list and a refactor_id
      to pass to apply_refactor_tool. Requires old_name and new_name.
    - dead_code: Find unreferenced functions/classes (no callers, tests, or
      importers, and not entry points).
    - suggest: Get community-driven refactoring suggestions (move misplaced
      functions, remove dead code).

    Args:
        mode: Operation mode: "rename", "dead_code", or "suggest".
        old_name: (rename) Current symbol name to rename.
        new_name: (rename) Desired new name for the symbol.
        kind: (dead_code) Optional filter: Function or Class.
        file_pattern: (dead_code) Filter by file path substring.
        repo_root: Repository root path. Auto-detected if omitted.
        max_results: Maximum edits/symbols/suggestions in the response;
            total reports the full count. The stored rename preview keeps
            every edit, so apply_refactor_tool still applies them all.
            Default: 50.
        detail_level: "standard" for full records, "minimal" for
            identifying fields only. Default: standard.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "refactor_tool",
        lambda: refactor_func(
            mode=mode, old_name=old_name, new_name=new_name,
            kind=kind, file_pattern=file_pattern, repo_root=root,
            max_results=max_results, detail_level=detail_level,
        ),
        root,
    )


@mcp.tool()
async def apply_refactor_tool(
    refactor_id: str,
    repo_root: Optional[str] = None,
    dry_run: bool = False,
    max_diff_files: int = 25,
) -> dict:
    """Apply a previously previewed refactoring to source files.

    Takes a refactor_id from a prior refactor_tool(mode="rename") call and
    applies the exact string replacements to the target files. Previews
    expire after 10 minutes.

    Security: All edit paths are validated to be within the repo root.
    Only exact string replacements are performed (no regex, no eval).

    Args:
        refactor_id: The refactor ID from refactor_tool's response.
        repo_root: Repository root path. Auto-detected if omitted.
        dry_run: If True, return a unified diff of what would change
            without touching any files. The refactor_id remains valid so
            the same preview can be applied in a follow-up call without
            dry_run. Use this for a human-in-the-loop review before
            committing changes to disk. See: #176
        max_diff_files: Maximum per-file diffs to include in a dry run.
            would_modify still lists every file. Default: 25.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "apply_refactor_tool",
        lambda: apply_refactor_func(
            refactor_id=refactor_id, repo_root=root,
            dry_run=dry_run, max_diff_files=max_diff_files,
        ),
        root,
        # Writes to source files: a cancelled await would report failure with
        # the rename already on disk, and the retry would fail again with
        # "not found or expired".
        bounded=False,
    )


@mcp.tool()
async def generate_wiki_tool(
    repo_root: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Generate a markdown wiki from the code community structure.

    Creates a wiki page for each detected community and an index page.
    Pages are written to .code-review-graph/wiki/ inside the repository.
    Only regenerates pages whose content has changed unless force=True.

    Offloaded via ``_offload`` (unbounded: the wiki tree is written) — on
    large graphs the page-generation loop touches every community and issues
    many SQLite reads, which would block the MCP event loop. See: #46, #136.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        force: If True, regenerate all pages even if content unchanged. Default: False.
    """
    root = _resolve_repo_root(repo_root)

    return await _offload(
        "generate_wiki_tool",
        lambda: generate_wiki_func(repo_root=root, force=force),
        root,
        # Writes the wiki tree; minutes is normal, and a cancelled await
        # would report failure while the writes continue.
        bounded=False,
    )


@mcp.tool()
async def get_wiki_page_tool(
    community_name: str,
    repo_root: Optional[str] = None,
    max_chars: int = 20000,
) -> dict:
    """Retrieve a specific wiki page by community name.

    Returns the markdown content of the wiki page for the given community.
    The wiki must have been generated first via generate_wiki_tool.

    Args:
        community_name: Community name to look up.
        repo_root: Repository root path. Auto-detected if omitted.
        max_chars: Maximum characters of page content to return;
            total_chars reports the real length. Default: 20000.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_wiki_page_tool",
        lambda: get_wiki_page_func(
            community_name=community_name, repo_root=root,
            max_chars=max_chars,
        ),
        root,
    )


@mcp.tool()
async def get_hub_nodes_tool(
    top_n: int = 10,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
) -> dict:
    """Find the most connected nodes in the codebase (architectural hotspots).

    Hub nodes have the highest total degree (in + out edges). Changes to
    them have disproportionate blast radius. Excludes File nodes.

    Args:
        top_n: Number of top hubs to return (capped at 100). Default: 10.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full node data, "minimal" for name,
            kind, and total_degree only. Default: standard.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_hub_nodes_tool",
        lambda: get_hub_nodes_func(
            repo_root=root, top_n=top_n, detail_level=detail_level,
        ),
        root,
    )


@mcp.tool()
async def get_bridge_nodes_tool(
    top_n: int = 10,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
) -> dict:
    """Find architectural chokepoints via betweenness centrality.

    Bridge nodes sit on shortest paths between many node pairs.
    If they break, multiple code regions lose connectivity.
    Uses sampling approximation for graphs > 5000 nodes.

    Args:
        top_n: Number of top bridges to return (capped at 100). Default: 10.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full node data, "minimal" for name,
            kind, and betweenness only. Default: standard.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_bridge_nodes_tool",
        lambda: get_bridge_nodes_func(
            repo_root=root, top_n=top_n, detail_level=detail_level,
        ),
        root,
    )


@mcp.tool()
async def get_knowledge_gaps_tool(
    repo_root: Optional[str] = None,
    max_per_category: int = 15,
    detail_level: str = "standard",
) -> dict:
    """Identify structural weaknesses in the codebase graph.

    Finds isolated nodes (disconnected), thin communities (< 3 members),
    untested hotspots (high-degree nodes without test coverage), and
    single-file communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        max_per_category: Maximum entries per gap category; summary and
            total_gaps still report the untruncated counts. Default: 15.
        detail_level: "standard" for full gap records, "minimal" to drop
            file paths. Default: standard.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_knowledge_gaps_tool",
        lambda: get_knowledge_gaps_func(
            repo_root=root, max_per_category=max_per_category,
            detail_level=detail_level,
        ),
        root,
    )


@mcp.tool()
async def get_surprising_connections_tool(
    top_n: int = 15,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
) -> dict:
    """Find unexpected architectural coupling via composite surprise scoring.

    Scores edges by: cross-community (+0.3), cross-language (+0.2),
    peripheral-to-hub (+0.2), cross-test-boundary (+0.15), and
    unusual edge kinds (+0.15).

    Args:
        top_n: Number of top surprises to return (capped at 100). Default: 15.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full edge records, "minimal" for
            source, target, kind, and score only. Default: standard.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_surprising_connections_tool",
        lambda: get_surprising_connections_func(
            repo_root=root, top_n=top_n, detail_level=detail_level,
        ),
        root,
    )


@mcp.tool()
async def get_suggested_questions_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Auto-generate review questions from graph analysis.

    Produces prioritized questions about: bridge nodes needing tests,
    untested hub nodes, surprising cross-community coupling, thin
    communities, and untested hotspots.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "get_suggested_questions_tool",
        lambda: get_suggested_questions_func(repo_root=root),
        root,
    )


@mcp.tool()
async def traverse_graph_tool(
    query: str,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
    repo_root: Optional[str] = None,
) -> dict:
    """BFS/DFS traversal from best-matching node with token budget.

    Free-form graph exploration: finds the node best matching your
    query, then traverses outward via BFS or DFS up to the given
    depth, collecting connected nodes within the token budget.

    Args:
        query: Search string to find the starting node.
        mode: Traversal mode: "bfs" (breadth-first) or "dfs"
            (depth-first). Default: bfs.
        depth: Max traversal depth (1-6). Default: 3.
        token_budget: Approximate token limit for results.
            Default: 2000.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "traverse_graph_tool",
        lambda: traverse_graph_func(
            query=query, mode=mode, depth=depth,
            token_budget=token_budget,
            repo_root=root or "",
        ),
        root,
    )


@mcp.tool()
def list_repos_tool() -> dict:
    """List all registered repositories in the multi-repo registry.

    Returns the list of repos registered at ~/.code-review-graph/registry.json.
    Use the CLI 'register' command to add repos.
    """
    return list_repos_func()


@mcp.tool()
async def cross_repo_search_tool(
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
    max_results: int = 50,
) -> dict:
    """Search for code entities across all registered repositories.

    Runs hybrid search on each registered repo's graph database and interleaves
    results by repository-local rank. Equal ranks follow registry order, and up
    to ``limit`` results per searched repo may be returned. Register repos first
    with the CLI 'register' command.

    Args:
        query: Search string to match against node names.
        kind: Optional filter: File, Class, Function, Type, or Test.
        limit: Maximum results per repo. Default: 20.
        max_results: Maximum merged results across all repos; total reports
            the untruncated merged count. Default: 50.
    """
    return await _offload(
        "cross_repo_search_tool",
        lambda: cross_repo_search_func(
            query=query, kind=kind, limit=limit, max_results=max_results,
        ),
        None,
        provenance=False,
    )


@mcp.tool()
async def orient_tool(
    query: str,
    repo_root: Optional[str] = None,
    provider: Optional[str] = None,
    limit: int = 8,
    detail_level: str = "standard",
) -> dict:
    """One-call codebase mini-map for a task string.

    Returns top functions/classes (keyword search, hybrid with vectors when
    embeddings are enabled), top files, matching communities and 1-line
    stats. Use FIRST for orientation instead of 3-4 separate search calls.

    Args:
        query: Natural-language or symbol-ish task description.
        repo_root: Repository root path. Auto-detected if omitted.
        provider: Embedding provider for this call; the repository's
            embedding settings decide when omitted.
        limit: Maximum top functions/classes. Default: 8.
        detail_level: "standard", or "minimal" for names and locations only.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "orient_tool",
        lambda: orient(
            query=query, repo_root=root, provider=provider, limit=limit,
            detail_level=detail_level,
        ),
        root,
    )


@mcp.tool()
async def shortest_path_between_tool(
    symbol_a: str,
    symbol_b: str,
    mode: str = "call",
    max_depth: int = 6,
    repo_root: Optional[str] = None,
) -> dict:
    """BFS shortest paths between two symbols over CALLS/IMPORTS_FROM edges.

    mode: "call" (A calls ... B), "import", "both". Returns up to 3
    paths as repo-relative qualified-name chains. Hub helpers
    (toString/equals/Logger/...) are skipped as intermediates.

    Args:
        symbol_a: Start symbol (bare or qualified name).
        symbol_b: End symbol (bare or qualified name).
        mode: "call", "import", or "both". Default: call.
        max_depth: Maximum hops to search. Default: 6.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "shortest_path_between_tool",
        lambda: shortest_path_between(
            symbol_a=symbol_a, symbol_b=symbol_b, mode=mode,
            max_depth=max_depth, repo_root=root,
        ),
        root,
    )


@mcp.tool()
async def common_callers_of_tool(
    symbol_a: str,
    symbol_b: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Callers shared by both symbols (intersection of callers).

    Accepts bare names (resolved via an anchored name-boundary match)
    or qualified names.

    Args:
        symbol_a: First symbol (bare or qualified name).
        symbol_b: Second symbol (bare or qualified name).
        repo_root: Repository root path. Auto-detected if omitted.
    """
    root = _resolve_repo_root(repo_root)
    return await _offload(
        "common_callers_of_tool",
        lambda: common_callers_of(
            symbol_a=symbol_a, symbol_b=symbol_b, repo_root=root,
        ),
        root,
    )


@mcp.prompt()
def review_changes(base: str = "HEAD~1") -> list[Message]:
    """Pre-commit review workflow using detect_changes, affected_flows, and test gaps.

    Produces a structured code review with risk levels and actionable findings.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return review_changes_prompt(base=base)


@mcp.prompt()
def architecture_map() -> list[Message]:
    """Architecture documentation using communities, flows, and Mermaid diagrams.

    Generates a comprehensive architecture map with module summaries and coupling warnings.
    """
    return architecture_map_prompt()


@mcp.prompt()
def debug_issue(description: str = "") -> list[Message]:
    """Guided debugging using search, flow tracing, and recent changes.

    Systematic debugging workflow that traces execution paths and identifies root causes.

    Args:
        description: Description of the issue to debug.
    """
    return debug_issue_prompt(description=description)


@mcp.prompt()
def onboard_developer() -> list[Message]:
    """New developer orientation using stats, architecture, and critical flows.

    Creates an onboarding guide covering codebase structure, key modules, and patterns.
    """
    return onboard_developer_prompt()


@mcp.prompt()
def pre_merge_check(base: str = "HEAD~1") -> list[Message]:
    """PR readiness check with risk scoring, test gaps, and dead code detection.

    Produces a merge readiness report with risk assessment and recommendations.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return pre_merge_check_prompt(base=base)


# Named tool sets for ``serve --tools`` / ``CRG_TOOLS``. ``all`` keeps every
# tool. ``agent`` is the working set coding agents need: orient, look up,
# assess a change, keep the graph fresh.
TOOL_PRESETS: dict[str, tuple[str, ...]] = {
    "agent": (
        "get_minimal_context_tool",
        "orient_tool",
        "semantic_search_nodes_tool",
        "query_graph_tool",
        "batch_query_tool",
        "traverse_graph_tool",
        "get_impact_radius_tool",
        "get_affected_flows_tool",
        "detect_changes_tool",
        "get_review_context_tool",
        "build_or_update_graph_tool",
        "coverage_report_tool",
        "list_graph_stats_tool",
        "get_docs_section_tool",
    ),
    "all": (),
}


def resolve_tool_selection(raw: str | None) -> set[str] | None:
    """Tool names a ``--tools`` value keeps; None keeps every tool.

    Entries are tool names or preset names (``agent``, ``all``), comma
    separated and combinable (``agent,embed_graph_tool``).
    """
    entries = [t.strip() for t in (raw or "").split(",") if t.strip()]
    if not entries or "all" in entries:
        return None
    allowed: set[str] = set()
    for entry in entries:
        allowed.update(TOOL_PRESETS.get(entry, (entry,)))
    return allowed


def _apply_tool_filter(tools: str | None = None) -> None:
    """Remove tools not listed in the allow-list.

    Accepts a comma-separated string of tool names and presets (see
    ``TOOL_PRESETS``: ``agent``, ``all``).  When set, every registered MCP
    tool whose name is **not** selected is removed via
    ``FastMCP.remove_tool()``.

    The allow-list can be supplied in two ways (first match wins):

    1. ``tools`` argument (from ``serve --tools ...``).
    2. ``CRG_TOOLS`` environment variable.

    When neither is set, or ``all`` is listed, all tools remain available.

    This is useful for token-constrained environments: every registered
    tool's description is sent on each LLM turn, and the ``agent`` preset
    keeps the 14 tools coding agents use.

    Example::

        # via CLI
        code-review-graph serve --tools agent
        code-review-graph serve --tools query_graph_tool,semantic_search_nodes_tool

        # via env var
        CRG_TOOLS=agent
    """
    import asyncio
    import os

    allowed = resolve_tool_selection(tools or os.environ.get("CRG_TOOLS"))
    if not allowed:
        return
    # FastMCP >=3 exposes tool enumeration via the async ``list_tools``
    # method.  ``_apply_tool_filter`` is typically called from
    # ``main()`` before the MCP event loop starts, but tests may invoke
    # it from within a running event loop — in that case ``asyncio.run``
    # raises ``RuntimeError``.  Fall back to running the coroutine on a
    # dedicated short-lived loop in a worker thread.  Earlier code path
    # relied on ``mcp._tool_manager._tools`` which is a private
    # attribute that was removed in fastmcp>=3.0.
    def _list_tool_names() -> list[str]:
        coro_factory = mcp.list_tools
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return [t.name for t in asyncio.run(coro_factory())]
        import concurrent.futures

        def _runner() -> list[str]:
            return [t.name for t in asyncio.run(coro_factory())]

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result()

    names = _list_tool_names()
    unknown = sorted(allowed - set(names))
    if unknown:
        logger.warning("Unknown tool names in tool selection ignored: %s", unknown)
    for name in names:
        if name not in allowed:
            mcp.local_provider.remove_tool(name)



def main(
    repo_root: str | None = None,
    tools: str | None = None,
    auto_watch: bool = False,
    *,
    transport: str = "stdio",
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Run the MCP server (stdio or HTTP).

    On Windows, Python 3.8+ defaults to ``ProactorEventLoop``, which
    interacts poorly with ``concurrent.futures.ProcessPoolExecutor``
    (used by ``full_build``) over a stdio MCP transport — the combination
    produces silent hangs on ``build_or_update_graph_tool`` and
    ``embed_graph_tool``. Switching to ``WindowsSelectorEventLoopPolicy``
    before fastmcp starts its loop avoids the deadlock.
    See: #46, #136

    Args:
        repo_root: Default repository root for all tool calls.
        tools: Comma-separated list of tool names to expose.
            Falls back to ``CRG_TOOLS`` env var.  When unset, all
            tools are available.
        auto_watch: Start filesystem watcher in a background daemon thread
            while the MCP server runs.
        transport: ``"stdio"`` (default) or ``"streamable-http"`` for local HTTP.
        host: Bind address when using HTTP (required for HTTP; set by CLI).
        port: Port when using HTTP (required for HTTP; set by CLI).
    """
    global _default_repo_root
    root = Path(repo_root) if repo_root else find_project_root()
    # A client-launched server often starts in $HOME or "/"; that cwd is not a
    # repository, so it must not become every call's silent default.
    if repo_root or (root / ".git").exists() or (root / ".code-review-graph").exists():
        _default_repo_root = str(root)
    else:
        _default_repo_root = None
    _apply_tool_filter(tools)

    previous_stdio_state = _incremental._MCP_STDIO_ACTIVE
    _incremental._MCP_STDIO_ACTIVE = transport == "stdio"
    watch_store: GraphStore | None = None
    try:
        if auto_watch:
            watch_store = GraphStore(get_db_path(root))
            thread = start_watch_thread(root, watch_store, daemon=True)
            if thread is None:
                logger.warning("Auto-watch was requested but could not be started")

        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            # Pre-warm sentence-transformers on the main thread before fastmcp's
            # event loop starts. Lazy-loading ``torch`` + tokenizers inside an
            # executor worker thread deadlocks ``semantic_search_nodes_tool`` on
            # Windows stdio MCP (DLL init / OpenMP thread-pool registration grabs
            # locks the loop needs). #385 added ``asyncio.to_thread`` to peer
            # tools but cannot fix this case — the dangerous initialization has
            # to happen on the main thread before any worker thread is spawned.
            # Only the sentence-transformers profiles load torch; the rest stay lazy.
            settings = load_embedding_settings(root)
            if settings.enabled and settings.profile in ("legacy", "local"):
                from .embedding_providers.profiles import LEGACY
                from .embeddings import prewarm_local_embeddings

                legacy_model = LEGACY.model if settings.profile == "legacy" else None
                prewarm_local_embeddings(settings.model or legacy_model)

        if transport == "stdio":
            # Stdio MCP must keep stdout strictly JSON-RPC. FastMCP's banner/update
            # notices corrupt the handshake stream on clients like Codex CLI.
            mcp.run(transport="stdio", show_banner=False)
        elif transport == "streamable-http":
            if host is None or port is None:
                raise ValueError("streamable-http transport requires host and port")
            # Validate Host/Origin on the loopback HTTP endpoint. Without it a web
            # page the user visits can point a hostname it controls at 127.0.0.1
            # (DNS rebinding) and drive the tools, which read the user's code.
            # Non-browser MCP clients send no Origin and are unaffected; see
            # code_review_graph.http_origin_guard.
            from .http_origin_guard import build_http_middleware

            mcp.run(
                transport="streamable-http",
                host=host,
                port=port,
                middleware=build_http_middleware(host, port),
            )
        else:
            raise ValueError(f"unsupported transport: {transport!r}")
    finally:
        if watch_store is not None:
            watch_store.close()
        _incremental._MCP_STDIO_ACTIVE = previous_stdio_state


if __name__ == "__main__":
    main()
