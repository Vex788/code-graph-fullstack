"""Shared helpers for witness tests built on the fullstack Stripes fixture.

Every witness copies the committed fixture into a temporary git repository,
so no test ever writes a graph under ``tests/fixtures``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

FIXTURE_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "fullstack_stripes"
GOLDEN_TSV = FIXTURE_SRC / "expected_edges.tsv"

_GIT_IDENTITY = ["-c", "user.email=witness@example.invalid", "-c", "user.name=witness"]


def git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return stdout; raises on failure."""
    completed = subprocess.run(
        ["git", *_GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return completed.stdout


def copy_fixture(dest: Path) -> Path:
    """Copy the fixture app into *dest* as a one-commit git repository."""
    shutil.copytree(FIXTURE_SRC, dest, ignore=shutil.ignore_patterns("expected_edges.tsv"))
    git(dest, "init", "-q")
    # Fixture repos must not fire machine-level git hooks: a global
    # core.hooksPath with a post-commit graph refresh would race the builds
    # under test. A nonexistent hooks path runs no hooks.
    git(dest, "config", "core.hooksPath", str(dest / ".githooks-disabled"))
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "fixture")
    return dest.resolve()


def build(repo: Path, *, full: bool = True, postprocess: str = "full") -> dict[str, Any]:
    """Build (or update) the graph for *repo* through the public build tool."""
    from code_review_graph.tools.build import build_or_update_graph

    return build_or_update_graph(
        full_rebuild=full, repo_root=str(repo), postprocess=postprocess,
    )


def open_store(repo: Path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path

    return GraphStore(get_db_path(repo))


def rel(repo: Path, qualified: str) -> str:
    prefix = str(repo) + "/"
    return qualified[len(prefix):] if qualified.startswith(prefix) else qualified


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A fresh, unbuilt git copy of the fixture."""
    return copy_fixture(tmp_path / "app")


@pytest.fixture(scope="session")
def built_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One fully built copy shared by read-only witnesses."""
    home = tmp_path_factory.mktemp("crg-home-session")
    with pytest.MonkeyPatch.context() as patch:
        # The per-test CRG_HOME isolation in tests/conftest.py is function
        # scoped and not active yet when a session fixture runs.
        patch.setenv("CRG_HOME", str(home))
        repo = copy_fixture(tmp_path_factory.mktemp("built") / "app")
        result = build(repo)
    assert result["status"] == "ok", result
    return repo
