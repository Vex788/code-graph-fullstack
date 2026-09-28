"""Registry of every node and edge kind the graph stores.

This table is the single source for per-kind traversal weights, impact
direction and presentation. ``constants.py`` and ``communities.py`` derive
their weight tables from it, and ``scripts/gen_kinds.py`` renders
``docs/spec/EDGES.md`` and the VS Code ``src/generated/kinds.ts`` from it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Optional

# Impact propagation relative to the stored edge (source -> target).
DIRECTION_INCOMING = "incoming"
DIRECTION_OUTGOING = "outgoing"
DIRECTION_NONE = "none"

FALLBACK_NODE_COLOR = "#cdd6f4"
FALLBACK_EDGE_COLOR = "#8b949e"

# Release that introduced the newest node and edge kinds. Kinds marked with
# it are emitted by the fs.6 cross-stack resolvers.
SINCE_FS6 = "fs.6"


@dataclass(frozen=True)
class EdgeKind:
    name: str
    # None: follow the default impact direction (incoming) without an
    # explicit entry in IMPACT_EDGE_DIRECTIONS.
    direction: Optional[str] = None
    # None: use IMPACT_DEFAULT_EDGE_WEIGHT / the community default.
    impact_weight: Optional[float] = None
    community_weight: Optional[float] = None
    # A changed target re-parses the files holding these edges.
    dependents_trigger: bool = False
    # Connects different layers of a stack (page <-> server code).
    cross_stack: bool = False
    # Counted as callers by change-risk scoring.
    risk_counts: bool = False
    vscode_color: str = FALLBACK_EDGE_COLOR
    since: Optional[str] = None
    description: str = ""


@dataclass(frozen=True)
class NodeKind:
    name: str
    vscode_color: str = FALLBACK_NODE_COLOR
    since: Optional[str] = None
    description: str = ""


NODE_KINDS: tuple[NodeKind, ...] = (
    NodeKind("File", "#58a6ff", description="A source file"),
    NodeKind("Class", "#f0883e", description="Class, interface, struct, enum or record"),
    NodeKind("Function", "#3fb950", description="Function, method or constructor"),
    NodeKind("Type", "#8b949e", description="Type alias or type declaration"),
    NodeKind("Test", "#d2a8ff", description="Test function"),
    NodeKind("Endpoint", "#89dceb", description="HTTP route exposed by a handler"),
    NodeKind("ConfigProperty", "#f9e2af", description="Configuration key"),
    NodeKind("Scheduler", "#fab387", description="Scheduled job trigger"),
    NodeKind("Event", "#f38ba8", description="Application event published or handled"),
    NodeKind("Table", "#94e2d5", since=SINCE_FS6, description="Database table of an ORM mapping"),
)

EDGE_KINDS: tuple[EdgeKind, ...] = (
    EdgeKind(
        "CALLS", DIRECTION_INCOMING, 1.0, 1.0,
        dependents_trigger=True, risk_counts=True, vscode_color="#3fb950",
        description="Caller invokes callee",
    ),
    EdgeKind(
        "INHERITS", DIRECTION_INCOMING, 0.9, 0.8,
        dependents_trigger=True, vscode_color="#d2a8ff",
        description="Subtype extends supertype",
    ),
    EdgeKind(
        "OVERRIDES", DIRECTION_INCOMING, 0.9,
        vscode_color="#cba6f7", description="Method overrides a supertype method",
    ),
    EdgeKind(
        "IMPLEMENTS", DIRECTION_INCOMING, 0.9, 0.7,
        dependents_trigger=True, vscode_color="#f9e2af",
        description="Class implements an interface",
    ),
    EdgeKind(
        "TESTED_BY", DIRECTION_OUTGOING, 0.7, 0.4,
        vscode_color="#f38ba8", description="Production code -> the test covering it",
    ),
    EdgeKind(
        "REFERENCES", DIRECTION_INCOMING, 0.6,
        vscode_color="#94e2d5", description="Symbol mentions another symbol",
    ),
    EdgeKind(
        "DEPENDS_ON", DIRECTION_INCOMING, 0.6, 0.6,
        vscode_color="#fab387", description="Generic dependency",
    ),
    EdgeKind(
        "IMPORTS_FROM", DIRECTION_INCOMING, 0.5, 0.5,
        dependents_trigger=True, vscode_color="#f0883e",
        description="Module imports from another module",
    ),
    # Presentation layer: the page depends on what it renders, requests and
    # includes. RENDERS names one class deliberately, like an explicit reference;
    # REQUESTS matches a URL against a route and is commonly many-to-one;
    # INCLUDES is file-granular, like an import.
    EdgeKind(
        "RENDERS", DIRECTION_INCOMING, 0.6,
        cross_stack=True, vscode_color="#89b4fa",
        description="Page renders a server class (useActionBean, bean binding)",
    ),
    EdgeKind(
        "REQUESTS", DIRECTION_INCOMING, 0.5,
        cross_stack=True, vscode_color="#74c7ec",
        description="Page script requests a server route",
    ),
    EdgeKind(
        "INCLUDES", DIRECTION_INCOMING, 0.5,
        cross_stack=True, vscode_color="#b4befe",
        description="Page includes another page or script file",
    ),
    EdgeKind(
        "CONTAINS", DIRECTION_NONE, 0.3, 0.3,
        vscode_color="rgba(139,148,158,0.15)", description="Parent contains child",
    ),
    EdgeKind("INJECTS", vscode_color="#a6e3a1", description="Dependency injection point"),
    EdgeKind("HANDLES", vscode_color="#89dceb", description="Handler serves an endpoint"),
    EdgeKind("TRIGGERS", vscode_color="#fab387", description="Scheduler triggers a job"),
    EdgeKind("PUBLISHES", vscode_color="#f5c2e7", description="Code publishes an event"),
    EdgeKind("CONSUMES", vscode_color="#eba0ac", description="Code consumes a message"),
    EdgeKind("PRODUCES", vscode_color="#f5e0dc", description="Code produces a message"),
    EdgeKind(
        "TEMPORAL_STUB", vscode_color="#9399b2",
        description="Unresolved Temporal workflow/activity stub",
    ),
    EdgeKind(
        "DEPENDS_ON_CONFIG", vscode_color="#f9e2af",
        description="Code reads a configuration key",
    ),
    # Cross-stack kinds for the Stripes/JSP stack, introduced in fs.6. Like
    # RENDERS/REQUESTS they pull impact from the page back to the server side.
    EdgeKind(
        "FORWARDS_TO", DIRECTION_INCOMING, 0.6,
        cross_stack=True, vscode_color="#89b4fa", since=SINCE_FS6,
        description="Action forwards or redirects to a page",
    ),
    EdgeKind(
        "BINDS", DIRECTION_INCOMING, 0.5,
        cross_stack=True, vscode_color="#94e2d5", since=SINCE_FS6,
        description="Form field binds to a bean property",
    ),
    EdgeKind(
        "USES_STYLE", DIRECTION_INCOMING, 0.4,
        cross_stack=True, vscode_color="#f2cdcd", since=SINCE_FS6,
        description="Page uses a stylesheet class selector",
    ),
    EdgeKind(
        "MAPS_TO", DIRECTION_INCOMING, 0.6,
        cross_stack=True, vscode_color="#74c7ec", since=SINCE_FS6,
        description="Entity or mapping file maps to a database table",
    ),
)

EDGE_KINDS_BY_NAME: dict[str, EdgeKind] = {k.name: k for k in EDGE_KINDS}
NODE_KINDS_BY_NAME: dict[str, NodeKind] = {k.name: k for k in NODE_KINDS}


def impact_edge_weights() -> dict[str, float]:
    return {k.name: k.impact_weight for k in EDGE_KINDS if k.impact_weight is not None}


def impact_edge_directions() -> dict[str, str]:
    return {k.name: k.direction for k in EDGE_KINDS if k.direction is not None}


def community_edge_weights() -> dict[str, float]:
    return {
        k.name: k.community_weight for k in EDGE_KINDS if k.community_weight is not None
    }


def edge_kinds_where(**flags: bool) -> frozenset[str]:
    """Names of edge kinds whose boolean fields match every given flag."""
    return frozenset(
        k.name for k in EDGE_KINDS
        if all(getattr(k, field) == value for field, value in flags.items())
    )


def kinds_as_dict() -> dict[str, Any]:
    return {
        "node_kinds": [asdict(k) for k in NODE_KINDS],
        "edge_kinds": [asdict(k) for k in EDGE_KINDS],
    }


# ---------------------------------------------------------------------------
# Generated artifacts (scripts/gen_kinds.py writes them; tests check drift)
# ---------------------------------------------------------------------------

_GENERATED_NOTE = "Generated by scripts/gen_kinds.py from code_review_graph/kinds.py"


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else ""
    return str(value)


def render_edges_md() -> str:
    lines = [
        "# Node and edge kinds",
        "",
        f"<!-- {_GENERATED_NOTE}. Do not edit. -->",
        "",
        "Edges are stored as `source -> target`. `direction` says how impact",
        "propagates: `incoming` walks from a changed target back to its sources,",
        "`outgoing` from a changed source to its targets, `none` is not traversed.",
        "`-` means the default (incoming direction, weight 0.5 for impact;",
        "community default for clustering).",
        "",
        "## Edge kinds",
        "",
        "| Kind | Direction | Impact weight | Community weight | Dependents | "
        "Cross-stack | Risk callers | Since | Description |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for k in EDGE_KINDS:
        lines.append(
            f"| `{k.name}` | {_fmt(k.direction)} | {_fmt(k.impact_weight)} | "
            f"{_fmt(k.community_weight)} | {_fmt(k.dependents_trigger)} | "
            f"{_fmt(k.cross_stack)} | {_fmt(k.risk_counts)} | {k.since or ''} | "
            f"{k.description} |"
        )
    lines += ["", "## Node kinds", "", "| Kind | Since | Description |", "|---|---|---|"]
    for n in NODE_KINDS:
        lines.append(f"| `{n.name}` | {n.since or ''} | {n.description} |")
    return "\n".join(lines) + "\n"


def render_kinds_ts() -> str:
    node_colors = {n.name: n.vscode_color for n in NODE_KINDS}
    edge_colors = {k.name: k.vscode_color for k in EDGE_KINDS}
    return (
        f"// {_GENERATED_NOTE}. Do not edit.\n\n"
        f"export const NODE_KINDS = {json.dumps([n.name for n in NODE_KINDS])} as const;\n"
        f"export const EDGE_KINDS = {json.dumps([k.name for k in EDGE_KINDS])} as const;\n\n"
        "export type KnownNodeKind = (typeof NODE_KINDS)[number];\n"
        "export type KnownEdgeKind = (typeof EDGE_KINDS)[number];\n\n"
        "export const NODE_KIND_COLORS: Record<string, string> = "
        f"{json.dumps(node_colors, indent=2)};\n\n"
        "export const EDGE_KIND_COLORS: Record<string, string> = "
        f"{json.dumps(edge_colors, indent=2)};\n\n"
        f"export const FALLBACK_NODE_COLOR = {json.dumps(FALLBACK_NODE_COLOR)};\n"
        f"export const FALLBACK_EDGE_COLOR = {json.dumps(FALLBACK_EDGE_COLOR)};\n"
    )
