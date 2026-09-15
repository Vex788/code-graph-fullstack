"""Tests for the resolver registry: code_review_graph/resolvers/.

Plain assert-based; runnable directly with `python3 tests/test_resolver_registry.py`
or under pytest, matching this repo's existing dual-mode test style (see
tests/test_hybrid_exact_pin.py).
"""

from __future__ import annotations

from unittest.mock import patch

import code_review_graph.incremental as incremental_module
from code_review_graph.resolvers import RESOLVERS, run_resolver


def test_every_resolver_is_callable_with_a_nonempty_language_set():
    assert RESOLVERS, "registry must not be empty"
    for name, (resolver, label, languages) in RESOLVERS.items():
        assert callable(resolver), f"{name}: resolver is not callable"
        assert isinstance(label, str) and label, f"{name}: missing log label"
        assert isinstance(languages, frozenset) and languages, (
            f"{name}: declares an empty language set"
        )
    print(f"OK: all {len(RESOLVERS)} registered resolvers are callable with a language set")


def test_registry_order_matches_the_old_hardcoded_chain():
    # This was the literal call order in incremental.py before the registry existed:
    # python_stats, rescript_stats, spring_stats, spring_event_stats,
    # temporal_stats, hcl_stats, scoped_stats.
    # jsp joined the Java-adjacent group after temporal; it has no ordering
    # dependency of its own, it reads .java sources straight off disk.
    assert list(RESOLVERS.keys()) == [
        "python",
        "rescript",
        "spring",
        "spring_event",
        "temporal",
        "jsp",
        "hcl",
        "scoped",
    ]
    print(
        "OK: registry order == python, rescript, spring, spring_event, "
        "temporal, hcl, scoped"
    )


def test_gating_reproduces_the_old_per_resolver_booleans():
    # In the old code, a single `spring_changed` boolean (any .java file
    # changed) gated three different resolvers: spring, spring_event, and
    # temporal. Reproduce that exactly, not "fix" it.
    # "jsp" joined _RECONCILE_ON_DELETE with the fullstack fork: its edges
    # are derived from live templates, so a deletion that only surfaces
    # through reconciliation must still clear them.
    assert incremental_module._RECONCILE_ON_DELETE == frozenset(
        {"python", "spring", "spring_event", "temporal", "jsp"}
    )
    assert (
        RESOLVERS["spring"][2]
        == RESOLVERS["spring_event"][2]
        == RESOLVERS["temporal"][2]
        == frozenset({"java"})
    )
    assert RESOLVERS["python"][2] == frozenset({"python"})
    assert RESOLVERS["rescript"][2] == frozenset({"rescript"})
    assert RESOLVERS["hcl"][2] == frozenset({"hcl"})
    assert RESOLVERS["scoped"][2] == frozenset({"php", "rust", "csharp"})
    # The jsp resolver binds to Java Endpoint/Class nodes and to frontend
    # asset File nodes alike, so every one of those languages re-runs it.
    assert RESOLVERS["jsp"][2] == frozenset(
        {"jsp", "java", "javascript", "html", "css", "scss"}
    )
    print("OK: gating groups match the old *_changed booleans exactly")


def test_a_resolver_that_raises_is_logged_and_returns_none():
    def boom(store):
        raise ValueError("synthetic failure")

    original = RESOLVERS["python"]
    RESOLVERS["python"] = (boom, original[1], original[2])
    try:
        with patch("code_review_graph.resolvers.logger.warning") as warn:
            result = run_resolver("python", store=None, repo_root=None)
    finally:
        RESOLVERS["python"] = original

    assert result is None
    assert warn.call_count == 1
    assert warn.call_args[0][0] == "%s failed: %s"
    assert warn.call_args[0][1] == original[1]
    print("OK: a raising resolver is caught, logged once, and returns None")


def test_resolver_declaring_repo_root_receives_it():
    seen = {}

    def wants_repo_root(store, repo_root):
        seen["store"] = store
        seen["repo_root"] = repo_root
        return {"ok": True}

    original = RESOLVERS["python"]
    RESOLVERS["python"] = (wants_repo_root, original[1], original[2])
    try:
        result = run_resolver("python", store="STORE", repo_root="REPO_ROOT")
    finally:
        RESOLVERS["python"] = original

    assert result == {"ok": True}
    assert seen == {"store": "STORE", "repo_root": "REPO_ROOT"}
    print("OK: a resolver declaring (store, repo_root) receives repo_root")


def test_resolver_not_needing_repo_root_still_works():
    def store_only(store):
        return {"store_seen": store}

    original = RESOLVERS["python"]
    RESOLVERS["python"] = (store_only, original[1], original[2])
    try:
        result = run_resolver("python", store="STORE", repo_root="REPO_ROOT")
    finally:
        RESOLVERS["python"] = original

    assert result == {"store_seen": "STORE"}
    print("OK: a resolver declaring only (store,) is called without repo_root")


def test_unchanged_language_resolver_is_not_run_on_the_incremental_path():
    # Reuses the exact gating helpers incremental_update calls: a change set
    # containing only a .tf file should activate "hcl" but not "python",
    # "spring", or "scoped".
    changed = {"infra/main.tf"}
    reconciled = incremental_module._changed_languages(changed)
    narrow = incremental_module._changed_languages(changed)

    ran = {}
    for name, (_resolver, _label, languages) in RESOLVERS.items():
        active = reconciled if name in incremental_module._RECONCILE_ON_DELETE else narrow
        ran[name] = bool(languages & active)

    assert ran["hcl"] is True
    assert ran["python"] is False
    assert ran["spring"] is False
    assert ran["spring_event"] is False
    assert ran["temporal"] is False
    assert ran["jsp"] is False
    assert ran["rescript"] is False
    assert ran["scoped"] is False
    print("OK: a resolver whose language did not change is skipped on the incremental path")


if __name__ == "__main__":
    test_every_resolver_is_callable_with_a_nonempty_language_set()
    test_registry_order_matches_the_old_hardcoded_chain()
    test_gating_reproduces_the_old_per_resolver_booleans()
    test_a_resolver_that_raises_is_logged_and_returns_none()
    test_resolver_declaring_repo_root_receives_it()
    test_resolver_not_needing_repo_root_still_works()
    test_unchanged_language_resolver_is_not_run_on_the_incremental_path()
    print("ALL PASSED")
