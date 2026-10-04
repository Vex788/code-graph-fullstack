#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema==4.25.1"]
# ///
"""Build an immutable three-dot context pack for one pull request.

Graph data comes only from the code-review-graph CLI JSON (a ``status --json``
receipt plus one ``impact`` call); the graph database is never opened.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph_health import (  # noqa: E402
    DEGRADED,
    PREP_MARKER,
    READY,
    changed_dispositions,
    probe,
    run_json,
)

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
CLASS_RE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
METHOD_RE = re.compile(
    r"(?:public|protected|private|static|final|synchronized|native|abstract|default|\s)+"
    r"[\w$<>\[\],.?]+\s+([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:\{|throws\b)"
)
CONTEXT_SCHEMA = Path(__file__).resolve().parent.parent / "references/context-pack.schema.json"
DEFAULT_MAX_DIFF_BYTES = 524_288
DEFAULT_MAX_HUNK_BYTES = 65_536
DEFAULT_MAX_GRAPH_NODES = 80
DEFAULT_MAX_GRAPH_CALLERS = 80
DEFAULT_MAX_GRAPH_CALLEES = 80
IMPACT_TIMEOUT_SECONDS = 180
READY_STATUSES = READY | DEGRADED
DOMAIN_ROUTER = "{{domain_router}}"
DEFAULT_DOMAIN_ROUTER = Path(DOMAIN_ROUTER).expanduser() if DOMAIN_ROUTER else None


class PackError(RuntimeError):
    pass


def git_bytes(repo: Path, *args: str) -> bytes:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=environment,
    )
    return result.stdout


def git(repo: Path, *args: str) -> str:
    return git_bytes(repo, *args).decode("utf-8")


def commit_sha(repo: Path, ref: str) -> str:
    try:
        return git(repo, "rev-parse", "--verify", ref + "^{commit}").strip()
    except subprocess.CalledProcessError as error:
        raise PackError(f"not a commit object: {ref}") from error


def commit_time(repo: Path, sha: str) -> str:
    return git(repo, "show", "-s", "--format=%cI", sha).strip()


def parse_hunks(
    patch: bytes,
) -> tuple[list[dict], dict[str, list[list[int]]], list[dict], dict[str, bytes]]:
    text = patch.decode("utf-8", errors="replace")
    lines = text.splitlines()
    hunks: list[dict] = []
    ranges: dict[str, list[list[int]]] = {}
    symbols: list[dict] = []
    artifacts: dict[str, bytes] = {}
    current_path = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ "):
            value = line[4:]
            current_path = "" if value == "/dev/null" else value.removeprefix("b/")
            index += 1
            continue
        match = HUNK_RE.match(line)
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        body: list[str] = []
        cursor = index + 1
        new_line = new_start
        while cursor < len(lines) and not lines[cursor].startswith(("@@ ", "diff --git ")):
            body_line = lines[cursor]
            body.append(body_line)
            if body_line.startswith("+") and not body_line.startswith("+++"):
                content = body_line[1:]
                path_ranges = ranges.setdefault(current_path, [])
                if path_ranges and path_ranges[-1][1] + 1 == new_line:
                    path_ranges[-1][1] = new_line
                else:
                    path_ranges.append([new_line, new_line])
                class_match = CLASS_RE.search(content)
                method_match = METHOD_RE.search(content)
                if class_match:
                    symbols.append({"path": current_path, "line": new_line,
                                    "kind": class_match.group(1), "name": class_match.group(2)})
                elif method_match:
                    symbols.append({"path": current_path, "line": new_line,
                                    "kind": "method", "name": method_match.group(1)})
                new_line += 1
            elif not body_line.startswith("-"):
                new_line += 1
            cursor += 1
        identity = f"{current_path}\0{line}\0" + "\n".join(body)
        hunk_id = "H-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        artifact = (line + "\n" + "\n".join(body) + "\n").encode("utf-8")
        artifact_path = f"hunks/{hunk_id}.patch"
        artifacts[artifact_path] = artifact
        hunks.append({
            "id": hunk_id,
            "path": current_path,
            "old_start": old_start,
            "old_count": old_count,
            "new_start": new_start,
            "new_count": new_count,
            "heading": match.group(5).strip(),
            "artifact": artifact_path,
            "bytes": len(artifact),
            "sha256": hashlib.sha256(artifact).hexdigest(),
        })
        index = cursor
    return hunks, ranges, symbols, artifacts


def _receipt_summary(receipt: dict) -> dict:
    return {
        "repo_root": receipt.get("repo_root"),
        "built_at_commit": receipt.get("built_at_commit"),
        "current_sha": receipt.get("current_sha"),
        "nodes": receipt.get("nodes"),
        "files": receipt.get("files"),
        "readiness": receipt.get("readiness"),
        "source_identity": receipt.get("source_identity", {}),
    }


def _empty_graph(status: str, changed: list[dict], limits: dict) -> dict:
    return {
        "status": status,
        "changed_files": changed,
        "nodes": [],
        "callers": [],
        "callees": [],
        "limits": limits,
        "truncated": {"nodes": False, "callers": False, "callees": False},
    }


def _relative(repo: Path, path: object) -> str:
    text = str(path or "")
    try:
        return Path(text).resolve().relative_to(repo).as_posix()
    except (ValueError, OSError):
        return text


def orientation(
    repo: Path,
    indexed: list[str],
    ranges: dict[str, list[list[int]]],
    max_nodes: int,
    max_callers: int,
    max_callees: int,
) -> dict:
    """Changed nodes plus one-hop callers/callees from one ``code-review-graph impact`` call."""
    empty = {"nodes": [], "callers": [], "callees": [],
             "truncated": {"nodes": False, "callers": False, "callees": False}}
    if not indexed:
        return {**empty, "orientation": "no-indexed-changes"}
    impact, text = run_json(
        ["impact", "--repo", str(repo), "--depth", "1",
         "--max-results", str(max_callers + max_callees + 1), "--files", *indexed],
        IMPACT_TIMEOUT_SECONDS,
    )
    if impact is None or impact.get("status") not in (None, "ok"):
        return {**empty, "orientation": "unavailable", "orientation_error": text or str(impact)}
    candidates = []
    for node in impact.get("changed_nodes") or []:
        path = _relative(repo, node.get("file_path"))
        start, end = node.get("line_start") or 0, node.get("line_end") or 0
        changed = ranges.get(path, [])
        if changed and node.get("kind") != "File" and not any(
            start <= high and end >= low for low, high in changed
        ):
            continue
        candidates.append({
            "name": node.get("name"), "kind": node.get("kind"),
            "qualified_name": node.get("qualified_name"), "file_path": path,
            "start_line": start, "end_line": end,
        })
    candidates.sort(key=lambda n: (n["file_path"], n["start_line"], str(n["qualified_name"])))
    nodes = candidates[:max_nodes]
    selected = {n["qualified_name"] for n in nodes}
    known = {
        n.get("qualified_name"): n
        for n in [*(impact.get("changed_nodes") or []), *(impact.get("impacted_nodes") or [])]
    }

    def endpoint(qualified: str, kind: str) -> dict:
        node = known.get(qualified) or {}
        return {"qualified_name": qualified, "file_path": _relative(repo, node.get("file_path")),
                "start_line": node.get("line_start") or 0, "kind": kind}

    callers, callees = [], []
    for edge in impact.get("edges") or []:
        source, target, kind = edge.get("source"), edge.get("target"), edge.get("kind", "")
        if target in selected and source not in selected:
            callers.append(endpoint(source, kind))
        if source in selected and target not in selected:
            callees.append(endpoint(target, kind))

    def unique(rows: list[dict]) -> list[dict]:
        seen = {json.dumps(r, sort_keys=True): r for r in rows}
        return sorted(seen.values(), key=lambda r: (r["file_path"], r["start_line"],
                                                    r["qualified_name"], r["kind"]))

    callers, callees = unique(callers), unique(callees)
    return {
        "orientation": "impact",
        "nodes": nodes,
        "callers": callers[:max_callers],
        "callees": callees[:max_callees],
        "truncated": {
            "nodes": len(candidates) > max_nodes,
            "callers": len(callers) > max_callers or bool(impact.get("truncated")),
            "callees": len(callees) > max_callees or bool(impact.get("truncated")),
        },
    }


def graph_context(
    live_probe: bool,
    receipt_path: Path | None,
    changed_files: list[str],
    ranges: dict[str, list[list[int]]],
    repo: Path,
    head: str,
    max_nodes: int,
    max_callers: int,
    max_callees: int,
) -> dict:
    """Graph section of the pack, from a status receipt and the CLI JSON only."""
    limits = {"nodes": max_nodes, "callers": max_callers, "callees": max_callees}
    if receipt_path is not None:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        status = str((receipt.get("readiness") or {}).get("status") or "unavailable")
        identity = receipt.get("source_identity") or {}
        if status not in READY_STATUSES:
            raise PackError(f"{PREP_MARKER}: graph receipt status is {status}; "
                            "run crg-heal once and re-read the status")
        if receipt.get("repo_root") != str(repo) or receipt.get("current_sha") != head:
            raise PackError(f"{PREP_MARKER}: graph receipt is not for this checkout at the head")
    elif live_probe:
        report = probe(repo, changed_files, head)
        if report["verdict"] not in {"ready", "degraded"}:
            return {**_empty_graph("fallback", report["changed_files"], limits),
                    "fallback_reason": f"graph-{report['graph_status']}"}
        receipt, status = report["receipt"], report["graph_status"]
        identity = receipt.get("source_identity") or {}
    else:
        changed = changed_dispositions(changed_files, False, "receipt-omitted", {}, set())
        return {**_empty_graph("fallback", changed, limits), "fallback_reason": "no-graph-receipt"}
    changed = changed_dispositions(changed_files, True, status, identity, set())
    indexed = [item["path"] for item in changed if item["disposition"] == "indexed"]
    graph = _empty_graph("receipt", changed, limits)
    graph["receipt"] = _receipt_summary(receipt)
    graph["degraded"] = status != "ok"
    graph.update(orientation(repo, indexed, ranges, max_nodes, max_callers, max_callees))
    return graph


def canonical_bytes(value: dict) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def empty_domain_context(base: str) -> dict:
    """Same key shape as route_domain_cards.route(), all-empty: no router was available."""
    return {
        "schema": "{{pack_schema}}/domain-routing/1",
        "base_sha": base,
        "dirty_files": [],
        "selected": [],
        "candidate_count": 0,
        "truncated": False,
        "truncated_candidates": [],
        "unselected_files": [],
        "unselected_symbols": [],
        "unmatched_files": [],
        "unmatched_symbols": [],
        "surfaces": [],
    }


def domain_context(
    router_path: Path | None,
    cards_path: Path | None,
    repo: Path,
    base: str,
    changed_files: list[str],
    symbols: list[dict],
) -> dict:
    if router_path is None or not router_path.is_file():
        return empty_domain_context(base)
    spec = importlib.util.spec_from_file_location("pms_domain_router", router_path)
    if spec is None or spec.loader is None:
        raise PackError(f"cannot load domain router: {router_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cards = (
        json.loads(cards_path.read_text(encoding="utf-8"))
        if cards_path is not None
        else {"schema": "{{pack_schema}}/domain-cards/1", "cards": []}
    )
    return module.route(
        cards,
        repo,
        base,
        changed_files,
        sorted({item["name"] for item in symbols}),
        3,
    )


def build(args: argparse.Namespace) -> Path:
    repo = args.repo.resolve()
    if not repo.is_dir():
        raise PackError(f"repository does not exist: {repo}")
    base = commit_sha(repo, args.base)
    head = commit_sha(repo, args.head)
    merge_base = git(repo, "merge-base", base, head).strip()
    patch = git_bytes(repo, "diff", "--binary", "--full-index", "--find-renames",
                      "--no-ext-diff", f"{base}...{head}", "--")
    diff_sha = hashlib.sha256(patch).hexdigest()
    changed_raw = git_bytes(repo, "diff", "--name-only", "-z", f"{base}...{head}", "--")
    changed_files = sorted(path.decode("utf-8") for path in changed_raw.split(b"\0") if path)
    hunks, ranges, symbols, hunk_artifacts = parse_hunks(patch)
    graph = graph_context(
        args.graph_db is not None, args.graph_receipt, changed_files, ranges, repo, head,
        args.max_graph_nodes, args.max_graph_callers, args.max_graph_callees,
    )
    router = args.domain_router if args.domain_router is not None else DEFAULT_DOMAIN_ROUTER
    domain_router_active = router is not None and router.is_file()
    domain = domain_context(router, args.domain_cards, repo, base, changed_files, symbols)
    oversized_hunks = [
        hunk["id"] for hunk in hunks if hunk["bytes"] > args.max_hunk_bytes
    ]
    oversized = len(patch) > args.max_diff_bytes or bool(oversized_hunks)
    core = {
        "schema": "{{pack_schema}}/pr-context-pack/1",
        "pr": str(args.pr),
        "repo": str(repo),
        "git": {
            "base_sha": base,
            "head_sha": head,
            "merge_base_sha": merge_base,
            "diff_mode": "three-dot",
            "diff_sha256": diff_sha,
            "head_committed_at": commit_time(repo, head),
        },
        "changed_files": changed_files,
        "changed_line_ranges": ranges,
        "hunks": hunks,
        "diff_symbols": symbols,
        "graph": graph,
        "domain": domain,
        "review_scope": {
            "status": "oversized" if oversized else "ready",
            "diff_bytes": len(patch),
            "max_diff_bytes": args.max_diff_bytes,
            "max_hunk_bytes": args.max_hunk_bytes,
            "oversized_hunks": oversized_hunks,
            "action": "escalate-or-split" if oversized else "review",
        },
        "capabilities": {
            "git": True,
            "graph": graph["status"] == "receipt",
            "domain": domain_router_active,
            "text_fallback": True,
            "semantic_review": not oversized,
        },
    }
    pack_id = hashlib.sha256(canonical_bytes(core) + b"\0" + patch).hexdigest()
    manifest = dict(core)
    manifest["pack_id"] = pack_id
    jsonschema.validate(manifest, json.loads(CONTEXT_SCHEMA.read_text(encoding="utf-8")))
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / f"pr-{args.pr}-{head[:12]}-{diff_sha[:12]}"
    if target.exists():
        existing_path = target / "context-pack.json"
        existing_patch = target / "diff.patch"
        if not existing_path.is_file() or not existing_patch.is_file():
            raise PackError(f"immutable pack path is incomplete: {target}")
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
        existing_sha = hashlib.sha256(existing_patch.read_bytes()).hexdigest()
        if existing.get("pack_id") != pack_id or existing_sha != diff_sha:
            raise PackError(f"immutable pack collision: {target}")
        for hunk in existing.get("hunks", []):
            artifact = target / hunk.get("artifact", "")
            if (
                not artifact.is_file()
                or len(artifact.read_bytes()) != hunk.get("bytes")
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != hunk.get("sha256")
            ):
                raise PackError(f"immutable hunk artifact mismatch: {artifact}")
        return target
    temporary = Path(tempfile.mkdtemp(prefix=target.name + ".", dir=output_root))
    try:
        (temporary / "diff.patch").write_bytes(patch)
        for relative, content in hunk_artifacts.items():
            artifact = temporary / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(content)
        (temporary / "context-pack.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--graph-db", type=Path,
        help="deprecated: the path is ignored; probes the live graph through the CLI instead",
    )
    parser.add_argument("--graph-receipt", type=Path,
                        help="code-review-graph status --json output for the review head")
    parser.add_argument(
        "--domain-router", type=Path,
        help="route_domain_cards.py (default: the harness copy when installed); "
             "missing means no domain enrichment",
    )
    parser.add_argument("--domain-cards", type=Path)
    parser.add_argument("--max-diff-bytes", type=int, default=DEFAULT_MAX_DIFF_BYTES)
    parser.add_argument("--max-hunk-bytes", type=int, default=DEFAULT_MAX_HUNK_BYTES)
    parser.add_argument("--max-graph-nodes", type=int, default=DEFAULT_MAX_GRAPH_NODES)
    parser.add_argument("--max-graph-callers", type=int, default=DEFAULT_MAX_GRAPH_CALLERS)
    parser.add_argument("--max-graph-callees", type=int, default=DEFAULT_MAX_GRAPH_CALLEES)
    args = parser.parse_args()
    if args.graph_db is not None and args.graph_receipt is not None:
        parser.error("--graph-db and --graph-receipt are mutually exclusive")
    for name in (
        "max_diff_bytes", "max_hunk_bytes", "max_graph_nodes",
        "max_graph_callers", "max_graph_callees",
    ):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    try:
        target = build(args)
    except (
        PackError, OSError, subprocess.CalledProcessError,
        jsonschema.ValidationError,
    ) as error:
        parser.exit(2, f"build_context_pack.py: {error}\n")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
