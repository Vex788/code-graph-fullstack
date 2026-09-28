"""Change risk across the stack: pages, stylesheets and the code they reach."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes, compute_risk_score
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo

from .witness.conftest import build, copy_fixture

PAGE = "/repo/web/order/view.jsp"
HEADER = "/repo/web/common/header.jspf"
BEAN_FILE = "/repo/src/OrderActionBean.java"
BEAN = f"{BEAN_FILE}::OrderActionBean"
CSS = "/repo/web/css/app.css"


@pytest.fixture
def store(tmp_path: Path):
    s = GraphStore(tmp_path / "graph.db")
    yield s
    s.close()


def _node(store: GraphStore, kind: str, name: str, path: str, language: str,
          parent: str | None = None, lines: tuple[int, int] = (1, 10)) -> None:
    store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=path, line_start=lines[0],
        line_end=lines[1], language=language, parent_name=parent,
    ), file_hash="h")


def _edge(store: GraphStore, kind: str, source: str, target: str, path: str) -> None:
    store.upsert_edge(EdgeInfo(kind=kind, source=source, target=target, file_path=path, line=1))


def _web_graph(store: GraphStore) -> None:
    _node(store, "File", PAGE, PAGE, "jsp")
    _node(store, "File", HEADER, HEADER, "jsp")
    _node(store, "File", BEAN_FILE, BEAN_FILE, "java", lines=(1, 40))
    _node(store, "Class", "OrderActionBean", BEAN_FILE, "java", lines=(3, 40))
    _edge(store, "RENDERS", PAGE, BEAN, PAGE)
    _edge(store, "INCLUDES", PAGE, HEADER, PAGE)
    store.commit()


def test_incoming_cross_stack_edges_count_as_callers(store):
    _web_graph(store)
    bean = store.get_node(BEAN)
    with_page = compute_risk_score(store, bean)
    store._conn.execute("DELETE FROM edges WHERE kind = 'RENDERS'")
    store.commit()
    assert with_page == pytest.approx(compute_risk_score(store, bean) + 0.05)


def test_planned_cross_stack_kind_counts_without_code_change(store):
    # FORWARDS_TO has no producer yet; the kinds registry alone makes it count.
    _web_graph(store)
    _node(store, "Function", "view", BEAN_FILE, "java", parent="OrderActionBean")
    _edge(store, "FORWARDS_TO", f"{BEAN}.view", PAGE, BEAN_FILE)
    store.commit()
    result = analyze_changes(store, [PAGE], changed_ranges={PAGE: [(2, 2)]})
    incoming = [
        link for link in result["cross_stack_links"] if link["direction"] == "incoming"
    ]
    assert [link["kind"] for link in incoming] == ["FORWARDS_TO"]


def test_file_only_jsp_change_is_scored_from_its_links(store):
    _web_graph(store)
    _node(store, "File", "/repo/web/user/list.jsp", "/repo/web/user/list.jsp", "jsp")
    _edge(store, "INCLUDES", "/repo/web/user/list.jsp", HEADER, "/repo/web/user/list.jsp")
    store.commit()

    page = analyze_changes(store, [PAGE], changed_ranges={PAGE: [(2, 2)]})
    # RENDERS bean + INCLUDES header: two cross-stack targets.
    assert page["risk_score"] == pytest.approx(0.20)
    [entry] = page["changed_functions"]
    assert entry["kind"] == "File" and entry["risk_score"] == page["risk_score"]
    targets = {(link["kind"], link["target"]) for link in page["cross_stack_links"]}
    assert targets == {("RENDERS", BEAN), ("INCLUDES", HEADER)}
    assert page["test_gaps"] == []
    assert "1 changed web file(s)" in page["summary"]

    header = analyze_changes(store, [HEADER], changed_ranges={HEADER: [(1, 1)]})
    # Included by two other pages.
    assert header["risk_score"] == pytest.approx(0.10)


def test_js_function_change_is_not_scored_again_as_a_file(store):
    js = "/repo/web/js/app.js"
    _node(store, "File", js, js, "javascript", lines=(1, 20))
    _node(store, "Function", "load", js, "javascript", lines=(2, 5))
    store.commit()
    result = analyze_changes(store, [js], changed_ranges={js: [(3, 3)]})
    assert [f["kind"] for f in result["changed_functions"]] == ["Function"]
    assert result["cross_stack_links"] == []


def test_css_selectors_get_no_test_gap_or_security_bonus(store):
    _node(store, "File", CSS, CSS, "css", lines=(1, 20))
    _node(store, "Class", "login-error", CSS, "css", lines=(2, 4))
    store.commit()
    result = analyze_changes(store, [CSS], changed_ranges={CSS: [(3, 3)]})
    assert result["test_gaps"] == []
    [selector] = result["changed_functions"]
    assert selector["name"] == "login-error"
    # No untested term (0.30) and no "login" security bonus (0.20).
    assert selector["risk_score"] == 0.0

    _edge(store, "USES_STYLE", PAGE, f"{CSS}::login-error", PAGE)
    store.commit()
    styled = analyze_changes(store, [CSS], changed_ranges={CSS: [(3, 3)]})
    assert styled["risk_score"] == pytest.approx(0.05)


def test_web_file_security_bonus_uses_the_file_name(store):
    login = "/repo/web/session/login.jsp"
    _node(store, "File", login, login, "jsp")
    store.commit()
    result = analyze_changes(store, [login], changed_ranges={login: [(1, 1)]})
    assert result["risk_score"] == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# detect_changes on the fullstack fixture
# ---------------------------------------------------------------------------

HEADER_JSPF = "web/WEB-INF/jsp/common/header.jspf"
VIEW_JSP = "web/WEB-INF/jsp/order/view.jsp"


@pytest.fixture(scope="module")
def fixture_app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    home = tmp_path_factory.mktemp("crg-home")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CRG_HOME", str(home))
        repo = copy_fixture(tmp_path_factory.mktemp("xstack") / "app")
        assert build(repo)["status"] == "ok"
    return repo


def _names(result: dict) -> set[str]:
    return {Path(n["qualified_name"]).name for n in result["impacted_nodes"]}


def test_detect_changes_honours_max_depth(fixture_app: Path):
    from code_review_graph.tools.review import detect_changes_func

    kwargs = {"changed_files": [HEADER_JSPF], "repo_root": str(fixture_app)}
    flat = detect_changes_func(max_depth=0, **kwargs)
    one_hop = detect_changes_func(max_depth=1, **kwargs)
    # Depth 0 lists only the cross-stack targets of the changed page: the
    # two routes its anchors request and the selector it uses.
    assert _names(flat) == {"List.action", "Invoice.action", "common.css::page-header"}
    # Pages that include the header are one hop away.
    assert {"view.jsp", "list.jsp"} <= _names(one_hop)
    assert one_hop["max_depth"] == 1
    assert "within 1 hop(s)" in one_hop["summary"]
    assert one_hop["risk_score"] > 0

    with pytest.raises(ValueError):
        detect_changes_func(max_depth=-1, **kwargs)


def test_detect_changes_explicit_files_ignore_other_diff_ranges(tmp_path: Path, monkeypatch):
    from code_review_graph.tools.review import detect_changes_func

    from .witness.conftest import git

    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    repo = copy_fixture(tmp_path / "app")
    build(repo)
    page = repo / VIEW_JSP
    page.write_text(page.read_text(encoding="utf-8") + "\n<p/>\n", encoding="utf-8")
    git(repo, "commit", "-qam", "touch view")

    result = detect_changes_func(changed_files=[HEADER_JSPF], repo_root=str(repo))
    changed = [Path(f["qualified_name"]).name for f in result["changed_functions"]]
    assert changed == ["header.jspf"]
    assert "OrderActionBean" not in {n["name"] for n in result["impacted_nodes"]}


def test_minimal_context_reports_the_same_risk(tmp_path: Path, monkeypatch):
    from code_review_graph.tools.context import get_minimal_context
    from code_review_graph.tools.review import detect_changes_func

    from .witness.conftest import git

    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    repo = copy_fixture(tmp_path / "app")
    build(repo)
    page = repo / VIEW_JSP
    page.write_text(
        page.read_text(encoding="utf-8").replace("Place order", "Confirm order"),
        encoding="utf-8",
    )
    git(repo, "commit", "-qam", "relabel")
    build(repo, full=False)

    detected = detect_changes_func(repo_root=str(repo))
    context = get_minimal_context(task="review", repo_root=str(repo))
    assert detected["risk_score"] > 0
    assert f"({detected['risk_score']:.2f})" in context["summary"], context
