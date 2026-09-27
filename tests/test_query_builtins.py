"""callers_of skips JS builtin names only when no other language defines them."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph


def _repo(tmp_path: Path, language: str, file: str, name: str) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".code-review-graph").mkdir()
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        target = str(root / file)
        store.upsert_node(NodeInfo(
            kind="Function", name=name, parent_name="Dao", file_path=target,
            line_start=1, line_end=2, language=language,
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name="run", parent_name="Service",
            file_path=str(root / f"Service{Path(file).suffix}"),
            line_start=1, line_end=2, language=language,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{root / ('Service' + Path(file).suffix)}::Service.run",
            target=f"{target}::Dao.{name}",
            file_path=str(root / f"Service{Path(file).suffix}"), line=1,
        ))
        store.commit()
    finally:
        store.close()
    return root


def test_java_method_named_like_a_js_builtin_has_callers(tmp_path):
    for index, name in enumerate(("get", "update", "set")):
        root = _repo(tmp_path / f"case{index}", "java", "Dao.java", name)
        result = query_graph("callers_of", name, str(root))
        assert [r["name"] for r in result.get("results", [])] == ["run"], (name, result)


def test_js_builtin_name_is_still_skipped_for_js(tmp_path):
    root = _repo(tmp_path, "javascript", "dao.js", "map")
    result = query_graph("callers_of", "map", str(root))
    assert result["results"] == []
    assert "builtin" in result["summary"]
