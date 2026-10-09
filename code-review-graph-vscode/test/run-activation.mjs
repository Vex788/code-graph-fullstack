/**
 * Runs the extension test suite inside a downloaded VS Code.
 *
 * Usage:
 *   node test/run-activation.mjs                                  # source checkout
 *   CRG_EXTENSION_DIR=/tmp/ext node test/run-activation.mjs       # unpacked VSIX
 *
 * The runner prepares a fixture workspace with a small
 * `.code-review-graph/graph.db`, downloads VS Code stable, and executes
 * `test/suite/index.cjs` in the extension host.
 */

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { runTests } from '@vscode/test-electron';

const here = path.dirname(fileURLToPath(import.meta.url));
const extensionDir = process.env.CRG_EXTENSION_DIR
  ? path.resolve(process.env.CRG_EXTENSION_DIR)
  : path.resolve(here, '..');

if (!fs.existsSync(path.join(extensionDir, 'dist', 'extension.js'))) {
  console.error(
    `Missing ${path.join(extensionDir, 'dist', 'extension.js')}: run "npm run compile" first.`
  );
  process.exit(1);
}
if (!fs.existsSync(path.join(here, '..', 'out', 'activation.test.cjs'))) {
  console.error('Missing out/activation.test.cjs: run "npm run compile:tests" first.');
  process.exit(1);
}

// The VS Code IPC socket lives under the cache path's user-data dir; a deep
// checkout path can exceed the 103-char Unix socket limit (macOS).
const cachePath = process.platform === 'darwin'
  ? '/tmp/crg-vscode-test'
  : path.join(os.tmpdir(), 'crg-vscode-test');

// CRG_VSCODE_VERSION pins a VS Code release (e.g. 1.115.0); default is stable.
const vscodeVersion = process.env.CRG_VSCODE_VERSION;

// ---------------------------------------------------------------------------
// Fixture workspace with a small graph database
// ---------------------------------------------------------------------------

const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'crg-workspace-'));
const graphDir = path.join(workspace, '.code-review-graph');
fs.mkdirSync(graphDir, { recursive: true });

const db = new DatabaseSync(path.join(graphDir, 'graph.db'));
db.exec(`
  CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
  );
  CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
  );
  CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
`);
const now = Date.now();
const insertNode = db.prepare(`
  INSERT INTO nodes
    (kind, name, qualified_name, file_path, line_start, line_end,
     language, parent_name, params, return_type, modifiers, is_test,
     file_hash, extra, updated_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`);
insertNode.run(
  'File', 'app.py', 'src/app.py', 'src/app.py', 1, 10,
  'python', null, null, null, null, 0, 'hash-app', '{}', now
);
insertNode.run(
  'Function', 'main', 'app.main', 'src/app.py', 2, 4,
  'python', null, '()', 'None', null, 0, 'hash-app', '{}', now
);
const insertEdge = db.prepare(`
  INSERT INTO edges
    (kind, source_qualified, target_qualified, file_path, line, extra, updated_at)
  VALUES (?, ?, ?, ?, ?, '{}', ?)
`);
insertEdge.run('CONTAINS', 'src/app.py', 'app.main', 'src/app.py', 2, now);
const insertMeta = db.prepare('INSERT INTO metadata (key, value) VALUES (?, ?)');
insertMeta.run('schema_version', '10');
insertMeta.run('last_updated', new Date(now).toISOString());
db.close();

console.log(`Extension under test: ${extensionDir}`);
console.log(`Fixture workspace:    ${workspace}`);

try {
  await runTests({
    extensionDevelopmentPath: extensionDir,
    extensionTestsPath: path.join(here, 'suite', 'index.cjs'),
    cachePath,
    ...(vscodeVersion ? { version: vscodeVersion } : {}),
    launchArgs: [
      workspace,
      '--disable-extensions',
      '--disable-workspace-trust',
      // Keep the IPC socket under a short path (macOS 103-char limit).
      `--user-data-dir=${path.join(cachePath, 'user-data')}`,
      `--extensions-dir=${path.join(cachePath, 'extensions')}`,
    ],
  });
} finally {
  fs.rmSync(workspace, { recursive: true, force: true });
}
