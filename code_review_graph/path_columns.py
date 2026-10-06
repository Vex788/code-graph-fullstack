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

import re
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


# Updating a big table row by row maintains every index per row; copying it with the
# transform in the SELECT and building the indexes once is about 2.5x faster. Only for
# tables with no UNIQUE path column, so one pass is safe even for nested roots.
_COPY_TABLES = ("edges",)


def _copy_rewrite(
    conn: sqlite3.Connection, table: str, old: str, new: str,
) -> dict[str, int] | None:
    """Rebuild *table* with re-rooted values; ``None`` when it must be updated in place."""
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    has_trigger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
    ).fetchone()
    scratch = f"{table}_reroot"
    ddl = re.sub(
        rf'(?i)^(CREATE\s+TABLE\s+)["`\[]?{table}["`\]]?', rf"\g<1>{scratch}", ddl_row[0], count=1,
    )
    if has_trigger or ddl == ddl_row[0]:
        return None
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]  # noqa: S608
    select: list[str] = []
    select_params: list[object] = []
    counts: list[str] = []
    counted: list[str] = []
    count_params: list[object] = []
    specs = {s.column: s for s in PATH_COLUMNS if s.table == table and s.column in columns}
    if not specs:
        return None
    for column in columns:
        spec = specs.get(column)
        if spec is None:
            select.append(column)
            continue
        args: tuple[object, ...]
        value_args: tuple[object, ...]
        if spec.mode == "path":
            test = f"typeof({column}) = 'text' AND substr({column}, 1, ?) = ?"
            args = (len(old), old)
            value, value_args = f"? || substr({column}, ?)", (new, len(old) + 1)
        else:
            test = f"typeof({column}) = 'text' AND instr({column}, ?) > 0"
            args = ('"' + old,)
            value, value_args = f"replace({column}, ?, ?)", ('"' + old, '"' + new)
        select.append(f"CASE WHEN {test} THEN {value} ELSE {column} END")
        select_params += [*args, *value_args]
        counts.append(f"coalesce(sum({test}), 0)")
        counted.append(column)
        count_params += args
    totals = conn.execute(  # nosec B608
        f"SELECT {', '.join(counts)} FROM {table}", count_params,  # noqa: S608
    ).fetchone()
    indexes = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    )]
    has_sequence = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
    ).fetchone()
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
    ).fetchone() if has_sequence else None
    conn.execute(ddl)
    conn.execute(  # nosec B608
        f"INSERT INTO {scratch} ({', '.join(columns)}) "  # noqa: S608
        f"SELECT {', '.join(select)} FROM {table}",
        select_params,
    )
    conn.execute(f"DROP TABLE {table}")  # noqa: S608
    conn.execute(f"ALTER TABLE {scratch} RENAME TO {table}")  # noqa: S608
    if sequence is not None:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = ?", (sequence[0], table),
        )
    for sql in indexes:
        conn.execute(sql)
    return {f"{table}.{column}": int(total) for column, total in zip(counted, totals)}


# Never a real path prefix, so a nested-root first pass cannot collide with a UNIQUE row.
_PLACEHOLDER = "\x01crg-clone\x01/"


def rewrite_root(conn: sqlite3.Connection, old_root: str, new_root: str) -> dict[str, int]:
    """Replace the *old_root* prefix with *new_root* in every registered column.

    Runs inside the caller's transaction. Only registered columns are touched,
    and only text values, so vectors and FTS shadow tables stay byte-identical.
    Roots that nest in either direction (``repo/.claude/worktrees/x``) go
    through a placeholder in two passes so rewritten rows cannot collide on
    ``qualified_name``; disjoint roots rewrite in one pass.
    Returns ``{"table.column": rows_changed}``.
    """
    old = old_root.rstrip("/") + "/"
    new = new_root.rstrip("/") + "/"
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    changed: dict[str, int] = {}
    for table in _COPY_TABLES:
        copied = _copy_rewrite(conn, table, old, new) if table in tables else None
        if copied is not None:
            changed.update(copied)
            tables.discard(table)
    if not (new.startswith(old) or old.startswith(new)):
        changed.update(_rewrite(conn, tables, old, new))
        return changed
    changed.update(_rewrite(conn, tables, old, _PLACEHOLDER))
    _rewrite(conn, tables, _PLACEHOLDER, new)
    return changed


def is_registered(table: str, column: str) -> bool:
    return (table, column) in _KNOWN
