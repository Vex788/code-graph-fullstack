# Readiness, write epoch, lock and exit codes

The machine-readable form of everything here is [contract.json](contract.json)
(`statuses`, `embeddings_statuses`, `exit_codes`, `schemas.graph_receipt`,
`schemas.status_json`). This page explains what the values mean.

## Status machine

`compute_readiness(facts)` in `code_review_graph/readiness.py` is a pure
function: callers gather facts (metadata rows, git state, source check), it
returns one status plus every failed reason. The most severe status wins:

`missing_graph > building > rebuild_required > partial_index > stale_graph > stale_worktree > ok`

| Status | Reasons | Meaning | What to do |
|---|---|---|---|
| `missing_graph` | `missing_graph` | No `graph.db` for this root. | `code-review-graph build` |
| `building` | `build_in_progress` | A writer holds the lock (build, update, migration). | Wait and retry; never build inline. |
| `rebuild_required` | `schema_migration_pending`, `index_generation_mismatch` | The schema or index generation is older than this build reads. | `code-review-graph build` (an `update` exits 4) |
| `partial_index` | `write_epoch_open`, `failed_files`, `resolver_failures` | A write did not finish, or some files or resolvers failed. Results are usable but incomplete. | Say "degraded" in any claim; `update` retries failures. |
| `stale_graph` | `head_moved`, `git_unavailable` | HEAD differs from `built_at_commit`, or git failed (never read as clean). | `code-review-graph update` |
| `stale_worktree` | `worktree_changed` | A file on disk has no node, or an indexed file is gone. | `code-review-graph update` |
| `ok` | none | Epoch closed, no failures, generation current, HEAD matches the build, source matches the build. | Use the graph. |

Edited indexed files alone do not make a graph `stale_worktree`: they are
counted in `source_identity.edited_indexed_count` and the hook updates them.
Likewise an indexable file over `CRG_MAX_FILE_BYTES` that has no node is never
parsed, so it is listed in `source_identity.skipped_oversize_paths` (`skipped_oversize_count`
in the `_graph` receipt) instead of counting as drift; it can never be indexed, so
treating it as `stale_worktree` would re-run every update forever.

Embeddings have their own sub-state, independent of the status above:

| `embeddings` | Meaning |
|---|---|
| `off` | Not enabled (the default). Search runs keyword/FTS. |
| `ready` | Every embeddable node has a vector. |
| `stale` | Some nodes lack vectors; a background job is catching up. |
| `unavailable` | Enabled, but the provider cannot load (missing extra, offline model). |

## Write epoch

Every graph write runs inside an epoch:

1. `write_epoch_open` is bumped when the write starts.
2. Files are parsed and diff-stored in batched transactions; each resolver runs
   in its own transaction and records its failure instead of aborting.
3. One final transaction stamps `built_at_commit`, `failed_files`,
   `resolver_failures` and `write_epoch_closed`.

A process killed between 1 and 3 leaves `write_epoch_closed < write_epoch_open`,
which reads as `partial_index` (`write_epoch_open`) until the next write closes it.

## Writer lock

- One writer per database: an OS file lock on `<data_dir>/graph.db.lock`, held
  for one operation. The kernel drops it when the holder dies, so a killed
  writer never leaves a stale lock.
- A child the holder starts inherits the lock through `CRG_WRITER_LOCK_TOKEN`.
- Readers never lock. A reader that finds a pending migration while another
  process writes reports `building` or `rebuild_required`.
- Writing commands take `--if-locked skip|wait|fail` and `--lock-wait SECONDS`
  (default `wait`, `CRG_WRITER_LOCK_WAIT`). `code-review-graph lock -- CMD`
  runs CMD holding the lock with the token exported.

## Exit codes

| Code | Name | Meaning |
|---|---|---|
| 0 | `ok` | Success. `status --json` also exits 0 on `missing_graph`: it answers. |
| 1 | `error` | Failure (includes a schema newer than this build). |
| 2 | `usage` | Bad arguments. |
| 3 | `degraded` | A build or update finished `partial` (see `partial_index`). |
| 4 | `rebuild_required` | An update cannot proceed incrementally; run `build`. |
| 75 | `lock_busy` | Another writer holds the lock (`--if-locked skip` or `fail`), or a migration is pending under another writer. Hooks treat it as a no-op. |

## The `_graph` receipt

Every MCP tool response carries `_graph` (schema `graph_receipt`). The fields
agents act on:

| Field | Meaning |
|---|---|
| `status` | One of the statuses above, or `error`. |
| `reasons` | Every failed readiness condition. |
| `embeddings` | Embeddings sub-state. |
| `contract_version`, `schema_version`, `index_generation` | What the reader and the database speak. |
| `built_at_commit`, `current_sha`, `head_matches_build` | Build anchor versus HEAD. |
| `failed_files`, `resolver_failures` | Counts behind `partial_index`. |
| `source_identity.runtime_matches_source` | The running server's version equals the installed package's; false after an upgrade until the server restarts. |
| `source_identity.index_matches_runtime` | Schema and index generation are current for this build. |
| `source_identity.source_matches_build` | No missing or deleted indexed paths. |
| `source_identity.edited_indexed_count` | Indexed files edited since the build. |
| `etag` | Changes whenever the graph or git state changes; the receipt is reused for `CRG_RECEIPT_TTL` seconds otherwise. |

Rule for agents: act on `ok`; treat `partial_index`, `stale_graph` and `stale_worktree`
as degraded with data (usable: say so and cite the gaps); for anything else heal once
and continue (`GRAPH_PREP_REQUIRED` in the kit now means "run `crg-heal`, then continue").
