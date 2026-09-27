"""Gather the observed facts that :func:`readiness.compute_readiness` maps to a status.

Read-only: the database is opened with ``mode=ro``, never migrated, and the
writer lock is only probed. Metadata keys the writer path has not started
writing yet read as their healthy defaults (no epoch keys: closed; no
``failed_files``: none), so older graphs keep their current answer.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .locking import LockState, probe
from .migrations import (
    INDEX_GENERATION,
    LATEST_VERSION,
    check_readable,
    get_index_generation,
)
from .readiness import (
    GIT_NOT_A_REPO,
    GIT_OK,
    GIT_UNAVAILABLE,
    Readiness,
    ReadinessFacts,
    compute_readiness,
)

logger = logging.getLogger(__name__)

_READ_TIMEOUT_SECONDS = 0.05

_META_KEYS = (
    "write_epoch_open",
    "write_epoch_closed",
    "failed_files",
    "resolver_failures",
    "built_at_commit",
    "git_head_sha",
    "git_branch",
    "last_updated",
)


@dataclass(frozen=True)
class ReadinessReport:
    facts: ReadinessFacts
    readiness: Readiness
    db_path: Path
    schema_version: Optional[int] = None
    metadata: dict[str, str] = field(default_factory=dict)
    drift: Optional[dict[str, Any]] = None
    lock: Optional[LockState] = None
    failed_file_paths: tuple[str, ...] = ()

    @property
    def source_identity(self) -> dict[str, Any]:
        drift = self.drift or {}
        return {
            "source_matches_build": self.facts.source_matches is True,
            "missing_indexed_paths": list(drift.get("missing", [])),
            "deleted_indexed_paths": list(drift.get("deleted", [])),
            "mismatched_indexed_paths": list(drift.get("mismatched", [])),
            "edited_indexed_count": len(drift.get("mismatched", [])),
            "check": drift.get("check", "unavailable"),
        }


def source_matches_build(drift: dict[str, Any]) -> bool:
    """No indexable file lacks a node and no indexed file is gone.

    Edited (mismatched) files do not count: their symbols are still found, so
    they are reported, not treated as drift. The hash cap (``partial``) only
    limits the edited check; ``unavailable`` cannot rule out a hidden deletion.
    """
    return drift["check"] != "unavailable" and not (drift["missing"] or drift["deleted"])


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _entries(value: Optional[str]) -> list[str]:
    """Failure entries from a metadata value: JSON list/dict/int, or a bare count."""
    if value is None or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        # Unparsable but present: something failed, we just cannot say what.
        return [value]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    if isinstance(parsed, dict):
        return [str(key) for key in parsed]
    if isinstance(parsed, bool):
        return ["unknown"] if parsed else []
    if isinstance(parsed, int):
        return ["unknown"] * max(parsed, 0)
    return [str(parsed)]


def _epochs(meta: dict[str, str]) -> tuple[int, Optional[int]]:
    opened = _int_or_none(meta.get("write_epoch_open"))
    closed = _int_or_none(meta.get("write_epoch_closed"))
    if opened is None:
        # No epoch protocol yet (or only a close stamp): nothing is open.
        value = closed if closed is not None else 0
        return value, value
    return opened, closed


_EMBEDDING_META_KEYS = ("embeddings_state", "embeddings_provider", "embeddings_stale_count")


def _embeddings(conn: sqlite3.Connection) -> tuple[bool, bool, int, int]:
    """(enabled, provider_available, embedded, embeddable); cheap when embeddings are off.

    The ``embeddings_*`` metadata the embedder writes wins over raw row counts:
    ``off`` stays off even when vectors were kept, and only the recorded
    provider's vectors count. Graphs without that metadata count every row.
    """
    try:
        marks = ",".join("?" * len(_EMBEDDING_META_KEYS))
        try:
            meta = {
                str(k): str(v) for k, v in conn.execute(
                    f"SELECT key, value FROM metadata WHERE key IN ({marks})",  # nosec B608
                    _EMBEDDING_META_KEYS,
                )
            }
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            meta = {}
        state = meta.get("embeddings_state")
        if state == "off":
            return False, True, 0, 0
        if state == "unavailable":
            return True, False, 0, 0
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'"
        ).fetchone() is not None
        if not has_table or conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone() is None:
            if state is None:
                return False, True, 0, 0
            embedded = 0
        elif provider := meta.get("embeddings_provider"):
            embedded = int(conn.execute(
                "SELECT count(*) FROM embeddings WHERE provider = ?", (provider,),
            ).fetchone()[0])
        else:
            embedded = int(conn.execute("SELECT count(*) FROM embeddings").fetchone()[0])
        embeddable = int(conn.execute(
            "SELECT count(*) FROM nodes WHERE kind != 'File'"
        ).fetchone()[0])
    except sqlite3.OperationalError:
        return False, True, 0, 0
    stale = _int_or_none(meta.get("embeddings_stale_count")) or 0
    if state == "stale":
        stale = max(stale, 1)
    if stale > 0:
        embedded = min(embedded, max(embeddable - stale, 0))
        embeddable = max(embeddable, embedded + stale)
    return True, True, embedded, embeddable


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=_READ_TIMEOUT_SECONDS)


def gather_report(
    repo_root: str | Path,
    db_path: str | Path,
    *,
    check_source: bool = True,
    git_timeout: Optional[float] = None,
) -> ReadinessReport:
    """Collect facts and compute readiness for one graph.

    Raises:
        SchemaTooNewError: the database was written by a newer build.
    """
    from .tools._common import (
        GitUnavailableError,
        read_dirty_paths,
        read_git_head_state,
        working_tree_drift_conn,
    )

    root = Path(repo_root)
    db = Path(db_path)
    if not db.is_file():
        facts = ReadinessFacts(graph_exists=False)
        return ReadinessReport(facts, compute_readiness(facts), db)

    lock = probe(db)
    building = lock.held
    git_state, head = read_git_head_state(root)

    meta: dict[str, str] = {}
    schema_version: Optional[int] = None
    generation: Optional[int] = None
    drift: Optional[dict[str, Any]] = None
    source_matches: Optional[bool] = None
    embeddings = (False, True, 0, 0)
    try:
        conn = _connect_ro(db)
    except sqlite3.Error as exc:
        logger.warning("Cannot open graph database %s read-only: %s", db, exc)
        facts = ReadinessFacts(building=True, git_state=git_state, head_commit=head)
        return ReadinessReport(facts, compute_readiness(facts), db, lock=lock)
    try:
        schema_version = check_readable(conn)
        placeholders = ", ".join("?" * len(_META_KEYS))
        try:
            meta = {
                str(k): str(v) for k, v in conn.execute(
                    f"SELECT key, value FROM metadata WHERE key IN ({placeholders})",  # nosec B608
                    _META_KEYS,
                ).fetchall()
            }
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                building = True
            else:
                logger.warning("Cannot read graph metadata from %s: %s", db, exc)
        generation = get_index_generation(conn)
        embeddings = _embeddings(conn)
        if check_source and git_state != GIT_UNAVAILABLE:
            try:
                dirty = read_dirty_paths(root, timeout=git_timeout)
            except GitUnavailableError as exc:
                if git_state == GIT_OK:
                    logger.warning("git status failed for %s: %s", root, exc)
                    git_state = GIT_UNAVAILABLE
                dirty = None
            if dirty is not None:
                drift = working_tree_drift_conn(root, conn, dirty)
                source_matches = source_matches_build(drift)
    except sqlite3.OperationalError as exc:
        logger.warning("Graph database %s busy while reading readiness: %s", db, exc)
        building = True
    finally:
        conn.close()

    failed = _entries(meta.get("failed_files"))
    opened, closed = _epochs(meta)
    facts = ReadinessFacts(
        graph_exists=True,
        building=building,
        schema_current=schema_version is None or schema_version >= LATEST_VERSION,
        index_generation=generation,
        expected_generation=INDEX_GENERATION,
        write_epoch_open=opened,
        write_epoch_closed=closed,
        failed_files=len(failed),
        resolver_failures=len(_entries(meta.get("resolver_failures"))),
        git_state=git_state,
        head_commit=head,
        built_at_commit=meta.get("built_at_commit") or meta.get("git_head_sha"),
        source_matches=source_matches,
        embeddings_enabled=embeddings[0],
        embeddings_provider_available=embeddings[1],
        embedded_nodes=embeddings[2],
        embeddable_nodes=embeddings[3],
    )
    return ReadinessReport(
        facts=facts,
        readiness=compute_readiness(facts),
        db_path=db,
        schema_version=schema_version,
        metadata=meta,
        drift=drift,
        lock=lock,
        failed_file_paths=tuple(failed[:20]),
    )


def gather_facts(repo_root: str | Path, db_path: str | Path) -> ReadinessFacts:
    """Readiness facts for the graph at *db_path* built from *repo_root*."""
    return gather_report(repo_root, db_path).facts


__all__ = [
    "GIT_NOT_A_REPO",
    "ReadinessReport",
    "gather_facts",
    "gather_report",
]
