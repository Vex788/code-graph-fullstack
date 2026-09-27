#!/usr/bin/env python3
"""PostToolUse: refresh the code graph after an edit or a HEAD-moving git command.

Placeholder kit hook; the full observer lands with the kit content.
Exit 75 from the CLI means another writer holds the lock: a no-op here.
"""
import json
import re
import shutil
import subprocess
import sys

GIT_MOVES_HEAD = re.compile(
    r"(?:^|\s)git\s+(?:\S+\s+)*?(checkout|switch|merge|rebase|pull|reset|stash)(?![\w-])"
)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (OSError, ValueError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool == "Bash" and not GIT_MOVES_HEAD.search(str(tool_input.get("command") or "")):
        return 0
    if shutil.which("code-review-graph") is None:
        return 0
    cwd = event.get("cwd") or None
    rc = subprocess.run(
        ["code-review-graph", "update", "--skip-flows", "--if-locked=skip"],
        cwd=cwd, stdin=subprocess.DEVNULL, check=False,
    ).returncode
    if rc in (0, 75):
        return 0
    print(f"code-review-graph update failed (exit {rc})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
