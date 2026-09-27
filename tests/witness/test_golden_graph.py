"""Golden graph for the fullstack Stripes fixture (``expected_edges.tsv``).

``now`` rows describe edges the current code already produces correctly and
must keep producing. Each ``target`` kind is one xfail(strict) test: when the
wave that adds those edges lands, the test XPASSes, fails the suite, and the
fixer removes its entry from ``_TARGET_REASONS``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ._golden import GraphEdges, load_rows
from .conftest import FIXTURE_SRC, GOLDEN_TSV, open_store

_TARGET_REASONS = {
    "HANDLES": "W5a: @HandlesEvent/@DefaultHandler bind to the @UrlBinding endpoint",
    "FORWARDS_TO": "W5a: ForwardResolution/RedirectResolution edges",
    "REQUESTS": "W5a: jQuery $.get/$.post/$.getJSON, ctx + '/...', and .action links",
    "RENDERS": "W5a: jsp resolver skips first-party pages under a vendor/ directory",
    "INCLUDES": "W5a: jsp resolver skips first-party pages under a vendor/ directory",
    "REFERENCES": "W5a: jsp resolver skips first-party pages under a vendor/ directory",
    "BINDS": "W5a: form name= binds to the ActionBean property",
    "USES_STYLE": "W5a: class= links to the CSS selector",
    "MAPS_TO": "W5a: @Entity/@Table and *.hbm.xml map to Table nodes",
}

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
