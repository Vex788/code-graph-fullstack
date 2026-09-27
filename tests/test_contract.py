"""The committed contract matches the code and its schemas are usable."""

import asyncio
import json
from pathlib import Path

import pytest

from code_review_graph import contract as contract_module
from code_review_graph.contract import (
    SCHEMAS,
    WRITE_TOOLS,
    build_contract,
    render_contract_json,
)
from code_review_graph.locking import EXIT_CODES
from code_review_graph.migrations import LATEST_VERSION
from code_review_graph.readiness import ReadinessStatus

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "docs" / "spec" / "contract.json"

EXAMPLES = {
    "error": {"status": "error", "error_code": "lock_busy", "message": "busy"},
    "graph_receipt": {
        "updated_at": "2026-09-27T10:00:00", "age_seconds": 5,
        "built_at_sha": "abc", "head_sha": "abc", "head_matches_build": True,
        "status": "ok", "embeddings": "off", "reasons": [],
        "source_identity": {"runtime_matches_source": True, "index_matches_runtime": True},
    },
    "status_json": {
        "nodes": 3, "edges": 2, "files": 1, "languages": ["java"],
        "last_updated": None, "vcs": "git", "built_on_branch": None,
        "built_at_commit": None, "current_branch": "develop", "current_sha": "abc",
        "svn_branch": None, "svn_revision": None,
        "readiness": {"status": "stale_graph", "embeddings": "off", "reasons": ["head_moved"]},
    },
    "harness_fragment": {
        "owner": "code-review-graph", "version": "2.3.8+fs.6", "target": "claude",
        "hooks": [{"event": "PostToolUse", "matcher": "Edit|Write",
                   "command": "crg-update --if-locked=skip", "timeout": 30}],
        "mcpServers": {"code-review-graph": {
            "command": "code-review-graph", "args": ["serve", "--tools", "agent"]}},
        "permissions": {"allow": ["mcp__code-review-graph__query_graph_tool"]},
    },
}

BAD_EXAMPLES = {
    "error": {"status": "failed", "message": "x"},
    "graph_receipt": {"status": "fine"},
    "status_json": {"nodes": 1, "files": 1},
    "harness_fragment": {"owner": "x", "version": "1", "target": "vim",
                         "hooks": [], "mcpServers": {}, "permissions": {"allow": []}},
}


def test_committed_contract_is_current():
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render_contract_json(), (
        "docs/spec/contract.json is stale; run "
        "python -m code_review_graph.contract --json > docs/spec/contract.json"
    )


def test_contract_core_fields():
    c = build_contract()
    assert c["contract_version"] == "1.0.0-rc1"
    assert c["compat_epoch"] == 1
    assert c["schema_version"] == LATEST_VERSION
    assert c["reader_compat"] <= c["schema_version"]
    assert c["statuses"] == [s.value for s in ReadinessStatus]
    assert c["exit_codes"] == EXIT_CODES
    assert {k["name"] for k in c["kinds"]["edge_kinds"]} >= {"CALLS", "RENDERS"}


def test_every_registered_tool_present():
    from code_review_graph import main

    registered = {t.name for t in asyncio.run(main.mcp.list_tools())}
    listed = {t["name"] for t in build_contract()["tools"]}
    assert listed == registered
    assert WRITE_TOOLS <= registered


def test_read_only_flags():
    tools = {t["name"]: t for t in build_contract()["tools"]}
    for name, tool in tools.items():
        assert tool["read_only"] is (name not in WRITE_TOOLS), name
    params = {p["name"]: p for p in tools["query_graph_tool"]["params"]}
    assert params["pattern"]["required"] is True
    assert params["repo_root"]["type"] == "string|null"


def test_param_type_rendering():
    render = contract_module._param_type
    assert render({"anyOf": [{"type": "integer"}, {"type": "null"}]}) == "integer|null"
    assert render({"type": "array", "items": {"type": "string"}}) == "array<string>"
    assert render({}) == "any"


def test_contract_is_json_serialisable_and_sorted_stably():
    first = render_contract_json()
    assert json.loads(first)["tools"] == sorted(
        json.loads(first)["tools"], key=lambda t: t["name"],
    )
    assert render_contract_json() == first


def test_main_cli(capsys):
    assert contract_module.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["contract_version"] == "1.0.0-rc1"
    assert contract_module.main([]) == EXIT_CODES["usage"]


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_valid_and_examples(name):
    schema = SCHEMAS[name]
    jsonschema = pytest.importorskip("jsonschema")
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    validator.validate(EXAMPLES[name])
    assert not validator.is_valid(BAD_EXAMPLES[name])


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_structure_without_jsonschema(name):
    schema = SCHEMAS[name]
    assert schema["type"] == "object"
    assert set(schema.get("required", [])) <= set(schema["properties"])
    assert set(schema.get("required", [])) <= set(EXAMPLES[name])
