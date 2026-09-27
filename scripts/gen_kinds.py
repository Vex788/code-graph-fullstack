"""Render the kind registry into docs/spec/EDGES.md and the VS Code kinds.ts.

Usage: python scripts/gen_kinds.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from code_review_graph.kinds import render_edges_md, render_kinds_ts  # noqa: E402

TARGETS = {
    ROOT / "docs" / "spec" / "EDGES.md": render_edges_md,
    ROOT / "code-review-graph-vscode" / "src" / "generated" / "kinds.ts": render_kinds_ts,
}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, render in TARGETS.items():
        text = render()
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        if check:
            stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT)}")
    for path in stale:
        print(f"stale: {path.relative_to(ROOT)} (run scripts/gen_kinds.py)")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
