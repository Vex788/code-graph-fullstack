"""Dependency compatibility: fastmcp Message import and uv.lock/pyproject drift."""

import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PINNED = ("fastmcp", "tree-sitter-language-pack", "sentence-transformers")


def test_message_shim_imports_under_installed_fastmcp():
    from code_review_graph import prompts

    msg = prompts.Message(role="user", content="x")
    assert msg.role == "user"
    assert prompts.review_changes_prompt()[0].role == "user"


def _pyproject_specifiers() -> dict[str, SpecifierSet]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    found: dict[str, SpecifierSet] = {}
    for raw in reqs:
        req = Requirement(raw)
        if req.name in PINNED:
            found[req.name] = req.specifier
    return found


def _lock_specifiers(project_name: str) -> dict[str, SpecifierSet]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    pkg = next(p for p in lock["package"] if p["name"] == project_name)
    return {
        dep["name"]: SpecifierSet(dep.get("specifier", ""))
        for dep in pkg["metadata"]["requires-dist"]
        if dep["name"] in PINNED
    }


@pytest.mark.parametrize("name", PINNED)
def test_uv_lock_specifiers_match_pyproject(name):
    project_name = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["name"]
    expected = _pyproject_specifiers()
    locked = _lock_specifiers(project_name)
    assert name in expected, f"{name} missing from pyproject.toml"
    assert name in locked, f"{name} missing from uv.lock metadata"
    assert locked[name] == expected[name], (
        f"uv.lock records {name}{locked[name]} but pyproject.toml has "
        f"{name}{expected[name]}; run `uv lock`"
    )
