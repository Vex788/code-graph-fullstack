---
name: context-efficient-code-research
description: "Use when code research needs bounded symbol, caller, file, log, build, or test evidence without flooding context. Use for: RTK, code-review graph, graph receipt, crg-heal, fixed-window reads, large files, logs, callers."
---

# Context-efficient code research

Use the narrowest authoritative source. Exact text owns literal lookup; the code graph owns structural-completeness claims.

## Routing

1. Resolve the exact <!-- if:claude|zcode -->PMS <!-- endif -->repository root with `git rev-parse --show-toplevel`.
2. RTK-first remains the shell route. Per `code-search-routing`, use Grep/Glob or `rtk rg` for exact symbols, exception text, columns, annotations, JSP/JSPF, JavaScript, CSS, SQL, XML, configuration, generated call sites and logs. This is the primary text surface, not a degraded fallback.
3. Before caller/callee, inheritance, impact, sibling/dead-code completeness, cross-stack or semantic reachability work, go to the code-review-graph tools (`{{mcp_prefix}}<tool>`) with an explicit `repo_root`. There is no mandatory entry call: open with the lookup you need (`query_graph_tool`, `batch_query_tool` for several at once, `orient_tool` for a mini-map) at `detail_level="minimal"`, under a ceiling of ≤5 graph calls / ≤800 graph tokens per task. `get_docs_section_tool("usage")` is the bounded reference for anything this skill does not cover.
4. The first graph response is the receipt. `_graph.status` `ok` is ready. `partial_index`, `stale_graph` and `stale_worktree` are degraded with data: the lane is usable, so proceed, name the degradation in the claim, cite the gaps (`missing_indexed_paths`, failed files) and prove gap files by Read. A blocking status (`missing_graph`, `building`, `rebuild_required`, or `error` with an `error_code`) means heal, then continue (item 5). Call `get_minimal_context_tool` only when a response carries no `_graph`. When the MCP lane is down, the shell receipt is `code-review-graph status --repo ROOT --json` (its `readiness.status` uses the same values; run the binary directly, never through `python3`). `source_identity` appears only in `status --json`; read it once per task when gaps matter, not on every call.
5. Who heals: any agent, once, with `crg-heal --repo ROOT --json`, never by hand with `build_or_update_graph_tool`, `code-review-graph build`, `code-review-graph update` or `code-review-graph clone-graph`. It updates a stale graph, polls a `building` one (never starts a competing build), and re-seeds a missing or `rebuild_required` worktree graph from a validated seed, one clone at a time. Exit 0 ready, 3 usable but degraded (continue, cite `gaps`), 4 not usable (graph-dependent claims `UNVERIFIED`, nothing else), 75 busy (continue with exact-source evidence, retry later). The PostToolUse hook refreshes an existing graph after edits and never builds; re-check the receipt before graph-backed impact or final review.
6. A heal that exits 4 never licenses a manual build, not even once. Focused exact-source evidence stays valid for every claim.<!-- if:claude|zcode --> Review and checkup worktrees read the main root's graph through `graph_repo_root`, or a graph seeded by `crg-heal` or `graph-bootstrap`.<!-- endif -->
7. Use `query_graph_tool` (`callers_of`, `callees_of`, `inheritors_of`, `tests_for`, `file_summary`), `get_impact_radius_tool` and the cross-stack patterns only after an exact symbol or path anchor. For concept questions without an anchor use `semantic_search_nodes_tool` and read its `search_mode` and `warning`: embeddings are off by default, so the usual mode is keyword/FTS. Treat ambiguous same-name results, an unexpected zero, or truncated impact output as low signal: increment `graph_low_signal_events`, switch to exact bounded source for that claim, and do not count the graph result as coverage. Never treat graph absence as proof of no callers or no code.
8. Expand from one concrete symbol, error, or call site. Enumerate callers before an impact or dead-code claim.

<!-- if:claude|zcode -->
For a noisy Maven suite, use `{{harness_home}}/bin/pms-test`; it captures Maven output to a file itself, so no rtk piping is needed. Verify counters and unexpected exceptions from the structured test reports; a clean log summary alone is not proof of success, and a run reporting zero tests is not a pass.
<!-- endif -->
<!-- if:bug-hunter -->
For a noisy test suite, prefer a wrapper that captures output to a file. Verify counters and unexpected exceptions from the structured test reports; a clean log summary alone is not proof of success, and a run reporting zero tests is not a pass.
<!-- endif -->

## Reading contract

- The unit of reading is the enclosing method or class body, not a line count. A method you edit, cite, or judge must be read from its entry point through its closing brace in one contiguous read.
- Keep every direct source window at or below 800 lines. The global pre-tool guard rejects larger native Read windows and common shell whole-file dumps for source files over 800 lines.
<!-- if:claude|zcode -->
- Never page a Java file with `sed -n` or `cat`: use the Read tool with offset+limit (up to 800 lines) or `{{harness_home}}/bin/read-method`. `awk` line filters, `git show HEAD:path`, and reads through a python one-liner are refused too, because their size cannot be known before the read. Locate START with `rg -n 'methodName\(' path`, then take the numeric window through the closing brace. `{{harness_home}}/bin/read-method FILE LINE` prints the enclosing method with its javadoc and annotations, and `--range` gives `START END` for a Read window. It is brace-based, so it works in worktrees the graph does not index. A `[!] boundary inferred` note means the signature was not recognised: verify the closing brace before citing the body.
<!-- endif -->
<!-- if:bug-hunter -->
- A shell window must be numeric: `sed -n 'START,ENDp'`. Regex ranges (`sed -n '/start/,/end/p'`), `awk` line filters, `git show HEAD:path`, and reads through a python one-liner are refused, because their size cannot be known before the read. Locate START with `rg -n 'methodName\(' path`, then take the numeric window through the closing brace.
<!-- endif -->
- Expand into the adjacent window needed to resolve a reference, guard, transaction boundary, or error path.
- Having read a method, state its execution model before judging it: entry point, the branches and early returns in order, and which code below them becomes unreachable under each. A guard that returns early makes every later use of the same flag dead in that path; say so explicitly.
- A source file of 800 lines or fewer may be read whole. Reading a larger file whole requires a whole-file shell read only when the task is explicitly file-wide, the graph is stale, the file is generated, or the user requested it. Set `CLAUDE_FULL_FILE_READ_REASON` to `file-wide-task`, `graph-stale`, `generated-source`, or `user-requested`; every such exception is recorded without file contents.
- Do not split a whole-file read that exceeds the ceiling into sequential windows to evade it. Extending a window to cover the rest of the method or class already under read is not evasion and is expected; a method never ends at an arbitrary window boundary.
- RTK structural truncation is not proof that a contiguous body was read; use a fixed window through `rtk proxy` when exact continuity matters.

## Output discipline

- Ask one focused discovery question per call; batch only independent bounded questions (`batch_query_tool` for graph lookups).
- Prefer counts, filenames, symbol ranges, and failing summaries over raw output.
- Follow an RTK tee path only for the exact failure detail needed.
- Stop searching once an authoritative symbol body, caller set, test result, or configuration value answers the question and the reachability of the code you cite is accounted for.
- Never repeat a broad scan merely to confirm an executable oracle.

## Failure modes

- Graph unavailable after `crg-heal` exited 4: use focused RTK evidence and mark semantic coverage `UNVERIFIED`.
- Blocking receipt: run `crg-heal` once and continue; a repeat call in the same task changes nothing.
- `rebuild_required`: the graph predates the current index generation; `crg-heal` re-seeds a PMS worktree from the seed, the seed and `sp_api_library` wait for the nightly postprocess (the update hook never builds).
- Large file (over the ceiling): do not dump it. Locate the symbol first, then read its whole body.
- Large log: summarize with `rtk log`, search the signature, then inspect only surrounding lines.

## Activation graph

```yaml skill-activation-graph
version: 1
kind: modifier
mode: implicit-root
phase: modifier
requires: []
routes: []
conflicts: []
```
