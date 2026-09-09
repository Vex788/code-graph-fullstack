"""Tests for the orient / shortest_path_between / common_callers_of tools.

Builds a small real repository in a temp dir and runs the actual parser
over it (``full_build``), so qualified names and path shapes come from the
real graph rather than hand-seeded rows — this is what proves the
repo-relative path shortening actually works against real graph identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.tools import navigation

_WORKFLOW_PY = '''"""Fixture module: a tiny interlinked call graph for navigation tests."""


def entry_point():
    return middle_step()


def middle_step():
    return terminal_step()


def terminal_step():
    return 42


def helper_one():
    return 1


def helper_two():
    return 2


def orchestrate():
    helper_one()
    helper_two()
'''

_UNRELATED_PY = '''def standalone_function():
    return "isolated"
'''


def _noise_module(n: int = 40) -> str:
    """Generate filler call chains with degree > 2.

    shortest_path_between's degree-based hub damping excludes the top 32
    nodes by (in-degree + out-degree) from intermediate hops (see
    navigation.py). On a graph this small every real node would otherwise
    fall inside that floor and the hub filter would eat the very path the
    test is trying to find. This gives the graph 40+ higher-degree nodes so
    the fixture's own call chain (degree 1-2) sits below the cutoff, the way
    it would in a real repository.
    """
    lines = ["def orchestrator_noise():"]
    lines += [f"    hub_{i}()" for i in range(n)]
    lines.append("")
    for i in range(n):
        lines += [
            f"def hub_{i}():",
            f"    leaf_{i}a()",
            f"    leaf_{i}b()",
            "",
            f"def leaf_{i}a():",
            "    return 0",
            "",
            f"def leaf_{i}b():",
            "    return 0",
            "",
        ]
    return "\n".join(lines)


def _build_fixture_repo(root: Path) -> None:
    (root / ".git").mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "workflow.py").write_text(_WORKFLOW_PY, encoding="utf-8")
    (pkg / "unrelated.py").write_text(_UNRELATED_PY, encoding="utf-8")
    (pkg / "noise.py").write_text(_noise_module(), encoding="utf-8")


def _path_part(qualified_or_path: str) -> str:
    return qualified_or_path.partition("::")[0]


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = tmp_path / "fixture_repo"
    root.mkdir()
    _build_fixture_repo(root)
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        store.commit()
    finally:
        store.close()
    return root


class TestOrient:
    def test_real_symbol_returns_nonempty_top_functions(self, repo):
        result = navigation.orient("terminal_step", repo_root=str(repo))
        assert result["status"] == "ok"
        assert result["top_functions"]
        assert "terminal_step" in [f["name"] for f in result["top_functions"]]

    def test_results_are_repo_relative_not_absolute(self, repo):
        result = navigation.orient("terminal_step", repo_root=str(repo))
        for entry in result["top_functions"] + result["top_files"]:
            path_part = _path_part(entry["where"])
            assert not Path(path_part).is_absolute(), entry
            assert str(repo) not in entry["where"], entry

    def test_low_confidence_flag_for_near_zero_semantic_scores(self, repo, monkeypatch):
        # The optional "embeddings" extra (sentence-transformers) is not
        # installed in the base test environment, so real hybrid_search
        # never reports "semantic"/"hybrid" mode here. Stub it to exercise
        # the low_confidence threshold deterministically, the way a real
        # gibberish query against a real embedding index would.
        def fake_hybrid_search(store, query, limit=12, _out_mode=None, **kwargs):
            if _out_mode is not None:
                _out_mode.append("semantic")
            return [{
                "name": "terminal_step", "kind": "Function",
                "qualified_name": f"{repo}/pkg/workflow.py::terminal_step",
                "file_path": f"{repo}/pkg/workflow.py", "score": 0.001,
            }]

        monkeypatch.setattr(navigation, "hybrid_search", fake_hybrid_search)
        result = navigation.orient("zzqxjklw92384nonsense", repo_root=str(repo))
        assert result["low_confidence"] is True

    def test_confident_result_does_not_set_low_confidence(self, repo, monkeypatch):
        def fake_hybrid_search(store, query, limit=12, _out_mode=None, **kwargs):
            if _out_mode is not None:
                _out_mode.append("semantic")
            return [{
                "name": "terminal_step", "kind": "Function",
                "qualified_name": f"{repo}/pkg/workflow.py::terminal_step",
                "file_path": f"{repo}/pkg/workflow.py", "score": 0.91,
            }]

        monkeypatch.setattr(navigation, "hybrid_search", fake_hybrid_search)
        result = navigation.orient("terminal step handling", repo_root=str(repo))
        assert result["low_confidence"] is False


class TestShortestPathBetween:
    def test_finds_real_call_path(self, repo):
        result = navigation.shortest_path_between(
            "entry_point", "terminal_step", repo_root=str(repo),
        )
        assert result["status"] == "ok"
        assert result["paths"], result
        path = result["paths"][0]["path"]
        assert path[0].endswith("::entry_point")
        assert path[-1].endswith("::terminal_step")
        for hop in path:
            assert not Path(_path_part(hop)).is_absolute(), hop
            assert str(repo) not in hop, hop

    def test_reports_cleanly_when_no_path_exists(self, repo):
        result = navigation.shortest_path_between(
            "terminal_step", "standalone_function", repo_root=str(repo),
        )
        assert result["status"] == "no_path"
        assert result["paths"] == []
        assert "note" in result

    def test_unresolved_symbol_reports_ambiguous(self, repo):
        result = navigation.shortest_path_between(
            "totally_unknown_symbol_xyz", "terminal_step", repo_root=str(repo),
        )
        assert result["status"] == "ambiguous"


class TestCommonCallersOf:
    def test_finds_shared_caller(self, repo):
        result = navigation.common_callers_of(
            "helper_one", "helper_two", repo_root=str(repo),
        )
        assert result["status"] == "ok"
        assert result["count"] >= 1
        assert any(c.endswith("::orchestrate") for c in result["common_callers"])
        for caller in result["common_callers"]:
            assert not Path(_path_part(caller)).is_absolute(), caller
            assert str(repo) not in caller, caller

    def test_no_shared_caller_returns_empty(self, repo):
        result = navigation.common_callers_of(
            "terminal_step", "standalone_function", repo_root=str(repo),
        )
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["common_callers"] == []
