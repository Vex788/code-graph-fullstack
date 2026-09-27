"""Hybrid search engine combining FTS5 (BM25) and vector embeddings.

Uses Reciprocal Rank Fusion (RRF) to merge results from full-text search
and semantic similarity, with query-aware kind boosting and context-file
boosting for relevance tuning.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

from .graph import GraphStore, _sanitize_name
from .migrations import (
    create_fts_triggers,
    drop_fts_triggers,
    rebuild_fts_in_transaction,
    split_name_tokens,
)
from .parser import normalize_file_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FTS5 index management
# ---------------------------------------------------------------------------


def disable_fts_triggers(conn: sqlite3.Connection) -> None:
    """Stop row-level FTS maintenance before a bulk load.

    The index is stale until :func:`rebuild_fts` runs, which also re-enables
    the triggers.
    """
    drop_fts_triggers(conn)


def enable_fts_triggers(conn: sqlite3.Connection) -> None:
    """Resume row-level FTS maintenance (only valid on an in-sync index)."""
    create_fts_triggers(conn)


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """Rebuild ``nodes_fts`` from ``nodes`` and re-enable its triggers.

    Runs inside the caller's transaction when one is open, otherwise in its
    own ``BEGIN IMMEDIATE`` transaction. Returns the number of indexed rows.
    """
    if conn.in_transaction:
        return rebuild_fts_in_transaction(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        count = rebuild_fts_in_transaction(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return count


def rebuild_fts_index(store: GraphStore) -> int:
    """Rebuild the FTS5 index from the nodes table.

    Drops the sync triggers, recreates ``nodes_fts`` from every row in
    ``nodes``, then re-creates the triggers, all in one transaction so a
    crash cannot leave the DB without an FTS table (#259).

    Returns:
        Number of rows indexed.
    """
    # NOTE: rebuild_fts_index uses store._conn directly because it manages
    # the FTS5 virtual table DDL, which is tightly coupled to SQLite internals.
    conn = store._conn
    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    count = rebuild_fts(conn)
    logger.info("FTS index rebuilt: %d rows indexed", count)
    return count


# ---------------------------------------------------------------------------
# Query kind boosting heuristics
# ---------------------------------------------------------------------------


_DOTTED_IDENT_RE = re.compile(r'\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b')
_SNAKE_IDENT_RE = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')
_PASCAL_IDENT_RE = re.compile(r'\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b')


def extract_query_identifiers(query: str) -> list[str]:
    """Pull out identifier-shaped tokens from anywhere in a query.

    Catches dotted forms (``Context.Next``), snake_case (``get_dependant``),
    and CamelCase (``APIRoute``) even when they're embedded in a natural-
    language sentence. Used to boost search hits whose qualified_name
    contains any of these tokens, so an LLM asking "Who advances the gin
    middleware chain via Context.Next" lands on ``Context.Next`` instead of
    the bare ``Context`` class.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pat in (_DOTTED_IDENT_RE, _SNAKE_IDENT_RE, _PASCAL_IDENT_RE):
        for match in pat.findall(query):
            lo = match.lower()
            if lo not in seen and len(lo) >= 3:
                seen.add(lo)
                found.append(lo)
    return found


def detect_query_kind_boost(query: str) -> dict[str, Any]:
    """Detect query patterns and return per-node boost multipliers.

    Heuristics:
    - PascalCase queries (e.g. ``MyClass``) boost Class/Type by 1.5x
    - snake_case queries (e.g. ``get_users``) boost Function by 1.5x
    - Queries containing ``.`` boost qualified name matches by 2.0x
    - Identifier-shaped tokens *anywhere* in the query (dotted, snake_case,
      CamelCase) boost results whose qualified_name contains them by 2.0x.
      See ``extract_query_identifiers``.

    Returns:
        Dict whose keys are either node kind strings (mapped to float
        multipliers) or one of the special keys ``_qualified``,
        ``_qualified_identifiers``.
    """
    boosts: dict[str, Any] = {}

    if not query or not query.strip():
        return boosts

    q = query.strip()

    # PascalCase: starts with uppercase, has at least one lowercase after
    if re.match(r'^[A-Z][a-z]', q) and not q.isupper():
        boosts["Class"] = 1.5
        boosts["Type"] = 1.5

    # snake_case or SCREAMING_SNAKE_CASE: contains underscore with letters
    if '_' in q and re.search(r'[a-zA-Z]', q):
        boosts["Function"] = 1.5

    # Dotted path: boost qualified name matches
    if '.' in q:
        boosts["_qualified"] = 2.0

    # Identifiers extracted from anywhere in the query
    idents = extract_query_identifiers(q)
    if idents:
        boosts["_qualified_identifiers"] = idents

    return boosts


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def rrf_merge(*result_lists: list[tuple[int, float]], k: int = 60) -> list[tuple[int, float]]:
    """Merge multiple ranked result lists using Reciprocal Rank Fusion.

    Each input list contains ``(id, score)`` tuples, ordered by score
    descending. The RRF score for each item is the sum of
    ``1 / (k + rank + 1)`` across all lists it appears in, where rank is
    the 0-based position.

    Args:
        *result_lists: Variable number of ranked result lists.
        k: RRF constant (default 60). Higher values reduce the impact of
           rank differences.

    Returns:
        Merged list of ``(id, rrf_score)`` tuples sorted by score descending.
    """
    scores: dict[int, float] = {}

    for result_list in result_lists:
        for rank, (item_id, _score) in enumerate(result_list):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)

    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return merged


# ---------------------------------------------------------------------------
# FTS5 search
# ---------------------------------------------------------------------------


_QUERY_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
# Words that would OR half the index into the candidate list.
_FTS_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in",
    "is", "it", "of", "on", "or", "the", "to", "what", "where", "which", "who", "with",
})


def _fts_quote(text: str) -> str:
    """One FTS5 string literal, so query text can never become an operator."""
    return '"' + text.replace('"', '""') + '"'


def _query_terms(query: str) -> tuple[list[str], list[str], str]:
    """Words of a query: ``(raw words, distinct name tokens, full token sequence)``.

    The sequence keeps every token in order, as ``name_tokens`` stores it, so
    a phrase over it matches the same identifier split the same way.
    """
    raw = [w for w in _QUERY_WORD_RE.findall(query) if w.lower() not in _FTS_STOPWORDS]
    sequence = " ".join(split_name_tokens(word) for word in raw).strip()
    tokens: list[str] = []
    for tok in sequence.split():
        if len(tok) >= 2 and tok not in _FTS_STOPWORDS and tok not in tokens:
            tokens.append(tok)
    return raw, tokens, sequence


def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[int, float]]:
    """Run an FTS5 BM25 search against the nodes_fts table.

    Phrase hits come first: the whole query as one phrase in any column, or
    its camelCase/snake_case words as one phrase in ``name_tokens`` (so
    "vendor invoice" finds ``VendorInvoiceActionBean``). Broader hits follow:
    any word of a multi-word query, or all parts of a single identifier in
    any order, so a query never misses because one word is absent.

    Returns list of ``(node_id, bm25_score)`` tuples. The BM25 score is
    negated so higher = better (FTS5 returns negative BM25).
    """
    raw, tokens, sequence = _query_terms(query)
    phrase = _fts_quote(query)
    if sequence:
        phrase += " OR name_tokens : " + _fts_quote(sequence)
    broad: Optional[str] = None
    if len(raw) > 1:
        words = dict.fromkeys([w.lower() for w in raw] + tokens)
        broad = " OR ".join(_fts_quote(word) for word in words)
    elif len(tokens) > 1:
        broad = " AND ".join(_fts_quote(tok) for tok in tokens)

    sql = "SELECT rowid, rank FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?"
    try:
        # FTS5 rank is negative BM25 (lower = better), negate for consistency
        hits = [(row[0], -row[1]) for row in conn.execute(sql, (phrase, limit)).fetchall()]
        if broad and len(hits) < limit:
            seen = {node_id for node_id, _ in hits}
            for row in conn.execute(sql, (broad, limit)).fetchall():
                if row[0] not in seen:
                    seen.add(row[0])
                    hits.append((row[0], -row[1]))
        return hits[:limit]
    except sqlite3.OperationalError as e:
        logger.warning("FTS5 search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Embedding search (optional)
# ---------------------------------------------------------------------------


def _embedding_search(
    store: GraphStore,
    query: str,
    limit: int = 50,
    model: str | None = None,
    provider: str | None = None,
    repo_root: str | None = None,
    info: Optional[dict[str, Any]] = None,
) -> list[tuple[int, float]]:
    """Run a vector similarity search using the embedding store.

    Returns list of ``(node_id, similarity_score)`` tuples, empty when
    embeddings are off, unavailable or not built yet. *info* (when given)
    receives ``embeddings_state`` and a ``warning`` explaining any fallback.
    """
    out = info if info is not None else {}
    out.setdefault("embeddings_state", "off")
    try:
        from .embeddings import open_search_store
    except ImportError:
        return []

    try:
        emb_store, state = open_search_store(
            store.db_path, repo_root=repo_root, provider=provider, model=model,
        )
        out["embeddings_state"] = state["state"]
        if state.get("provider"):
            out["embeddings_provider"] = state["provider"]
        if state.get("warning"):
            out["warning"] = state["warning"]
        if emb_store is None:
            return []
        try:
            results = emb_store.search(query, limit=limit)
        finally:
            emb_store.close()
        # Map qualified names back to node IDs in batched lookups.
        ids: dict[str, int] = {}
        names = [qn for qn, _ in results]
        for i in range(0, len(names), 450):
            chunk = names[i:i + 450]
            marks = ",".join("?" * len(chunk))
            for row in store._conn.execute(
                f"SELECT id, qualified_name FROM nodes WHERE qualified_name IN ({marks})",  # nosec B608
                chunk,
            ):
                ids[row["qualified_name"]] = row["id"]
        return [(ids[qn], score) for qn, score in results if qn in ids]
    except Exception as e:
        logger.warning("Embedding search failed: %s", e)
        out["embeddings_state"] = "unavailable"
        out["warning"] = f"embedding search failed ({type(e).__name__}); keyword results only"
        return []


# ---------------------------------------------------------------------------
# Keyword LIKE fallback
# ---------------------------------------------------------------------------


def _keyword_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[int, float]]:
    """Fall back to simple LIKE keyword matching.

    Each word in the query must match independently (AND logic).
    Returns ``(node_id, score)`` tuples with a basic relevance score.
    """
    words = query.lower().split()
    if not words:
        return []

    conditions: list[str] = []
    params: list[str | int] = []
    for word in words:
        conditions.append(
            "(LOWER(name) LIKE ? OR LOWER(qualified_name) LIKE ?)"
        )
        params.extend([f"%{word}%", f"%{word}%"])

    where = " AND ".join(conditions)
    params.append(limit)
    sql = f"SELECT id, name, qualified_name FROM nodes WHERE {where} LIMIT ?"  # nosec B608

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    # Assign a simple relevance score: exact name match > prefix > contains
    q_lower = query.lower()
    results: list[tuple[int, float]] = []
    for row in rows:
        name_lower = row["name"].lower()
        if name_lower == q_lower:
            score = 3.0
        elif name_lower.startswith(q_lower):
            score = 2.0
        else:
            score = 1.0
        results.append((row["id"], score))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Main hybrid search
# ---------------------------------------------------------------------------

# Below this many ranked hits the LIKE lane tops the list up.
_FEW_HITS = 5


def hybrid_search(
    store: GraphStore,
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
    context_files: Optional[list[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    _out_mode: Optional[list[str]] = None,
    repo_root: Optional[str] = None,
    _out_info: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Hybrid search combining FTS5 BM25 and vector embeddings via RRF.

    Attempts FTS5 + embedding search first, falling back to FTS5-only,
    then keyword LIKE matching if FTS5 is unavailable.

    Args:
        store: The graph store to search.
        query: Search query string.
        kind: Optional node kind filter (e.g. ``"Function"``, ``"Class"``).
        limit: Maximum results to return (default 20).
        context_files: Optional list of file paths. Nodes in these files
            receive a 1.5x score boost.
        _out_mode: Optional output list. If provided, a single string is
            appended indicating which search path(s) contributed:
            ``"hybrid"`` (FTS + embeddings), ``"fts"`` (FTS only),
            ``"semantic"`` (embeddings only), ``"keyword"`` (LIKE fallback),
            or ``"none"`` (empty query, or all search paths returned 0 results).
        repo_root: Repository whose ``.code-review-graph.toml`` decides
            whether embeddings are on (derived from the database path when
            omitted).
        _out_info: Optional dict that receives ``embeddings_state``
            (off|ready|stale|unavailable) and a ``warning`` when semantic
            search was wanted but keyword search had to answer alone.

    Returns:
        List of dicts with node metadata and ``score`` field.
    """
    if not query or not query.strip():
        if _out_mode is not None:
            _out_mode.append("none")
        return []

    # NOTE: hybrid_search uses store._conn for FTS5 and keyword queries
    # because those operate on the FTS virtual table or need raw Row
    # access for batch-fetch performance.  This is documented coupling.
    conn = store._conn
    fetch_limit = limit * 3  # Fetch extra to allow for filtering and boosting

    # ------ Phase 1: Gather ranked lists ------
    fts_results: list[tuple[int, float]] = []
    emb_results: list[tuple[int, float]] = []

    # Try FTS5 search
    try:
        fts_results = _fts_search(conn, query, limit=fetch_limit)
    except Exception as e:
        logger.warning("FTS5 unavailable, will use fallback: %s", e)

    # Try embedding search
    info: dict[str, Any] = _out_info if _out_info is not None else {}
    emb_results = _embedding_search(
        store, query, limit=fetch_limit, model=model, provider=provider,
        repo_root=repo_root, info=info,
    )

    # ------ Phase 2: Merge via RRF or fallback ------
    if fts_results or emb_results:
        lists_to_merge = []
        if fts_results:
            lists_to_merge.append(fts_results)
        if emb_results:
            lists_to_merge.append(emb_results)
        merged = rrf_merge(*lists_to_merge)
        if len(merged) < _FEW_HITS:
            # Few ranked hits: substring matches catch spellings FTS tokens miss.
            seen = {node_id for node_id, _ in merged}
            floor = merged[-1][1] if merged else 1.0
            for node_id, _score in _keyword_search(conn, query, limit=fetch_limit):
                if node_id not in seen:
                    seen.add(node_id)
                    floor *= 0.99
                    merged.append((node_id, floor))
        if _out_mode is not None:
            if fts_results and emb_results:
                _out_mode.append("hybrid")
            elif fts_results:
                _out_mode.append("fts")
            else:
                _out_mode.append("semantic")
    else:
        # Fallback: keyword LIKE matching
        keyword_results = _keyword_search(conn, query, limit=fetch_limit)
        if not keyword_results:
            if _out_mode is not None:
                _out_mode.append("none")
            return []
        if _out_mode is not None:
            _out_mode.append("keyword")
        merged = keyword_results

    # ------ Phase 3+4: Batch-fetch nodes, apply boosting and kind filter ------
    kind_boosts = detect_query_kind_boost(query)
    # Stored file paths use POSIX separators (#774); bridge native spellings.
    context_set = (
        {normalize_file_path(p) for p in context_files} if context_files else set()
    )

    # Batch-fetch all candidate nodes in one query
    candidate_ids = [node_id for node_id, _ in merged]
    node_rows: dict[int, Any] = {}
    batch_size = 450
    for i in range(0, len(candidate_ids), batch_size):
        batch = candidate_ids[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders})",  # nosec B608
            batch,
        ).fetchall()
        for row in rows:
            node_rows[row["id"]] = row

    # Apply boosting
    boosted: list[tuple[int, float]] = []
    for node_id, score in merged:
        row = node_rows.get(node_id)
        if not row:
            continue

        node_kind = row["kind"]
        file_path = row["file_path"]
        qualified_name = row["qualified_name"]

        boost = 1.0
        if node_kind in kind_boosts:
            boost *= kind_boosts[node_kind]
        if "_qualified" in kind_boosts and '.' in query:
            if query.lower() in qualified_name.lower():
                boost *= kind_boosts["_qualified"]
        idents = kind_boosts.get("_qualified_identifiers")
        if idents:
            qn_lo = qualified_name.lower()
            if any(ident in qn_lo for ident in idents):
                boost *= 2.0
        if context_set and file_path in context_set:
            boost *= 1.5

        boosted.append((node_id, score * boost))

    boosted.sort(key=lambda x: x[1], reverse=True)

    # Build results from the already-fetched rows
    results: list[dict[str, Any]] = []
    for node_id, final_score in boosted:
        if len(results) >= limit:
            break

        row = node_rows.get(node_id)
        if not row:
            continue

        node_kind = row["kind"]
        if kind and node_kind != kind:
            continue

        results.append({
            "name": _sanitize_name(row["name"]),
            "qualified_name": _sanitize_name(row["qualified_name"]),
            "kind": node_kind,
            "file_path": row["file_path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "language": row["language"] or "",
            "params": row["params"],
            "return_type": row["return_type"],
            "signature": row["signature"] if "signature" in row.keys() else None,
            "score": round(final_score, 6),
        })

    # Pin exact-name matches ahead of fuzzy neighbours. When the FTS lane is
    # crowded with same-prefix symbols (e.g. a class full of getters), a node
    # whose simple name exactly equals a query token can rank below fuzzy
    # neighbours despite being the obvious match. Reordering is stable: the
    # relative order within each group is unchanged. Short tokens are
    # excluded so common words don't pin half the result set.
    query_tokens = {
        tok.lower() for tok in re.split(r"[^A-Za-z0-9]+", query) if len(tok) >= 6
    }
    if not query_tokens:
        return results

    def _is_exact_name_match(node: dict[str, Any]) -> bool:
        simple_name = (node.get("name") or "").split("(", 1)[0]
        return len(simple_name) >= 6 and simple_name.lower() in query_tokens

    exact = [n for n in results if _is_exact_name_match(n)]
    rest = [n for n in results if not _is_exact_name_match(n)]
    return exact + rest
