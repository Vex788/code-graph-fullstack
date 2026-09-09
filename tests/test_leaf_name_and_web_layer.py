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
    ):
        assert EXTENSION_TO_LANGUAGE.get(suffix) == language, suffix


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
