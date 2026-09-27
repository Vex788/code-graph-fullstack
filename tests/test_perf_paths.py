"""Performance paths keep their results: JSP caching, parser probes, size limits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.resolvers import jsp

from .witness.conftest import build, copy_fixture, git, open_store

_JSP_KINDS = "('RENDERS', 'REQUESTS', 'INCLUDES', 'REFERENCES')"


def _jsp_edges(store: GraphStore) -> list[tuple]:
    return sorted(
        tuple(row) for row in store._conn.execute(
            "SELECT kind, source_qualified, target_qualified, file_path, line, extra "
            f"FROM edges WHERE kind IN {_JSP_KINDS}"  # nosec B608
        )
    )


def _count_reads(monkeypatch) -> list[str]:
    reads: list[str] = []
    real = jsp._read

    def counting(path):
        reads.append(str(path))
        return real(path)

    monkeypatch.setattr(jsp, "_read", counting)
    return reads


@pytest.fixture
def built_repo(tmp_path: Path) -> Path:
    repo = copy_fixture(tmp_path / "app")
    assert build(repo)["status"] == "ok"
    return repo


def _from_scratch(store: GraphStore, repo: Path) -> list[tuple]:
    """The resolver's edges with no cached state, for comparison."""
    store.delete_metadata(jsp._STATE_KEY)
    jsp.resolve_jsp_links(store, repo)
    return _jsp_edges(store)


def test_rerun_reads_no_unchanged_file(built_repo, monkeypatch):
    store = open_store(built_repo)
    try:
        before = _jsp_edges(store)
        assert before, "fixture has no JSP edges"
        reads = _count_reads(monkeypatch)
        jsp.resolve_jsp_links(store, built_repo)
        assert reads == []
        assert _jsp_edges(store) == before
    finally:
        store.close()


def test_changed_page_is_reread_and_rebound(built_repo, monkeypatch):
    pages = sorted((built_repo / "web").rglob("*.jsp"))
    page = pages[0]
    page.write_text(
        page.read_text(encoding="utf-8")
        + '\n<a href="/order.action">again</a>\n<form action="/vendor/view"></form>\n',
        encoding="utf-8",
    )
    git(built_repo, "commit", "-qam", "edit page")
    reads = _count_reads(monkeypatch)
    update = build(built_repo, full=False)
    assert update["status"] == "ok"
    assert [Path(p).name for p in reads if p.endswith(".jsp")] == [page.name]
    store = open_store(built_repo)
    try:
        incremental = _jsp_edges(store)
        assert incremental == _from_scratch(store, built_repo)
    finally:
        store.close()


def test_target_change_rebinds_without_rereading_pages(built_repo, monkeypatch):
    store = open_store(built_repo)
    try:
        first = _jsp_edges(store)
        # A class the pages bind to disappears: bindings must fall back.
        target = next(row[2] for row in first if row[0] == "RENDERS" and "::" in row[2])
        store._conn.execute("DELETE FROM nodes WHERE qualified_name = ?", (target,))
        store._conn.commit()
        reads = _count_reads(monkeypatch)
        jsp.resolve_jsp_links(store, built_repo)
        assert not [p for p in reads if p.endswith((".jsp", ".html", ".js"))]
        rebound = _jsp_edges(store)
        assert rebound != first
        assert rebound == _from_scratch(store, built_repo)
    finally:
        store.close()


def test_ignored_java_sources_bind_no_routes(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "index.jsp").write_text(
        '<form action="/hidden"></form>\n<form action="/shown"></form>\n', encoding="utf-8",
    )
    for folder, name, route in (("gen", "Hidden", "/hidden"), ("app", "Shown", "/shown")):
        source = repo / "src" / folder / f"{name}.java"
        source.parent.mkdir(parents=True)
        source.write_text(
            f'package x;\n@UrlBinding("{route}")\npublic class {name} {{}}\n', encoding="utf-8",
        )
    (repo / ".code-review-graphignore").write_text("src/gen/**\n", encoding="utf-8")
    store = GraphStore(tmp_path / "graph.db")
    try:
        from code_review_graph.parser import NodeInfo

        page = str((repo / "web" / "index.jsp").resolve())
        store.upsert_node(NodeInfo(
            kind="File", name=page, file_path=page, line_start=1, line_end=2, language="jsp",
        ))
        store.commit()
        result = jsp.resolve_jsp_links(store, repo)
        targets = {row[2] for row in _jsp_edges(store) if row[0] == "REQUESTS"}
    finally:
        store.close()
    assert result["bindings"] == 1
    assert targets == {"x.Shown"}


def test_line_numbers_match_a_plain_count():
    text = "a\nbb\n\nccc <x>\n<y>"
    lines = jsp._Lines(text)
    for index in range(len(text) + 1):
        assert lines.of(index) == text.count("\n", 0, index) + 1


def test_state_is_dropped_when_the_config_changes(built_repo, monkeypatch):
    store = open_store(built_repo)
    try:
        state = json.loads(store.get_metadata(jsp._STATE_KEY))
        assert state["pages"]
        store.set_metadata(
            jsp._STATE_KEY, json.dumps({**state, "config": "something else"}),
        )
        reads = _count_reads(monkeypatch)
        jsp.resolve_jsp_links(store, built_repo)
        assert any(p.endswith(".jsp") for p in reads)
    finally:
        store.close()
