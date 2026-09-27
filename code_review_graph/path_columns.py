"""Registry of every graph column that can hold an absolute repository path.

Graphs store absolute paths (``normalize_file_path``), and qualified names are
``<path>::<symbol>``. Moving a graph to another checkout therefore rewrites
exactly these columns; ``tests/test_clone_graph.py`` scans every text column of
a cloned fixture graph so a new path-bearing column cannot slip past this list.

Modes:
    ``path``: the value itself starts with the root (a path or qualified name).
    ``json``: JSON text whose string values may start with the root.

BLOB columns (embedding vectors, FTS shadow tables) are never listed; the FTS
index is rebuilt from ``nodes`` instead of being rewritten.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

Mode = Literal["path", "json"]


@dataclass(frozen=True)
class PathColumn:
    table: str
    column: str
    mode: Mode


PATH_COLUMNS: tuple[PathColumn, ...] = (
    PathColumn("nodes", "file_path", "path"),
    PathColumn("nodes", "qualified_name", "path"),
    # File nodes are named, and signed, by their path.
    PathColumn("nodes", "name", "path"),
    PathColumn("nodes", "signature", "path"),
    PathColumn("nodes", "parent_name", "path"),
    PathColumn("nodes", "extra", "json"),
    PathColumn("edges", "source_qualified", "path"),
    PathColumn("edges", "target_qualified", "path"),
    PathColumn("edges", "file_path", "path"),
    PathColumn("edges", "extra", "json"),
    # Node ids today; listed so a path-bearing format cannot slip through.
    PathColumn("flows", "path_json", "json"),
    PathColumn("flow_snapshots", "entry_point", "path"),
    PathColumn("flow_snapshots", "critical_path", "json"),
    PathColumn("community_summaries", "key_symbols", "json"),
    PathColumn("risk_index", "qualified_name", "path"),
    PathColumn("embeddings", "qualified_name", "path"),
    PathColumn("metadata", "value", "json"),
)

_KNOWN = {(c.table, c.column) for c in PATH_COLUMNS}


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608


def _rewrite(conn: sqlite3.Connection, tables: set[str], old: str, new: str) -> dict[str, int]:
    changed: dict[str, int] = {}
    for spec in PATH_COLUMNS:
        if spec.table not in tables or spec.column not in _existing_columns(conn, spec.table):
            continue
        # Identifiers come from the static registry above, never from input.
        if spec.mode == "path":
            cursor = conn.execute(
                f"UPDATE {spec.table} SET {spec.column} = ? || substr({spec.column}, ?) "  # nosec B608
                f"WHERE typeof({spec.column}) = 'text' AND substr({spec.column}, 1, ?) = ?",
                (new, len(old) + 1, len(old), old),
            )
        else:
            cursor = conn.execute(
                f"UPDATE {spec.table} SET {spec.column} = replace({spec.column}, ?, ?) "  # nosec B608
                f"WHERE typeof({spec.column}) = 'text' AND instr({spec.column}, ?) > 0",
                ('"' + old, '"' + new, '"' + old),
            )
        changed[f"{spec.table}.{spec.column}"] = cursor.rowcount
    return changed


# Never a real path prefix, so the first pass cannot collide with a UNIQUE row.
_PLACEHOLDER = "\x01crg-clone\x01/"


def rewrite_root(conn: sqlite3.Connection, old_root: str, new_root: str) -> dict[str, int]:
    """Replace the *old_root* prefix with *new_root* in every registered column.

    Runs inside the caller's transaction. Only registered columns are touched,
    and only text values, so vectors and FTS shadow tables stay byte-identical.
    Two passes through a placeholder keep a target nested inside the seed
    (``repo/.claude/worktrees/x``) from colliding on ``qualified_name``.
    Returns ``{"table.column": rows_changed}``.
    """
    old = old_root.rstrip("/") + "/"
    new = new_root.rstrip("/") + "/"
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    changed = _rewrite(conn, tables, old, _PLACEHOLDER)
    _rewrite(conn, tables, _PLACEHOLDER, new)
    return changed


def is_registered(table: str, column: str) -> bool:
    return (table, column) in _KNOWN
