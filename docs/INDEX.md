# Documentation Index

## Specification (this fork; generated pages are checked by `tests/test_docs_drift.py`)

- [spec/contract.json](spec/contract.json) -- Machine-readable consumer contract (`code-review-graph contract --json`)
- [spec/TOOLS.md](spec/TOOLS.md) -- Every MCP tool: purpose, parameters, read-only flag, presets (generated)
- [spec/CLI.md](spec/CLI.md) -- Every CLI command and its options (generated)
- [spec/EDGES.md](spec/EDGES.md) -- Node and edge kinds from the kind registry (generated)
- [spec/READINESS.md](spec/READINESS.md) -- Status machine, write epoch, writer lock, exit codes, `_graph` receipt
- [spec/CONFIG.md](spec/CONFIG.md) -- Every `CRG_*` variable, `.code-review-graph.toml`, `languages.toml`, `watch.toml`
- [spec/HARNESS.md](spec/HARNESS.md) -- Harness kit, targets, regions, config fragment, ownership with laya
- [perf/](perf/) -- Stage timing baselines (`baseline.json`, `fs6-current.json`)

## Guides (inherited from upstream; the spec pages above win where they differ)

- [USAGE.md](USAGE.md) -- How to install and use
- [FAQ.md](FAQ.md) -- How it compares to LSP, RAG, grep, and similar tools; when not to use it
- [FEATURES.md](FEATURES.md) -- What's included, changelog
- [COMMANDS.md](COMMANDS.md) -- MCP tools (current list: spec/TOOLS.md), 5 MCP prompts, skills, and CLI commands
- [GITHUB_ACTION.md](GITHUB_ACTION.md) -- Risk-scored PR review comments via GitHub Actions
- [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md) -- Bring your own language via `.code-review-graph/languages.toml`
- [LLM-OPTIMIZED-REFERENCE.md](../code_review_graph/docs/LLM-OPTIMIZED-REFERENCE.md) -- Token-optimized reference for MCP-capable AI coding agents
- [architecture.md](architecture.md) -- System design and data flow
- [schema.md](schema.md) -- Graph node/edge schema, SQLite tables (including flows, communities, FTS5)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) -- Common issues and fixes (including Windows/WSL)
- [REPRODUCING.md](REPRODUCING.md) -- Reproducing every benchmark number (pinned SHAs, seeded runs, tokenizer calibration)
- [ROADMAP.md](ROADMAP.md) -- Shipped and planned features
- [LEGAL.md](LEGAL.md) -- License and privacy
