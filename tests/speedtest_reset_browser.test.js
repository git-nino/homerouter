/* Browser-lifecycle regression checks for Speedtest result display.
   No external dependencies: a minimal DOM/fetch harness runs static/app.js in
   Node's vm so the real page code is exercised, not a reimplementation. */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const SCRIPT = process.env.SPEEDTEST_APP_JS || path.join(ROOT, 'static', 'app.js');
const CODE = fs.readFileSync(SCRIPT, 'utf8') +
  '\n;globalThis.__test = {' +
  '  refreshData, enterDashboard, leaveDashboard, pollSpeed, renderSpeed,' +
  '  resetSpeedDisplay, flag: () => speedResultFromThisPage' +
  '};\n';

const DEFAULTS = {
  speedPanel: true, speedResult: true, speedProgress: true,
  speedError: true, speedIdle: false, loginView: false, dashView: true,
};

function element(id) {
  const el = {
    id,
    hidden: Object.prototype.hasOwnProperty.call(DEFAULTS, id) ? DEFAULTS[id] : false,
    textContent: (id === 'spDown' || id === 'spUp' || id === 'spPing' || id === 'spJitter') ? '–' : '',
    className: 'pill',
    title: '',
    disabled: false,
    value: '',
    type: 'password',
    innerHTML: '',
    dataset: {},
    children: [],
    handlers: {},
    addEventListener(type, handler) {
      (this.handlers[type] = this.handlers[type] || []).push(handler);
    },
    appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    querySelector() { return element(id + '-tbody'); },
    click() {
      for (const handler of this.handlers.click || []) handler({ preventDefault() {} });
    },
  };
  return el;
}

function model(speed) {
  const available = speed.status === 'done';
  const metric = (value) => ({
    availability: available ? 'available' : 'not_loaded',
    value: available ? value : null,
    source_value: available ? value : null,
    unit: null,
  });
  return {
    domains: {
      speed_test: {
        'speed_test.status': { availability: 'available', value: speed.status, source_value: speed.status, unit: null },
        'speed_test.download_mbps': metric(speed.download),
        'speed_test.upload_mbps': metric(speed.upload),
        'speed_test.ping_ms': metric(speed.ping),
        'speed_test.jitter_ms': metric(speed.jitter),
        'speed_test.sponsor': metric(speed.sponsor),
        'speed_test.server': metric(speed.server),
        'speed_test.country': metric(speed.country),
      },
    },
    collections: {},
    devices_loaded: false,
    call_action: { enabled: false, dial_uri: null },
  };
}

const STALE = model({ status: 'done', download: 9.9, upload: 1.1, ping: 88, jitter: 7, sponsor: 'Old ISP', server: 'Old server', country: 'Old country' });
const FRESH = model({ status: 'done', download: 94.2, upload: 41.7, ping: 12.3, jitter: 2.1, sponsor: 'New ISP', server: 'New server', country: 'New country' });

async function boot(overrides = {}) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  const responses = Object.assign({
    '/api/manufacturers': { ok: true, manufacturers: [{ id: 'fiberhome', name: 'FiberHome' }], message: '' },
    '/api/login-defaults': { gateway: '192.168.1.1', gateway_detected: true, username: 'user', password: 'pw', supported: true, message: null },
    '/api/session': { connected: true, gateway: '192.168.1.1' },
    '/api/data': STALE,
    '/api/speedtest': { ok: true },
    '/api/speedtest/status': { status: 'idle', progress: '', result: null, error: null },
  }, overrides);
  const calls = [];
  const intervals = [];
  const context = {
    document: {
      getElementById: get,
      createElement: (tag) => element(tag),
    },
    navigator: {},
    window: { addEventListener() {} },
    fetch: async (url) => {
      calls.push(url);
      const payload = responses[url] || responses[url.split('?')[0]];
      return { ok: true, status: 200, json: async () => payload };
    },
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: () => {},
    setTimeout: () => 0,
    alert: () => {},
    console,
  };
  vm.createContext(context);
  vm.runInContext(CODE, context, { filename: 'app.js' });
  await flush();
  return { get, responses, calls, api: context.__test };
}

async function flush() {
  for (let i = 0; i < 30; i += 1) await Promise.resolve();
}

function assertDefault(page, label) {
  assert.strictEqual(page.get('speedResult').hidden, true, label + ': result hidden');
  assert.strictEqual(page.get('speedIdle').hidden, false, label + ': idle visible');
  assert.strictEqual(page.get('speedProgress').hidden, true, label + ': progress hidden');
  assert.strictEqual(page.get('speedError').hidden, true, label + ': error hidden');
  assert.strictEqual(page.get('speedState').textContent, 'idle', label + ': status idle');
  for (const id of ['spDown', 'spUp', 'spPing', 'spJitter']) {
    assert.strictEqual(page.get(id).textContent, '–', label + ': ' + id + ' empty');
  }
  assert.strictEqual(page.get('spServer').textContent, '', label + ': server empty');
  assert.strictEqual(page.api.flag(), false, label + ': result flag reset');
}

async function main() {
  // 1. A fresh page must ignore a completed result still held by the backend.
  const first = await boot();
  assertDefault(first, 'fresh load');
  await first.api.refreshData();
  assertDefault(first, 'refresh after load');
  await flush();

  // 2. A new Speedtest started on this page still displays its result.
  first.responses['/api/speedtest/status'] = { status: 'running', progress: 'Measuring download speed…', result: null, error: null };
  first.responses['/api/data'] = STALE;
  first.get('speedBtn').click();
  await flush();
  assert.strictEqual(first.api.flag(), true, 'flag set after local start');
  assert.strictEqual(first.get('speedProgress').hidden, false, 'progress shown while running');
  assert.strictEqual(first.get('speedState').textContent, 'running', 'status running');

  first.responses['/api/speedtest/status'] = { status: 'done', progress: '', result: { download: 94.2 }, error: null };
  first.responses['/api/data'] = FRESH;
  await first.api.pollSpeed();
  await flush();
  assert.strictEqual(first.get('speedResult').hidden, false, 'result shown after completion');
  assert.strictEqual(first.get('speedIdle').hidden, true, 'idle hidden after completion');
  assert.strictEqual(first.get('speedState').textContent, 'done', 'status done');
  assert.strictEqual(String(first.get('spDown').textContent), '94.2', 'download displayed');
  assert.strictEqual(String(first.get('spUp').textContent), '41.7', 'upload displayed');
  assert.strictEqual(first.get('spPing').textContent, '12.3', 'ping displayed');
  assert.ok(first.get('spServer').textContent.includes('New ISP'), 'server displayed');

  // 3. Result stays visible while the page remains open.
  await first.api.refreshData();
  await flush();
  assert.strictEqual(first.get('speedResult').hidden, false, 'result persists on same page');
  assert.strictEqual(first.get('spDown').textContent, 94.2, 'value persists on same page');

  // 4. A full reload starts clean even though server state is unchanged.
  const reloaded = await boot();
  assertDefault(reloaded, 'reload');
  assert.strictEqual(reloaded.responses['/api/data'], STALE, 'backend still holds previous result');
  await reloaded.api.refreshData();
  assertDefault(reloaded, 'reload then refresh');

  // 5. Reopening the dashboard without a new test also starts clean.
  reloaded.get('speedBtn').click();
  await flush();
  reloaded.responses['/api/data'] = FRESH;
  reloaded.responses['/api/speedtest/status'] = { status: 'done', progress: '', result: {}, error: null };
  await reloaded.api.pollSpeed();
  await flush();
  assert.strictEqual(reloaded.get('speedResult').hidden, false, 'result shown before leaving');
  reloaded.api.leaveDashboard('Disconnected.');
  reloaded.api.enterDashboard();
  await flush();
  assertDefault(reloaded, 'dashboard reopened');

  console.log('Speedtest browser-lifecycle checks: PASS');
}

main().catch((error) => {
  console.error('Speedtest browser-lifecycle checks: FAIL');
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
