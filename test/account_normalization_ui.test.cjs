const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");
const { webcrypto } = require("node:crypto");

test("browser submits normalized digests without changing the password", async () => {
  const source = readFileSync(join(__dirname, "../webui/ui-check/app.js"), "utf8");
  const vectors = JSON.parse(readFileSync(join(__dirname, "data/hashes.json"), "utf8"));
  for (const vector of vectors) {
    const elements = {
      "check-form": { username: { value: vector.username }, password: { value: vector.password } },
      "submit-btn": {}, "hash-output": {}, "api-output": {}, "status": {},
    };
    const requests = [];
    const context = {
      crypto: webcrypto, TextEncoder,
      document: { getElementById: (id) => elements[id] },
      window: { addEventListener() {} },
      fetch: async (url, options) => {
        requests.push({ url, body: JSON.parse(options.body) });
        return { ok: true, json: async () => ({ found: true }) };
      },
    };
    runInNewContext(source, context);
    await context.submitCheck({ preventDefault() {} });
    assert.deepEqual(requests, [{ url: "/checkhash", body: { hash: vector.hash } }]);
    assert.equal(elements["hash-output"].textContent, vector.hash);
    assert.equal(elements["check-form"].username.value, vector.username);
    assert.equal(elements["check-form"].password.value, vector.password);
  }
});
