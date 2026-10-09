'use strict';

/**
 * Entry point used by @vscode/test-electron as `extensionTestsPath`.
 * Delegates to the compiled activation bundle (out/activation.test.cjs).
 */

const path = require('path');

const bundle = require(path.join(__dirname, '..', '..', 'out', 'activation.test.cjs'));

exports.run = bundle.run;
