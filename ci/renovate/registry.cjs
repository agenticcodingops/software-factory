// Network-only fixture: the real CLI, managers, rules and branch planner run unchanged.
const nock = require('nock');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const scenario = process.env.WP7_SCENARIO;
const now = Date.now();
const age = (days) => new Date(now - days * 86400000).toISOString();
const digest = (letter) => `sha256:${letter.repeat(64)}`;
const old = 'a'.repeat(40);
const head = 'b'.repeat(40);
nock.disableNetConnect();
const defaults = JSON.parse(fs.readFileSync(`${root}/renovate/default.json`));
const instance = JSON.parse(fs.readFileSync(`${root}/renovate/instance.json`));
instance.extends = instance.extends.map(ref => {
  const prefix = 'github>agenticcodingops/software-factory//';
  assert.ok(ref.startsWith(prefix), 'instance must inherit this repository policy');
  const relative = ref.slice(prefix.length);
  const parent = JSON.parse(fs.readFileSync(path.join(root, relative)));
  nock('http://preset.test').persist().get(`/${relative}`).reply(200, parent);
  return `http://preset.test/${relative}`;
});
nock('http://preset.test').persist().get('/default.json').reply(200, defaults);
nock('http://preset.test').persist().get('/instance.json').reply(200, instance);

const paperTag = scenario === 'prefixed' ? 'v2026.1124.0' : '2026.1124.0';
const packages = [
  { name: digest('a'), created_at: age(20), metadata: { container: { tags: ['2026.1117.0', 'v2026.1117.0'] } } },
  { name: digest('b'), created_at: scenario === 'missing' ? null : age(scenario === 'young' ? 2 : 3.001),
    metadata: { container: { tags: [paperTag, 'nightly', '2026.1124.0-cloud', '2026.1124.0-beta'] } } },
];
nock('https://api.github.com').persist()
  .matchHeader('authorization', /^(?:Bearer|token) fixture-placeholder$/i)
  .get('/orgs/paperclipai/packages/container/paperclip/versions').query({per_page: 100}).reply(200, packages);
nock('https://releases.hashicorp.com').persist().get('/terraform/index.json').reply(200, {
  versions: { '1.17.2': {}, '1.17.3': {} },
});

nock('https://registry.terraform.io').persist()
  .get('/.well-known/terraform.json').reply(200, {'modules.v1': '/v1/modules/', 'providers.v1': '/v1/providers/'});
const providers = {'hashicorp/azurerm': '4.81.0', 'Azure/azapi': '2.14.0', 'hashicorp/random': '3.9.1',
  'hashicorp/time': '0.14.2', 'hashicorp/tls': '4.4.2', 'tailscale/tailscale': '0.30.0'};
for (const [name, version] of Object.entries(providers)) {
  const parts = version.split('.').map(Number); parts[2]++;
  nock('https://registry.terraform.io').persist().get(`/v1/providers/${name.toLowerCase()}`).reply(200, {
    versions: [version, parts.join('.')], version: parts.join('.'), published_at: age(10),
  });
}
// Registry fixture responds to every AVM pinned in the real Terraform tree.
function walk(dir) {
  return fs.readdirSync(dir, {withFileTypes: true}).flatMap(e =>
    e.isDirectory() && e.name !== '.terraform' ? walk(path.join(dir, e.name)) : e.isFile() && e.name.endsWith('.tf') ? [path.join(dir, e.name)] : []);
}
const modules = new Map();
for (const file of walk(`${root}/hosting/paperclip-hermes/terraform`)) {
  for (const m of fs.readFileSync(file, 'utf8').matchAll(/source\s*=\s*"(Azure\/avm-[^"]+)"\s+version\s*=\s*"([\d.]+)"/g)) {
    modules.set(m[1], m[2]);
  }
}
for (const [name, version] of modules) {
  const [namespace, mod, provider] = name.split('/');
  const parts = version.split('.').map(Number); parts[1]++; parts[2] = 0;
  nock('https://registry.terraform.io').persist().get(`/v1/modules/${name}`).reply(200, {
    namespace, name: mod, provider, versions: [version, parts.join('.')], version: parts.join('.'), published_at: age(10),
  });
}

const images = {'nousresearch/hermes-agent': ['v0.23.4','v0.23.5','v2026.9.24','v0.99.0-canary','v0.24.0-rc.1','v0.24.0-desktop'],
  'library/postgres': ['17','17.1','18'], 'library/caddy': ['2-alpine','2.1-alpine']};
for (const [name, tags] of Object.entries(images)) {
  nock('https://hub.docker.com').persist().get(`/v2/repositories/${name}/tags`).query(true).reply(200, {
    count: tags.length, next: null, results: tags.map((name, id) => ({name, id, last_updated: age(10), tag_last_pushed: age(10), digest: digest('c')})),
  });
}
// Source labels are optional; image tag/digest/timestamps come from the Hub response.
nock('https://index.docker.io').persist().get('/v2/').reply(200, {});
nock('https://index.docker.io').persist().get(/\/manifests\//).reply(404);

nock('https://api.github.com').persist().post('/graphql').reply((uri, body) => {
  const query = body.query;
  const repo = query.match(/repository\(owner: "([^"]+)", name: "([^"]+)"/);
  const name = repo ? repo[2] : body.variables?.name;
  const versions = name === 'software-factory' ? ['v0.1.0', 'v0.2.0'] : ['v7.0.1','v7.0.2'];
  const tags = versions.map((version, i) => ({version, target: {type: 'Commit', oid: i ? head : old, releaseTimestamp: age(10)}}));
  const releases = versions.map((version, id) => ({version, releaseTimestamp: age(10), isDraft: false, isPrerelease: false,
    url: `https://github.com/fixture/repo/releases/tag/${version}`, id: id + 1, name: version, description: 'Routine fixes.'}));
  return [200, {data: {repository: {isRepoPrivate: false, payload: {pageInfo: {hasNextPage: false, endCursor: null}, nodes: query.includes('refs(') ? tags : releases}}}}];
});
process.on('exit', () => {
  // Unmatched requests fail the fixture even if Renovate treats an HTTP error as recoverable.
  fs.writeFileSync(process.env.WP7_NETWORK_REPORT, JSON.stringify(unmatched));
});
const unmatched = [];
nock.emitter.on('no match', (request) => unmatched.push(`${request.method} ${request.options?.href || request.path}`));
