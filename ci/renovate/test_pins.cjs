const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {readPins, bump} = require('./pins.cjs');
assert.equal(bump('1.17.2','patch'), '1.17.3');
assert.equal(bump('v0.23.4','patch'), 'v0.23.5');
assert.equal(bump('2026.1117.0','minor'), '2026.1118.0');
assert.equal(bump('0.30.0','minor'), '0.31.0');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wp7-pins-'));
try {
  fs.writeFileSync(path.join(dir, '.terraform-version'), '1.17.2\n');
  const stack = path.join(dir, 'hosting/paperclip-hermes/terraform/stacks/host');
  fs.mkdirSync(stack, {recursive:true});
  fs.writeFileSync(path.join(stack, 'versions.tf'), 'required_version = "= 1.17.3"\n');
  assert.throws(() => readPins(dir), /stacks\/host\/versions\.tf.*\.terraform-version/);
  fs.writeFileSync(path.join(dir, '.terraform-version'), 'wrong\n');
  assert.throws(() => readPins(dir), /\.terraform-version/);
} finally { fs.rmSync(dir, {recursive:true, force:true}); }
console.log('PASS pin reader and semantic version bumps');
