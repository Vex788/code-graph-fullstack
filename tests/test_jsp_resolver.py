"""Tests for code_review_graph/resolvers/jsp.py: the JSP link resolver.

Since the fullstack fork the resolver creates no nodes of its own: it
discovers pages from the graph's File nodes (absolute-path qualified names,
language ``jsp``/``html``) and binds to the graph's Java Class/Endpoint
nodes. These tests pre-seed that graph state via GraphStore and check the
derived RENDERS/REQUESTS/INCLUDES/REFERENCES edges.

Plain assert-based; runnable directly with `python3 tests/test_jsp_resolver.py`
or under pytest, matching this repo's existing dual-mode test style (see
tests/test_hybrid_exact_pin.py).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.repo_config import clear_cache
from code_review_graph.resolvers.jsp import resolve_jsp_links

CONFIG_TOML = '[resolvers.jsp]\nweb_root = "web"\nsource_root = "src"\n'
DISABLED_TOML = '[resolvers.jsp]\nenabled = false\n'

INDEX_JSP = """<html>
<%@ include file="footer.jspf" %>
<div beanclass="com.example.HomeBean">
  <form action="/home" method="post">
    <input type="submit"/>
  </form>
</div>
</html>
"""

BROKEN_JSP = """<html>
<div beanclass="com.example.GoneBean">no such class</div>
</html>
"""

FOOTER_JSPF = "<div>footer</div>\n"

HOME_BEAN_JAVA = """package com.example;

@UrlBinding("/home")
public class HomeBean {
}
"""


def _write_repo(root: Path) -> dict[str, Path]:
    """Create the on-disk fixture; return the absolute paths the graph mirrors."""
    paths: dict[str, Path] = {}
    web = root / "web"
    web.mkdir(parents=True, exist_ok=True)
    paths["index"] = web / "index.jsp"
    paths["broken"] = web / "broken.jsp"
    paths["footer"] = web / "footer.jspf"
    paths["index"].write_text(INDEX_JSP, encoding="utf-8")
    paths["broken"].write_text(BROKEN_JSP, encoding="utf-8")
    paths["footer"].write_text(FOOTER_JSPF, encoding="utf-8")

    bean_dir = root / "src" / "com" / "example"
    bean_dir.mkdir(parents=True, exist_ok=True)
    paths["bean"] = bean_dir / "HomeBean.java"
    paths["bean"].write_text(HOME_BEAN_JAVA, encoding="utf-8")
    return paths


def _write_config(root: Path, text: str | None) -> None:
    clear_cache()
    if text is None:
        return
    cfg_dir = root / ".code-review-graph"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(text, encoding="utf-8")


def _seed_graph(root: Path, paths: dict[str, Path], *, with_broken: bool) -> GraphStore:
    """Pre-seed the File/Class nodes the resolver is contracted to read."""
    store = GraphStore(root / "graph.db")
    store.upsert_node(_file_node(paths["index"], "jsp", 7))
    if with_broken:
        store.upsert_node(_file_node(paths["broken"], "jsp", 3))
    store.upsert_node(_file_node(paths["footer"], "jsp", 1))
    # The java Class node a real parser build would have produced: the
    # qualified name the store derives is "<file_path>::<name>".
    store.upsert_node(NodeInfo(
        kind="Class",
        name="HomeBean",
        file_path=str(paths["bean"]),
        line_start=3,
        line_end=4,
        language="java",
    ))
    store.commit()
    return store


def _file_node(path: Path, language: str, line_end: int) -> NodeInfo:
    return NodeInfo(
        kind="File",
        name=str(path),
        file_path=str(path),
        line_start=1,
        line_end=line_end,
        language=language,
    )


def _snapshot(store: GraphStore) -> tuple[set[tuple], set[tuple]]:
    conn = store._conn
    nodes = {
        (row["kind"], row["name"], row["qualified_name"], row["file_path"], row["language"])
        for row in conn.execute(
            "SELECT kind, name, qualified_name, file_path, language FROM nodes"
        ).fetchall()
    }
    edges = {
        (row["kind"], row["source_qualified"], row["target_qualified"], row["file_path"], row["line"])
        for row in conn.execute(
            "SELECT kind, source_qualified, target_qualified, file_path, line "
            "FROM edges WHERE kind IN ('RENDERS', 'REQUESTS', 'INCLUDES', 'REFERENCES')"
        ).fetchall()
    }
    return nodes, edges


def _edge_rows(store: GraphStore, kind: str) -> list[dict]:
    return store._conn.execute(
        "SELECT kind, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges WHERE kind = ? ORDER BY line",
        (kind,),
    ).fetchall()


def test_derives_the_four_edge_kinds_from_preseeded_graph_nodes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root, CONFIG_TOML)
        paths = _write_repo(root)
        store = _seed_graph(root, paths, with_broken=False)
        try:
            result = resolve_jsp_links(store, root)

            assert result["files_indexed"] == 2  # index.jsp + footer.jspf
            assert result["bindings"] == 1  # @UrlBinding("/home")
            assert result["renders"] == 1
            assert result["requests"] == 1
            assert result["includes"] == 1
            assert result["references"] == 0

            bean_qn = f"{paths['bean']}::HomeBean"

            renders = _edge_rows(store, "RENDERS")
            assert [(r["source_qualified"], r["target_qualified"], r["line"]) for r in renders] == [
                (str(paths["index"]), bean_qn, 3),
            ]
            assert _extra(renders[0]) == {
                "fqn": "com.example.HomeBean", "resolution": "class",
            }

            requests_ = _edge_rows(store, "REQUESTS")
            assert [(r["source_qualified"], r["target_qualified"], r["line"]) for r in requests_] == [
                (str(paths["index"]), bean_qn, 4),
            ]
            assert _extra(requests_[0]) == {
                "fqn": "com.example.HomeBean", "resolution": "class",
                "route": "/home", "url": "/home",
            }

            includes = _edge_rows(store, "INCLUDES")
            assert [(r["source_qualified"], r["target_qualified"], r["line"]) for r in includes] == [
                (str(paths["index"]), str(paths["footer"]), 2),
            ]

            # The resolver manufactures nothing: exactly the seeded nodes remain
            # (index.jsp, footer.jspf, HomeBean).
            nodes, _ = _snapshot(store)
            assert len(nodes) == 3
        finally:
            store.close()
    print("OK: edges derived from pre-seeded File/Class nodes, no node creation")


def test_unresolved_beanclass_falls_back_to_the_raw_fqn():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root, CONFIG_TOML)
        paths = _write_repo(root)
        store = _seed_graph(root, paths, with_broken=True)
        try:
            result = resolve_jsp_links(store, root)

            assert result["renders"] == 2
            assert result["unresolved_targets"] == 1
            broken = [
                r for r in _edge_rows(store, "RENDERS")
                if r["source_qualified"] == str(paths["broken"])
            ]
            assert [r["target_qualified"] for r in broken] == ["com.example.GoneBean"]
            assert _extra(broken[0]) == {
                "fqn": "com.example.GoneBean", "resolution": "raw", "unresolved": True,
            }
        finally:
            store.close()
    print("OK: a beanclass with no Class node targets the raw FQN, flagged unresolved")


def test_running_twice_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root, CONFIG_TOML)
        paths = _write_repo(root)
        store = _seed_graph(root, paths, with_broken=True)
        try:
            first_stats = resolve_jsp_links(store, root)
            first = _snapshot(store)

            second_stats = resolve_jsp_links(store, root)
            second = _snapshot(store)

            assert first_stats == second_stats
            assert first == second
        finally:
            store.close()
    print("OK: re-running the resolver converges to the identical graph")


def test_no_config_runs_with_framework_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root, None)  # no .code-review-graph/config.toml at all
        paths = _write_repo(root)
        store = _seed_graph(root, paths, with_broken=False)
        try:
            result = resolve_jsp_links(store, root)
            # Defaults web_root="web" / source_root="src" describe the fixture,
            # so a missing config must still resolve every edge.
            assert result["files_indexed"] == 2
            assert result["bindings"] == 1
            assert result["renders"] == 1
            assert result["requests"] == 1
            assert result["includes"] == 1
        finally:
            store.close()
    print("OK: no [resolvers.jsp] section -> defaults, resolver still runs")


def test_enabled_false_returns_zero_stats_and_preserves_existing_edges():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root, DISABLED_TOML)
        paths = _write_repo(root)
        store = _seed_graph(root, paths, with_broken=False)
        survivor_qn = f"{paths['bean']}::HomeBean"
        try:
            store.upsert_edge(EdgeInfo(
                kind="RENDERS",
                source=str(paths["index"]),
                target=survivor_qn,
                file_path=str(paths["index"]),
                line=3,
            ))
            store.upsert_edge(EdgeInfo(
                kind="REFERENCES",
                source=str(paths["footer"]),
                target=str(paths["index"]),
                file_path=str(paths["footer"]),
                line=1,
                extra={"asset": "page"},
            ))
            store.commit()
            before = _snapshot(store)

            result = resolve_jsp_links(store, root)

            assert result == {
                "files_indexed": 0,
                "endpoints": 0,
                "bindings": 0,
                "renders": 0,
                "requests": 0,
                "includes": 0,
                "references": 0,
                "binds": 0,
                "styles": 0,
                "unresolved_references": 0,
                "unresolved_targets": 0,
            }
            assert _snapshot(store) == before
        finally:
            store.close()
    print("OK: enabled=false -> zero stats, pre-existing edges untouched")


def _extra(row) -> dict:
    import json

    return json.loads(row["extra"])


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"\n{len(tests)} jsp resolver tests passed")


if __name__ == "__main__":
    _run_all()
