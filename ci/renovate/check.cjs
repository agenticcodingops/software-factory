#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
require('./test_pins.cjs');
const {readPins, bump, next} = require('./pins.cjs');
const root = path.resolve(__dirname, '../..');
const temporary = path.join(os.tmpdir(), `wp7-renovate-${require('node:crypto').randomBytes(6).toString('hex')}`);
fs.mkdirSync(temporary);
const cli = path.join(root, 'node_modules/renovate/dist/renovate.js');
const run = (command, args, cwd, env = {}) => {
  const result = spawnSync(command, args, {cwd, env: {...process.env, ...env}, encoding:'utf8', maxBuffer: 32 * 1024 * 1024, timeout:180000});
  assert.equal(result.status, 0, result.stdout + result.stderr);
  return result.stdout;
};
function write(dir, file, value) {
  fs.mkdirSync(path.dirname(path.join(dir, file)), {recursive: true});
  fs.writeFileSync(path.join(dir, file), value);
}
function cliRun(dir, name, preset) {
  // Promote only the final branch plans; full trace logs repeat the entire Terraform tree.
  const log = path.join(temporary, `${name}.jsonl`);
  const network = path.join(temporary, `${name}-network.json`);
  write(dir, 'renovate.json', JSON.stringify({extends:[`github>agenticcodingops/software-factory//renovate/${preset}.json#v1.2.3`],
    logLevelRemap:[{matchMessage:'/^branches$/',newLogLevel:'debug'}]}));
  run('git', ['init','-q'], dir);
  run('git', ['add','.'], dir);
  run(process.execPath, ['--require', path.join(__dirname,'registry.cjs'), cli, '--platform=local', '--dry-run=lookup'], dir, {
    WP7_SCENARIO: name, WP7_NETWORK_REPORT: network, LOG_LEVEL:'debug', LOG_FORMAT:'json',
    RENOVATE_LOG_FILE: log, RENOVATE_LOG_FILE_LEVEL:'debug', RENOVATE_CACHE_DIR:path.join(temporary, `cache-${name}`),
    RENOVATE_BASE_DIR: dir, RENOVATE_REPOSITORY_CACHE:'disabled', RENOVATE_REDIS_URL:'', RENOVATE_X_IGNORE_RE2:'true', GITHUB_COM_TOKEN:'fixture-placeholder',
  });
  const events = fs.readFileSync(log,'utf8').trim().split('\n').map(JSON.parse);
  assert.deepEqual(JSON.parse(fs.readFileSync(network)), [], 'unexpected network requests');
  assert.deepEqual(events.filter(e => e.level >= 40), [], 'Renovate warnings/errors');
  fs.writeFileSync(path.join(temporary, `${name}-events.json`), JSON.stringify(events,null,2));
  return events;
}
function extracted(events) {
  const result = events.find(e => e.msg === 'packageFiles with updates');
  assert.ok(result, 'missing CLI extraction output');
  return Object.entries(result.config).flatMap(([manager, files]) => files.flatMap(f =>
    f.deps.map(d => ({...d, manager, packageFile:f.packageFile}))));
}
function branches(events) {
  const result = events.find(e => e.msg === 'branches' && Array.isArray(e.branches));
  assert.ok(result, 'missing CLI branch plans');
  return result.branches;
}
function assertMergeLabels(plans) {
  for (const b of plans.filter(b => b.updateType === 'major')) {
    assert.ok((b.addLabels || []).includes('needs-operator'), `${b.branchName}: major without needs-operator`);
  }
  for (const b of plans) {
    const labels = b.addLabels || [];
    assert.ok(!(labels.includes('agent-merge') && labels.includes('needs-operator')), `${b.branchName}: conflicting merge labels`);
  }
}
function assertPublic(events, scenario, pins, want, saveDigest = true) {
  const deps = extracted(events);
  const plans = branches(events);
  assert.ok(plans.length > 0);
  assert.ok(plans.every(b => b.automerge === false), 'agents, not Renovate, perform merges');
  assert.ok(deps.every(d => !(d.warnings || []).length), 'dependency lookup warning');
  const paper = deps.find(d => d.depName === 'ghcr.io/paperclipai/paperclip' && d.packageFile.startsWith('hosting/'));
  const prefix = pins.paperclip.prefix;
  const paperVersion = scenario === 'paperclip-patch' ? bump(pins.paperclip.version, 'patch') : want.paperclip;
  const update = paper.updates.find(u => u.newValue === `${prefix}${paperVersion}`);
  assert.ok(update, 'Paperclip CalVer update must be proposed');
  assert.equal(update.newDigest, `sha256:${'b'.repeat(64)}`);
  assert.equal(Boolean(update.pendingChecks), ['young','missing'].includes(scenario));
  assert.ok(paper.updates.every(u => !/nightly|cloud|beta/.test(u.newValue)));
  for (const plan of plans.filter(b => b.depName === 'ghcr.io/paperclipai/paperclip' && b.updateType !== 'digest')) {
    assert.ok(!(plan.addLabels || []).includes('agent-merge'), 'Paperclip version bump must not carry agent-merge');
  }
  if (scenario === 'paperclip-patch') {
    assert.ok(plans.some(b => b.depName === 'ghcr.io/paperclipai/paperclip' && b.updateType === 'patch'), 'Paperclip patch must be proposed');
  }
  if (scenario === 'mature') {
    const hermes = deps.find(d => d.depName === 'nousresearch/hermes-agent' && d.packageFile.startsWith('hosting/'));
    assert.ok(hermes.updates.some(u => u.newValue === want.hermes));
    assert.ok(hermes.updates.every(u => !/2026|canary|rc|desktop/.test(u.newValue)));
    const postgres = deps.find(d => d.depName === 'postgres' && d.packageFile.startsWith('hosting/'));
    assert.ok(postgres.updates.length);
    assert.ok(postgres.updates.every(u => /^17(?:\.|$)/.test(u.newValue)));
    const imagePlans = plans.filter(b => ['docker-compose','dockerfile'].includes(b.manager));
    assert.ok(imagePlans.length >= 4);
    for (const b of imagePlans) {
      assert.match(b.commitMessage, /^fix\(deps\): /);
      if (['digest','patch'].includes(b.updateType) && (b.depName !== 'ghcr.io/paperclipai/paperclip' || b.updateType === 'digest')) assert.ok(b.addLabels.includes('agent-merge'));
      for (const u of b.upgrades) assert.match(u.newDigest, /^sha256:[a-f0-9]{64}$/);
    }
    const caddyMajor = imagePlans.find(b => b.depName === 'caddy' && b.updateType === 'major');
    assert.ok(caddyMajor, 'Caddy 2-alpine -> 3-alpine major must be proposed');
    assert.ok(caddyMajor.upgrades.every(u => u.newValue === '3-alpine'));
    assert.ok((caddyMajor.addLabels || []).includes('needs-operator'));
    assert.ok(!(caddyMajor.addLabels || []).includes('agent-merge'));
    assertMergeLabels(plans);
    console.log('PASS majors from every manager: needs-operator, never agent-merge');
    const provider = plans.find(b => b.depName === 'azurerm' && b.updateType === 'patch');
    assert.ok(provider.addLabels.includes('needs-operator'));
    assert.ok(!provider.addLabels.includes('agent-merge'));
    assert.ok(provider.upgrades.every(u => u.lockedVersion === pins.providers['hashicorp/azurerm']));
    assert.ok(provider.upgrades.some(u => u.packageFile.endsWith('stacks/host/versions.tf')));
    const avm = plans.filter(b => b.datasource === 'terraform-module');
    assert.ok(avm.length >= 5);
    for (const b of avm) {
      assert.equal(b.updateType,'minor');
      assert.equal(b.isBreaking,true, 'AVM 0.x minor is major-change risk');
      assert.ok(b.addLabels.includes('needs-operator'));
      assert.ok(b.upgrades.every(u => u.prBodyNotes.some(n => n.includes('treated as major'))));
    }
    const runtime = plans.find(b => b.branchName === 'renovate/terraform-runtime');
    assert.ok(runtime.addLabels.includes('agent-merge'));
    assert.equal(runtime.upgrades.length,4, 'customManager plus three exact stack requirements');
    assert.ok(runtime.upgrades.some(u => u.manager === 'regex' && u.packageFile === '.terraform-version'));
    assert.ok(runtime.upgrades.every(u => u.newVersion === want.terraform));
    const outside = deps.filter(d => d.packageFile.startsWith('docs/example/'));
    assert.ok(outside.some(d => d.manager === 'docker-compose'));
    assert.ok(outside.some(d => d.manager === 'dockerfile'));
    assert.ok(outside.some(d => d.manager === 'terraform'));
    assert.ok(outside.some(d => d.manager === 'npm'));
    assert.ok(outside.every(d => d.updates.length === 0));
    assert.ok(outside.filter(d => d.currentValue).every(d => d.skipReason === 'disabled'));
    assert.ok(plans.every(b => b.upgrades.every(u => !u.packageFile.startsWith('docs/example/'))));
    const tailscale = plans.find(b => b.depName === 'tailscale' && b.updateType === 'minor');
    assert.ok(tailscale.addLabels.includes('needs-operator'));
    assert.ok(!tailscale.addLabels.includes('agent-merge'));
    const range = deps.find(d => d.depName === 'tailscale' && d.packageFile.includes('/modules/'));
    const {api: versioning} = require('renovate/dist/modules/versioning/hashicorp/index.js');
    assert.ok(versioning.matches(tailscale.upgrades[0].newVersion, range.currentValue));
    const tooling = plans.find(b => b.groupSlug === 'validator-release-tooling');
    assert.ok(tooling, 'validator and release action updates must be proposed');
    for (const name of ['renovate', 'nock', 'release-please', 'googleapis/release-please-action']) {
      assert.ok(tooling.upgrades.some(u => u.depName === name), `${name} must share the tooling group`);
    }
    assert.ok(tooling.addLabels.includes('needs-operator'));
    assert.ok(!tooling.addLabels.includes('agent-merge'));
    assert.match(tooling.commitMessage, /^chore\(deps\): /);
    for (const b of plans.filter(b => !b.isLockFileMaintenance && b.upgrades.some(u => ['docker-compose', 'dockerfile', 'terraform', 'regex'].includes(u.manager)))) {
      assert.deepEqual(b.schedule, ['* 0-5 * * 1']);
    }
    console.log('PASS file scope, weekly schedule, compatible Tailscale minor and grouped validator/release tooling updates');
    const digest = imagePlans.find(b => b.depName === 'postgres' && b.updateType === 'digest');
    if (saveDigest) fs.writeFileSync(path.join(temporary,'digest-branch.json'), JSON.stringify(digest));
    console.log(`PASS Paperclip ${prefix}${want.paperclip} + digest after 3 days; Hermes ${want.hermes}, unstable tags rejected`);
    console.log('PASS AVM 0.x minor + provider patch: needs-operator, no automerge');
    console.log('PASS Compose/Dockerfile digest pins + fix(deps); PostgreSQL stays on 17');
    console.log(`PASS Terraform ${want.terraform} + stack pins; provider ${pins.providers['hashicorp/azurerm']} lock files extracted`);
  } else {
    console.log(`PASS Paperclip ${scenario}: ${update.pendingChecks ? 'held' : 'passed'}`);
  }
}
function assertInstance(events) {
  const deps = extracted(events);
  const native = deps.filter(d => d.manager === 'github-actions' && d.packageName === 'agenticcodingops/software-factory');
  assert.equal(native.length, 14);
  assert.ok(native.every(d => d.skipReason === 'disabled' && d.updates.length === 0));
  const workflows = deps.filter(d => d.manager === 'regex' && d.depType === 'workflow');
  assert.equal(workflows.length,14);
  const prod = workflows.filter(d => d.updates.length);
  assert.equal(prod.length,6);
  assert.ok(prod.every(d => !/ph-(ops|power|cost-guard|backup-verify)/.test(d.packageFile)));
  const ops = workflows.filter(d => /ph-(ops|power|cost-guard|backup-verify)/.test(d.packageFile));
  assert.equal(ops.length,8);
  assert.ok(ops.every(d => d.skipReason === 'disabled' && d.updates.length === 0));
  const plans = branches(events);
  assertMergeLabels(plans);
  const grouped = plans.filter(b => b.groupName === 'software-factory production workflows');
  assert.equal(grouped.length,2);
  const major = grouped.find(b => b.updateType === 'major');
  assert.ok(major.addLabels.includes('needs-operator'));
  assert.ok(!major.addLabels.includes('agent-merge'));
  assert.ok(major.upgrades.every(u => u.newValue === 'v1.0.0'));
  const minor = grouped.find(b => b.updateType === 'minor');
  assert.ok(minor.addLabels.includes('agent-merge'));
  assert.ok(!minor.addLabels.includes('needs-operator'));
  assert.equal(minor.upgrades.length,6);
  assert.ok(minor.upgrades.every(u => u.newDigest === 'b'.repeat(40) && u.newValue === 'v0.2.0'));
  assert.match(minor.commitMessage,/^chore\(deps\): /);
  const action = plans.find(b => b.depName === 'actions/checkout' && b.updateType === 'patch');
  assert.ok(action.addLabels.includes('agent-merge'));
  assert.equal(action.newDigest,'b'.repeat(40));
  const actionMajor = plans.find(b => b.depName === 'actions/checkout' && b.updateType === 'major');
  assert.ok(actionMajor.addLabels.includes('needs-operator'));
  assert.ok(!actionMajor.addLabels.includes('agent-merge'));
  console.log('PASS Actions and instance workflow majors require operator; patch/minor allow agent merge');
  console.log('PASS production workflow SHAs: six callers per update stream; eight per-target ops callers excluded');
  console.log('PASS SHA-pinned GitHub Actions remain chore(deps), agent-merge');
}
try {
  const defaults = JSON.parse(fs.readFileSync(path.join(root, 'renovate/default.json')));
  const {cronMatches} = require('renovate/dist/workers/repository/update/branch/schedule.js');
  const {DateTime} = require('luxon');
  for (const [time, expected] of [['2026-11-23T05:59:00Z', true], ['2026-11-23T06:00:00Z', false], ['2026-11-24T02:00:00Z', false]]) {
    assert.equal(defaults.schedule.some(cron => cronMatches(cron, DateTime.fromISO(time), defaults.timezone)), expected, `weekly schedule at ${time}`);
  }
  for (const scenario of ['mature', 'young', 'missing', 'prefixed', 'bumped', 'paperclip-patch']) {
    const dir = path.join(temporary,scenario); fs.mkdirSync(dir);
    const livePins = readPins(root);
    for (const file of ['hosting/paperclip-hermes/kit/compose.yaml','hosting/paperclip-hermes/kit/Dockerfile.hermes','.terraform-version']) {
      let content = fs.readFileSync(path.join(root,file),'utf8');
      if (scenario === 'prefixed' && file.endsWith('compose.yaml')) {
        const before = content;
        content = content.replace(`:${livePins.paperclip.prefix}${livePins.paperclip.version}@`, `:v${livePins.paperclip.version}@`);
        assert.notEqual(content, before, 'prefixed scenario must change the compose pin');
      }
      write(dir,file,content);
    }
    fs.cpSync(path.join(root,'hosting/paperclip-hermes/terraform'),path.join(dir,'hosting/paperclip-hermes/terraform'), {
      recursive:true, filter: p => !p.split(path.sep).includes('.terraform'),
    });
    if (['mature', 'bumped'].includes(scenario)) {
      for (const [file, source] of Object.entries({
        'docs/example/compose.yaml': 'hosting/paperclip-hermes/kit/compose.yaml',
        'docs/example/Dockerfile.hermes': 'hosting/paperclip-hermes/kit/Dockerfile.hermes',
        'docs/example/versions.tf': 'hosting/paperclip-hermes/terraform/stacks/tailnet/versions.tf',
        'package.json': 'package.json',
        'package-lock.json': 'package-lock.json',
        'docs/example/package.json': 'package.json',
        '.github/workflows/release.yml': '.github/workflows/release.yml',
      })) write(dir, file, fs.readFileSync(path.join(root, source), 'utf8'));
    }
    if (scenario === 'bumped') {
      const pins = readPins(dir), want = next(pins);
      const rewrite = (file, before, after) => {
        const content = fs.readFileSync(path.join(dir,file),'utf8');
        const changed = content.replaceAll(before, after);
        assert.notEqual(content, changed, `bumped rewrite did not change ${file}`);
        write(dir,file,changed);
      };
      rewrite('.terraform-version', pins.terraform, want.terraform);
      for (const stack of ['host','trust','tailnet']) {
        rewrite(`hosting/paperclip-hermes/terraform/stacks/${stack}/versions.tf`, `required_version = "= ${pins.terraform}"`, `required_version = "= ${want.terraform}"`);
      }
      for (const stack of ['host','trust']) {
        for (const file of ['versions.tf', '.terraform.lock.hcl']) {
          rewrite(`hosting/paperclip-hermes/terraform/stacks/${stack}/${file}`, pins.providers['hashicorp/azurerm'], want.providers['hashicorp/azurerm']);
        }
      }
      rewrite('hosting/paperclip-hermes/kit/compose.yaml', `:${pins.paperclip.prefix}${pins.paperclip.version}@`, `:${pins.paperclip.prefix}${want.paperclip}@`);
      rewrite('hosting/paperclip-hermes/kit/Dockerfile.hermes', `:${pins.hermes}@`, `:${want.hermes}@`);
      rewrite('.github/workflows/release.yml', `# ${pins.actions['googleapis/release-please-action']}`, `# ${want.actions['googleapis/release-please-action']}`);
    }
    const pins = readPins(dir), want = next(pins);
    const events = cliRun(dir,scenario,'default');
    assertPublic(events, scenario === 'bumped' ? 'mature' : scenario, pins, want, scenario !== 'bumped');
    if (scenario === 'bumped') console.log('PASS fixtures stay valid after pins advance (runtime, provider, Paperclip, Hermes, release action)');
  }
  const dir = path.join(temporary,'instance'); fs.mkdirSync(dir);
  for (const name of ['deploy','restore','vault-move','teardown','plan','tailnet-import','ops','power','cost-guard','backup-verify','ops-target-b','power-target-b','cost-guard-target-b','backup-verify-target-b']) {
    write(dir,`.github/workflows/ph-${name}.yml`, `name: fixture\non: workflow_dispatch\npermissions: {}\njobs:\n  call:\n    permissions: {}\n    uses: agenticcodingops/software-factory/.github/workflows/ph-${name.replace('-target-b','')}.yml@${'a'.repeat(40)} # v0.1.0\n`);
  }
  write(dir,'.github/workflows/ci.yml',`name: CI\non: push\npermissions: {}\njobs:\n  test:\n    permissions: {}\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: actions/checkout@${'a'.repeat(40)} # v7.0.1\n`);
  assertInstance(cliRun(dir,'instance','instance'));
  run(process.execPath, [path.join(__dirname,'release-please.cjs'), path.join(temporary,'digest-branch.json')], root);
  console.log('PASS merged fixture digest PR -> release-please release PR');
  fs.rmSync(temporary, {recursive:true, force:true});
} catch (error) {
  console.error(`Fixture failed; inspect ${temporary}`);
  throw error;
}
