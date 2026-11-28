const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const root = path.resolve(__dirname, '../..');
const validator = path.join(root, 'node_modules/renovate/dist/config-validator.js');
const script = JSON.parse(fs.readFileSync(path.join(root, 'package.json'))).scripts['validate:renovate'];
assert.equal(script, 'renovate-config-validator --strict --no-global');
function validate(files) {
  return spawnSync(process.execPath, [validator, '--strict', '--no-global', ...files], {
    cwd:root, encoding:'utf8', timeout:60000, env:{...process.env, RENOVATE_X_IGNORE_RE2:'true'},
  });
}
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wp7-validator-'));
try {
  const file = path.join(dir, 'invalid-preset.json');
  fs.writeFileSync(file, JSON.stringify({allowedCommands:['x']}));
  const invalid = validate([file]);
  assert.equal(invalid.error, undefined);
  assert.notEqual(invalid.status, 0, 'global-only option must fail in repo config');
  assert.match(invalid.stdout + invalid.stderr, /allowedCommands/);
  const files = ['renovate.json', 'renovate/default.json', 'renovate/instance.json'];
  const valid = validate(files);
  assert.equal(valid.status, 0, valid.stdout + valid.stderr);
  for (const file of files) assert.ok((valid.stdout + valid.stderr).includes(`Validating ${file} as repo config`), `${file} must validate as repo config`);
} finally { fs.rmSync(dir, {recursive:true, force:true}); }
console.log('PASS validator rejects global-only preset options and validates all presets as repo config');
