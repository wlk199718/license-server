const API = location.origin;
let adminKey = localStorage.getItem('adminKey') || '';
let allProjects = [];
let allLicenses = [];
let licPage = 1;
let licTotalPages = 1;
let licTotal = 0;

// ===== Init =====
async function init() {
  if (adminKey) {
    try {
      const r = await api('GET', '/admin/projects');
      if (r.ok) { showApp(); allProjects = r.projects; await refreshAll(); return; }
    } catch (e) {}
    doLogout();
  } else { showLogin(); }
}
init();

// ===== Auth =====
function showLogin() { $('loginPage').style.display = 'flex'; $('appPage').style.display = 'none'; }
function showApp() { $('loginPage').style.display = 'none'; $('appPage').style.display = 'block'; }

async function doLogin() {
  const key = $('adminKeyInput').value.trim();
  if (!key) { toast('请输入管理员密钥', 'error'); return; }
  adminKey = key;
  try {
    const r = await api('GET', '/admin/projects');
    if (!r.ok) { toast('密钥无效，请重新输入', 'error'); adminKey = ''; return; }
    localStorage.setItem('adminKey', adminKey);
    allProjects = r.projects;
    showApp();
    await refreshAll();
    toast('登录成功', 'success');
  } catch (e) { toast('无法连接服务器', 'error'); adminKey = ''; }
}

function doLogout() { adminKey = ''; localStorage.removeItem('adminKey'); showLogin(); }

// ===== API Helper =====
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json', 'X-Admin-Key': adminKey } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API + path, opts);
  if (r.status === 403) { toast('登录已过期', 'error'); doLogout(); throw new Error('403'); }
  return await r.json();
}

// ===== Navigation =====
function switchPage(page) {
  document.querySelectorAll('.page').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  $('page-' + page).style.display = 'block';
  document.querySelector(`.nav-item[data-page="${page}"]`).classList.add('active');
  if (page === 'licenses') loadLicenses();
  if (page === 'projects') loadProjects();
}

// ===== Refresh All =====
async function refreshAll() {
  try {
    const [pRes, lRes] = await Promise.all([api('GET', '/admin/projects'), api('GET', '/admin/licenses')]);
    if (pRes.ok) allProjects = pRes.projects;
    if (lRes.ok) allLicenses = lRes.licenses;
    renderStats();
    renderDashTable();
    updateNavCounts();
    updateProjectSelects();
  } catch (e) {}
}

// ===== Stats =====
function renderStats() {
  const total = allLicenses.length;
  const active = allLicenses.filter(l => l.is_active && !isExpired(l)).length;
  const expired = allLicenses.filter(l => isExpired(l)).length;
  const devices = allLicenses.reduce((s, l) => s + l.current_devices, 0);
  $('statsGrid').innerHTML = `
    <div class="stat"><div class="label"><i data-lucide="key-round" style="width:14px;height:14px;color:var(--primary)"></i> 总卡密数</div><div class="val c1">${total}</div></div>
    <div class="stat"><div class="label"><i data-lucide="circle-check" style="width:14px;height:14px;color:var(--success)"></i> 有效</div><div class="val c2">${active}</div></div>
    <div class="stat"><div class="label"><i data-lucide="clock-alert" style="width:14px;height:14px;color:var(--warning)"></i> 已过期</div><div class="val c3">${expired}</div></div>
    <div class="stat"><div class="label"><i data-lucide="monitor-smartphone" style="width:14px;height:14px;color:var(--info)"></i> 在线设备</div><div class="val c4">${devices}</div></div>`;
  refreshIcons();
}

function renderDashTable() {
  const list = allLicenses.slice(0, 10);
  $('dashTable').innerHTML = list.length ? list.map(l => licRow(l, true)).join('') : '<tr><td colspan="6" class="empty">暂无卡密，请先创建项目并生成卡密</td></tr>';
  refreshIcons();
}

// ===== Licenses =====
async function loadLicenses() {
  const proj = $('licProjectFilter') ? $('licProjectFilter').value : '';
  let url = '/admin/licenses?page=' + licPage + '&page_size=20';
  if (proj) url += '&project=' + proj;
  try {
    const r = await api('GET', url);
    if (!r.ok) return;
    allLicenses = r.licenses;
    licTotal = r.total || 0;
    licTotalPages = r.total_pages || 1;
    $('licTable').innerHTML = r.licenses.length ? r.licenses.map(l => licRow(l, false)).join('') : '<tr><td colspan="8" class="empty">暂无卡密</td></tr>';
    renderPagination();
    updateNavCounts();
    refreshIcons();
  } catch (e) {}
}

function renderPagination() {
  let el = $('licPagination');
  if (!el) return;
  if (licTotalPages <= 1) { el.innerHTML = `<span style="color:var(--text3);font-size:13px">共 ${licTotal} 条</span>`; return; }
  let html = `<span style="color:var(--text3);font-size:13px;margin-right:12px">共 ${licTotal} 条</span>`;
  html += `<button class="btn btn-ghost btn-sm" ${licPage <= 1 ? 'disabled style="opacity:.4;pointer-events:none"' : ''} onclick="licGoPage(${licPage - 1})">上一页</button>`;
  // 页码
  const maxShow = 5;
  let start = Math.max(1, licPage - Math.floor(maxShow / 2));
  let end = Math.min(licTotalPages, start + maxShow - 1);
  if (end - start < maxShow - 1) start = Math.max(1, end - maxShow + 1);
  if (start > 1) html += `<button class="btn btn-ghost btn-sm" onclick="licGoPage(1)">1</button>`;
  if (start > 2) html += `<span style="color:var(--text3);padding:0 4px">...</span>`;
  for (let i = start; i <= end; i++) {
    html += `<button class="btn ${i === licPage ? 'btn-primary' : 'btn-ghost'} btn-sm" onclick="licGoPage(${i})">${i}</button>`;
  }
  if (end < licTotalPages - 1) html += `<span style="color:var(--text3);padding:0 4px">...</span>`;
  if (end < licTotalPages) html += `<button class="btn btn-ghost btn-sm" onclick="licGoPage(${licTotalPages})">${licTotalPages}</button>`;
  html += `<button class="btn btn-ghost btn-sm" ${licPage >= licTotalPages ? 'disabled style="opacity:.4;pointer-events:none"' : ''} onclick="licGoPage(${licPage + 1})">下一页</button>`;
  el.innerHTML = html;
}

function licGoPage(p) {
  licPage = p;
  loadLicenses();
}

function licRow(l, short) {
  const k = l.key;
  const cols = short ? `
    <td><code class="mono" style="cursor:pointer" title="点击复制完整卡密" data-key="${k}" onclick="copyText(this.dataset.key)">${mask(k)}</code></td>
    <td><span class="badge badge-project">${l.project_code}</span></td>
    <td>${statusBadge(l)}</td>
    <td><span style="cursor:pointer;color:var(--primary)" data-key="${k}" onclick="showDevices(this.dataset.key)">${l.current_devices}/${l.max_devices}</span></td>
    <td style="font-size:12px">${fmtDate(l.expires_at)}</td>
    <td>${actionBtns(l)}</td>` : `
    <td><code class="mono" style="cursor:pointer" title="点击复制完整卡密" data-key="${k}" onclick="copyText(this.dataset.key)">${mask(k)}</code></td>
    <td><span class="badge badge-project">${l.project_code}</span></td>
    <td style="color:var(--text3);font-size:12px">${l.note || '-'}</td>
    <td>${statusBadge(l)}</td>
    <td><span style="cursor:pointer;color:var(--primary)" data-key="${k}" onclick="showDevices(this.dataset.key)">${l.current_devices}/${l.max_devices}</span></td>
    <td style="font-size:12px">${fmtDate(l.expires_at)}</td>
    <td class="hide-m" style="font-size:12px;color:var(--text3)">${fmtDate(l.created_at)}</td>
    <td>${actionBtns(l)}</td>`;
  return '<tr>' + cols + '</tr>';
}

function statusBadge(l) {
  if (!l.is_active) return '<span class="badge badge-revoked">已吊销</span>';
  if (isExpired(l)) return '<span class="badge badge-expired">已过期</span>';
  return '<span class="badge badge-active">有效</span>';
}

function actionBtns(l) {
  const k = l.key;
  const revokeOrEnable = l.is_active
    ? `<button class="btn btn-danger btn-sm" data-key="${k}" onclick="doRevoke(this.dataset.key)"><i data-lucide="ban" style="width:13px;height:13px"></i> 吊销</button>`
    : `<button class="btn btn-success btn-sm" data-key="${k}" onclick="doActivate(this.dataset.key)"><i data-lucide="check-circle" style="width:13px;height:13px"></i> 启用</button>`;
  const del = `<button class="btn btn-ghost btn-sm" data-key="${k}" onclick="doDelete(this.dataset.key)" style="color:var(--danger)"><i data-lucide="trash-2" style="width:13px;height:13px"></i></button>`;
  return `<div style="display:flex;gap:4px">${revokeOrEnable}${del}</div>`;
}

async function doRevoke(key) {
  if (!confirm('确定吊销此卡密？所有绑定设备将被清除。')) return;
  try { const r = await api('POST', '/admin/revoke', { license_key: key }); if (r.ok) { toast('已吊销', 'success'); refreshAll(); loadLicenses(); } else toast(r.detail || '操作失败', 'error'); } catch (e) {}
}

async function doActivate(key) {
  try { const r = await api('POST', '/admin/activate', { license_key: key }); if (r.ok) { toast('已启用', 'success'); refreshAll(); loadLicenses(); } else toast(r.detail || '操作失败', 'error'); } catch (e) {}
}

async function doDelete(key) {
  if (!confirm('确定彻底删除此卡密？此操作不可恢复！')) return;
  try { const r = await api('POST', '/admin/delete', { license_key: key }); if (r.ok) { toast('已删除', 'success'); refreshAll(); loadLicenses(); } else toast(r.detail || '删除失败', 'error'); } catch (e) {}
}

// ===== Create License =====
async function doCreateLicense() {
  const project_code = $('cLicProject').value;
  if (!project_code) { toast('请选择所属项目', 'error'); return; }
  const count = parseInt($('cLicCount').value) || 1;
  const max_devices = parseInt($('cLicDevices').value) || 1;
  const expires_days = parseInt($('cLicDays').value) || null;
  const note = $('cLicNote').value.trim();
  try {
    const r = await api('POST', '/admin/licenses', { project_code, count, max_devices, expires_days, note });
    if (!r.ok) { toast(r.detail || '生成失败', 'error'); return; }
    closeModal('createLicModal');
    $('cLicResult').innerHTML = '';
    toast(`成功生成 ${r.licenses.length} 个卡密`, 'success');
    // 弹出复制窗口
    showKeysModal(r.licenses);
    refreshAll();
  } catch (e) {}
}

// ===== Projects =====
async function loadProjects() {
  try {
    const r = await api('GET', '/admin/projects');
    if (!r.ok) return;
    allProjects = r.projects;
    renderProjects();
    updateNavCounts();
  } catch (e) {}
}

function renderProjects() {
  const grid = $('projectGrid');
  if (!allProjects.length) { grid.innerHTML = '<div class="empty" style="grid-column:1/-1">暂无项目，点击上方按钮创建</div>'; return; }
  grid.innerHTML = allProjects.map(p => `
    <div class="project-card">
      <div class="head">
        <h4><i data-lucide="folder-open" style="width:16px;height:16px;color:var(--primary);vertical-align:-2px"></i> ${p.name}</h4>
        <span class="badge ${p.is_active ? 'badge-active' : 'badge-revoked'}">${p.is_active ? '启用' : '停用'}</span>
      </div>
      <div class="desc">${p.description || '暂无描述'}</div>
      <div class="meta">
        <span>标识: <b>${p.code}</b></span>
        <span>卡密数: <b>${p.license_count}</b></span>
        <span>有效: <b>${p.active_license_count}</b></span>
      </div>
      <div class="actions">
        ${p.is_active ? `<button class="btn btn-danger btn-sm" onclick="toggleProject('${p.code}',false)"><i data-lucide="pause-circle" style="width:13px;height:13px"></i> 停用</button>` : `<button class="btn btn-success btn-sm" onclick="toggleProject('${p.code}',true)"><i data-lucide="play-circle" style="width:13px;height:13px"></i> 启用</button>`}
        <button class="btn btn-ghost btn-sm" onclick="if(confirm('确定删除项目 ${p.code} 及其所有卡密？'))deleteProject('${p.code}')"><i data-lucide="trash-2" style="width:13px;height:13px"></i> 删除</button>
      </div>
    </div>`).join('');
  refreshIcons();
}

async function doCreateProject() {
  const code = $('cProjCode').value.trim();
  const name = $('cProjName').value.trim();
  const description = $('cProjDesc').value.trim();
  if (!code || !name) { toast('项目标识和名称为必填项', 'error'); return; }
  try {
    const r = await api('POST', '/admin/projects', { code, name, description });
    if (r.ok) { toast('项目已创建', 'success'); closeModal('createProjModal'); $('cProjCode').value = ''; $('cProjName').value = ''; $('cProjDesc').value = ''; await refreshAll(); loadProjects(); } else toast(r.detail || '创建失败', 'error');
  } catch (e) {}
}

async function toggleProject(code, active) {
  try { const r = await api('PUT', '/admin/projects/' + code, { is_active: active }); if (r.ok) { toast(active ? '已启用' : '已停用', 'success'); loadProjects(); } } catch (e) {}
}

async function deleteProject(code) {
  try { const r = await api('DELETE', '/admin/projects/' + code); if (r.ok) { toast('已删除', 'success'); loadProjects(); refreshAll(); } else toast(r.detail || '删除失败', 'error'); } catch (e) {}
}

// ===== Devices =====
async function showDevices(key) {
  $('devModalKey').textContent = mask(key);
  $('deviceList').innerHTML = '<div class="empty">加载中...</div>';
  openModal('deviceModal');
  try {
    const r = await api('GET', '/admin/devices/' + key);
    if (!r.ok || !r.devices.length) { $('deviceList').innerHTML = '<div class="empty">暂无绑定设备</div>'; return; }
    $('deviceList').innerHTML = r.devices.map(d => `
      <div class="device-item">
        <div class="device-meta">
          <div class="name"><i data-lucide="${d.is_online ? 'monitor-check' : 'monitor-x'}" style="width:14px;height:14px;vertical-align:-2px"></i> ${d.device_info || '未知设备'} <span class="badge ${d.is_online ? 'badge-online' : 'badge-offline'}">${d.is_online ? '在线' : '离线'}</span></div>
          <div class="sub">${d.device_id}</div>
          <div class="sub">最后心跳: ${fmtDate(d.last_heartbeat)}</div>
        </div>
        <button class="btn btn-danger btn-sm" data-key="${key}" data-did="${d.device_id}" onclick="doUnbind(this.dataset.key,this.dataset.did)"><i data-lucide="unlink" style="width:13px;height:13px"></i> 解绑</button>
      </div>`).join('');
    refreshIcons();
  } catch (e) { $('deviceList').innerHTML = '<div class="empty" style="color:var(--danger)">网络错误</div>'; }
}

async function doUnbind(key, did) {
  if (!confirm('确定解绑此设备？')) return;
  try { const r = await api('POST', '/admin/unbind', { license_key: key, device_id: did }); if (r.ok) { toast('已解绑', 'success'); showDevices(key); refreshAll(); } } catch (e) {}
}

// ===== Keys Result Modal =====
function showKeysModal(keys) {
  const html = `<div class="copy-box" id="generatedKeys">${keys.join('\n')}</div>
    <button class="btn btn-primary btn-sm" style="margin-top:10px" onclick="copyText(document.getElementById('generatedKeys').textContent)"><i data-lucide="copy" style="width:13px;height:13px"></i> 复制全部</button>`;
  $('deviceList').innerHTML = html;
  $('devModalKey').textContent = `共 ${keys.length} 个`;
  openModal('deviceModal');
  refreshIcons();
}

// ===== Helpers =====
function $(id) { return document.getElementById(id); }
function mask(k) { return k.slice(0, 8) + '····' + k.slice(-4); }
function isExpired(l) { return l.expires_at && new Date(l.expires_at) < new Date(); }
function refreshIcons() { if (window.lucide) lucide.createIcons(); }
function fmtDate(iso) {
  if (!iso) return '<span style="color:var(--text3)">永久</span>';
  const d = new Date(iso);
  return d.toLocaleDateString('zh-CN') + ' ' + d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}
function openModal(id) { $(id).classList.add('show'); }
function closeModal(id) { $(id).classList.remove('show'); }
function copyText(t) { navigator.clipboard.writeText(t).then(() => toast('已复制', 'success')).catch(() => toast('复制失败', 'error')); }
function toast(msg, type) {
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2500);
}
function updateNavCounts() {
  $('navLicCount').textContent = licTotal || allLicenses.length;
  $('navProjCount').textContent = allProjects.length;
}
function updateProjectSelects() {
  const opts = allProjects.map(p => `<option value="${p.code}">${p.name} (${p.code})</option>`).join('');
  $('cLicProject').innerHTML = '<option value="">-- 请选择 --</option>' + opts;
  const filter = $('licProjectFilter');
  if (filter) filter.innerHTML = '<option value="">全部项目</option>' + opts;
}
