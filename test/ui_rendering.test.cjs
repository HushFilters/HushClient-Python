const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

function element() {
  let text = '';
  return {
    writes: 0, replacements: 0, children: [], listeners: {},
    get textContent() { return text; },
    set textContent(value) { text = value; this.writes++; },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    append(...items) { this.children.push(...items); },
    appendChild(item) { this.children.push(item); },
    replaceChildren() { this.children = []; this.replacements++; },
  };
}

function setup(page, fetch = async () => {}) {
  const elements = {};
  const intervals = [];
  const context = {
    document: {
      hidden: false,
      getElementById: id => elements[id] ||= element(),
      querySelectorAll: () => [],
      createElement: element,
    },
    window: { addEventListener() {}, setInterval: callback => intervals.push(callback) },
    fetch,
  };
  runInNewContext(readFileSync(join(__dirname, `../webui/${page}/app.js`), 'utf8'), context);
  return { context, elements, intervals };
}

test('unchanged sync polls preserve log nodes, history, and progress; changed data still renders', () => {
  const { context, elements } = setup('ui-sync');
  const payload = { operation: 'sync_apply', active: false, success: true, logs: ['Validated all filters'], progress: [] };
  context.renderStatusLogs(payload);
  for (let i = 0; i < 60; i++) context.renderStatusLogs(payload);
  assert.equal(elements['sync-output'].writes, 1);
  assert.equal(elements['sync-progress'].replacements, 1);
  const history = [{ status: 'success', triggered_at: '2026-11-01T01:00:00Z', completed_at: '2026-11-01T01:01:00Z', filter_count: 256 }];
  context.renderAutoUpdateHistory(history);
  const firstRow = elements['auto-update-history'].children[0];
  for (let i = 0; i < 12; i++) context.renderAutoUpdateHistory(JSON.parse(JSON.stringify(history)));
  assert.equal(elements['auto-update-history'].replacements, 1);
  assert.equal(elements['auto-update-history'].children[0], firstRow);
  context.renderStatusLogs({ ...payload, logs: [...payload.logs, 'Activation complete'] });
  assert.equal(elements['sync-output'].writes, 2);
  assert.match(elements['sync-output'].textContent, /Activation complete/);
  context.renderOutput({ detail: 'Complete', logs: ['New result'] });
  context.renderOutput({ detail: 'Complete', logs: ['New result'] });
  assert.equal(elements['sync-output'].writes, 3);
  context.renderAutoUpdateHistory([{ ...history[0], filter_count: 512 }]);
  assert.equal(elements['auto-update-history'].replacements, 2);
});

test('large unchanged client logs cause no DOM writes on subsequent refreshes', async () => {
  const payload = { text: 'Retained log entry\n'.repeat(14500), size_bytes: 261000, max_size_bytes: 1073741824, settings: {} };
  const { context, elements } = setup('ui-logs', async () => ({ ok: true, json: async () => payload }));
  await new Promise(resolve => setImmediate(resolve)); // Initial refresh.
  for (let i = 0; i < 30; i++) await context.refresh();
  for (const id of ['log-output', 'log-size', 'log-status']) assert.equal(elements[id].writes, 1, id);
  payload.text += 'A new entry\n';
  await context.refresh();
  assert.equal(elements['log-output'].writes, 2);
  assert.match(elements['log-output'].textContent, /A new entry/);
  payload.text = '';
  await context.refresh();
  assert.equal(elements['log-output'].textContent, 'No log entries.');
});

test('schedule polling skips hidden pages and does not overlap slow requests', async () => {
  let requests = 0;
  let finish;
  const { context } = setup('ui-sync', () => {
    requests++;
    return new Promise(resolve => { finish = () => resolve({ ok: false, text: async () => '{}' }); });
  });
  context.document.hidden = true;
  await context.refreshAutoUpdateStatus();
  assert.equal(requests, 0);
  context.document.hidden = false;
  const first = context.refreshAutoUpdateStatus();
  await context.refreshAutoUpdateStatus();
  assert.equal(requests, 1);
  finish();
  await first;
  const next = context.refreshAutoUpdateStatus();
  assert.equal(requests, 2);
  finish();
  await next;
});

test('auto-scroll batches mutations and respects closing, disabling follow, and hidden pages', () => {
  const frames = [];
  let notify;
  let layoutReads = 0;
  const output = { scrollTop: 0, get scrollHeight() { layoutReads++; return 999; } };
  const follow = { ...element(), checked: true };
  const panel = { ...element(), open: true, querySelector: selector => selector === 'pre' ? output : follow };
  const context = {
    document: { hidden: false, querySelectorAll: () => [panel] },
    window: { requestAnimationFrame: callback => frames.push(callback) },
    MutationObserver: class { constructor(callback) { notify = callback; } observe() {} },
  };
  runInNewContext(readFileSync(join(__dirname, '../webui/assets/log-window.js'), 'utf8'), context);
  notify(); notify();
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.equal(output.scrollTop, 999);
  assert.equal(layoutReads, 1);
  notify();
  follow.checked = false;
  frames.shift()();
  assert.equal(layoutReads, 1);
  notify();
  assert.equal(frames.length, 0);
  follow.checked = true;
  follow.listeners.change();
  panel.open = false;
  frames.shift()();
  assert.equal(layoutReads, 1);
  panel.open = true;
  context.document.hidden = true;
  notify();
  assert.equal(frames.length, 0);
});
