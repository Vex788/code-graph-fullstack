# Configuration

Three layers, most specific first: environment variables (`CRG_*`), the tracked
repository file `.code-review-graph.toml`, and per-repository or per-user files
(`languages.toml`, the legacy `.code-review-graph/config.toml`, `watch.toml`).
A missing or broken file never raises: it is logged once and read as empty.

## Environment variables

`tests/test_docs_drift.py` fails when code reads a `CRG_*` name this page does
not list. Defaults are the values at the read site.

### Storage and repository

| Variable | Default | Effect |
|---|---|---|
| `CRG_DATA_DIR` | `<repo>/.code-review-graph` | Where the graph lives, used verbatim (a registry `--data-dir` entry wins). |
| `CRG_HOME` | `~/.code-review-graph` | Per-user state: `registry.json`, `watch.toml`, daemon pid/state, logs. |
| `CRG_REPO_ROOT` | auto-detected | Explicit repository root override. |
| `CRG_RECURSE_SUBMODULES` | off | `1`/`true`/`yes`: include git submodule files. |
| `CRG_GIT_TIMEOUT` | `30` | Seconds per git subprocess (build, update, watch). When set explicitly it is also the fallback discovery budget. |
| `CRG_DISCOVERY_TIMEOUT` | `5` | Seconds per subprocess for read-only change discovery (a review tool auto-detecting `changed_files`). Exhausting it is reported as a git failure, never as "no changes" (#262). |

### Parsing and build

| Variable | Default | Effect |
|---|---|---|
| `CRG_PARSE_WORKERS` | `min(cpu, 8)` | Parallel parse workers. |
| `CRG_PARSE_EXECUTOR` | `auto` | `process` or `thread` parse pool. |
| `CRG_SERIAL_PARSE` | off | `1`: parse in the main process (debugging). |
| `CRG_PARSER_LOAD_TIMEOUT_SECONDS` | `5` | Budget for loading one tree-sitter grammar. |
| `CRG_MAX_FILE_BYTES` | `2097152` | Files larger than this are skipped and reported. |
| `CRG_NESTED_OUTPUT_SCAN` | `1` | `0` turns off detection of nested build-output directories. |
| `CRG_NESTED_IGNORE_TTL` | `300` | Seconds the nested-output scan result is reused. |
| `CRG_MODULE_SCAN_DEPTH` | `3` | Depth limit of the module-directory scan. |
| `CRG_MODULE_SCAN_MAX_DIRS` | `2000` | Directory cap of the module-directory scan. |
| `CRG_DEPENDENT_HOPS` | `2` | Hops of dependents re-parsed after a change. |
| `CRG_DRIFT_MTIME_TOLERANCE` | `0.25` | Seconds of mtime slack before the drift sweep re-hashes a file (the hash decides drift). |
| `CRG_MAX_DELTA_ENTRIES` | `50000` | Journaled change-delta size above which the next post-processing recomputes flows and communities fully. |
| `CRG_LEIDEN_SEED` | `42` | Seed for Leiden community detection. |

### Locking and jobs

| Variable | Default | Effect |
|---|---|---|
| `CRG_WRITER_LOCK_WAIT` | `120` | Seconds a writer waits for the graph lock. |
| `CRG_WRITER_LOCK_TOKEN` | set by the holder | Lets a child inherit the writer lock ([READINESS.md](READINESS.md#writer-lock)). |
| `CRG_REGISTRY_LOCK_TOKEN` | set by the holder | The same re-entrancy for the multi-repo registry lock. |
| `CRG_BUILD_WAIT_SECONDS` | `25` | How long `build_or_update_graph_tool` waits before answering `building`. |
| `CRG_FAULT_AT` | unset | Test hook: fail a write at the named stage. |
| `CRG_FAULT_MODE` | raise | Test hook: `kill` SIGKILLs at `CRG_FAULT_AT` instead of raising. |

### MCP server and queries

| Variable | Default | Effect |
|---|---|---|
| `CRG_TOOLS` | all tools | Tool names and presets to expose, like `serve --tools` ([TOOLS.md](TOOLS.md)). |
| `CRG_TOOL_TIMEOUT` | `0` (off) | Seconds before a bounded tool call is cancelled and answered with `status: error`. Writing tools (build, post-process, embed, wiki, apply-refactor) are never cut short. |
| `CRG_RECEIPT_TTL` | `2` | Seconds the `_graph` receipt is reused while graph and git are unchanged; `0` disables. |
| `CRG_SELF_HEAL_BUDGET` | `40` | Seconds a tool call may spend catching a stale graph up before answering; `0` disables the query-time self-heal. |
| `CRG_SELF_HEAL_DEBOUNCE` | `60` | Seconds between self-heal attempts per repository. |
| `CRG_MAX_SEARCH_RESULTS` | `20` | Default search result cap. |
| `CRG_MAX_IMPACT_NODES` | `500` | Impact radius node cap. |
| `CRG_MAX_IMPACT_DEPTH` | `2` | Default impact depth. |
| `CRG_MAX_BFS_DEPTH` | `15` | Hard BFS depth ceiling. |
| `CRG_BFS_ENGINE` | `sql` | `networkx` selects the legacy traversal. |
| `CRG_IMPACT_DEPTH_DECAY` | `0.6` | Impact score decay per hop (0 to 1). |
| `CRG_IMPACT_SCORE_FLOOR` | `0.05` | Impact scores below this are dropped. |
| `CRG_MAX_TRANSITIVE_FRONTIER` | `50` | Frontier cap for transitive expansion. |
| `CRG_MAX_CHANGED_FUNCS` | `500` | Changed-function cap in change detection. |
| `CRG_CHURN_WINDOW_DAYS` | `90` | Git churn window for risk scoring. |

### Watch and daemon

| Variable | Default | Effect |
|---|---|---|
| `CRG_WATCH_MAX_WAIT` | `10` | Maximum debounce wait before a watch batch runs. |
| `CRG_WATCH_PLAN_DEPTH` | `3` | Depth to which watch planning splits the tree to keep ignored dirs off the OS watch list. |
| `CRG_WATCH_SPLIT_MIN_DIRS` | `4` | Directories an ignored tree must hold before the plan splits around it. |
| `CRG_MAX_WATCH_SCHEDULES` | `24` | Cap on watched directory schedules. |
| `CRG_WATCH_HEALTH_INTERVAL` | `10` | Seconds between watch health beats. |
| `CRG_WATCH_HEALTH_STALE` | `90` | Seconds without a beat before the daemon restarts a watcher. |
| `CRG_DAEMON_BUILD_TIMEOUT` | `3600` | Seconds allowed for the daemon's initial build per repo. |
| `CRG_RESTART_BACKOFF` | `30` | First restart backoff (seconds). |
| `CRG_RESTART_BACKOFF_MAX` | `900` | Restart backoff ceiling. |
| `CRG_RESTART_HEALTHY_AFTER` | `600` | Seconds of health that reset the backoff. |

### Embeddings

| Variable | Default | Effect |
|---|---|---|
| `CRG_EMBEDDINGS` | file, else `off` | `off`, `on`, a profile (`fast`, `balanced`, `accurate`, `legacy`) or a cloud provider; overrides `[embeddings]`. |
| `CRG_EMBEDDING_DIM` | profile | Vector size override. |
| `CRG_EMBEDDING_MODEL` | profile | sentence-transformers model; applies to `legacy`/`local` only. |
| `CRG_ALLOW_REMOTE_CODE` | `0` | `1` allows `trust_remote_code` for sentence-transformers models. |
| `CRG_ACCEPT_CLOUD_EMBEDDINGS` | unset | `1` silences the warning before code is sent to a cloud provider. |
| `CRG_OPENAI_API_KEY`, `CRG_OPENAI_BASE_URL`, `CRG_OPENAI_MODEL` | unset | OpenAI-compatible provider (all three required). |
| `CRG_OPENAI_DIMENSION`, `CRG_OPENAI_BATCH_SIZE` | provider | Optional dimension and batch size; `CRG_OPENAI_*` is this family. |
| `CRG_VOYAGE_MODEL`, `CRG_VOYAGE_BASE_URL` | provider | Voyage provider model and endpoint. |
| `CRG_VOYAGE_OUTPUT_DIMENSION`, `CRG_VOYAGE_OUTPUT_DTYPE` | provider | Voyage vector size and dtype. |
| `CRG_VOYAGE_BATCH_SIZE`, `CRG_VOYAGE_MIN_INTERVAL_SEC` | provider | Voyage batch size and request spacing. |

### Harness kit hook (`crg-update.py`)

| Variable | Default | Effect |
|---|---|---|
| `CRG_BIN` | `code-review-graph` on PATH | Graph binary the hook and kit scripts call. |
| `CRG_UPDATE_DEBOUNCE` | `2` | Seconds the hook worker waits to batch edits. |
| `CRG_UPDATE_INLINE` | off | `1`: update in the hook process (tests). |
| `CRG_UPDATE_STATE_DIR` | target `state_home` | Hook queue and state directory. |
| `CRG_UPDATE_LOG_DIR` | target `log_home` | Hook log directory. |
| `CRG_HEAL_STATE_DIR` | `crg-heal` next to the target's hook state | `crg-heal` lock directory (per-repo locks, `clone.lock`). |
| `CRG_RECONCILE_STATE_DIR` | `crg-reconcile` next to the target's hook state | Per-root update-attempt fingerprints (worktree state plus the tool's `--version` and contract version) `crg-heal` shares with the reconcile loop. |
| `CRG_HEAL_POLL_SECONDS` | `5` | Seconds `crg-heal` waits between status reads of a `building` graph (tests). |
| `CRG_HEAL_UPDATE_SECONDS` | `180` | Cap in seconds for the separate `update --skip-flows` stage after `crg-heal` clones a seed graph (tests); a timeout leaves the clone `stale_graph`. |
| `CRG_STUB_MSG` | unset | Message a test stub binary prints; used only by the harness kit fixtures. |
| `CRG_STUB_RC` | `0` | Exit code of the `--selftest` stub binary. |

### Names in code that are not settings

`CRG_HOME_ENV` (the constant holding `"CRG_HOME"`), `CRG_HOOK_MARKER` (marker
comment in installed hooks), `CRG_MSG` (a shell variable inside a generated
hook) and `CRG_REPO__` (from the `__CRG_REPO__` placeholder in hook templates).

## `.code-review-graph.toml` (tracked, repository root)

```toml
[embeddings]
enabled = false          # off by default; `code-review-graph embeddings enable`
profile = "balanced"     # fast | balanced | accurate | legacy, or a cloud provider
# model = "..."          # override the profile's model
# dim = 256              # vector size (MRL-truncated where the model supports it)
dtype = "float16"        # float16 | float32
batch_size = 64
# threads = 4            # default: half the cores
idle_unload_s = 600      # release the model after this many idle seconds

[resolvers.jsp]          # every key optional; these are the defaults' shape
enabled = true
web_root = "web"
source_root = "src"
route_annotations = ["UrlBinding", "RequestMapping"]
bean_attribute = "beanclass"
bean_package_prefix = "com."
dead_url_suffixes = [".action"]
context_paths = ["/myapp"]
```

`[resolvers.*]` falls back to the untracked `.code-review-graph/config.toml`
when the tracked file has no such table.

## `languages.toml`

`.code-review-graph/languages.toml` adds languages without code: one
`[languages.<name>]` table per language (extensions, grammar, node types). See
[CUSTOM_LANGUAGES.md](../CUSTOM_LANGUAGES.md).

## `watch.toml`

`$CRG_HOME/watch.toml` configures `code-review-graph daemon`, which runs one
`watch` child per repository and reloads the file live:

```toml
[daemon]
session_name = "crg-watch"
log_dir = "~/.code-review-graph/logs"
poll_interval = 2

[[repos]]
path = "~/src/my-repo"
alias = "my-repo"        # default: the directory name
```
