"""Load ``expected_edges.tsv`` and match its rows against a built graph.

The matching rules are documented in the TSV header; this module is the
single implementation of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoldenRow:
    kind: str
    source: str
    target: str
    status: str

    def __str__(self) -> str:
        return f"{self.kind} {self.source} -> {self.target} [{self.status}]"


def load_rows(path: Path) -> list[GoldenRow]:
    rows: list[GoldenRow] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line.startswith("kind\t"):
            continue
        kind, source, target, status = line.split("\t")
        if status not in ("now", "target"):
            raise ValueError(f"unknown status {status!r} in {line!r}")
        rows.append(GoldenRow(kind, source, target, status))
    return rows


def _tail(name: str) -> str:
    return name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


class GraphEdges:
    """Repo-relative view of a store's edges and the nodes they point at."""

    def __init__(self, store, repo: Path) -> None:
        prefix = str(repo) + "/"

        def rel(value: str) -> str:
            return value[len(prefix):] if value.startswith(prefix) else value

        conn = store._conn
        self.nodes: dict[str, tuple[str, str, dict]] = {}
        for kind, name, qualified, extra in conn.execute(
            "SELECT kind, name, qualified_name, extra FROM nodes"
        ):
            try:
                parsed = json.loads(extra) if extra else {}
            except ValueError:
                parsed = {}
            self.nodes[rel(qualified)] = (kind, name, parsed if isinstance(parsed, dict) else {})
        self.edges: set[tuple[str, str, str]] = {
            (kind, rel(source), rel(target))
            for kind, source, target in conn.execute(
                "SELECT kind, source_qualified, target_qualified FROM edges"
            )
        }

    def _source_ok(self, golden: str, stored: str) -> bool:
        if stored == golden:
            return True
        return "::" not in golden and stored.startswith(golden + "::")

    def _target_ok(self, golden: str, stored: str, exact: bool) -> bool:
        if stored == golden:
            return True
        if golden.startswith("route:"):
            node = self.nodes.get(stored)
            route = golden[len("route:"):]
            return bool(node and node[0] == "Endpoint" and node[2].get("route") == route)
        if golden.startswith("table:"):
            node = self.nodes.get(stored)
            table = golden[len("table:"):]
            return bool(node and node[0] == "Table" and node[1].lower() == table.lower())
        if exact:
            return False
        bare = "/" not in stored and "::" not in stored
        return bare and stored == _tail(golden)

    def has(self, row: GoldenRow) -> bool:
        exact = row.status == "target"
        return any(
            kind == row.kind
            and self._source_ok(row.source, source)
            and self._target_ok(row.target, target, exact)
            for kind, source, target in self.edges
        )
