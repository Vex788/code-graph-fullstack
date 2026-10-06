"""Seed a checkout's graph from another checkout's graph.

A worktree shares nearly every file with the checkout it branched from, so
copying that graph and re-rooting its paths is far cheaper than a full build.
Raw ``UPDATE`` rewrites of a copied ``graph.db`` are unsupported: they miss
columns and desync ``nodes_fts``. This module is the supported path:

1. SQLite backup API from the seed in one step. That is a consistent snapshot
   under a read transaction, so the seed's writer lock is not needed.
2. Under the target's writer lock, the copy is a private ``graph.db.cloning``
   file: one transaction rewrites the registered path columns
   (:mod:`path_columns`), rebuilds the FTS index from ``nodes`` and closes the
   write epoch, then ``os.replace`` publishes it. A failed or killed clone
   never leaves a half-rewritten ``graph.db``.
3. Optionally an incremental ``update`` from the seed's build commit.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .graph import GraphStore
from .incremental import get_db_path
from .locking import lock_path, writer_lock
from .migrations import drop_fts_triggers
from .path_columns import rewrite_root
from .search import rebuild_fts

logger = logging.getLogger(__name__)


def resolve_seed(source: str | Path, seed_root: Optional[str | Path] = None) -> tuple[Path, Path]:
    """Return ``(seed_root, seed_db)`` for a repo root or a ``graph.db`` path."""
    path = Path(source).expanduser().resolve()
    if path.is_dir():
        root = path
        db = get_db_path(root, read_only=True)
    elif path.is_file():
        db = path
        if seed_root is None and path.parent.name != ".code-review-graph":
            raise ValueError(
                f"cannot infer the seed repository root for {path}; pass --seed-root",
            )
        root = path.parent.parent
    else:
        raise FileNotFoundError(f"seed not found: {path}")
    if seed_root is not None:
        root = Path(seed_root).expanduser().resolve()
    if not db.is_file():
        raise FileNotFoundError(f"no graph database at {db}; build the seed first")
    return root, db


def _backup(seed_db: Path, target_db: Path) -> None:
    source = sqlite3.connect(f"{seed_db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        target_db.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(str(target_db), timeout=30)
        try:
            # The copy is private until os.replace: no rollback journal, no fsyncs.
            destination.execute("PRAGMA journal_mode=OFF")
            destination.execute("PRAGMA synchronous=OFF")
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _discard_sidecars(db: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)


def _discard(db: Path) -> None:
    db.unlink(missing_ok=True)
    _discard_sidecars(db)
    lock_path(db).unlink(missing_ok=True)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reroot(db: Path, old_root: str, new_root: str) -> tuple[dict[str, int], int]:
    """Re-root the unpublished copy at *db*; returns ``(rows_changed, fts_rows)``.

    Nobody else can see the file yet, so it runs without a rollback journal or
    fsyncs; a failure leaves it unusable and the caller discards it.
    """
    # Opening migrates an older seed.
    store = GraphStore(db)
    try:
        conn = store._conn
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-1048576")  # 1 GiB; 64 MiB thrashes the big indexes
        conn.execute("BEGIN IMMEDIATE")
        drop_fts_triggers(conn)
        changed = rewrite_root(conn, old_root, new_root)
        fts_rows = rebuild_fts(conn)
        _close_epoch(conn)
        _set_meta(conn, "cloned_from", old_root)
        _set_meta(conn, "cloned_at", datetime.now().isoformat(timespec="seconds"))
        conn.execute("COMMIT")
        conn.execute("PRAGMA journal_mode=WAL")
        return changed, fts_rows
    finally:
        store.close()


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value),
    )


def _close_epoch(conn: sqlite3.Connection) -> None:
    rows = dict(conn.execute(
        "SELECT key, value FROM metadata WHERE key IN ('write_epoch_open', 'write_epoch_closed')"
    ).fetchall())
    epochs = []
    for key in ("write_epoch_open", "write_epoch_closed"):
        try:
            epochs.append(int(rows.get(key, 0)))
        except ValueError:
            epochs.append(0)
    epoch = str(max(epochs))
    _set_meta(conn, "write_epoch_open", epoch)
    _set_meta(conn, "write_epoch_closed", epoch)


def clone_graph(
    source: str | Path,
    target_root: str | Path,
    *,
    seed_root: Optional[str | Path] = None,
    force: bool = False,
    update: bool = True,
    lock_wait: float = 120.0,
) -> dict[str, Any]:
    """Copy the seed graph into *target_root* and re-root its paths.

    ``built_at_commit`` stays the seed's so the follow-up ``update`` diffs from
    it. Raises ``FileExistsError`` when the target already has a graph and
    *force* is false, and :class:`locking.LockBusyError` when another writer
    holds the target lock past *lock_wait*.
    """
    old_root, seed_db = resolve_seed(source, seed_root)
    new_root = Path(target_root).expanduser().resolve()
    if not new_root.is_dir():
        raise NotADirectoryError(f"target is not a directory: {new_root}")
    target_db = get_db_path(new_root)
    if target_db.resolve() == seed_db.resolve():
        raise ValueError("seed and target resolve to the same graph database")

    with writer_lock(target_db, wait=lock_wait):
        if target_db.exists() and not force:
            raise FileExistsError(f"{target_db} exists; pass --force to replace it")
        building = target_db.with_name(target_db.name + ".cloning")
        _discard(building)
        try:
            _backup(seed_db, building)
            changed, fts_rows = _reroot(building, str(old_root), str(new_root))
            _fsync(building)
            # A stale WAL must never pair with the new file.
            _discard_sidecars(target_db)
            os.replace(building, target_db)
        except BaseException:
            _discard(building)
            raise
        _discard(building)
        logger.info(
            "Cloned graph %s -> %s (%d FTS rows)", seed_db, target_db, fts_rows,
        )

        update_result: Optional[dict[str, Any]] = None
        if update:
            from .tools.build import build_or_update_graph

            update_result = build_or_update_graph(full_rebuild=False, repo_root=str(new_root))

    return {
        "status": "ok",
        "seed_root": str(old_root),
        "seed_db": str(seed_db),
        "target_root": str(new_root),
        "target_db": str(target_db),
        "rows_rewritten": changed,
        "fts_rows": fts_rows,
        "temp_replaced": True,
        "update": update_result,
    }
