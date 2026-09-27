"""Machine-readable contract between the graph and its consumers.

Consumers (harnesses, hooks, the VS Code extension) depend on this document,
never on ``graph.db`` SQL or private modules. Minor versions only add
fields; removing one takes a deprecation release and a ``compat_epoch`` bump.

``python -m code_review_graph.contract --json`` prints it; the committed copy
is ``docs/spec/contract.json`` and a test keeps the two identical.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
from typing import Any

from . import __version__
from .kinds import kinds_as_dict
from .locking import EXIT_CODES
from .migrations import INDEX_GENERATION, LATEST_VERSION, MIN_WRITER_VERSION, READER_COMPAT
from .readiness import STATUS_PRECEDENCE, EmbeddingsStatus

CONTRACT_VERSION = "1.0.0-rc1"
COMPAT_EPOCH = 1

# Every other registered tool only reads.
WRITE_TOOLS = frozenset({
    "build_or_update_graph_tool",
    "run_postprocess_tool",
    "embed_graph_tool",
    "apply_refactor_tool",
    "generate_wiki_tool",
})

_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

STATUS_VALUES = [s.value for s in STATUS_PRECEDENCE]
EMBEDDINGS_VALUES = [s.value for s in EmbeddingsStatus]

ERROR_SCHEMA: dict[str, Any] = {
    "$schema": _SCHEMA_DIALECT,
    "title": "Error response",
    "type": "object",
    "required": ["status", "error_code", "message"],
    "properties": {
        "status": {"const": "error"},
        "error_code": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "message": {"type": "string"},
    },
}

READINESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "embeddings", "reasons"],
    "properties": {
        "status": {"enum": STATUS_VALUES},
        "embeddings": {"enum": EMBEDDINGS_VALUES},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
}

_SOURCE_IDENTITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "runtime_matches_source": {"type": "boolean"},
        "index_matches_runtime": {"type": "boolean"},
        "source_matches_build": {"type": "boolean"},
        "missing_indexed_paths": {"type": "array", "items": {"type": "string"}},
        "deleted_indexed_paths": {"type": "array", "items": {"type": "string"}},
        "mismatched_indexed_paths": {"type": "array", "items": {"type": "string"}},
        # Edited indexed files: reported, never part of source_matches_build.
        "edited_indexed_count": {"type": "integer", "minimum": 0},
        "check": {"enum": ["full", "partial", "unavailable"]},
    },
}

_NULLABLE_STRING = {"type": ["string", "null"]}

RECEIPT_SCHEMA: dict[str, Any] = {
    "$schema": _SCHEMA_DIALECT,
    "title": "_graph receipt attached to tool responses",
    "type": "object",
    "properties": {
        "updated_at": {"type": "string"},
        "age_seconds": {"type": "integer", "minimum": 0},
        "built_at_sha": {"type": "string"},
        "built_on_branch": {"type": "string"},
        "head_sha": {"type": "string"},
        "head_matches_build": {"type": "boolean"},
        "missing_build_anchor": {"type": "boolean"},
        # "error" only with error_code (e.g. schema_too_new).
        "status": {"enum": [*STATUS_VALUES, "error"]},
        "error_code": {"type": "string"},
        "message": {"type": "string"},
        "embeddings": {"enum": EMBEDDINGS_VALUES},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "contract_version": {"type": "string"},
        "schema_version": {"type": ["integer", "null"]},
        "index_generation": {"type": ["integer", "null"]},
        "built_at_commit": _NULLABLE_STRING,
        "current_sha": _NULLABLE_STRING,
        "failed_files": {"type": "integer", "minimum": 0},
        "resolver_failures": {"type": "integer", "minimum": 0},
        "etag": {"type": "string"},
        "source_identity": _SOURCE_IDENTITY_SCHEMA,
    },
}

STATUS_JSON_SCHEMA: dict[str, Any] = {
    "$schema": _SCHEMA_DIALECT,
    "title": "code-review-graph status --json",
    "type": "object",
    # Existing consumers read these keys; they never go away.
    "required": ["nodes", "files", "last_updated", "built_at_commit"],
    "properties": {
        "nodes": {"type": "integer", "minimum": 0},
        "edges": {"type": "integer", "minimum": 0},
        "files": {"type": "integer", "minimum": 0},
        "languages": {"type": "array", "items": {"type": "string"}},
        "last_updated": _NULLABLE_STRING,
        "vcs": _NULLABLE_STRING,
        "built_on_branch": _NULLABLE_STRING,
        "built_at_commit": _NULLABLE_STRING,
        "current_branch": _NULLABLE_STRING,
        "current_sha": _NULLABLE_STRING,
        "svn_branch": _NULLABLE_STRING,
        "svn_revision": _NULLABLE_STRING,
        "readiness": READINESS_SCHEMA,
        "repo_root": {"type": "string"},
        "schema_version": {"type": ["integer", "null"]},
        "index_generation": {"type": ["integer", "null"]},
        "contract_version": {"type": "string"},
        "failed_files": {"type": "integer", "minimum": 0},
        "resolver_failures": {"type": "integer", "minimum": 0},
        "source_identity": _SOURCE_IDENTITY_SCHEMA,
    },
}

HARNESS_FRAGMENT_SCHEMA: dict[str, Any] = {
    "$schema": _SCHEMA_DIALECT,
    "title": "code-review-graph harness fragment --json",
    "type": "object",
    "required": ["owner", "version", "target", "hooks", "mcpServers", "permissions"],
    "properties": {
        "owner": {"type": "string"},
        "version": {"type": "string"},
        "target": {"enum": ["claude", "zcode", "bug-hunter"]},
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["event", "command"],
                "properties": {
                    "event": {"type": "string"},
                    "matcher": {"type": "string"},
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "minimum": 0},
                    "rewrites_input": {"type": "boolean"},
                },
            },
        },
        "mcpServers": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "env": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        },
        "permissions": {
            "type": "object",
            "required": ["allow"],
            "properties": {"allow": {"type": "array", "items": {"type": "string"}}},
        },
        "statusLine": {
            "type": "object",
            "required": ["command"],
            "properties": {"type": {"type": "string"}, "command": {"type": "string"}},
        },
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "error": ERROR_SCHEMA,
    "graph_receipt": RECEIPT_SCHEMA,
    "status_json": STATUS_JSON_SCHEMA,
    "harness_fragment": HARNESS_FRAGMENT_SCHEMA,
}


def _param_type(schema: dict[str, Any]) -> str:
    if "anyOf" in schema:
        parts: list[str] = []
        for option in schema["anyOf"]:
            part = _param_type(option)
            if part not in parts:
                parts.append(part)
        return "|".join(parts)
    kind = schema.get("type", "any")
    if isinstance(kind, list):
        return "|".join(str(k) for k in kind)
    if kind == "array" and isinstance(schema.get("items"), dict):
        return f"array<{_param_type(schema['items'])}>"
    return str(kind)


def _list_tools() -> list[Any]:
    from . import main  # heavy: imports every tool module

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return list(asyncio.run(main.mcp.list_tools()))
    # Called from inside an event loop (e.g. a tool): list on a worker thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return list(pool.submit(asyncio.run, main.mcp.list_tools()).result())


def tool_specs() -> list[dict[str, Any]]:
    specs = []
    for tool in sorted(_list_tools(), key=lambda t: t.name):
        schema = tool.parameters or {}
        required = set(schema.get("required", []))
        params = []
        for name, prop in (schema.get("properties") or {}).items():
            param: dict[str, Any] = {
                "name": name,
                "type": _param_type(prop),
                "required": name in required,
            }
            if "default" in prop:
                param["default"] = prop["default"]
            params.append(param)
        specs.append({
            "name": tool.name,
            "read_only": tool.name not in WRITE_TOOLS,
            "params": params,
        })
    return specs


def cli_command_specs() -> list[dict[str, Any]]:
    from .cli import cli_commands

    return cli_commands()


def build_contract() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "compat_epoch": COMPAT_EPOCH,
        "fork_version": __version__,
        "schema_version": LATEST_VERSION,
        "reader_compat": READER_COMPAT,
        "min_writer_version": MIN_WRITER_VERSION,
        "index_generation": INDEX_GENERATION,
        "statuses": STATUS_VALUES,
        "embeddings_statuses": EMBEDDINGS_VALUES,
        "exit_codes": dict(EXIT_CODES),
        "kinds": kinds_as_dict(),
        "tools": tool_specs(),
        "cli_commands": cli_command_specs(),
        "schemas": SCHEMAS,
    }


def render_contract_json() -> str:
    return json.dumps(build_contract(), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["--json"]:
        print("usage: python -m code_review_graph.contract --json", file=sys.stderr)
        return EXIT_CODES["usage"]
    sys.stdout.write(render_contract_json())
    return EXIT_CODES["ok"]


if __name__ == "__main__":
    sys.exit(main())
