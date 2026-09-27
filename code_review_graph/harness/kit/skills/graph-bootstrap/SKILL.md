---
name: graph-bootstrap
description: "Prepare a code graph for a new checkout or worktree without a multi-hour full build: seed it from another checkout's graph with clone-graph, then verify readiness. Root controller only."
---

# graph-bootstrap

Only the root controller prepares graphs. Read-only agents, reviewers and hooks never build: they return `GRAPH_PREP_REQUIRED` and wait for this step. Rules live in `{{rules_file}}`.

## When

- A new worktree of a repository that already has a graph somewhere (a develop-pinned seed checkout): seed it.
- No seed anywhere, or `rebuild_required` (the index generation changed): run one full `code-review-graph build --repo ROOT` (or `build_or_update_graph_tool` with `full_rebuild=true`). This is the only full build.
- An existing graph that is `stale_graph` or `stale_worktree`: `code-review-graph update --repo ROOT`. The PostToolUse hook does this after edits and HEAD-moving git commands, but only for a graph that already exists.

## Seed a worktree

```bash
python3 {{skills_home}}/graph-bootstrap/scripts/graph_bootstrap.py \
  --worktree NEW_WORKTREE --seed SEED_CHECKOUT
```

It runs `code-review-graph clone-graph --from SEED --to WORKTREE --json --force` (backup API copy, path re-rooting, search-index rebuild, incremental update to the worktree's HEAD), then reads `code-review-graph status --repo WORKTREE --json`:

| `readiness.status` after the clone | Result | Exit |
|---|---|---|
| `ok` | `status: ok` | 0 |
| `partial_index`, `stale_graph`, `stale_worktree` | `status: degraded`; usable, name the degradation in graph-backed claims | 2 |
| anything else, or a failed or timed-out stage | `status: failed` with the `stage` | 2 |

A seed that is stale is refreshed first with `code-review-graph update --if-locked=skip` (at most `--refresh-seed-seconds`); a seed without a graph returns `status: skip`. Embeddings stay off during the bootstrap (`CRG_EMBEDDINGS=off`). The whole run fits in 840 s (`TOTAL_BUDGET_SECONDS`), so a caller's own timeout must be longer; every stage catches its timeout and reports it as JSON instead of hanging.

## After preparation

Check `_graph.status` in the first graph response of the session (`ok`, or `partial_index` as degraded) and pass root, branch/revision, status, nodes and files to graph-dependent agents.
<!-- if:zcode -->
ZCode: the update hook also fires on ApplyPatch.
<!-- endif -->

{{block:graph-search}}
