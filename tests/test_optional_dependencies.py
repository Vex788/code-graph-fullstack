"""Regression checks for optional dependency metadata."""

from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).parents[1]


def _project() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as pyproject:
        return tomllib.load(pyproject)["project"]


def _optional_dependencies() -> dict[str, list[str]]:
    return _project()["optional-dependencies"]


def test_google_embeddings_extra_installs_current_google_sdk():
    optional = _optional_dependencies()
    assert optional["google-embeddings"] == ["google-genai>=1.0.0,<3"]


def test_all_extra_includes_google_embeddings():
    optional = _optional_dependencies()
    name = _project()["name"]
    assert f"{name}[google-embeddings]" in optional["all"]
