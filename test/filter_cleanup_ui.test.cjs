const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

function setup() {
  const elements = Object.fromEntries([
    "cleanup-mode", "cleanup-days", "cleanup-days-field", "cleanup-save", "cleanup-message",
    "cleanup-count", "cleanup-size", "cleanup-checked-at", "cleanup-delete", "cleanup-files-message", "cleanup-files-refresh",
  ].map(id => [id, {}]));
  elements["history-toggle"] = { addEventListener() {} };
  const requests = [];
  const context = {
    document: { getElementById: id => elements[id] },
    window: { addEventListener() {} },
    fetch: async (url, options) => {
      const body = JSON.parse(options.body);
      requests.push({ url, body });
      return { ok: true, text: async () => JSON.stringify(body) };
    },
  };
  runInNewContext(readFileSync(join(__dirname, "../webui/ui-sync/app.js"), "utf8"), context);
  return { context, elements, requests };
}

test("cleanup loads the default and saves all three deletion choices", async () => {
  const { context, elements, requests } = setup();
  context.showCleanupSettings({ enabled: true, retention_days: 31 });
  assert.equal(elements["cleanup-mode"].value, "retained");
  assert.equal(elements["cleanup-days"].value, 31);
  for (const [mode, days, expected] of [
    ["retained", "7", { enabled: true, retention_days: 7 }],
    ["immediate", "7", { enabled: true, retention_days: 0 }],
    ["off", "31", { enabled: false, retention_days: 31 }],
  ]) {
    elements["cleanup-mode"].value = mode;
    elements["cleanup-days"].value = days;
    context.syncCleanupDaysAvailability();
    assert.equal(elements["cleanup-days"].disabled, mode !== "retained");
    assert.equal(elements["cleanup-days-field"].hidden, mode !== "retained");
    await context.saveCleanupSettings({ preventDefault() {} });
    assert.deepEqual(requests.at(-1), { url: "/sync/cleanup", body: expected });
    assert.match(elements["cleanup-message"].textContent, /saved/);
  }
});

test("invalid retention never submits and failed saves remain visible", async () => {
  const { context, elements, requests } = setup();
  elements["cleanup-mode"].value = "retained";
  for (const days of ["", "0", "32", "1.5"]) {
    elements["cleanup-days"].value = days;
    await context.saveCleanupSettings({ preventDefault() {} });
    assert.equal(requests.length, 0);
    assert.match(elements["cleanup-message"].textContent, /whole number/);
  }
  elements["cleanup-days"].value = "7";
  context.fetch = async () => ({ ok: false, text: async () => JSON.stringify({ detail: "Could not save settings" }) });
  await context.saveCleanupSettings({ preventDefault() {} });
  assert.equal(elements["cleanup-message"].className, "status bad");
  assert.equal(elements["cleanup-message"].textContent, "Could not save settings");
  assert.equal(elements["cleanup-save"].disabled, false);
});

test("manual deletion shows fresh totals and cancellation never sends a delete", async () => {
  const { context, elements } = setup();
  const requests = [];
  context.fetch = async url => {
    requests.push(url);
    return { ok: true, text: async () => JSON.stringify({ active: false, inventory: { filter_count: 2, total_bytes: 4096, confirmation_token: "snapshot" } }) };
  };
  let prompt;
  context.window.confirm = text => { prompt = text; return false; };
  await context.deleteSupersededFilters();
  assert.deepEqual(requests, ["/sync/cleanup/files/refresh"]);
  assert.match(prompt, /Are you sure.*2 superseded filter files.*4.00 KiB/);
  assert.equal(elements["cleanup-count"].textContent, "2");
  assert.match(elements["cleanup-size"].textContent, /4.00 KiB/);
  assert.equal(elements["cleanup-delete"].disabled, false);
});

test("confirmation sends the snapshot token and updates totals after deletion", async () => {
  const { context, elements } = setup();
  const requests = [];
  context.window.confirm = () => true;
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    const result = url.endsWith("/delete")
      ? { filter_count: 0, total_bytes: 0, deleted_count: 2, deleted_bytes: 4096 }
      : { active: false, inventory: url.endsWith("/status")
        ? { filter_count: 0, total_bytes: 0, confirmation_token: "empty" }
        : { filter_count: 2, total_bytes: 4096, confirmation_token: "snapshot" } };
    return { ok: true, text: async () => JSON.stringify(result) };
  };
  await context.deleteSupersededFilters();
  assert.equal(requests[1].url, "/sync/cleanup/delete");
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), { confirmation_token: "snapshot" });
  assert.equal(elements["cleanup-delete"].disabled, true);
  assert.equal(elements["cleanup-count"].textContent, "0");
  assert.match(elements["cleanup-files-message"].textContent, /Deleted 2 superseded filters/);
});

test("empty inventory cannot be deleted and stale confirmation errors remain visible", async () => {
  const { context, elements } = setup();
  let confirmations = 0;
  context.window.confirm = () => { confirmations++; return true; };
  context.fetch = async () => ({ ok: true, text: async () => JSON.stringify({ active: false, inventory: { filter_count: 0, total_bytes: 0 } }) });
  await context.deleteSupersededFilters();
  assert.equal(confirmations, 0);
  assert.equal(elements["cleanup-delete"].disabled, true);
  context.fetch = async (url, options) => ({
    ok: !url.endsWith("/delete"),
    text: async () => JSON.stringify(url.endsWith("/delete") ? { detail: "Refresh and confirm again" } : { active: false, inventory: { filter_count: 1, total_bytes: 6, confirmation_token: "old" } }),
  });
  await context.deleteSupersededFilters();
  assert.equal(elements["cleanup-files-message"].textContent, "Refresh and confirm again");
  assert.equal(elements["cleanup-delete"].disabled, true);
});

test("background refresh keeps totals visible and shares an in-flight scan", async () => {
  const { context, elements } = setup();
  context.showCleanupInventory({ filter_count: 7, total_bytes: 700, confirmation_token: "previous" });
  const requests = [];
  let resume;
  context.setTimeout = callback => { resume = callback; };
  context.fetch = async url => {
    requests.push(url);
    return { ok: true, text: async () => JSON.stringify(url.endsWith("/refresh")
      ? { active: true, inventory: null }
      : { active: false, inventory: { filter_count: 8, total_bytes: 800, confirmation_token: "new" } }) };
  };
  const first = context.refreshCleanupInventory();
  const second = context.refreshCleanupInventory();
  assert.equal(first, second);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements["cleanup-count"].textContent, "7");
  assert.equal(elements["cleanup-files-refresh"].textContent, "Scanning…");
  assert.equal(elements["cleanup-delete"].disabled, true);
  assert.deepEqual(requests, ["/sync/cleanup/files/refresh"]);
  resume();
  await first;
  assert.equal(elements["cleanup-count"].textContent, "8");
  assert.equal(elements["cleanup-files-refresh"].disabled, false);
  assert.deepEqual(requests, ["/sync/cleanup/files/refresh", "/sync/cleanup/files/status"]);
});

test("failed background refresh preserves displayed totals but disables deletion", async () => {
  const { context, elements } = setup();
  context.showCleanupInventory({ filter_count: 7, total_bytes: 700, confirmation_token: "previous" });
  context.fetch = async () => ({ ok: true, text: async () => JSON.stringify({ active: false, error: "Scan failed" }) });
  assert.equal(await context.refreshCleanupInventory(), null);
  assert.equal(elements["cleanup-count"].textContent, "7");
  assert.equal(elements["cleanup-delete"].disabled, true);
  assert.equal(elements["cleanup-files-refresh"].disabled, false);
  assert.match(elements["cleanup-files-message"].textContent, /Scan failed.*out of date/);
});

test("page visits reuse cached totals and their last successful check time", async () => {
  const { context, elements } = setup();
  const requests = [];
  const checkedAt = "2026-11-01T02:03:04+00:00";
  context.fetch = async url => {
    requests.push(url);
    return { ok: true, text: async () => JSON.stringify({ active: false, checked_at: checkedAt,
      inventory: { filter_count: 7, total_bytes: 700, confirmation_token: "cached" } }) };
  };
  await context.loadCleanupInventory();
  await context.loadCleanupInventory();
  assert.deepEqual(requests, ["/sync/cleanup/files/status", "/sync/cleanup/files/status"]);
  assert.equal(elements["cleanup-count"].textContent, "7");
  assert.equal(elements["cleanup-checked-at"].textContent, new Date(checkedAt).toLocaleString());
});

test("an empty cache does not start a scan on page load", async () => {
  const { context, elements } = setup();
  const requests = [];
  context.fetch = async url => {
    requests.push(url);
    return { ok: true, text: async () => JSON.stringify({ active: false, inventory: null, checked_at: null }) };
  };
  await context.loadCleanupInventory();
  assert.deepEqual(requests, ["/sync/cleanup/files/status"]);
  assert.equal(elements["cleanup-checked-at"].textContent, "Never checked");
  assert.equal(elements["cleanup-delete"].disabled, true);
});

test("cached totals appear immediately while an existing scan is running", async () => {
  const { context, elements } = setup();
  const checkedAt = "2026-11-01T02:03:04+00:00";
  let resume;
  let active = true;
  context.setTimeout = callback => { resume = callback; };
  context.fetch = async () => ({ ok: true, text: async () => JSON.stringify({ active, checked_at: checkedAt,
    inventory: { filter_count: active ? 7 : 8, total_bytes: 700, confirmation_token: "cached" } }) });
  const loaded = context.loadCleanupInventory();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements["cleanup-count"].textContent, "7");
  assert.equal(elements["cleanup-checked-at"].textContent, new Date(checkedAt).toLocaleString());
  assert.equal(elements["cleanup-delete"].disabled, true);
  active = false;
  resume();
  await loaded;
  assert.equal(elements["cleanup-count"].textContent, "8");
});

test("a manual refresh during a cached read still requests a scan", async () => {
  const { context } = setup();
  const requests = [];
  let release;
  const cachedRead = new Promise(resolve => { release = resolve; });
  context.fetch = async url => {
    requests.push(url);
    if (url.endsWith("/status")) await cachedRead;
    return { ok: true, text: async () => JSON.stringify({ active: false,
      inventory: { filter_count: 7, total_bytes: 700, confirmation_token: "cached" } }) };
  };
  const load = context.loadCleanupInventory();
  const refresh = context.refreshCleanupInventory();
  release();
  await Promise.all([load, refresh]);
  assert.deepEqual(requests, ["/sync/cleanup/files/status", "/sync/cleanup/files/refresh"]);
});

test("cached totals recover after a temporary status failure", async () => {
  const { context, elements } = setup();
  const status = { active: false, checked_at: "2026-11-01T02:03:04+00:00",
    inventory: { filter_count: 7, total_bytes: 700, confirmation_token: "cached" } };
  context.fetch = async () => ({ ok: true, text: async () => JSON.stringify(status) });
  await context.loadCleanupInventory();
  context.fetch = async () => { throw new Error("Connection lost"); };
  await context.loadCleanupInventory();
  assert.equal(elements["cleanup-count"].textContent, "7");
  assert.equal(elements["cleanup-delete"].disabled, true);
  context.fetch = async () => ({ ok: true, text: async () => JSON.stringify(status) });
  await context.loadCleanupInventory();
  assert.equal(elements["cleanup-delete"].disabled, false);
});
