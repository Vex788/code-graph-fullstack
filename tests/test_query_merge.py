"""query_graph answers same-named candidates instead of returning ``ambiguous``."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph


def _make_repo(tmp_path: Path) -> tuple[Path, GraphStore]:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".code-review-graph").mkdir()
    return root, GraphStore(root / ".code-review-graph" / "graph.db")


def _function(root: Path, file: str, name: str, parent: str | None = None,
              language: str = "java", line: int = 10) -> NodeInfo:
    return NodeInfo(
        kind="Function", name=name, parent_name=parent,
        file_path=str(root / file), line_start=line, line_end=line + 5,
        language=language,
    )


def test_same_method_in_two_java_classes_is_answered_per_candidate(tmp_path):
    root, store = _make_repo(tmp_path)
    try:
        for cls in ("SAPItemService", "SAPUOMGroupBuilder"):
            store.upsert_node(_function(root, f"{cls}.java", "createGroup", cls))
            store.upsert_node(_function(root, f"{cls}Caller.java", "run", f"{cls}Caller"))
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=f"{root / (cls + 'Caller.java')}::{cls}Caller.run",
                target=f"{root / (cls + '.java')}::{cls}.createGroup",
                file_path=str(root / f"{cls}Caller.java"), line=12,
            ))
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "createGroup", str(root))

    assert result["status"] == "ok"
    assert result["resolution"] == "per_candidate"
    assert {(g["parent_name"], g["result_count"]) for g in result["groups"]} == {
        ("SAPItemService", 1), ("SAPUOMGroupBuilder", 1),
    }
    assert {(r["parent_name"], r["via"].rsplit("::", 1)[-1]) for r in result["results"]} == {
        ("SAPItemServiceCaller", "SAPItemService.createGroup"),
        ("SAPUOMGroupBuilderCaller", "SAPUOMGroupBuilder.createGroup"),
    }
    assert len(result["candidates"]) == 2


def test_name_only_fallback_callers_are_tagged_once_and_not_counted(tmp_path):
    root, store = _make_repo(tmp_path)
    try:
        for module in ("a.py", "b.py"):
            store.upsert_node(_function(root, module, "helper", language="python"))
        store.upsert_node(_function(root, "c.py", "caller", language="python"))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{root / 'c.py'}::caller", target="helper",
            file_path=str(root / "c.py"), line=12,
        ))
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "helper", str(root))

    assert result["resolution"] == "per_candidate"
    assert [(r["name"], r["via"]) for r in result["results"]] == [("caller", "name_only")]
    assert [g["result_count"] for g in result["groups"]] == [0, 0]
    assert result["result_count"] == 1
    assert len(result["edges"]) == 1


def test_cpp_overload_set_unions_callers_including_ambiguous_calls(tmp_path):
    source_path = tmp_path / "IWorkspace.cpp"
    source_path.write_text(
        "void process(int value) {}\n"
        "void process(double value) {}\n"
        "void caller() { process(1); }\n",
        encoding="utf-8",
    )
    nodes, edges = CodeParser().parse_file(source_path)
    (tmp_path / ".code-review-graph").mkdir()
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    store.store_file_nodes_edges(str(source_path), nodes, edges)
    store.close()
    prefix = source_path.as_posix()

    result = query_graph(
        "callers_of", "process", repo_root=str(tmp_path), detail_level="minimal",
    )

    assert result["status"] == "ok"
    assert result["resolution"] == "overload_set"
    assert {g["qualified_name"] for g in result["groups"]} == {
        f"{prefix}::process(int)", f"{prefix}::process(double)",
    }
    assert result["results"] == [{
        "name": "caller", "kind": "Function", "file_path": prefix,
        "via": "ambiguous_overload",
    }]


def test_more_than_five_same_named_candidates_stay_ambiguous(tmp_path):
    root, store = _make_repo(tmp_path)
    try:
        for index in range(6):
            store.upsert_node(_function(root, f"m{index}.py", "process", language="python"))
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "process", str(root))

    assert result["status"] == "ambiguous"
    assert len(result["disambiguation"]) == 6


def test_fuzzy_name_candidates_stay_ambiguous(tmp_path):
    root, store = _make_repo(tmp_path)
    try:
        store.upsert_node(_function(root, "a.py", "process", language="python"))
        store.upsert_node(_function(root, "b.py", "process_data", language="python"))
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "process", str(root))

    assert result["status"] == "ambiguous"
    assert "resolution" not in result


def test_children_of_is_never_merged(tmp_path):
    root, store = _make_repo(tmp_path)
    try:
        for file in ("a/Handler.java", "b/Handler.java"):
            store.upsert_node(NodeInfo(
                kind="Class", name="Handler", file_path=str(root / file),
                line_start=1, line_end=20, language="java",
            ))
        store.commit()
    finally:
        store.close()

    result = query_graph("children_of", "Handler", str(root))

    assert result["status"] == "ambiguous"
