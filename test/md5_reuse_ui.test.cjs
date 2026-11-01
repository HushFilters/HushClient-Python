const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

function setup() {
  const elements = {};
  const requests = [];
  const context = {
    document: { getElementById: id => elements[id] ||= { addEventListener() {} } },
    window: { addEventListener() {} },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, text: async () => options?.body || '{"reuse_verified_md5":false}' };
    },
  };
  runInNewContext(readFileSync(join(__dirname, "../webui/ui-sync/app.js"), "utf8"), context);
  return { context, elements, requests };
}

test("verification loads disabled by default and saves enable/disable choices", async () => {
  const { context, elements, requests } = setup();
  await context.loadSyncSettings();
  assert.equal(elements["reuse-verified-md5"].checked, false);
  assert.equal(elements["reuse-verified-md5"].disabled, false);
  for (const enabled of [true, false]) {
    elements["reuse-verified-md5"].checked = enabled;
    await context.saveSyncSettings({ preventDefault() {} });
    assert.equal(requests.at(-1).url, "/sync/settings");
    assert.equal(requests.at(-1).options.method, "PUT");
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), { reuse_verified_md5: enabled });
    assert.match(elements["sync-settings-message"].textContent, /saved/);
  }
});

test("failed saves show the API error and allow retry", async () => {
  const { context, elements } = setup();
  await context.loadSyncSettings();
  context.fetch = async () => ({ ok: false, text: async () => '{"detail":"A filter operation is in progress"}' });
  await context.saveSyncSettings({ preventDefault() {} });
  assert.equal(elements["sync-settings-message"].textContent, "A filter operation is in progress");
  assert.equal(elements["sync-settings-message"].className, "status bad");
  assert.equal(elements["sync-settings-save"].disabled, false);
});
