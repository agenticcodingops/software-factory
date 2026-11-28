// Normalize acceptance fixtures independently of the live Paperclip tag prefix.
const assert = require('node:assert/strict');
function fixtureCompose(content, paperclip, scenario) {
  const live = `:${paperclip.prefix}${paperclip.version}@`;
  const bare = `:${paperclip.version}@`;
  const vpref = `:v${paperclip.version}@`;
  assert.ok(content.includes(live), 'live Paperclip pin must be present in compose.yaml');
  if (scenario === 'prefixed') {
    content = content.replace(live, vpref);
    assert.ok(content.includes(vpref));
    assert.ok(!content.includes(bare));
  } else {
    content = content.replace(live, bare);
    assert.ok(content.includes(bare));
  }
  return content;
}
module.exports = {fixtureCompose};
