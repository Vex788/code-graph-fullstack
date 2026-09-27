"""Docs that restate the contract must stay in step with the code."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from code_review_graph.contract import EXIT_CODES, build_contract
from code_review_graph.readiness import EmbeddingsStatus, ReadinessStatus

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "spec"


def _gen_module():
    path = ROOT / "scripts" / "gen_tool_reference.py"
    spec = importlib.util.spec_from_file_location("gen_tool_reference", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract_doc() -> dict:
    return build_contract()


@pytest.mark.parametrize(("name", "render"), [
    ("TOOLS.md", "render_tools_md"),
    ("CLI.md", "render_cli_md"),
])
def test_generated_reference_is_current(name: str, render: str, contract_doc: dict):
    committed = (SPEC / name).read_text(encoding="utf-8")
    assert committed == getattr(_gen_module(), render)(contract_doc), (
        f"docs/spec/{name} is stale; run python scripts/gen_tool_reference.py"
    )


def test_tools_md_lists_every_tool_and_param(contract_doc: dict):
    text = (SPEC / "TOOLS.md").read_text(encoding="utf-8")
    for tool in contract_doc["tools"]:
        assert f"## {tool['name']}" in text
        for param in tool["params"]:
            assert f"`{param['name']}`" in text


def test_every_env_var_in_code_is_documented():
    names: set[str] = set()
    for path in (ROOT / "code_review_graph").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".toml", ".json"}:
            names.update(re.findall(r"CRG_[A-Z0-9_]+", path.read_text(encoding="utf-8")))
    config = (SPEC / "CONFIG.md").read_text(encoding="utf-8")
    missing = sorted(n for n in names if n not in config)
    assert not missing, f"add to docs/spec/CONFIG.md: {missing}"


def test_readiness_page_covers_statuses_and_exit_codes():
    text = (SPEC / "READINESS.md").read_text(encoding="utf-8")
    for status in list(ReadinessStatus) + list(EmbeddingsStatus):
        assert f"`{status.value}`" in text, status.value
    for name, code in EXIT_CODES.items():
        assert f"| {code} | `{name}` |" in text, name


def test_claude_md_tool_count_matches_contract(contract_doc: dict):
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    count = len(contract_doc["tools"])
    assert f"registers {count} tools" in text
    assert f"{count} MCP tool implementations" in text
