"""Tests for code_review_graph/resolvers/jsp.py: the JSP link resolver.

Plain assert-based; runnable directly with `python3 tests/test_jsp_resolver.py`
or under pytest, matching this repo's existing dual-mode test style (see
tests/test_hybrid_exact_pin.py).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.resolvers.jsp import resolve_jsp_links

CONFIG_TOML = '[resolvers.jsp]\nweb_root = "web"\nsource_root = "src"\n'

INDEX_JSP = """<html>
<%@ include file="footer.jspf" %>
<div beanclass="com.example.HomeBean">
  <form action="/home" method="post">
    <input type="submit"/>
  </form>
</div>
</html>
"""

FOOTER_JSPF = "<div>footer</div>\n"

HOME_BEAN_JAVA = """package com.example;

@UrlBinding("/home")
public class HomeBean {
}
"""


def _build_repo(root: Path, *, with_config: bool) -> None:
    if with_config:
        cfg_dir = root / ".code-review-graph"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")

    web = root / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "index.jsp").write_text(INDEX_JSP, encoding="utf-8")
    (web / "footer.jspf").write_text(FOOTER_JSPF, encoding="utf-8")

    src = root / "src" / "com" / "example"
    src.mkdir(parents=True, exist_ok=True)
    (src / "HomeBean.java").write_text(HOME_BEAN_JAVA, encoding="utf-8")


def _snapshot(store: GraphStore) -> tuple[list[tuple], list[tuple]]:
    conn = store._conn
    nodes = sorted(
        (row["kind"], row["name"], row["qualified_name"], row["file_path"], row["language"])
        for row in conn.execute(
            "SELECT kind, name, qualified_name, file_path, language "
            "FROM nodes WHERE kind = 'File' AND language = 'jsp'"
        ).fetchall()
    )
    edges = sorted(
        (row["kind"], row["source_qualified"], row["target_qualified"], row["file_path"], row["line"])
        for row in conn.execute(
            "SELECT kind, source_qualified, target_qualified, file_path, line "
            "FROM edges WHERE kind IN ('RENDERS', 'REQUESTS', 'INCLUDES')"
        ).fetchall()
    )
    return nodes, edges


def test_produces_jsp_file_nodes_and_the_three_edge_kinds():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_repo(root, with_config=True)
        store = GraphStore(root / ".code-review-graph" / "graph.db")
        try:
            result = resolve_jsp_links(store, root)

            assert result["files_indexed"] == 2
            assert result["renders"] == 1
            assert result["requests"] == 1
            assert result["includes"] == 1

            nodes, edges = _snapshot(store)
            assert nodes == [
                ("File", "web/footer.jspf", "web/footer.jspf", "web/footer.jspf", "jsp"),
                ("File", "web/index.jsp", "web/index.jsp", "web/index.jsp", "jsp"),
            ]

            renders = [e for e in edges if e[0] == "RENDERS"]
            requests_ = [e for e in edges if e[0] == "REQUESTS"]
            includes = [e for e in edges if e[0] == "INCLUDES"]

            assert renders == [("RENDERS", "web/index.jsp", "com.example.HomeBean", "web/index.jsp", 3)]
            assert requests_ == [
                ("REQUESTS", "web/index.jsp", "com.example.HomeBean", "web/index.jsp", 4)
            ]
            assert includes == [
                ("INCLUDES", "web/index.jsp", "web/footer.jspf", "web/index.jsp", 2)
            ]
        finally:
            store.close()
    print("OK: JSP File nodes plus one RENDERS/REQUESTS/INCLUDES edge, right endpoints")


def test_running_twice_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_repo(root, with_config=True)
        store = GraphStore(root / ".code-review-graph" / "graph.db")
        try:
            resolve_jsp_links(store, root)
            first = _snapshot(store)

            resolve_jsp_links(store, root)
            second = _snapshot(store)

            assert first == second
            # And the row counts didn't silently double either.
            node_count = store._conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind = 'File' AND language = 'jsp'"
            ).fetchone()[0]
            edge_count = store._conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind IN ('RENDERS', 'REQUESTS', 'INCLUDES')"
            ).fetchone()[0]
            assert node_count == 2
            assert edge_count == 3
        finally:
            store.close()
    print("OK: re-running the resolver converges to the identical graph")


def test_no_config_does_nothing_and_does_not_raise():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_repo(root, with_config=False)  # no .code-review-graph/config.toml at all
        store = GraphStore(root / ".code-review-graph" / "graph.db")
        try:
            result = resolve_jsp_links(store, root)
            assert result == {
                "files_indexed": 0,
                "bindings": 0,
                "renders": 0,
                "requests": 0,
                "includes": 0,
            }
            nodes, edges = _snapshot(store)
            assert nodes == []
            assert edges == []
        finally:
            store.close()
    print("OK: no [resolvers.jsp] section -> no-op, zero-stats dict, no raise")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"\n{len(tests)} jsp resolver tests passed")


if __name__ == "__main__":
    _run_all()
