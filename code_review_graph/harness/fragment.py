"""The graph's config fragment for one harness (``harness fragment --target T --json``).

laya merges this into the harness config; the kit never writes config itself.
The shape is ``contract.SCHEMAS["harness_fragment"]``.
"""

from __future__ import annotations

from typing import Any

from .. import __version__
from .targets import Target, get_target

OWNER = "code-review-graph"
MCP_SERVER = "code-review-graph"
# The "agent" tool preset lands with W5b; until then serve ignores the value.
MCP_ARGS = ["serve", "--tools", "agent"]
READ_ONLY_CLI = ("status", "contract", "query", "impact", "search")
UPDATE_HOOK = "crg-update.py"
UPDATE_TIMEOUT = 15


def _read_only_tools() -> list[str]:
    from ..contract import tool_specs

    return sorted(spec["name"] for spec in tool_specs() if spec["read_only"])


def fragment(target: str | Target) -> dict[str, Any]:
    """Build and validate the fragment for ``target``."""
    spec = get_target(target) if isinstance(target, str) else target
    if spec.regions_only or spec.command_root is None:
        raise ValueError(f"target {spec.name!r} takes no config fragment")
    hook_path = f"{spec.command_root}/{spec.hooks_dir}/{UPDATE_HOOK}"
    allow = [f"mcp__{MCP_SERVER}__{name}" for name in _read_only_tools()]
    allow += [f"Bash(code-review-graph {cmd}:*)" for cmd in READ_ONLY_CLI]
    doc: dict[str, Any] = {
        "owner": OWNER,
        "version": __version__,
        "target": spec.name,
        "hooks": [
            {
                "event": spec.event("post_tool_use"),
                # The hook ignores Bash unless it moves HEAD (checkout, merge, ...).
                "matcher": f"{spec.edit_matcher}|Bash",
                "command": f'python3 "{hook_path}"',
                "timeout": UPDATE_TIMEOUT,
            },
        ],
        "mcpServers": {MCP_SERVER: {"command": "code-review-graph", "args": list(MCP_ARGS)}},
        "permissions": {"allow": allow},
    }
    errors = validate_fragment(doc)
    if errors:
        raise ValueError("fragment does not match the contract: " + "; ".join(errors))
    return doc


def validate_fragment(doc: dict[str, Any]) -> list[str]:
    """Validate against the contract schema; jsonschema when installed, else key checks."""
    from ..contract import SCHEMAS

    schema = SCHEMAS["harness_fragment"]
    try:
        import jsonschema
    except ImportError:
        missing = [key for key in schema["required"] if key not in doc]
        return [f"missing {key}" for key in missing]
    validator = jsonschema.validators.validator_for(schema)(schema)
    return [error.message for error in validator.iter_errors(doc)]
