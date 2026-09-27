"""Witnesses for Java 17 / Hibernate correctness on the fullstack fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import open_store, rel

DAO = "src/main/java/com/acme/dao"
USER_DAO_SAVE = f"{DAO}/UserDao.java::UserDao.save"


def _rows(repo: Path, sql: str, params: tuple = ()) -> list[tuple]:
    store = open_store(repo)
    try:
        return [tuple(row) for row in store._conn.execute(sql, params).fetchall()]
    finally:
        store.close()


def test_callers_control_finds_real_callers(built_fixture: Path):
    from code_review_graph.tools.query import query_graph

    result = query_graph(
        pattern="callers_of", target=USER_DAO_SAVE, repo_root=str(built_fixture),
    )
    callers = {rel(built_fixture, r["qualified_name"]) for r in result["results"]}
    assert "src/main/java/com/acme/service/UserService.java::UserService.register" in callers


@pytest.mark.xfail(strict=True, reason="W3a: session.save() binds to any repo method named save")
def test_hibernate_session_save_is_not_a_caller_of_user_dao(built_fixture: Path):
    from code_review_graph.tools.query import query_graph

    result = query_graph(
        pattern="callers_of", target=USER_DAO_SAVE, repo_root=str(built_fixture),
    )
    callers = {rel(built_fixture, r["qualified_name"]) for r in result["results"]}
    false_callers = callers & {
        "src/main/java/com/acme/audit/AuditLogWriter.java::AuditLogWriter.write",
        "src/main/java/com/acme/dao/OrderDao.java::OrderDao.persist",
    }
    assert false_callers == set()


@pytest.mark.xfail(strict=True, reason="W3a: Java overloads collapse into one node")
def test_user_dao_save_overloads_are_distinct_nodes(built_fixture: Path):
    rows = _rows(
        built_fixture,
        "SELECT qualified_name FROM nodes WHERE kind = 'Function' AND name = 'save' "
        "AND file_path = ?",
        (str(built_fixture / DAO / "UserDao.java"),),
    )
    assert len(rows) == 2, rows


@pytest.mark.xfail(strict=True, reason="W3a: Java records get no Class node")
def test_record_money_has_class_node(built_fixture: Path):
    rows = _rows(
        built_fixture,
        "SELECT qualified_name FROM nodes WHERE kind = 'Class' AND name = 'Money'",
    )
    assert len(rows) == 1


@pytest.mark.xfail(strict=True, reason="W3a: interface extends emits no INHERITS edge")
def test_interface_extends_emits_inherits(built_fixture: Path):
    rows = _rows(
        built_fixture,
        "SELECT target_qualified FROM edges WHERE kind = 'INHERITS' AND source_qualified = ?",
        (str(built_fixture / DAO / "UserDaoApi.java") + "::UserDaoApi",),
    )
    assert any(target.endswith("GenericDao") for (target,) in rows), rows


def test_test_detection_control_marks_maven_test_file(built_fixture: Path):
    rows = _rows(
        built_fixture,
        "SELECT is_test FROM nodes WHERE kind = 'File' AND file_path = ?",
        (str(built_fixture / "src/test/java/com/acme/dao/UserDaoTest.java"),),
    )
    assert rows == [(1,)]


@pytest.mark.xfail(strict=True, reason="W3a: a com/acme/latest/ path is classified as test")
def test_latest_package_is_production_code(built_fixture: Path):
    rows = _rows(
        built_fixture,
        "SELECT file_path, is_test FROM nodes WHERE kind = 'File' AND file_path LIKE ?",
        (str(built_fixture / "src/main/java/com/acme/latest") + "/%",),
    )
    assert len(rows) == 2
    assert [rel(built_fixture, path) for path, is_test in rows if is_test] == []
