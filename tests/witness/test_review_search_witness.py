"""Witnesses for agent-facing review risk and search on the fullstack fixture."""

from __future__ import annotations

from pathlib import Path

from .conftest import build, git

ORDER_VIEW_JSP = "web/WEB-INF/jsp/order/view.jsp"


def test_jsp_only_change_has_nonzero_risk(fixture_repo: Path):
    # Red before W5b: a File-only JSP change scored risk 0 and named nothing.
    from code_review_graph.tools.review import detect_changes_func

    build(fixture_repo)
    page = fixture_repo / ORDER_VIEW_JSP
    page.write_text(
        page.read_text(encoding="utf-8").replace("Place order", "Confirm order"),
        encoding="utf-8",
    )
    git(fixture_repo, "commit", "-qam", "relabel order button")

    result = detect_changes_func(
        base="HEAD~1", changed_files=[ORDER_VIEW_JSP], repo_root=str(fixture_repo),
    )
    assert result["status"] == "ok"
    # view.jsp renders OrderActionBean, so the change reaches Java.
    assert result["risk_score"] > 0, result["summary"]
    impacted = [n["qualified_name"] for n in result["impacted_nodes"]]
    assert any(qn.endswith("OrderActionBean.java::OrderActionBean") for qn in impacted), (
        impacted
    )

    minimal = detect_changes_func(
        base="HEAD~1", changed_files=[ORDER_VIEW_JSP], repo_root=str(fixture_repo),
        detail_level="minimal",
    )
    assert minimal["risk_score"] == result["risk_score"]


def test_fts_control_finds_exact_class_name(built_fixture: Path):
    from code_review_graph.tools.query import semantic_search_nodes

    result = semantic_search_nodes("VendorInvoiceActionBean", repo_root=str(built_fixture))
    assert "VendorInvoiceActionBean" in [r["name"] for r in result["results"]]


def test_fts_search_vendor_invoice_finds_action_bean(built_fixture: Path, monkeypatch):
    # Red at 434aac4 (phrase FTS over whole identifiers); green since the v10
    # name_tokens column (04b8b7b).
    from code_review_graph.tools.query import semantic_search_nodes

    monkeypatch.setenv("CRG_EMBEDDINGS", "off")
    result = semantic_search_nodes("vendor invoice", repo_root=str(built_fixture))
    names = [r["name"] for r in result["results"]]
    assert "VendorInvoiceActionBean" in names, names
