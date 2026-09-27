"""The contract ``_graph`` receipt and get_minimal_context readiness mapping."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from code_review_graph.contract import CONTRACT_VERSION, SCHEMAS
from code_review_graph.graph import GraphStore, NodeInfo
from code_review_graph.locking import writer_lock
from code_review_graph.tools import _common as common_module

_IDENTITY = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "app.py"
    source.write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        for kind, name in (("File", "app.py"), ("Function", "handle")):
            store.upsert_node(NodeInfo(
                kind=kind, name=name, file_path=str(source), line_start=1, line_end=2,
                language="python",
            ), file_hash=digest)
        store.set_metadata("git_head_sha", _git(root, "rev-parse", "HEAD"))
        store.set_metadata("git_branch", "main")
        store.set_metadata("indexed_dirty_paths", "[]")
        store.set_metadata("last_updated", "2026-01-01T00:00:00")
        store.commit()
    finally:
        store.close()
    return root.resolve()


def _db(root: Path) -> Path:
    return root / ".code-review-graph" / "graph.db"


def test_receipt_carries_contract_fields_and_keeps_legacy_ones(repo):
    receipt = common_module.graph_receipt(str(repo))
    head = _git(repo, "rev-parse", "HEAD")
    assert receipt["status"] == "ok"
    assert receipt["contract_version"] == CONTRACT_VERSION
    assert receipt["built_at_commit"] == head
    assert receipt["current_sha"] == head
    assert receipt["head_matches_build"] is True
    assert (receipt["failed_files"], receipt["resolver_failures"]) == (0, 0)
    assert receipt["embeddings"] == "off"
    assert receipt["index_generation"] is not None
    assert len(receipt["etag"]) == 16
    assert receipt["source_identity"] == {
        "source_matches_build": True,
        "index_matches_runtime": True,
        "runtime_matches_source": True,
    }
    # Legacy provenance fields stay.
    assert receipt["updated_at"] == "2026-01-01T00:00:00"
    assert receipt["built_at_sha"] == head
    assert receipt["built_on_branch"] == "main"


def test_receipt_validates_against_contract_schema(repo):
    jsonschema = pytest.importorskip("jsonschema")
    schema = SCHEMAS["graph_receipt"]
    jsonschema.validators.validator_for(schema)(schema).validate(
        common_module.graph_receipt(str(repo)),
    )


def test_receipt_etag_changes_with_the_graph(repo):
    before = common_module.graph_receipt(str(repo))["etag"]
    store = GraphStore(_db(repo))
    store.set_metadata("last_updated", "2026-02-02T00:00:00")
    store.commit()
    store.close()
    assert common_module.graph_receipt(str(repo))["etag"] != before


def test_receipt_git_timeout_is_stale_not_ok(repo, monkeypatch):
    real_run = subprocess.run

    def slow_status(cmd, *args, **kwargs):
        if list(cmd[:2]) == ["git", "status"]:
            raise subprocess.TimeoutExpired(cmd, 5)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow_status)
    receipt = common_module.graph_receipt(str(repo))
    assert receipt["status"] == "stale_graph"
    assert "git_unavailable" in receipt["reasons"]
    assert receipt["source_identity"]["source_matches_build"] is False


def test_receipt_reports_building_while_a_writer_holds_the_lock(repo):
    with writer_lock(_db(repo)):
        assert common_module.graph_receipt(str(repo))["status"] == "building"


def test_receipt_for_schema_too_new_is_error_shaped(repo):
    conn = sqlite3.connect(_db(repo))
    conn.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    conn.execute("DELETE FROM metadata WHERE key = 'reader_compat'")
    conn.commit()
    conn.close()
    receipt = common_module.graph_receipt(str(repo))
    assert receipt["status"] == "error"
    assert receipt["error_code"] == "schema_too_new"


def test_with_provenance_attaches_the_receipt(repo):
    result = common_module.with_provenance({"status": "ok"}, str(repo))
    assert result["_graph"]["status"] == "ok"
    assert result["_graph"]["contract_version"] == CONTRACT_VERSION


def test_minimal_context_ok_carries_readiness(repo):
    from code_review_graph.tools.context import get_minimal_context

    result = get_minimal_context(repo_root=str(repo))
    assert result["status"] == "ok"
    assert result["readiness"]["status"] == "ok"


def test_minimal_context_git_failure_is_not_ready(repo, monkeypatch):
    from code_review_graph.tools.context import get_minimal_context

    real_run = subprocess.run

    def broken_git(cmd, *args, **kwargs):
        if list(cmd[:1]) == ["git"]:
            raise subprocess.TimeoutExpired(cmd, 1)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", broken_git)
    result = get_minimal_context(repo_root=str(repo))
    assert result["status"] == "not_ready"
    assert result["reason"] == "git_unavailable"


def test_minimal_context_building_while_locked(repo):
    from code_review_graph.tools.context import get_minimal_context

    with writer_lock(_db(repo)):
        result = get_minimal_context(repo_root=str(repo))
    assert result["status"] == "building"
    assert result["readiness"]["status"] == "building"


def test_minimal_context_schema_too_new_is_error(repo):
    from code_review_graph.tools.context import get_minimal_context

    conn = sqlite3.connect(_db(repo))
    conn.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    conn.execute("DELETE FROM metadata WHERE key = 'reader_compat'")
    conn.commit()
    conn.close()
    result = get_minimal_context(repo_root=str(repo))
    assert (result["status"], result["error_code"]) == ("error", "schema_too_new")


def test_minimal_context_generation_mismatch_requires_rebuild(repo):
    from code_review_graph.tools.context import get_minimal_context

    store = GraphStore(_db(repo))
    store.set_metadata("index_generation", "999")
    store.commit()
    store.close()
    result = get_minimal_context(repo_root=str(repo))
    assert (result["status"], result["reason"]) == ("not_ready", "rebuild_required")


def test_minimal_context_partial_index_still_answers(repo):
    from code_review_graph.tools.context import get_minimal_context

    store = GraphStore(_db(repo))
    store.set_metadata("failed_files", '["broken.py"]')
    store.commit()
    store.close()
    result = get_minimal_context(repo_root=str(repo))
    assert result["status"] == "ok"
    assert result["readiness"]["status"] == "partial_index"
    assert result["failed_files"] == 1
