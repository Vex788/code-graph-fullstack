"""Harness targets for the graph kit, read from ``targets.toml``."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_tomllib: Any = importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")

HARNESS_DIR = Path(__file__).resolve().parent
KIT_DIR = HARNESS_DIR / "kit"
TARGETS_FILE = HARNESS_DIR / "targets.toml"


@dataclass(frozen=True)
class Region:
    file: str
    id: str
    block: str


@dataclass(frozen=True)
class Target:
    name: str
    default_root: str | None = None
    command_root: str | None = None
    hooks_dir: str = "hooks"
    skills_dir: str = "skills"
    data_dir: str = "hooks"
    edit_matcher: str = "Edit|Write|MultiEdit"
    regions_only: bool = False
    events: dict[str, str] = field(default_factory=dict)
    vars: dict[str, str] = field(default_factory=dict)
    regions: tuple[Region, ...] = ()

    def resolve_root(self, root: str | Path | None) -> Path:
        if root is not None:
            return Path(root).expanduser()
        if self.default_root is None:
            raise ValueError(f"target {self.name!r} has no default root; pass --root")
        return Path(self.default_root).expanduser()

    def event(self, key: str) -> str:
        return self.events.get(key, key)


def load_targets(path: Path = TARGETS_FILE) -> dict[str, Target]:
    data = _tomllib.loads(path.read_text(encoding="utf-8"))
    targets: dict[str, Target] = {}
    for name, raw in data.get("targets", {}).items():
        spec = dict(raw)
        regions = tuple(Region(**r) for r in spec.pop("regions", []))
        targets[name] = Target(name=name, regions=regions, **spec)
    return targets


def get_target(name: str, path: Path = TARGETS_FILE) -> Target:
    targets = load_targets(path)
    if name not in targets:
        raise ValueError(f"unknown target {name!r}; expected one of {sorted(targets)}")
    return targets[name]
