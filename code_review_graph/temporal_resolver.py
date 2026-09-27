"""Post-build Temporal workflow/activity call resolver.

After tree-sitter parsing, Java CALLS edges whose target is a bare method
name carry ``extra.receiver`` naming the local variable called on.  This
module resolves those receivers through the TEMPORAL_STUB map to their
declared Temporal interface type, then optionally to the unique concrete
implementation via INHERITS edges.

Resolution chain:
    receiver variable name
        → temporal stub field type (from TEMPORAL_STUB.extra.field_name)
        → concrete implementation (from INHERITS, when unique)

Only Java files are processed.  TEMPORAL_STUB edges whose target is not a
node with ``temporal_role`` in extra are silently skipped (they may be
non-Temporal types that happen to end in 'Activity'/'Workflow').
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .spring_resolver import (
    _bare_type_name,
    called_method_name,
    calls_in_files,
    enclosing_class_chain,
    java_class_owners,
    java_method_index,
    pick_java_method,
)

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_temporal_calls(store: GraphStore) -> dict:
    """Resolve Java CALLS edges whose receiver is a Temporal activity/workflow stub.

    Safe to call multiple times — already-resolved edges (with
    ``extra.temporal_resolved``) are skipped.

    Returns a dict with resolution counts for telemetry.
    """
    with store.transaction():
        return _resolve_temporal_calls(store)


def _resolve_temporal_calls(store: GraphStore) -> dict:
    conn = store._conn

    java_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'java'"
        ).fetchall()
    }
    if not java_files:
        return {"files_indexed": 0, "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Collect Temporal interface nodes: bare name → qualified_name
    # (nodes whose extra contains temporal_role = workflow_interface|activity_interface)
    # -----------------------------------------------------------------------
    temporal_interfaces: dict[str, str] = {}  # bare_name → qualified_name
    for row in conn.execute(
        "SELECT name, qualified_name, extra FROM nodes "
        "WHERE language = 'java' AND extra IS NOT NULL AND extra LIKE '%temporal_role%'"
    ).fetchall():
        try:
            ex = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            ex = {}
        if ex.get("temporal_role") in ("workflow_interface", "activity_interface"):
            temporal_interfaces[row["name"]] = row["qualified_name"]

    if not temporal_interfaces:
        logger.info("Temporal resolver: no Workflow/ActivityInterface nodes, skipping")
        return {"files_indexed": len(java_files), "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Build field_map: (source_qualified_class, field_name) → interface_type
    # from TEMPORAL_STUB edges whose target is a known Temporal interface
    # -----------------------------------------------------------------------
    field_map: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'TEMPORAL_STUB'"
    ).fetchall():
        bare_target = row["target_qualified"]
        if bare_target not in temporal_interfaces:
            continue
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        fname = extra.get("field_name")
        if not fname:
            continue
        field_map[(row["source_qualified"], fname)] = bare_target

    if not field_map:
        logger.info("Temporal resolver: no TEMPORAL_STUB edges found, skipping")
        return {"files_indexed": len(java_files), "calls_resolved": 0}

    method_index = java_method_index(conn)
    class_owners = java_class_owners(conn)

    # -----------------------------------------------------------------------
    # implementors: bare interface name → list of implementing class quals
    # -----------------------------------------------------------------------
    implementors: dict[str, list[str]] = {}
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
            implementors.setdefault(key, []).append(impl)

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

        if extra.get("temporal_resolved") or extra.get("spring_resolved"):
            continue

        method_name = called_method_name(row["target_qualified"])
        source_qual = row["source_qualified"]

        interface_bare = next(
            (
                field_map[(class_qual, receiver)]
                for class_qual in enclosing_class_chain(source_qual, class_owners)
                if (class_qual, receiver) in field_map
            ),
            None,
        )
        if not interface_bare:
            continue

        interface_qual = temporal_interfaces.get(interface_bare, interface_bare)

        # ``implementors`` indexes every interface under its bare name too:
        # INHERITS targets are qualified only when the parser resolved them.
        impls = list(dict.fromkeys(implementors.get(interface_bare, [])))
        owner = impls[0] if len(impls) == 1 else interface_qual
        if "::" in owner:
            new_target = pick_java_method(method_index, owner, method_name, extra.get("arg_count"))
        else:
            new_target = f"{owner}.{method_name}"

        extra["temporal_resolved"] = True
        extra["temporal_interface"] = interface_bare
        new_extra = json.dumps(extra)

        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
            (new_target, new_extra, row["id"]),
        )
        resolved += 1
        logger.debug(
            "Temporal resolved: %s → %s (receiver=%s, interface=%s)",
            source_qual, new_target, receiver, interface_bare,
        )

    logger.info("Temporal resolver: resolved %d CALLS edges in %d Java files",
                resolved, len(java_files))
    return {"files_indexed": len(java_files), "calls_resolved": resolved}
