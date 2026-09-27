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


@pytest.fixture
def clean_probes():
    from code_review_graph import parser

    parser._clear_parser_probe_cache()
    yield parser
    parser._clear_parser_probe_cache()


def test_grammars_are_probed_once_in_the_parent(clean_probes, monkeypatch):
    parser = clean_probes
    probed: list[str] = []
    monkeypatch.setattr(
        parser, "_run_parser_load_probe", lambda g, t: probed.append(g) or g != "nosuch",
    )
    assert parser.probe_grammars(["java", "python", "nosuch", "java"]) == {
        "java": True, "python": True, "nosuch": False,
    }
    assert parser.probe_grammars(["java", "python"]) == {"java": True, "python": True}
    assert sorted(probed) == ["java", "nosuch", "python"]


def test_seeded_workers_do_not_probe_and_warn_on_first_use(clean_probes, monkeypatch, caplog):
    parser = clean_probes
    monkeypatch.setattr(
        parser, "_run_parser_load_probe",
        lambda g, t: pytest.fail(f"worker probed {g} again"),
    )
    parser.seed_parser_probes({"java": True, "jsp": False})
    assert "Skipping" not in caplog.text
    assert parser._parser_load_probe_succeeds("java") is True
    assert parser._parser_load_probe_succeeds("jsp") is False
    assert parser._parser_load_probe_succeeds("jsp") is False
    assert caplog.text.count("Skipping unavailable tree-sitter parser for jsp") == 1


def test_process_pool_workers_start_with_the_parent_probes(monkeypatch):
    from code_review_graph import incremental, parser

    monkeypatch.setenv("CRG_PARSE_EXECUTOR", "process")
    with incremental._make_executor(1, {"java": True}) as executor:
        assert executor._initializer is parser.seed_parser_probes
        assert executor._initargs == ({"java": True},)


def test_parse_pool_probes_the_files_grammars(tmp_path, monkeypatch):
    from code_review_graph import incremental
    from code_review_graph.parser import CodeParser

    for index in range(10):
        (tmp_path / f"m{index}.py").write_text(f"def f{index}():\n    pass\n", encoding="utf-8")
    (tmp_path / "A.java").write_text("class A {}\n", encoding="utf-8")
    seen: list[list[str]] = []
    real = incremental.probe_grammars
    monkeypatch.setattr(
        incremental, "probe_grammars",
        lambda grammars: seen.append(list(grammars)) or real(grammars),
    )
    monkeypatch.setenv("CRG_PARSE_EXECUTOR", "thread")
    store = GraphStore(tmp_path / "graph.db")
    try:
        files = sorted(p.name for p in tmp_path.iterdir() if p.suffix in (".py", ".java"))
        outcome = incremental._parse_and_store(tmp_path, store, CodeParser(tmp_path), files)
    finally:
        store.close()
    assert outcome.parsed == 11
    assert seen == [["java", "python"]]


def _java(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_java_import_walk_stops_at_the_repository_root(tmp_path):
    from code_review_graph.parser import CodeParser

    _java(tmp_path / "com" / "outside" / "Leak.java", "package com.outside;\nclass Leak {}\n")
    repo = tmp_path / "repo"
    caller = _java(
        repo / "src" / "main" / "java" / "com" / "acme" / "App.java",
        "package com.acme;\nimport com.outside.Leak;\nimport static com.outside.Leak.x;\n"
        "class App {}\n",
    )
    walker = CodeParser(repo)
    assert walker._resolve_module_to_file("com.outside.Leak", str(caller), "java") is None
    assert walker._resolve_module_to_file("com.outside.Leak.x", str(caller), "java") is None


def test_java_tests_still_resolve_main_sources(tmp_path):
    from code_review_graph.parser import CodeParser

    repo = tmp_path / "repo"
    main = _java(
        repo / "src" / "main" / "java" / "com" / "acme" / "Money.java",
        "package com.acme;\nclass Money {}\n",
    )
    test = _java(
        repo / "src" / "test" / "java" / "com" / "acme" / "MoneyTest.java",
        "package com.acme;\nimport com.acme.Money;\nclass MoneyTest {}\n",
    )
    walker = CodeParser(repo)
    resolved = walker._resolve_module_to_file("com.acme.Money", str(test), "java")
    assert resolved == main.resolve().as_posix()


def _small_repo(root: Path) -> Path:
    root.mkdir()
    (root / "small.py").write_text("def small():\n    return 1\n", encoding="utf-8")
    (root / "big.py").write_text(
        "def big():\n" + "    x = 1\n" * 40 + "    return x\n", encoding="utf-8",
    )
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def _indexed(repo: Path) -> set[str]:
    store = open_store(repo)
    try:
        return {Path(path).name for path in store.get_all_files()}
    finally:
        store.close()


def test_oversized_files_are_skipped_and_reported(tmp_path, monkeypatch):
    repo = _small_repo(tmp_path / "repo")
    monkeypatch.setenv("CRG_MAX_FILE_BYTES", "200")
    result = build(repo)
    assert result["status"] == "ok"
    assert _indexed(repo) == {"small.py"}
    assert result["files_skipped"] == 1
    assert [entry["file"] for entry in result["skipped_files"]] == ["big.py"]
    assert result["skipped_files"][0]["bytes"] > 200


def test_a_file_that_grows_past_the_limit_leaves_the_graph(tmp_path, monkeypatch):
    repo = _small_repo(tmp_path / "repo")
    monkeypatch.setenv("CRG_MAX_FILE_BYTES", "200")
    build(repo)
    (repo / "small.py").write_text("def small():\n" + "    y = 2\n" * 40, encoding="utf-8")
    git(repo, "commit", "-qam", "grow")
    update = build(repo, full=False)
    assert update["build_type"] == "incremental"
    assert update["files_skipped"] == 1
    assert _indexed(repo) == set()


def test_default_file_size_limit_is_two_megabytes(monkeypatch):
    from code_review_graph import incremental

    monkeypatch.delenv("CRG_MAX_FILE_BYTES", raising=False)
    assert incremental._max_file_bytes() == 2 * 1024 * 1024
