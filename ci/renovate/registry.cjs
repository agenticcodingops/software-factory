// Network-only fixture: the real CLI, managers, rules and branch planner run unchanged.
const nock = require('nock');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const scenario = process.env.WP7_SCENARIO;
const {readPins, bump, next} = require('./pins.cjs');
const pins = scenario === 'instance' ? null : readPins(process.cwd());
const want = pins && next(pins);
const now = Date.now();
const age = (days) => new Date(now - days * 86400000).toISOString();
const digest = (letter) => `sha256:${letter.repeat(64)}`;
const old = 'a'.repeat(40);
const head = 'b'.repeat(40);
nock.disableNetConnect();
const defaults = JSON.parse(fs.readFileSync(`${root}/renovate/default.json`));
const instance = JSON.parse(fs.readFileSync(`${root}/renovate/instance.json`));
// Fetch the committed presets exactly as an adopter does, at one reviewed tag.
// No parent rewriting: the real resolver must carry the tag through ./default.
for (const [name, preset] of Object.entries({default: defaults, instance})) {
  nock('https://api.github.com').persist()
    .get(`/repos/agenticcodingops/software-factory/contents/renovate/${name}.json`)
    .query({ref: 'v1.2.3'})
    .reply(200, {content: Buffer.from(JSON.stringify(preset)).toString('base64')});
}

if (pins) {
const paperVersion = scenario === 'paperclip-patch' ? bump(pins.paperclip.version, 'patch') : want.paperclip;
const paperTag = (pins.paperclip.prefix ? 'v' : '') + paperVersion;
const packages = [
  { name: digest('a'), created_at: age(20), metadata: { container: { tags: [pins.paperclip.version, 'v' + pins.paperclip.version] } } },
  { name: digest('b'), created_at: scenario === 'missing' ? null : age(scenario === 'young' ? 2 : 3.1),
    metadata: { container: { tags: [paperTag, 'nightly', want.paperclip + '-cloud', want.paperclip + '-beta'] } } },
];
nock('https://api.github.com').persist()
  .matchHeader('authorization', /^(?:Bearer|token) fixture-placeholder$/i)
  .get('/orgs/paperclipai/packages/container/paperclip/versions').query({per_page: 100}).reply(200, packages);
nock('https://releases.hashicorp.com').persist().get('/terraform/index.json').reply(200, {
  versions: {[pins.terraform]: {}, [want.terraform]: {}},
});
}

nock('https://registry.terraform.io').persist()
  .get('/.well-known/terraform.json').reply(200, {'modules.v1': '/v1/modules/', 'providers.v1': '/v1/providers/'});
for (const [name, version] of Object.entries(pins?.providers || {})) {
  nock('https://registry.terraform.io').persist().get(`/v2/providers/${name.toLowerCase()}`).query({include: 'provider-versions'}).reply(200, {
    data: {attributes: {}},
    included: [version, want.providers[name]].map(version => ({type: 'provider-versions', attributes: {version, 'published-at': age(10)}})),
  });
}
// Registry fixture responds to every AVM pinned in the real Terraform tree.
function walk(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, {withFileTypes: true}).flatMap(e =>
    e.isDirectory() && e.name !== '.terraform' ? walk(path.join(dir, e.name)) : e.isFile() && e.name.endsWith('.tf') ? [path.join(dir, e.name)] : []);
}
const modules = new Map();
for (const file of walk(`${process.cwd()}/hosting/paperclip-hermes/terraform`)) {
  for (const m of fs.readFileSync(file, 'utf8').matchAll(/source\s*=\s*"(Azure\/avm-[^"]+)"\s+version\s*=\s*"([\d.]+)"/g)) {
    modules.set(m[1], m[2]);
  }
}
for (const [name, version] of modules) {
  const parts = version.split('.').map(Number); parts[1]++; parts[2] = 0;
  nock('https://registry.terraform.io').persist().get(`/v2/modules/${name}`).query({include: 'module-versions'}).reply(200, {
    data: {attributes: {}},
    included: [version, parts.join('.')].map(version => ({type: 'module-versions', attributes: {version, 'published-at': age(10)}})),
  });
}

// A PostgreSQL major move is a policy change and should require a test edit.
const images = {...(pins ? {'nousresearch/hermes-agent': [pins.hermes, want.hermes, 'v2026.9.24', 'v0.99.0-canary', bump(pins.hermes, 'minor') + '-rc.1', bump(pins.hermes, 'minor') + '-desktop']} : {}),
  'library/postgres': ['17','17.1','18'], 'library/caddy': ['2-alpine','2.1-alpine','3-alpine']};
for (const [name, tags] of Object.entries(images)) {
  nock('https://hub.docker.com').persist().get(`/v2/repositories/${name}/tags`).query(true).reply(200, {
    count: tags.length, next: null, results: tags.map((name, id) => ({name, id, last_updated: age(10), tag_last_pushed: age(10), digest: digest('c')})),
  });
}
// Source labels are optional; image tag/digest/timestamps come from the Hub response.
nock('https://index.docker.io').persist().get('/v2/').reply(200, {});
nock('https://index.docker.io').persist().get(/\/manifests\//).reply(404);
nock('https://index.docker.io').persist().head(/\/manifests\//).reply(200);

nock('https://api.github.com').persist().post('/graphql').reply((uri, body) => {
  const query = body.query;
  const repo = query.match(/repository\(owner: "([^"]+)", name: "([^"]+)"/);
  const name = repo ? repo[2] : body.variables?.name;
  const owner = repo ? repo[1] : body.variables?.owner;
  const action = `${owner}/${name}`;
  const versions = name === 'node-versions' ? ['24.22.3', '24.22.4'] : pins?.actions[action] ? [pins.actions[action], want.actions[action], ...(action === 'actions/checkout' ? [`v${Number(pins.actions[action].slice(1).split('.')[0]) + 1}.0.0`] : [])] : name === 'software-factory' ? ['v0.1.0', 'v0.2.0', 'v1.0.0'] : ['v7.0.1','v7.0.2','v8.0.0'];
  const tags = versions.map((version, i) => ({version, target: {type: 'Commit', oid: i ? head : old, releaseTimestamp: age(10)}}));
  const releases = versions.map((version, id) => ({version, releaseTimestamp: age(10), isDraft: false, isPrerelease: false,
    url: `https://github.com/fixture/repo/releases/tag/${version}`, id: id + 1, name: version, description: 'Routine fixes.'}));
  return [200, {data: {repository: {isRepoPrivate: false, payload: {pageInfo: {hasNextPage: false, endCursor: null}, nodes: query.includes('refs(') ? tags : releases}}}}];
});
const packageFile = path.join(process.cwd(), 'package.json');
const devDependencies = fs.existsSync(packageFile) ? JSON.parse(fs.readFileSync(packageFile)).devDependencies : {};
for (const [name, version] of Object.entries(devDependencies)) {
  const parts = version.split('.').map(Number); parts[2]++;
  const versions = [version, parts.join('.')];
  nock('https://registry.npmjs.org').persist().get(`/${name}`).reply(200, {
    name, 'dist-tags': {latest: versions[1]},
    time: Object.fromEntries(versions.map(v => [v, age(10)])),
    versions: Object.fromEntries(versions.map(v => [v, {name, version: v}])),
  });
}
process.on('exit', () => {
  // Unmatched requests fail the fixture even if Renovate treats an HTTP error as recoverable.
  fs.writeFileSync(process.env.WP7_NETWORK_REPORT, JSON.stringify(unmatched));
});
const unmatched = [];
nock.emitter.on('no match', (request) => unmatched.push(`${request.method} ${request.options?.href || request.path}`));
