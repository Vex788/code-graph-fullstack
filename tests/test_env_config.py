"""Bad CRG_* integers warn and fall back; they never crash an import or a tool."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


def _import_value(expr: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"import code_review_graph.constants as c; print({expr})"],
        capture_output=True, text=True, env={**os.environ, **env}, timeout=60,
    )


@pytest.mark.parametrize("value", ["abc", "", "1.5", "-3"])
def test_bad_limit_falls_back_to_default_with_warning(value):
    completed = _import_value("c.MAX_IMPACT_NODES", CRG_MAX_IMPACT_NODES=value)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "500"
    if value:
        assert "CRG_MAX_IMPACT_NODES" in completed.stderr


def test_valid_limit_is_used():
    completed = _import_value("c.MAX_BFS_DEPTH", CRG_MAX_BFS_DEPTH="7")
    assert completed.stdout.strip() == "7"


def test_env_int_helper(monkeypatch, caplog):
    from code_review_graph.constants import env_float, env_int

    monkeypatch.setenv("CRG_X", "12")
    assert env_int("CRG_X", 3) == 12
    monkeypatch.setenv("CRG_X", "nope")
    assert env_int("CRG_X", 3) == 3
    monkeypatch.setenv("CRG_X", "0")
    assert env_int("CRG_X", 3, minimum=1) == 3
    monkeypatch.delenv("CRG_X")
    assert env_int("CRG_X", 3) == 3
    monkeypatch.setenv("CRG_X", "2.5")
    assert env_float("CRG_X", 1.0) == 2.5
    monkeypatch.setenv("CRG_X", "inf")
    assert env_float("CRG_X", 1.0) == 1.0
    assert "CRG_X" in caplog.text


@pytest.mark.asyncio
async def test_bad_tool_timeout_does_not_break_detect_changes(monkeypatch):
    from code_review_graph import main as crg_main

    monkeypatch.setenv("CRG_TOOL_TIMEOUT", "soon")
    monkeypatch.setattr(crg_main, "detect_changes_func", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(crg_main, "with_provenance", lambda result, root=None: result)
    tool = getattr(crg_main.detect_changes_tool, "fn", crg_main.detect_changes_tool)
    assert await tool() == {"status": "ok"}
