# Changelog

## 0.2.3 - 2026-10-09

### Fixed
- The extension activates on WSL 2 and other hosts whose Node.js ABI differs
  from the native module the VSIX was built against (issue #63), and on VS Code
  1.115 / Electron 39 where `better-sqlite3` cannot be compiled at all
  (issue #218). The graph reader uses the SQLite module built into the
  extension host (`node:sqlite`, Node.js 22.13+), so the VSIX ships no native
  binary and nothing needs rebuilding; `better-sqlite3` is now only an
  optional fallback for older runtimes.
- Activation no longer aborts when the graph database cannot be opened. The
  commands and views are registered and the reason is reported once, instead
  of every command failing with "command not found".

### Added
- CI job that type-checks, unit-tests, packages the VSIX and activates the
  packaged extension on Node 22 (VS Code Server / WSL) and Node 24.
- `npm test` reader unit tests and `npm run test:activation` smoke test that
  run under plain Node, without downloading Electron.

## 0.2.2 - 2026-04-11

### Fixed
- Compatible with VS Code 1.115 / Electron 39 by updating the extension SQLite dependency stack.

## 0.2.1 — 2026-04-08

### Fixed
- Compatible with Python backend schema v6 (no extension-side schema changes in this release)

## 0.2.0 — 2026-03-20

### Added
- **Query Graph** command with 8 query patterns (callers_of, callees_of, imports_of, etc.)
- **Find Callees** command to trace all functions called by a target
- **Find Large Functions** command to identify oversized functions/classes
- **Compute Embeddings** command to generate vector embeddings
- **Watch Mode** command for continuous graph updates
- Cursor-aware resolution for blast radius and navigation commands
- Fuzzy fallback search when exact node matches fail
- SCM decorations for git-aware file status

### Changed
- Updated README with complete command table (13 commands)
- All 13 commands now documented

## 0.1.1 — 2026-03-17

### Fixed
- CLI path setting scoped to `machine` level (security fix)
- Secure nonce generation using `crypto.randomBytes()`

## 0.1.0 — 2026-03-17

Initial release.

- Code Graph tree view with file, class, function, type, and test nodes
- Interactive D3.js graph visualisation in a webview panel
- Blast radius analysis from cursor position
- Find callers and find tests commands
- Search across all graph nodes
- Review changes with git-aware impact analysis
- Auto-update graph on file save
- CLI auto-detection and guided installation
- Getting Started walkthrough
