"""Post-build resolution of ORM entity -> table mappings.

Hibernate maps entities to database tables two ways: JPA annotations
(``@Table(name = ...)`` on an entity class) and ``*.hbm.xml`` mapping files
(``<class table = ...>``). Neither produces a graph edge on its own: the
annotation lives inside a Java file, the mapping file is plain XML. This
resolver owns the derived persistence graph:

  Table      one node per table name (qualified name ``table::<name>``,
             mirroring the virtual ``event::`` marker of the Spring event
             resolver)
  MAPS_TO    entity Class node -> Table node for annotated entities; the
             ``.hbm.xml`` File node -> Table node for XML-mapped entities

Tables are created only when a real mapping source names them; a database
with no mapped entities contributes nothing. Runs with the framework-level
conventions of :mod:`code_review_graph.repo_config` (the shared frontend
table drives the source root for class resolution).

Idempotent: the write is diff-based — unchanged Table nodes and edges
are only re-upserted (stable row ids, no journal churn) and stale rows are
deleted — so rebuilds converge and deletions never leave stale rows behind.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..parser import EdgeInfo, NodeInfo
from ..repo_config import JspResolverConfig, load_jsp_resolver_config
from .jsp import (
    _class_index,
    _java_sources,
    _Lines,
    _resolve_class,
)

if TYPE_CHECKING:
    from ..graph import GraphStore

logger = logging.getLogger(__name__)

TABLE_ANNO = re.compile(r'@Table\s*\(\s*name\s*=\s*"([^"]+)"\s*\)')
PACKAGE_DECL = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
HBM_MAPPING_PACKAGE = re.compile(r'<hibernate-mapping\b[^>]*\bpackage\s*=\s*"([^"]+)"', re.S)
HBM_CLASS = re.compile(r"<class\b[^>]*\btable\s*=\s*\"([^\"]+)\"", re.S)

_TABLE_FILE_PATH = "table"

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _annotated_entities(
    repo_root: Path, config: JspResolverConfig,
) -> list[tuple[str, int, str, str]]:
    """``(file, line, class simple name, table)`` for every ``@Table`` entity."""
    source_root = repo_root / config.source_root
    if not source_root.is_dir():
        return []
    found: list[tuple[str, int, str, str]] = []
    for path in _java_sources(repo_root, source_root):
        text = _read(path)
        if "@Table" not in text:
            continue
        package = PACKAGE_DECL.search(text)
        if package is None:
            continue
        lines = _Lines(text)
        for match in TABLE_ANNO.finditer(text):
            klass = re.search(r"\b(?:class|interface)\s+(\w+)", text[match.end():])
            if klass is None:
                continue
            found.append((
                path.as_posix(),
                lines.of(match.start()),
                klass.group(1),
                match.group(1),
            ))
    return found


def _class_qn(
    text: str,
    klass: str,
    config: JspResolverConfig,
    classes: dict[str, list[tuple[str, str]]],
) -> str | None:
    """Resolve an entity's dotted FQN to its Class node by path suffix."""
    package = PACKAGE_DECL.search(text)
    if package is None:
        return None
    return _resolve_class(f"{package.group(1)}.{klass}", classes)


def _hbm_sources(conn) -> list[str]:
    """Qualified names of the graph's ``*.hbm.xml`` File nodes."""
    rows = conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE kind = 'File' AND language = 'xml' AND qualified_name LIKE '%.hbm.xml' "
        "ORDER BY qualified_name"
    )
    return [row["qualified_name"] for row in rows]


def resolve_hibernate_mappings(store: GraphStore, repo_root: Path) -> dict[str, int]:
    """Rebuild Table nodes and MAPS_TO edges from live mapping sources.

    Every node and edge is re-derived from the entity sources currently on
    disk plus the graph's own Class/File nodes; a repository without ORM
    mappings is a clean no-op that still clears this resolver's stale state.
    """
    with store.transaction():
        return _resolve(store, repo_root)


def _resolve(store: GraphStore, repo_root: Path) -> dict[str, int]:
    repo_root = Path(repo_root).resolve()
    config = load_jsp_resolver_config(repo_root) or JspResolverConfig()

    conn = store._conn  # intentional: bounded post-build maintenance pass
    classes = _class_index(conn)
    wanted_tables: set[str] = set()
    wanted_edges: dict[tuple[str, str, str, int], dict] = {}
    unresolved = 0

    for path, line, klass, table in _annotated_entities(repo_root, config):
        class_qn = _class_qn(_read(Path(path)), klass, config, classes)
        if class_qn is None:
            unresolved += 1
            continue
        wanted_tables.add(table)
        wanted_edges[(class_qn, f"{_TABLE_FILE_PATH}::{table}", path, line)] = {
            "table": table, "via": "annotation",
        }
    for qn in _hbm_sources(conn):
        text = _read(Path(qn))
        lines = _Lines(text)
        for match in HBM_CLASS.finditer(text):
            table = match.group(1)
            wanted_tables.add(table)
            wanted_edges[(
                qn, f"{_TABLE_FILE_PATH}::{table}", qn, lines.of(match.start()),
            )] = {"table": table, "via": "hbm"}

    # Diff-based write: unchanged Table nodes and MAPS_TO edges are only
    # re-upserted (stable row ids, no journal churn); stale rows are deleted.
    existing_tables = {
        row[0] for row in conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind = 'Table'"
        )
    }
    stale_tables = sorted(existing_tables - {f"{_TABLE_FILE_PATH}::{t}" for t in wanted_tables})
    if stale_tables:
        marks = ", ".join("?" for _ in stale_tables)
        conn.execute(
            f"DELETE FROM nodes WHERE qualified_name IN ({marks})", stale_tables,  # nosec B608
        )
    existing_edges = {
        (row[0], row[1], row[2], row[3]) for row in conn.execute(
            "SELECT source_qualified, target_qualified, file_path, line "
            "FROM edges WHERE kind = 'MAPS_TO'"
        )
    }
    for source, target, file_path, line in sorted(existing_edges - set(wanted_edges)):
        conn.execute(
            "DELETE FROM edges WHERE kind = 'MAPS_TO' AND source_qualified = ? "
            "AND target_qualified = ? AND file_path = ? AND line = ?",
            (source, target, file_path, line),
        )

    for table in sorted(wanted_tables):
        store.upsert_node(NodeInfo(
            kind="Table",
            name=table,
            file_path=_TABLE_FILE_PATH,
            line_start=0,
            line_end=0,
            language="sql",
            extra={"table": table},
        ))
    for (source, target, file_path, line), extra in wanted_edges.items():
        store.upsert_edge(EdgeInfo(
            kind="MAPS_TO",
            source=source,
            target=target,
            file_path=file_path,
            line=line,
            extra=extra,
        ))

    store._invalidate_cache()
    result = {
        "entities": sum(1 for extra in wanted_edges.values() if extra["via"] == "annotation"),
        "mappings": sum(1 for extra in wanted_edges.values() if extra["via"] == "hbm"),
        "tables": len(wanted_tables),
        "unresolved": unresolved,
    }
    logger.info("Hibernate mapping resolution: %s", result)
    return result
