"""Tools 2, 3, 5, 6, 9: query / search / stats helpers."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config_keys import normalize_spring_config_key
from ..context_savings import attach_context_savings, estimate_file_tokens
from ..embeddings import EmbeddingStore
from ..graph import GraphNode, GraphStore, _sanitize_name, edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import GitUnavailableError, discover_review_changes, get_db_path
from ..kinds import EDGE_KINDS_BY_NAME
from ..parser import _is_test_file, normalize_file_path
from ..search import hybrid_search
from ..uncertainty import (
    empty_impact_confidence,
    empty_query_confidence,
    empty_search_confidence,
)
from ._common import (
    _BUILTIN_CALL_NAMES,
    _get_store,
    _resolve_graph_file_paths,
    _resolve_root,
    is_builtin_call_name,
)
from .context import missing_graph_response
from .navigation import _short

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool 2: get_impact_radius
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = {
    "callers_of": "Find all functions that call a given function",
    "references_to": "Find all nodes that reference a given symbol",
    "callees_of": "Find all functions called by a given function",
    "imports_of": "Find all imports of a given file or module",
    "importers_of": "Find all files that import a given file or module",
    "children_of": "Find all nodes contained in a file or class",
    "tests_for": "Find all tests for a given function or class",
    "inheritors_of": "Find all classes that inherit from a given class",
    "triggers_of": "Find methods invoked by a scheduler or other trigger",
    "triggered_by": "Find schedulers or other triggers that invoke a method",
    "publishers_of": "Find methods that publish an event",
    "listeners_of": "Find methods that listen for an event",
    "handlers_of": "Find methods that handle an endpoint",
    "endpoints_for": "Find endpoints handled by a method",
    "consumers_of": "Find classes that consume a Spring configuration property",
    "file_summary": "Get a summary of all nodes in a file",
}


def _kinds(*names: str) -> frozenset[str]:
    """Edge kind names checked against the registry, so a rename fails loudly."""
    unknown = [n for n in names if n not in EDGE_KINDS_BY_NAME]
    if unknown:
        raise KeyError(f"edge kinds missing from kinds.py: {unknown}")
    return frozenset(names)


# Cross-stack patterns: (description, incoming kinds, outgoing kinds). A
# class target also covers its members, and the endpoints they handle, so
# "pages for OrderActionBean" finds pages bound to the class, its handler
# methods or their routes. Kinds with no producer yet return empty results
# until a resolver emits them.
_CROSS_STACK_PATTERNS: dict[str, tuple[str, frozenset[str], frozenset[str]]] = {
    "pages_for": (
        "Find pages that render, request or are forwarded to from a class or endpoint",
        _kinds("RENDERS", "REQUESTS"), _kinds("FORWARDS_TO"),
    ),
    "requests_to": (
        "Find pages and scripts that request an endpoint, class or URL",
        _kinds("REQUESTS"), frozenset(),
    ),
    "included_by": (
        "Find pages that include a page or script",
        _kinds("INCLUDES"), frozenset(),
    ),
    "views_of": (
        "Find pages an action forwards or redirects to",
        frozenset(), _kinds("FORWARDS_TO"),
    ),
    "forwards_to": (
        "Find actions that forward or redirect to a page",
        _kinds("FORWARDS_TO"), frozenset(),
    ),
    "maps_to": (
        "Find tables an entity maps to, or entities mapped to a table",
        _kinds("MAPS_TO"), _kinds("MAPS_TO"),
    ),
    "binds_to": (
        "Find bean properties a form binds, or forms bound to a property",
        _kinds("BINDS"), _kinds("BINDS"),
    ),
    "styles_of": (
        "Find selectors a page uses, or pages using a selector or stylesheet",
        _kinds("USES_STYLE"), _kinds("USES_STYLE"),
    ),
}
_QUERY_PATTERNS.update({name: spec[0] for name, spec in _CROSS_STACK_PATTERNS.items()})
# forwards_to joins the URL-addressable patterns: its edges record the
# matched route in extra, so "/x/Y.action" resolves like a requests_to URL.
_ROUTE_PATTERNS = frozenset({"pages_for", "requests_to", "forwards_to"})
_MEMBER_DEPTH = 2


def query_patterns() -> dict[str, str]:
    """Every query_graph pattern name with its description."""
    return dict(_QUERY_PATTERNS)


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """The contract error shape; ``error`` repeats the message for old readers."""
    return {"status": "error", "error_code": code, "message": message,
            "error": message, **extra}


def _next_offset(offset: int, shown: int, total: int) -> int | None:
    """Offset of the next page, or None when this page reached the end."""
    end = offset + shown
    return end if end < total else None


def _validate_offset(offset: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be an integer greater than or equal to 0")


def _looks_like_url(target: str) -> bool:
    return target.startswith(("/", "http://", "https://")) and "::" not in target


def _member_qns(store: GraphStore, qn: str) -> set[str]:
    """*qn* plus nodes it contains (file -> class -> method) and their endpoints."""
    members = {qn}
    frontier = {qn}
    for _ in range(_MEMBER_DEPTH):
        nxt: set[str] = set()
        for parent in frontier:
            for e in store.iter_edges_by_source(parent):
                if e.kind == "CONTAINS" and e.target_qualified not in members:
                    nxt.add(e.target_qualified)
        members |= nxt
        frontier = nxt
    for member in list(members):
        for e in store.iter_edges_by_source(member):
            if e.kind == "HANDLES":
                members.add(e.target_qualified)
    return members


def _route_edges(store: GraphStore, kinds: frozenset[str], url: str) -> list[Any]:
    """Edges of *kinds* whose recorded route or URL is *url*."""
    route = url.split("?", 1)[0].split("#", 1)[0]
    if not kinds:
        return []
    marks = ", ".join("?" for _ in kinds)
    rows = store._conn.execute(
        f"SELECT * FROM edges WHERE kind IN ({marks}) AND ("  # nosec B608
        "target_qualified = ? OR json_extract(extra, '$.route') = ? "
        "OR json_extract(extra, '$.url') = ?)",
        (*sorted(kinds), url, route, url),
    ).fetchall()
    return [store._row_to_edge(r) for r in rows]


def _route_endpoints(store: GraphStore, url: str) -> set[str]:
    route = url.split("?", 1)[0].split("#", 1)[0]
    rows = store._conn.execute(
        "SELECT qualified_name FROM nodes WHERE kind = 'Endpoint' "
        "AND json_extract(extra, '$.route') = ?",
        (route,),
    ).fetchall()
    return {r[0] for r in rows}


def _cross_stack_results(
    store: GraphStore,
    pattern: str,
    qn: str,
    add_result: Any,
) -> None:
    """Emit the far end of every cross-stack edge the pattern follows."""
    _, incoming, outgoing = _CROSS_STACK_PATTERNS[pattern]
    url = pattern in _ROUTE_PATTERNS and _looks_like_url(qn)
    members = _route_endpoints(store, qn) | {qn} if url else _member_qns(store, qn)
    seen: set[tuple[str, str]] = set()

    def emit(edge: Any, far: str, direction: str) -> None:
        key = (far, edge.kind)
        if key in seen or far in members:
            return
        seen.add(key)
        node = store.get_node(far)
        result: dict[str, Any] = (
            node_to_dict(node) if node
            else {"name": _sanitize_name(far), "qualified_name": _sanitize_name(far),
                  "kind": None, "resolution": "unresolved"}
        )
        result["via"] = edge.kind
        result["direction"] = direction
        add_result(result, edge)

    for member in sorted(members):
        if incoming:
            for e in store.iter_edges_by_target(member):
                if e.kind in incoming:
                    emit(e, e.source_qualified, "incoming")
        if outgoing:
            for e in store.iter_edges_by_source(member):
                if e.kind in outgoing:
                    emit(e, e.target_qualified, "outgoing")
    if url:
        for e in _route_edges(store, incoming, qn):
            emit(e, e.source_qualified, "incoming")

_JAVA_FQN_PART = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_MAX_FQN_CANDIDATES = 100

# Statically typed languages: a member call there belongs to the receiver's
# type, so a bare-name match on a receiver edge is not evidence of a caller.
# Go is absent: its ``pkg.Func()`` qualifier is recorded as a receiver.
_TYPED_RECEIVER_LANGUAGES = frozenset({
    "java", "kotlin", "csharp", "cpp", "rust", "scala", "swift",
    "typescript", "tsx", "dart",
})


def _is_member_call(extra: dict[str, Any]) -> bool:
    """A call on an object, not on ``this``/``self`` or an imported namespace."""
    receiver = extra.get("receiver")
    lexical = receiver in ("self", "cls", "this")
    if extra.get("receiver_import"):
        return False
    return bool((receiver and not lexical) or extra.get("receiver_expression"))


def _looks_like_java_method_fqn(target: str) -> bool:
    """Return whether *target* has a package/Class/method-like shape."""
    if "::" in target:
        return False
    parts = target.split(".")
    if len(parts) < 2 or not all(_JAVA_FQN_PART.fullmatch(part) for part in parts):
        return False
    # Two segments are accepted only for the conventional Class.method form;
    # this keeps ordinary dotted filenames/modules on the legacy path.
    return len(parts) >= 3 or parts[-2][:1].isupper()


def _java_fqn_candidates(store: GraphStore, target: str) -> list[GraphNode] | None:
    """Resolve Java FQNs using language plus class/file evidence.

    ``None`` means that the target is not Java-FQN-shaped. An empty list means
    it is shaped like one but no safe match exists, so callers must not fall
    back to an unrelated globally unique method name.
    """
    if not _looks_like_java_method_fqn(target):
        return None

    parts = target.split(".")
    class_name, method_name = parts[-2:]
    # Filter by name and language in SQL: a common method name (save, get)
    # has far more matches than any fixed search limit would return.
    rows = store._conn.execute(
        "SELECT * FROM nodes WHERE name = ? AND lower(language) = 'java' "
        "AND kind IN ('Function', 'Test') "
        "AND (parent_name = ? OR parent_name LIKE ? OR file_path LIKE ? "
        "OR qualified_name LIKE ?) ORDER BY qualified_name",
        (
            method_name, class_name, f"%.{class_name}", f"%/{class_name}.java",
            f"%{class_name}.{method_name}%",
        ),
    ).fetchall()
    matches: list[GraphNode] = []
    for candidate in map(store._row_to_node, rows):
        parent_name = candidate.parent_name or ""
        parent_match = parent_name.rsplit(".", 1)[-1] == class_name
        file_match = Path(candidate.file_path).stem == class_name
        qualified_tail = candidate.qualified_name.rsplit("::", 1)[-1].split("(", 1)[0]
        qualified_match = qualified_tail.endswith(f"{class_name}.{method_name}")
        if parent_match or file_match or qualified_match:
            matches.append(candidate)
    return matches


def _overload_nodes(store: GraphStore, target: str) -> list[GraphNode]:
    """Nodes whose identity is ``target(...)``: the overloads behind a base name."""
    if "::" not in target or target.endswith(")"):
        return []
    rows = store._conn.execute(
        "SELECT * FROM nodes WHERE qualified_name >= ? AND qualified_name < ? "
        "ORDER BY qualified_name",
        (f"{target}(", f"{target})"),
    ).fetchall()
    return [store._row_to_node(row) for row in rows]


def _rank_disambiguation_candidates(
    candidates: list[GraphNode], target: str,
) -> list[dict[str, Any]]:
    """Return deterministic, sanitized candidates ordered by match quality."""
    target_lower = target.lower()

    def score(node: GraphNode) -> tuple[int, str]:
        if node.qualified_name == target:
            rank = 0
        elif node.name == target:
            rank = 1
        elif target_lower in node.qualified_name.lower():
            rank = 2
        else:
            rank = 3
        return rank, node.qualified_name

    return [node_to_dict(node) for node in sorted(candidates, key=score)]


_MERGE_PATTERNS = ("callers_of", "callees_of", "tests_for")
_MERGE_MAX_CANDIDATES = 5


def _overload_label(node: GraphNode) -> str:
    """``name(params)`` from a C++ or Java overload's qualified name."""
    tail = node.qualified_name.rsplit("::", 1)[-1]
    start = tail.find(f"{node.name}(")
    return _sanitize_name(tail[start:] if start >= 0 else tail)


def _merge_candidates(
    pattern: str,
    target: str,
    candidates: list[GraphNode],
    ranked: list[dict[str, Any]],
    detail_level: str,
    max_results: int,
    response_limit: int,
    store_root: tuple[GraphStore, Path],
    offset: int = 0,
) -> dict[str, Any]:
    """Answer for every same-named candidate instead of returning ``ambiguous``.

    Distinct owners (file, parent) give ``per_candidate``; several candidates in
    one owner are a C++ overload set whose callers may be recorded only as
    ambiguous bare-name edges, so those are added for ``callers_of``.
    """
    owners = {(c.file_path, c.parent_name) for c in candidates}
    overload_set = len(owners) < len(candidates)
    groups: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_edges: set[str] = set()

    def add(result: dict[str, Any], via: str) -> None:
        key = result.get("qualified_name") or repr(sorted(result.items()))
        if key in seen:
            return
        seen.add(key)
        results.append({**result, "via": via})

    for candidate in candidates:
        sub = query_graph(
            pattern, candidate.qualified_name, detail_level="standard",
            max_results=max_results, _store=store_root,
        )
        sub_results = sub.get("results", [])
        # Name-only fallback matches are the same for every same-named
        # candidate: report them once and keep them out of group counts.
        name_only = sum(1 for r in sub_results if r.get("target_resolution") == "unresolved")
        groups.append({
            "name": _sanitize_name(candidate.name),
            "qualified_name": _sanitize_name(candidate.qualified_name),
            "parent_name": _sanitize_name(candidate.parent_name)
            if candidate.parent_name else candidate.parent_name,
            "line_start": candidate.line_start,
            "result_count": sub.get("result_count", 0) - name_only,
        })
        via = (
            _overload_label(candidate) if overload_set
            else _sanitize_name(candidate.qualified_name)
        )
        for result in sub_results:
            unresolved = result.get("target_resolution") == "unresolved"
            add(result, "name_only" if unresolved else via)
        for edge in sub.get("edges", []):
            edge_key = repr(sorted(edge.items()))
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append(edge)

    if overload_set and pattern == "callers_of":
        store = store_root[0]
        overload_qns = {c.qualified_name for c in candidates}
        first = candidates[0]
        # Unbound calls keep the bare name (C++) or the Java base name ``C.m``.
        bare_edges = list(store.iter_edges_by_target_name(
            first.name, language=first.language or None,
        ))
        for base in sorted({c.qualified_name.split("(", 1)[0] for c in candidates}):
            if "::" in base and base not in overload_qns:
                bare_edges.extend(store.iter_edges_by_target(base))
        for edge in bare_edges:
            ambiguous = edge.extra.get("ambiguous_targets")
            if (
                edge.kind != "CALLS"
                or not isinstance(ambiguous, list)
                or not set(ambiguous) <= overload_qns
            ):
                continue
            caller = store.get_node(edge.source_qualified)
            if caller:
                add(node_to_dict(caller), "ambiguous_overload")
                edges.append(edge_to_dict(edge))

    total = len(results)
    visible = results[offset:offset + response_limit]
    if detail_level == "minimal":
        visible = [
            {k: r[k] for k in ("name", "kind", "file_path", "indirect", "via") if k in r}
            for r in visible
        ]
    resolution = "overload_set" if overload_set else "per_candidate"
    response: dict[str, Any] = {
        "status": "ok",
        "pattern": pattern,
        "target": target,
        "description": _QUERY_PATTERNS[pattern],
        "summary": (
            f"'{target}' matches {len(candidates)} same-named node(s); "
            f"{resolution}: {total} result(s) across all of them."
        ),
        "resolution": resolution,
        "groups": groups,
        "candidates": ranked,
        "result_count": total,
        "results_omitted": max(0, total - offset - len(visible)),
        "next_offset": _next_offset(offset, len(visible), total),
        "results": visible,
    }
    if detail_level != "minimal":
        response["edges"] = edges
    return response


def get_impact_radius(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    max_results: int = 100,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
    offset: int = 0,
) -> dict[str, Any]:
    """Analyze the blast radius of changed files.

    Args:
        changed_files: Explicit list of changed file paths (relative to repo root).
                       If omitted, auto-detects from git diff.
        max_depth: How many hops to traverse in the graph (default: 2).
        max_results: Maximum impacted nodes to return (default: 100).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        detail_level: "standard" (full output) or "minimal" (summary only).
        offset: Impacted nodes to skip, highest impact first (default: 0).
            Pass the previous response's ``next_offset`` for the next page.

    Returns:
        Changed nodes, impacted nodes, impacted files, connecting edges,
        plus ``truncated`` flag, ``total_impacted`` count and
        ``next_offset`` (None on the last page).
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")
    _validate_offset(offset)

    store, root = _get_store(repo_root)
    try:
        git_error = ""
        if changed_files is None:
            try:
                changed_files, base = discover_review_changes(root, base)
            except GitUnavailableError as exc:
                # Distinct from the "no changed files" answer below: that one
                # is an all-clear a client will act on. Git that could not be
                # run, or that overran the discovery budget, says nothing
                # about the working tree (#262).
                logger.warning("change discovery unavailable for %s: %s", root, exc)
                git_error = str(exc)

        if not changed_files:
            empty: dict[str, Any] = {
                "status": "ok",
                "summary": "No changed files detected.",
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "truncated": False,
                "total_impacted": 0,
            }
            if git_error:
                empty["summary"] = "No changed files: git unavailable. " + git_error
                empty["git"] = "unavailable"
                empty["warning"] = git_error
            return empty

        # Resolve user-facing paths to the file paths stored in the graph.
        original_tokens = estimate_file_tokens(root, changed_files)
        abs_files = _resolve_graph_file_paths(store, root, changed_files)
        result = store.get_impact_radius(
            abs_files, max_depth=max_depth, max_nodes=offset + max_results
        )

        impact_scores = result.get("impact_scores", {})
        changed_dicts = [node_to_dict(n) for n in result["changed_nodes"]]
        impacted_dicts = []
        for node in result["impacted_nodes"][offset:]:
            node_dict = node_to_dict(node)
            score = impact_scores.get(node.qualified_name)
            if score is not None:
                node_dict["impact_score"] = score
            impacted_dicts.append(node_dict)
        edge_dicts = [edge_to_dict(e) for e in result["edges"]]
        total_impacted = result["total_impacted"]
        next_offset = _next_offset(offset, len(impacted_dicts), total_impacted)
        truncated = next_offset is not None

        summary_parts = [
            f"Blast radius for {len(changed_files)} changed file(s):",
            f"  - {len(changed_dicts)} nodes directly changed",
            f"  - {len(impacted_dicts)} nodes impacted (within {max_depth} hops)",
            f"  - {len(result['impacted_files'])} additional files affected",
        ]
        if truncated or offset:
            summary_parts.append(
                f"  - Showing {len(impacted_dicts)} of {total_impacted} impacted"
                f" nodes from offset {offset}"
            )

        # "Nothing is impacted" and "nothing about these files is indexed"
        # look identical to a reader without this marker.
        confidence = None
        if not impacted_dicts:
            changed_language = next(
                (n.language for n in result["changed_nodes"] if n.language), None,
            )
            confidence = empty_impact_confidence(
                store, root, changed_files, abs_files, changed_language,
            )

        if detail_level == "minimal":
            impacted_count = len(impacted_dicts)
            if impacted_count > 20:
                risk = "high"
            elif impacted_count > 5:
                risk = "medium"
            else:
                risk = "low"
            key_entities = [
                n["name"] for n in impacted_dicts[:5]
            ]
            minimal_response = {
                "status": "ok",
                "summary": "\n".join(summary_parts),
                "risk": risk,
                "impacted_file_count": len(result["impacted_files"]),
                "key_entities": key_entities,
                "truncated": truncated,
                "nodes_omitted": max(0, total_impacted - offset - len(impacted_dicts)),
                "next_offset": next_offset,
            }
            if confidence:
                minimal_response["confidence"] = confidence
            attach_context_savings(minimal_response, original_tokens=original_tokens)
            return minimal_response

        response: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "changed_files": changed_files,
            "changed_nodes": changed_dicts,
            "impacted_nodes": impacted_dicts,
            "impacted_files": result["impacted_files"],
            "edges": edge_dicts,
            "truncated": truncated,
            "total_impacted": total_impacted,
            "nodes_omitted": max(0, total_impacted - offset - len(impacted_dicts)),
            "next_offset": next_offset,
        }
        if confidence:
            response["confidence"] = confidence
        attach_context_savings(response, original_tokens=original_tokens)
        return response
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 3: query_graph
# ---------------------------------------------------------------------------


def query_graph(
    pattern: str,
    target: str,
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_results: int = 100,
    _store: tuple[GraphStore, Path] | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Run a predefined graph query.

    Args:
        pattern: Query pattern. One of: callers_of, references_to, callees_of,
                 imports_of, importers_of, children_of, tests_for, inheritors_of,
                 triggers_of, triggered_by, publishers_of, listeners_of,
                 handlers_of, endpoints_for, consumers_of, file_summary, and the
                 cross-stack pages_for, requests_to, included_by, views_of,
                 forwards_to, maps_to, binds_to, styles_of.
        target: The node name, qualified name, or file path to query about
            (a URL such as ``/order/Order.action`` for requests_to/pages_for).
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" (full output) or "minimal" (summary only).
        max_results: Maximum results to return. Minimal mode additionally caps
            visible results at five and reports the exact omitted count.
        _store: An open ``(store, root)`` owned by the caller (``batch_query``);
            it is neither readiness-checked nor closed here.
        offset: Results to skip before the returned page (default 0); pass
            the previous response's ``next_offset``.

    Returns:
        Matching nodes and their aligned edges, with total and omitted counts
        and ``next_offset`` (None on the last page).
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")
    _validate_offset(offset)

    if pattern not in _QUERY_PATTERNS:
        if target in _QUERY_PATTERNS:
            swapped = query_graph(
                target, pattern, repo_root, detail_level, max_results, _store,
                offset,
            )
            swapped["argument_swap"] = True
            return swapped
        return _error(
            "unknown_pattern",
            f"Unknown pattern '{pattern}'. Available: {list(_QUERY_PATTERNS.keys())}",
        )

    if _store is None:
        missing = missing_graph_response(_resolve_root(repo_root))
        if missing is not None:
            return missing
    store, root = _store or _get_store(repo_root)
    try:
        response_limit = min(max_results, 5) if detail_level == "minimal" else max_results
        results: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = []
        total_results = 0

        def add_result(result: dict[str, Any], edge: Any | None = None) -> None:
            """Count every logical result but retain only the requested page."""
            nonlocal total_results
            total_results += 1
            if total_results <= offset or len(results) >= response_limit:
                return
            results.append(result)
            if edge is not None:
                edges_out.append(edge_to_dict(edge))

        # For callers_of, skip common builtins early (bare names only)
        # "Who calls .map()?" returns hundreds of useless hits.
        # Qualified names (e.g. "utils.py::map") bypass this filter.
        if (
            pattern == "callers_of"
            and target in _BUILTIN_CALL_NAMES
            and "::" not in target
            and is_builtin_call_name(target, {
                row[0] for row in store._conn.execute(
                    "SELECT DISTINCT lower(language) FROM nodes "
                    "WHERE name = ? AND kind IN ('Function', 'Test')",
                    (target,),
                )
            })
        ):
            return {
                "status": "ok", "pattern": pattern, "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": (
                    f"'{target}' is a common builtin "
                    "— callers_of skipped to avoid noise."
                ),
                "result_count": 0,
                "results_omitted": 0,
                "next_offset": None,
                "results": [], "edges": [],
            }

        # Resolve target - try as-is, then as absolute path, then search.
        # file_summary targets are paths, so skip broad node search.
        node = None
        raw_config_target = pattern == "consumers_of" and "::" not in target
        raw_url_target = pattern in _ROUTE_PATTERNS and _looks_like_url(target)
        if pattern != "file_summary" and not raw_config_target and not raw_url_target:
            node = store.get_node(target)
            abs_target = normalize_file_path(root / target)
            if not node:
                node = store.get_node(abs_target)
            if not node:
                overloads = _overload_nodes(store, target) or _overload_nodes(store, abs_target)
                java_candidates = (
                    overloads if overloads else _java_fqn_candidates(store, target)
                )
                candidates = (
                    java_candidates
                    if java_candidates is not None
                    else store.search_nodes(target, limit=20)
                )
                if pattern in _CROSS_STACK_PATTERNS and "::" not in target:
                    exact = [c for c in candidates if c.name == target]
                    if len(exact) == 1:
                        candidates = exact
                if pattern == "inheritors_of" and "::" not in target:
                    exact_type_candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.name == target
                        and candidate.kind
                        in {"Class", "Interface", "Type", "Struct", "Enum", "Trait"}
                    ]
                    if exact_type_candidates:
                        candidates = exact_type_candidates
                if len(candidates) == 1:
                    node = candidates[0]
                    target = node.qualified_name
                elif len(candidates) > 1:
                    candidate_count = (
                        len(candidates)
                        if java_candidates is not None
                        else store.count_search_nodes(target)
                    )
                    ranked = _rank_disambiguation_candidates(candidates, target)
                    bare_name = (
                        candidates[0].name if overloads
                        else target.rsplit(".", 1)[-1] if java_candidates is not None
                        else target
                    )
                    if (
                        pattern in _MERGE_PATTERNS
                        and len(candidates) <= _MERGE_MAX_CANDIDATES
                        and candidate_count <= len(candidates)
                        and all(c.name == bare_name for c in candidates)
                    ):
                        return _merge_candidates(
                            pattern, target, candidates, ranked, detail_level,
                            max_results, response_limit, (store, root), offset,
                        )
                    return {
                        "status": "ambiguous",
                        "summary": (
                            f"'{target}' matches {candidate_count} node(s). "
                            "Re-run with a qualified_name from disambiguation."
                        ),
                        # Preserve the established key while adding the clearer
                        # agent-facing name introduced by #458.
                        "candidates": ranked,
                        "disambiguation": ranked,
                        "candidate_count": candidate_count,
                        "candidates_truncated": candidate_count > len(candidates),
                        "hint": (
                            "Use a qualified_name from disambiguation as the "
                            "target parameter."
                        ),
                    }

        if (
            not node
            and pattern not in ("consumers_of", "file_summary")
            and not raw_url_target
        ):
            # This branch, not the empty-result path below, is where an
            # unresolved target actually lands for most patterns, so the
            # not-indexed marker has to be attached here too.
            unresolved: dict[str, Any] = {
                "status": "not_found",
                "summary": f"No node found matching '{target}'.",
            }
            unresolved_note = empty_query_confidence(store, root, pattern, target, None)
            if unresolved_note:
                unresolved["confidence"] = unresolved_note
            return unresolved

        qn = node.qualified_name if node else target

        if pattern == "callers_of":
            seen_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if e.kind == "CALLS":
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            add_result(node_to_dict(caller), e)
            # Fallback: CALLS edges store unqualified target names
            # (e.g. "generateTestCode") while qn is fully qualified
            # (e.g. "file.ts::generateTestCode"). Search by plain name too.
            if node:
                cpp_overload_count = (
                    store.count_nodes_by_name(
                        node.name,
                        language="cpp",
                        kinds=("Function", "Test"),
                    )
                    if node.language == "cpp"
                    else 0
                )
                for e in store.iter_edges_by_target_name(
                    node.name,
                    language=node.language or None,
                ):
                    # A C++ overload set deliberately keeps the target bare.
                    # Its candidates support disambiguation, but do not prove
                    # that any one exact overload was called.
                    if (
                        "ambiguous_targets" in e.extra
                        or "unresolved_targets" in e.extra
                        or (node.language == "cpp" and e.extra.get("receiver"))
                        or (
                            node.language in _TYPED_RECEIVER_LANGUAGES
                            and _is_member_call(e.extra)
                        )
                    ):
                        continue
                    # Neither a bare name proves which overload was called.
                    if cpp_overload_count > 1 or (
                        node.language == "java" and node.qualified_name.endswith(")")
                    ):
                        continue
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            caller_result = node_to_dict(caller)
                            caller_result["target_resolution"] = "unresolved"
                            add_result(caller_result, e)

        elif pattern == "references_to":
            seen_reference_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if (
                    e.kind != "REFERENCES"
                    or e.source_qualified in seen_reference_sources
                ):
                    continue
                source = store.get_node(e.source_qualified)
                if source:
                    seen_reference_sources.add(e.source_qualified)
                    add_result(node_to_dict(source), e)

        elif pattern == "callees_of":
            seen_targets: set[str] = set()
            for e in store.iter_edges_by_source(qn):
                if e.kind == "CALLS":
                    if e.target_qualified not in seen_targets:
                        seen_targets.add(e.target_qualified)
                        callee = store.get_node(e.target_qualified)
                        if callee:
                            add_result(node_to_dict(callee), e)
                        elif (
                            isinstance(e.extra.get("ambiguous_targets"), list)
                            or isinstance(e.extra.get("unresolved_targets"), list)
                            or "::" not in e.target_qualified
                            or (node is not None and node.language == "cpp")
                        ):
                            unresolved = (
                                e.extra.get("ambiguous_targets")
                                or e.extra.get("unresolved_targets")
                            )
                            result: dict[str, Any] = {
                                "kind": "Function",
                                "name": e.target_qualified,
                                "qualified_name": e.target_qualified,
                            }
                            if isinstance(unresolved, list):
                                resolution = (
                                    "ambiguous"
                                    if e.extra.get("ambiguous_targets")
                                    else "unresolved"
                                )
                                result["resolution"] = resolution
                                result["candidates"] = [
                                    _sanitize_name(candidate)
                                    for candidate in unresolved[:20]
                                    if isinstance(candidate, str)
                                ]
                                candidate_count = e.extra.get(
                                    f"{resolution}_target_count",
                                )
                                if not isinstance(candidate_count, int):
                                    candidate_count = len(unresolved)
                                result["candidate_count"] = candidate_count
                                result["candidates_truncated"] = bool(
                                    e.extra.get(
                                        f"{resolution}_targets_truncated",
                                    )
                                    or candidate_count > len(result["candidates"])
                                )
                            add_result(result, e)

        elif pattern == "imports_of":
            for e in store.iter_edges_by_source(qn):
                if e.kind == "IMPORTS_FROM":
                    add_result({"import_target": e.target_qualified}, e)

        elif pattern == "importers_of":
            # Find edges where target matches this file.
            # Use resolve() to canonicalize the path, matching how
            # _resolve_module_to_file stores edge targets.
            abs_target = (
                str((root / target).resolve()) if node is None
                else node.file_path
            )
            seen_importers: set[str] = set()
            for e in store.iter_edges_by_target(abs_target):
                if e.kind == "IMPORTS_FROM":
                    if e.source_qualified in seen_importers:
                        continue
                    seen_importers.add(e.source_qualified)
                    add_result({
                        "importer": e.source_qualified,
                        "file": e.file_path,
                    }, e)
            # C# fallback: `using X.Y;` directives produce IMPORTS_FROM edges
            # whose target is the raw namespace string, not a file path, so
            # the path lookup above misses them. Resolve the target file's
            # declared namespace(s) and also search edges by namespace.
            # See: #310
            if node is not None and node.language == "csharp":
                declared_ns: list[str] = []
                for n in store.iter_nodes_by_file(node.file_path):
                    if n.kind == "File":
                        declared_ns = list(
                            n.extra.get("csharp_namespaces", []) or []
                        )
                        break
                for ns in declared_ns:
                    for e in store.iter_edges_by_target(ns):
                        if e.kind != "IMPORTS_FROM":
                            continue
                        if e.source_qualified in seen_importers:
                            continue
                        seen_importers.add(e.source_qualified)
                        add_result({
                            "importer": e.source_qualified,
                            "file": e.file_path,
                        }, e)

        elif pattern == "children_of":
            for e in store.iter_edges_by_source(qn):
                if e.kind == "CONTAINS":
                    child = store.get_node(e.target_qualified)
                    if child:
                        add_result(node_to_dict(child))

        elif pattern == "tests_for":
            # Keep the normal sanitized node response while adding the
            # direct/indirect marker returned by the bounded store lookup.
            seen: set[str] = set()
            for match in store.get_transitive_tests(qn):
                test_qn = match.get("qualified_name")
                if not isinstance(test_qn, str) or test_qn in seen:
                    continue
                test = store.get_node(test_qn)
                if test:
                    result = node_to_dict(test)
                    result["indirect"] = bool(match.get("indirect", False))
                    add_result(result)
                    seen.add(test_qn)
            # Also search by naming convention
            name = node.name if node else target
            cpp_overload_set = bool(
                node
                and node.language == "cpp"
                and store.count_nodes_by_name(
                    node.name,
                    language="cpp",
                    kinds=("Function", "Test"),
                ) > 1
            )
            test_nodes = []
            if not cpp_overload_set:
                test_nodes = store.search_nodes(f"test_{name}", limit=10)
                test_nodes += store.search_nodes(f"Test{name}", limit=10)
            for t in test_nodes:
                if t.qualified_name not in seen and t.is_test:
                    result = node_to_dict(t)
                    result["indirect"] = False
                    result["inferred_by"] = "naming_convention"
                    add_result(result)
                    seen.add(t.qualified_name)

        elif pattern == "inheritors_of":
            for e in store.iter_edges_by_target(qn):
                if e.kind in ("INHERITS", "IMPLEMENTS"):
                    child = store.get_node(e.source_qualified)
                    if child:
                        add_result(node_to_dict(child), e)
            # Fallback: INHERITS/IMPLEMENTS edges store unqualified base names
            # (e.g. "Animal") while qn is fully qualified
            # (e.g. "sample.dart::Animal"). Search by plain name too. See: #87
            if total_results == 0 and node:
                for kind in ("INHERITS", "IMPLEMENTS"):
                    for e in store.iter_edges_by_target_name(
                        node.name, kind=kind, language=node.language or None,
                    ):
                        child = store.get_node(e.source_qualified)
                        if child:
                            add_result(node_to_dict(child), e)

        elif pattern == "triggers_of":
            for edge in store.get_edges_by_source(qn):
                if edge.kind != "TRIGGERS":
                    continue
                triggered = store.get_node(edge.target_qualified)
                if triggered:
                    add_result(node_to_dict(triggered), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "triggered_by":
            for edge in store.get_edges_by_target(qn):
                if edge.kind != "TRIGGERS":
                    continue
                trigger = store.get_node(edge.source_qualified)
                if trigger:
                    add_result(node_to_dict(trigger), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern in ("publishers_of", "listeners_of"):
            edge_kind = "PUBLISHES" if pattern == "publishers_of" else "HANDLES"
            for edge in store.get_edges_by_target(qn):
                if edge.kind != edge_kind:
                    continue
                source = store.get_node(edge.source_qualified)
                if source:
                    add_result(node_to_dict(source), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "handlers_of":
            for edge in store.get_edges_by_target(qn):
                if edge.kind != "HANDLES":
                    continue
                handler = store.get_node(edge.source_qualified)
                if handler:
                    add_result(node_to_dict(handler), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "endpoints_for":
            for edge in store.get_edges_by_source(qn):
                if edge.kind != "HANDLES":
                    continue
                endpoint = store.get_node(edge.target_qualified)
                if endpoint and endpoint.kind == "Endpoint":
                    add_result(node_to_dict(endpoint), edge)
                elif endpoint is None:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "consumers_of":
            raw_key = node.name if node else target.removeprefix("config:")
            raw_key = raw_key.removesuffix(".*")
            key = normalize_spring_config_key(raw_key)
            seen_config_sources: set[str] = set()
            for edge in store.get_config_consumers(key):
                consumer = store.get_node(edge.source_qualified)
                if consumer and consumer.qualified_name not in seen_config_sources:
                    add_result(node_to_dict(consumer), edge)
                    seen_config_sources.add(consumer.qualified_name)
                elif consumer is None:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "file_summary":
            graph_paths = _resolve_graph_file_paths(store, root, [target])
            for graph_path in graph_paths:
                for n in store.iter_nodes_by_file(graph_path):
                    add_result(node_to_dict(n))

        elif pattern in _CROSS_STACK_PATTERNS:
            _cross_stack_results(store, pattern, qn, add_result)

        results_omitted = max(0, total_results - offset - len(results))
        next_offset = _next_offset(offset, len(results), total_results)
        summary = (
            f"Found {total_results} result(s) "
            f"for {pattern}('{target}')"
        )
        if results_omitted or offset:
            summary += (
                f" — showing {len(results)} from offset {offset}, "
                f"{results_omitted} omitted"
            )

        # A zero here is the dangerous direction: agents read it as "none
        # exist" and either conclude wrongly or fall back to grepping the
        # repository. One capped sentence prevents both, and is attached only
        # when the result set is empty so non-empty responses are unchanged.
        confidence = (
            empty_query_confidence(store, root, pattern, target, node)
            if total_results == 0
            else None
        )

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "indirect")
                    if k in r
                }
                for r in results
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "pattern": pattern,
                "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": summary,
                "result_count": total_results,
                "results_omitted": results_omitted,
                "next_offset": next_offset,
                "results": minimal_results,
            }
            if confidence:
                minimal_response["confidence"] = confidence
            return minimal_response

        response: dict[str, Any] = {
            "status": "ok",
            "pattern": pattern,
            "target": target,
            "description": _QUERY_PATTERNS[pattern],
            "summary": summary,
            "result_count": total_results,
            "results_omitted": results_omitted,
            "next_offset": next_offset,
            "results": results,
            "edges": edges_out,
        }
        if confidence:
            response["confidence"] = confidence
        return response
    finally:
        if _store is None:
            store.close()


# ---------------------------------------------------------------------------
# batch_query: many query_graph calls over one open store
# ---------------------------------------------------------------------------

_BATCH_MAX_QUERIES = 25
_BATCH_MAX_CANDIDATES = 8
_BATCH_ITEM_RESULTS = 500


def _compact_label(result: dict[str, Any], root: Path) -> str:
    """``Parent.name:line``; the short qualified name when there is no parent."""
    name = result.get("name")
    parent = result.get("parent_name")
    if parent and name:
        label = f"{parent}.{name}"
    else:
        qn = (
            result.get("qualified_name") or result.get("importer")
            or result.get("import_target") or name or ""
        )
        label = _short(str(qn), root)
    line = result.get("line_start")
    return f"{label}:{line}" if line else label


def _compact_item(
    pattern: str,
    target: str,
    result: dict[str, Any],
    store: GraphStore,
    root: Path,
    limit: int,
) -> dict[str, Any]:
    """Shrink one query_graph response to the fields an agent acts on."""
    item: dict[str, Any] = {
        "pattern": result.get("pattern", pattern),
        "target": target,
        "status": result.get("status", "error"),
    }
    if result.get("argument_swap"):
        item["argument_swap"] = True
    if item["status"] == "ambiguous":
        item["candidate_count"] = result.get("candidate_count")
        item["candidates"] = [
            _compact_label(c, root)
            for c in result.get("candidates", [])[:_BATCH_MAX_CANDIDATES]
        ]
        return item
    if item["status"] != "ok":
        item["summary"] = result.get("summary") or result.get("error")
        return item

    results = result.get("results", [])
    if "resolution" in result:
        item["resolution"] = result["resolution"]
        item["groups"] = [
            {"resolved": _compact_label(g, root), "result_count": g["result_count"]}
            for g in result["groups"]
        ]
        self_qns = {g["qualified_name"] for g in result["groups"]}
    else:
        resolved = result.get("target", target)
        node = store.get_node(resolved) or store.get_node(
            normalize_file_path(root / resolved),
        )
        self_qns = {_sanitize_name(node.qualified_name)} if node else set()
        if node:
            item["resolved"] = _compact_label(node_to_dict(node), root)
    if result.get("results_omitted"):
        item["truncated"] = True

    if item["pattern"] == "tests_for":
        item["tests"] = result.get("result_count", len(results))
        item["test_names"] = [_compact_label(r, root) for r in results[:limit]]
        return item

    def is_test(r: dict[str, Any]) -> bool:
        # helpers in test files (stubs, fixtures) carry is_test=False
        path = str(r.get("file_path") or "")
        try:
            path = Path(path).relative_to(root).as_posix()
        except ValueError:
            pass
        return bool(r.get("is_test")) or _is_test_file(path)

    tests = [r for r in results if is_test(r)]
    prod = [
        r for r in results
        if not is_test(r) and r.get("qualified_name") not in self_qns
    ]
    item["prod"] = [_compact_label(r, root) for r in prod[:limit]]
    item["prod_count"] = len(prod)
    item["tests"] = len(tests)
    item["self_call"] = any(r.get("qualified_name") in self_qns for r in results)
    return item


def _open_batch_root(repo_root: str | None) -> tuple[GraphStore, Path] | dict[str, Any]:
    """An open store for *repo_root*, or the error/not_ready answer for it."""
    try:
        root = _resolve_root(repo_root)
    except ValueError as exc:
        return _error("invalid_repo_root", str(exc))
    missing = missing_graph_response(root)
    if missing is not None:
        return missing
    return _get_store(str(root))


def batch_query(
    queries: list[dict[str, Any]],
    repo_root: str | None = None,
    max_results_per_query: int = 10,
) -> dict[str, Any]:
    """Run several ``query_graph`` lookups over one open graph store per root.

    Duplicate ``(pattern, target, repo_root)`` entries run once; beyond
    ``_BATCH_MAX_QUERIES`` the rest are counted in ``queries_dropped``. Each
    item carries its own status, so one bad item never fails the batch. An
    item may name its own ``repo_root``; an invalid or unbuilt root turns
    into that item's error, never a raise.
    """
    if isinstance(max_results_per_query, bool) or max_results_per_query < 1:
        raise ValueError("max_results_per_query must be an integer >= 1")

    unique: list[tuple[Any, Any, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for spec in queries:
        pattern = spec.get("pattern") if isinstance(spec, dict) else None
        target = spec.get("target") if isinstance(spec, dict) else None
        entry_root = (spec.get("repo_root") if isinstance(spec, dict) else None) or repo_root
        key = (repr(pattern), repr(target), repr(entry_root))
        if key not in seen:
            seen.add(key)
            unique.append((pattern, target, entry_root))
    queries_dropped = max(0, len(unique) - _BATCH_MAX_QUERIES)
    kept = unique[:_BATCH_MAX_QUERIES]

    stores: dict[Any, tuple[GraphStore, Path] | dict[str, Any]] = {}
    # One shared root keeps the old whole-batch not_ready answer.
    if all(entry_root == repo_root for _, _, entry_root in kept):
        shared = stores[repo_root] = _open_batch_root(repo_root)
        if isinstance(shared, dict) and shared.get("status") == "not_ready":
            return shared

    items: list[dict[str, Any]] = []
    try:
        for pattern, target, entry_root in kept:
            if not isinstance(pattern, str) or not isinstance(target, str):
                items.append({
                    "pattern": pattern, "target": target, "status": "error",
                    "error_code": "invalid_argument",
                    "summary": "Each query needs string 'pattern' and 'target'.",
                })
                continue
            if entry_root not in stores:
                stores[entry_root] = _open_batch_root(entry_root)
            handle = stores[entry_root]
            if isinstance(handle, dict):
                item = {"pattern": pattern, "target": target,
                        "status": handle.get("status", "error"),
                        "summary": handle.get("message") or handle.get("summary")}
                for field in ("error_code", "reason"):
                    if field in handle:
                        item[field] = handle[field]
                if entry_root != repo_root:
                    item["repo_root"] = entry_root
                items.append(item)
                continue
            store, root = handle
            try:
                result = query_graph(
                    pattern, target, max_results=_BATCH_ITEM_RESULTS,
                    _store=(store, root),
                )
            except ValueError as exc:
                result = _error("invalid_argument", str(exc))
            item = _compact_item(pattern, target, result, store, root, max_results_per_query)
            if entry_root != repo_root:
                item["repo_root"] = entry_root
            items.append(item)
    finally:
        for handle in stores.values():
            if not isinstance(handle, dict):
                handle[0].close()

    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = f"{len(items)} queries: " + ", ".join(
        f"{n} {status}" for status, n in counts.items()
    )
    if queries_dropped:
        summary += f"; {queries_dropped} dropped over the {_BATCH_MAX_QUERIES} cap"
    return {
        "status": "ok",
        "summary": summary,
        "queries_dropped": queries_dropped,
        "results": items,
    }


# ---------------------------------------------------------------------------
# Tool 5: semantic_search_nodes
# ---------------------------------------------------------------------------


def semantic_search_nodes(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    repo_root: str | None = None,
    context_files: list[str] | None = None,
    model: str | None = None,
    provider: str | None = None,
    detail_level: str = "standard",
    offset: int = 0,
) -> dict[str, Any]:
    """Search for nodes by name, keyword, or semantic similarity.

    Uses hybrid search (FTS5 BM25 + vector embeddings merged via Reciprocal
    Rank Fusion) as the primary search path, with graceful fallback to
    keyword matching.

    Args:
        query: Search string to match against node names and qualified names.
        kind: Optional filter by node kind (File, Class, Function, Type, Test).
        limit: Maximum results to return (default: 20).
        repo_root: Repository root path. Auto-detected if omitted.
        context_files: Optional list of file paths. Nodes in these files
            receive a relevance boost.
        detail_level: "standard" (full output) or "minimal" (summary only).
        offset: Ranked results to skip (default 0); pass the previous
            response's ``next_offset`` for the next page.

    Returns:
        Ranked list of matching nodes, ``next_offset`` (None when no further
        result exists), the ``search_mode`` that produced them,
        ``embeddings_state`` (off|ready|stale|unavailable) and a ``warning``
        whenever semantic search was wanted but keyword search answered alone.
    """
    _validate_offset(offset)
    store, root = _get_store(repo_root)
    try:
        mode_out: list[str] = []
        info: dict[str, Any] = {}
        # One extra row tells whether another page exists.
        ranked = hybrid_search(
            store, query, kind=kind, limit=offset + limit + 1,
            context_files=context_files, model=model, provider=provider,
            _out_mode=mode_out, repo_root=str(root), _out_info=info,
        )
        results = ranked[offset:offset + limit]
        next_offset = offset + len(results) if len(ranked) > offset + limit else None

        search_mode = mode_out[0] if mode_out else "keyword"
        embeddings_state = info.get("embeddings_state", "off")
        warning = info.get("warning")

        summary = f"Found {len(results)} node(s) matching '{query}'" + (
            f" (kind={kind})" if kind else ""
        )

        # Zero hits can mean "no such symbol" or "never indexed"/"stale index";
        # only the marker distinguishes them.
        confidence = (
            empty_search_confidence(store, root, query) if not results else None
        )

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "score")
                    if k in r
                }
                for r in results[:limit]
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "query": query,
                "search_mode": search_mode,
                "embeddings_state": embeddings_state,
                "summary": summary,
                "results": minimal_results,
                "result_count": len(results),
                "results_omitted": max(0, len(results) - len(minimal_results)),
                "next_offset": next_offset,
            }
            if warning:
                minimal_response["warning"] = warning
            if confidence:
                minimal_response["confidence"] = confidence
            return minimal_response

        result: dict[str, object] = {
            "status": "ok",
            "query": query,
            "search_mode": search_mode,
            "embeddings_state": embeddings_state,
            "summary": summary,
            "results": results,
            "next_offset": next_offset,
        }
        if warning:
            result["warning"] = warning
        if confidence:
            result["confidence"] = confidence
        result["_hints"] = generate_hints(
            "semantic_search_nodes", result, get_session()
        )
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 6: list_graph_stats
# ---------------------------------------------------------------------------


def list_graph_stats(repo_root: str | None = None) -> dict[str, Any]:
    """Get aggregate statistics about the knowledge graph.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Total nodes, edges, breakdown by kind, languages, and last update time.
    """
    store, root = _get_store(repo_root)
    try:
        stats = store.get_stats()

        summary_parts = [
            f"Graph statistics for {root.name}:",
            f"  Files: {stats.files_count}",
            f"  Total nodes: {stats.total_nodes}",
            f"  Total edges: {stats.total_edges}",
            f"  Languages: {', '.join(stats.languages) if stats.languages else 'none'}",
            f"  Last updated: {stats.last_updated or 'never'}",
            "",
            "Nodes by kind:",
        ]
        for kind, count in sorted(stats.nodes_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")
        summary_parts.append("")
        summary_parts.append("Edges by kind:")
        for kind, count in sorted(stats.edges_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")

        # Add embedding info if available
        emb_store = EmbeddingStore(get_db_path(root))
        try:
            emb_count = emb_store.count()
            summary_parts.append("")
            summary_parts.append(f"Embeddings: {emb_count} nodes embedded")
            if not emb_store.available:
                summary_parts.append(
                    "  (install sentence-transformers for semantic search)"
                )
        finally:
            emb_store.close()

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_nodes": stats.total_nodes,
            "total_edges": stats.total_edges,
            "nodes_by_kind": stats.nodes_by_kind,
            "edges_by_kind": stats.edges_by_kind,
            "languages": stats.languages,
            "files_count": stats.files_count,
            "last_updated": stats.last_updated,
            "embeddings_count": emb_count,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 9: find_large_functions
# ---------------------------------------------------------------------------


def find_large_functions(
    min_lines: int = 50,
    kind: str | None = None,
    file_path_pattern: str | None = None,
    limit: int = 50,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for identifying decomposition targets, code-quality audits,
    and enforcing size limits during code review.

    Args:
        min_lines: Minimum line count to flag (default: 50).
        kind: Filter by node kind: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results (default: 50).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Oversized nodes with line counts, ordered largest first.
    """
    store, root = _get_store(repo_root)
    try:
        nodes = store.get_nodes_by_size(
            min_lines=min_lines,
            kind=kind,
            file_path_pattern=file_path_pattern,
            limit=limit,
        )

        results = []
        for n in nodes:
            d = node_to_dict(n)
            d["line_count"] = (
                (n.line_end - n.line_start + 1)
                if n.line_start and n.line_end
                else 0
            )
            # Make file_path relative for readability
            try:
                d["relative_path"] = str(Path(n.file_path).relative_to(root))
            except ValueError:
                d["relative_path"] = n.file_path
            results.append(d)

        summary_parts = [
            f"Found {len(results)} node(s) with >= {min_lines} lines"
            + (f" (kind={kind})" if kind else "")
            + (f" matching '{file_path_pattern}'" if file_path_pattern else "")
            + ":",
        ]
        for r in results[:10]:
            summary_parts.append(
                f"  {r['line_count']:>4} lines | {r['kind']:>8} | "
                f"{r['name']} ({r['relative_path']}:{r['line_start']})"
            )
        if len(results) > 10:
            summary_parts.append(f"  ... and {len(results) - 10} more")

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_found": len(results),
            "min_lines": min_lines,
            "results": results,
        }
    finally:
        store.close()


# -------------------------------------------------------------------
# traverse_graph: free-form BFS / DFS traversal
# -------------------------------------------------------------------


def traverse_graph_func(
    query: str,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """BFS/DFS traversal from best-matching node.

    Args:
        query: Search string to find the starting node.
        mode: "bfs" (breadth-first) or "dfs" (depth-first).
        depth: Max traversal depth (1-6). Default: 3.
        token_budget: Approximate token limit for results.
        repo_root: Repository root path.
    """
    if mode not in ("bfs", "dfs"):
        return _error("invalid_argument", f"mode must be 'bfs' or 'dfs', got {mode!r}")
    store, root = _get_store(repo_root)
    try:
        results = hybrid_search(store, query, limit=1)
        if not results:
            return _error(
                "not_found", f"No node matching '{query}'", nodes=[], traversal=[],
            )

        start_qn = results[0]["qualified_name"]
        depth = max(1, min(depth, 6))

        # BFS / DFS traversal
        visited: dict[str, int] = {}  # qn -> depth
        queue: list[tuple[str, int]] = [
            (start_qn, 0),
        ]
        traversal: list[dict] = []
        approx_tokens = 0

        while queue:
            if mode == "bfs":
                current_qn, cur_depth = queue.pop(0)
            else:
                current_qn, cur_depth = queue.pop()

            if current_qn in visited:
                continue
            if cur_depth > depth:
                continue

            visited[current_qn] = cur_depth
            node = store.get_node(current_qn)
            if not node:
                continue

            entry = {
                "name": _sanitize_name(node.name),
                "qualified_name": node.qualified_name,
                "kind": node.kind,
                "file": node.file_path,
                "depth": cur_depth,
            }
            approx_tokens += len(str(entry)) // 4
            if approx_tokens > token_budget:
                break

            traversal.append(entry)

            # Get neighbours
            out_edges = store.get_edges_by_source(
                current_qn
            )
            in_edges = store.get_edges_by_target(
                current_qn
            )
            for e in out_edges:
                tgt = e.target_qualified
                if tgt not in visited:
                    queue.append((tgt, cur_depth + 1))
            for e in in_edges:
                src = e.source_qualified
                if src not in visited:
                    queue.append((src, cur_depth + 1))

        return {
            "status": "ok",
            "start_node": start_qn,
            "mode": mode,
            "max_depth": depth,
            "nodes_visited": len(traversal),
            "traversal": traversal,
            "truncated": approx_tokens > token_budget,
            "next_tool_suggestions": [
                "query_graph_tool callers_of -- focused relationship query",
                "get_impact_radius_tool -- blast radius analysis",
            ],
        }
    finally:
        store.close()
