"""registry.json: atomic writes, cross-process updates, and no silent data loss."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.registry import Registry


def _repo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    (path / ".git").mkdir(parents=True)
    return path.resolve()


def test_unparsable_registry_is_backed_up_before_any_write(tmp_path, caplog):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"repos": [ {"path": "/kept"', encoding="utf-8")
    registry = Registry(path=registry_path)
    assert registry.list_repos() == []
    assert "registry.json" in caplog.text

    registry.register(str(_repo(tmp_path, "a")))

    backups = list(tmp_path.glob("registry.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"repos": [ {"path": "/kept"'
    assert [e["path"] for e in json.loads(registry_path.read_text())["repos"]] == [
        str(tmp_path / "a")
    ]


def test_top_level_list_is_tolerated(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps([
        {"path": "/one", "alias": "one"}, "junk", {"no_path": True},
    ]), encoding="utf-8")
    registry = Registry(path=registry_path)
    assert registry.list_repos() == [{"path": "/one", "alias": "one"}]
    registry.register(str(_repo(tmp_path, "b")))
    saved = json.loads(registry_path.read_text())
    assert [e["path"] for e in saved["repos"]] == ["/one", str(tmp_path / "b")]
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_write_is_atomic(tmp_path, monkeypatch):
    import os

    registry_path = tmp_path / "registry.json"
    registry = Registry(path=registry_path)
    registry.register(str(_repo(tmp_path, "a")))
    before = registry_path.read_text()

    def crash(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        registry.register(str(_repo(tmp_path, "b")))
    assert registry_path.read_text() == before
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_two_instances_do_not_lose_updates(tmp_path):
    registry_path = tmp_path / "registry.json"
    first = Registry(path=registry_path)
    second = Registry(path=registry_path)
    first.register(str(_repo(tmp_path, "a")))
    second.register(str(_repo(tmp_path, "b")))
    paths = {e["path"] for e in json.loads(registry_path.read_text())["repos"]}
    assert paths == {str(tmp_path / "a"), str(tmp_path / "b")}


def test_concurrent_processes_keep_every_entry(tmp_path):
    registry_path = tmp_path / "registry.json"
    repos = [_repo(tmp_path, f"r{i}") for i in range(6)]
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from code_review_graph.registry import Registry\n"
        "Registry(path=Path(sys.argv[1])).register(sys.argv[2])\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(registry_path), str(repo)])
        for repo in repos
    ]
    assert [p.wait(timeout=60) for p in procs] == [0] * len(repos)
    paths = {e["path"] for e in json.loads(registry_path.read_text())["repos"]}
    assert paths == {str(r) for r in repos}
