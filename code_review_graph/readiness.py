"""Pure readiness computation for a graph database.

``compute_readiness`` maps observed facts to one status. Callers gather the
facts (metadata rows, git state, lock probe); this module does no I/O, so the
precedence and the ``ok`` invariant are testable exhaustively.

``ok`` holds exactly when the write epoch is closed, nothing failed, the
index generation is current, HEAD matches the build and the working tree
matches the build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ReadinessStatus(str, Enum):
    MISSING_GRAPH = "missing_graph"
    BUILDING = "building"
    REBUILD_REQUIRED = "rebuild_required"
    PARTIAL_INDEX = "partial_index"
    STALE_GRAPH = "stale_graph"
    STALE_WORKTREE = "stale_worktree"
    OK = "ok"


# Most severe first; the first matching status wins.
STATUS_PRECEDENCE: tuple[ReadinessStatus, ...] = tuple(ReadinessStatus)


class EmbeddingsStatus(str, Enum):
    OFF = "off"
    READY = "ready"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


GIT_OK = "ok"
GIT_UNAVAILABLE = "unavailable"
GIT_NOT_A_REPO = "not_a_repo"


@dataclass(frozen=True)
class ReadinessFacts:
    graph_exists: bool = True
    building: bool = False
    schema_current: bool = True
    index_generation: Optional[int] = None
    expected_generation: int = 1
    write_epoch_open: Optional[int] = None
    write_epoch_closed: Optional[int] = None
    failed_files: int = 0
    resolver_failures: int = 0
    # GIT_OK, GIT_UNAVAILABLE (git failed; never read as clean) or GIT_NOT_A_REPO.
    git_state: str = GIT_OK
    head_commit: Optional[str] = None
    built_at_commit: Optional[str] = None
    # None: not checked, so it cannot prove a match.
    source_matches: Optional[bool] = None
    embeddings_enabled: bool = False
    embeddings_provider_available: bool = True
    embedded_nodes: int = 0
    embeddable_nodes: int = 0


@dataclass(frozen=True)
class Readiness:
    status: ReadinessStatus
    embeddings: EmbeddingsStatus
    # Every failed condition, not only the one that set the status.
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status is ReadinessStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "embeddings": self.embeddings.value,
            "reasons": list(self.reasons),
        }


def epoch_closed(facts: ReadinessFacts) -> bool:
    return (
        facts.write_epoch_open is not None
        and facts.write_epoch_closed is not None
        and facts.write_epoch_closed >= facts.write_epoch_open
    )


def head_matches(facts: ReadinessFacts) -> bool:
    if facts.git_state == GIT_NOT_A_REPO:
        return True
    return (
        facts.git_state == GIT_OK
        and facts.head_commit is not None
        and facts.head_commit == facts.built_at_commit
    )


def generation_current(facts: ReadinessFacts) -> bool:
    return facts.index_generation == facts.expected_generation


def _embeddings_status(facts: ReadinessFacts) -> EmbeddingsStatus:
    if not facts.embeddings_enabled:
        return EmbeddingsStatus.OFF
    if not facts.embeddings_provider_available:
        return EmbeddingsStatus.UNAVAILABLE
    if facts.embedded_nodes < facts.embeddable_nodes:
        return EmbeddingsStatus.STALE
    return EmbeddingsStatus.READY


def compute_readiness(facts: ReadinessFacts) -> Readiness:
    embeddings = _embeddings_status(facts)
    if not facts.graph_exists:
        return Readiness(ReadinessStatus.MISSING_GRAPH, embeddings, ("missing_graph",))

    checks: list[tuple[ReadinessStatus, str, bool]] = [
        (ReadinessStatus.BUILDING, "build_in_progress", facts.building),
        (ReadinessStatus.REBUILD_REQUIRED, "schema_migration_pending",
         not facts.schema_current),
        (ReadinessStatus.REBUILD_REQUIRED, "index_generation_mismatch",
         not generation_current(facts)),
        (ReadinessStatus.PARTIAL_INDEX, "write_epoch_open", not epoch_closed(facts)),
        (ReadinessStatus.PARTIAL_INDEX, "failed_files", facts.failed_files > 0),
        (ReadinessStatus.PARTIAL_INDEX, "resolver_failures", facts.resolver_failures > 0),
        (ReadinessStatus.STALE_GRAPH, "git_unavailable", facts.git_state == GIT_UNAVAILABLE),
        (ReadinessStatus.STALE_GRAPH, "head_moved",
         facts.git_state == GIT_OK and not head_matches(facts)),
        (ReadinessStatus.STALE_WORKTREE, "worktree_changed", facts.source_matches is not True),
    ]
    failed = [(status, reason) for status, reason, bad in checks if bad]
    if not failed:
        return Readiness(ReadinessStatus.OK, embeddings, ())
    rank = {status: i for i, status in enumerate(STATUS_PRECEDENCE)}
    status = min((s for s, _ in failed), key=rank.__getitem__)
    return Readiness(status, embeddings, tuple(reason for _, reason in failed))
