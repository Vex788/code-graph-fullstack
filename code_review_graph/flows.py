"""Execution flow detection, tracing, and criticality scoring.

Detects entry points in the codebase (functions with no incoming CALLS edges,
framework-decorated handlers, and conventional name patterns), traces execution
paths via forward BFS through CALLS edges, scores each flow for criticality,
and persists results to the ``flows`` / ``flow_memberships`` tables.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from typing import Optional

from .constants import SECURITY_KEYWORDS as _SECURITY_KEYWORDS
from .graph import FlowAdjacency, GraphNode, GraphStore, _sanitize_name
from .parser import normalize_file_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Decorator patterns that indicate a function is a framework entry point.
# Each must match a decorator's whole name: the text before its argument
# list, without ``@``. A dotted Java name (``@org.x.Bean``) is also tried by
# its last segment. ``@Override`` alone says nothing about who calls a method.
_I = re.IGNORECASE
_FRAMEWORK_DECORATOR_PATTERNS: list[re.Pattern[str]] = [
    # Python web frameworks
    re.compile(r"app\.(get|post|put|delete|patch|route|websocket|on_event)", _I),
    re.compile(r"router\.(get|post|put|delete|patch|route)", _I),
    re.compile(r"blueprint\.(route|before_request|after_request)", _I),
    re.compile(r"(\w+\.)?(before|after)_(request|response)", _I),
    # CLI frameworks
    re.compile(r"click\.(command|group)", _I),
    re.compile(r"\w+\.(command|group)", _I),  # Click subgroups: @mygroup.command()
    # Pydantic validators/serializers
    re.compile(r"(field|model)_(serializer|validator)", _I),
    # Task queues
    re.compile(r"(\w+\.)?(task|shared_task|periodic_task)", _I),
    # Django
    re.compile(r"receiver", _I),
    re.compile(r"api_view", _I),
    re.compile(r"action", _I),
    # Testing
    re.compile(r"pytest\.(fixture|mark)(\.\w+)*"),
    re.compile(r"(override_settings|modify_settings)", _I),
    # SQLAlchemy / event systems
    re.compile(r"(event\.)?listens_for", _I),
    # Java Spring
    re.compile(r"(Get|Post|Put|Delete|Patch|Request)Mapping", _I),
    re.compile(r"(Scheduled|EventListener|Bean|Configuration)", _I),
    re.compile(r"KafkaListener", _I),
    # Temporal Java callbacks are invoked by the workflow runtime.
    re.compile(r"(WorkflowMethod|ActivityMethod)", _I),
    # Stripes event handlers and lifecycle interceptors
    re.compile(r"(DefaultHandler|HandlesEvent|Before|After|ValidationMethod)"),
    # JS/TS frameworks
    re.compile(r"(Component|Injectable|Controller|Module|Guard|Pipe)", _I),
    re.compile(r"(Subscribe|Mutation|Query|Resolver)", _I),
    # Express / Koa / Hono route handlers
    re.compile(r"(app|router)\.(get|post|put|delete|patch|use|all)"),
    # Android lifecycle
    re.compile(r"(OnLifecycleEvent|Composable)", _I),
    # Kotlin coroutines / Android ViewModel
    re.compile(r"(HiltViewModel|AndroidEntryPoint|Inject)", _I),
    # AI/agent frameworks (pydantic-ai, langchain, etc.)
    re.compile(r"\w+\.(tool|tool_plain|system_prompt|result_validator)", _I),
    re.compile(r"tool"),  # bare @tool (LangChain, etc.)
    # Middleware and exception handlers (Starlette, FastAPI, Sanic)
    re.compile(r"\w+\.(middleware|exception_handler|on_exception)", _I),
    # Generic route decorator (Flask blueprints: @bp.route, @auth_bp.route, etc.)
    re.compile(r"\w+\.route", _I),
]

# Name patterns that indicate conventional entry points.
_ENTRY_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^main$"),
    re.compile(r"^__main__$"),
    re.compile(r"^test_"),
    re.compile(r"^Test[A-Z]"),
    re.compile(r"^on_"),
    re.compile(r"^handle_"),
    # Lambda / serverless handler functions (wired via config, not code calls)
    re.compile(r"^handler$"),
    re.compile(r"^handle$"),
    re.compile(r"^lambda_handler$"),
    # Alembic migration entry points
    re.compile(r"^upgrade$"),
    re.compile(r"^downgrade$"),
    # FastAPI lifecycle / dependency injection
    re.compile(r"^lifespan$"),
    re.compile(r"^get_db$"),
    # Android Activity/Fragment lifecycle
    re.compile(r"^on(Create|Start|Resume|Pause|Stop|Destroy|Bind|Receive)"),
    # Servlet / JAX-RS
    re.compile(r"^do(Get|Post|Put|Delete)$"),
    # Python BaseHTTPRequestHandler
    re.compile(r"^do_(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)$"),
    re.compile(r"^log_message$"),
    # Express middleware signature
    re.compile(r"^(middleware|errorHandler)$"),
    # Angular lifecycle hooks
    re.compile(
        r"^ng(OnInit|OnChanges|OnDestroy|DoCheck"
        r"|AfterContentInit|AfterContentChecked|AfterViewInit|AfterViewChecked)$"
    ),
    # Angular Pipe / ControlValueAccessor / Guards / Resolvers
    re.compile(r"^(transform|writeValue|registerOnChange|registerOnTouched|setDisabledState)$"),
    re.compile(r"^(canActivate|canDeactivate|canActivateChild|canLoad|canMatch|resolve)$"),
    # React class component lifecycle
    re.compile(
        r"^(componentDidMount|componentDidUpdate|componentWillUnmount"
        r"|shouldComponentUpdate|render)$"
    ),
]

# Framework and language conventions that must not pollute other parsers.
_LANGUAGE_ENTRY_NAME_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "php": (
        re.compile(r"^(boot|register)$"),
        re.compile(r"^__invoke$"),
    ),
}


# ---------------------------------------------------------------------------
# Entry-point detection
# ---------------------------------------------------------------------------


def _has_framework_decorator(node: GraphNode) -> bool:
    """Return True if *node* has a decorator matching a framework pattern."""
    decorators = node.extra.get("decorators")
    if not decorators:
        return False
    if isinstance(decorators, str):
        decorators = [decorators]
    for dec in decorators:
        head = str(dec).strip().lstrip("@").split("(", 1)[0].strip()
        names = (head, head.rsplit(".", 1)[-1]) if "." in head else (head,)
        for pat in _FRAMEWORK_DECORATOR_PATTERNS:
            if any(pat.fullmatch(name) for name in names):
                return True
    return False


def _stripes_action_beans(store: GraphStore) -> set[tuple[str, str]]:
    """``(file, class)`` of every class that reaches Stripes ``ActionBean`` via INHERITS."""
    children: dict[str, list[str]] = {}
    for source, target in store._conn.execute(
        "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'INHERITS'"
    ):
        parent = target.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        children.setdefault(parent, []).append(source)
    beans: set[tuple[str, str]] = set()
    frontier = ["ActionBean"]
    while frontier:
        for child in children.get(frontier.pop(), ()):
            file_path, _, class_name = child.partition("::")
            key = (file_path, class_name.rsplit(".", 1)[-1])
            if key not in beans:
                beans.add(key)
                frontier.append(key[1])
    return beans


def _is_stripes_handler(node: GraphNode, action_beans: set[tuple[str, str]]) -> bool:
    """A method returning a Stripes ``Resolution`` on an ActionBean is an event handler."""
    return_type = (node.return_type or "").split("<", 1)[0].strip()
    return (
        node.language == "java"
        and return_type.rsplit(".", 1)[-1].endswith("Resolution")
        and (node.file_path, node.parent_name or "") in action_beans
    )


def _matches_entry_name(node: GraphNode) -> bool:
    """Return True if *node*'s name matches a conventional entry-point pattern."""
    for pat in _ENTRY_NAME_PATTERNS:
        if pat.search(node.name):
            return True
    for pat in _LANGUAGE_ENTRY_NAME_PATTERNS.get(node.language, ()):
        if pat.search(node.name):
            return True
    return False


_TEST_FILE_RE = re.compile(
    r"([\\/]__tests__[\\/]|\.spec\.[jt]sx?$|\.test\.[jt]sx?$|[\\/]test_[^/\\]*\.py$)",
)


def _is_test_file(file_path: str) -> bool:
    """Return True if *file_path* looks like a test file."""
    return bool(_TEST_FILE_RE.search(file_path))


def detect_entry_points(
    store: GraphStore,
    include_tests: bool = False,
) -> list[GraphNode]:
    """Find functions that are entry points in the graph.

    An entry point is a Function/Test node that either:
    1. Has no incoming CALLS edges (true root), or
    2. Has a framework decorator (e.g. ``@app.get``), or
    3. Matches a conventional name pattern (``main``, ``test_*``, etc.), or
    4. Returns a Stripes ``Resolution`` from an ``ActionBean`` implementor.

    When *include_tests* is False (the default), Test nodes are excluded so
    that flow analysis focuses on production entry points.
    """
    # Build a set of all qualified names that are CALLS targets. Exclude
    # edges sourced at File nodes so that script-/notebook-/top-level-only
    # callees (e.g. ``run_job()`` invoked from module scope, a top-level
    # ``<App />`` render) remain detectable as entry points.
    called_qnames = store.get_all_call_targets(include_file_sources=False)

    # Scan all nodes for entry-point candidates.
    candidate_nodes = store.get_nodes_by_kind(["Function", "Test"])
    action_beans = _stripes_action_beans(store)

    entry_points: list[GraphNode] = []
    seen_qn: set[str] = set()

    for node in candidate_nodes:
        if _is_entry_point(node, called_qnames, action_beans, include_tests) and (
            node.qualified_name not in seen_qn
        ):
            entry_points.append(node)
            seen_qn.add(node.qualified_name)

    return entry_points


def _is_entry_point(
    node: GraphNode,
    called_qnames: set[str],
    action_beans: set[tuple[str, str]],
    include_tests: bool,
) -> bool:
    if not include_tests and (node.is_test or _is_test_file(node.file_path)):
        return False
    if node.extra.get("verilog_kind"):
        return False
    return (
        # True root: no one calls this function.
        node.qualified_name not in called_qnames
        # Framework decorator match.
        or _has_framework_decorator(node)
        # Conventional name match.
        or _matches_entry_name(node)
        # Stripes dispatches events to handlers by name, not by call.
        or bool(action_beans and _is_stripes_handler(node, action_beans))
    )


def _entry_points_among(store: GraphStore, qualified_names: set[str]) -> list[GraphNode]:
    """:func:`detect_entry_points` restricted to *qualified_names*, filtered in SQL."""
    conn = store._conn
    nodes: list[GraphNode] = []
    called: set[str] = set()
    for chunk in _chunks(sorted(qualified_names)):
        marks = ",".join("?" * len(chunk))
        nodes.extend(
            store._row_to_node(row) for row in conn.execute(
                f"SELECT * FROM nodes WHERE kind IN ('Function', 'Test') "  # nosec B608
                f"AND qualified_name IN ({marks})",
                chunk,
            )
        )
        called.update(
            row[0] for row in conn.execute(
                f"SELECT DISTINCT e.target_qualified FROM edges e "  # nosec B608
                f"LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
                f"WHERE e.kind = 'CALLS' AND (n.kind IS NULL OR n.kind != 'File') "
                f"AND e.target_qualified IN ({marks})",
                chunk,
            )
        )
    stripes_candidates = any(
        node.language == "java" and (node.return_type or "").split("<", 1)[0].strip()
        .endswith("Resolution")
        for node in nodes
    )
    action_beans = _stripes_action_beans(store) if stripes_candidates else set()
    return [node for node in nodes if _is_entry_point(node, called, action_beans, False)]


_SQL_CHUNK = 450


def _chunks(values: list) -> list[list]:
    return [values[i:i + _SQL_CHUNK] for i in range(0, len(values), _SQL_CHUNK)]


# ---------------------------------------------------------------------------
# Flow tracing (BFS)
# ---------------------------------------------------------------------------


def _trace_single_flow(
    adj: FlowAdjacency,
    ep: GraphNode,
    max_depth: int = 15,
) -> Optional[dict]:
    """Trace a single execution flow from *ep* via forward BFS.

    Returns a flow dict (see :func:`trace_flows` for the schema) or ``None``
    if the flow is trivial (single-node, no outgoing CALLS that resolve).
    """
    path_ids: list[int] = [ep.id]
    path_qnames: list[str] = [ep.qualified_name]
    visited: set[str] = {ep.qualified_name}
    queue: deque[tuple[str, int]] = deque([(ep.qualified_name, 0)])

    actual_depth = 0
    nodes_by_qn = adj.nodes_by_qn
    calls_out = adj.calls_out

    while queue:
        current_qn, depth = queue.popleft()
        if depth > actual_depth:
            actual_depth = depth
        if depth >= max_depth:
            continue

        for target_qn in calls_out.get(current_qn, ()):
            if target_qn in visited:
                continue
            target_node = nodes_by_qn.get(target_qn)
            if target_node is None:
                continue
            visited.add(target_qn)
            path_ids.append(target_node.id)
            path_qnames.append(target_qn)
            queue.append((target_qn, depth + 1))

    # Skip trivial single-node flows.
    if len(path_ids) < 2:
        return None

    files = list({
        n.file_path
        for qn in path_qnames
        if (n := nodes_by_qn.get(qn)) is not None
    })

    flow: dict = {
        "name": _sanitize_name(ep.name),
        "entry_point": ep.qualified_name,
        "entry_point_id": ep.id,
        "path": path_ids,
        "depth": actual_depth,
        "node_count": len(path_ids),
        "file_count": len(files),
        "files": files,
        "criticality": 0.0,
    }
    flow["criticality"] = compute_criticality(flow, adj)
    return flow


def trace_flows(
    store: GraphStore,
    max_depth: int = 15,
    include_tests: bool = False,
) -> list[dict]:
    """Trace execution flows from every entry point via forward BFS.

    Returns a list of flow dicts, each containing:
      - name: human-readable flow name (entry point name)
      - entry_point: qualified name of the entry point
      - entry_point_id: node database id of the entry point
      - path: ordered list of node IDs in the flow
      - depth: maximum BFS depth reached
      - node_count: number of distinct nodes in the path
      - file_count: number of distinct files touched
      - files: list of distinct file paths
      - criticality: computed criticality score (0.0-1.0)
    """
    entry_points = detect_entry_points(store, include_tests=include_tests)
    if not entry_points:
        return []

    adj = store.load_flow_adjacency()
    # BFS order follows callee order; sorted, it matches an incremental re-trace.
    for callees in adj.calls_out.values():
        callees.sort()
    flows: list[dict] = []

    for ep in entry_points:
        flow = _trace_single_flow(adj, ep, max_depth)
        if flow is not None:
            flows.append(flow)

    # Sort by criticality descending.
    flows.sort(key=lambda f: f["criticality"], reverse=True)
    return flows


# ---------------------------------------------------------------------------
# Criticality scoring
# ---------------------------------------------------------------------------


def compute_criticality(flow: dict, adj: FlowAdjacency) -> float:
    """Score a flow from 0.0 to 1.0 based on multiple weighted factors.

    Weights:
      - File spread:         0.30
      - External calls:      0.20
      - Security sensitivity: 0.25
      - Test coverage gap:   0.15
      - Depth:               0.10
    """
    node_ids: list[int] = flow.get("path", [])
    if not node_ids:
        return 0.0

    nodes_by_id = adj.nodes_by_id
    nodes_by_qn = adj.nodes_by_qn
    calls_out = adj.calls_out
    has_tested_by = adj.has_tested_by

    nodes: list[GraphNode] = [
        n for nid in node_ids if (n := nodes_by_id.get(nid)) is not None
    ]
    if not nodes:
        return 0.0

    # --- File spread (0.0 - 1.0) ---
    file_count = len({n.file_path for n in nodes})
    # Normalize: 1 file => 0.0, 5+ files => 1.0
    file_spread = min((file_count - 1) / 4.0, 1.0) if file_count > 1 else 0.0

    # --- External calls (0.0 - 1.0) ---
    # Calls that target nodes NOT in the graph are considered external.
    external_count = 0
    for n in nodes:
        for target_qn in calls_out.get(n.qualified_name, ()):
            if target_qn not in nodes_by_qn:
                external_count += 1
    # Normalize: 0 => 0.0, 5+ => 1.0
    external_score = min(external_count / 5.0, 1.0)

    # --- Security sensitivity (0.0 - 1.0) ---
    security_hits = 0
    for n in nodes:
        name_lower = n.name.lower()
        qn_lower = n.qualified_name.lower()
        for kw in _SECURITY_KEYWORDS:
            if kw in name_lower or kw in qn_lower:
                security_hits += 1
                break  # Count each node at most once.
    security_score = min(security_hits / max(len(nodes), 1), 1.0)

    # --- Test coverage gap (0.0 - 1.0) ---
    tested_count = sum(1 for n in nodes if n.qualified_name in has_tested_by)
    coverage = tested_count / max(len(nodes), 1)
    test_gap = 1.0 - coverage

    # --- Depth (0.0 - 1.0) ---
    depth = flow.get("depth", 0)
    # Normalize: 0 => 0.0, 10+ => 1.0
    depth_score = min(depth / 10.0, 1.0)

    # --- Weighted sum ---
    criticality = (
        file_spread * 0.30
        + external_score * 0.20
        + security_score * 0.25
        + test_gap * 0.15
        + depth_score * 0.10
    )
    return round(min(max(criticality, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def store_flows(store: GraphStore, flows: list[dict]) -> int:
    """Clear existing flows and persist new ones.

    Returns the number of flows stored.
    """
    # NOTE: store_flows uses _conn directly because it performs
    # multi-statement batch writes (DELETE + INSERT loop) that are
    # tightly coupled to the DB transaction lifecycle.
    conn = store._conn

    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    # Wrap the full DELETE + INSERT sequence in an explicit transaction
    # so partial writes cannot occur if an exception interrupts the loop.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM flow_memberships")
        conn.execute("DELETE FROM flows")

        count = 0
        for flow in flows:
            _insert_flow(conn, flow)
            count += 1

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return count


def _insert_flow(conn, flow: dict) -> None:
    cursor = conn.execute(
        """INSERT INTO flows
           (name, entry_point_id, depth, node_count, file_count,
            criticality, path_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            flow["name"],
            flow["entry_point_id"],
            flow["depth"],
            flow["node_count"],
            flow["file_count"],
            flow["criticality"],
            json.dumps(flow.get("path", [])),
        ),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO flow_memberships (flow_id, node_id, position) "
        "VALUES (?, ?, ?)",
        [(cursor.lastrowid, node_id, position)
         for position, node_id in enumerate(flow.get("path", []))],
    )


def incremental_trace_flows(
    store: GraphStore,
    changed_files: list[str],
    max_depth: int = 15,
    *,
    delta: Optional[dict] = None,
) -> int:
    """Re-trace only the flows a graph change can have altered.

    *delta* is the net change an incremental update journaled (see
    ``incremental.mark_flows_stale``): the result then equals a full
    :func:`trace_flows`. Without it the change is approximated from the
    nodes and edges now in *changed_files*.

    A flow is re-traced when its path holds a node that changed, lost or
    gained outgoing calls or test coverage, or calls a node that appeared or
    disappeared; an entry point is re-checked when it changed or its
    incoming calls did. Returns the number of re-traced flows stored.
    """
    if delta is None:
        if not changed_files:
            return 0
        delta = _delta_from_files(store, [normalize_file_path(p) for p in changed_files])
    conn = store._conn
    nodes = set(delta.get("nodes", ()))
    touched = nodes | set(delta.get("sources", ())) | _callers_of(conn, nodes)
    status = nodes | set(delta.get("targets", ()))
    if delta.get("inherits"):
        status |= _stripes_handler_candidates(conn)
    gone = set(delta.get("deleted_ids", ()))
    touched_ids = _node_ids(conn, touched) | gone
    status_ids = _node_ids(conn, status) | gone

    affected: set[int] = set()
    for chunk in _chunks(sorted(touched_ids)):
        marks = ",".join("?" * len(chunk))
        affected.update(row[0] for row in conn.execute(
            f"SELECT DISTINCT flow_id FROM flow_memberships "  # nosec B608
            f"WHERE node_id IN ({marks})",
            chunk,
        ))
    for chunk in _chunks(sorted(status_ids)):
        marks = ",".join("?" * len(chunk))
        affected.update(row[0] for row in conn.execute(
            f"SELECT id FROM flows WHERE entry_point_id IN ({marks})",  # nosec B608
            chunk,
        ))
    candidates = touched | status
    for chunk in _chunks(sorted(affected)):
        marks = ",".join("?" * len(chunk))
        candidates.update(row[0] for row in conn.execute(
            f"SELECT n.qualified_name FROM flows f "  # nosec B608
            f"JOIN nodes n ON n.id = f.entry_point_id WHERE f.id IN ({marks})",
            chunk,
        ))

    entry_points = _entry_points_among(store, candidates) if candidates else []
    new_flows: list[dict] = []
    if entry_points:
        adj = _lazy_flow_adjacency(store, entry_points)
        for ep in entry_points:
            flow = _trace_single_flow(adj, ep, max_depth)
            if flow is not None:
                new_flows.append(flow)
    new_flows.sort(key=lambda f: f["criticality"], reverse=True)

    # One transaction: a crash never leaves orphaned memberships (#258).
    with store.transaction():
        for chunk in _chunks(sorted(affected)):
            marks = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM flow_memberships WHERE flow_id IN ({marks})",  # nosec B608
                chunk,
            )
            conn.execute(f"DELETE FROM flows WHERE id IN ({marks})", chunk)  # nosec B608
        for flow in new_flows:
            _insert_flow(conn, flow)
    return len(new_flows)


def _delta_from_files(store: GraphStore, files: list[str]) -> dict:
    """Approximate a journaled delta from the current contents of *files*."""
    conn = store._conn
    nodes: set[str] = set()
    targets: set[str] = set()
    for chunk in _chunks(sorted(set(files))):
        marks = ",".join("?" * len(chunk))
        nodes.update(row[0] for row in conn.execute(
            f"SELECT qualified_name FROM nodes WHERE file_path IN ({marks})",  # nosec B608
            chunk,
        ))
        targets.update(row[0] for row in conn.execute(
            f"SELECT target_qualified FROM edges "  # nosec B608
            f"WHERE kind = 'CALLS' AND file_path IN ({marks})",
            chunk,
        ))
    orphans = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT fm.node_id FROM flow_memberships fm "
            "LEFT JOIN nodes n ON n.id = fm.node_id WHERE n.id IS NULL"
        )
    }
    return {"nodes": nodes, "sources": nodes, "targets": targets, "deleted_ids": orphans}


def _callers_of(conn, qualified_names: set[str]) -> set[str]:
    callers: set[str] = set()
    for chunk in _chunks(sorted(qualified_names)):
        marks = ",".join("?" * len(chunk))
        callers.update(row[0] for row in conn.execute(
            f"SELECT source_qualified FROM edges "  # nosec B608
            f"WHERE target_qualified IN ({marks}) AND kind = 'CALLS'",
            chunk,
        ))
    return callers


def _node_ids(conn, qualified_names: set[str]) -> set[int]:
    ids: set[int] = set()
    for chunk in _chunks(sorted(qualified_names)):
        marks = ",".join("?" * len(chunk))
        ids.update(row[0] for row in conn.execute(
            f"SELECT id FROM nodes WHERE qualified_name IN ({marks})",  # nosec B608
            chunk,
        ))
    return ids


def _stripes_handler_candidates(conn) -> set[str]:
    """Java methods that could be Stripes handlers once inheritance changes."""
    return {
        row[0] for row in conn.execute(
            "SELECT qualified_name FROM nodes WHERE language = 'java' "
            "AND kind IN ('Function', 'Test') AND return_type LIKE '%Resolution%'"
        )
    }


class _LazyCalls(dict):
    """``calls_out`` read from the edge index on first use."""

    def __init__(self, conn) -> None:
        super().__init__()
        self._conn = conn

    def get(self, qualified_name, default=None):  # type: ignore[override]
        if not dict.__contains__(self, qualified_name):
            self[qualified_name] = [
                row[0] for row in self._conn.execute(
                    "SELECT target_qualified FROM edges "
                    "WHERE kind = 'CALLS' AND source_qualified = ? "
                    "ORDER BY target_qualified, file_path, line",
                    (qualified_name,),
                )
            ]
        return dict.get(self, qualified_name, default)


class _LazyNodes(dict):
    """``nodes_by_qn`` read from the node index on first use."""

    def __init__(self, store: GraphStore, by_id: dict[int, GraphNode]) -> None:
        super().__init__()
        self._store = store
        self._by_id = by_id
        self._missing: set[str] = set()

    def add(self, node: GraphNode) -> None:
        self[node.qualified_name] = node
        self._by_id[node.id] = node

    def _load(self, qualified_name) -> None:
        if dict.__contains__(self, qualified_name) or qualified_name in self._missing:
            return
        row = self._store._conn.execute(
            "SELECT * FROM nodes WHERE qualified_name = ?", (qualified_name,),
        ).fetchone()
        if row is None:
            self._missing.add(qualified_name)
        else:
            self.add(self._store._row_to_node(row))

    def get(self, qualified_name, default=None):  # type: ignore[override]
        self._load(qualified_name)
        return dict.get(self, qualified_name, default)

    def __contains__(self, qualified_name) -> bool:
        self._load(qualified_name)
        return dict.__contains__(self, qualified_name)


class _LazyTested(set):
    """``has_tested_by`` read from the edge index on first use."""

    def __init__(self, conn) -> None:
        super().__init__()
        self._conn = conn
        self._known: dict[str, bool] = {}

    def __contains__(self, qualified_name) -> bool:
        known = self._known.get(qualified_name)
        if known is None:
            known = self._conn.execute(
                "SELECT 1 FROM edges WHERE source_qualified = ? "
                "AND kind = 'TESTED_BY' LIMIT 1",
                (qualified_name,),
            ).fetchone() is not None
            self._known[qualified_name] = known
        return known


def _lazy_flow_adjacency(store: GraphStore, entry_points: list[GraphNode]) -> FlowAdjacency:
    """Adjacency that loads only what the re-traced flows reach."""
    by_id: dict[int, GraphNode] = {}
    nodes = _LazyNodes(store, by_id)
    for ep in entry_points:
        nodes.add(ep)
    return FlowAdjacency(
        calls_out=_LazyCalls(store._conn),
        has_tested_by=_LazyTested(store._conn),
        nodes_by_qn=nodes,
        nodes_by_id=by_id,
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_flows(
    store: GraphStore,
    sort_by: str = "criticality",
    limit: int = 50,
) -> list[dict]:
    """Retrieve stored flows from the database.

    Args:
        store: The graph store.
        sort_by: Column to sort by (``criticality``, ``depth``, ``node_count``).
        limit: Maximum number of flows to return.
    """
    allowed_sort = {"criticality", "depth", "node_count", "file_count", "name"}
    if sort_by not in allowed_sort:
        sort_by = "criticality"

    order = "DESC" if sort_by in ("criticality", "depth", "node_count", "file_count") else "ASC"

    # NOTE: get_flows reads from the flows table which is managed by
    # the flows module; _conn access is documented coupling.
    rows = store._conn.execute(
        f"SELECT * FROM flows ORDER BY {sort_by} {order} LIMIT ?",  # nosec B608
        (limit,),
    ).fetchall()

    results: list[dict] = []
    for row in rows:
        results.append({
            "id": row["id"],
            "name": _sanitize_name(row["name"]),
            "entry_point_id": row["entry_point_id"],
            "depth": row["depth"],
            "node_count": row["node_count"],
            "file_count": row["file_count"],
            "criticality": row["criticality"],
            "path": json.loads(row["path_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return results


def get_flow_by_id(store: GraphStore, flow_id: int) -> Optional[dict]:
    """Retrieve a single flow with full path details.

    Returns a dict with the flow metadata plus a ``steps`` list containing
    each node's name, kind, file, and line info.
    """
    # NOTE: get_flow_by_id reads from the flows table; see store_flows note.
    row = store._conn.execute(
        "SELECT * FROM flows WHERE id = ?", (flow_id,)
    ).fetchone()
    if row is None:
        return None

    path_ids: list[int] = json.loads(row["path_json"])

    # Build detailed step info.
    steps: list[dict] = []
    for nid in path_ids:
        node = store.get_node_by_id(nid)
        if node:
            steps.append({
                "node_id": node.id,
                "name": _sanitize_name(node.name),
                "kind": node.kind,
                "file": node.file_path,
                "line_start": node.line_start,
                "line_end": node.line_end,
                "qualified_name": _sanitize_name(node.qualified_name),
            })

    return {
        "id": row["id"],
        "name": _sanitize_name(row["name"]),
        "entry_point_id": row["entry_point_id"],
        "depth": row["depth"],
        "node_count": row["node_count"],
        "file_count": row["file_count"],
        "criticality": row["criticality"],
        "path": path_ids,
        "steps": steps,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_affected_flows(
    store: GraphStore,
    changed_files: list[str],
) -> dict:
    """Find flows that include nodes from the given changed files.

    Returns::

        {
            "affected_flows": [<flow dicts>],
            "total": <int>,
        }
    """
    if not changed_files:
        return {"affected_flows": [], "total": 0}

    # Find node IDs belonging to changed files.
    node_ids = store.get_node_ids_by_files(changed_files)

    if not node_ids:
        return {"affected_flows": [], "total": 0}

    # Find flow IDs that contain any of these nodes.
    flow_ids = store.get_flow_ids_by_node_ids(node_ids)

    if not flow_ids:
        return {"affected_flows": [], "total": 0}

    affected: list[dict] = []
    for fid in flow_ids:
        flow = get_flow_by_id(store, fid)
        if flow:
            affected.append(flow)

    # Sort by criticality descending.
    affected.sort(key=lambda f: f.get("criticality", 0), reverse=True)

    return {
        "affected_flows": affected,
        "total": len(affected),
    }
