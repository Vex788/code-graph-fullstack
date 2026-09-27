"""Java 17 constructs, Java method identity, and type resolution in the parser."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.parser import CodeParser, NodeInfo


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _parse(root: Path, path: Path):
    return CodeParser(root).parse_file(path)


def _qn(path: Path, tail: str) -> str:
    return f"{path.as_posix()}::{tail}"


SRC = "src/main/java/com/acme"


class TestJava17Constructs:
    def test_record_is_a_class_containing_its_methods(self, tmp_path):
        path = _write(tmp_path, f"{SRC}/Money.java", (
            "package com.acme;\n"
            "public record Money(long cents, String currency) implements Comparable<Money> {\n"
            "    public Money { java.util.Objects.requireNonNull(currency); }\n"
            "    public Money plus(Money other) { return other; }\n"
            "    public int compareTo(Money o) { return 0; }\n"
            "}\n"
        ))
        nodes, edges = _parse(tmp_path, path)
        classes = [n for n in nodes if n.kind == "Class"]
        assert [(c.name, c.parent_name) for c in classes] == [("Money", None)]
        contains = {(e.source, e.target) for e in edges if e.kind == "CONTAINS"}
        money = _qn(path, "Money")
        assert (money, _qn(path, "Money.plus")) in contains
        assert (money, _qn(path, "Money.compareTo")) in contains
        # compact canonical constructor
        assert (money, _qn(path, "Money.Money")) in contains
        inherits = {e.target for e in edges if e.kind == "INHERITS"}
        assert inherits == {"Comparable"}

    def test_annotation_type_is_a_class(self, tmp_path):
        path = _write(tmp_path, f"{SRC}/Audited.java", (
            "package com.acme;\n"
            "public @interface Audited { String value() default \"\"; }\n"
        ))
        nodes, _ = _parse(tmp_path, path)
        assert [(n.kind, n.name) for n in nodes if n.kind == "Class"] == [("Class", "Audited")]

    def test_interface_extends_is_inherits_with_erased_generics(self, tmp_path):
        generic = _write(tmp_path, f"{SRC}/dao/GenericDao.java", (
            "package com.acme.dao;\npublic interface GenericDao<T> { void save(T t); }\n"
        ))
        api = _write(tmp_path, f"{SRC}/dao/UserDaoApi.java", (
            "package com.acme.dao;\n"
            "import com.acme.model.User;\n"
            "public interface UserDaoApi extends GenericDao<User>, java.io.Serializable {}\n"
        ))
        _, edges = _parse(tmp_path, api)
        inherits = sorted(e.target for e in edges if e.kind == "INHERITS")
        assert inherits == sorted([_qn(generic.resolve(), "GenericDao"), "java.io.Serializable"])


class TestJavaTypeResolution:
    def test_bases_resolve_through_imports_package_and_same_file(self, tmp_path):
        base = _write(tmp_path, f"{SRC}/core/Base.java", (
            "package com.acme.core;\npublic abstract class Base {}\n"
        ))
        sibling = _write(tmp_path, f"{SRC}/web/Handler.java", (
            "package com.acme.web;\npublic interface Handler {}\n"
        ))
        outer = _write(tmp_path, f"{SRC}/core/Outer.java", (
            "package com.acme.core;\npublic class Outer { public interface Inner {} }\n"
        ))
        path = _write(tmp_path, f"{SRC}/web/Page.java", (
            "package com.acme.web;\n"
            "import com.acme.core.Base;\n"
            "import com.acme.core.Outer.Inner;\n"
            "import net.sourceforge.stripes.action.ActionBean;\n"
            "public class Page extends Base implements Handler, Local, Inner, ActionBean {}\n"
            "interface Local {}\n"
        ))
        _, edges = _parse(tmp_path, path)
        inherits = {e.target for e in edges if e.kind == "INHERITS"}
        assert inherits == {
            _qn(base.resolve(), "Base"),
            _qn(sibling.resolve(), "Handler"),
            _qn(path, "Local"),
            _qn(outer.resolve(), "Outer.Inner"),
            "ActionBean",
        }


_SESSION_DAO = (
    "package com.acme.dao;\n"
    "import org.hibernate.Session;\n"
    "public class OrderDao {\n"
    "    private Session session;\n"
    "    public void save(Object o) { }\n"
    "    public void persist(Object o) {\n"
    "        session.save(o);\n"
    "        verify(session).save(o);\n"
    "        save(o);\n"
    "    }\n"
    "    static Session verify(Session s) { return s; }\n"
    "}\n"
)


class TestJavaReceivers:
    def test_library_receiver_is_marked_external(self, tmp_path):
        path = _write(tmp_path, f"{SRC}/dao/OrderDao.java", _SESSION_DAO)
        _, edges = _parse(tmp_path, path)
        saves = sorted(
            (e.line, e.target, e.extra.get("receiver_external"), e.extra.get("receiver_expression"))
            for e in edges if e.kind == "CALLS" and e.target.endswith("save")
        )
        assert saves == [
            (7, "save", True, None),
            (8, "save", None, True),
            (9, _qn(path, "OrderDao.save"), None, None),
        ]

    def test_receiver_edges_never_bind_by_bare_name(self, tmp_path, monkeypatch):
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build

        monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
        path = _write(tmp_path, f"{SRC}/dao/OrderDao.java", _SESSION_DAO)
        with GraphStore(tmp_path / "graph.db") as store:
            assert full_build(tmp_path, store)["errors"] == []
            store.resolve_bare_call_targets()
            store.commit()
            callers = {
                (e.source_qualified, e.line)
                for e in store.get_edges_by_target(_qn(path, "OrderDao.save"))
                if e.kind == "CALLS"
            }
        assert callers == {(_qn(path, "OrderDao.persist"), 9)}

    def test_callers_of_fallback_skips_typed_member_calls(self, tmp_path, monkeypatch):
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path
        from code_review_graph.tools.query import query_graph

        monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
        path = _write(tmp_path, f"{SRC}/dao/OrderDao.java", _SESSION_DAO)
        _write(tmp_path, f"{SRC}/audit/Writer.java", (
            "package com.acme.audit;\n"
            "import org.hibernate.Session;\n"
            "public class Writer {\n"
            "    public void write(Session session) { session.save(this); }\n"
            "}\n"
        ))
        db = get_db_path(tmp_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db) as store:
            assert full_build(tmp_path, store)["errors"] == []
        result = query_graph("callers_of", _qn(path, "OrderDao.save"), repo_root=str(tmp_path))
        callers = {r["qualified_name"] for r in result["results"]}
        assert callers == {_qn(path, "OrderDao.persist")}


def test_generation_one_index_requires_rebuild_via_cli(tmp_path):
    import os
    import subprocess
    import sys

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path
    from code_review_graph.locking import EXIT_REBUILD_REQUIRED
    from code_review_graph.migrations import INDEX_GENERATION

    assert INDEX_GENERATION == 2
    repo = tmp_path / "app"
    path = _write(repo, f"{SRC}/dao/OrderDao.java", _SESSION_DAO)
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run([*git, *args], cwd=repo, check=True, capture_output=True)
    env = {**os.environ, "CRG_SERIAL_PARSE": "1"}

    def cli(command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "code_review_graph", command, "--repo", str(repo)],
            env=env, capture_output=True, text=True, timeout=300,
        )

    assert cli("build").returncode == 0
    with GraphStore(get_db_path(repo)) as store:
        store.set_metadata("index_generation", "1")
    path.write_text(_SESSION_DAO.replace("save(o);\n    }", "save(o);\n        save(o);\n    }"))
    subprocess.run([*git, "commit", "-qam", "edit"], cwd=repo, check=True, capture_output=True)

    stale = cli("update")
    assert stale.returncode == EXIT_REBUILD_REQUIRED, stale.stdout + stale.stderr
    rebuilt = cli("build")
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    with GraphStore(get_db_path(repo)) as store:
        assert store.get_metadata("index_generation") == "2"
    assert cli("update").returncode == 0


_OVERLOADED_DAO = (
    "package com.acme.dao;\n"
    "import java.util.List;\n"
    "public class UserDao {\n"
    "    public void save(User u) { save(u, false); }\n"
    "    public void save(User u, boolean flush) { }\n"
    "    public void saveAll(java.util.List<User>[] batches, String... tags) { }\n"
    "    public void load(Long id) { }\n"
    "    public Object sorted() {\n"
    "        Runnable first = new Runnable() {\n"
    "            public void run() { load(1L); }\n"
    "            Object inner = new Object() { public String toString() { return \"\"; } };\n"
    "        };\n"
    "        return new java.util.Comparator<User>() {\n"
    "            public int compare(User a, User b) { return 0; }\n"
    "        };\n"
    "    }\n"
    "}\n"
)

_USER_SERVICE = (
    "package com.acme.service;\n"
    "import com.acme.dao.UserDao;\n"
    "public class UserService {\n"
    "    private UserDao userDao;\n"
    "    public void register(User user) { userDao.save(user); }\n"
    "    public void registerAndFlush(User user) { userDao.save(user, true); }\n"
    "    public void load() { userDao.load(1L); }\n"
    "}\n"
)


class TestJavaIdentity:
    def test_only_overloads_carry_erased_signatures(self, tmp_path):
        path = _write(tmp_path, f"{SRC}/dao/UserDao.java", _OVERLOADED_DAO)
        nodes, _ = _parse(tmp_path, path)
        functions = {n.identity_name or n.name for n in nodes if n.kind == "Function"}
        assert {"save(User)", "save(User,boolean)", "saveAll", "load", "sorted"} <= functions

    def test_erased_parameter_types(self):
        from tree_sitter_language_pack import get_parser

        source = b"class A { void m(java.util.List<String>[] a, int... r, Map.Entry<K,V> e) {} }"
        tree = get_parser("java").parse(source)
        method = tree.root_node.children[0].child_by_field_name("body").named_children[0]
        assert CodeParser._java_parameter_types(method) == ["List[]", "int[]", "Entry"]

    def test_anonymous_classes_are_numbered_per_enclosing_class(self, tmp_path):
        path = _write(tmp_path, f"{SRC}/dao/UserDao.java", _OVERLOADED_DAO)
        nodes, edges = _parse(tmp_path, path)
        classes = sorted(n.name for n in nodes if n.kind == "Class")
        assert classes == ["UserDao", "UserDao$1", "UserDao$1$1", "UserDao$2"]
        contains = {(e.source, e.target) for e in edges if e.kind == "CONTAINS"}
        assert (_qn(path, "UserDao"), _qn(path, "UserDao$2")) in contains
        assert (_qn(path, "UserDao$2"), _qn(path, "UserDao$2.compare")) in contains
        assert (_qn(path, "UserDao$1"), _qn(path, "UserDao$1$1")) in contains
        calls = {(e.source, e.target) for e in edges if e.kind == "CALLS"}
        # the body's calls belong to the anonymous class, not to sorted()
        assert (_qn(path, "UserDao$1.run"), _qn(path, "UserDao.load")) in calls

    def test_same_file_overload_binds_by_arity(self, tmp_path):
        path = _write(tmp_path, f"{SRC}/dao/UserDao.java", _OVERLOADED_DAO)
        _, edges = _parse(tmp_path, path)
        calls = {(e.source, e.target) for e in edges if e.kind == "CALLS"}
        assert (_qn(path, "UserDao.save(User)"), _qn(path, "UserDao.save(User,boolean)")) in calls

    def _build(self, tmp_path, monkeypatch):
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
        dao = _write(tmp_path, f"{SRC}/dao/UserDao.java", _OVERLOADED_DAO)
        service = _write(tmp_path, f"{SRC}/service/UserService.java", _USER_SERVICE)
        db = get_db_path(tmp_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db) as store:
            assert full_build(tmp_path, store)["errors"] == []
        return dao.resolve(), service.resolve(), db

    def test_cross_file_calls_bind_to_the_overload_their_arity_fits(self, tmp_path, monkeypatch):
        from code_review_graph.graph import GraphStore

        dao, service, db = self._build(tmp_path, monkeypatch)
        with GraphStore(db) as store:
            def callers(target):
                return {
                    e.source_qualified for e in store.get_edges_by_target(target)
                    if e.kind == "CALLS"
                }
            assert callers(_qn(dao, "UserDao.save(User)")) == {_qn(service, "UserService.register")}
            assert callers(_qn(dao, "UserDao.save(User,boolean)")) == {
                _qn(service, "UserService.registerAndFlush"), _qn(dao, "UserDao.save(User)"),
            }
            assert callers(_qn(dao, "UserDao.load")) == {
                _qn(service, "UserService.load"), _qn(dao, "UserDao$1.run"),
            }

    def test_callers_of_base_name_answers_for_the_overload_set(self, tmp_path, monkeypatch):
        from code_review_graph.tools.query import query_graph

        dao, service, _ = self._build(tmp_path, monkeypatch)
        result = query_graph("callers_of", _qn(dao, "UserDao.save"), repo_root=str(tmp_path))
        assert result["resolution"] == "overload_set"
        vias = {(r["name"], r["via"]) for r in result["results"]}
        assert vias == {
            ("register", "save(User)"),
            ("registerAndFlush", "save(User,boolean)"),
            ("save", "save(User,boolean)"),
        }
        precise = query_graph(
            "callers_of", _qn(dao, "UserDao.save(User)"), repo_root=str(tmp_path),
        )
        assert [r["name"] for r in precise["results"]] == ["register"]
        fqn = query_graph("callers_of", "com.acme.dao.UserDao.save", repo_root=str(tmp_path))
        assert fqn["resolution"] == "overload_set"


def test_java_fqn_candidates_are_not_capped_by_search_limit(tmp_path, monkeypatch):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build, get_db_path
    from code_review_graph.tools.query import _java_fqn_candidates

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    for index in range(120):
        _write(tmp_path, f"{SRC}/gen/Svc{index:03d}.java", (
            f"package com.acme.gen;\npublic class Svc{index:03d} {{ public void save() {{}} }}\n"
        ))
    db = get_db_path(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db) as store:
        full_build(tmp_path, store)
        # Inserted last, so a rowid-ordered search limited to 100 misses it.
        store.upsert_node(NodeInfo(
            kind="Function", name="save", file_path=str(tmp_path / "Target.java"),
            line_start=1, line_end=1, language="java", parent_name="Target",
        ))
        store.commit()
        matches = _java_fqn_candidates(store, "com.acme.gen.Target.save")
    assert [m.parent_name for m in matches] == ["Target"]
