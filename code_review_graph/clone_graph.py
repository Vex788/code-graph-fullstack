"""Seed a checkout's graph from another checkout's graph.

A worktree shares nearly every file with the checkout it branched from, so
copying that graph and re-rooting its paths is far cheaper than a full build.
Raw ``UPDATE`` rewrites of a copied ``graph.db`` are unsupported: they miss
columns and desync ``nodes_fts``. This module is the supported path:

1. SQLite backup API from the seed in one step. That is a consistent snapshot
   under a read transaction, so the seed's writer lock is not needed.
2. Under the target's writer lock, one transaction rewrites the registered
   path columns (:mod:`path_columns`), rebuilds the FTS index from ``nodes``
   and closes the write epoch.
3. Optionally an incremental ``update`` from the seed's build commit.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .graph import GraphStore
from .incremental import get_db_path
from .locking import writer_lock
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
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


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
        _backup(seed_db, target_db)
        # Opening migrates an older seed; the held lock makes that re-entrant.
        store = GraphStore(target_db)
        try:
            conn = store._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                drop_fts_triggers(conn)
                changed = rewrite_root(conn, str(old_root), str(new_root))
                fts_rows = rebuild_fts(conn)
                _close_epoch(conn)
                _set_meta(conn, "cloned_from", str(old_root))
                _set_meta(conn, "cloned_at", datetime.now().isoformat(timespec="seconds"))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            store.close()
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
        "update": update_result,
    }
