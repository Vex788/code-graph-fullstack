"""Shared constants for code-review-graph."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from .kinds import (
    DIRECTION_INCOMING,
    DIRECTION_NONE,
    DIRECTION_OUTGOING,
    impact_edge_directions,
    impact_edge_weights,
)


def _bounded_float_env(
    name: str,
    default: float,
    *,
    lower: float,
    upper: float,
) -> float:
    """Read a finite float strictly inside ``(lower, upper)``.

    Invalid environment configuration falls back to the documented default
    instead of making graph traversal unbounded or failing during import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or not lower < value < upper:
        return default
    return value


_logger = logging.getLogger(__name__)


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read an integer setting; a malformed or out-of-range value warns and
    falls back to *default*, so a typo never breaks an import or a tool."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < minimum:
        _logger.warning("%s=%d is below %d; using %d", name, value, minimum, default)
        return default
    return value


def env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Float counterpart of :func:`env_int`; non-finite values are rejected."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("%s=%r is not a number; using %g", name, raw, default)
        return default
    if not math.isfinite(value) or value < minimum:
        _logger.warning("%s=%r is out of range; using %g", name, raw, default)
        return default
    return value


SECURITY_KEYWORDS: frozenset[str] = frozenset({
    "auth", "login", "password", "token", "session", "crypt", "secret",
    "credential", "permission", "sql", "query", "execute", "connect",
    "socket", "request", "http", "sanitize", "validate", "encrypt",
    "decrypt", "hash", "sign", "verify", "admin", "privilege",
})

# ---------------------------------------------------------------------------
# Version-control subprocess budgets
# ---------------------------------------------------------------------------

#: Seconds allowed for one Git or SVN subprocess. Build, incremental update
#: and watch all inherit it, and they legitimately run long commands, so the
#: default stays generous.
#:
#: Read once, at import: the value has to be stable for the life of a process
#: so a long build cannot have the budget change underneath it. A test that
#: sets ``CRG_GIT_TIMEOUT`` after import will not see it; set it in the child
#: process's environment instead.
#:
#: Previously defined twice, at ``changes.py`` and ``incremental.py``. Two
#: definitions of one budget is one too many -- they cannot be told apart at
#: a call site and they drift. Both modules now alias this one.
GIT_TIMEOUT = env_int("CRG_GIT_TIMEOUT", 30, minimum=1)  # seconds

#: Seconds allowed for one subprocess in the change-discovery chain when
#: neither ``CRG_DISCOVERY_TIMEOUT`` nor ``CRG_GIT_TIMEOUT`` is set.
DISCOVERY_TIMEOUT_DEFAULT = 5.0

#: Name of the variable that sets :func:`discovery_timeout` directly.
DISCOVERY_TIMEOUT_ENV = "CRG_DISCOVERY_TIMEOUT"

#: Name of the general Git budget's variable. Read here as a *string* to tell
#: "the operator set this" from "it defaulted to 30", which :data:`GIT_TIMEOUT`
#: alone cannot express.
GIT_TIMEOUT_ENV = "CRG_GIT_TIMEOUT"


def discovery_timeout() -> float:
    """Return the per-subprocess budget for read-only change discovery.

    Discovery is what a review tool runs when the caller did **not** pass
    ``changed_files``: diff the review base, then fall back to the working
    tree, three or four Git subprocesses in series. At the 30-second
    :data:`GIT_TIMEOUT` default that chain has a two-minute worst case, which
    is how one MCP review call overruns a client's request ceiling and comes
    back as MCP error -32001 (#262). All discovery is ever answering is "what
    am I looking at?", so it gets its own, far shorter budget by default, and
    build/update/watch keep the generous one.

    A short budget is only safe because the discovery chain fails loudly:
    when a subprocess cannot answer within it,
    :func:`~code_review_graph.incremental.discover_review_changes` raises
    instead of returning an empty diff. Shortening a budget whose timeout
    returns ``[]`` would only make a wrong all-clear more likely (#913).

    Resolved on **every call**, deliberately. ``CRG_GIT_TIMEOUT`` is parsed
    once at import into :data:`GIT_TIMEOUT`, so a test or a long-lived MCP
    server that sets that variable afterwards never sees the new value.
    Whatever these variables say when a discovery call starts is what that
    call uses.

    Precedence:

    1. ``CRG_DISCOVERY_TIMEOUT``, when it parses as a number >= 0. Used as
       given, including values above :data:`GIT_TIMEOUT` -- an explicit
       override is an instruction, not a hint.
    2. Otherwise ``CRG_GIT_TIMEOUT`` when the operator set it, verbatim.
       Raising that variable is the documented answer to slow Git, and it
       predates this one; a new default must not quietly cap it.
    3. Otherwise :data:`DISCOVERY_TIMEOUT_DEFAULT`, capped at
       :data:`GIT_TIMEOUT` so an unset-but-lowered general budget still wins.

    An unparseable or negative value warns and falls back to the next rule
    rather than leaving discovery unbounded or raising inside a tool call.
    """
    explicit_git = os.environ.get(GIT_TIMEOUT_ENV)
    if explicit_git is not None and explicit_git.strip():
        fallback = float(GIT_TIMEOUT)
    else:
        fallback = min(DISCOVERY_TIMEOUT_DEFAULT, float(GIT_TIMEOUT))

    raw = os.environ.get(DISCOVERY_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return fallback
    return env_float(DISCOVERY_TIMEOUT_ENV, fallback)


# ---------------------------------------------------------------------------
# Configurable limits (override via environment variables)
# ---------------------------------------------------------------------------
MAX_IMPACT_NODES = env_int("CRG_MAX_IMPACT_NODES", 500, minimum=1)
MAX_IMPACT_DEPTH = env_int("CRG_MAX_IMPACT_DEPTH", 2)
MAX_BFS_DEPTH = env_int("CRG_MAX_BFS_DEPTH", 15)
MAX_SEARCH_RESULTS = env_int("CRG_MAX_SEARCH_RESULTS", 20, minimum=1)

# Impact traversal engine: "sql" (bounded SQLite relaxation) or "networkx".
BFS_ENGINE = os.environ.get("CRG_BFS_ENGINE", "sql")

# ---------------------------------------------------------------------------
# Impact-radius scoring
# ---------------------------------------------------------------------------
# Each hop multiplies the best score so strongly coupled nodes rank first.
# These review-risk weights intentionally differ from community-clustering
# affinity weights.
# Per-kind values live in kinds.py (the kind registry).
IMPACT_EDGE_WEIGHTS: dict[str, float] = impact_edge_weights()
IMPACT_DEFAULT_EDGE_WEIGHT = 0.5

# Stored dependency edges point from the dependent to its dependency, so impact
# normally propagates against the stored edge (target -> source). TESTED_BY is
# intentionally stored in the opposite orientation (production -> test).
# CONTAINS is not traversed: changing a file already seeds every node in it, and
# following containment can bridge into unrelated structure through stale edges.
IMPACT_DIRECTION_INCOMING = DIRECTION_INCOMING
IMPACT_DIRECTION_OUTGOING = DIRECTION_OUTGOING
IMPACT_DIRECTION_NONE = DIRECTION_NONE
IMPACT_EDGE_DIRECTIONS: dict[str, str] = impact_edge_directions()
# Unknown relationships conservatively follow the dominant graph convention:
# source depends on target. This includes possible dependents without claiming
# that a changed node's own unclassified dependency is impacted.
IMPACT_DEFAULT_EDGE_DIRECTION = IMPACT_DIRECTION_INCOMING

IMPACT_DEPTH_DECAY = _bounded_float_env(
    "CRG_IMPACT_DEPTH_DECAY", 0.6, lower=0.0, upper=1.0,
)
IMPACT_SCORE_FLOOR = _bounded_float_env(
    "CRG_IMPACT_SCORE_FLOOR", 0.05, lower=0.0, upper=1.0,
)


#: Overrides the per-user state directory that holds ``registry.json``,
#: ``watch.toml``, ``daemon.pid``, ``daemon-state.json`` and ``logs/``.
#: Follows the same convention as CRG_DATA_DIR.
CRG_HOME_ENV = "CRG_HOME"

_DEFAULT_CRG_HOME = Path.home() / ".code-review-graph"


def crg_home() -> Path:
    """Return the per-user state directory for code-review-graph.

    ``$CRG_HOME`` wins when set and non-empty; otherwise
    ``~/.code-review-graph``.

    Resolved per call rather than captured in a module-level constant. An
    import-time constant cannot be redirected afterwards, which is what let
    the test suite write into the real home directory of whoever ran it: by
    the time a fixture set the variable, the value had already been frozen.
    """
    override = os.environ.get(CRG_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_CRG_HOME
