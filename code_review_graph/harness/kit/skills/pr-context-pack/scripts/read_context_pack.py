#!/usr/bin/env python3
"""Read a verified, bounded slice from an immutable pull-request context pack."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


class ContextReadError(ValueError):
    pass


def canonical_bytes(value: dict) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def load_verified(context_path: Path) -> tuple[dict, Path]:
    context_path = context_path.resolve()
    pack_dir = context_path.parent
    pack = json.loads(context_path.read_text(encoding="utf-8"))
    patch_path = pack_dir / "diff.patch"
    patch = patch_path.read_bytes()
    if hashlib.sha256(patch).hexdigest() != pack["git"]["diff_sha256"]:
        raise ContextReadError("diff.patch hash does not match context-pack.json")
    core = dict(pack)
    pack_id = core.pop("pack_id")
    if hashlib.sha256(canonical_bytes(core) + b"\0" + patch).hexdigest() != pack_id:
        raise ContextReadError("context pack id does not match its immutable content")
    return pack, pack_dir


def summary(pack: dict) -> dict:
    graph = pack["graph"]
    return {
        "schema": "{{pack_schema}}/pr-context-summary/1",
        "pack_id": pack["pack_id"],
        "pr": pack["pr"],
        "git": pack["git"],
        "review_scope": pack["review_scope"],
        "changed_files": pack["changed_files"],
        "changed_line_ranges": pack["changed_line_ranges"],
        "hunks": pack["hunks"],
        "diff_symbols": pack["diff_symbols"],
        "graph": {
            "status": graph["status"],
            "changed_files": graph["changed_files"],
            "limits": graph["limits"],
            "truncated": graph["truncated"],
            "counts": {
                "nodes": len(graph["nodes"]),
                "callers": len(graph["callers"]),
                "callees": len(graph["callees"]),
            },
        },
        "domain": pack["domain"],
        "capabilities": pack["capabilities"],
    }


def read_hunk(pack: dict, pack_dir: Path, hunk_id: str, max_bytes: int) -> bytes:
    matches = [hunk for hunk in pack["hunks"] if hunk["id"] == hunk_id]
    if len(matches) != 1:
        raise ContextReadError(f"unknown hunk id: {hunk_id}")
    hunk = matches[0]
    artifact = (pack_dir / hunk["artifact"]).resolve()
    if artifact.parent != (pack_dir / "hunks").resolve():
        raise ContextReadError("hunk artifact escapes the immutable pack")
    content = artifact.read_bytes()
    if len(content) != hunk["bytes"] or hashlib.sha256(content).hexdigest() != hunk["sha256"]:
        raise ContextReadError(f"hunk artifact integrity mismatch: {hunk_id}")
    if len(content) > max_bytes:
        raise ContextReadError(
            f"hunk {hunk_id} is {len(content)} bytes, above --max-bytes={max_bytes}; "
            "inspect the governing method with a fixed-range Git/RTK read"
        )
    return content


def file_slice(pack: dict, path: str) -> dict:
    graph = pack["graph"]
    return {
        "schema": "{{pack_schema}}/pr-context-file/1",
        "pack_id": pack["pack_id"],
        "path": path,
        "changed_line_ranges": pack["changed_line_ranges"].get(path, []),
        "hunks": [hunk for hunk in pack["hunks"] if hunk["path"] == path],
        "diff_symbols": [item for item in pack["diff_symbols"] if item["path"] == path],
        "graph_nodes": [item for item in graph["nodes"] if item["file_path"] == path],
        "graph_limits": graph["limits"],
        "graph_truncated": graph["truncated"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("summary", "file", "hunk"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--context", required=True, type=Path)
    subparsers.choices["file"].add_argument("--path", required=True)
    subparsers.choices["hunk"].add_argument("--hunk", required=True)
    subparsers.choices["hunk"].add_argument("--max-bytes", type=int, default=65_536)
    args = parser.parse_args()
    try:
        pack, pack_dir = load_verified(args.context)
        if args.command == "summary":
            print(json.dumps(summary(pack), indent=2, sort_keys=True, ensure_ascii=False))
        elif args.command == "file":
            print(json.dumps(file_slice(pack, args.path), indent=2, sort_keys=True,
                             ensure_ascii=False))
        else:
            if args.max_bytes < 1:
                raise ContextReadError("--max-bytes must be positive")
            content = read_hunk(pack, pack_dir, args.hunk, args.max_bytes)
            print(content.decode("utf-8", errors="replace"), end="")
    except (OSError, KeyError, json.JSONDecodeError, ContextReadError) as error:
        parser.exit(2, f"read_context_pack.py: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
