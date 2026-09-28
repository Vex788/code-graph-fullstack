"""Tests for code_review_graph/resolvers/hibernate.py: ORM mapping resolver.

Annotated entities map their Class node to a Table node; ``*.hbm.xml``
mapping files map the XML File node to one. The resolver owns both the
Table nodes (qualified ``table::<name>``) and every MAPS_TO edge, and a
mapping that disappears from the sources takes its node and edges with it.

Plain assert-based; runnable directly with
`python3 tests/test_hibernate_resolver.py` or under pytest.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.resolvers.hibernate import resolve_hibernate_mappings

ENTITY_JAVA = """package com.example.model;

import javax.persistence.*;

@Entity
@Table(name = "users")
public class User {
}
"""

HBM_XML = """<?xml version="1.0"?>
<hibernate-mapping package="com.example.model">
  <class name="Order" table="orders">
    <id name="id"/>
  </class>
</hibernate-mapping>
"""


def _write_repo(root: Path, entity_text: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    model = root / "src" / "com" / "example" / "model"
    model.mkdir(parents=True)
    paths["entity"] = model / "User.java"
    paths["entity"].write_text(entity_text, encoding="utf-8")
    paths["hbm"] = model / "Order.hbm.xml"
    paths["hbm"].write_text(HBM_XML, encoding="utf-8")
    return paths


def _seed_graph(root: Path, paths: dict[str, Path]) -> GraphStore:
    store = GraphStore(root / "graph.db")
    for path in paths.values():
        store.upsert_node(NodeInfo(
            kind="File", name=str(path), file_path=str(path),
            line_start=1, line_end=5,
            language="java" if path.suffix == ".java" else "xml",
        ))
    store.upsert_node(NodeInfo(
        kind="Class", name="User", file_path=str(paths["entity"]),
        line_start=6, line_end=8, language="java",
    ))
    store.commit()
    return store


def _table_qns(store: GraphStore) -> set[str]:
    return {
        row["qualified_name"] for row in store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind = 'Table'"
        )
    }


def test_emits_table_nodes_and_maps_to_edges():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        paths = _write_repo(root, ENTITY_JAVA)
        store = _seed_graph(root, paths)
        try:
            stats = resolve_hibernate_mappings(store, root)

            assert stats["entities"] == 1
            assert stats["mappings"] == 1
            assert stats["tables"] == 2
            assert stats["unresolved"] == 0

            assert _table_qns(store) == {"table::users", "table::orders"}
            users = store._conn.execute(
                "SELECT node.name FROM edges e "
                "JOIN nodes node ON node.qualified_name = e.target_qualified "
                "WHERE e.kind = 'MAPS_TO' AND e.target_qualified = 'table::users'"
            ).fetchone()
            assert users is not None and users["name"] == "users"
            hbm_edge = store._conn.execute(
                "SELECT source_qualified FROM edges "
                "WHERE kind = 'MAPS_TO' AND target_qualified = 'table::orders'"
            ).fetchone()
            assert hbm_edge["source_qualified"] == str(paths["hbm"])
        finally:
            store.close()
    print("OK: annotation and hbm.xml mappings produce Table nodes and MAPS_TO")


def test_removed_mapping_takes_its_table_with_it():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        paths = _write_repo(root, ENTITY_JAVA)
        store = _seed_graph(root, paths)
        try:
            resolve_hibernate_mappings(store, root)
            assert _table_qns(store) == {"table::users", "table::orders"}

            paths["entity"].write_text(
                ENTITY_JAVA.replace("@Table(name = \"users\")\n", ""),
                encoding="utf-8",
            )
            stats = resolve_hibernate_mappings(store, root)

            assert stats["entities"] == 0
            assert _table_qns(store) == {"table::orders"}
            assert store._conn.execute(
                "SELECT count(*) FROM edges WHERE kind = 'MAPS_TO' "
                "AND target_qualified = 'table::users'"
            ).fetchone()[0] == 0
        finally:
            store.close()
    print("OK: dropping the annotation removes the Table node and its edges")


def test_unresolvable_entity_class_is_counted_not_guessed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        paths = _write_repo(root, ENTITY_JAVA)
        store = GraphStore(root / "graph.db")
        try:
            store.upsert_node(NodeInfo(
                kind="File", name=str(paths["hbm"]), file_path=str(paths["hbm"]),
                line_start=1, line_end=6, language="xml",
            ))
            store.commit()
            stats = resolve_hibernate_mappings(store, root)

            assert stats["entities"] == 0
            assert stats["unresolved"] == 1
            assert _table_qns(store) == {"table::orders"}
        finally:
            store.close()
    print("OK: an entity without a Class node is counted unresolved, not faked")


if __name__ == "__main__":
    test_emits_table_nodes_and_maps_to_edges()
    test_removed_mapping_takes_its_table_with_it()
    test_unresolvable_entity_class_is_counted_not_guessed()
    print("\nALL PASSED")
