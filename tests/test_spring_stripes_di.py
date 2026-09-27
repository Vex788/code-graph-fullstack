"""Stripes @SpringBean injection and Spring DI call resolution."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.spring_resolver import resolve_spring_di_calls

SRC = "src/main/java/com/acme"


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.resolve()


def _repo(root: Path) -> dict[str, Path]:
    return {
        "iface": _write(root, f"{SRC}/service/InvoiceService.java", (
            "package com.acme.service;\n"
            "public interface InvoiceService { void find(Long id); void find(Long id, int v); }\n"
        )),
        "impl": _write(root, f"{SRC}/service/InvoiceServiceImpl.java", (
            "package com.acme.service;\n"
            "public class InvoiceServiceImpl implements InvoiceService {\n"
            "    public void find(Long id) { }\n"
            "    public void find(Long id, int v) { }\n"
            "}\n"
        )),
        # Same simple name, other package: must never be picked for the above.
        "other": _write(root, f"{SRC}/legacy/InvoiceServiceImpl.java", (
            "package com.acme.legacy;\n"
            "public class InvoiceServiceImpl { public void find(Long id) { } }\n"
        )),
        "bean": _write(root, f"{SRC}/web/InvoiceActionBean.java", (
            "package com.acme.web;\n"
            "import com.acme.service.InvoiceService;\n"
            "import net.sourceforge.stripes.integration.spring.SpringBean;\n"
            "public class InvoiceActionBean {\n"
            "    @SpringBean\n"
            "    private InvoiceService service;\n"
            "    public void view() { service.find(1L); }\n"
            "    public Runnable later() {\n"
            "        return new Runnable() { public void run() { service.find(1L, 2); } };\n"
            "    }\n"
            "}\n"
        )),
    }


def _calls(store: GraphStore, source: str) -> set[tuple[str, bool]]:
    return {
        (e.target_qualified, bool(e.extra.get("spring_derived")))
        for e in store.get_edges_by_source(source)
        if e.kind == "CALLS"
    }


def test_spring_bean_field_injects_resolved_interface(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    files = _repo(tmp_path)
    with GraphStore(tmp_path / "graph.db") as store:
        assert full_build(tmp_path, store)["errors"] == []
        injects = [
            (e.source_qualified, e.target_qualified, e.extra.get("field_name"))
            for e in store.get_edges_by_source(f"{files['bean']}::InvoiceActionBean")
            if e.kind == "INJECTS"
        ]
    assert injects == [
        (f"{files['bean']}::InvoiceActionBean", f"{files['iface']}::InvoiceService", "service"),
    ]


def test_injected_calls_keep_interface_edge_and_add_impl(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    files = _repo(tmp_path)
    iface, impl, bean = files["iface"], files["impl"], files["bean"]
    with GraphStore(tmp_path / "graph.db") as store:
        assert full_build(tmp_path, store)["errors"] == []
        resolve_spring_di_calls(store)  # a second run must not duplicate
        view = _calls(store, f"{bean}::InvoiceActionBean.view")
        run = _calls(store, f"{bean}::InvoiceActionBean$1.run")

    assert view == {
        (f"{iface}::InvoiceService.find(Long)", False),
        (f"{impl}::InvoiceServiceImpl.find(Long)", True),
    }
    # The anonymous class reads the outer class's injected field.
    assert (f"{impl}::InvoiceServiceImpl.find(Long,int)", True) in run
