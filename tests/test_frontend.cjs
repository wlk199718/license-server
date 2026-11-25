const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('full cards remain visible/copyable while actions keep their identifier', () => {
  const { context } = setup();
  const row = context.licRow({
    key: 'card-id', full_key: 'card-id.full-secret', is_legacy: false,
    project_code: 'test', current_devices: 0, max_devices: 1, is_active: true
  }, false);
  assert.ok(row.includes('card-id.full-secret'));
  assert.ok(row.includes('data-key="card-id.full-secret"'));
  assert.ok(row.includes('data-key="card-id"'));
  assert.ok(!row.includes('只在生成时'));
});

test('new key dialog can be closed without one-time-copy confirmation', () => {
  const { context, element } = setup();
  context.confirm = () => { throw new Error('unexpected confirmation'); };
  context.showKeysModal(['example-secret']);
  assert.ok(element('deviceList').innerHTML.includes('随时查看和复制'));
  context.closeModal('deviceModal');
  assert.equal(element('deviceModal').classList.contains('show'), false);
});

function setup() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      elements.set(id, {
        value: '', checked: false, innerHTML: '', textContent: '',
        style: {}, options: [], attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        querySelector() { return null; },
        focus() {},
        classList: {
          values: new Set(),
          add(name) { this.values.add(name); },
          remove(name) { this.values.delete(name); },
          contains(name) { return this.values.has(name); }
        }
      });
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    location: { origin: 'http://localhost' },
    URLSearchParams,
    document: {
      cookie: '',
      getElementById: element,
      querySelector: () => null,
      activeElement: null,
      contains: () => false,
      addEventListener() {},
      body: { appendChild() {} }
    },
    window: {},
    fetch: async () => ({ status: 200, ok: true, json: async () => ({ ok: false }) }),
    setTimeout: () => {},
    console
  });
  const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
  vm.runInContext(source, context);
  return { context, element };
}

test('initial unauthenticated session check does not show an expired-login toast', async () => {
  const { context } = setup();
  const messages = [];
  context.toast = message => messages.push(message);
  context.fetch = async () => ({
    status: 401,
    ok: false,
    json: async () => ({ detail: '未登录或登录已过期' })
  });
  const result = await context.api('GET', '/admin/me');
  assert.equal(result.detail, '未登录或登录已过期');
  assert.deepEqual(messages, []);
});

test('license filters query the server and reset page number', async () => {
  const { context, element } = setup();
  const urls = [];
  context.api = async (_method, url) => {
    urls.push(url);
    return { ok: true, licenses: [], total: 0, total_pages: 0 };
  };
  element('licProjectFilter').value = 'my project';
  element('licSearchInput').value = ' customer_100% ';
  element('licStatusFilter').value = 'active';
  vm.runInContext('licPage = 3', context);
  await context.applyLicenseFilters();
  assert.equal(urls.at(-1), '/admin/licenses?page=1&page_size=20&project=my%20project&search=customer_100%25&status=active');
  await context.resetLicenseFilters();
  assert.equal(urls.at(-1), '/admin/licenses?page=1&page_size=20');
  assert.equal(element('licSearchInput').value, '');
});

test('successful creation refreshes the list and clears the next form', async () => {
  const { context, element } = setup();
  let refreshed = 0;
  let submitted;
  context.api = async (_method, _path, body) => {
    submitted = body;
    return { ok: true, licenses: ['new-secret'] };
  };
  context.refreshAll = async () => { refreshed++; return true; };
  context.showKeysModal = () => {};
  context.toast = () => {};
  element('cLicProject').value = 'project_a';
  element('cLicCount').value = '2';
  element('cLicDevices').value = '3';
  element('cLicDays').value = '30';
  element('cLicStartOnActivation').checked = true;
  element('cLicNote').value = 'old note';
  element('licSearchInput').value = 'old filter';
  await context.doCreateLicense();
  assert.equal(submitted.project_code, 'project_a');
  assert.equal(refreshed, 1);
  assert.equal(element('licProjectFilter').value, 'project_a');
  assert.equal(element('licSearchInput').value, '');
  assert.equal(element('cLicProject').value, '');
  assert.equal(element('cLicCount').value, '1');
  assert.equal(element('cLicDevices').value, '1');
  assert.equal(element('cLicDays').value, '');
  assert.equal(element('cLicStartOnActivation').checked, false);
  assert.equal(element('cLicNote').value, '');
  element('cLicNote').value = 'cancelled draft';
  context.openModal('createLicModal');
  assert.equal(element('cLicNote').value, '');
});

test('a slower older search cannot replace the newest results', async () => {
  const { context, element } = setup();
  const pending = [];
  context.api = () => new Promise(resolve => pending.push(resolve));
  element('licSearchInput').value = 'old';
  const oldSearch = context.applyLicenseFilters();
  element('licSearchInput').value = 'new';
  const newSearch = context.applyLicenseFilters();
  pending[1]({ ok: true, licenses: [], total: 1, total_pages: 1 });
  await newSearch;
  pending[0]({ ok: true, licenses: [], total: 99, total_pages: 5 });
  await oldSearch;
  assert.match(element('licPagination').innerHTML, /共 1 条/);
  assert.equal(element('licTable').attributes['aria-busy'], 'false');
});

test('pending first-activation license is not labelled permanent', () => {
  const { context } = setup();
  const license = {
    key: 'new-id', project_code: 'sample', note: '', is_active: true,
    current_devices: 0, max_devices: 1, duration_days: 7,
    activated_at: null, expires_at: null, created_at: '2026-10-06T00:00:00Z',
  };
  assert.match(context.statusBadge(license), /待激活/);
  assert.match(context.licRow(license, false), /待激活 · 7天/);
  assert.doesNotMatch(context.licRow(license, false), /永久/);
});

test('double submission sends only one creation request', async () => {
  const { context, element } = setup();
  let resolveRequest;
  let calls = 0;
  context.api = (_method, _path, body) => {
    calls++;
    assert.ok(body.request_id.length >= 16);
    return new Promise(resolve => { resolveRequest = resolve; });
  };
  context.toast = () => {};
  context.showKeysModal = () => {};
  context.refreshAll = async () => true;
  context.openModal('createLicModal');
  element('cLicProject').value = 'sample';
  element('cLicCount').value = '1';
  element('cLicDevices').value = '1';
  const first = context.doCreateLicense();
  await context.doCreateLicense();
  assert.equal(calls, 1);
  assert.equal(element('createLicSubmit').disabled, true);
  context.closeModal('createLicModal');
  assert.equal(element('createLicModal').classList.contains('show'), true);
  resolveRequest({ ok: true, licenses: ['sample-secret'] });
  await first;
  assert.equal(element('createLicSubmit').disabled, false);
});

test('uncertain network retry reuses the same creation id', async () => {
  const { context, element } = setup();
  const requestIds = [];
  context.api = async (_method, _path, body) => {
    requestIds.push(body.request_id);
    if (requestIds.length === 1) throw new Error('network');
    return { ok: false, detail: '这次生成请求已处理' };
  };
  context.toast = () => {};
  element('cLicProject').value = 'sample';
  element('cLicCount').value = '1';
  element('cLicDevices').value = '1';
  await context.doCreateLicense();
  await context.doCreateLicense();
  assert.equal(requestIds.length, 2);
  assert.equal(requestIds[0], requestIds[1]);
});

test('audit rows escape untrusted fields', async () => {
  const { context, element } = setup();
  context.api = async () => ({
    ok: true, total: 1, total_pages: 1,
    items: [{
      action: '<script>bad</script>',
      target: '<img src=x>',
      actor_ip: '<svg>',
      created_at: '2026-10-06T00:00:00Z',
    }]
  });
  assert.equal(await context.loadAudit(), true);
  assert.doesNotMatch(element('auditTable').innerHTML, /<script>|<img|<svg>/);
  assert.match(element('auditTable').innerHTML, /&lt;script&gt;/);
});

test('heartbeat rows keep eight aligned columns, project and failure tooltips', async () => {
  const { context, element } = setup();
  context.api = async () => ({
    ok: true, total: 1, total_pages: 1,
    online_device_count: 3, online_timeout_seconds: 180,
    items: [{
      created_at: '2026-10-06T00:00:00Z',
      event_type: 'verify',
      ok: false,
      license_key: 'abcd',
      project_code: 'custom-video',
      client_ip: '2001:db8::1',
      device_info: 'Windows 11',
      license_note: '销售组 <一>',
      error: '设备数已达上限',
    }],
    summary: [{ project_code: 'custom-video', license_note: '销售组 <一>', count: 3 }],
  });
  assert.equal(await context.loadHeartbeatLogs(), true);
  const row = element('heartbeatTable').innerHTML;
  assert.equal((row.match(/<td>/g) || []).length, 8);
  assert.match(row, /title="设备数已达上限"/);
  assert.match(row, /class="mono cell-nowrap"/);
  assert.match(row, /class="cell-device"/);
  assert.match(row, /custom-video/);
  assert.match(row, /销售组 &lt;一&gt;/);
  assert.doesNotMatch(row, /销售组 <一>/);
  const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
  const header = html.match(/<table class="heartbeat-table"><thead><tr>([\s\S]*?)<\/tr>/);
  assert.ok(header);
  assert.equal((header[1].match(/<th>/g) || []).length, 8);
  assert.match(header[1], /<th>项目<\/th>/);
  assert.match(
    header[1],
    /<th>项目<\/th><th>卡密<\/th><th>事件<\/th><th>状态<\/th><th>客户端 IP<\/th><th>设备信息<\/th><th>时间<\/th><th>备注<\/th>/
  );
  assert.doesNotMatch(header[1], /<th>设备<\/th>/);
  assert.match(header[1], /<th>备注<\/th>/);
  assert.doesNotMatch(header[1], /说明/);
  assert.match(element('heartbeatSummary').innerHTML, /销售组 &lt;一&gt;/);
  assert.match(element('heartbeatSummary').innerHTML, />3 台</);
  assert.match(element('heartbeatSummaryMeta').textContent, /当前在线 3 台/);
});

test('locked-card list escapes fields and unlocks only the selected card', async () => {
  const { context, element } = setup();
  context.renderClientBlacklist([{
    license_identifier: 'card"><img src=x>', project_code: 'sample',
    license_note: '<script>bad</script>', failed_attempts: 10,
    blocked_at: '2026-10-13T10:00:00Z'
  }]);
  const row = element('blacklistTable').innerHTML;
  assert.ok(!row.includes('<img src=x>'));
  assert.ok(!row.includes('<script>'));
  assert.match(row, /&lt;script&gt;/);
  const calls = [];
  context.confirm = () => true;
  context.toast = () => {};
  context.loadHeartbeatLogs = async () => true;
  context.api = async (method, url, body) => {
    calls.push({ method, url, body });
    return { ok: true };
  };
  await context.unblockClient('license-id-00001');
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].url, '/admin/blacklist/unblock');
  assert.equal(calls[0].body.license_identifier, 'license-id-00001');
  assert.equal(Object.keys(calls[0].body).length, 1);
});

test('project deletion needs the exact code and forwards the expected count', () => {
  const { context } = setup();
  const calls = [];
  context.toast = () => {};
  context.deleteProject = (code, count) => calls.push([code, count]);
  context.prompt = () => 'wrong_code';
  context.confirmProjectDelete('sample', 12);
  assert.equal(calls.length, 0);
  context.prompt = () => 'sample';
  context.confirmProjectDelete('sample', 12);
  assert.deepEqual(calls, [['sample', 12]]);
});
