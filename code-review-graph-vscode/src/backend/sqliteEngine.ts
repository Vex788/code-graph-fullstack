/**
 * SQLite engine selection for the graph reader.
 *
 * The VS Code extension host is a Node.js runtime that differs per install:
 * Electron on the desktop, the VS Code Server's own Node over WSL/SSH, and a
 * different major version with every Electron upgrade. A prebuilt native
 * module can never match all of them -- that is why activation died on WSL 2
 * with NODE_MODULE_VERSION 137 vs 127 (issue #63) and why `better-sqlite3`
 * could not be compiled against Electron 39 at all (issue #218).
 *
 * The reader therefore prefers the SQLite that ships inside the runtime
 * itself: `node:sqlite` (Node.js 22.13+, no flag), which has no ABI to match
 * and nothing to package. `better-sqlite3` stays as an optional fallback for
 * runtimes without the built-in module.
 */

export type SqlValue = null | number | bigint | string | Uint8Array;

/** Minimal prepared-statement surface used by `SqliteReader`. */
export interface SqliteStatement {
  all(...params: SqlValue[]): unknown[];
  get(...params: SqlValue[]): unknown;
}

/** Minimal database surface used by `SqliteReader`. */
export interface SqliteEngine {
  readonly name: string;
  exec(sql: string): void;
  prepare(sql: string): SqliteStatement;
  close(): void;
}

export interface SqliteEngineFactory {
  readonly name: string;
  /** Opens the database read-only. Real open errors propagate to the caller. */
  open(dbPath: string): SqliteEngine;
}

/** One candidate engine; `attempt()` reports either a factory or the reason it is unusable. */
export interface SqliteEngineProbe {
  readonly name: string;
  attempt(): { factory: SqliteEngineFactory } | { reason: string };
}

/** Thrown when the runtime can offer no SQLite engine at all. */
export class SqliteUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SqliteUnavailableError';
  }
}

const BUSY_TIMEOUT_MS = 5000;

/**
 * Connection pragmas shared by both engines. Best effort: a read-only
 * connection must not fail to open because a pragma is rejected.
 */
function applyPragmas(exec: (sql: string) => void): void {
  try {
    exec(`PRAGMA busy_timeout = ${BUSY_TIMEOUT_MS}`);
  } catch {
    // Keep the default timeout; reads may report SQLITE_BUSY under a writer.
  }
  try {
    exec('PRAGMA journal_mode = WAL');
  } catch {
    // The database keeps whatever journal mode it already has.
  }
}

/**
 * The runtime's built-in SQLite. Node.js 22.5+ ships `node:sqlite`
 * (unflagged since 22.13); Electron builds since 37 expose it too.
 */
export const nodeSqliteProbe: SqliteEngineProbe = {
  name: 'node:sqlite',
  attempt() {
    let mod: typeof import('node:sqlite');
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      mod = require('node:sqlite');
    } catch {
      return { reason: `not present on Node ${process.versions.node}` };
    }
    if (!mod || typeof mod.DatabaseSync !== 'function') {
      return { reason: 'DatabaseSync is missing from the module' };
    }
    const DatabaseSync = mod.DatabaseSync;

    return {
      factory: {
        name: 'node:sqlite',
        open(dbPath: string): SqliteEngine {
          const db = new DatabaseSync(dbPath, { readOnly: true });
          applyPragmas((sql) => db.exec(sql));
          return {
            name: 'node:sqlite',
            exec: (sql) => db.exec(sql),
            prepare(sql: string): SqliteStatement {
              const stmt = db.prepare(sql);
              return {
                // The built-in typings model a named-parameters object as the
                // first argument; this adapter only ever binds `?` parameters.
                all: (...params) => stmt.all(...(params as never[])) as unknown[],
                get: (...params) => stmt.get(...(params as never[])),
              };
            },
            close: () => db.close(),
          };
        },
      },
    };
  },
};

/** Minimal shape of the optional `better-sqlite3` module (no bundled types needed). */
interface BetterSqlite3Statement {
  all(...params: unknown[]): unknown[];
  get(...params: unknown[]): unknown;
}

interface BetterSqlite3Database {
  prepare(sql: string): BetterSqlite3Statement;
  exec(sql: string): void;
  close(): void;
}

interface BetterSqlite3Constructor {
  new (dbPath: string, options?: { readonly?: boolean }): BetterSqlite3Database;
}

function describeLoadError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  if (msg.includes('NODE_MODULE_VERSION') || msg.includes('was compiled against')) {
    return 'native binary built for a different Node.js version';
  }
  if (msg.includes('Cannot find module')) {
    return 'module is not installed';
  }
  return msg;
}

/**
 * Optional fallback for runtimes that predate Node.js 22.13. Not shipped in
 * the VSIX; present on dev machines and in source installs that ran
 * `npm install` in the extension folder.
 */
export const betterSqlite3Probe: SqliteEngineProbe = {
  name: 'better-sqlite3',
  attempt() {
    let ctor: BetterSqlite3Constructor;
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      ctor = require('better-sqlite3');
    } catch (err) {
      return { reason: describeLoadError(err) };
    }
    return {
      factory: {
        name: 'better-sqlite3',
        open(dbPath: string): SqliteEngine {
          const db = new ctor(dbPath, { readonly: true });
          applyPragmas((sql) => db.exec(sql));
          return {
            name: 'better-sqlite3',
            exec: (sql) => db.exec(sql),
            prepare(sql: string): SqliteStatement {
              const stmt = db.prepare(sql);
              return {
                all: (...params) => stmt.all(...params) as unknown[],
                get: (...params) => stmt.get(...params),
              };
            },
            close: () => db.close(),
          };
        },
      },
    };
  },
};

const defaultProbes: SqliteEngineProbe[] = [nodeSqliteProbe, betterSqlite3Probe];

/**
 * Open `dbPath` read-only with the first engine the runtime can supply.
 * Probes are injectable so tests can exercise fallback order and failures.
 */
export function openSqliteEngine(
  dbPath: string,
  probes: SqliteEngineProbe[] = defaultProbes,
): SqliteEngine {
  const reasons: string[] = [];
  for (const probe of probes) {
    const result = probe.attempt();
    if ('factory' in result) {
      const engine = result.factory.open(dbPath);
      console.log(
        `[code-review-graph] SQLite engine: ${engine.name} (Node ${process.versions.node})`,
      );
      return engine;
    }
    reasons.push(`${probe.name}: ${result.reason}`);
  }
  throw new SqliteUnavailableError(
    'Code Graph found no usable SQLite engine in this VS Code runtime. '
    + `The extension uses the built-in 'node:sqlite' module (Node.js 22.13+; `
    + `this runtime is Node ${process.versions.node}) with optional `
    + `'better-sqlite3' as a fallback. Tried -- ${reasons.join('; ')}. `
    + 'Update VS Code, or run "npm install" in the extension folder.',
  );
}
