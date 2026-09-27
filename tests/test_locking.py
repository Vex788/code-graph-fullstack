"""Writer lock: contention, timeout, token re-entrancy, crash release."""

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from code_review_graph.locking import (
    EXIT_CODES,
    EXIT_LOCK_BUSY,
    TOKEN_ENV,
    LockBusyError,
    holds_writer_lock,
    lock_path,
    probe,
    writer_lock,
)

HOLDER = textwrap.dedent(
    """
    import sys, time
    from code_review_graph.locking import writer_lock
    with writer_lock(sys.argv[1], wait=0):
        print("locked", flush=True)
        time.sleep(float(sys.argv[2]))
    """
)

TRY_ACQUIRE = textwrap.dedent(
    """
    import sys
    from code_review_graph.locking import EXIT_LOCK_BUSY, LockBusyError, writer_lock
    try:
        with writer_lock(sys.argv[1], wait=0):
            pass
    except LockBusyError:
        sys.exit(EXIT_LOCK_BUSY)
    """
)


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
    env.update(extra)
    return env


def _start_holder(db, seconds):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(db), str(seconds)],
        stdout=subprocess.PIPE, text=True, env=_env(),
    )
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_exit_codes():
    assert EXIT_CODES == {
        "ok": 0, "error": 1, "usage": 2, "degraded": 3,
        "rebuild_required": 4, "lock_busy": 75,
    }


def test_lock_file_next_to_db(tmp_path):
    db = tmp_path / "graph.db"
    with writer_lock(db):
        assert (tmp_path / "graph.db.lock").exists()
    assert lock_path(db) == tmp_path / "graph.db.lock"


def test_multiprocess_contention_and_wait(tmp_path):
    db = tmp_path / "graph.db"
    holder = _start_holder(db, 1.0)
    try:
        state = probe(db)
        assert state.held and state.holder_pid == holder.pid
        assert not state.held_by_this_process
        with pytest.raises(LockBusyError) as exc:
            with writer_lock(db, wait=0):
                pass
        assert exc.value.exit_code == EXIT_LOCK_BUSY
        assert exc.value.holder_pid == holder.pid
        # Waiting long enough outlasts the holder.
        with writer_lock(db, wait=10):
            assert probe(db).held_by_this_process
    finally:
        holder.wait(timeout=10)


def test_timeout_is_bounded(tmp_path):
    db = tmp_path / "graph.db"
    holder = _start_holder(db, 5.0)
    try:
        start = time.monotonic()
        with pytest.raises(LockBusyError):
            with writer_lock(db, wait=0.3):
                pass
        elapsed = time.monotonic() - start
        assert 0.25 <= elapsed < 3.0
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_child_with_token_reenters_without_deadlock(tmp_path):
    db = tmp_path / "graph.db"
    with writer_lock(db, wait=0) as token:
        assert os.environ[TOKEN_ENV] == token
        # The child inherits the token through the environment.
        child = subprocess.run(
            [sys.executable, "-c", TRY_ACQUIRE, str(db)],
            env={**os.environ}, timeout=30,
        )
        assert child.returncode == 0
        # Without the token the child is just another writer.
        stranger = subprocess.run(
            [sys.executable, "-c", TRY_ACQUIRE, str(db)], env=_env(), timeout=30,
        )
        assert stranger.returncode == EXIT_LOCK_BUSY
        # A forged token fails validation.
        forged = subprocess.run(
            [sys.executable, "-c", TRY_ACQUIRE, str(db)],
            env=_env(**{TOKEN_ENV: "1:deadbeef"}), timeout=30,
        )
        assert forged.returncode == EXIT_LOCK_BUSY
    assert TOKEN_ENV not in os.environ
    assert not probe(db).held


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX-only")
def test_lock_is_free_after_holder_sigkill(tmp_path):
    db = tmp_path / "graph.db"
    holder = _start_holder(db, 60)
    stale_token = lock_path(db).read_text()
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=10)
    assert not probe(db).held
    with writer_lock(db, wait=0):
        # The dead holder's token no longer admits anyone.
        stale = subprocess.run(
            [sys.executable, "-c", TRY_ACQUIRE, str(db)],
            env=_env(**{TOKEN_ENV: stale_token}), timeout=30,
        )
        assert stale.returncode == EXIT_LOCK_BUSY


def test_same_thread_nests_other_thread_waits(tmp_path):
    db = tmp_path / "graph.db"
    results = []

    def other():
        try:
            with writer_lock(db, wait=0):
                results.append("acquired")
        except LockBusyError:
            results.append("busy")

    with writer_lock(db) as outer:
        with writer_lock(db) as inner:
            assert inner == outer
            assert holds_writer_lock(db)
        # The token in os.environ belongs to this pid; threads still exclude.
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert probe(db).held
    assert results == ["busy"]
    assert not holds_writer_lock(db)
    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert results == ["busy", "acquired"]


def test_probe_without_lock_file(tmp_path):
    state = probe(tmp_path / "graph.db")
    assert not state.held
    assert state.holder_pid is None
    assert not (tmp_path / "graph.db.lock").exists()
