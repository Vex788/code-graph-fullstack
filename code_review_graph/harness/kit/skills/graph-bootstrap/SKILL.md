---
name: graph-bootstrap
description: "Heal or prepare a code graph for a checkout or worktree without a multi-hour full build: crg-heal for a blocking status, clone-graph seeding from another checkout's graph, then verify readiness. Any agent may run crg-heal."
---

# graph-bootstrap

Any agent heals a blocking graph with one `crg-heal` call; nobody runs a raw build, update or clone-graph by hand. Hooks never build. Rules live in `{{rules_file}}`.

## crg-heal

```bash
crg-heal --repo ROOT --json [--budget 240] [--no-clone]
```

`crg-heal` is a shim for `{{skills_home}}/graph-bootstrap/scripts/crg_heal.py`. Run it once when `_graph.status` is `missing_graph`, `building`, `rebuild_required` or `error`, then continue. It prints `{repo_root, before, after, action, usable, claim_scope, healed, seconds, fingerprint, receipt, gaps, next}`; cite `gaps` and `claim_scope` in graph-backed claims. Exit 0 ready, 3 usable but degraded, 4 not usable (graph rows `UNVERIFIED`, nothing else), 75 busy past the budget.

| `before` | Action |
|---|---|
| `ok`, `partial_index` | none (`partial_index` reports its gaps) |
| `stale_graph` | one `code-review-graph update --skip-flows --if-locked=wait --lock-wait 60`, at most 180 s |
| `stale_worktree` | one update per fingerprint (HEAD + `git status --porcelain` + `git diff HEAD`, the `crg-reconcile` state), then continue with the gaps |
| `building` | poll every 5 s for at most 120 s; never starts a build |
| `missing_graph`, `rebuild_required` on a PMS worktree | `clone-graph` from the validated seed (`--force` only for `rebuild_required`), one clone machine-wide |
| `rebuild_required` on the seed or `sp_api_library` | none, exit 4, `next: nightly crg-postprocess-all` |
| `schema_too_new`, `error` | none, exit 4 |

Scope is an allowlist: the PMS checkout, `sp_api_library`, their worktrees and the seed checkout; any other path exits 4 with `out_of_scope`. The seed is `~/IdeaProjects/.crg-seed-pms` when it exists, else the PMS checkout, and must read `ok` (never `partial_index`). A per-repo lock serialises healers; a lock still held at the budget exits 75.

## When (by hand, owner only)

- A new worktree of a repository that already has a graph somewhere (a develop-pinned seed checkout): `crg-heal` seeds it, or run the script below.
- No seed anywhere: the owner runs one full `code-review-graph build --repo ROOT`. This is the only full build, and the nightly postprocess covers `rebuild_required` on the seed.
- An existing graph that is `stale_graph` or `stale_worktree`: `crg-heal`. The PostToolUse hook also updates it after edits and HEAD-moving git commands, but only for a graph that already exists.

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

A stale seed is refreshed first with `code-review-graph update --if-locked=skip` (at most `--refresh-seed-seconds`); a seed without a graph, or a `partial_index` seed (its gaps would be copied into every clone), returns `status: skip`. Embeddings stay off during the bootstrap (`CRG_EMBEDDINGS=off`). The whole run fits in 840 s (`TOTAL_BUDGET_SECONDS`), so a caller's own timeout must be longer; every stage catches its timeout and reports it as JSON instead of hanging.

## After preparation

Check `_graph.status` in the first graph response of the session (`ok` ready; `partial_index`, `stale_graph`, `stale_worktree` degraded with data) and pass root, branch/revision, status, nodes, files and `gaps` to graph-dependent agents.
<!-- if:zcode -->
ZCode: the update hook also fires on ApplyPatch.
<!-- endif -->

{{block:graph-search}}
