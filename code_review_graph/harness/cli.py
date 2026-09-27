"""``code-review-graph harness …`` subcommands; also ``python -m code_review_graph.harness``."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from ..locking import EXIT_ERROR, EXIT_OK, EXIT_USAGE
from .targets import load_targets

# A pin is a git ref after the package's repository name, e.g.
# "git+https://github.com/Vex788/code-graph-fullstack@391609a".
PIN_LITERAL = re.compile(r"(code-graph-fullstack(?:\.git)?@)[A-Za-z0-9][\w.+-]*")
PIN_FILE = "crg.pin"
_TAG = re.compile(r"^[A-Za-z0-9][\w.+-]*$")


def bump_pin(tag: str, files: Sequence[str | Path]) -> tuple[list[str], list[str]]:
    """Point every pin in ``files`` at ``tag``; returns (changed, files with no pin)."""
    if not _TAG.match(tag):
        raise ValueError(f"not a valid tag: {tag!r}")
    from .apply import _atomic_write

    changed: list[str] = []
    no_pin: list[str] = []
    for name in files:
        path = Path(name)
        text = path.read_text(encoding="utf-8")
        if path.name == PIN_FILE:
            updated, count = f"{tag}\n", 1
        else:
            updated, count = PIN_LITERAL.subn(lambda m: m.group(1) + tag, text)
        if count == 0:
            no_pin.append(str(path))
        elif updated != text:
            _atomic_write(path, updated.encode("utf-8"), path.stat().st_mode & 0o777)
            changed.append(str(path))
    return changed, no_pin


def _add_commands(parser: argparse.ArgumentParser) -> None:
    targets = sorted(load_targets())
    sub = parser.add_subparsers(dest="harness_command", required=True)

    apply_cmd = sub.add_parser("apply", help="Write the graph kit's files into a harness root")
    apply_cmd.add_argument("--target", required=True, choices=targets)
    apply_cmd.add_argument("--root", help="Harness root (default: the target's default_root)")
    apply_cmd.add_argument("--kit-dir", help="Kit directory (default: the packaged kit)")
    mode = apply_cmd.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Write nothing; exit 1 on drift")
    mode.add_argument("--dry-run", action="store_true", help="Show what apply would do")
    mode.add_argument("--revert", action="store_true", help="Restore the newest backup")
    apply_cmd.add_argument(
        "--adopt", action="store_true", help="Take over modified or unowned files",
    )
    apply_cmd.add_argument(
        "--files-only", action="store_true",
        help="Accepted for laya; apply only ever writes files, never config",
    )

    frag_cmd = sub.add_parser("fragment", help="Print the config fragment laya merges")
    frag_cmd.add_argument("--target", required=True, choices=targets)
    frag_cmd.add_argument("--json", action="store_true", help="JSON output (the only format)")

    pin_cmd = sub.add_parser("bump-pin", help="Rewrite code-graph-fullstack pins to a tag")
    pin_cmd.add_argument("--tag", required=True)
    pin_cmd.add_argument("--files", nargs="+", required=True)


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``harness`` to the main CLI's subparsers; dispatch with ``run(args)``."""
    parser = subparsers.add_parser("harness", help="Graph kit files and config fragment")
    _add_commands(parser)
    parser.set_defaults(harness_run=run)
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        if args.harness_command == "apply":
            return _run_apply(args)
        if args.harness_command == "fragment":
            from .fragment import fragment

            sys.stdout.write(json.dumps(fragment(args.target), indent=2) + "\n")
            return EXIT_OK
        if args.harness_command == "bump-pin":
            changed, no_pin = bump_pin(args.tag, args.files)
            for path in changed:
                print(f"PINNED  {path} -> {args.tag}")
            for path in no_pin:
                print(f"NO PIN  {path}", file=sys.stderr)
            return EXIT_ERROR if no_pin else EXIT_OK
    except (ValueError, OSError) as exc:
        print(f"code-review-graph harness: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_USAGE


def _run_apply(args: argparse.Namespace) -> int:
    from .apply import apply
    from .targets import KIT_DIR

    report = apply(
        args.target,
        args.root,
        kit_dir=Path(args.kit_dir) if args.kit_dir else KIT_DIR,
        check=args.check,
        dry_run=args.dry_run,
        adopt=args.adopt,
        revert=args.revert,
    )
    for line in report.lines():
        print(line)
    if report.mode == "check" and report.status == EXIT_OK:
        print(f"harness apply --check: {report.target} clean")
    return report.status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-review-graph harness")
    _add_commands(parser)
    args = parser.parse_args(argv)
    return run(args)
