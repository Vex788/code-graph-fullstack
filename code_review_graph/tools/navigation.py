"""Token-efficient codebase navigation: orient, shortest_path_between, common_callers_of.

``orient`` is a one-call mini-map for a task string, meant to replace the
3-4 separate search calls an agent otherwise makes for basic orientation.
``shortest_path_between`` and ``common_callers_of`` are graph-BFS helpers for
"how does A reach B" and "what do A and B have in common" questions that
``query_graph``'s single-hop patterns cannot answer directly.
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

from ..graph import GraphStore
from ..search import hybrid_search
from ._common import _get_store

# ---------------------------------------------------------------------------
# Path shortening
# ---------------------------------------------------------------------------


def _repo_relative(path_str: str, root: Path) -> str:
    """Best-effort repo-relative form of a graph-identity path.

    Graph node ``file_path`` values (and the path component baked into every
    qualified name) are absolute, so leaving them unshortened puts the local
    checkout location in every response. Falls back to the original string
    when it does not sit under *root* (e.g. a graph built from elsewhere).
    """
    if not path_str:
        return path_str
    candidate = Path(path_str)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(root).as_posix()
        except ValueError:
            pass
    root_str = root.as_posix()
    if path_str.startswith(root_str + "/"):
        return path_str[len(root_str) + 1:]
    return path_str


def _short(qualified_or_path: str, root: Path) -> str:
    """Shorten the path component of a qualified name or bare path.

    Qualified names have the shape ``path::name`` or ``path::Class.name``;
    only the path component before ``::`` is a filesystem path, so the
    ``Class.name`` suffix (if any) passes through untouched.
    """
    path_part, sep, rest = qualified_or_path.partition("::")
    return f"{_repo_relative(path_part, root)}{sep}{rest}"


# ---------------------------------------------------------------------------
# Symbol resolution shared by shortest_path_between and common_callers_of
# ---------------------------------------------------------------------------


def _resolve_symbol(store: GraphStore, symbol: str) -> str | None:
    """Resolve a bare or qualified symbol to one graph ``qualified_name``.

    Tries an exact ``qualified_name`` match first, then an anchored suffix
    match on the two shapes this parser emits (see ``parser._qualify``):
    ``path::name`` and ``path::Class.name``. The match is anchored on a name
    boundary (``::`` or ``.``) and the end of the string, so a search for
    ``validateBeforePosting`` cannot match ``...Manager.xvalidateBeforePosting``
    — a plain substring search (``GraphStore.search_nodes``) would.
    """
    node = store.get_node(symbol)
    if node:
        return node.qualified_name
    escaped = symbol.replace("%", "/%").replace("_", "/_")
    rows = store._conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE kind != 'File' AND (qualified_name LIKE ? ESCAPE '/' "
        "OR qualified_name LIKE ? ESCAPE '/') "
        "ORDER BY kind = 'Class' DESC, line_start LIMIT 1",
        (f"%::{escaped}", f"%.{escaped}"),
    ).fetchall()
    return rows[0]["qualified_name"] if rows else None


def _methods_of(store: GraphStore, class_qname: str) -> list[str]:
    """Return qualified names of methods belonging to one class."""
    return [
        row["qualified_name"]
        for row in store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE parent_name IN "
            "(SELECT name FROM nodes WHERE qualified_name = ?) "
            "AND qualified_name LIKE ? LIMIT 200",
            (class_qname, class_qname + ".%"),
        )
    ]


# ---------------------------------------------------------------------------
# orient
# ---------------------------------------------------------------------------


def orient(query: str, repo_root: str | None = None) -> dict[str, Any]:
    """One-call codebase mini-map for a task string.

    Returns top functions/classes (hybrid FTS + vector search), top files,
    matching communities, and a node-count stat. Meant to replace 3-4
    separate search calls when an agent is orienting itself in a codebase.

    Args:
        query: Natural-language or symbol-ish task description.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    store, root = _get_store(repo_root)
    try:
        mode_out: list[str] = []
        nodes = hybrid_search(store, query, limit=12, _out_mode=mode_out)

        files: list[dict[str, Any]] = []
        funcs: list[dict[str, Any]] = []
        file_scores: dict[str, float] = {}
        for node in nodes:
            where = node.get("qualified_name") or node.get("file_path") or node.get("name", "")
            simple = node.get("name", "")
            score = node.get("score", 0.0)
            entry = {
                "name": simple, "kind": node.get("kind"),
                "where": _short(where, root), "score": round(score, 3),
            }
            if node.get("kind") == "File":
                files.append(entry)
                fp = _short(node.get("file_path") or where, root)
                file_scores[fp] = file_scores.get(fp, 0.0) + score
            else:
                # Demote tiny generic identifiers (get, run, ...) out of the
                # function list; they crowd out meaningful matches without
                # ever being what the caller meant.
                if len(simple) >= 6:
                    funcs.append(entry)
                fp = _short(node.get("file_path") or "", root)
                if fp:
                    file_scores[fp] = file_scores.get(fp, 0.0) + score

        # One batched community lookup instead of one query per node.
        qname_list = [n.get("qualified_name") for n in nodes if n.get("qualified_name")]
        community_ids = store.get_community_ids_by_qualified_names(qname_list)
        comm_votes: dict[int, int] = {}
        for qname in qname_list:
            community_id = community_ids.get(qname)
            if community_id is not None:
                comm_votes[community_id] = comm_votes.get(community_id, 0) + 1

        top_communities = sorted(comm_votes.items(), key=lambda kv: -kv[1])[:2]
        comm_rows: list[dict[str, Any]] = []
        if top_communities:
            ids = [community_id for community_id, _votes in top_communities]
            marks = ",".join("?" * len(ids))
            comm_rows = [
                {"id": row["id"], "name": row["name"], "size": row["size"]}
                for row in store._conn.execute(
                    f"SELECT id, name, size FROM communities WHERE id IN ({marks})",  # nosec B608
                    ids,
                )
            ]

        stats = store._conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]

        agg_files = sorted(file_scores.items(), key=lambda kv: -kv[1])[:3]
        top_files = files[:2]
        if len(top_files) < 3:
            seen = {f["where"] for f in top_files}
            for fp, agg_score in agg_files:
                if fp not in seen:
                    top_files.append({
                        "name": fp.rsplit("/", 1)[-1], "kind": "File",
                        "where": fp, "score": round(agg_score, 3),
                    })
                if len(top_files) >= 3:
                    break

        max_score = max((f.get("score", 0.0) for f in funcs), default=0.0)
        return {
            "status": "ok",
            "search_mode": mode_out[0] if mode_out else "none",
            # A gibberish query still returns near-zero-score neighbours from
            # embedding search rather than an empty result; surface that
            # instead of looking like a confident answer.
            "low_confidence": bool(
                mode_out and mode_out[0] in ("semantic", "hybrid") and max_score < 0.02
            ),
            "top_functions": funcs[:8],
            "top_files": top_files,
            "communities": comm_rows,
            "graph_nodes": stats,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# shortest_path_between
# ---------------------------------------------------------------------------


def shortest_path_between(
    symbol_a: str,
    symbol_b: str,
    mode: str = "call",
    max_depth: int = 6,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """BFS shortest paths between two symbols over CALLS/IMPORTS_FROM edges.

    Args:
        symbol_a: Start symbol (bare or qualified name).
        symbol_b: End symbol (bare or qualified name).
        mode: "call" (A calls ... B), "import", or "both".
        max_depth: Maximum hops to search. Default: 6.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Up to 3 paths as repo-relative qualified-name chains. Hub helpers
        (toString/equals/Logger/...) and the highest-degree nodes are
        skipped as intermediates so a path goes through meaningful code,
        not through whatever everything calls.
    """
    store, root = _get_store(repo_root)
    try:
        edge_kinds = {
            "call": ("CALLS",),
            "import": ("IMPORTS_FROM",),
            "both": ("CALLS", "IMPORTS_FROM"),
        }.get(mode, ("CALLS",))

        a_q = _resolve_symbol(store, symbol_a)
        if not a_q:
            return {"status": "ambiguous", "error": f"symbol_a not found: {symbol_a}"}
        b_q = _resolve_symbol(store, symbol_b)
        if not b_q:
            return {"status": "ambiguous", "error": f"symbol_b not found: {symbol_b}"}

        starts = [a_q] + _methods_of(store, a_q)
        goals = {b_q}
        b_row = store._conn.execute(
            "SELECT kind FROM nodes WHERE qualified_name = ?", (b_q,)
        ).fetchone()
        if b_row and b_row["kind"] == "Class":
            goals.update(_methods_of(store, b_q))

        adj: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
        placeholders = ",".join("?" * len(edge_kinds))
        rows = store._conn.execute(
            "SELECT source_qualified, target_qualified, kind FROM edges "  # nosec B608
            f"WHERE kind IN ({placeholders}) AND "
            "source_qualified LIKE '%::%' AND target_qualified LIKE '%::%'",
            edge_kinds,
        ).fetchall()
        for row in rows:
            adj[row["source_qualified"]].append((row["target_qualified"], row["kind"]))

        hub_words = (
            "toString", "valueOf", "equals", "hashCode", "getString",
            "isEmpty", "trim", "append", "Log.", "Logger",
        )
        # Degree-based hub damping: the highest-degree intermediates sit on
        # almost every path (loggers, base-class boilerplate, generic
        # utilities); exclude the top 0.5% of nodes by degree as
        # intermediate hops so a path answers "how does A reach B", not
        # "what does everything eventually touch".
        indeg: dict[str, int] = {}
        outdeg: dict[str, int] = {}
        for source, targets in adj.items():
            outdeg[source] = outdeg.get(source, 0) + len(targets)
            for target, _kind in targets:
                indeg[target] = indeg.get(target, 0) + 1
        degrees = {
            n: indeg.get(n, 0) + outdeg.get(n, 0) for n in set(indeg) | set(outdeg)
        }
        hub_cutoff = max(32, int(len(degrees) * 0.005))
        mega_hubs = {
            n for n, _degree in sorted(degrees.items(), key=lambda kv: -kv[1])[:hub_cutoff]
        }

        queue = collections.deque((s, [s]) for s in starts)
        visited = set(starts)
        paths: list[dict[str, Any]] = []
        seen_middle: set[tuple[str, ...]] = set()
        while queue and len(paths) < 3:
            current, path = queue.popleft()
            if len(path) > max_depth:
                continue
            for nxt, kind in adj.get(current, ()):
                if nxt in goals:
                    middle = tuple(path[1:])
                    if middle and middle in seen_middle:
                        continue
                    seen_middle.add(middle)
                    paths.append({
                        "path": [_short(p, root) for p in path + [nxt]],
                        "hops": len(path), "last_edge": kind,
                    })
                    if len(paths) >= 3:
                        break
                    continue
                nxt_short = nxt.rsplit("::", 1)[-1]
                if (
                    nxt in visited
                    or any(hub_word in nxt_short for hub_word in hub_words)
                    or (nxt in mega_hubs and nxt not in goals)
                ):
                    continue
                visited.add(nxt)
                queue.append((nxt, path + [nxt]))

        out: dict[str, Any] = {
            "status": "ok" if paths else "no_path", "paths": paths, "mode": mode,
        }
        if not paths:
            out["note"] = (
                f"no path within max_depth={max_depth} over "
                f"{', '.join(edge_kinds)} edges with hub filtering; "
                "raise max_depth or check mode"
            )
        return out
    finally:
        store.close()


# ---------------------------------------------------------------------------
# common_callers_of
# ---------------------------------------------------------------------------


def common_callers_of(
    symbol_a: str,
    symbol_b: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Callers shared by both symbols (intersection of CALLS callers).

    Args:
        symbol_a: First symbol (bare or qualified name).
        symbol_b: Second symbol (bare or qualified name).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Up to 10 repo-relative common callers, plus the untruncated count.
    """
    store, root = _get_store(repo_root)
    try:
        a_q = _resolve_symbol(store, symbol_a)
        b_q = _resolve_symbol(store, symbol_b)
        if not a_q or not b_q:
            unresolved = " ".join(
                symbol for symbol, resolved in
                ((symbol_a, a_q), (symbol_b, b_q)) if not resolved
            )
            return {"status": "ambiguous", "error": f"unresolved: {unresolved}"}

        def callers_of(qualified_name: str) -> set[str]:
            return {
                row["source_qualified"]
                for row in store._conn.execute(
                    "SELECT DISTINCT source_qualified FROM edges "
                    "WHERE kind='CALLS' AND target_qualified=?", (qualified_name,),
                )
            }

        common = callers_of(a_q) & callers_of(b_q)
        return {
            "status": "ok",
            "common_callers": [_short(c, root) for c in sorted(common)][:10],
            "count": len(common),
        }
    finally:
        store.close()
