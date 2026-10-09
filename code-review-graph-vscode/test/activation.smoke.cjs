/**
 * Activation smoke test for a packaged extension.
 *
 * Loads the compiled bundle from an extension directory (a directory that has
 * `package.json` and `dist/extension.js`, e.g. an unpacked VSIX) inside the
 * stub `vscode` module from `vscodeStub.cjs`, runs `activate()` against a
 * temporary workspace that holds a real graph database, and asserts:
 *
 *   1. activation does not throw;
 *   2. every command contributed in package.json is registered;
 *   3. the reader opens the database through the runtime's own SQLite engine
 *      (`node:sqlite`) and answers a real query end to end.
 *
 * Nothing here downloads Electron, so the check runs in CI on the same plain
 * `node` runtime a VS Code Server (WSL/SSH) extension host uses. That is the
 * environment from issue #63, where the extension used to fail activation
 * with "Cannot find module 'better-sqlite3'" or a NODE_MODULE_VERSION
 * mismatch and its commands were never registered.
 *
 * Usage: node test/activation.smoke.cjs [extension-root] [degraded]
 *   extension-root  directory holding package.json + dist/extension.js
 *                   (default: the repository's code-review-graph-vscode/)
 *   degraded        simulate a runtime with neither `node:sqlite` nor
 *                   `better-sqlite3`; activation must still register every
 *                   command and report the missing engine
 */

'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { loadExtensionWithStub } = require('./vscodeStub.cjs');

const extensionRoot = path.resolve(
  process.argv[2] || path.join(__dirname, '..'),
);
const degraded = process.argv[3] === 'degraded';

// ---------------------------------------------------------------------------
// Fixture: workspace with a graph database built by the Python writer
// ---------------------------------------------------------------------------

function createWorkspaceWithGraph() {
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'crg-smoke-'));
  const dbDir = path.join(workspaceRoot, '.code-review-graph');
  fs.mkdirSync(dbDir, { recursive: true });
  const dbPath = path.join(dbDir, 'graph.db');

  if (degraded) {
    // Engine selection fails before the file is opened; contents are moot.
    fs.writeFileSync(dbPath, '');
    return workspaceRoot;
  }

  const { DatabaseSync } = require('node:sqlite');
  const db = new DatabaseSync(dbPath);
  db.exec(`
    CREATE TABLE nodes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL UNIQUE,
      file_path TEXT NOT NULL, line_start INTEGER, line_end INTEGER,
      language TEXT, parent_name TEXT, params TEXT, return_type TEXT,
      modifiers TEXT, is_test INTEGER DEFAULT 0, file_hash TEXT,
      extra TEXT DEFAULT '{}', updated_at REAL NOT NULL
    );
    CREATE TABLE edges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL, source_qualified TEXT NOT NULL,
      target_qualified TEXT NOT NULL, file_path TEXT NOT NULL,
      line INTEGER DEFAULT 0, extra TEXT DEFAULT '{}', updated_at REAL NOT NULL
    );
    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, language, is_test, file_hash, updated_at)
      VALUES ('File', 'auth.py', 'src/auth.py', 'src/auth.py', 1, 50, 'python', 0, 'aaa', 1.0);
    INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, language, is_test, file_hash, updated_at)
      VALUES ('Function', 'login', 'src/auth.py::login', 'src/auth.py', 5, 20, 'python', 0, 'aaa', 1.0);
    INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, language, is_test, file_hash, updated_at)
      VALUES ('Function', 'handle_login', 'src/routes.py::handle_login', 'src/routes.py', 10, 30, 'python', 0, 'bbb', 1.0);
    INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line, updated_at)
      VALUES ('CALLS', 'src/routes.py::handle_login', 'src/auth.py::login', 'src/routes.py', 15, 1.0);
    INSERT INTO metadata (key, value) VALUES ('last_updated', '2026-01-01T00:00:00Z');
  `);
  db.close();
  return workspaceRoot;
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

async function main() {
  const bundlePath = path.join(extensionRoot, 'dist', 'extension.js');
  const manifestPath = path.join(extensionRoot, 'package.json');
  if (!fs.existsSync(bundlePath)) {
    throw new Error(`No compiled bundle at ${bundlePath}`);
  }
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

  const workspaceRoot = createWorkspaceWithGraph();
  const { extension, vscode, state, restore } = loadExtensionWithStub(
    bundlePath,
    workspaceRoot,
    { blockedModules: degraded ? ['node:sqlite', 'better-sqlite3'] : [] },
  );

  // Capture the engine diagnostics line while activate() runs.
  const engineLines = [];
  const originalLog = console.log;
  console.log = (...args) => {
    engineLines.push(args.join(' '));
    originalLog(...args);
  };

  try {
    const context = {
      subscriptions: [],
      extensionUri: vscode.Uri.file(extensionRoot),
      globalState: { get: () => undefined, update: async () => undefined },
      workspaceState: { get: () => undefined, update: async () => undefined },
    };
    await extension.activate(context);
    restore();
  } catch (err) {
    console.log = originalLog;
    restore();
    throw new Error(`activate() threw: ${err && err.stack ? err.stack : err}`);
  } finally {
    console.log = originalLog;
    fs.rmSync(workspaceRoot, { recursive: true, force: true });
  }

  // 1. Every contributed command is registered (issue #63: they were not).
  const contributed = (manifest.contributes?.commands ?? []).map((c) => c.command);
  const missing = contributed.filter((id) => !state.registeredCommands.has(id));
  assert.deepStrictEqual(
    missing,
    [],
    `contributed commands not registered: ${missing.join(', ')}`,
  );
  originalLog(`registered commands: ${contributed.length}/${contributed.length}`);

  if (degraded) {
    // No engine: activation must survive and say why.
    const engineLine = engineLines.find((l) => l.includes('SQLite engine:'));
    assert.strictEqual(engineLine, undefined, 'no engine should have opened');
    const reported = state.errorMessages.find((m) =>
      /no usable SQLite engine/i.test(m),
    );
    assert.ok(
      reported,
      `expected an actionable engine error, got: ${JSON.stringify(state.errorMessages)}`,
    );
    originalLog(`reported to the user: ${reported.split('\n')[0]}`);
    originalLog('PASS: activation survives a runtime without any SQLite engine.');
    return;
  }

  // 2. The runtime's built-in SQLite is the engine that opened the graph.
  const engineLine = engineLines.find((l) => l.includes('SQLite engine:'));
  assert.ok(engineLine, 'expected an engine diagnostics line');
  assert.ok(
    engineLine.includes('node:sqlite'),
    `expected node:sqlite engine, got: ${engineLine}`,
  );
  originalLog(engineLine);

  // 3. A real query end to end through a registered command handler.
  const findCallers = state.registeredCommands.get('codeReviewGraph.findCallers');
  assert.ok(findCallers, 'findCallers handler is registered');
  await findCallers('src/auth.py::login');
  const pick = state.quickPicks.at(-1);
  assert.ok(pick && pick.length === 1, 'findCallers should offer one caller');
  assert.strictEqual(pick[0].label, 'handle_login');
  originalLog(`findCallers(src/auth.py::login) -> ${pick[0].label}`);

  originalLog('PASS: activation, command registration and graph reads are healthy.');
}

main().catch((err) => {
  console.error('FAIL:', err && err.stack ? err.stack : err);
  process.exit(1);
});
