/**
 * Minimal `vscode` module stub for tests that load `dist/extension.js`
 * under plain Node (the activation smoke test and the reader unit tests).
 *
 * It records command registrations, tree providers, quick picks and messages
 * so the tests can assert on extension behavior without the real editor.
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');

class Disposable {
  dispose() {}
}

class EventEmitter {
  constructor() {
    this.event = () => new Disposable();
  }
  fire() {}
  dispose() {}
}

class Uri {
  constructor(fsPath) {
    this.fsPath = fsPath;
    this.scheme = 'file';
    this.path = fsPath;
  }
  static file(p) {
    return new Uri(p);
  }
  static parse(p) {
    return new Uri(p);
  }
  static joinPath(base, ...parts) {
    return new Uri(path.join(base.fsPath, ...parts));
  }
  toString() {
    return `file://${this.fsPath}`;
  }
}

class Range {
  constructor(startLine, startChar, endLine, endChar) {
    this.start = { line: startLine, character: startChar };
    this.end = { line: endLine, character: endChar };
  }
}

class TreeItem {
  constructor(label, collapsibleState) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
}

class ThemeIcon {
  constructor(id) {
    this.id = id;
  }
}

/**
 * Build the stub API. `workspaceRoot` becomes the single workspace folder.
 * The returned object has a `state` record of everything the extension did.
 */
function createVscodeStub(workspaceRoot) {
  const state = {
    registeredCommands: new Map(),
    treeDataProviders: [],
    fileDecorationProviders: [],
    errorMessages: [],
    warningMessages: [],
    infoMessages: [],
    quickPicks: [],
  };

  const vscode = {
    Disposable,
    EventEmitter,
    Uri,
    Range,
    TreeItem,
    ThemeIcon,
    TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
    ProgressLocation: { SourceControl: 1, Window: 10, Notification: 15 },
    StatusBarAlignment: { Left: 1, Right: 2 },
    FileDecoration: class {},
    state,
    workspace: {
      workspaceFolders: [
        { uri: Uri.file(workspaceRoot), name: path.basename(workspaceRoot), index: 0 },
      ],
      getConfiguration: () => ({ get: (_key, fallback) => fallback }),
      onDidSaveTextDocument: () => new Disposable(),
      createFileSystemWatcher: () => ({
        onDidChange: () => new Disposable(),
        onDidCreate: () => new Disposable(),
        onDidDelete: () => new Disposable(),
        dispose() {},
      }),
      openTextDocument: async () => ({}),
      registerTreeDataProvider: (id, provider) => {
        state.treeDataProviders.push({ id, provider });
        return new Disposable();
      },
      fs: {
        stat: async (uri) => {
          fs.statSync(uri.fsPath);
          return {};
        },
      },
    },
    window: {
      showErrorMessage: (message) => {
        state.errorMessages.push(message);
        return Promise.resolve(undefined);
      },
      showWarningMessage: (message) => {
        state.warningMessages.push(message);
        return Promise.resolve(undefined);
      },
      showInformationMessage: (message) => {
        state.infoMessages.push(message);
        return Promise.resolve(undefined);
      },
      showInputBox: async () => undefined,
      showQuickPick: async (items) => {
        state.quickPicks.push(items);
        return undefined;
      },
      withProgress: async (_options, task) => task(),
      createOutputChannel: () => ({ appendLine() {}, show() {} }),
      createTerminal: () => ({ show() {}, sendText() {} }),
      createStatusBarItem: () => ({
        text: '',
        tooltip: '',
        command: '',
        show() {},
        hide() {},
        dispose() {},
      }),
      registerTreeDataProvider: (id, provider) => {
        state.treeDataProviders.push({ id, provider });
        return new Disposable();
      },
      registerFileDecorationProvider: (provider) => {
        state.fileDecorationProviders.push(provider);
        return new Disposable();
      },
    },
    commands: {
      registerCommand: (id, handler) => {
        state.registeredCommands.set(id, handler);
        return new Disposable();
      },
      executeCommand: async () => undefined,
    },
    env: {
      openExternal: async () => true,
    },
  };

  return vscode;
}

/**
 * Patch `require('vscode')` to return the stub, then load `bundlePath`.
 *
 * `blockedModules` makes listed require() calls fail for as long as the patch
 * is installed, simulating runtimes the extension must survive: `node:sqlite`
 * absent (Node < 22.13) or `better-sqlite3` missing / ABI-mismatched. The
 * probe requires happen inside `activate()`, so keep the patch installed
 * until the test is done and then call `restore()`.
 *
 * Returns the loaded extension module, the stub, the recorded state and
 * `restore`.
 */
function loadExtensionWithStub(bundlePath, workspaceRoot, options = {}) {
  const blocked = new Set(options.blockedModules ?? []);
  const vscode = createVscodeStub(workspaceRoot);
  const originalLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === 'vscode') {
      return vscode;
    }
    if (blocked.has(request)) {
      const err = new Error(
        request === 'node:sqlite'
          ? 'No such built-in module: node:sqlite'
          : `Cannot find module '${request}'`,
      );
      err.code = request === 'node:sqlite' ? 'ERR_UNKNOWN_BUILTIN_MODULE' : 'MODULE_NOT_FOUND';
      throw err;
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  const restore = () => {
    Module._load = originalLoad;
  };
  const extension = require(bundlePath);
  return { extension, vscode, state: vscode.state, restore };
}

module.exports = { createVscodeStub, loadExtensionWithStub };
