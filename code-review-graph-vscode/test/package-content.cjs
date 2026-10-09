/**
 * Package-content check for an unpacked VSIX.
 *
 * The extension must not ship native binaries: the graph reader uses the
 * SQLite module built into the extension host, so `node_modules/**` and any
 * `*.node` file in the package are regressions that would reintroduce the
 * ABI-mismatch class of failures from issue #63. This check walks the
 * unpacked extension and fails on either, then prints the file count so a
 * trivially empty package cannot pass.
 *
 * Usage: node test/package-content.cjs <unpacked-extension-root>
 */

'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(process.argv[2] || '');
if (!process.argv[2]) {
  console.error('Usage: node test/package-content.cjs <unpacked-extension-root>');
  process.exit(2);
}

function walk(dir, files = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full, files);
    } else if (entry.isFile()) {
      files.push(path.relative(root, full));
    }
  }
  return files;
}

const required = ['package.json', 'dist/extension.js'];
for (const rel of required) {
  assert.ok(
    fs.existsSync(path.join(root, rel)),
    `packaged extension is missing ${rel}`,
  );
}

const files = walk(root);
const nodeModules = files.filter((f) => f.split(path.sep).includes('node_modules'));
assert.deepStrictEqual(
  nodeModules,
  [],
  `packaged extension must not ship node_modules: ${nodeModules.slice(0, 5).join(', ')}`,
);

const native = files.filter((f) => f.endsWith('.node'));
assert.deepStrictEqual(
  native,
  [],
  `packaged extension must not ship native binaries: ${native.join(', ')}`,
);

// An empty-ish package (manifest + bundle only) is fine; a package with no
// bundle is not, and that is covered above.
assert.ok(files.length >= 2, `unexpectedly empty package: ${files.length} files`);

console.log(
  `package content OK: ${files.length} files, no node_modules, no native binaries`,
);
