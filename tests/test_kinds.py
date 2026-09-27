"""The kind registry drives weights and generated artifacts without drift."""

from pathlib import Path

from code_review_graph import communities, constants
from code_review_graph.kinds import (
    EDGE_KINDS,
    NODE_KINDS,
    PLANNED_SINCE,
    edge_kinds_where,
    render_edges_md,
    render_kinds_ts,
)

ROOT = Path(__file__).resolve().parent.parent

# Values before the registry existed; derivation must not change behaviour.
PRE_REGISTRY_IMPACT_WEIGHTS = {
    "CALLS": 1.0, "INHERITS": 0.9, "OVERRIDES": 0.9, "IMPLEMENTS": 0.9,
    "TESTED_BY": 0.7, "REFERENCES": 0.6, "DEPENDS_ON": 0.6, "IMPORTS_FROM": 0.5,
    "RENDERS": 0.6, "REQUESTS": 0.5, "INCLUDES": 0.5, "CONTAINS": 0.3,
}
PRE_REGISTRY_DIRECTIONS = {
    "CALLS": "incoming", "INHERITS": "incoming", "OVERRIDES": "incoming",
    "IMPLEMENTS": "incoming", "TESTED_BY": "outgoing", "REFERENCES": "incoming",
    "DEPENDS_ON": "incoming", "IMPORTS_FROM": "incoming", "RENDERS": "incoming",
    "REQUESTS": "incoming", "INCLUDES": "incoming", "CONTAINS": "none",
}
PRE_REGISTRY_COMMUNITY_WEIGHTS = {
    "CALLS": 1.0, "IMPORTS_FROM": 0.5, "INHERITS": 0.8, "IMPLEMENTS": 0.7,
    "CONTAINS": 0.3, "TESTED_BY": 0.4, "DEPENDS_ON": 0.6,
}


def test_impact_weights_unchanged():
    assert constants.IMPACT_EDGE_WEIGHTS == PRE_REGISTRY_IMPACT_WEIGHTS


def test_impact_directions_unchanged():
    assert constants.IMPACT_EDGE_DIRECTIONS == PRE_REGISTRY_DIRECTIONS
    assert constants.IMPACT_DIRECTION_INCOMING == "incoming"
    assert constants.IMPACT_DIRECTION_OUTGOING == "outgoing"
    assert constants.IMPACT_DIRECTION_NONE == "none"


def test_community_weights_unchanged():
    assert communities.EDGE_WEIGHTS == PRE_REGISTRY_COMMUNITY_WEIGHTS


def test_names_unique():
    edge_names = [k.name for k in EDGE_KINDS]
    node_names = [k.name for k in NODE_KINDS]
    assert len(edge_names) == len(set(edge_names))
    assert len(node_names) == len(set(node_names))


def test_emitted_kinds_registered():
    emitted_edges = {
        "CALLS", "IMPORTS_FROM", "INHERITS", "IMPLEMENTS", "OVERRIDES", "CONTAINS",
        "TESTED_BY", "DEPENDS_ON", "REFERENCES", "RENDERS", "REQUESTS", "INCLUDES",
        "INJECTS", "HANDLES", "TRIGGERS", "PUBLISHES", "CONSUMES", "PRODUCES",
        "TEMPORAL_STUB", "DEPENDS_ON_CONFIG",
    }
    emitted_nodes = {
        "File", "Class", "Function", "Type", "Test", "Endpoint", "ConfigProperty",
        "Scheduler", "Event",
    }
    assert emitted_edges <= {k.name for k in EDGE_KINDS}
    assert emitted_nodes <= {k.name for k in NODE_KINDS}


def test_planned_kinds_marked():
    planned = {k.name for k in EDGE_KINDS if k.since == PLANNED_SINCE}
    assert planned == {"FORWARDS_TO", "BINDS", "USES_STYLE", "MAPS_TO", "HANDLES_EVENT"}
    assert planned <= edge_kinds_where(cross_stack=True)


def test_flag_sets_match_current_consumers():
    # incremental._single_hop_dependents and changes risk callers today.
    assert edge_kinds_where(dependents_trigger=True) == {
        "CALLS", "IMPORTS_FROM", "INHERITS", "IMPLEMENTS",
    }
    assert edge_kinds_where(risk_counts=True) == {"CALLS"}


def test_generated_edges_md_up_to_date():
    path = ROOT / "docs" / "spec" / "EDGES.md"
    assert path.read_text(encoding="utf-8") == render_edges_md(), (
        "docs/spec/EDGES.md is stale; run python scripts/gen_kinds.py"
    )


def test_generated_kinds_ts_up_to_date():
    path = ROOT / "code-review-graph-vscode" / "src" / "generated" / "kinds.ts"
    assert path.read_text(encoding="utf-8") == render_kinds_ts(), (
        "code-review-graph-vscode/src/generated/kinds.ts is stale; "
        "run python scripts/gen_kinds.py"
    )
