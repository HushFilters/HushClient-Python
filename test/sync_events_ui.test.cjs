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
    connect(payload = status()) {
      this.onopen();
      this.send('auto-update', schedule());
      this.send('sync-status', payload);
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
      return response({ active: false, inventory: null });
    },
  };
  runInNewContext(readFileSync(join(__dirname, '../webui/ui-sync/app.js'), 'utf8'), context);
  windowEvents.DOMContentLoaded();
  const statusReads = () => requests.filter(r => ['/sync/status', '/sync/auto-update'].includes(r.url));
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
  assert.deepEqual(ui.statusReads().map(r => r.url).sort(), ['/sync/auto-update', '/sync/status']);
  source.connect(status({ logs: ['Caught up after reconnect'] }));
  for (const { callback } of ui.intervals.values()) callback();
  await flush();
  assert.equal(ui.statusReads().length, 2);
  assert.match(ui.elements['sync-output'].textContent, /Caught up after reconnect/);
});

test('unsupported SSE and a stalled initial snapshot both retain REST polling', async () => {
  const unsupported = setup({ streaming: false });
  await flush();
  assert.equal(unsupported.sources.length, 0);
  assert.equal(unsupported.statusReads().length, 2);
  const stalled = setup();
  stalled.sources[0].onopen(); // HTTP headers arrived, but no event body.
  for (const { callback, delay } of [...stalled.timeouts.values()]) if (delay === 5000) callback();
  await flush();
  assert.equal(stalled.statusReads().length, 2);
  stalled.sources[0].send('auto-update', schedule());
  stalled.sources[0].send('sync-status', status());
  for (const { callback } of stalled.intervals.values()) callback();
  await flush();
  assert.equal(stalled.statusReads().length, 2);
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
  assert.equal(ui.sources[1].closed, true);
  assert.equal(ui.statusReads().length, 0);
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
