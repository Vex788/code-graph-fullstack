"""A resolver that fails midway leaves the previous edges intact.

Each resolver is called directly, outside ``run_resolver``'s transaction, with
a fault injected after its first write statement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import graph as graph_module
from code_review_graph.event_resolver import resolve_spring_events
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.resolvers.jsp import resolve_jsp_links
from code_review_graph.spring_resolver import resolve_spring_di_calls

from .witness.conftest import copy_fixture

_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


class _InjectedFaultError(RuntimeError):
    pass


def _fail_after_first_write(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Let one write statement through, then raise on the next one."""
    seen = {"writes": 0}
    connection = graph_module._GraphConnection
    real_execute = connection.execute
    real_executemany = connection.executemany

    def guard(sql: str) -> None:
        if sql.lstrip().split(None, 1)[0].upper() in _WRITES:
            seen["writes"] += 1
            if seen["writes"] > 1:
                raise _InjectedFaultError("injected resolver fault")

    def execute(self, sql, *args, **kwargs):
        guard(sql)
        return real_execute(self, sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        guard(sql)
        return real_executemany(self, sql, *args, **kwargs)

    monkeypatch.setattr(connection, "execute", execute)
    monkeypatch.setattr(connection, "executemany", executemany)
    return seen


def _edges(store: GraphStore) -> list[tuple]:
    return store._conn.execute(
        "SELECT kind, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges ORDER BY kind, source_qualified, target_qualified, file_path, line, extra"
    ).fetchall()


def _snapshot(store: GraphStore) -> list[tuple]:
    return [tuple(row) for row in _edges(store)]


def _assert_rolled_back(repo: Path, run, monkeypatch: pytest.MonkeyPatch, prepare=None) -> None:
    store = GraphStore(get_db_path(repo))
    try:
        if prepare is not None:
            prepare(store)
            store._conn.commit()
        before = _snapshot(store)
        seen = _fail_after_first_write(monkeypatch)
        with pytest.raises(_InjectedFaultError):
            run(store)
        monkeypatch.undo()
        assert seen["writes"] > 1, "the resolver never reached a second write"
        assert _snapshot(store) == before, "a failed resolver left partial edges behind"
    finally:
        store.close()
    reopened = GraphStore(get_db_path(repo))
    try:
        assert _snapshot(reopened) == before
    finally:
        reopened.close()


def _unmark_spring_edges(store: GraphStore) -> None:
    """Make the already-resolved DI calls resolvable again."""
    store._conn.execute(
        "UPDATE edges SET extra = json_remove(extra, '$.spring_resolved') "
        "WHERE extra LIKE '%spring_resolved%'"
    )


@pytest.fixture
def built_repo(tmp_path: Path) -> Path:
    repo = copy_fixture(tmp_path / "app")
    store = GraphStore(get_db_path(repo))
    try:
        assert full_build(repo, store)["status"] == "ok"
    finally:
        store.close()
    return repo


def test_spring_resolver_failure_keeps_previous_edges(built_repo, monkeypatch):
    _assert_rolled_back(
        built_repo, resolve_spring_di_calls, monkeypatch, prepare=_unmark_spring_edges,
    )


def test_jsp_resolver_failure_keeps_previous_edges(built_repo, monkeypatch):
    _assert_rolled_back(
        built_repo, lambda store: resolve_jsp_links(store, built_repo), monkeypatch,
    )


def test_event_resolver_failure_keeps_previous_edges(tmp_path, monkeypatch):
    package = tmp_path / "alpha"
    package.mkdir()
    (package / "SharedEvent.java").write_text(
        "package alpha;\nclass SharedEvent {}\n", encoding="utf-8",
    )
    (package / "Publisher.java").write_text(
        "package alpha;\nclass Publisher {\n"
        "    void publish() { events.publishEvent(new SharedEvent()); }\n"
        "    void again() { events.publishEvent(new SharedEvent()); }\n}\n",
        encoding="utf-8",
    )
    (package / "Listener.java").write_text(
        "package alpha;\nimport org.springframework.context.event.EventListener;\n"
        "class Listener {\n    @EventListener\n    void on(SharedEvent event) {}\n}\n",
        encoding="utf-8",
    )
    store = GraphStore(get_db_path(tmp_path))
    try:
        full_build(tmp_path, store)
        assert any("spring_application_event" in (row[5] or "") for row in _edges(store))
    finally:
        store.close()
    _assert_rolled_back(tmp_path, resolve_spring_events, monkeypatch)
