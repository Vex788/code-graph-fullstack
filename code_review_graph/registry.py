"""Multi-repo registry and connection pool.

Manages a registry of multiple repositories at ``~/.code-review-graph/registry.json``
and provides a connection pool for concurrent access to multiple graph databases.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, TypeVar

from .constants import crg_home
from .locking import writer_lock

_T = TypeVar("_T")

# Separate from the graph writer token so holding one never admits the other.
_REGISTRY_TOKEN_ENV = "CRG_REGISTRY_LOCK_TOKEN"  # nosec B105 - env var name, not a secret
_REGISTRY_LOCK_WAIT_SECONDS = 10.0

logger = logging.getLogger(__name__)

def default_registry_path() -> Path:
    """Return the full path to ``registry.json``.

    Lives under :func:`~code_review_graph.constants.crg_home`, so ``$CRG_HOME``
    redirects it along with the rest of the per-user state.
    """
    return crg_home() / "registry.json"


class Registry:
    """Manages a JSON-based registry of code-review-graph repositories.

    Each entry stores the repo path and an optional alias.
    The registry lives at ``~/.code-review-graph/registry.json``, or under
    ``$CRG_HOME`` when that is set.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_registry_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._repos: list[dict[str, Any]] = []
        self._unparsable = False
        self._load()

    def _load(self) -> None:
        """Load registry from disk; an unparsable file reads as empty."""
        self._repos = []
        self._unparsable = False
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            logger.warning("Unparsable registry %s (%s); it is kept until backed up", self._path,
                           exc)
            self._unparsable = True
            return
        entries = data.get("repos") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            logger.warning("Registry %s has no repos list; treating it as empty", self._path)
            self._unparsable = True
            return
        self._repos = [
            dict(entry) for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        ]

    def _save(self) -> None:
        """Write registry to disk atomically; an unparsable file is backed up first."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if getattr(self, "_unparsable", False) and self._path.exists():
            backup = self._path.with_name(
                f"{self._path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            os.replace(self._path, backup)
            logger.warning("Backed up unparsable registry to %s", backup)
            self._unparsable = False
        data = {"repos": self._repos}
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=f".{self._path.name}.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mutate(self, change: Callable[[], _T]) -> _T:
        """Re-read, apply *change*, write: under the thread lock and a file lock,
        so two processes (or two instances) never drop each other's entries."""
        with self._lock, writer_lock(
            self._path, wait=_REGISTRY_LOCK_WAIT_SECONDS, token_env=_REGISTRY_TOKEN_ENV,
        ):
            self._load()
            return change()

    def register(
        self, path: str, alias: str | None = None, data_dir: str | None = None,
    ) -> dict[str, str]:
        """Register a repository path.

        Validates that the path contains a ``.git`` or ``.code-review-graph``
        directory.

        Args:
            path: Absolute or relative path to the repository root.
            alias: Optional short alias for the repository.
            data_dir: Optional external directory for graph database.

        Returns:
            The registered entry dict.

        Raises:
            ValueError: If the path is not a valid repository.
        """
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"Path is not a directory: {resolved}")
        has_repo_marker = (
            (resolved / ".git").exists()
            or (resolved / ".svn").exists()
            or (resolved / ".code-review-graph").exists()
        )
        if not has_repo_marker:
            raise ValueError(
                f"Path does not look like a repository "
                f"(no .git, .svn, or .code-review-graph): {resolved}"
            )

        def change() -> dict[str, str]:
            # Check for duplicate path
            str_path = str(resolved)
            for entry in self._repos:
                if entry["path"] == str_path:
                    # Update alias and/or data_dir if provided
                    if alias:
                        entry["alias"] = alias
                    if data_dir:
                        entry["data_dir"] = str(Path(data_dir).resolve())
                    self._save()
                    return entry

            new_entry: dict[str, str] = {"path": str_path}
            if alias:
                new_entry["alias"] = alias
            if data_dir:
                new_entry["data_dir"] = str(Path(data_dir).resolve())
            self._repos.append(new_entry)
            self._save()
            return new_entry

        return self._mutate(change)

    def unregister(self, path_or_alias: str) -> bool:
        """Remove a repository by path or alias.

        Args:
            path_or_alias: Either the absolute path or the alias.

        Returns:
            True if an entry was removed, False otherwise.
        """
        resolved = str(Path(path_or_alias).resolve())

        def change() -> bool:
            original_len = len(self._repos)
            self._repos = [
                entry for entry in self._repos
                if entry["path"] != resolved
                and entry.get("alias") != path_or_alias
            ]
            if len(self._repos) < original_len:
                self._save()
                return True
            return False

        return self._mutate(change)

    def list_repos(self) -> list[dict[str, str]]:
        """Return list of all registered repositories.

        Returns:
            List of dicts with 'path' and optional 'alias' keys.
        """
        with self._lock:
            return list(self._repos)

    def find_by_alias(self, alias: str) -> dict[str, str] | None:
        """Look up a repository by its alias.

        Args:
            alias: The alias to search for.

        Returns:
            The matching entry, or None.
        """
        with self._lock:
            for entry in self._repos:
                if entry.get("alias") == alias:
                    return dict(entry)
            return None

    def find_by_path(self, path: str) -> dict[str, str] | None:
        """Look up a repository by its path.

        Args:
            path: The path to search for.

        Returns:
            The matching entry, or None.
        """
        resolved = str(Path(path).resolve())
        with self._lock:
            for entry in self._repos:
                if entry["path"] == resolved:
                    return dict(entry)
            return None

    def set_data_dir(self, path: str, data_dir: str) -> dict[str, str]:
        """Set the external data directory for a repository.

        Args:
            path: Repository path (absolute or relative).
            data_dir: External directory path to store graph database.

        Returns:
            The updated or created registry entry.
        """
        resolved = str(Path(path).resolve())
        data_resolved = str(Path(data_dir).resolve())

        def change() -> dict[str, str]:
            # Check for existing entry
            for entry in self._repos:
                if entry["path"] == resolved:
                    entry["data_dir"] = data_resolved
                    self._save()
                    return dict(entry)

            # Create new entry if not found
            new_entry = {
                "path": resolved,
                "data_dir": data_resolved
            }
            self._repos.append(new_entry)
            self._save()
            return new_entry

        return self._mutate(change)

    def get_data_dir_for_repo(self, path: str) -> str | None:
        """Get the stored data directory for a repository.

        Args:
            path: Repository path (absolute or relative).

        Returns:
            The stored data_dir path, or None if not set.
        """
        resolved = str(Path(path).resolve())
        with self._lock:
            for entry in self._repos:
                if entry["path"] == resolved:
                    return entry.get("data_dir")
            return None


class ConnectionPool:
    """LRU connection pool for SQLite graph databases.

    Caches open connections keyed by database path, evicting the least
    recently used connection when the pool is full.
    """

    def __init__(self, max_size: int = 10) -> None:
        self._max_size = max_size
        self._pool: OrderedDict[str, sqlite3.Connection] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, db_path: str) -> sqlite3.Connection:
        """Get or create a connection for the given database path.

        Args:
            db_path: Path to the SQLite database file.

        Returns:
            An open SQLite connection.
        """
        key = str(Path(db_path).resolve())
        with self._lock:
            if key in self._pool:
                self._pool.move_to_end(key)
                return self._pool[key]

            # Evict LRU if full
            while len(self._pool) >= self._max_size:
                evict_key, evict_conn = self._pool.popitem(last=False)
                try:
                    evict_conn.close()
                except sqlite3.Error:
                    logger.debug("Failed to close evicted connection: %s", evict_key)
                logger.debug("Evicted connection: %s", evict_key)

            conn = sqlite3.connect(
                key, timeout=30, check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._pool[key] = conn
            return conn

    def close_all(self) -> None:
        """Close all connections in the pool."""
        with self._lock:
            for key, conn in self._pool.items():
                try:
                    conn.close()
                except sqlite3.Error:
                    logger.debug("Failed to close connection: %s", key)
            self._pool.clear()

    @property
    def size(self) -> int:
        """Current number of open connections."""
        with self._lock:
            return len(self._pool)


def resolve_repo(
    registry: Registry,
    repo: str | None,
    cwd: str | None = None,
) -> str | None:
    """Resolve a repo parameter to an absolute path.

    Resolution order:
    1. If repo is given, try as alias first.
    2. If repo is given and not an alias, try as a direct path.
    3. If repo is None, use cwd.

    Args:
        registry: The Registry instance.
        repo: Alias or path string, or None.
        cwd: Current working directory fallback.

    Returns:
        Resolved absolute path string, or None if unresolvable.
    """
    if repo:
        # Try alias first
        entry = registry.find_by_alias(repo)
        if entry:
            return entry["path"]

        # Try as direct path
        path = Path(repo).resolve()
        if path.is_dir():
            return str(path)

    # Fall back to CWD
    if cwd:
        return str(Path(cwd).resolve())

    return None
