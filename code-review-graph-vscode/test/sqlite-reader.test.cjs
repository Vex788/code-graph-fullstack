/**
 * Unit tests for the graph reader and its engine selection.
 *
 * Runs the code the extension ships (`dist/extension.js`) under plain Node,
 * with fixtures built by `node:sqlite`. No VS Code download, no Electron:
 * CI runs these on the Node 22 runtime that VS Code Server (WSL/SSH) uses,
 * which is the environment from issue #63.
 */

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { loadExtensionWithStub } = require('./vscodeStub.cjs');

// Load the compiled bundle the way the activation smoke test does: with a
// stub `vscode` module, under plain Node.
const { extension } = loadExtensionWithStub(
  path.join(__dirname, '..', 'dist', 'extension.js'),
  os.tmpdir(),
);

const {
  SqliteReader,
  openSqliteEngine,
  SqliteUnavailableError,
  betterSqlite3Probe,
} = extension;

const { DatabaseSync } = require('node:sqlite');

// ---------------------------------------------------------------------------
// Fixture
// ---------------------------------------------------------------------------

const SCHEMA_SQL = `
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
CREATE INDEX idx_nodes_file ON nodes(file_path);
CREATE INDEX idx_edges_source ON edges(source_qualified);
CREATE INDEX idx_edges_target ON edges(target_qualified);
`;

const NODES = [
  { kind: 'File', name: 'auth.py', qn: 'src/auth.py', file: 'src/auth.py', start: 1, end: 50, lang: 'python', isTest: 0 },
  { kind: 'Function', name: 'login', qn: 'src/auth.py::login', file: 'src/auth.py', start: 5, end: 20, lang: 'python', isTest: 0, params: '(username, password)', ret: 'bool' },
  { kind: 'Function', name: 'logout', qn: 'src/auth.py::logout', file: 'src/auth.py', start: 22, end: 35, lang: 'python', isTest: 0 },
  { kind: 'File', name: 'routes.py', qn: 'src/routes.py', file: 'src/routes.py', start: 1, end: 40, lang: 'python', isTest: 0 },
  { kind: 'Function', name: 'handle_login', qn: 'src/routes.py::handle_login', file: 'src/routes.py', start: 10, end: 30, lang: 'python', isTest: 0 },
  { kind: 'File', name: 'test_auth.py', qn: 'tests/test_auth.py', file: 'tests/test_auth.py', start: 1, end: 30, lang: 'python', isTest: 0 },
  { kind: 'Test', name: 'test_login', qn: 'tests/test_auth.py::test_login', file: 'tests/test_auth.py', start: 5, end: 25, lang: 'python', isTest: 1 },
];

const EDGES = [
  { kind: 'CALLS', src: 'src/routes.py::handle_login', dst: 'src/auth.py::login', file: 'src/routes.py', line: 15 },
  { kind: 'IMPORTS_FROM', src: 'src/routes.py', dst: 'src/auth.py', file: 'src/routes.py', line: 1 },
  { kind: 'CONTAINS', src: 'src/auth.py', dst: 'src/auth.py::login', file: 'src/auth.py', line: 5 },
  { kind: 'CONTAINS', src: 'src/auth.py', dst: 'src/auth.py::logout', file: 'src/auth.py', line: 22 },
  { kind: 'TESTED_BY', src: 'src/auth.py::login', dst: 'tests/test_auth.py::test_login', file: 'tests/test_auth.py', line: 5 },
];

function createFixtureDb() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'crg-reader-'));
  const dbPath = path.join(dir, 'graph.db');
  const db = new DatabaseSync(dbPath);
  db.exec(SCHEMA_SQL);

  const insertNode = db.prepare(
    `INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end,
       language, params, return_type, is_test, file_hash, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  for (const n of NODES) {
    insertNode.run(
      n.kind, n.name, n.qn, n.file, n.start, n.end, n.lang,
      n.params ?? null, n.ret ?? null, n.isTest, 'hash', 1.0,
    );
  }

  const insertEdge = db.prepare(
    `INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line, updated_at)
     VALUES (?, ?, ?, ?, ?, ?)`,
  );
  for (const e of EDGES) {
    insertEdge.run(e.kind, e.src, e.dst, e.file, e.line, 1.0);
  }
  db.prepare(`INSERT INTO metadata (key, value) VALUES ('last_updated', ?)`).run('2026-01-01T00:00:00Z');
  db.close();
  return { dir, dbPath };
}

const fixture = createFixtureDb();
process.on('exit', () => fs.rmSync(fixture.dir, { recursive: true, force: true }));

// ---------------------------------------------------------------------------
// Engine selection
// ---------------------------------------------------------------------------

const hasNodeSqlite = (() => {
  try {
    return typeof require('node:sqlite').DatabaseSync === 'function';
  } catch {
    return false;
  }
})();

test('default engine selection prefers the runtime built-in SQLite', { skip: !hasNodeSqlite }, () => {
  const engine = openSqliteEngine(fixture.dbPath);
  try {
    assert.strictEqual(engine.name, 'node:sqlite');
    const row = engine.prepare('SELECT COUNT(*) AS cnt FROM nodes').get();
    assert.strictEqual(row.cnt, NODES.length);
  } finally {
    engine.close();
  }
});

test('fallback engine is used when the preferred one reports unavailable', () => {
  const calls = [];
  const selected = openSqliteEngine(fixture.dbPath, [
    { name: 'node:sqlite', attempt: () => ({ reason: 'simulated ABI mismatch' }) },
    {
      name: 'fake-sqlite',
      attempt: () => {
        calls.push('fake');
        return {
          factory: {
            name: 'fake-sqlite',
            open: () => ({
              name: 'fake-sqlite',
              exec() {},
              prepare: () => ({ all: () => [], get: () => undefined }),
              close() {},
            }),
          },
        };
      },
    },
  ]);
  assert.strictEqual(selected.name, 'fake-sqlite');
  assert.deepStrictEqual(calls, ['fake']);
  selected.close();
});

test('all engines unavailable raises an actionable SqliteUnavailableError', () => {
  assert.throws(
    () => openSqliteEngine(fixture.dbPath, [
      { name: 'node:sqlite', attempt: () => ({ reason: 'not present on Node 20.11.0' }) },
      { name: 'better-sqlite3', attempt: () => ({ reason: 'native binary built for a different Node.js version' }) },
    ]),
    (err) => {
      assert.ok(err instanceof SqliteUnavailableError);
      assert.match(err.message, /node:sqlite/);
      assert.match(err.message, /Node\.js 22\.13/);
      assert.match(err.message, /not present on Node 20\.11\.0/);
      assert.match(err.message, /native binary built for a different Node\.js version/);
      return true;
    },
  );
});

test('optional better-sqlite3 driver reads the same graph', (t) => {
  const loaded = betterSqlite3Probe.attempt();
  if (!('factory' in loaded)) {
    t.skip(`better-sqlite3 unavailable: ${loaded.reason}`);
    return;
  }
  const engine = openSqliteEngine(fixture.dbPath, [betterSqlite3Probe]);
  try {
    assert.strictEqual(engine.name, 'better-sqlite3');
    const row = engine.prepare('SELECT COUNT(*) AS cnt FROM nodes').get();
    assert.strictEqual(row.cnt, NODES.length);
  } finally {
    engine.close();
  }
});

// ---------------------------------------------------------------------------
// SqliteReader against the fixture
// ---------------------------------------------------------------------------

test('SqliteReader reads nodes, edges, metadata and stats', () => {
  const reader = new SqliteReader(fixture.dbPath);
  try {
    assert.strictEqual(reader.isValid(), true);

    assert.deepStrictEqual(reader.getAllFiles(), [
      'src/auth.py',
      'src/routes.py',
      'tests/test_auth.py',
    ]);

    const nodes = reader.getNodesByFile('src/auth.py');
    assert.deepStrictEqual(nodes.map((n) => n.name), ['auth.py', 'login', 'logout']);
    assert.strictEqual(nodes[1].qualifiedName, 'src/auth.py::login');
    assert.strictEqual(nodes[1].lineStart, 5);
    assert.strictEqual(nodes[1].lineEnd, 20);
    assert.strictEqual(nodes[1].returnType, 'bool');
    assert.strictEqual(nodes[1].isTest, false);

    const login = reader.getNode('src/auth.py::login');
    assert.strictEqual(login.params, '(username, password)');
    assert.strictEqual(reader.getNode('src/auth.py::missing'), undefined);

    // Innermost node wins; File node outside any function.
    assert.strictEqual(reader.getNodeAtCursor('src/auth.py', 10).name, 'login');
    assert.strictEqual(reader.getNodeAtCursor('src/auth.py', 45).kind, 'File');
    assert.strictEqual(reader.getNodeAtCursor('src/auth.py', 999), undefined);

    const outgoing = reader.getEdgesBySource('src/routes.py::handle_login');
    assert.strictEqual(outgoing.length, 1);
    assert.strictEqual(outgoing[0].kind, 'CALLS');
    assert.strictEqual(outgoing[0].targetQualified, 'src/auth.py::login');

    const incoming = reader.getEdgesByTarget('src/auth.py::login');
    assert.deepStrictEqual(incoming.map((e) => e.kind).sort(), ['CALLS', 'CONTAINS']);

    assert.ok(reader.searchNodes('login').length >= 2);
    assert.strictEqual(reader.searchNodes('login', 1).length, 1);
    assert.deepStrictEqual(reader.searchNodes('zzz_no_match_zzz'), []);

    const stats = reader.getStats();
    assert.strictEqual(stats.totalNodes, NODES.length);
    assert.strictEqual(stats.totalEdges, EDGES.length);
    assert.strictEqual(stats.filesCount, 3);
    assert.deepStrictEqual(stats.languages, ['python']);
    assert.strictEqual(stats.lastUpdated, '2026-01-01T00:00:00Z');
    assert.strictEqual(stats.nodesByKind.Function, 3);
    assert.strictEqual(stats.edgesByKind.CONTAINS, 2);
    assert.strictEqual(reader.getMetadata('last_updated'), '2026-01-01T00:00:00Z');
    assert.strictEqual(reader.getMetadata('missing_key'), undefined);

    const impact = reader.getImpactRadius(['src/auth.py'], 2);
    assert.strictEqual(impact.changedNodes.length, 3);
    const impactedNames = impact.impactedNodes.map((n) => n.name);
    assert.ok(impactedNames.includes('handle_login'));
    assert.ok(impactedNames.includes('test_login'));
    assert.ok(impact.impactedFiles.length > 0);
    assert.ok(impact.edges.length > 0);

    const shallow = reader.getImpactRadius(['src/auth.py'], 0);
    assert.strictEqual(shallow.changedNodes.length, 3);
    assert.strictEqual(shallow.impactedNodes.length, 0);

    assert.strictEqual(reader.getNodesBySize(15)[0].name.length > 0, true);
  } finally {
    reader.close();
  }
});

test('SqliteReader reports a closed database and a missing file', () => {
  const reader = new SqliteReader(fixture.dbPath);
  reader.close();
  assert.strictEqual(reader.isValid(), false);
  assert.strictEqual(reader.checkSchemaCompatibility(), 'Database is not open');

  assert.throws(() => new SqliteReader(path.join(fixture.dir, 'missing.db')));
});
