"""Changes made between watch()'s startup reconciliation and the observer start.

The reconciliation has already looked, and the observer is not listening yet:
nothing would ever report such a change until the file is touched again.
"""

from __future__ import annotations

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
