"""Readiness precedence and the ``ok`` invariant."""

import itertools

import pytest

from code_review_graph.readiness import (
    GIT_NOT_A_REPO,
    GIT_OK,
    GIT_UNAVAILABLE,
    STATUS_PRECEDENCE,
    EmbeddingsStatus,
    ReadinessFacts,
    ReadinessStatus,
    compute_readiness,
)

HEALTHY = ReadinessFacts(
    graph_exists=True,
    index_generation=1,
    expected_generation=1,
    write_epoch_open=3,
    write_epoch_closed=3,
    head_commit="abc",
    built_at_commit="abc",
    source_matches=True,
)


def _with(**changes):
    return ReadinessFacts(**{**HEALTHY.__dict__, **changes})


def test_precedence_order():
    assert [s.value for s in STATUS_PRECEDENCE] == [
        "missing_graph", "building", "rebuild_required", "partial_index",
        "stale_graph", "stale_worktree", "ok",
    ]


@pytest.mark.parametrize(
    "changes, status, reason",
    [
        ({}, ReadinessStatus.OK, None),
        ({"graph_exists": False}, ReadinessStatus.MISSING_GRAPH, "missing_graph"),
        ({"building": True}, ReadinessStatus.BUILDING, "build_in_progress"),
        ({"schema_current": False}, ReadinessStatus.REBUILD_REQUIRED,
         "schema_migration_pending"),
        ({"index_generation": 0}, ReadinessStatus.REBUILD_REQUIRED,
         "index_generation_mismatch"),
        ({"index_generation": None}, ReadinessStatus.REBUILD_REQUIRED,
         "index_generation_mismatch"),
        ({"write_epoch_open": 4}, ReadinessStatus.PARTIAL_INDEX, "write_epoch_open"),
        ({"write_epoch_closed": None}, ReadinessStatus.PARTIAL_INDEX, "write_epoch_open"),
        ({"failed_files": 2}, ReadinessStatus.PARTIAL_INDEX, "failed_files"),
        ({"resolver_failures": 1}, ReadinessStatus.PARTIAL_INDEX, "resolver_failures"),
        ({"git_state": GIT_UNAVAILABLE}, ReadinessStatus.STALE_GRAPH, "git_unavailable"),
        ({"git_capture_failed": True}, ReadinessStatus.STALE_GRAPH, "git_capture_failed"),
        ({"git_capture_failed": True, "head_commit": "def"}, ReadinessStatus.STALE_GRAPH,
         "git_capture_failed"),
        ({"head_commit": "def"}, ReadinessStatus.STALE_GRAPH, "head_moved"),
        ({"built_at_commit": None}, ReadinessStatus.STALE_GRAPH, "head_moved"),
        ({"source_matches": False}, ReadinessStatus.STALE_WORKTREE, "worktree_changed"),
        ({"source_matches": None}, ReadinessStatus.STALE_WORKTREE, "worktree_changed"),
        ({"git_state": GIT_NOT_A_REPO, "head_commit": None, "built_at_commit": None},
         ReadinessStatus.OK, None),
    ],
)
def test_single_fact_table(changes, status, reason):
    result = compute_readiness(_with(**changes))
    assert result.status is status
    if reason is None:
        assert result.reasons == ()
    else:
        assert reason in result.reasons


def test_building_outranks_partial_and_stale():
    result = compute_readiness(
        _with(building=True, write_epoch_open=9, head_commit="zzz", source_matches=False)
    )
    assert result.status is ReadinessStatus.BUILDING
    assert {"build_in_progress", "write_epoch_open", "head_moved", "worktree_changed"} <= set(
        result.reasons
    )


def test_git_failure_is_never_clean():
    result = compute_readiness(_with(git_state=GIT_UNAVAILABLE, head_commit=None))
    assert result.status is not ReadinessStatus.OK


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, EmbeddingsStatus.OFF),
        ({"embeddings_enabled": True, "embeddings_provider_available": False},
         EmbeddingsStatus.UNAVAILABLE),
        ({"embeddings_enabled": True, "embedded_nodes": 1, "embeddable_nodes": 5},
         EmbeddingsStatus.STALE),
        ({"embeddings_enabled": True, "embedded_nodes": 5, "embeddable_nodes": 5},
         EmbeddingsStatus.READY),
    ],
)
def test_embeddings_substate_independent(changes, expected):
    result = compute_readiness(_with(**changes))
    assert result.embeddings is expected
    assert result.status is ReadinessStatus.OK


def test_to_dict_shape():
    assert compute_readiness(HEALTHY).to_dict() == {
        "status": "ok", "embeddings": "off", "reasons": [],
    }


# Small domains: the exhaustive product stands in for a property test.
DOMAINS = {
    "graph_exists": [True, False],
    "building": [False, True],
    "schema_current": [True, False],
    "index_generation": [1, 0, None],
    "write_epoch_open": [None, 1, 2],
    "write_epoch_closed": [None, 1, 2],
    "failed_files": [0, 1],
    "resolver_failures": [0, 1],
    "git_state": [GIT_OK, GIT_UNAVAILABLE, GIT_NOT_A_REPO],
    "head_commit": ["a", "b", None],
    "built_at_commit": ["a", None],
    "source_matches": [True, False, None],
}


def _expected_ok(f: ReadinessFacts) -> bool:
    epoch_closed = (
        f.write_epoch_open is not None
        and f.write_epoch_closed is not None
        and f.write_epoch_closed >= f.write_epoch_open
    )
    head = f.git_state == GIT_NOT_A_REPO or (
        f.git_state == GIT_OK and f.head_commit is not None
        and f.head_commit == f.built_at_commit
    )
    return (
        f.graph_exists and not f.building and f.schema_current and epoch_closed
        and f.failed_files == 0 and f.resolver_failures == 0
        and f.index_generation == f.expected_generation and head
        and f.source_matches is True
    )


def test_ok_invariant_exhaustive():
    keys = list(DOMAINS)
    seen = set()
    for values in itertools.product(*(DOMAINS[k] for k in keys)):
        facts = ReadinessFacts(expected_generation=1, **dict(zip(keys, values)))
        result = compute_readiness(facts)
        seen.add(result.status)
        assert result.ok == _expected_ok(facts), facts
        if not facts.graph_exists:
            assert result.status is ReadinessStatus.MISSING_GRAPH
        elif facts.building:
            assert result.status is ReadinessStatus.BUILDING
        elif result.ok:
            assert result.reasons == ()
        else:
            assert result.reasons
    assert seen == set(ReadinessStatus)
