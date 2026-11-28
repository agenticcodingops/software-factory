// Read pins from each isolated scenario, so routine repository updates keep the fixture useful.
const fs = require('node:fs');
const path = require('node:path');
function bump(version, part) {
  if (!['patch', 'minor'].includes(part) || !/^v?\d+\.\d+\.\d+$/.test(version)) throw new Error(`invalid bump: ${version}, ${part}`);
  const prefix = version.startsWith('v') ? 'v' : '';
  const parts = version.replace(/^v/, '').split('.').map(Number);
  parts[part === 'patch' ? 2 : 1]++;
  if (part === 'minor') parts[2] = 0;
  return prefix + parts.join('.');
}
function readPins(dir) {
  const read = file => fs.readFileSync(path.join(dir, file), 'utf8');
  const match = (file, pattern) => {
    const result = read(file).match(pattern);
    if (!result) throw new Error(`${file}: pin pattern did not match`);
    return result;
  };
  const terraform = read('.terraform-version').trim();
  if (!/^\d+\.\d+\.\d+$/.test(terraform)) throw new Error('.terraform-version: invalid pin');
  const stacks = 'hosting/paperclip-hermes/terraform/stacks';
  const providers = {};
  for (const entry of fs.readdirSync(path.join(dir, stacks), {withFileTypes:true})) {
    if (!entry.isDirectory()) continue;
    const file = `${stacks}/${entry.name}/versions.tf`;
    const required = match(file, /required_version\s*=\s*"= (\d+\.\d+\.\d+)"/)[1];
    if (required !== terraform) throw new Error(`${file}: required_version differs from .terraform-version`);
    const text = read(file);
    const sources = [...text.matchAll(/source\s*=\s*"([^"/]+\/[^"/]+)"/g)];
    if (!sources.length) throw new Error(`${file}: no provider sources`);
    for (const source of sources) {
      const pin = text.slice(source.index).match(/^source\s*=\s*"([^"/]+\/[^"/]+)"\s+version\s*=\s*"= (\d+\.\d+\.\d+)"/);
      if (!pin) throw new Error(`${file}: provider pin pattern did not match`);
      const [, name, value] = pin;
      if (providers[name] && providers[name] !== value) throw new Error(`${file}: conflicting versions for ${name}`);
      providers[name] = value;
    }
  }
  if (!Object.keys(providers).length) throw new Error(`${stacks}: no provider pins`);
  const paper = match('hosting/paperclip-hermes/kit/compose.yaml', /ghcr\.io\/paperclipai\/paperclip:(v?)(\d{4}\.\d{3,4}\.\d+)@/);
  const hermes = match('hosting/paperclip-hermes/kit/Dockerfile.hermes', /^FROM nousresearch\/hermes-agent:(v0\.\d+\.\d+)@/m)[1];
  const actions = {};
  const workflow = '.github/workflows/release.yml';
  if (fs.existsSync(path.join(dir, workflow))) {
    for (const m of read(workflow).matchAll(/uses: (\S+?)@[0-9a-f]{40} # (v\d+\.\d+\.\d+)/g)) actions[m[1]] = m[2];
    if (!Object.keys(actions).length) throw new Error(`${workflow}: no action pins`);
  }
  return {terraform, providers, paperclip:{prefix:paper[1], version:paper[2]}, hermes, actions};
}
function next(pins) {
  return {
    terraform:bump(pins.terraform, 'patch'),
    providers:Object.fromEntries(Object.entries(pins.providers).map(([name, version]) => [name, bump(version, name === 'tailscale/tailscale' ? 'minor' : 'patch')])),
    paperclip:bump(pins.paperclip.version, 'minor'), hermes:bump(pins.hermes, 'patch'),
    actions:Object.fromEntries(Object.entries(pins.actions).map(([name, version]) => [name, bump(version, 'patch')])),
  };
}
module.exports = {bump, readPins, next};
