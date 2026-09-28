"""Changes made between watch()'s startup reconciliation and the observer start.

The reconciliation has already looked, and the observer is not listening yet:
nothing would ever report such a change until the file is touched again.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

from code_review_graph import incremental
from code_review_graph.graph import GraphStore

from ._watch_sleep import watch_loop_sleep
from .test_watch_robustness import FakeObserver, _tick_driver

_real_sleep = time.sleep


def _names(store: GraphStore, path: Path) -> set[str]:
    return {node.name for node in store.get_nodes_by_file(str(path))}


def _run_watch(repo: Path, store: GraphStore, during_window, until) -> None:
    """Run watch() with *during_window* fired right after the startup reconciliation."""
    real_update = incremental.incremental_update
    calls = {"count": 0}

    def update(*args, **kwargs):
        result = real_update(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            during_window()
        return result

    def wait_for_index():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not until():
            _real_sleep(0.05)

    observer = FakeObserver()
    with (
        patch("watchdog.observers.Observer", return_value=observer),
        patch.object(incremental, "incremental_update", side_effect=update),
        patch.object(incremental, "_WATCH_HEALTH_INTERVAL", 0.0),
        watch_loop_sleep(_tick_driver(wait_for_index)),
    ):
        incremental.watch(repo, store)


def test_directory_created_during_startup_is_indexed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    store = GraphStore(tmp_path / "graph.db")
    late = tmp_path / "src" / "newpkg" / "late.py"

    def create_directory():
        late.parent.mkdir()
        late.write_text("def late_handler():\n    return 2\n", encoding="utf-8")

    try:
        _run_watch(tmp_path, store, create_directory, lambda: _names(store, late))
        assert "late_handler" in _names(store, late)
    finally:
        store.close()


def test_file_edited_during_startup_is_reindexed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    app = tmp_path / "src" / "app.py"
    app.write_text("def app():\n    return 1\n", encoding="utf-8")
    store = GraphStore(tmp_path / "graph.db")
    try:
        incremental.full_build(tmp_path, store)
        assert _names(store, app) >= {"app"}

        def edit():
            app.write_text("def app_renamed():\n    return 1\n", encoding="utf-8")

        _run_watch(tmp_path, store, edit, lambda: "app_renamed" in _names(store, app))
        assert "app_renamed" in _names(store, app)
    finally:
        store.close()


def test_owner_reads_during_a_watch_batch_do_not_corrupt_the_store(tmp_path: Path) -> None:
    """The owning thread may use its store while a watch batch runs.

    Batches are processed on the debouncer thread, and the owner keeps
    reading meanwhile — the polling in ``wait_for_index`` above is exactly
    such a read, and on CI (run 36374365209) it died with
    ``sqlite3.InterfaceError: bad parameter or other API misuse`` because
    both threads drove one connection.  So the batch must never touch the
    owner's connection; this pins that seam directly: every statement on
    the owner's connection is recorded, and none may come from another
    thread.
    """
    (tmp_path / "src").mkdir()
    app = tmp_path / "src" / "app.py"
    app.write_text("def app():\n    return 1\n", encoding="utf-8")
    store = GraphStore(tmp_path / "graph.db")
    late = tmp_path / "src" / "newpkg" / "late.py"

    class SpyConnection:
        """Forward everything, record which thread executes statements."""

        def __init__(self, conn: object) -> None:
            self._inner = conn
            self.executed_by: list[threading.Thread] = []

        def execute(self, sql: str, parameters: object = ()) -> object:
            self.executed_by.append(threading.current_thread())
            return self._inner.execute(sql, parameters)  # type: ignore[attr-defined]

        def executemany(self, sql: str, seq: object) -> object:
            self.executed_by.append(threading.current_thread())
            return self._inner.executemany(sql, seq)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    spy = SpyConnection(store._conn)
    store._conn = spy
    owner = threading.current_thread()

    def during_window():
        late.parent.mkdir()
        late.write_text("def late_handler():\n    return 2\n", encoding="utf-8")

    _run_watch(tmp_path, store, during_window, lambda: _names(store, late))

    foreign = [thread for thread in spy.executed_by if thread is not owner]
    assert not foreign, (
        f"{len(foreign)} statements ran on the owner's connection from "
        f"another thread ({foreign[0].name}); that is the "
        f"sqlite3.InterfaceError race"
    )
    assert "late_handler" in _names(store, late)
