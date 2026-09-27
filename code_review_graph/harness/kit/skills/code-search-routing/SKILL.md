---
name: code-search-routing
description: "Use for any <!-- if:claude|zcode -->PMS <!-- endif -->code search, caller lookup, file structure inspection, cross-stack (JSP/JS/CSS ↔ Java) lookup, or impact analysis routing. Use for: the Grep tool, code graph escalation, semantic search, RTK, context savings, dead-code discovery."
---

# Code search routing: the Grep tool + code graph

Use the cheapest tool that answers the question; escalate only for structural-completeness claims. The Grep/Glob tools are the first surface for exact text; the code graph owns callers, impact, dead-code and cross-stack completeness; RTK owns shell pipelines and non-code output.

"The code-review-graph tools" means the MCP tools of the `code-review-graph` server, named `{{mcp_prefix}}<tool>` in this harness (for example `{{mcp_prefix}}query_graph_tool`). The `code-review-graph` CLI answers the same questions as JSON (`code-review-graph query`, `code-review-graph impact`, `code-review-graph search`, `code-review-graph status --json`).

## Routing table

| Question | First tool | Escalate to |
|---|---|---|
| Exact text, symbol, string, annotation | `Grep` (literal first, regex only if needed) | RTK `rg` when a shell pipe needs the hits |
| File layout: functions, classes, offsets | `query_graph_tool` with `file_summary` | read the whole method or class body (window ceiling 800 lines) |
| Find files by name | `Glob` | `fd` for exotic name globs |
| Who calls X, relationship between symbols | `query_graph_tool` (`callers_of`, `callees_of`, `inheritors_of`, `tests_for`); `batch_query_tool` for several at once | `get_impact_radius_tool` before any impact or dead-code claim |
| Completeness claim ("all callers, pages, assets covered") | `coverage_report_tool` (CLI `code-review-graph coverage --json`) | the root controller prepares the graph; still failing → claim is `UNVERIFIED` |
| Concept search, no exact anchor ("vendor invoice approval") | `semantic_search_nodes_tool`; read `search_mode` and any `warning` in the response | `Grep` on the best candidate names |
| Broad exploration with ranking | `orient_tool` for a task mini-map | `semantic_search_nodes_tool`, then exact anchors |
| JSP ↔ Java binding (which handler renders a page, which pages break when a handler class changes) | Graph FIRST: RENDERS/REQUESTS/INCLUDES/REFERENCES edges plus `get_impact_radius_tool` on the handler class | `Grep` on the handler or bean name when the graph lane is down (text evidence only) |
| CSS selector or stylesheet usage | Graph (`css`/`scss` File nodes and REFERENCES edges; USES_STYLE from fs.6) | `Grep` on the selector text |

## Entry rule and receipt

- No mandatory entry call. Start with the lookup you need and read `_graph.status` in the first response.
- `ok`: ready. `partial_index`: usable but degraded; name the degradation in the claim.
- Any other status (`missing_graph`, `building`, `rebuild_required`, `stale_graph`, `stale_worktree`, or `error` with an `error_code`) means `GRAPH_PREP_REQUIRED` for graph-dependent claims. Exact-source evidence stays valid.
- Call `get_minimal_context_tool` only when a response carries no `_graph` block.
- Read-only agents never build or update the graph. Only the root controller prepares it (see `context-efficient-code-research`).

## Cross-stack query patterns

Edges that exist now: RENDERS (JSP → handler class or Endpoint), REQUESTS (JSP/JS URL → Endpoint or handler), INCLUDES (JSP → JSP), REFERENCES (asset tags: script, stylesheet, page). From fs.6 the graph also carries FORWARDS_TO (`ForwardResolution`/`RedirectResolution`), HANDLES_EVENT (`@HandlesEvent`/`@DefaultHandler`), BINDS (form `name=` → property), USES_STYLE (`class=` → CSS selector) and MAPS_TO (`@Entity`/`@Table`/`*.hbm.xml` → table).

`query_graph_tool` patterns for them (fs.6+; also usable inside `batch_query_tool`):

| Pattern | Answers |
|---|---|
| `pages_for` | JSPs that render or request a handler class |
| `requests_to` | JSP/JS call sites that request an endpoint |
| `included_by` | pages that include a JSP fragment |
| `views_of` | views a handler renders or forwards to |
| `forwards_to` | forward and redirect targets of a handler |
| `maps_to` | table a Hibernate entity maps to |
| `binds_to` | form fields bound to a bean property |
| `styles_of` | CSS selectors a page uses |

Before fs.6, use `callers_of`/`importers_of` on the handler class plus `get_impact_radius_tool`; they follow RENDERS/REQUESTS/INCLUDES too.

## Semantic search

`semantic_search_nodes_tool` always states how it searched in `search_mode` and adds a `warning` when it fell back (for example FTS-only because embeddings are off). Embeddings are off by default; the root controller enables them once with `code-review-graph embeddings enable --profile balanced`. Without them the search is keyword/FTS ranked: treat a miss as "not found by keywords", never as absence.

## Hard rules

- NEVER answer an impact, rename, or dead-code question from Grep output alone. Grep is the discovery front end; the graph is the completeness oracle. Graph unavailable → the claim is `UNVERIFIED`/degraded, not "no callers".
- Grep results count as text evidence only. Reflection, named queries, Hibernate XML and string-built URLs still need explicit scans, EXCEPT JSP/JS → Java links resolved as graph edges: for those the graph IS the oracle.
- Web-layer edges are native indexing, so they survive every build and update: File nodes exist for jsp/jspf/tag/js/jsx/html/htm/css/scss/xml/yaml.
- Completeness claims pass the coverage gate first: `coverage_report_tool` (CLI `code-review-graph coverage --json`) reports `missing_from_graph_total`; with `_graph.status` not `ok`/`partial_index` the count is not believed.
- Known limits: Stripes `{$event}` URL templates do not exact-match, and plain `.properties` values are not edges. URLs ending in `.action` are dropped by the generic default; <!-- if:claude|zcode -->PMS keeps `.action` as a dead suffix in its profile (legacy URLs), so those links are text-search only.<!-- endif --><!-- if:bug-hunter -->a repository profile can keep it as a dead suffix, in which case those links are text-search only.<!-- endif -->
- Escalate only when Grep leaves a concrete unresolved question (ambiguous same-name hits, unexpected zero results, truncated region). Record the escalation reason.
- RTK remains primary for Maven runs, log summaries (`rtk log`), anything piped through a shell, and reads of non-source output.
- One focused question per Grep call; batch independent calls in one block.

## Composite workflow (impact analysis)

1. Discover the anchor: Grep for the symbol; `query_graph_tool` `file_summary` for the owning file.
2. Inventory callers: `query_graph_tool` `callers_of` (and the cross-stack patterns for pages); Grep across the repo, bounded by path or glob, as a cross-check.
3. Verify completeness: `get_impact_radius_tool` on the changed files; reconcile deltas between the two lists before claiming coverage.
4. Read evidence: the full body of each caller cited in the claim, entry point through closing brace.

## Anti-patterns

- Whole-file dumps of files over the read ceiling after the outline is known.
- Repeating the same grep with broader limits to "confirm" a negative result.
- Treating a graph absence, an FTS-only semantic miss, or an empty Grep result as proof a symbol is dead.
- Building the graph from a read-only agent instead of returning `GRAPH_PREP_REQUIRED`.
