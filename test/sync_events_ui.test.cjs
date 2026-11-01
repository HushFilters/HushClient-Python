const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

const flush = () => new Promise(resolve => setImmediate(resolve));
const status = (extra = {}) => ({ operation: 'sync_apply', active: false, success: true, logs: ['Previous run'], progress: [], ...extra });
const schedule = (extra = {}) => ({ enabled: true, hour: 23, current_time: '2026-11-01T10:00:00+13:00', timezone: 'NZDT', history: [], ...extra });

function setup({ streaming = true, fetchOverride } = {}) {
  const elements = {}, windowEvents = {}, documentEvents = {};
  const intervals = new Map(), timeouts = new Map(), sources = [], requests = [];
  let timerId = 0;
  const element = () => ({
    textContent: '', children: [], listeners: {}, value: '',
    addEventListener(name, handler) { this.listeners[name] = handler; },
    append(...items) { this.children.push(...items); },
    appendChild(item) { this.children.push(item); },
    replaceChildren() { this.children = []; },
    getBoundingClientRect() { return { top: 0 }; },
  });
  class EventSource {
    constructor(url) { this.url = url; this.listeners = {}; sources.push(this); }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    close() { this.closed = true; }
    send(name, payload) { this.listeners[name]({ data: JSON.stringify(payload) }); }
    connect(payload = status(), cleanup = { active: false, inventory: null, checked_at: null, error: null }) {
      this.onopen();
      this.send('auto-update', schedule());
      this.send('sync-status', payload);
      this.send('cleanup-status', cleanup);
    }
  }
  const response = payload => ({ ok: true, text: async () => JSON.stringify(payload), json: async () => payload });
  const context = {
    document: {
      hidden: false,
      addEventListener: (name, handler) => { documentEvents[name] = handler; },
      getElementById: id => elements[id] ||= element(),
      createElement: element, querySelectorAll: () => [],
    },
    window: {
      EventSource: streaming ? EventSource : undefined,
      addEventListener: (name, handler) => { windowEvents[name] = handler; },
      setTimeout: (callback, delay) => { timeouts.set(++timerId, { callback, delay }); return timerId; },
      clearTimeout: id => timeouts.delete(id),
      setInterval: (callback, delay) => { intervals.set(++timerId, { callback, delay }); return timerId; },
      clearInterval: id => intervals.delete(id),
      scrollTo() {}, scrollY: 0, matchMedia: () => ({ matches: true }),
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (fetchOverride) {
        const result = await fetchOverride(url, options);
        if (result) return result;
      }
      if (url === '/sync/status') return response(status());
      if (url === '/sync/auto-update') return response(schedule());
      if (url === '/sync/apply') return response({ started: true, operation: 'sync_apply' });
      if (url === '/sync/cleanup') return response({ enabled: false, retention_days: 31 });
      if (url === '/stats') return response({ filter_count: 1280 });
      return response({ active: false, inventory: null });
    },
  };
  context.setTimeout = context.window.setTimeout;
  runInNewContext(readFileSync(join(__dirname, '../webui/ui-sync/app.js'), 'utf8'), context);
  windowEvents.DOMContentLoaded();
  const statusReads = () => requests.filter(r => ['/sync/status', '/sync/auto-update', '/sync/cleanup/files/status'].includes(r.url));
  return { context, elements, sources, requests, intervals, timeouts, documentEvents, windowEvents, statusReads };
}

test('one SSE connection replaces periodic status reads, preserves edits, and renders live updates', async () => {
  const ui = setup();
  await flush();
  assert.equal(ui.sources.length, 1);
  assert.equal(ui.sources[0].url, '/sync/events');
  ui.sources[0].connect();
  for (let i = 0; i < 10; i++) for (const { callback } of ui.intervals.values()) callback();
  await flush();
  assert.equal(ui.statusReads().length, 0);
  ui.sources[0].send('sync-status', status({ active: true, logs: ['Downloading new filters'] }));
  assert.match(ui.elements['sync-output'].textContent, /Downloading new filters/);
  assert.equal(ui.elements['apply-button'].disabled, true);
  ui.elements['auto-update-hour'].value = '8';
  ui.elements['auto-update-hour'].listeners.input();
  ui.sources[0].send('auto-update', schedule({ hour: 22 }));
  assert.equal(ui.elements['auto-update-hour'].value, '8');
  ui.sources[0].send('sync-status', status({ success: false, logs: ['Validation failed'] }));
  assert.equal(ui.elements['apply-button'].disabled, false);
  assert.match(ui.elements['sync-status'].textContent, /failed/);
  // Explicit user refresh remains a REST read even with a healthy stream.
  ui.elements['auto-update-refresh'].listeners.click();
  await flush();
  assert.equal(ui.statusReads().length, 1);
});

test('stream errors fall back to polling; reconnect snapshots stop polling again', async () => {
  const ui = setup();
  const source = ui.sources[0];
  source.connect();
  source.onerror();
  await flush();
  assert.deepEqual(ui.statusReads().map(r => r.url).sort(), ['/sync/auto-update', '/sync/cleanup/files/status', '/sync/status']);
  source.connect(status({ logs: ['Caught up after reconnect'] }));
  for (const { callback } of ui.intervals.values()) callback();
  await flush();
  assert.equal(ui.statusReads().length, 3);
  assert.match(ui.elements['sync-output'].textContent, /Caught up after reconnect/);
});

test('unsupported SSE and a stalled initial snapshot both retain REST polling', async () => {
  const unsupported = setup({ streaming: false });
  await flush();
  assert.equal(unsupported.sources.length, 0);
  assert.equal(unsupported.statusReads().length, 3);
  const stalled = setup();
  stalled.sources[0].onopen(); // HTTP headers arrived, but no event body.
  for (const { callback, delay } of [...stalled.timeouts.values()]) if (delay === 5000) callback();
  await flush();
  assert.equal(stalled.statusReads().length, 3);
  stalled.sources[0].send('auto-update', schedule());
  stalled.sources[0].send('sync-status', status());
  stalled.sources[0].send('cleanup-status', { active: false, inventory: null });
  for (const { callback } of stalled.intervals.values()) callback();
  await flush();
  assert.equal(stalled.statusReads().length, 3);
});

test('idle hidden tabs close SSE, visible tabs reconnect, and old connection events are ignored', async () => {
  const ui = setup();
  const original = ui.sources[0];
  original.connect();
  ui.context.document.hidden = true;
  ui.documentEvents.visibilitychange();
  assert.equal(original.closed, true);
  for (const { callback } of ui.intervals.values()) callback();
  await flush();
  assert.equal(ui.statusReads().length, 0);
  ui.context.document.hidden = false;
  ui.documentEvents.visibilitychange();
  assert.equal(ui.sources.length, 2);
  ui.sources[1].connect(status({ logs: ['Current connection'] }));
  original.send('sync-status', status({ logs: ['Stale connection'] }));
  assert.match(ui.elements['sync-output'].textContent, /Current connection/);
  ui.windowEvents.pagehide();
  assert.equal(ui.sources[1].closed, true);
  ui.windowEvents.pageshow();
  assert.equal(ui.sources.length, 3);
});

test('background commands await their fresh SSE result, including fast completion and hidden tabs', async () => {
  const ui = setup();
  ui.sources[0].connect(); // A completed earlier run must not satisfy this command.
  let finished = false;
  const operation = ui.context.runBackgroundApplyOperation({
    endpoint: '/sync/apply', operation: 'sync_apply', inProgress: 'Starting sync',
    success: 'New sync complete', failure: 'New sync failed',
  }).then(() => { finished = true; });
  await flush();
  assert.equal(ui.sources[0].closed, true);
  assert.equal(ui.sources.length, 2);
  assert.equal(finished, false);
  ui.context.document.hidden = true;
  ui.documentEvents.visibilitychange();
  assert.equal(ui.sources[1].closed, undefined); // Keep completion delivery alive.
  // The run can finish before the new connection's first snapshot arrives.
  ui.sources[1].connect(status({ logs: ['New run completed'], filter_count: 42 }));
  await operation;
  assert.equal(finished, true);
  assert.match(ui.elements['sync-output'].textContent, /New run completed/);
  assert.equal(ui.elements['sync-status'].textContent, 'New sync complete');
  assert.equal(ui.elements['loaded-count'].textContent, '42');
  assert.equal(ui.sources[1].closed, true);
  assert.equal(ui.statusReads().length, 0);
});

test('live activation results refresh loaded filters for manual, scheduled, and external reloads', async () => {
  for (const operation of ['sync_apply', 'auto_sync_apply', 'sync_reload']) {
    const ui = setup();
    await flush();
    assert.equal(ui.elements['loaded-count'].textContent, '1280');
    ui.sources[0].connect(status({ operation, active: true, success: null, filter_count: 0 }));
    assert.equal(ui.elements['loaded-count'].textContent, '1280');
    // The sync was started elsewhere; this tab has no onSuccess callback.
    ui.sources[0].send('sync-status', status({ operation, filter_count: 512 }));
    assert.equal(ui.elements['loaded-count'].textContent, '512');
    assert.equal(ui.requests.filter(r => r.url === '/stats').length, 1);
  }
});

test('staged filter counts and failed activation leave the active count unchanged', async () => {
  const ui = setup();
  await flush();
  ui.sources[0].connect(status({ filter_count: 512 }));
  for (const payload of [
    status({ operation: 'sync_manifest', filter_count: 2048 }),
    status({ operation: 'sync_filters', filter_count: 2048 }),
    status({ active: true, filter_count: 2048 }),
    status({ success: false, filter_count: 0 }),
    status({ filter_count: undefined }),
  ]) {
    ui.sources[0].send('sync-status', payload);
    assert.equal(ui.elements['loaded-count'].textContent, '512');
  }
  ui.sources[0].send('sync-status', status({ filter_count: 0 }));
  assert.equal(ui.elements['loaded-count'].textContent, '0');
});

test('manual reload uses its activation result without fetching stale stats again', async () => {
  const ui = setup({ fetchOverride: url => url === '/sync/reload'
    ? { ok: true, text: async () => JSON.stringify({ success: true, filter_count: 512 }) } : null });
  await flush();
  ui.sources[0].connect(status({ active: true, success: null }));
  ui.elements['reload-button'].listeners.click();
  await flush();
  assert.equal(ui.elements['loaded-count'].textContent, '512');
  assert.equal(ui.requests.filter(r => r.url === '/stats').length, 1);
});

test('an old stats response cannot overwrite a newer activated filter count', async () => {
  let finishStats;
  const ui = setup({ fetchOverride: url => url === '/stats'
    ? new Promise(resolve => { finishStats = () => resolve({ ok: true, json: async () => ({ filter_count: 1280 }) }); }) : null });
  ui.sources[0].connect(status({ filter_count: 512 }));
  assert.equal(ui.elements['loaded-count'].textContent, '512');
  finishStats();
  await flush();
  assert.equal(ui.elements['loaded-count'].textContent, '512');
  assert.equal(ui.requests.find(r => r.url === '/stats').options.cache, 'no-store');
});

test('REST polling and reconnect snapshots refresh the count after missed completion events', async () => {
  const polling = setup({ streaming: false, fetchOverride: url => url === '/sync/status'
    ? { ok: true, json: async () => status({ filter_count: 512 }) } : null });
  await flush();
  assert.equal(polling.elements['loaded-count'].textContent, '512');
  const ui = setup();
  await flush();
  ui.sources[0].connect(status({ active: true, success: null }));
  ui.context.document.hidden = true;
  ui.documentEvents.visibilitychange();
  ui.context.document.hidden = false;
  ui.documentEvents.visibilitychange();
  ui.sources[1].connect(status({ filter_count: 512 }));
  assert.equal(ui.elements['loaded-count'].textContent, '512');
});

test('local clock advances with the server UTC offset without network requests', () => {
  const ui = setup();
  let now = Date.now();
  ui.context.Date = class extends Date { static now() { return now; } };
  ui.sources[0].connect();
  ui.sources[0].send('auto-update', schedule({ active: true, active_since: '2026-11-01T10:00:00+13:00' }));
  now += 5000;
  ui.context.tickScheduleClock();
  assert.equal(ui.elements['auto-update-current-time'].textContent, '2026-11-01 10:00:05+13:00');
  const running = ui.elements['auto-update-history'].children[0];
  assert.match(running.children[1].textContent, /Running for 5s/);
  assert.equal(ui.statusReads().length, 0);
});

test('cleanup refreshes use POST and SSE completion, share requests, and keep receiving while hidden', async () => {
  const cached = { active: false, checked_at: '2026-11-01T01:00:00Z', error: null,
    inventory: { filter_count: 7, total_bytes: 700, confirmation_token: 'old' } };
  const active = { ...cached, active: true };
  const ui = setup({ fetchOverride: async url => url.endsWith('/files/refresh')
    ? { ok: true, text: async () => JSON.stringify(active) } : null });
  ui.sources[0].connect(status(), cached);
  let completed = false;
  const refresh = ui.context.refreshCleanupInventory();
  assert.equal(ui.context.refreshCleanupInventory(), refresh);
  refresh.then(() => { completed = true; });
  await flush();
  assert.equal(ui.sources.length, 2);
  assert.equal(completed, false);
  ui.sources[1].connect(status(), active);
  assert.equal(ui.elements['cleanup-count'].textContent, '7');
  assert.equal(ui.elements['cleanup-delete'].disabled, true);
  ui.context.document.hidden = true;
  ui.documentEvents.visibilitychange();
  assert.equal(ui.sources[1].closed, undefined);
  for (const { callback } of ui.intervals.values()) callback();
  await flush();
  assert.equal(ui.statusReads().length, 0);
  const finalInventory = { filter_count: 8, total_bytes: 800, confirmation_token: 'fresh' };
  ui.sources[1].send('cleanup-status', { active: false, checked_at: '2026-11-01T01:01:00Z', error: null, inventory: finalInventory });
  assert.deepEqual(JSON.parse(JSON.stringify(await refresh)), finalInventory);
  assert.equal(ui.elements['cleanup-count'].textContent, '8');
  assert.equal(ui.sources[1].closed, true);
  assert.equal(ui.requests.filter(r => r.url.endsWith('/files/refresh')).length, 1);
  assert.equal(ui.statusReads().length, 0);
});

test('cleanup events preserve totals on failure and invalidate deletion until a successful snapshot', () => {
  const ui = setup();
  const inventory = { filter_count: 7, total_bytes: 700, confirmation_token: 'cached' };
  ui.sources[0].connect(status(), { active: false, inventory, checked_at: null, error: null });
  ui.sources[0].send('cleanup-status', { active: true, inventory, checked_at: null, error: null });
  assert.equal(ui.elements['cleanup-files-refresh'].textContent, 'Scanning…');
  ui.sources[0].send('cleanup-status', { active: false, inventory, checked_at: null, error: 'Scan failed' });
  assert.equal(ui.elements['cleanup-count'].textContent, '7');
  assert.equal(ui.elements['cleanup-delete'].disabled, true);
  assert.match(ui.elements['cleanup-files-message'].textContent, /Scan failed.*out of date/);
  ui.sources[0].send('cleanup-status', { active: false, inventory, checked_at: null, error: null });
  assert.equal(ui.elements['cleanup-delete'].disabled, false);
  assert.equal(ui.statusReads().length, 0);
});

test('cleanup scan can finish before reconnect snapshots arrive without reusing old totals', async () => {
  const inventory = { filter_count: 4, total_bytes: 400, confirmation_token: 'previous' };
  const ui = setup({ fetchOverride: async url => url.endsWith('/files/refresh')
    ? { ok: true, text: async () => JSON.stringify({ active: true, inventory }) } : null });
  ui.sources[0].connect(status(), { active: false, inventory });
  const pending = ui.context.refreshCleanupInventory();
  await flush();
  ui.sources[0].send('cleanup-status', { active: false, inventory }); // Old source is ignored.
  const completed = { ...inventory, confirmation_token: 'new-scan' };
  ui.sources[1].connect(status(), { active: false, inventory: completed });
  assert.equal((await pending).confirmation_token, 'new-scan');
  assert.equal(ui.statusReads().length, 0);
});

test('cleanup refresh switches to REST after a stream failure and stops reads after recovery', async () => {
  const inventory = { filter_count: 4, total_bytes: 400, confirmation_token: 'previous' };
  const final = { active: false, inventory: { ...inventory, confirmation_token: 'fallback-result' } };
  const ui = setup({ fetchOverride: async url => {
    if (url.endsWith('/files/refresh')) return { ok: true, text: async () => JSON.stringify({ active: true, inventory }) };
    if (url.endsWith('/files/status')) return { ok: true, text: async () => JSON.stringify(final) };
  } });
  ui.sources[0].connect(status(), { active: false, inventory });
  const refresh = ui.context.refreshCleanupInventory();
  await flush();
  ui.sources[1].connect(status(), { active: true, inventory });
  ui.sources[1].onerror();
  await flush();
  for (const [id, { callback, delay }] of [...ui.timeouts]) {
    if (delay === 1000) { ui.timeouts.delete(id); callback(); }
  }
  assert.equal((await refresh).confirmation_token, 'fallback-result');
  const reads = () => ui.requests.filter(r => r.url.endsWith('/files/status')).length;
  assert.equal(reads(), 1);
  ui.sources[1].connect(status(), final);
  for (const { callback } of ui.intervals.values()) callback();
  await flush();
  assert.equal(reads(), 1);
});

test('deletion still confirms fresh SSE totals and submits only the new confirmation token', async () => {
  const old = { filter_count: 1, total_bytes: 100, confirmation_token: 'old' };
  const fresh = { filter_count: 5, total_bytes: 500, confirmation_token: 'new-confirmation' };
  const ui = setup({ fetchOverride: async url => {
    if (url.endsWith('/files/refresh')) return { ok: true, text: async () => JSON.stringify({ active: true, inventory: old }) };
    if (url.endsWith('/cleanup/delete')) return { ok: true, text: async () => JSON.stringify({ deleted_count: 5, deleted_bytes: 500, filter_count: 0 }) };
  } });
  let confirmation;
  ui.context.window.confirm = text => { confirmation = text; return true; };
  ui.sources[0].connect(status(), { active: false, inventory: old });
  const deletion = ui.context.deleteSupersededFilters();
  await flush();
  ui.sources[1].connect(status(), { active: false, inventory: fresh });
  await deletion;
  assert.match(confirmation, /5 superseded filter files/);
  const request = ui.requests.find(r => r.url.endsWith('/cleanup/delete'));
  assert.deepEqual(JSON.parse(request.options.body), { confirmation_token: 'new-confirmation' });
  assert.equal(ui.statusReads().length, 0);
});
