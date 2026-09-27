#!/usr/bin/env python3
"""Benchmark the embedding profiles on the fullstack Stripes fixture.

For each profile (each in its own child process, so peak RSS is per profile):
model load time, document throughput (texts/s), peak RSS, and recall@10 on
natural-language queries whose expected symbols are listed below, for the
vector lane alone and for hybrid search (FTS + vectors). An FTS-only
baseline row is printed once.

Run on the machine you care about (the MLX rows need Apple Silicon)::

    uv sync --extra embeddings-fast --extra embeddings-onnx --extra embeddings-mlx
    uv run python scripts/bench_embeddings.py                 # all profiles
    uv run python scripts/bench_embeddings.py --profiles fast,balanced --json

A profile whose backend is not installed, or whose model cannot be
downloaded, is reported as ``unavailable`` with the reason; it is never
silently skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fullstack_stripes"
PROFILES = ("fast", "balanced", "accurate", "legacy")

# Natural-language query -> acceptable symbol names (simple node names).
QUERIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("web page action that shows vendor invoices", ("VendorInvoiceActionBean",)),
    ("persist an invoice for a vendor", ("saveInvoice",)),
    ("list all users sorted alphabetically", ("findAllSortedByName", "listUsers")),
    ("invoices that belong to one vendor", ("findByVendor", "listForVendor")),
    ("place a customer order", ("place",)),
    ("add two money amounts together", ("plus",)),
    ("format a money amount as text with cents", ("format",)),
    ("append an entry to the audit log", ("AuditLogWriter", "write")),
    ("latest exchange rate snapshot", ("RateSnapshot", "LatestRatesService")),
    ("open unpaid invoices", ("findOpen", "isOpen")),
    ("hibernate session helper base class", ("HibernateSupport", "currentSession")),
    ("shorten a long string", ("abbreviate",)),
    ("check whether a string is empty or whitespace", ("isBlank",)),
    ("format a date in ISO style", ("iso",)),
    ("render the invoice report", ("InvoiceReport", "render")),
    ("stock keeping unit of an order line", ("getSku", "OrderLine")),
    ("register a new user and flush the session", ("registerAndFlush", "register")),
    ("invoice lifecycle status values", ("InvoiceStatus",)),
    ("total price of an order", ("total", "getTotal")),
    ("base class for stripes action beans", ("BaseActionBean",)),
)


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _prepare_repo(workdir: Path) -> Path:
    repo = workdir / "fixture"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("expected_edges.tsv"))
    git = ["git", "-c", "user.email=bench@example.invalid", "-c", "user.name=bench"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(git + args, cwd=repo, check=True, capture_output=True)
    env = {**os.environ, "CRG_EMBEDDINGS": "off"}
    subprocess.run(
        [sys.executable, "-m", "code_review_graph", "build", "--repo", str(repo)],
        cwd=repo, check=True, capture_output=True, env=env,
    )
    return repo


def _simple_name(qualified_name: str) -> str:
    tail = qualified_name.rsplit("::", 1)[-1]
    return tail.rsplit(".", 1)[-1].split("(", 1)[0]


def _recall(ranked: list[list[str]]) -> float:
    hits = sum(
        1 for names, (_, expected) in zip(ranked, QUERIES)
        if any(_simple_name(n) in expected for n in names[:10])
    )
    return round(hits / len(QUERIES), 3)


def _fts_baseline(repo: Path) -> dict[str, Any]:
    os.environ["CRG_EMBEDDINGS"] = "off"
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path
    from code_review_graph.search import hybrid_search

    store = GraphStore(get_db_path(repo))
    try:
        ranked = [[r["qualified_name"] for r in hybrid_search(store, q, limit=10,
                                                               repo_root=str(repo))]
                  for q, _ in QUERIES]
    finally:
        store.close()
    return {"profile": "fts-only", "recall_at_10_hybrid": _recall(ranked)}


def run_profile(profile: str, repo: Path, dim: int | None) -> dict[str, Any]:
    """Measure one profile in this process (call it in a fresh child)."""
    os.environ["CRG_EMBEDDINGS"] = profile
    from code_review_graph.embeddings import (
        EmbeddingStore,
        _node_to_text,
        provider_for_settings,
    )
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path
    from code_review_graph.repo_settings import load_embedding_settings
    from code_review_graph.search import hybrid_search

    # No idle unload timer: the child exits when it is done.
    settings = replace(load_embedding_settings(repo), idle_unload_s=0.0,
                       **({"dim": dim} if dim else {}))
    provider, res = provider_for_settings(settings)
    row: dict[str, Any] = {
        "profile": profile, "served_by": res.profile, "backend": res.backend,
        "model": res.model, "dim": res.dim, "warning": res.warning,
    }
    if provider is None:
        row["status"] = "unavailable"
        return row

    db = get_db_path(repo)
    graph = GraphStore(db)
    try:
        nodes = graph.get_all_nodes(exclude_files=True)
        texts = [_node_to_text(n) for n in nodes]
        started = time.perf_counter()
        try:
            provider.embed_query("warm up")
        except Exception as exc:  # download blocked, backend broken, ...
            row.update(status="unavailable", error=f"{type(exc).__name__}: {exc}")
            return row
        row["load_s"] = round(time.perf_counter() - started, 2)

        corpus = (texts * (1 + 2000 // max(1, len(texts))))[:2000]
        started = time.perf_counter()
        for i in range(0, len(corpus), settings.batch_size):
            provider.embed_documents(corpus[i:i + settings.batch_size])
        elapsed = time.perf_counter() - started
        row["texts_per_s"] = round(len(corpus) / elapsed, 1)

        emb = EmbeddingStore(db, embedding_provider=provider, dtype=settings.dtype)
        try:
            emb.embed_nodes(nodes, batch_size=settings.batch_size)
            vector_ranked = [[qn for qn, _ in emb.search(q, limit=10)] for q, _ in QUERIES]
            started = time.perf_counter()
            for q, _ in QUERIES:
                provider.embed_query(q)
            row["query_ms"] = round(1000 * (time.perf_counter() - started) / len(QUERIES), 1)
        finally:
            emb.close()
        hybrid_ranked = [
            [r["qualified_name"] for r in hybrid_search(graph, q, limit=10,
                                                         repo_root=str(repo))]
            for q, _ in QUERIES
        ]
    finally:
        graph.close()
    row.update(
        status="ok", nodes=len(nodes),
        recall_at_10_vector=_recall(vector_ranked),
        recall_at_10_hybrid=_recall(hybrid_ranked),
        peak_rss_mb=round(_peak_rss_mb(), 1),
    )
    return row


def _child(profile: str, repo: Path, dim: int | None) -> dict[str, Any]:
    cmd = [sys.executable, __file__, "--child", profile, "--repo", str(repo)]
    if dim:
        cmd += ["--dim", str(dim)]
    done = subprocess.run(cmd, capture_output=True, text=True)
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if done.returncode != 0 or not lines:
        tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
        return {"profile": profile, "status": "failed", "error": " | ".join(tail)}
    return json.loads(lines[-1])


def _table(rows: list[dict[str, Any]]) -> str:
    cols = ("profile", "status", "backend", "dim", "load_s", "texts_per_s", "query_ms",
            "peak_rss_mb", "recall_at_10_vector", "recall_at_10_hybrid")
    out = [" | ".join(cols), " | ".join("---" for _ in cols)]
    for row in rows:
        out.append(" | ".join(str(row.get(c, "")) for c in cols))
    notes = [f"- {r['profile']}: {r.get('error') or r.get('warning')}" for r in rows
             if r.get("error") or r.get("warning")]
    return "\n".join(out + ([""] + notes if notes else []))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profiles", default=",".join(PROFILES))
    ap.add_argument("--dim", type=int, default=None, help="override the profile dimension")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--child", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.child:
        print(json.dumps(run_profile(args.child, Path(args.repo), args.dim)))
        return 0

    with tempfile.TemporaryDirectory(prefix="crg-bench-") as tmp:
        repo = _prepare_repo(Path(tmp))
        rows = [_fts_baseline(repo)]
        for profile in [p.strip() for p in args.profiles.split(",") if p.strip()]:
            rows.append(_child(profile, repo, args.dim))
    meta = {"platform": f"{sys.platform}-{os.uname().machine}", "python": sys.version.split()[0],
            "queries": len(QUERIES)}
    if args.json:
        print(json.dumps({"meta": meta, "rows": rows}, indent=2))
    else:
        print(f"platform {meta['platform']}, python {meta['python']}, "
              f"{meta['queries']} queries\n")
        print(_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
