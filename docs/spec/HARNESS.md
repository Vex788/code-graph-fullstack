# Harness kit

The fork ships everything a coding harness needs to use the graph, in
`code_review_graph/harness/`. The kit writes **files only**; all JSON config
(Claude `settings.json`, `~/.claude.json`, ZCode `config.json`) is written by
[laya](#ownership-with-laya), which merges the kit's config fragment.

## What the kit contains

| Path in `harness/kit/` | Lands in the target as |
|---|---|
| `hooks/crg-update.py` | PostToolUse hook: debounced background `update --skip-flows --if-locked=skip` after edits and HEAD moves. Never builds; exit 75 and 4 are logged no-ops. |
| `skills/code-search-routing` | When to use Grep, the graph, or RTK; cross-stack query patterns. |
| `skills/context-efficient-code-research` | Bounded research loop over the graph. |
| `skills/pr-context-pack` | Immutable PR context packets with graph receipt and coverage. |
| `skills/graph-bootstrap` | First build and health check for a repository. |
| `blocks/graph-routing.md`, `graph-search.md`, `impact-claim.md` | Text blocks for marker regions in hand-maintained files. |
| `data/crg_rules.json` | Tool names, statuses, exit codes and cross-stack kinds, generated from [contract.json](contract.json). |

Templates use `{{var}}` from the target's `vars` and `<!-- if:claude|zcode -->`
directives (the same engine as zcode's `render-agents`).

## Targets (`harness/targets.toml`)

| Target | Default root | Regions filled |
|---|---|---|
| `claude` | `~/.claude` | `CLAUDE.md#graph-routing` |
| `zcode` | `~/.zcode/harness` | `agent-src/blocks/{graph-search,impact-claim,routing}.md`, `AGENTS.md#graph-routing` |
| `bug-hunter` | none (pass `--root`) | `CLAUDE.md`, `agents/reviewer.md`, `skills/pr-review-orchestrator/SKILL.md` (all `#graph-routing`) |
| `laya` | regions only | `README.md#graph-search` |

Claude Code never loads a plugin-root `CLAUDE.md`, so bug-hunter also carries
the routing block in the reviewer agent and the orchestrator skill.

A region is `<!-- crg-kit:begin ID -->` ... `<!-- crg-kit:end ID -->` in a file
the harness owns. The kit rewrites only what is between the markers; a file
without markers is reported `region-missing` and never edited.

## Commands

```bash
code-review-graph harness apply --target claude [--root DIR]   # write kit files
code-review-graph harness apply --target claude --dry-run      # show, write nothing
code-review-graph harness apply --target claude --check        # exit 1 on any drift
code-review-graph harness apply --target claude --adopt        # take over edited/unowned files
code-review-graph harness apply --target claude --revert       # restore the newest backup
code-review-graph harness fragment --target claude --json      # config fragment for laya
code-review-graph harness bump-pin --tag v2.3.8-fs.6 --files ...  # rewrite version pins
```

`apply` exits 0 when clean, 3 when a region is missing (degraded), 1 on refusal
or, with `--check`, on any drift.

### Drift kinds

| Kind | Meaning | `apply` does |
|---|---|---|
| `missing` | Shipped by the kit, not on disk. | Writes it. |
| `outdated` | Ours and unedited, but the kit changed. | Updates it. |
| `modified` | Ours, but edited since. | Refuses unless `--adopt`. |
| `unowned` | Exists and was never ours. | Refuses unless `--adopt`. |
| `unexpected` | Ours, no longer shipped. | Removes it. |
| `region-missing` | Hand-maintained file without markers. | Reports only. |

## Ownership

- Kit files: `<root>/.crg-kit.lock.json` records the sha256 of every file and
  region the kit wrote. Bytes that still match are the kit's to update or remove;
  anything else is drift.
- Every changing apply first copies what it replaces to
  `<root>/.crg-kit/backup/<timestamp>/`; `--revert` restores the newest.
- Writes are atomic (temp file plus rename).

## The config fragment

`harness fragment --target T --json` prints (schema `harness_fragment` in the contract):

- `hooks`: the PostToolUse `crg-update.py` hook on `<edit matcher>|Bash`, timeout 15 s;
- `mcpServers`: `code-review-graph serve --tools agent` ([TOOLS.md](TOOLS.md) lists the preset);
- `permissions.allow`: every read-only tool under both MCP prefixes,
  `mcp__code-review-graph__` (plain server) and
  `mcp__plugin_bug-hunter_code-review-graph__` (the bug-hunter plugin's server),
  plus `Bash(code-review-graph status|contract|query|impact|search:*)`.
  Write tools are never pre-approved.

## Ownership with laya

`laya harness-apply` is the one command that syncs a harness:

1. the kit's file apply for each target (`harness apply --files-only`);
2. `render-agents` (zcode) and `render-agent-variants` (claude), which pull kit
   blocks through marker regions;
3. the config merge of laya's base, MCP and graph fragments;
4. `--check` across all of it; any drift is non-zero.

laya records config entries in `~/.laya/state/config-ledger.json` (config path,
entry digest, owner@version). It removes only entries an owner wrote before;
unknown entries belong to the user and are never touched; a changed owned entry
is drift and needs `--adopt`. Configs with comments are never rewritten: laya
prints the patch instead.

First adoption of an existing harness:

```bash
laya harness-apply --dry-run --adopt   # review what would be taken over
laya harness-apply --adopt
laya harness-apply --check             # must exit 0
```
