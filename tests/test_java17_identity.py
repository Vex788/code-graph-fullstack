"""Java 17 constructs, Java method identity, and type resolution in the parser."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.parser import CodeParser


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
