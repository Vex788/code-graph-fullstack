"""batch_query: many query_graph lookups over one store, compact per-item output."""

from __future__ import annotations

from pathlib import Path

import code_review_graph.tools.query as query_module
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.query import batch_query


def _seed(tmp_path: Path) -> Path:
    """target() is called by Svc.run, by a test, and by itself."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".code-review-graph").mkdir()
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    target = f"{root / 'core.py'}::target"
    nodes = [
        NodeInfo(kind="Function", name="target", file_path=str(root / "core.py"),
                 line_start=3, line_end=9, language="python"),
        NodeInfo(kind="Function", name="run", parent_name="Svc",
                 file_path=str(root / "svc.py"), line_start=5, line_end=8,
                 language="python"),
        NodeInfo(kind="Test", name="check_everything", file_path=str(root / "test_core.py"),
                 line_start=1, line_end=4, language="python", is_test=True),
        NodeInfo(kind="Function", name="helper_one", file_path=str(root / "a.py"),
                 line_start=1, line_end=2, language="python"),
        NodeInfo(kind="Function", name="helper_two", file_path=str(root / "b.py"),
                 line_start=1, line_end=2, language="python"),
    ]
    try:
        for node in nodes:
            store.upsert_node(node)
        for source, file in (
            (f"{root / 'svc.py'}::Svc.run", "svc.py"),
            (f"{root / 'test_core.py'}::check_everything", "test_core.py"),
            (target, "core.py"),
        ):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=source, target=target,
                file_path=str(root / file), line=2,
            ))
        store.upsert_edge(EdgeInfo(
            kind="TESTED_BY", source=target,
            target=f"{root / 'test_core.py'}::check_everything",
            file_path=str(root / "test_core.py"), line=1,
        ))
        store.commit()
    finally:
        store.close()
    return root


def test_one_store_is_opened_for_the_whole_batch(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    opened = []
    real_get_store = query_module._get_store

    def counting_get_store(repo_root=None):
        opened.append(repo_root)
        return real_get_store(repo_root)

    monkeypatch.setattr(query_module, "_get_store", counting_get_store)

    result = batch_query([
        {"pattern": "callers_of", "target": "target"},
        {"pattern": "callees_of", "target": "run"},
        {"pattern": "tests_for", "target": "target"},
    ], repo_root=str(root))

    assert len(opened) == 1
    assert [item["status"] for item in result["results"]] == ["ok", "ok", "ok"]


def test_mixed_item_statuses_never_fail_the_batch(tmp_path):
    root = _seed(tmp_path)

    result = batch_query([
        {"pattern": "callers_of", "target": "target"},
        {"pattern": "callers_of", "target": "no_such_symbol_xyz"},
        {"pattern": "bogus_pattern", "target": "target"},
        {"pattern": "callers_of", "target": "helper"},
        {"pattern": "callers_of"},
    ], repo_root=str(root))

    assert result["status"] == "ok"
    assert [item["status"] for item in result["results"]] == [
        "ok", "not_found", "error", "ambiguous", "error",
    ]
    assert result["results"][3]["candidates"] == ["a.py::helper_one:1", "b.py::helper_two:1"]
    assert result["summary"].startswith("5 queries: ")


def test_duplicate_queries_run_once(tmp_path):
    root = _seed(tmp_path)

    result = batch_query([
        {"pattern": "callers_of", "target": "target"},
        {"pattern": "callers_of", "target": "target"},
    ], repo_root=str(root))

    assert len(result["results"]) == 1


def test_batch_is_capped_at_25_and_reports_dropped(tmp_path):
    root = _seed(tmp_path)

    result = batch_query(
        [{"pattern": "callers_of", "target": f"missing_{i}"} for i in range(30)],
        repo_root=str(root),
    )

    assert len(result["results"]) == 25
    assert result["queries_dropped"] == 5


def test_prod_and_tests_are_split_and_self_edge_is_dropped(tmp_path):
    root = _seed(tmp_path)

    result = batch_query([
        {"pattern": "callers_of", "target": "target"},
        {"pattern": "tests_for", "target": "target"},
    ], repo_root=str(root))

    callers, tests = result["results"]
    assert callers["resolved"] == "core.py::target:3"
    assert callers["prod"] == ["Svc.run:5"]
    assert callers["prod_count"] == 1
    assert callers["tests"] == 1
    assert callers["self_call"] is True
    assert tests["tests"] == 1
    assert tests["test_names"] == ["test_core.py::check_everything:1"]


def test_helper_in_test_file_counts_as_test_not_prod(tmp_path):
    root = _seed(tmp_path)
    helper_file = root / "tests" / "stubs.py"
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        store.upsert_node(NodeInfo(
            kind="Function", name="stub_dao", file_path=str(helper_file),
            line_start=4, line_end=6, language="python",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{helper_file}::stub_dao",
            target=f"{root / 'core.py'}::target", file_path=str(helper_file), line=5,
        ))
        store.commit()
    finally:
        store.close()

    result = batch_query([{"pattern": "callers_of", "target": "target"}], repo_root=str(root))

    callers = result["results"][0]
    assert callers["prod"] == ["Svc.run:5"]
    assert callers["tests"] == 2


def test_missing_graph_returns_not_ready_without_creating_database(tmp_path):
    repo = tmp_path / "cold"
    (repo / ".git").mkdir(parents=True)

    result = batch_query(
        [{"pattern": "callers_of", "target": "target"}], repo_root=str(repo),
    )

    assert result["status"] == "not_ready"
    assert result["reason"] == "missing_graph"
    assert not (repo / ".code-review-graph").exists()
