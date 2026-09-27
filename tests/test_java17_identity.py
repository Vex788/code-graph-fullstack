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
