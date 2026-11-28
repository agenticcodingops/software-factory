const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {fixtureCompose} = require('./fixture-compose.cjs');
const {readPins} = require('./pins.cjs');
const root = path.resolve(__dirname, '../..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'wp7-fixture-prefix-'));
const compose = 'hosting/paperclip-hermes/kit/compose.yaml';
try {
  for (const file of [compose, 'hosting/paperclip-hermes/kit/Dockerfile.hermes', '.terraform-version']) {
    fs.mkdirSync(path.dirname(path.join(temp, file)), {recursive:true});
    fs.copyFileSync(path.join(root, file), path.join(temp, file));
  }
  fs.cpSync(path.join(root, 'hosting/paperclip-hermes/terraform'), path.join(temp, 'hosting/paperclip-hermes/terraform'), {
    recursive:true, filter: p => !p.split(path.sep).includes('.terraform'),
  });
  const original = fs.readFileSync(path.join(root, compose), 'utf8');
  const live = readPins(root).paperclip;
  for (const prefix of ['v', '']) {
    const source = original.replace(`:${live.prefix}${live.version}@`, `:${prefix}${live.version}@`);
    fs.writeFileSync(path.join(temp, compose), source);
    const pins = readPins(temp).paperclip;
    assert.equal(pins.prefix, prefix);
    for (const scenario of ['prefixed', 'mature', 'young', 'missing', 'bumped', 'paperclip-patch']) {
      fs.writeFileSync(path.join(temp, compose), fixtureCompose(source, pins, scenario));
      assert.equal(readPins(temp).paperclip.prefix, scenario === 'prefixed' ? 'v' : '', `${prefix}/${scenario}`);
    }
    assert.throws(() => fixtureCompose('services: {}', pins, 'mature'), /live Paperclip pin/);
  }
  console.log('PASS fixture prefixes: bare and prefixed source pins cover every scenario; missing pins fail closed');
} finally {
  fs.rmSync(temp, {recursive:true, force:true});
}
