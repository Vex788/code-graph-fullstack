---
name: pr-context-pack
description: "Build immutable <!-- if:claude|zcode -->PMS <!-- endif -->pull-request context packets. Use for: exact three-dot diffs, hunk inventories, changed-line anchors, graph receipt and coverage, cross-stack orientation, and reusable code navigation."
---

# PR context pack

Build one immutable, deterministic packet before dispatching a PR reviewer. The packet is the shared discovery artifact; consumers verify evidence from it instead of rediscovering refs, files, and anchors.

## Workflow

1. Confirm base and head are exact commit objects available in the local repository.
2. Require the root controller's graph receipt before reviewer dispatch: `code-review-graph status --repo REPO --json` for the review head. Its `readiness.status` must be `ok`, or `partial_index` (degraded: the pack records `graph.degraded=true`); `built_at_commit` and `current_sha` must equal the head and `source_identity.source_matches_build` must be true. Anything else is `GRAPH_PREP_REQUIRED`: this lane never builds or updates the graph itself (see `context-efficient-code-research`).
3. Probe once with `scripts/graph_health.py --repo REPO --head HEAD_SHA --coverage`. It reads only the CLI JSON (`status --json`, `coverage --json`) and exits 2 with `"marker": "GRAPH_PREP_REQUIRED"` naming the gap (not ready, head mismatch, or `missing_from_graph_total > 0`).
4. Build the packet with `scripts/build_context_pack.py`, passing the status JSON as `--graph-receipt`. It adds changed nodes and one-hop callers/callees from a single `code-review-graph impact` call (JSON), never from the database. When the graph lane was unavailable after a recorded preparation attempt, omit the receipt: the pack records `graph.status=fallback`.
5. Attach the risk panel: run `code-review-graph detect-changes --brief --base BASE --repo REPO` into the pack output root as `risk-panel.txt`. The reviewer reads it as the risk orientation for the whole diff (advisory; not hash-bound into `context-pack.json`). JSP, JS and CSS changes carry risk too.
6. Pass only the absolute `context-pack.json` path. The reviewer starts with `read_context_pack.py summary` and reads individual hunk artifacts on demand.
7. Treat every changed source file as covered only when its graph disposition is `indexed` or an explicit `fallback` is recorded.
8. Domain enrichment is optional. Without `--domain-router` the builder uses the installed <!-- if:claude|zcode -->`pms-project-knowledge` router when present<!-- endif --><!-- if:bug-hunter -->router only when one is passed<!-- endif -->; `capabilities.domain=false` records its absence. Never read a missing domain section as evidence that no domain applies.

```bash
code-review-graph status --repo REPO --json > GRAPH_STATUS.json
python3 scripts/graph_health.py --repo REPO --head HEAD_SHA --coverage
python3 scripts/build_context_pack.py \
  --repo REPO --base BASE_SHA --head HEAD_SHA --pr PR_ID \
  --graph-receipt GRAPH_STATUS.json \
<!-- if:claude|zcode -->
  --domain-cards {{skills_home}}/pms-project-knowledge/references/domain-cards.json \
<!-- endif -->
<!-- if:bug-hunter -->
  --domain-router "$DOMAIN_ROUTER" --domain-cards "$DOMAIN_CARDS" \
<!-- endif -->
  --output-root OUTPUT_ROOT
python3 scripts/read_context_pack.py summary --context PACK/context-pack.json
python3 scripts/read_context_pack.py hunk --context PACK/context-pack.json --hunk H-ID
```

The builder uses `base...head`, records the merge base and SHA-256 of `diff.patch`, and writes one hash-bound artifact per hunk. The full patch remains an integrity artifact; never load it into model context. Graph nodes/callers/callees use one global PR budget (default 80 each) with explicit truncation flags. Diffs over 512 KiB or hunks over 64 KiB are marked `review_scope.status=oversized` and must be split or escalated.

## Gates

| Condition | Result |
|---|---|
| Base/head is not a commit | Stop before review |
| Receipt missing, or `readiness.status` not `ok`/`partial_index` | Return `GRAPH_PREP_REQUIRED` before reviewer dispatch |
| Coverage gate: `missing_from_graph_total > 0`, or `built_at_commit`/`current_sha` differ from the head | Return `GRAPH_PREP_REQUIRED` naming the gap |
| `partial_index` receipt | Build the pack; `graph.degraded=true`, and reviewers name the degradation in graph-backed claims |
| Graph unavailable after a recorded preparation attempt | Omit the receipt; `graph.status=fallback` records the semantic degradation |
| Same output path contains a different pack | Stop; never overwrite |
| Consumer sees a mismatched pack id or SHA | Reject the review |
| Diff/hunk exceeds its review budget | Do not dispatch; split or ESCALATE |
| Graph output is truncated | Use focused fallback and preserve the truncation state |

Use `references/context-pack.schema.json` when changing the packet format. JSP/JSPF/tag/JS/JSX/HTML/CSS/SCSS/XML/YAML are graph-indexed File nodes carrying RENDERS/REQUESTS/INCLUDES/REFERENCES edges (FORWARDS_TO, HANDLES_EVENT, BINDS, USES_STYLE, MAPS_TO and jQuery REQUESTS are not produced yet), so page and asset questions belong to the graph too, through the cross-stack `query_graph_tool` patterns (`pages_for`, `requests_to`, `included_by`, `views_of`); `callers_of`/`importers_of` follow only CALLS/IMPORTS_FROM; coverage is still inferred only through the coverage gate. Whatever the gate reports missing or excluded stays a text-search surface.

The legacy `--graph-db` flag is accepted for old callers, but its path is ignored: it probes the live graph through the CLI instead. No script here opens `graph.db`.

## Common mistakes

- Using `git diff base head`: branch drift enters the review. Use the builder.
- Building or updating the graph inside a reviewer: return `GRAPH_PREP_REQUIRED`; the root controller prepares once per worktree.
- Treating an unprepared graph as fallback: fallback requires a recorded preparation failure or unavailable graph lane.
- Treating a graph miss as absence: record the fallback and use focused text inspection.
- Skipping the graph because RTK is faster: the graph receipt and orientation come first; RTK is the bounded fallback and text surface.
- Reading `diff.patch` wholesale: use the summary, file slice, and exact hunk artifacts.

## Activation graph

```yaml skill-activation-graph
version: 1
kind: root
mode: implicit-root
phase: proof
requires: []
routes:
  - skill: context-efficient-code-research
    predicate: bounded-research
    when: callers, files, logs, or large sources require bounded research
  - skill: usage-analyzer
    predicate: impact-analysis
    when: a changed signature or symbol needs impact analysis
  - skill: evidence-gate
    predicate: completion-evaluation
    when: evaluating pack completeness, anchors, coverage, or completion
conflicts: []
```
