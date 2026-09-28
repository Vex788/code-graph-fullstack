"""Tests for code_review_graph/resolvers/stripes.py: the Stripes resolver.

The resolver creates one Endpoint node per ``@UrlBinding`` bean, HANDLES
edges from ``@HandlesEvent``/``@DefaultHandler`` methods and FORWARDS_TO
edges from ``ForwardResolution``/``RedirectResolution`` constructions. These
tests pre-seed the File/Class/Function nodes a real parser build produces
and run the resolver against real files on disk.

Plain assert-based; runnable directly with
`python3 tests/test_stripes_resolver.py` or under pytest.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.resolvers.stripes import resolve_stripes_actions

BEAN_JAVA = """package com.example.web;

import net.sourceforge.stripes.action.*;

@UrlBinding("/thing/Thing.action")
public class ThingActionBean {

    @DefaultHandler
    public Resolution view() {
        return new ForwardResolution("/WEB-INF/jsp/thing/view.jsp");
    }

    @HandlesEvent("save")
    public Resolution save() {
        return new RedirectResolution(ThingActionBean.class);
    }

    @HandlesEvent("back")
    public Resolution back() {
        return new RedirectResolution("/other/Other.action");
    }

    @HandlesEvent("gone")
    public Resolution gone() {
        return new ForwardResolution("/WEB-INF/jsp/thing/missing.jsp");
    }
}
"""

OTHER_JAVA = """package com.example.web;

@UrlBinding("/other/Other.action")
public class OtherActionBean {
    @DefaultHandler
    public Resolution view() {
        return new ForwardResolution("/WEB-INF/jsp/other/view.jsp");
    }
}
"""

PAGE = "<html>thing</html>\n"


def _write_repo(root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    bean_dir = root / "src" / "com" / "example" / "web"
    bean_dir.mkdir(parents=True)
    paths["bean"] = bean_dir / "ThingActionBean.java"
    paths["other"] = bean_dir / "OtherActionBean.java"
    paths["bean"].write_text(BEAN_JAVA, encoding="utf-8")
    paths["other"].write_text(OTHER_JAVA, encoding="utf-8")
    page_dir = root / "web" / "WEB-INF" / "jsp" / "thing"
    page_dir.mkdir(parents=True)
    paths["page"] = page_dir / "view.jsp"
    paths["page"].write_text(PAGE, encoding="utf-8")
    return paths


def _node(store: GraphStore, kind: str, name: str, path: Path, language: str,
          parent: str | None = None) -> None:
    store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=str(path), line_start=1, line_end=5,
        language=language, parent_name=parent,
    ))


def _seed_graph(root: Path, paths: dict[str, Path]) -> GraphStore:
    store = GraphStore(root / "graph.db")
    _node(store, "File", str(paths["page"]), paths["page"], "jsp")
    _node(store, "File", str(paths["bean"]), paths["bean"], "java")
    _node(store, "Class", "ThingActionBean", paths["bean"], "java")
    for method in ("view", "save", "back", "gone"):
        _node(store, "Function", method, paths["bean"], "java", "ThingActionBean")
    _node(store, "File", str(paths["other"]), paths["other"], "java")
    _node(store, "Class", "OtherActionBean", paths["other"], "java")
    _node(store, "Function", "view", paths["other"], "java", "OtherActionBean")
    store.commit()
    return store


def _edges(store: GraphStore, kind: str) -> dict[tuple[str, str], dict]:
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = ?",
        (kind,),
    ).fetchall()
    return {
        (row["source_qualified"], row["target_qualified"]):
            json.loads(row["extra"] or "{}")
        for row in rows
    }


def test_emits_endpoint_handles_and_every_resolution_form():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        paths = _write_repo(root)
        store = _seed_graph(root, paths)
        try:
            stats = resolve_stripes_actions(store, root)

            assert stats["beans"] == 2 and stats["endpoints"] == 2
            assert stats["handles"] == 5
            assert stats["forwards"] == 3
            assert stats["unresolved_resolutions"] == 2

            endpoints = store._conn.execute(
                "SELECT qualified_name, extra FROM nodes WHERE kind = 'Endpoint' "
                "ORDER BY qualified_name"
            ).fetchall()
            assert len(endpoints) == 2
            endpoint = next(
                e for e in endpoints
                if json.loads(e["extra"])["route"] == "/thing/Thing.action"
            )
            extra = json.loads(endpoint["extra"])
            assert extra["route"] == "/thing/Thing.action"
            assert extra["handler_qualified"].endswith("::ThingActionBean")

            bean_qn = f"{paths['bean']}::ThingActionBean"
            endpoint_qn = (
                f"{paths['bean']}::ThingActionBean."
                "ThingActionBean@UrlBinding[0:0] ANY /thing/Thing.action"
            )
            handles = _edges(store, "HANDLES")
            for method in ("view", "save", "back", "gone"):
                assert (f"{bean_qn}.{method}", endpoint_qn) in handles

            forwards = _edges(store, "FORWARDS_TO")
            assert (f"{bean_qn}.view", str(paths["page"])) in forwards
            assert (f"{bean_qn}.save", bean_qn) in forwards
            other_endpoint = (
                f"{paths['other']}::OtherActionBean."
                "OtherActionBean@UrlBinding[0:0] ANY /other/Other.action"
            )
            assert (f"{bean_qn}.back", other_endpoint) in forwards
        finally:
            store.close()
    print("OK: endpoint, handlers, forward/redirect class+route bindings emitted")


def test_running_twice_converges():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        paths = _write_repo(root)
        store = _seed_graph(root, paths)
        try:
            resolve_stripes_actions(store, root)
            first = store._conn.execute(
                "SELECT kind, source_qualified, target_qualified FROM edges "
                "WHERE kind IN ('HANDLES', 'FORWARDS_TO')"
            ).fetchall()
            nodes_first = store._conn.execute(
                "SELECT kind, name, qualified_name FROM nodes WHERE kind = 'Endpoint'"
            ).fetchall()

            resolve_stripes_actions(store, root)
            second = store._conn.execute(
                "SELECT kind, source_qualified, target_qualified FROM edges "
                "WHERE kind IN ('HANDLES', 'FORWARDS_TO')"
            ).fetchall()
            nodes_second = store._conn.execute(
                "SELECT kind, name, qualified_name FROM nodes WHERE kind = 'Endpoint'"
            ).fetchall()

            assert [tuple(r) for r in first] == [tuple(r) for r in second]
            assert [tuple(r) for r in nodes_first] == [tuple(r) for r in nodes_second]
        finally:
            store.close()
    print("OK: a second resolver run converges to the identical graph")


def test_repository_without_stripes_is_a_clean_noop():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "src").mkdir()
        store = GraphStore(root / "graph.db")
        try:
            stats = resolve_stripes_actions(store, root)
            assert stats == {
                "beans": 0, "endpoints": 0, "handles": 0, "forwards": 0,
                "unresolved_resolutions": 0,
            }
        finally:
            store.close()
    print("OK: no beans -> zero stats, clean no-op")


if __name__ == "__main__":
    test_emits_endpoint_handles_and_every_resolution_form()
    test_running_twice_converges()
    test_repository_without_stripes_is_a_clean_noop()
    print("\nALL PASSED")
