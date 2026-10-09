/**
 * Activation test run inside a real VS Code extension host (issue #218).
 *
 * The runner (test/run-activation.mjs) prepares a workspace containing a
 * small `.code-review-graph/graph.db` and opens it as the test folder.
 * `run()` must throw when a check fails.
 */

import * as assert from 'assert';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { SqliteReader } from '../src/backend/sqlite';

const EXTENSION_ID = 'tirth8205.code-review-graph';

function workspaceRoot(): string {
  const folder = vscode.workspace.workspaceFolders?.[0];
  assert.ok(folder, 'test workspace folder is open');
  return folder.uri.fsPath;
}

export async function run(): Promise<void> {
  // 1. The extension activates and its commands are registered.
  const ext = vscode.extensions.getExtension(EXTENSION_ID);
  assert.ok(ext, `extension ${EXTENSION_ID} is installed`);
  await ext.activate();
  assert.strictEqual(ext.isActive, true, 'extension activated');

  const commands = await vscode.commands.getCommands(true);
  for (const id of [
    'codeReviewGraph.buildGraph',
    'codeReviewGraph.updateGraph',
    'codeReviewGraph.codeGraph.refresh',
  ]) {
    assert.ok(commands.includes(id), `command is registered: ${id}`);
  }

  // 2. Refresh executes (regression: it was executed but never registered).
  await vscode.commands.executeCommand('codeReviewGraph.codeGraph.refresh');

  // 3. The workspace graph is readable through the extension host runtime.
  const dbPath = path.join(workspaceRoot(), '.code-review-graph', 'graph.db');
  assert.ok(fs.existsSync(dbPath), 'fixture graph.db exists');

  const reader = new SqliteReader(dbPath);
  try {
    assert.strictEqual(reader.isValid(), true);
    assert.deepStrictEqual(reader.getAllFiles(), ['src/app.py']);
    assert.strictEqual(reader.getNode('app.main')?.name, 'main');
    assert.ok(reader.getStats().totalNodes >= 2);
  } finally {
    reader.close();
  }

  console.log(
    'PASS: activation, command registration (incl. refresh) and graph reads are healthy.'
  );
}
