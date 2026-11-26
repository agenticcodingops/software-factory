// Exercise Release Please's real manifest and PR creation path at its SCM boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Manifest} = require('release-please');
const {setLogger} = require('release-please/build/src/util/logger');
const root = path.resolve(__dirname,'../..');
const branch = JSON.parse(fs.readFileSync(process.argv[2]));
assert.equal(branch.updateType,'digest');
assert.match(branch.commitMessage,/^fix\(deps\): /);
const quiet = {info(){},debug(){},warn(){},error(){},trace(){}};
setLogger(quiet);
async function prove(previous, expected) {
  const created = [];
  const base = 'd'.repeat(40);
  const merged = 'e'.repeat(40);
  const source = branch.upgrades[0].packageFile;
  const pr = {number:7,title:branch.prTitle,body:'',labels:[],files:[source],
    headBranchName:branch.branchName,baseBranchName:'main',mergeCommitSha:merged};
  const scm = {
    repository: {owner:'agenticcodingops',repo:'software-factory',defaultBranch:'main'},
    async getFileJson(file) {
      if (file === '.release-please-manifest.json') return {'.':previous};
      return JSON.parse(fs.readFileSync(path.join(root,file)));
    },
    async getFileContentsOnBranch(file) {
      const content = file === 'version.txt' ? previous + '\n' : fs.readFileSync(path.join(root,file),'utf8');
      return {content:Buffer.from(content).toString('base64'),parsedContent:content,sha:base};
    },
    async *releaseIterator() {
      if (previous !== '0.0.0') yield {id:1,tagName:`v${previous}`,sha:base,url:'https://github.com/agenticcodingops/software-factory/releases'};
    },
    async *tagIterator() {},
    async *mergeCommitIterator() {
      yield {sha:merged,message:branch.commitMessage,files:[source],pullRequest:pr};
      if (previous !== '0.0.0') yield {sha:base,message:`chore(main): release ${previous}`,files:['version.txt'],
        pullRequest:{...pr,number:6,labels:['autorelease: tagged'],mergeCommitSha:base}};
    },
    async *pullRequestIterator() {},
    async createPullRequest(pull, target, message, updates) {
      created.push({pull,target,message,updates}); return {...pull,number:8};
    },
  };
  const manifest = await Manifest.fromManifest(scm,'main',undefined,undefined,{logger:quiet});
  const prs = await manifest.createPullRequests();
  assert.equal(prs.length,1);
  assert.equal(created.length,1);
  const candidate = created[0];
  assert.equal(candidate.target,'main');
  assert.ok(candidate.pull.labels.includes('autorelease: pending'));
  assert.match(candidate.pull.title, new RegExp(expected.replaceAll('.', '\\.')));
  assert.ok(candidate.pull.body.includes('postgres'));
  for (const file of ['CHANGELOG.md','version.txt','.release-please-manifest.json']) {
    const update = candidate.updates.find(u => u.path === file);
    assert.ok(update, `missing release update ${file}`);
    const content = file === '.release-please-manifest.json' ? JSON.stringify({'.':previous}) : file === 'version.txt' ? previous : '';
    assert.ok(update.updater.updateContent(content).includes(expected));
  }
}
(async () => {
  await prove('0.0.0','0.1.0');
  await prove('0.1.0','0.1.1');
})().catch(error => {console.error(error);process.exitCode=1;});
