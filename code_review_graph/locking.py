"""Single-writer lock for a graph database.

Writers hold an OS file lock on ``<db_path>.lock`` (``graph.db.lock`` next to
the database) for the duration of one operation. The kernel drops the lock
when the holder dies, so a SIGKILLed writer never leaves a stale lock.

A child process started by the holder inherits the lock through
``CRG_WRITER_LOCK_TOKEN``: the holder writes ``pid:nonce`` into the lock file
and exports it, and the child is admitted only while that token is still the
file's content and the lock is still held. Readers never take this lock.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

TOKEN_ENV = "CRG_WRITER_LOCK_TOKEN"  # nosec B105 - env var name, not a secret

# Process exit codes shared by the CLI and hook scripts.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DEGRADED = 3
EXIT_REBUILD_REQUIRED = 4
EXIT_LOCK_BUSY = 75  # EX_TEMPFAIL: hooks treat it as "skipped, try later"

EXIT_CODES: dict[str, int] = {
    "ok": EXIT_OK,
    "error": EXIT_ERROR,
    "usage": EXIT_USAGE,
    "degraded": EXIT_DEGRADED,
    "rebuild_required": EXIT_REBUILD_REQUIRED,
    "lock_busy": EXIT_LOCK_BUSY,
}

_POLL_SECONDS = 0.05
# Windows locks a byte range; lock one far past the token so it stays readable.
_WIN_LOCK_OFFSET = 1 << 30


class LockBusyError(TimeoutError):
    """Another writer holds the lock and ``wait`` elapsed."""

    exit_code = EXIT_LOCK_BUSY

    def __init__(self, path: Path, holder_pid: Optional[int]) -> None:
        self.path = path
        self.holder_pid = holder_pid
        holder = f" (held by pid {holder_pid})" if holder_pid else ""
        super().__init__(f"graph writer lock busy: {path}{holder}")


@dataclass(frozen=True)
class LockState:
    path: str
    held: bool
    holder_pid: Optional[int] = None
    held_by_this_process: bool = False


def lock_path(db_path: str | Path) -> Path:
    return Path(f"{db_path}.lock")


# ---------------------------------------------------------------------------
# Platform primitives
# ---------------------------------------------------------------------------


def _try_lock(fd: int, shared: bool = False) -> bool:
    try:
        if sys.platform == "win32":
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(fd, flags | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("Failed to unlock graph writer lock: %s", exc)


def _read_token(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(256).decode("ascii", "replace").strip()
    except OSError:
        return ""


def _write_token(fd: int, token: str) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, token.encode("ascii"))


def _pid_of(token: str) -> Optional[int]:
    pid, sep, _nonce = token.partition(":")
    if not sep:
        return None
    try:
        return int(pid)
    except ValueError:
        return None


def _is_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        if _try_lock(fd, shared=True):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# In-process bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _Held:
    fd: int
    token: str
    depth: int
    previous_env: Optional[str]


_registry_lock = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held: dict[str, _Held] = {}


def _thread_lock(key: str) -> threading.RLock:
    with _registry_lock:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = _thread_locks[key] = threading.RLock()
        return lock


def _inherited_token(path: Path, token_env: str) -> Optional[str]:
    """Return the inherited token when it proves our parent holds the lock."""
    token = os.environ.get(token_env, "").strip()
    if not token:
        return None
    pid = _pid_of(token)
    # Our own token in os.environ must not admit sibling threads.
    if pid is None or pid == os.getpid():
        return None
    if _read_token(path) != token or not _is_held(path):
        return None
    return token


def holds_writer_lock(db_path: str | Path, token_env: str = TOKEN_ENV) -> bool:
    """True when this process holds the lock or inherited a valid token."""
    path = lock_path(db_path)
    with _registry_lock:
        if str(path.absolute()) in _held:
            return True
    return _inherited_token(path, token_env) is not None


@contextmanager
def writer_lock(
    db_path: str | Path,
    wait: float = 0.0,
    token_env: str = TOKEN_ENV,
) -> Iterator[str]:
    """Hold the writer lock for *db_path*; yields the lock token.

    Waits up to *wait* seconds, then raises :class:`LockBusyError`. The same
    thread may nest calls. A child that inherited a valid token enters
    without taking the OS lock again.
    """
    path = lock_path(db_path)
    inherited = _inherited_token(path, token_env)
    if inherited is not None:
        yield inherited
        return

    key = str(path.absolute())
    deadline = time.monotonic() + max(wait, 0.0)
    tlock = _thread_lock(key)
    acquired = tlock.acquire(timeout=wait) if wait > 0 else tlock.acquire(blocking=False)
    if not acquired:
        raise LockBusyError(path, _pid_of(_read_token(path)))
    try:
        with _registry_lock:
            held = _held.get(key)
            if held is not None:
                held.depth += 1
        if held is not None:
            try:
                yield held.token
            finally:
                with _registry_lock:
                    held.depth -= 1
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise LockBusyError(path, _pid_of(_read_token(path)))
                time.sleep(_POLL_SECONDS)
        except BaseException:
            os.close(fd)
            raise

        token = f"{os.getpid()}:{secrets.token_hex(8)}"
        try:
            _write_token(fd, token)
        except OSError:
            _unlock(fd)
            os.close(fd)
            raise
        entry = _Held(fd=fd, token=token, depth=1, previous_env=os.environ.get(token_env))
        with _registry_lock:
            _held[key] = entry
        os.environ[token_env] = token
        try:
            yield token
        finally:
            with _registry_lock:
                _held.pop(key, None)
            if entry.previous_env is None:
                os.environ.pop(token_env, None)
            else:
                os.environ[token_env] = entry.previous_env
            try:
                os.ftruncate(fd, 0)
            except OSError as exc:
                logger.warning("Failed to clear graph writer lock token: %s", exc)
            _unlock(fd)
            os.close(fd)
    finally:
        tlock.release()


def probe(db_path: str | Path) -> LockState:
    """Report whether a writer holds the lock, without taking it."""
    path = lock_path(db_path)
    with _registry_lock:
        mine = str(path.absolute()) in _held
    if mine:
        return LockState(str(path), True, os.getpid(), True)
    if not _is_held(path):
        return LockState(str(path), False)
    return LockState(str(path), True, _pid_of(_read_token(path)), False)
