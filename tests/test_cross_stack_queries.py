"""Cross-stack query_graph patterns, pagination and error shape."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.kinds import EDGE_KINDS_BY_NAME
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.query import (
    _CROSS_STACK_PATTERNS,
    batch_query,
    query_graph,
    query_patterns,
)

from .witness.conftest import build, copy_fixture

CROSS_STACK = (
    "pages_for", "requests_to", "included_by", "views_of",
    "forwards_to", "maps_to", "binds_to", "styles_of",
)


def _names(result: dict) -> set[str]:
    return {Path(str(r.get("qualified_name") or r.get("name"))).name for r in result["results"]}


def test_every_cross_stack_pattern_is_registered_from_kinds():
    assert set(CROSS_STACK) == set(_CROSS_STACK_PATTERNS)
    for _, incoming, outgoing in _CROSS_STACK_PATTERNS.values():
        assert (incoming | outgoing) <= set(EDGE_KINDS_BY_NAME)
    assert set(CROSS_STACK) <= set(query_patterns())


def test_cli_query_choices_match_query_graph_patterns():
    from code_review_graph.cli import build_parser

    parser, _ = build_parser()
    commands = next(a for a in parser._actions if a.dest == "command")
    pattern = next(a for a in commands.choices["query"]._actions if a.dest == "pattern")
    assert set(pattern.choices) == set(query_patterns())


# ---------------------------------------------------------------------------
# Real edges: the Stripes fixture (RENDERS, INCLUDES) and a Spring app (REQUESTS)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stripes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CRG_HOME", str(tmp_path_factory.mktemp("crg-home")))
        repo = copy_fixture(tmp_path_factory.mktemp("xq") / "app")
        assert build(repo)["status"] == "ok"
    return repo


def test_pages_for_action_bean_follows_renders_and_forwards(stripes: Path):
    result = query_graph("pages_for", "OrderActionBean", repo_root=str(stripes))
    assert result["status"] == "ok", result
    assert _names(result) == {"view.jsp"}
    assert {(row["via"], row["direction"]) for row in result["results"]} == {
        ("RENDERS", "incoming"),
        ("FORWARDS_TO", "outgoing"),
    }
    assert result["edges"][0]["kind"] == "RENDERS"


def test_included_by_lists_including_pages(stripes: Path):
    result = query_graph(
        "included_by", "web/WEB-INF/jsp/common/header.jspf", repo_root=str(stripes),
    )
    assert result["status"] == "ok", result
    assert _names(result) == {"view.jsp", "list.jsp", "invoice.jsp", "invoice_list.jsp"}
    assert {r["via"] for r in result["results"]} == {"INCLUDES"}


def test_batch_query_accepts_cross_stack_patterns(stripes: Path):
    result = batch_query([
        {"pattern": "pages_for", "target": "OrderActionBean"},
        {"pattern": "included_by", "target": "web/WEB-INF/jsp/common/footer.jspf"},
    ], repo_root=str(stripes))
    pages, included = result["results"]
    assert pages["status"] == "ok" and pages["prod_count"] == 2
    assert "view.jsp" in pages["prod"][0]
    assert included["status"] == "ok" and included["prod_count"] >= 2


def test_requests_to_stripes_endpoint_by_url(stripes: Path):
    result = query_graph("requests_to", "/vendor/Invoice.action", repo_root=str(stripes))
    assert result["status"] == "ok", result
    assert _names(result) == {"invoice.jsp", "invoice.js", "header.jspf", "index.jsp"}
    assert {r["via"] for r in result["results"]} == {"REQUESTS"}


def test_forwards_to_and_views_of_round_trip(stripes: Path):
    forwards = query_graph(
        "forwards_to", "web/WEB-INF/jsp/order/view.jsp", repo_root=str(stripes),
    )
    assert forwards["status"] == "ok", forwards
    assert _names(forwards) == {"OrderActionBean.java::OrderActionBean.view",
                                "OrderActionBean.java::OrderActionBean.place"}
    views = query_graph("views_of", "OrderActionBean", repo_root=str(stripes))
    assert _names(views) == {"view.jsp"}


def test_redirect_resolution_binds_to_the_endpoint(stripes: Path):
    forwards = query_graph(
        "forwards_to", "/user/List.action", repo_root=str(stripes),
    )
    assert forwards["status"] == "ok", forwards
    assert _names(forwards) == {"UserListActionBean.java::UserListActionBean.register"}


def test_binds_to_form_fields_and_back(stripes: Path):
    page = query_graph(
        "binds_to", "web/WEB-INF/jsp/vendor/invoice.jsp", repo_root=str(stripes),
    )
    assert page["status"] == "ok", page
    assert _names(page) == {
        "VendorInvoiceActionBean.java::VendorInvoiceActionBean.setInvoice",
        "VendorInvoiceActionBean.java::VendorInvoiceActionBean.setInvoiceId",
    }
    assert {r["direction"] for r in page["results"]} == {"outgoing"}
    setter = query_graph(
        "binds_to", "VendorInvoiceActionBean.setInvoiceId", repo_root=str(stripes),
    )
    assert _names(setter) == {"invoice.jsp"}


def test_styles_of_stylesheet_and_page(stripes: Path):
    stylesheet = query_graph("styles_of", "web/css/invoice.css", repo_root=str(stripes))
    assert stylesheet["status"] == "ok", stylesheet
    # invoice_list.jsp also links invoice.css and uses its .invoice-table.
    assert _names(stylesheet) == {"invoice.jsp", "invoice_list.jsp"}
    page = query_graph(
        "styles_of", "web/WEB-INF/jsp/vendor/invoice.jsp", repo_root=str(stripes),
    )
    assert _names(page) == {"invoice.css::invoice-form", "invoice.css::invoice-total"}


def test_maps_to_entities_and_tables(stripes: Path):
    entity = query_graph(
        "maps_to", "src/main/java/com/acme/model/User.java::User",
        repo_root=str(stripes),
    )
    assert entity["status"] == "ok", entity
    assert _names(entity) == {"table::users"}
    table = query_graph("maps_to", "table::invoices", repo_root=str(stripes))
    assert _names(table) == {"Invoice.java::Invoice"}
    [row] = table["results"]
    # The far end of the table query is the entity Class node.
    assert row["kind"] == "Class" and row["name"] == "Invoice"


_CONTROLLER = """package com.acme;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class OrderController {
    @GetMapping("/orders/list")
    public String list() {
        return "ok";
    }
}
"""
_PAGE = """<%@ page contentType="text/html" %>
<a href="${pageContext.request.contextPath}/orders/list">Orders</a>
"""


@pytest.fixture(scope="module")
def spring(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("spring") / "app"
    (repo / "src/main/java/com/acme").mkdir(parents=True)
    (repo / "src/main/webapp/WEB-INF/jsp").mkdir(parents=True)
    (repo / "src/main/java/com/acme/OrderController.java").write_text(_CONTROLLER)
    (repo / "src/main/webapp/WEB-INF/jsp/orders.jsp").write_text(_PAGE)
    for args in (["init", "-q"],
                 # No machine-level hooks: a global post-commit graph refresh
                 # would race the build under test.
                 ["config", "core.hooksPath", str(repo / ".githooks-disabled")],
                 ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "f"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CRG_HOME", str(tmp_path_factory.mktemp("crg-home")))
        assert build(repo.resolve())["status"] == "ok"
    return repo.resolve()


@pytest.mark.parametrize("target", ["/orders/list", "/orders/list?x=1", "OrderController"])
def test_requests_to_endpoint_url_or_handler_class(spring: Path, target: str):
    result = query_graph("requests_to", target, repo_root=str(spring))
    assert result["status"] == "ok", result
    assert _names(result) == {"orders.jsp"}
    assert result["results"][0]["via"] == "REQUESTS"


def test_pages_for_controller_reaches_pages_through_its_endpoint(spring: Path):
    result = query_graph("pages_for", "OrderController", repo_root=str(spring))
    assert _names(result) == {"orders.jsp"}


# ---------------------------------------------------------------------------
# Synthetic edges for kinds no resolver emits yet
# ---------------------------------------------------------------------------

BEAN_FILE = "/repo/src/InvoiceActionBean.java"
BEAN = f"{BEAN_FILE}::InvoiceActionBean"
PAGE = "/repo/web/invoice.jsp"
CSS = "/repo/web/invoice.css"
ENTITY_FILE = "/repo/src/Invoice.java"
ENTITY = f"{ENTITY_FILE}::Invoice"


@pytest.fixture
def synthetic(tmp_path: Path):
    store = GraphStore(tmp_path / "graph.db")

    def node(kind, name, path, language, parent=None):
        store.upsert_node(NodeInfo(kind=kind, name=name, file_path=path, line_start=1,
                                   line_end=5, language=language, parent_name=parent),
                          file_hash="h")

    def edge(kind, source, target, path):
        store.upsert_edge(EdgeInfo(kind=kind, source=source, target=target,
                                   file_path=path, line=1))

    node("File", BEAN_FILE, BEAN_FILE, "java")
    node("Class", "InvoiceActionBean", BEAN_FILE, "java")
    node("Function", "view", BEAN_FILE, "java", parent="InvoiceActionBean")
    node("Function", "setInvoiceId", BEAN_FILE, "java", parent="InvoiceActionBean")
    node("File", PAGE, PAGE, "jsp")
    node("File", CSS, CSS, "css")
    node("Class", "invoice-total", CSS, "css")
    node("Class", "Invoice", ENTITY_FILE, "java")
    edge("CONTAINS", BEAN_FILE, BEAN, BEAN_FILE)
    edge("CONTAINS", BEAN, f"{BEAN}.view", BEAN_FILE)
    edge("CONTAINS", BEAN, f"{BEAN}.setInvoiceId", BEAN_FILE)
    edge("CONTAINS", CSS, f"{CSS}::invoice-total", CSS)
    edge("FORWARDS_TO", f"{BEAN}.view", PAGE, BEAN_FILE)
    edge("BINDS", PAGE, f"{BEAN}.setInvoiceId", PAGE)
    edge("USES_STYLE", PAGE, f"{CSS}::invoice-total", PAGE)
    edge("MAPS_TO", ENTITY, "table::invoices", ENTITY_FILE)
    store.commit()
    yield store, Path("/repo")
    store.close()


@pytest.mark.parametrize("pattern,target,expected,direction", [
    ("views_of", BEAN, {"invoice.jsp"}, "outgoing"),
    ("views_of", BEAN_FILE, {"invoice.jsp"}, "outgoing"),
    ("pages_for", BEAN, {"invoice.jsp"}, "outgoing"),
    ("forwards_to", PAGE, {"InvoiceActionBean.java::InvoiceActionBean.view"}, "incoming"),
    ("binds_to", PAGE, {"InvoiceActionBean.java::InvoiceActionBean.setInvoiceId"},
     "outgoing"),
    ("binds_to", f"{BEAN}.setInvoiceId", {"invoice.jsp"}, "incoming"),
    ("styles_of", PAGE, {"invoice.css::invoice-total"}, "outgoing"),
    ("styles_of", CSS, {"invoice.jsp"}, "incoming"),
    ("maps_to", ENTITY, {"table::invoices"}, "outgoing"),
])
def test_synthetic_cross_stack_edges(synthetic, pattern, target, expected, direction):
    result = query_graph(pattern, target, _store=synthetic)
    assert result["status"] == "ok", result
    assert _names(result) == expected
    assert {r["direction"] for r in result["results"]} == {direction}


def test_unresolved_far_end_is_reported_not_dropped(synthetic):
    result = query_graph("maps_to", ENTITY, _store=synthetic)
    [row] = result["results"]
    assert row["resolution"] == "unresolved" and row["kind"] is None


# ---------------------------------------------------------------------------
# Pagination and error shape
# ---------------------------------------------------------------------------


def test_query_graph_offset_pages_through_every_result(synthetic):
    first = query_graph("children_of", BEAN, max_results=1, _store=synthetic)
    assert first["result_count"] == 2 and first["next_offset"] == 1
    second = query_graph("children_of", BEAN, max_results=1, offset=1, _store=synthetic)
    assert second["next_offset"] is None and second["results_omitted"] == 0
    seen = {r["name"] for r in first["results"] + second["results"]}
    assert seen == {"view", "setInvoiceId"}
    with pytest.raises(ValueError):
        query_graph("children_of", BEAN, offset=-1, _store=synthetic)


def test_unknown_pattern_uses_the_contract_error_shape(synthetic):
    result = query_graph("no_such_pattern", BEAN, _store=synthetic)
    assert result["status"] == "error"
    assert result["error_code"] == "unknown_pattern"
    assert "pages_for" in result["message"]


def test_batch_query_reports_invalid_repo_root_per_entry(stripes: Path, tmp_path: Path):
    bogus = str(tmp_path / "nowhere")
    result = batch_query([
        {"pattern": "pages_for", "target": "OrderActionBean"},
        {"pattern": "pages_for", "target": "OrderActionBean", "repo_root": bogus},
    ], repo_root=str(stripes))
    ok, bad = result["results"]
    assert result["status"] == "ok"
    assert ok["status"] == "ok"
    assert bad["status"] == "error" and bad["error_code"] == "invalid_repo_root"
    assert bad["repo_root"] == bogus

    all_bad = batch_query([{"pattern": "callers_of", "target": "x"}], repo_root=bogus)
    assert [i["error_code"] for i in all_bad["results"]] == ["invalid_repo_root"]


def test_impact_radius_offset_pages(stripes: Path):
    from code_review_graph.tools.query import get_impact_radius

    files = ["web/WEB-INF/jsp/common/header.jspf"]
    whole = get_impact_radius(changed_files=files, repo_root=str(stripes))
    assert whole["next_offset"] is None
    total = whole["total_impacted"]
    assert total >= 2
    first = get_impact_radius(changed_files=files, repo_root=str(stripes), max_results=1)
    assert first["next_offset"] == 1 and first["truncated"]
    rest = get_impact_radius(
        changed_files=files, repo_root=str(stripes), max_results=100, offset=1,
    )
    names = [n["qualified_name"] for n in first["impacted_nodes"] + rest["impacted_nodes"]]
    assert names == [n["qualified_name"] for n in whole["impacted_nodes"]]


def test_search_offset_and_traverse_error_shape(stripes: Path):
    from code_review_graph.tools.query import semantic_search_nodes, traverse_graph_func

    first = semantic_search_nodes("ActionBean", limit=2, repo_root=str(stripes))
    assert len(first["results"]) == 2 and first["next_offset"] == 2
    second = semantic_search_nodes("ActionBean", limit=2, offset=2, repo_root=str(stripes))
    firsts = {r["qualified_name"] for r in first["results"]}
    assert firsts.isdisjoint({r["qualified_name"] for r in second["results"]})

    missing = traverse_graph_func("zzqqxxnothing", repo_root=str(stripes))
    assert (missing["status"], missing["error_code"]) == ("error", "not_found")
    bad_mode = traverse_graph_func("OrderActionBean", mode="sideways", repo_root=str(stripes))
    assert bad_mode["error_code"] == "invalid_argument"
    found = traverse_graph_func("OrderActionBean", repo_root=str(stripes))
    assert found["status"] == "ok"
    assert all(s.split()[0].endswith("_tool") for s in found["next_tool_suggestions"])
