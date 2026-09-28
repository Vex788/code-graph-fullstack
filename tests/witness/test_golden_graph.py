"""Golden graph for the fullstack Stripes fixture (``expected_edges.tsv``).

``now`` rows describe edges the current code produces and must keep
producing — the fullstack resolvers (Stripes actions, Hibernate mappings and
the extended JSP linker) all feed these, so a regression in any of them
fails here.

Every row carries an executed derivation: none is aspirational any more, so
there is no xfail scaffolding left to maintain.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ._golden import GraphEdges, load_rows
from .conftest import FIXTURE_SRC, GOLDEN_TSV, open_store

_TARGET_REASONS: dict[str, str] = {}

ROWS = load_rows(GOLDEN_TSV)
TARGET_KINDS = sorted({row.kind for row in ROWS if row.status == "target"})


def _generator():
    path = FIXTURE_SRC.parent / "fullstack_stripes_gen.py"
    spec = importlib.util.spec_from_file_location("fullstack_stripes_gen", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def graph_edges(built_fixture: Path) -> GraphEdges:
    store = open_store(built_fixture)
    try:
        return GraphEdges(store, built_fixture)
    finally:
        store.close()


def test_committed_fixture_matches_generator():
    assert _generator().drift() == []


def test_generator_scale_is_deterministic():
    gen = _generator()
    first = gen.build_files(scale=300)
    assert first == gen.build_files(scale=300)
    assert 300 <= len(first) < 300 + gen.MODULE_FILE_COUNT


def test_every_target_kind_has_a_reason():
    assert set(TARGET_KINDS) <= set(_TARGET_REASONS)


def test_now_rows_are_present(graph_edges: GraphEdges):
    missing = [str(row) for row in ROWS if row.status == "now" and not graph_edges.has(row)]
    assert missing == []


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(kind, marks=pytest.mark.xfail(strict=True, reason=_TARGET_REASONS[kind]))
        if kind in _TARGET_REASONS
        else kind
        for kind in TARGET_KINDS
    ],
)
def test_target_rows_are_present(graph_edges: GraphEdges, kind: str):
    rows = [row for row in ROWS if row.status == "target" and row.kind == kind]
    missing = [str(row) for row in rows if not graph_edges.has(row)]
    assert missing == []
