"""Tests for the ignore-policy fingerprint and the reconcile pass it gates.

A changed ignore policy is invisible to a diff: files it newly admits were
never in the graph, so they never appear in ``git diff`` and an incremental
update can never discover them. Before the fingerprint the only repair was a
full rebuild, and an index could carry a current adapter stamp while silently
missing hundreds of files.
"""

from unittest.mock import patch

import pytest

import code_review_graph.incremental as incremental_module
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _IGNORE_POLICY_METADATA_KEY,
    full_build,
    ignore_policy_fingerprint,
    incremental_update,
)
from code_review_graph.search import rebuild_fts_index

TRACKED = "code_review_graph.incremental.get_all_tracked_files"


def _repo(tmp_path, *, composer: bool):
    """A repo with one ordinary module and one under ``vendor/``."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "app.py").write_text("def app():\n    pass\n")
    vendor = tmp_path / "pkg" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "supplier.py").write_text("def supplier():\n    pass\n")
    if composer:
        (tmp_path / "composer.json").write_text("{}")
    return ["app.py", "pkg/vendor/supplier.py"]


def _vendor_nodes(store):
    return [
        path for path in store.get_all_files() if "/vendor/" in path.replace("\\", "/")
    ]


class TestFingerprintStorage:
    def test_fingerprint_is_stored_by_full_build(self, tmp_path):
        files = _repo(tmp_path, composer=False)
        store = GraphStore(tmp_path / "test.db")
        try:
            with patch(TRACKED, return_value=files):
                full_build(tmp_path, store)
            assert store.get_metadata(_IGNORE_POLICY_METADATA_KEY)
        finally:
            store.close()

    def test_fingerprint_tracks_the_policy_actually_used(self, tmp_path):
        """Two different policies must not share a fingerprint."""
        from code_review_graph.incremental import _load_ignore_patterns

        plain = ignore_policy_fingerprint(_load_ignore_patterns(tmp_path))
        (tmp_path / "composer.json").write_text("{}")
        composer = ignore_policy_fingerprint(_load_ignore_patterns(tmp_path))
        assert plain != composer


class TestShortCircuit:
    def test_unchanged_policy_never_takes_inventory(self, tmp_path):
        """The common path must not pay for a repository walk.

        Expressed as a test rather than a benchmark: ``collect_all_files`` is
        replaced by a landmine, so any inventory work on a matching fingerprint
        fails loudly instead of quietly costing ~1.6 s on every update.
        """
        files = _repo(tmp_path, composer=False)
        store = GraphStore(tmp_path / "test.db")
        try:
            with patch(TRACKED, return_value=files):
                full_build(tmp_path, store)

            def landmine(*args, **kwargs):
                raise AssertionError("inventory taken on an unchanged policy")

            with patch.object(incremental_module, "collect_all_files", landmine):
                result = incremental_update(tmp_path, store, changed_files=[])
            assert result["files_updated"] == 0
        finally:
            store.close()

    def test_missing_fingerprint_counts_as_changed(self, tmp_path):
        """Every graph built before this change has no fingerprint at all.

        If a missing key did not count as a mismatch, the reconcile would never
        fire on exactly the population it exists to repair.
        """
        files = _repo(tmp_path, composer=False)
        store = GraphStore(tmp_path / "test.db")
        try:
            with patch(TRACKED, return_value=files):
                full_build(tmp_path, store)
            store._conn.execute(
                "DELETE FROM metadata WHERE key = ?", (_IGNORE_POLICY_METADATA_KEY,)
            )
            store.commit()

            taken = []
            original = incremental_module.collect_all_files

            def spy(*args, **kwargs):
                taken.append(True)
                return original(*args, **kwargs)

            with patch.object(incremental_module, "collect_all_files", spy):
                with patch(TRACKED, return_value=files):
                    incremental_update(tmp_path, store, changed_files=[])
            assert taken, "reconcile must run when the fingerprint is absent"
            assert store.get_metadata(_IGNORE_POLICY_METADATA_KEY)
        finally:
            store.close()


class TestReconcile:
    def test_changed_policy_adds_newly_indexable_files(self, tmp_path):
        """Dropping composer.json admits vendor/ and the file must appear."""
        files = _repo(tmp_path, composer=True)
        store = GraphStore(tmp_path / "test.db")
        try:
            with patch(TRACKED, return_value=files):
                full_build(tmp_path, store)
            assert _vendor_nodes(store) == []

            (tmp_path / "composer.json").unlink()
            with patch(TRACKED, return_value=files):
                result = incremental_update(tmp_path, store, changed_files=[])

            assert _vendor_nodes(store), "vendor file never entered the graph"
            assert result["files_updated"] > 0
        finally:
            store.close()

    def test_changed_policy_removes_newly_ignored_files(self, tmp_path):
        """The mirror: adding composer.json takes vendor/ back out."""
        files = _repo(tmp_path, composer=False)
        store = GraphStore(tmp_path / "test.db")
        try:
            with patch(TRACKED, return_value=files):
                full_build(tmp_path, store)
            assert _vendor_nodes(store)

            (tmp_path / "composer.json").write_text("{}")
            with patch(TRACKED, return_value=files):
                incremental_update(tmp_path, store, changed_files=[])

            assert _vendor_nodes(store) == []
        finally:
            store.close()

    def test_reconcile_does_not_repeat_itself(self, tmp_path):
        """One policy change costs one inventory, not one per update."""
        files = _repo(tmp_path, composer=True)
        store = GraphStore(tmp_path / "test.db")
        try:
            with patch(TRACKED, return_value=files):
                full_build(tmp_path, store)
            (tmp_path / "composer.json").unlink()
            with patch(TRACKED, return_value=files):
                incremental_update(tmp_path, store, changed_files=[])

            def landmine(*args, **kwargs):
                raise AssertionError("inventory taken twice for one policy change")

            with patch.object(incremental_module, "collect_all_files", landmine):
                incremental_update(tmp_path, store, changed_files=[])
        finally:
            store.close()


class TestFtsConsistency:
    """FTS is an external-content table whose maintenance is not uniform.

    Graphs at the same ``schema_version`` exist both with and without the
    ``nodes_fts_ai/ad/au`` triggers -- the trigger-ful ones are relics of an
    older release. Row-level FTS maintenance would double-insert on one and
    under-insert on the other, so the reconcile must stay compatible with both
    and leave the rebuild to ``rebuild_fts_index``.
    """

    @pytest.mark.parametrize("with_triggers", [False, True])
    def test_reconcile_keeps_fts_consistent(self, tmp_path, with_triggers):
        files = _repo(tmp_path, composer=True)
        store = GraphStore(tmp_path / "test.db")
        try:
            if with_triggers:
                store._conn.executescript(
                    """
                    CREATE TRIGGER nodes_fts_ai AFTER INSERT ON nodes BEGIN
                      INSERT INTO nodes_fts(rowid,name,qualified_name,file_path,signature)
                      VALUES(new.id,new.name,new.qualified_name,new.file_path,new.signature);
                    END;
                    CREATE TRIGGER nodes_fts_ad AFTER DELETE ON nodes BEGIN
                      INSERT INTO nodes_fts(nodes_fts,rowid,name,qualified_name,file_path,signature)
                      VALUES('delete',old.id,old.name,old.qualified_name,old.file_path,old.signature);
                    END;
                    """
                )
            with patch(TRACKED, return_value=files):
                full_build(tmp_path, store)

            (tmp_path / "composer.json").unlink()
            with patch(TRACKED, return_value=files):
                incremental_update(tmp_path, store, changed_files=[])

            rebuild_fts_index(store)
            nodes = store._conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
            fts = store._conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0]
            dangling = store._conn.execute(
                "SELECT count(*) FROM nodes_fts f "
                "LEFT JOIN nodes n ON n.rowid = f.rowid WHERE n.rowid IS NULL"
            ).fetchone()[0]
            assert fts == nodes
            assert dangling == 0

            hit = store._conn.execute(
                "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'supplier'"
            ).fetchone()[0]
            assert hit, "a reconciled symbol must be findable by search"
        finally:
            store.close()
