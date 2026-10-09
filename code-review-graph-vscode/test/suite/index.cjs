'use strict';

/**
 * Entry point used by @vscode/test-electron as `extensionTestsPath`.
 * Runs the compiled test bundles (out/*.cjs) inside the VS Code test host.
 */

const path = require('path');
const Mocha = require('mocha');

exports.run = async function run() {
  const mocha = new Mocha({ ui: 'bdd', color: true, timeout: 60000 });
  mocha.addFile(path.join(__dirname, '..', '..', 'out', 'sqlite.test.cjs'));
  mocha.addFile(path.join(__dirname, '..', '..', 'out', 'activation.test.cjs'));

  await new Promise((resolve, reject) => {
    mocha.run((failures) => {
      if (failures > 0) {
        reject(new Error(`${failures} test(s) failed`));
      } else {
        resolve();
      }
    });
  });
};
