"""Post-build Spring DI call resolver.

After tree-sitter parsing, Java CALLS edges whose target is a bare method
name (e.g. ``calculate``) carry ``extra.receiver`` naming the local variable
that was called on (e.g. ``invoiceCalculationService``).  This module
resolves those receivers through the INJECTS map to their declared type, then
optionally to the unique concrete implementation via INHERITS edges.

Resolution chain:
    receiver variable name
        → injected interface/class (from INJECTS.extra.field_name)
        → concrete implementation (from INHERITS, when unique)

Only Java files are processed.  Edges that are already qualified (contain
``::``) or have no ``receiver`` extra key are skipped.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING

from .parser import EdgeInfo, java_arity_matches

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def _bare_type_name(type_ref: str) -> str:
    """``path/X.java::Outer.X`` or ``a.b.X`` -> ``X``."""
    return type_ref.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def resolve_spring_di_calls(store: GraphStore) -> dict:
    """Resolve Java CALLS edges whose receiver is a Spring-injected field.

    Safe to call multiple times — already-resolved edges (targets containing
    ``::``) are skipped. Afterwards, calls aimed at an overloaded method's
    base name are bound to one overload (see :func:`bind_java_overload_targets`).

    Returns a dict with resolution counts for telemetry.
    """
    with store.transaction():
        stats = _resolve_injected_receivers(store)
        stats["overloads_bound"] = bind_java_overload_targets(store._conn)
    return stats


def _overload_base(qualified: str) -> str | None:
    """``f::C.save(User)`` -> ``f::C.save``; None for a non-overload identity."""
    paren = qualified.find("(", qualified.rfind("::") + 2)
    return qualified[:paren] if paren > 0 and qualified.endswith(")") else None


def bind_java_overload_targets(conn: sqlite3.Connection) -> int:
    """Bind Java CALLS targeting ``C.m`` to the overload ``C.m(T..)`` their arity fits.

    The parser resolves cross-file calls to ``file::Class.method`` without
    seeing the target file, so an overloaded method's callers point at a
    name no node carries. One arity match binds; several keep the base name
    with ``ambiguous_targets``. ``java_overload`` marks the edge so it
    re-binds when the overload set changes.
    """
    overloads: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT qualified_name FROM nodes WHERE language = 'java' "
        "AND kind IN ('Function', 'Test') AND qualified_name LIKE '%)'"
    ):
        base = _overload_base(row[0])
        if base:
            overloads.setdefault(base, []).append(row[0])

    select = (
        "SELECT id, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges WHERE kind = 'CALLS' AND "
    )
    rows = {
        row[0]: row
        for base in overloads
        for row in conn.execute(select + "target_qualified = ?", (base,))
    }
    for row in conn.execute(select + "extra LIKE '%\"java_overload\"%'"):
        rows.setdefault(row[0], row)

    changed = 0
    for edge_id, source, target, file_path, line, raw_extra in rows.values():
        try:
            extra = json.loads(raw_extra or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(extra, dict):
            continue
        base = (_overload_base(target) if extra.get("java_overload") else None) or target
        candidates = sorted(overloads.get(base, []))
        arg_count = extra.get("arg_count")
        if candidates and isinstance(arg_count, int):
            candidates = [q for q in candidates if java_arity_matches(q, arg_count)] or candidates
        new_extra = {
            key: value for key, value in extra.items()
            if key not in (
                "java_overload", "ambiguous_targets",
                "ambiguous_target_count", "ambiguous_targets_truncated",
            )
        }
        if len(candidates) == 1:
            new_target = candidates[0]
            new_extra["java_overload"] = True
        elif candidates:
            new_target = base
            new_extra.update({
                "java_overload": True,
                "ambiguous_targets": candidates[:20],
                "ambiguous_target_count": len(candidates),
                "ambiguous_targets_truncated": len(candidates) > 20,
            })
        else:
            # The overload set is gone: the base is a plain method again.
            new_target = base
        if new_target == target and new_extra == extra:
            continue
        serialized = json.dumps(new_extra)
        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
            (new_target, serialized, edge_id),
        )
        # Tests' TESTED_BY mirrors copy the CALLS target as their source.
        conn.execute(
            "UPDATE edges SET source_qualified = ?, extra = ? WHERE kind = 'TESTED_BY' "
            "AND source_qualified = ? AND target_qualified = ? AND file_path = ? AND line = ?",
            (new_target, serialized, target, source, file_path, line),
        )
        changed += 1
    return changed


def java_method_index(conn: sqlite3.Connection) -> dict[tuple[str, str, str], list[str]]:
    """``(file, class, method)`` -> method nodes; file-keyed so same-named classes stay apart."""
    index: dict[tuple[str, str, str], list[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name, file_path FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'java' AND parent_name IS NOT NULL"
    ):
        key = (row["file_path"], row["parent_name"].rsplit(".", 1)[-1], row["name"])
        index.setdefault(key, []).append(row["qualified_name"])
    return index


def pick_java_method(
    index: dict[tuple[str, str, str], list[str]],
    class_qual: str,
    method: str,
    arg_count: object,
) -> str:
    """The method of ``file::Class`` named *method* that fits *arg_count*.

    Falls back to ``file::Class.method`` (an overload base the overload
    binder resolves later) when none or several fit.
    """
    file_path, _, class_part = class_qual.partition("::")
    simple = class_part.rsplit(".", 1)[-1]
    candidates = index.get((file_path, simple, method), [])
    if len(candidates) > 1 and isinstance(arg_count, int):
        candidates = [q for q in candidates if java_arity_matches(q, arg_count)]
    return candidates[0] if len(candidates) == 1 else f"{file_path}::{simple}.{method}"


def java_class_owners(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """``(file, nested class)`` -> its enclosing class name."""
    return {
        (row["file_path"], row["name"]): row["parent_name"].rsplit(".", 1)[-1]
        for row in conn.execute(
            "SELECT file_path, name, parent_name FROM nodes "
            "WHERE kind = 'Class' AND language = 'java' AND parent_name IS NOT NULL"
        )
    }


def enclosing_class_chain(
    source_qual: str, owners: dict[tuple[str, str], str],
) -> list[str]:
    """``file::Class`` for the caller's class, then each lexically enclosing class.

    Anonymous (``Outer$1``) and inner classes read their outer class's fields.
    """
    file_path, sep, member = source_qual.partition("::")
    if not sep:
        return []
    current: str | None = member.split("(", 1)[0].rsplit(".", 1)[0] if "." in member else member
    chain: list[str] = []
    while current and current not in chain:
        chain.append(current)
        if "$" in current:
            current = current.rsplit("$", 1)[0]
        else:
            current = owners.get((file_path, current))
    return [f"{file_path}::{name}" for name in chain]


def calls_in_files(conn: sqlite3.Connection, files: set[str]) -> list[sqlite3.Row]:
    """CALLS edges recorded in *files*, read through the file index.

    A receiver resolves through its caller's enclosing classes, which never
    leave the caller's file, so only files declaring a mapped field matter.
    """
    rows: list[sqlite3.Row] = []
    for file_path in sorted(files):
        rows.extend(conn.execute(
            "SELECT id, source_qualified, target_qualified, extra, file_path, line "
            "FROM edges WHERE file_path = ? AND kind = 'CALLS'",
            (file_path,),
        ).fetchall())
    return rows


def called_method_name(target: str) -> str:
    """``f::C.save(User)`` / ``C.save`` / ``save`` -> ``save``."""
    return target.rsplit("::", 1)[-1].split("(", 1)[0].rsplit(".", 1)[-1]


def _resolve_injected_receivers(store: GraphStore) -> dict:
    conn = store._conn

    # Only process Java files
    java_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'java'"
        ).fetchall()
    }
    # Derived implementation edges are recomputed on every run.
    conn.execute("DELETE FROM edges WHERE kind = 'CALLS' AND extra LIKE '%\"spring_derived\"%'")
    if not java_files:
        return {"files_indexed": 0, "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Build field_map: (source_qualified_class, field_name) → injected_type
    # from INJECTS edges that carry extra.field_name
    # -----------------------------------------------------------------------
    field_map: dict[tuple[str, str], str] = {}
    injects_rows = conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'INJECTS'"
    ).fetchall()
    for row in injects_rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        fname = extra.get("field_name")
        if not fname:
            continue
        # source_qualified is the full class qualified name
        class_qual = row["source_qualified"]
        field_map[(class_qual, fname)] = row["target_qualified"]

    if not field_map:
        logger.info("Spring resolver: no INJECTS edges with field_name found, skipping")
        return {"files_indexed": len(java_files), "calls_resolved": 0}

    # Bare injected types (unresolved at parse time) name a class only when
    # exactly one Java class has that name.
    classes_by_name: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE kind = 'Class' AND language = 'java'"
    ).fetchall():
        classes_by_name.setdefault(row["name"], []).append(row["qualified_name"])

    method_index = java_method_index(conn)
    class_owners = java_class_owners(conn)

    # -----------------------------------------------------------------------
    # Build implementors: interface -> implementing class quals from INHERITS
    # edges (Java uses INHERITS for both extends and implements)
    # -----------------------------------------------------------------------
    implementors: dict[str, dict[str, None]] = {}
    for row in conn.execute(
        "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'INHERITS'"
    ).fetchall():
        iface = row["target_qualified"]
        impl = row["source_qualified"]
        if impl.partition("::")[0] not in java_files:
            continue
        # INHERITS targets are qualified when the parser resolved the type
        # and bare otherwise; index both spellings.
        for key in {iface, _bare_type_name(iface)}:
            implementors.setdefault(key, {})[impl] = None

    # -----------------------------------------------------------------------
    # Resolve CALLS edges
    # -----------------------------------------------------------------------
    calls_rows = calls_in_files(
        conn, {class_qual.partition("::")[0] for class_qual, _ in field_map} & java_files,
    )

    resolved = 0

    for row in calls_rows:
        if row["file_path"] not in java_files:
            continue

        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}

        receiver = extra.get("receiver")
        if not receiver:
            continue

        # Skip edges already spring-resolved in a previous pass
        if extra.get("spring_resolved"):
            continue

        raw_target = row["target_qualified"]
        method_name = called_method_name(raw_target)
        source_qual = row["source_qualified"]

        injected_type = next(
            (
                field_map[(class_qual, receiver)]
                for class_qual in enclosing_class_chain(source_qual, class_owners)
                if (class_qual, receiver) in field_map
            ),
            None,
        )
        if not injected_type:
            continue

        # Resolve to the concrete implementation when it is unique
        impls = list(
            implementors.get(injected_type)
            or implementors.get(_bare_type_name(injected_type))
            or {}
        )
        named = classes_by_name.get(injected_type, [])
        if len(impls) == 1:
            owner: str | None = impls[0]
        elif "::" in injected_type:
            owner = injected_type
        else:
            owner = named[0] if len(named) == 1 else None
        new_target = (
            pick_java_method(method_index, owner, method_name, extra.get("arg_count"))
            if owner
            else f"{injected_type}.{method_name}"
        )

        extra["spring_resolved"] = True
        extra["injected_type"] = injected_type

        if "::" in raw_target and extra.get("receiver_resolution") == "typed_receiver":
            # The parser already bound the call to the declared type; keep that
            # edge and add the injected implementation beside it.
            if new_target == raw_target:
                continue
            extra["spring_derived"] = True
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=source_qual,
                target=new_target,
                file_path=row["file_path"],
                line=row["line"],
                extra=extra,
            ))
        else:
            conn.execute(
                "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
                (new_target, json.dumps(extra), row["id"]),
            )
        resolved += 1
        logger.debug(
            "Spring resolved: %s → %s (was %s, receiver=%s)",
            source_qual, new_target, method_name, receiver,
        )

    logger.info("Spring DI resolver: resolved %d CALLS edges in %d Java files",
                resolved, len(java_files))
    return {"files_indexed": len(java_files), "calls_resolved": resolved}
