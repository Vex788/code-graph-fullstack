"""Registry of post-build cross-file resolvers.

Each resolver adds cross-file edges the per-file parser cannot see on its own
(Python imports, Spring DI, Spring events, Temporal, HCL module references,
scoped calls). Every resolver is best-effort: a failure is logged as a
warning and swallowed so it never fails the build that runs it.

Adding a resolver means adding one entry to [RESOLVERS] — no other file needs
to change.
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable, Optional

from ..event_resolver import resolve_spring_events
from ..graph import GraphStore
from ..hcl_resolver import resolve_hcl_module_references
from ..python_resolver import resolve_python_imports
from ..rescript_resolver import resolve_rescript_cross_module
from ..scoped_resolver import resolve_scoped_calls
from ..spring_resolver import resolve_spring_di_calls
from ..temporal_resolver import resolve_temporal_calls
from .jsp import resolve_jsp_links

logger = logging.getLogger(__name__)

Resolver = Callable[..., Optional[dict]]

# name -> (resolver callable, "<label> failed: %s" log label, the languages
# whose change should re-run it). Order matters: this is the order every
# resolver ran in before this registry existed, and full builds still run
# them in this order.
RESOLVERS: dict[str, tuple[Resolver, str, frozenset[str]]] = {
    "python": (
        resolve_python_imports,
        "Python import resolver",
        frozenset({"python"}),
    ),
    "rescript": (
        resolve_rescript_cross_module,
        "ReScript cross-module resolver",
        frozenset({"rescript"}),
    ),
    "spring": (
        resolve_spring_di_calls,
        "Spring DI resolver",
        frozenset({"java"}),
    ),
    "spring_event": (
        resolve_spring_events,
        "Spring event resolver",
        frozenset({"java"}),
    ),
    "temporal": (
        resolve_temporal_calls,
        "Temporal resolver",
        frozenset({"java"}),
    ),
    "jsp": (
        resolve_jsp_links,
        "JSP link resolver",
        # Edges bind to Java nodes (Endpoint/Class) and to frontend asset
        # File nodes (js/css/scss/jsp/html), so any of those changing can
        # invalidate this resolver's derived edges.
        frozenset({"jsp", "java", "javascript", "html", "css", "scss"}),
    ),
    "hcl": (
        resolve_hcl_module_references,
        "Terraform/HCL resolver",
        frozenset({"hcl"}),
    ),
    "scoped": (
        resolve_scoped_calls,
        "Scoped call resolver",
        frozenset({"php", "rust", "csharp"}),
    ),
}


def _accepts_repo_root(resolver: Resolver) -> bool:
    """True if [resolver] declares a second positional parameter.

    Most resolvers in [RESOLVERS] take only ``(store)``. A resolver that
    needs source access (e.g. ``jsp``) can declare ``(store, repo_root)`` and
    this adapts at the call boundary instead of every resolver module having
    to accept a parameter it does not use.
    """
    try:
        params = list(inspect.signature(resolver).parameters.values())
    except (TypeError, ValueError):
        return False
    return len(params) >= 2


def run_resolver(name: str, store: GraphStore, repo_root) -> Optional[dict]:
    """Run the named resolver, swallowing any failure so it never fails the build.

    Returns the resolver's stats dict, or None if it raised.
    """
    resolver, label, _languages = RESOLVERS[name]
    try:
        if _accepts_repo_root(resolver):
            return resolver(store, repo_root)
        return resolver(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("%s failed: %s", label, exc)
        return None
