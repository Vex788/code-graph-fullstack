"""Registry of post-build cross-file resolvers.

Each resolver adds cross-file edges the per-file parser cannot see on its own
(Python imports, Spring DI, Spring events, Temporal, HCL module references,
scoped calls). Each resolver runs in one write transaction: a failure rolls
its delete-and-reinsert back to the previous edges, is logged, and is
recorded for readiness instead of failing the build that runs it.

Adding a resolver means adding one entry to [RESOLVERS] — no other file needs
to change.
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable, Optional

from ..event_resolver import resolve_spring_events
from ..graph import GraphStore, is_locked_error
from ..hcl_resolver import resolve_hcl_module_references
from ..python_resolver import resolve_python_imports
from ..rescript_resolver import resolve_rescript_cross_module
from ..scoped_resolver import resolve_scoped_calls
from ..spring_resolver import resolve_spring_di_calls
from ..temporal_resolver import resolve_temporal_calls
from .hibernate import resolve_hibernate_mappings
from .jsp import resolve_jsp_links
from .stripes import resolve_stripes_actions

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
    # Stripes endpoints must exist before the JSP linker binds page links to
    # routes, and Hibernate tables before entity pages are traversed; both
    # sit ahead of "jsp" in every build.
    "stripes": (
        resolve_stripes_actions,
        "Stripes action resolver",
        # FORWARDS_TO binds to jsp File nodes, so page deletions reconcile it.
        frozenset({"java", "jsp"}),
    ),
    "hibernate": (
        resolve_hibernate_mappings,
        "Hibernate mapping resolver",
        frozenset({"java", "xml"}),
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


def run_resolver(
    name: str,
    store: GraphStore,
    repo_root,
    failures: Optional[dict[str, str]] = None,
) -> Optional[dict]:
    """Run the named resolver atomically; a failure never fails the build.

    Returns the resolver's stats dict, or None if it raised. A raising
    resolver's writes are rolled back and, when *failures* is given, its
    error is recorded there under *name*.
    """
    resolver, label, _languages = RESOLVERS[name]
    transaction = getattr(store, "transaction", None)
    try:
        if transaction is None:
            return _call(resolver, store, repo_root)
        with transaction():
            return _call(resolver, store, repo_root)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        if is_locked_error(exc):
            raise
        logger.warning("%s failed: %s", label, exc)
        if failures is not None:
            failures[name] = f"{type(exc).__name__}: {exc}"[:500]
        return None


def _call(resolver: Resolver, store: GraphStore, repo_root) -> Optional[dict]:
    if _accepts_repo_root(resolver):
        return resolver(store, repo_root)
    return resolver(store)
