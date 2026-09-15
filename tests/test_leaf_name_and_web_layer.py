"""Presentation-layer files must be indexed, and a leaf name node must yield a name.

Two defects this covers:

1. `.html`, `.css`, `.scss` and `.xml` had no extension mapping at all, so a change to a
   template or a deployment descriptor was invisible to the graph — the file was neither a
   node nor covered, even though every grammar involved already ships in the bundled
   language pack.

2. `_get_name` only looked for an identifier *child*. A node type registered as a class or
   function that is itself a leaf therefore produced nothing, silently. In CSS this meant
   `class_name` (which wraps an identifier) landed while `id_name` and `keyframes_name`
   (bare leaves) vanished with no error — a configuration that looks correct and half works.
"""

from pathlib import Path

from code_review_graph.parser import EXTENSION_TO_LANGUAGE, CodeParser


def test_presentation_extensions_are_mapped():
    for suffix, language in (
        (".html", "html"), (".htm", "html"),
        (".css", "css"), (".scss", "scss"), (".sass", "scss"),
        (".xml", "xml"),
        (".jsp", "jsp"), (".jspf", "jsp"), (".tag", "jsp"),
    ):
        assert EXTENSION_TO_LANGUAGE.get(suffix) == language, suffix


def test_jsp_family_yields_exactly_one_file_node_and_nothing_else(tmp_path: Path):
    # Fullstack-fork contract: the bundled language pack has no JSP grammar,
    # so .jsp/.jspf/.tag index as a bare File marker. The JSP link resolver
    # owns every cross-file edge; symbol extraction here would only invent
    # structure no grammar vouches for.
    source = (
        b"<html>\n"
        b'<%@ include file="footer.jspf" %>\n'
        b'<div beanclass="com.example.HomeBean"><a href="/home">go</a></div>\n'
        b"</html>\n"
    )
    for name in ("index.jsp", "footer.jspf", "grid.tag"):
        path = tmp_path / name
        path.write_bytes(source)

        assert CodeParser().detect_language(path) == "jsp", name
        nodes, edges = CodeParser().parse_bytes(path, source)

        assert len(nodes) == 1, f"{name}: expected exactly one File node"
        node = nodes[0]
        assert node.kind == "File"
        assert node.language == "jsp"
        assert node.name == node.file_path
        assert node.line_start == 1
        assert node.line_end == source.count(b"\n") + 1  # newline count, not line count
        assert edges == [], f"{name}: File-only contract forbids edges"

    # No trailing newline: a one-line file still spans exactly one line.
    single = tmp_path / "one.jsp"
    single.write_bytes(b"<div>x</div>")
    nodes, _ = CodeParser().parse_bytes(single, b"<div>x</div>")
    assert len(nodes) == 1 and nodes[0].line_end == 1


def test_html_file_node_regression_is_unaffected_by_the_jsp_branch(tmp_path: Path):
    # The JSP special-case must not leak into .html: that file type keeps its
    # grammar-driven File node (language "html"), which the JSP resolver
    # discovers as a page.
    path = tmp_path / "index.html"
    source = b"<html><body><p>x</p></body></html>\n"
    path.write_bytes(source)

    nodes, edges = CodeParser().parse_bytes(path, source)
    file_nodes = [node for node in nodes if node.kind == "File"]
    assert len(file_nodes) == 1
    assert file_nodes[0].language == "html"


def test_css_selector_kinds_all_produce_nodes(tmp_path: Path):
    source = b".header{color:red}\n#main{color:blue}\n@keyframes spin{from{}}\n"
    path = tmp_path / "site.css"
    path.write_bytes(source)

    nodes, _ = CodeParser().parse_bytes(path, source)
    names = {node.name for node in nodes if node.kind == "Class"}

    # class_name wraps an identifier; id_name and keyframes_name are bare leaves.
    # All three are registered, so all three must land.
    assert names == {"header", "main", "spin"}, names


def test_html_and_xml_yield_a_file_node(tmp_path: Path):
    for name, source in (
        ("index.html", b"<html><body><p>x</p></body></html>"),
        ("web.xml", b"<web-app><servlet><servlet-name>a</servlet-name></servlet></web-app>"),
    ):
        path = tmp_path / name
        path.write_bytes(source)
        nodes, _ = CodeParser().parse_bytes(path, source)
        assert any(node.kind == "File" for node in nodes), f"{name} produced no File node"


def test_leaf_fallback_does_not_invent_names_for_container_nodes(tmp_path: Path):
    # A Java class node has children, so the fallback must not fire and return the whole
    # class body as a name. Guards against the fallback leaking into real languages.
    source = b"package d;\npublic class App { public String render() { return \"x\"; } }\n"
    path = tmp_path / "App.java"
    path.write_bytes(source)

    nodes, _ = CodeParser().parse_bytes(path, source)
    # File nodes carry the path as their name by design; symbols must not.
    for node in (n for n in nodes if n.kind != "File"):
        assert "\n" not in node.name
        assert len(node.name) < 80, node.name
    assert {"App", "render"} <= {node.name for node in nodes}


def test_generic_yaml_yields_exactly_one_file_node_and_nothing_else(tmp_path: Path):
    # Fullstack-fork contract: generic .yml/.yaml has no grammar and no
    # structural extractor, so it indexes as a bare File marker — CI
    # workflows and docker-compose files stay visible to inventory,
    # coverage, and reconciliation instead of vanishing from the graph.
    source = (
        b"name: CI\n"
        b"on: [push]\n"
        b"jobs:\n"
        b"  test:\n"
        b"    runs-on: ubuntu-latest\n"
    )
    for name in ("ci.yml", "compose.yaml"):
        path = tmp_path / name
        path.write_bytes(source)

        assert CodeParser().detect_language(path) == "yaml", name
        nodes, edges = CodeParser().parse_bytes(path, source)

        assert len(nodes) == 1, f"{name}: expected exactly one File node"
        node = nodes[0]
        assert node.kind == "File"
        assert node.language == "yaml"
        assert node.name == node.file_path
        assert node.line_start == 1
        assert node.line_end == source.count(b"\n") + 1  # newline count, not line count
        assert edges == [], f"{name}: File-only contract forbids edges"

    # No trailing newline: a one-line file still spans exactly one line.
    single = tmp_path / "one.yaml"
    single.write_bytes(b"key: value")
    nodes, _ = CodeParser().parse_bytes(single, b"key: value")
    assert len(nodes) == 1 and nodes[0].line_end == 1

    # Plain .properties files stay out of the graph entirely: outside the
    # spring-config convention they map to no language at all.
    props = tmp_path / "build.properties"
    assert CodeParser().detect_language(props, b"key=value\n") is None
    assert CodeParser().parse_bytes(props, b"key=value\n") == ([], [])
