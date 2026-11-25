const API = location.origin;
let allProjects = [];
let allLicenses = [];
let dashboardLicenses = [];
let licPage = 1;
let licTotalPages = 1;
let licTotal = 0;
let licenseRequestId = 0;
let auditPage = 1;
let auditTotalPages = 1;
let auditTotal = 0;
let heartbeatPage = 1;
let heartbeatTotalPages = 1;
let heartbeatTotal = 0;
let heartbeatRefreshTimer = null;
let createLicenseSubmitting = false;
let createLicenseRequestId = null;
let generatedKeysCopied = false;
let activeModalId = null;
let lastFocusedElement = null;
let dashboardStats = {
  total_licenses: 0,
  active_licenses: 0,
  expired_licenses: 0,
  revoked_licenses: 0,
  bound_devices: 0,
};

const PAGE_META = {
  dashboard: {
    title: '仪表盘',
    sub: '授权池状态分布、项目配额与最近签发记录',
    cta: 'both',
  },
  licenses: {
    title: '卡密管理',
    sub: '按项目、状态或编号检索卡密，支持复制完整密钥与管理绑定设备',
    cta: 'lic',
  },
  projects: {
    title: '项目配置',
    sub: '隔离各业务线的授权命名空间与全局启停开关',
    cta: 'proj',
  },
  heartbeats: {
    title: '心跳与风控',
    sub: '实时追踪客户端验证流水与连续错误自动锁定记录（每 10 秒自动刷新）',
    cta: 'hb',
  },
  audit: {
    title: '调用审计',
    sub: '记录管理员对项目、卡密和设备绑定的安全变更流水',
    cta: 'audit',
  },
};

// ===== Theme & Drawer (video2api Style B Dark Permanent Default) =====
function initTheme() {
  try {
    const saved = localStorage.getItem('v2a_license_theme_v2') || 'dark';
    applyTheme(saved);
  } catch (e) {
    applyTheme('dark');
  }
}

function applyTheme(theme) {
  document.documentElement?.classList?.toggle('dark', theme !== 'light');
}

function toggleTheme() {
  const next = document.documentElement.classList.contains('dark') ? 'light' : 'dark';
  applyTheme(next);
  try {
    localStorage.setItem('v2a_license_theme_v2', next);
  } catch (e) {}
}

function togglePasswordVisibility() {
  const input = $('adminKeyInput');
  const icon = $('pwdEyeIcon');
  if (!input) return;
  const isPwd = input.type === 'password';
  input.type = isPwd ? 'text' : 'password';
  if (icon) {
    icon.setAttribute('data-lucide', isPwd ? 'eye-off' : 'eye');
    refreshIcons();
  }
}

function toggleDrawer(open) {
  $('appSidebar')?.classList.toggle('open', Boolean(open));
  $('drawerMask')?.classList.toggle('open', Boolean(open));
}

// ===== Init =====
initTheme();

async function init() {
  loadVersion();
  try {
    const r = await api('GET', '/admin/me');
    if (r.ok) {
      showApp();
      syncIslandHeader('dashboard');
      await refreshAll();
      return;
    }
  } catch (e) {}
  showLogin();
}
init();

// ===== Auth =====
function showLogin() {
  $('loginPage').style.display = 'flex';
  $('appPage').style.display = 'none';
}

function showApp() {
  $('loginPage').style.display = 'none';
  $('appPage').style.display = 'block';
}

async function doLogin() {
  const key = $('adminKeyInput').value.trim();
  if (!key) {
    toast('请输入管理员密码', 'error');
    $('adminKeyInput')?.focus();
    return;
  }
  const btn = $('loginSubmitBtn');
  if (btn) btn.disabled = true;
  try {
    const r = await api('POST', '/admin/login', { admin_key: key });
    if (!r.ok) {
      toast(r.detail || '密钥无效，请重新输入', 'error');
      $('adminKeyInput')?.focus();
      return;
    }
    showApp();
    syncIslandHeader(document.querySelector('.nav-item.active')?.dataset.page || 'dashboard');
    await refreshAll();
    toast('已登录控制台', 'success');
    $('adminKeyInput').value = '';
  } catch (e) {
    toast('无法连接服务器', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function doLogout() {
  try {
    const r = await api('POST', '/admin/logout');
    if (r.ok) {
      if (heartbeatRefreshTimer) {
        clearInterval(heartbeatRefreshTimer);
        heartbeatRefreshTimer = null;
      }
      showLogin();
    } else {
      toast(r.detail || '退出失败，请重试', 'error');
    }
  } catch (e) {
    showMutationError(e, '退出失败，请重试');
  }
}

// ===== API Helper =====
async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  const csrf = document.cookie
    .split('; ')
    .find(v => v.startsWith('csrf_token='))
    ?.split('=')
    .slice(1)
    .join('=');
  if (csrf && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers['X-CSRF-Token'] = decodeURIComponent(csrf);
  }
  const opts = { method, headers, credentials: 'same-origin' };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API + path, opts);
  if (
    (r.status === 401 || r.status === 403) &&
    !path.startsWith('/admin/login') &&
    path !== '/admin/me'
  ) {
    toast('登录已过期', 'error');
    showLogin();
    throw new Error(String(r.status));
  }
  const data = await r.json();
  if (!r.ok && Array.isArray(data.detail)) {
    data.detail = data.detail.map(e => e.msg || '参数错误').join('；');
  }
  return data;
}

// ===== Unified Island Header & Navigation =====
function syncIslandHeader(page) {
  const meta = PAGE_META[page] || PAGE_META.dashboard;
  if ($('islandTitle')) $('islandTitle').textContent = meta.title;
  if ($('mobilePageTitle')) $('mobilePageTitle').textContent = meta.title;
  if ($('islandSubtitle')) $('islandSubtitle').textContent = meta.sub;
  const actions = $('islandActions');
  if (!actions) return;
  if (meta.cta === 'lic') {
    actions.innerHTML = `
      <button type="button" class="btn btn-secondary btn-sm" onclick="loadLicenses()"><i data-lucide="refresh-cw" style="width:13.5px;height:13.5px"></i> 刷新</button>
      <button type="button" class="btn btn-primary btn-sm" onclick="openModal('createLicModal')"><i data-lucide="plus" style="width:14px;height:14px"></i> 生成卡密</button>`;
  } else if (meta.cta === 'proj') {
    actions.innerHTML = `
      <button type="button" class="btn btn-secondary btn-sm" onclick="loadProjects()"><i data-lucide="refresh-cw" style="width:13.5px;height:13.5px"></i> 刷新</button>
      <button type="button" class="btn btn-primary btn-sm" onclick="openModal('createProjModal')"><i data-lucide="plus" style="width:14px;height:14px"></i> 新建项目</button>`;
  } else if (meta.cta === 'hb') {
    actions.innerHTML = `<button type="button" class="btn btn-secondary btn-sm" onclick="loadHeartbeatLogs()"><i data-lucide="refresh-cw" style="width:13.5px;height:13.5px"></i> 刷新</button>`;
  } else if (meta.cta === 'audit') {
    actions.innerHTML = `<button type="button" class="btn btn-secondary btn-sm" onclick="loadAudit()"><i data-lucide="refresh-cw" style="width:13.5px;height:13.5px"></i> 刷新</button>`;
  } else {
    actions.innerHTML = `
      <button type="button" class="btn btn-secondary btn-sm" onclick="refreshAll()"><i data-lucide="refresh-cw" style="width:13.5px;height:13.5px"></i> 刷新</button>
      <button type="button" class="btn btn-secondary btn-sm" onclick="openModal('createProjModal')"><i data-lucide="folder-plus" style="width:13.5px;height:13.5px"></i> 新建项目</button>
      <button type="button" class="btn btn-primary btn-sm" onclick="openModal('createLicModal')"><i data-lucide="plus" style="width:14px;height:14px"></i> 生成卡密</button>`;
  }
  refreshIcons();
}

function switchPage(page) {
  toggleDrawer(false);
  if (heartbeatRefreshTimer) {
    clearInterval(heartbeatRefreshTimer);
    heartbeatRefreshTimer = null;
  }
  document.querySelectorAll('.page').forEach(p => (p.style.display = 'none'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const target = $('page-' + page);
  if (target) target.style.display = 'block';
  document.querySelector(`.nav-item[data-page="${page}"]`)?.classList.add('active');
  syncIslandHeader(page);

  if (page === 'licenses') loadLicenses();
  if (page === 'projects') loadProjects();
  if (page === 'audit') loadAudit();
  if (page === 'heartbeats') {
    loadHeartbeatLogs();
    heartbeatRefreshTimer = setInterval(() => {
      if (document.querySelector('.nav-item.active')?.dataset.page === 'heartbeats') {
        loadHeartbeatLogs(true);
      }
    }, 10000);
  }
}

// ===== Refresh All =====
async function refreshAll() {
  try {
    const [pRes, lRes, sRes] = await Promise.all([
      api('GET', '/admin/projects'),
      api('GET', '/admin/licenses?page=1&page_size=20'),
      api('GET', '/admin/stats'),
    ]);
    if (!pRes.ok || !lRes.ok || !sRes.ok) throw new Error('获取数据失败');
    allProjects = pRes.projects;
    dashboardLicenses = lRes.licenses;
    licTotal = lRes.total || 0;
    licTotalPages = lRes.total_pages || 1;
    dashboardStats = sRes;
    renderStats(dashboardStats);
    renderDashTable();
    renderPoolDistribution(dashboardStats);
    updateNavCounts();
    updateProjectSelects();
    renderProjects();
    loadVersion();
    const activePage = document.querySelector('.nav-item.active')?.dataset.page;
    if (activePage === 'licenses') return await loadLicenses();
    if (activePage === 'audit') return await loadAudit();
    if (activePage === 'heartbeats') return await loadHeartbeatLogs(true);
    return true;
  } catch (e) {
    if (e.message !== '401' && e.message !== '403') toast('数据刷新失败，请重试', 'error');
    return false;
  }
}

async function loadVersion() {
  try {
    const r = await fetch(API + '/api/version');
    const d = await r.json();
    if (d.version) {
      if ($('appVersion')) $('appVersion').textContent = 'v' + d.version;
      if ($('loginVersionBadge')) $('loginVersionBadge').textContent = 'v' + d.version;
    }
  } catch (e) {}
}

// ===== KPI Stats & 40-Stripe Distribution (1:1 video2api DashboardMetric + Pool Stripes) =====
function renderStats(stats = dashboardStats) {
  const total = Number(stats.total_licenses || 0);
  const active = Number(stats.active_licenses || 0);
  const expired = Number(stats.expired_licenses || 0);
  const revoked = Number(stats.revoked_licenses || 0);
  const devices = Number(stats.bound_devices || 0);
  const activePct = total > 0 ? ((active / total) * 100).toFixed(1) : '0.0';

  $('statsGrid').innerHTML = `
    <article class="v2a-surface metric-tile">
      <header class="metric-head"><span>卡密池总数</span><i data-lucide="key-round"></i></header>
      <div class="metric-val">${total}</div>
      <p class="metric-detail">有效 ${active} · 过期 ${expired} · 吊销 ${revoked}</p>
    </article>
    <article class="v2a-surface metric-tile">
      <header class="metric-head"><span>有效授权</span><i data-lucide="activity"></i></header>
      <div class="metric-val">${active}</div>
      <p class="metric-detail">授权可用率 ${activePct}%</p>
    </article>
    <article class="v2a-surface metric-tile">
      <header class="metric-head"><span>在线设备</span><i data-lucide="monitor-smartphone"></i></header>
      <div class="metric-val">${devices}</div>
      <p class="metric-detail">周期心跳实时存活绑定</p>
    </article>
    <article class="v2a-surface metric-tile">
      <header class="metric-head"><span>已过期卡密</span><i data-lucide="clock"></i></header>
      <div class="metric-val">${expired}</div>
      <p class="metric-detail">到期自动拦截客户端验证</p>
    </article>
    <article class="v2a-surface metric-tile">
      <header class="metric-head"><span>已吊销 / 项目数</span><i data-lucide="gauge"></i></header>
      <div class="metric-val">${revoked}</div>
      <p class="metric-detail">已启用项目 ${allProjects.filter(p => p.is_active).length} / 共 ${allProjects.length} 个</p>
    </article>`;
  refreshIcons();
}

function renderPoolDistribution(stats = dashboardStats) {
  const total = Number(stats.total_licenses || 0);
  const active = Number(stats.active_licenses || 0);
  const expired = Number(stats.expired_licenses || 0);
  const revoked = Number(stats.revoked_licenses || 0);
  const availability = total > 0 ? Math.round((active / total) * 100) : 0;

  if ($('dashAvailabilityPct')) $('dashAvailabilityPct').textContent = `${availability}%`;

  const segments = [
    { key: 'active', label: '有效授权', count: active, cls: 'stripe-active', dot: '#10B981', desc: '可正常通过签名验证与心跳续期' },
    { key: 'expired', label: '自然过期', count: expired, cls: 'stripe-expired', dot: 'var(--quota-3)', desc: '超过有效期，自动拒绝客户端请求' },
    { key: 'revoked', label: '手动吊销', count: revoked, cls: 'stripe-revoked', dot: 'var(--destructive)', desc: '管理员人工封禁并清空绑定设备' },
  ];

  const STRIPE_COUNT = 40;
  const bar = $('dashStripeBar');
  if (bar) {
    if (total <= 0) {
      bar.innerHTML = Array.from({ length: STRIPE_COUNT }, () => `<span class="stripe-seg"></span>`).join('');
    } else {
      const boundaries = [];
      let acc = 0;
      for (const item of segments) {
        acc += item.count;
        boundaries.push(acc / total);
      }
      const bars = Array.from({ length: STRIPE_COUNT }, (_, idx) => {
        const pos = (idx + 0.5) / STRIPE_COUNT;
        const foundIdx = boundaries.findIndex(b => pos <= b);
        const chosen = segments[foundIdx >= 0 ? foundIdx : segments.length - 1];
        const cls = chosen && chosen.count > 0 ? chosen.cls : '';
        const title = chosen && chosen.count > 0 ? `${chosen.label} (${chosen.count})` : '无数据';
        return `<span class="stripe-seg ${cls}" title="${escapeHtml(title)}"></span>`;
      });
      bar.innerHTML = bars.join('');
    }
  }

  const legend = $('dashPoolLegend');
  if (legend) {
    const projRow = {
      label: '关联业务项目',
      count: allProjects.length,
      share: allProjects.length > 0 ? `${allProjects.filter(p => p.is_active).length} 启用` : '0 启用',
      dot: 'var(--quota-1)',
      desc: '独立命名空间与项目级启停控制',
    };
    const rowsHtml = segments
      .map(seg => {
        const share = total > 0 ? ((seg.count / total) * 100).toFixed(1) + '%' : '0.0%';
        return `
        <div class="pool-legend-row">
          <div class="pool-legend-left">
            <span class="legend-dot" style="background:${seg.dot}"></span>
            <div style="min-width:0">
              <p style="font-size:12px;font-weight:500">${seg.label}</p>
              <p style="font-size:11px;color:var(--muted-foreground);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${seg.desc}</p>
            </div>
          </div>
          <div style="text-align:right;flex-shrink:0;font-variant-numeric:tabular-nums">
            <p style="font-size:12px;font-weight:500">${seg.count}</p>
            <p style="font-size:11px;color:var(--muted-foreground);margin-top:2px">${share}</p>
          </div>
        </div>`;
      })
      .join('');

    const projHtml = `
      <div class="pool-legend-row">
        <div class="pool-legend-left">
          <span class="legend-dot" style="background:${projRow.dot}"></span>
          <div style="min-width:0">
            <p style="font-size:12px;font-weight:500">${projRow.label}</p>
            <p style="font-size:11px;color:var(--muted-foreground);margin-top:2px">${projRow.desc}</p>
          </div>
        </div>
        <div style="text-align:right;flex-shrink:0;font-variant-numeric:tabular-nums">
          <p style="font-size:12px;font-weight:500">${projRow.count}</p>
          <p style="font-size:11px;color:var(--muted-foreground);margin-top:2px">${projRow.share}</p>
        </div>
      </div>`;

    legend.innerHTML = rowsHtml + projHtml;
  }
}

function renderDashTable() {
  const list = dashboardLicenses.slice(0, 8);
  $('dashTable').innerHTML = list.length
    ? list.map(l => licRow(l, true)).join('')
    : '<tr><td colspan="6" class="empty">暂无卡密，请先创建项目并生成卡密</td></tr>';
  refreshIcons();
}

// ===== Licenses =====
async function loadLicenses() {
  const requestId = ++licenseRequestId;
  const proj = $('licProjectFilter') ? $('licProjectFilter').value : '';
  const search = $('licSearchInput').value.trim();
  const status = $('licStatusFilter').value;
  let url = '/admin/licenses?page=' + licPage + '&page_size=20';
  if (proj) url += '&project=' + encodeURIComponent(proj);
  if (search) url += '&search=' + encodeURIComponent(search);
  if (status) url += '&status=' + encodeURIComponent(status);
  $('licTable').setAttribute('aria-busy', 'true');
  $('licTable').innerHTML = '<tr><td colspan="8" class="empty">加载中…</td></tr>';
  try {
    const r = await api('GET', url);
    if (requestId !== licenseRequestId) return false;
    if (!r.ok) throw new Error(r.detail || '查询失败');
    allLicenses = r.licenses;
    licTotal = r.total || 0;
    licTotalPages = r.total_pages || 1;
    if (licPage > licTotalPages) {
      licPage = licTotalPages;
      return loadLicenses();
    }
    $('licTable').innerHTML = r.licenses.length
      ? r.licenses.map(l => licRow(l, false)).join('')
      : '<tr><td colspan="8" class="empty">暂无符合筛选条件的卡密</td></tr>';
    $('licTable').setAttribute('aria-busy', 'false');
    renderPagination();
    updateNavCounts();
    refreshIcons();
    return true;
  } catch (e) {
    if (requestId !== licenseRequestId) return false;
    $('licTable').setAttribute('aria-busy', 'false');
    if (e.message !== '401' && e.message !== '403') {
      $('licTable').innerHTML = '<tr><td colspan="8" class="empty">查询失败，请点击「查询」重试</td></tr>';
      toast('卡密查询失败，请重试', 'error');
    }
    return false;
  }
}

function applyLicenseFilters() {
  licPage = 1;
  return loadLicenses();
}

function resetLicenseFilters() {
  $('licSearchInput').value = '';
  $('licProjectFilter').value = '';
  $('licStatusFilter').value = '';
  return applyLicenseFilters();
}

function renderPagerMarkup(total, page, totalPages, goFnName, extraMeta = '') {
  const metaText = `共 ${total} 条${totalPages > 1 ? ` · 第 ${page} / ${totalPages} 页` : ''}${extraMeta}`;
  if (totalPages <= 1) {
    return `<span style="color:var(--muted-foreground);font-size:12px">${metaText}</span><div></div>`;
  }
  const maxShow = 5;
  let start = Math.max(1, page - Math.floor(maxShow / 2));
  let end = Math.min(totalPages, start + maxShow - 1);
  if (end - start < maxShow - 1) start = Math.max(1, end - maxShow + 1);

  let btns = `<button class="btn btn-secondary btn-sm" ${page <= 1 ? 'disabled style="opacity:.45;pointer-events:none"' : ''} onclick="${goFnName}(${page - 1})">上一页</button>`;
  if (start > 1) btns += `<button class="btn btn-ghost btn-sm" onclick="${goFnName}(1)">1</button>`;
  if (start > 2) btns += `<span style="color:var(--muted-foreground);padding:0 4px">…</span>`;
  for (let i = start; i <= end; i++) {
    btns += `<button class="btn ${i === page ? 'btn-primary' : 'btn-ghost'} btn-sm" onclick="${goFnName}(${i})">${i}</button>`;
  }
  if (end < totalPages - 1) btns += `<span style="color:var(--muted-foreground);padding:0 4px">…</span>`;
  if (end < totalPages) btns += `<button class="btn btn-ghost btn-sm" onclick="${goFnName}(${totalPages})">${totalPages}</button>`;
  btns += `<button class="btn btn-secondary btn-sm" ${page >= totalPages ? 'disabled style="opacity:.45;pointer-events:none"' : ''} onclick="${goFnName}(${page + 1})">下一页</button>`;

  return `<span style="color:var(--muted-foreground);font-size:12px">${metaText}</span><div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">${btns}</div>`;
}

function renderPagination() {
  const el = $('licPagination');
  if (el) el.innerHTML = renderPagerMarkup(licTotal, licPage, licTotalPages, 'licGoPage');
}

function licGoPage(p) {
  licPage = p;
  loadLicenses();
}

function licRow(l, short) {
  const k = String(l.key || '');
  const safeKey = escapeHtml(k);
  const fullKey = l.full_key || (l.is_legacy ? k : '');
  const widthStyle = short ? 'min-width:190px;max-width:270px' : 'min-width:240px;max-width:360px';
  const keyCell = `<td style="${widthStyle}">
    <div style="display:flex;align-items:center;gap:6px">
      <code class="mono key-chip">${escapeHtml(fullKey || k)}</code>
      ${
        fullKey
          ? `<button class="copy-btn" data-key="${escapeHtml(fullKey)}" onclick="copyText(this.dataset.key)" title="复制完整卡密" aria-label="复制完整卡密"><i data-lucide="copy" style="width:13px;height:13px"></i></button>`
          : `<span title="${escapeHtml(l.display_error || '旧卡密仅保存摘要，无法还原')}" style="font-size:11px;color:var(--muted-foreground);white-space:nowrap">仅编号</span>`
      }
    </div>
  </td>`;
  const projCell = `<td><span class="badge badge-project" title="${escapeHtml(l.project_code)}">${escapeHtml(l.project_code)}</span></td>`;
  const devBtn = `<td><button class="device-link" data-key="${safeKey}" onclick="showDevices(this.dataset.key)" aria-label="查看卡密 ${safeKey} 的绑定设备">${l.current_devices} / ${l.max_devices}</button></td>`;

  const cols = short
    ? `${keyCell}${projCell}<td>${statusBadge(l)}</td>${devBtn}<td style="font-size:12px">${expiryText(l)}</td><td>${actionBtns(l)}</td>`
    : `${keyCell}${projCell}<td style="color:var(--muted-foreground);font-size:12px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(l.note || '')}">${escapeHtml(l.note || '—')}</td><td>${statusBadge(l)}</td>${devBtn}<td style="font-size:12px">${expiryText(l)}</td><td class="hide-m" style="font-size:12px;color:var(--muted-foreground)">${fmtDate(l.created_at)}</td><td>${actionBtns(l)}</td>`;
  return '<tr>' + cols + '</tr>';
}

function statusBadge(l) {
  if (!l.is_active) return '<span class="badge badge-revoked">已吊销</span>';
  if (l.duration_days && !l.activated_at && !l.expires_at) return '<span class="badge badge-pending">待激活</span>';
  if (isExpired(l)) return '<span class="badge badge-expired">已过期</span>';
  return '<span class="badge badge-active">有效</span>';
}

function expiryText(l) {
  if (l.duration_days && !l.activated_at && !l.expires_at) {
    return `<span class="badge badge-pending" title="首次激活后开始计时">待激活 · ${l.duration_days}天</span>`;
  }
  return fmtDate(l.expires_at);
}

function actionBtns(l) {
  const k = String(l.key || '');
  const safeKey = escapeHtml(k);
  const revokeOrEnable = l.is_active
    ? `<button class="btn btn-ghost btn-sm icon-action" data-key="${safeKey}" onclick="doRevoke(this.dataset.key)" title="吊销" aria-label="吊销卡密 ${safeKey}"><i data-lucide="shield-off" style="width:13.5px;height:13.5px"></i></button>`
    : `<button class="btn btn-success btn-sm icon-action" data-key="${safeKey}" onclick="doActivate(this.dataset.key)" title="启用" aria-label="启用卡密 ${safeKey}"><i data-lucide="rotate-ccw" style="width:13.5px;height:13.5px"></i></button>`;
  const edit = `<button class="btn btn-ghost btn-sm icon-action" data-key="${safeKey}" data-note="${escapeHtml(l.note || '')}" data-max="${escapeHtml(l.max_devices)}" data-expires="${escapeHtml(l.expires_at || '')}" onclick="openEditModal(this)" title="编辑" aria-label="编辑卡密 ${safeKey}"><i data-lucide="pencil" style="width:13.5px;height:13.5px"></i></button>`;
  const del = `<button class="btn btn-ghost btn-sm icon-action" data-key="${safeKey}" onclick="doDelete(this.dataset.key)" title="删除" aria-label="删除卡密 ${safeKey}" style="color:var(--destructive)"><i data-lucide="trash-2" style="width:13.5px;height:13.5px"></i></button>`;
  return `<div style="display:flex;gap:4px">${revokeOrEnable}${edit}${del}</div>`;
}

// ===== License Mutations =====
function openEditModal(el) {
  $('editLicKey').value = el.dataset.key;
  $('editLicNote').value = el.dataset.note;
  $('editLicDevices').value = el.dataset.max;
  openModal('editLicModal');
}

async function doEditLicense() {
  const key = $('editLicKey').value;
  const note = $('editLicNote').value.trim();
  const max_devices = parseInt($('editLicDevices').value, 10) || 1;
  try {
    const r = await api('POST', '/admin/update', { license_key: key, note, max_devices });
    if (r.ok) {
      toast('已保存', 'success');
      closeModal('editLicModal');
      await refreshAll();
    } else {
      toast(r.detail || '更新失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '更新失败，请重试');
  }
}

async function doRevoke(key) {
  const ok = await v2aConfirm({
    title: '吊销卡密',
    message: `确定吊销卡密「${mask(key)}」？吊销后所有已绑定设备将立即清除。`,
    confirmText: '确认吊销',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api('POST', '/admin/revoke', { license_key: key });
    if (r.ok) {
      toast('已吊销', 'success');
      await refreshAll();
    } else {
      toast(r.detail || '吊销失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '吊销失败，请重试');
  }
}

async function doActivate(key) {
  try {
    const r = await api('POST', '/admin/activate', { license_key: key });
    if (r.ok) {
      toast('已启用', 'success');
      await refreshAll();
    } else {
      toast(r.detail || '启用失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '启用失败，请重试');
  }
}

async function doDelete(key) {
  const ok = await v2aConfirm({
    title: '删除卡密',
    message: `确定彻底删除卡密「${mask(key)}」？此操作不可恢复。`,
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api('POST', '/admin/delete', { license_key: key });
    if (r.ok) {
      toast('已删除', 'success');
      await refreshAll();
    } else {
      toast(r.detail || '删除失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '删除失败，请重试');
  }
}

// ===== Create License =====
async function doCreateLicense() {
  if (createLicenseSubmitting) return;
  const project_code = $('cLicProject').value;
  if (!project_code) {
    toast('请选择所属项目', 'error');
    return;
  }
  const count = parseInt($('cLicCount').value, 10) || 1;
  const max_devices = parseInt($('cLicDevices').value, 10) || 1;
  const expires_days = parseInt($('cLicDays').value, 10) || null;
  const starts_on_first_activation = $('cLicStartOnActivation').checked;
  const note = $('cLicNote').value.trim();
  if (starts_on_first_activation && !expires_days) {
    toast('首次激活计时需要填写有效期', 'error');
    return;
  }
  const submitButton = $('createLicSubmit');
  const request_id = createLicenseRequestId || (createLicenseRequestId = newRequestId());
  createLicenseSubmitting = true;
  submitButton.disabled = true;
  submitButton.textContent = '生成中…';
  try {
    const r = await api('POST', '/admin/licenses', {
      project_code,
      count,
      max_devices,
      expires_days,
      starts_on_first_activation,
      note,
      request_id,
    });
    if (!r.ok) {
      toast(r.detail || '生成失败', 'error');
      return;
    }
    closeModal('createLicModal', true);
    resetCreateLicenseForm();
    $('licProjectFilter').value = project_code;
    $('licSearchInput').value = '';
    $('licStatusFilter').value = '';
    licPage = 1;
    toast(`成功生成 ${r.licenses.length} 个卡密`, 'success');
    showKeysModal(r.licenses);
    await refreshAll();
  } catch (e) {
    if (e.message !== '401' && e.message !== '403') {
      toast('请求结果未确认。请先查询卡密列表，勿立即重复生成。', 'error');
    }
  } finally {
    createLicenseSubmitting = false;
    submitButton.disabled = false;
    submitButton.textContent = '立即生成';
  }
}

function resetCreateLicenseForm() {
  createLicenseRequestId = null;
  $('cLicProject').value = '';
  $('cLicCount').value = '1';
  $('cLicDevices').value = '1';
  $('cLicDays').value = '';
  $('cLicStartOnActivation').checked = false;
  $('cLicNote').value = '';
  $('cLicResult').innerHTML = '';
}

// ===== Projects =====
async function loadProjects() {
  try {
    const r = await api('GET', '/admin/projects');
    if (!r.ok) {
      toast(r.detail || '项目加载失败，请重试', 'error');
      return false;
    }
    allProjects = r.projects;
    renderProjects();
    updateNavCounts();
    return true;
  } catch (e) {
    showMutationError(e, '项目加载失败，请重试');
    return false;
  }
}

function renderProjects() {
  const grid = $('projectGrid');
  if (!grid) return;
  if (!allProjects.length) {
    grid.innerHTML = '<div class="empty v2a-surface" style="grid-column:1/-1">暂无项目，点击右上角「新建项目」创建</div>';
    return;
  }
  grid.innerHTML = allProjects
    .map(
      p => `
    <div class="project-card v2a-surface">
      <div>
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:8px">
          <div style="min-width:0">
            <h4 style="font-size:14.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.name)}</h4>
            <span class="badge badge-project" style="margin-top:5px">${escapeHtml(p.code)}</span>
          </div>
          <span class="badge ${p.is_active ? 'badge-active' : 'badge-revoked'}">${p.is_active ? '已启用' : '已禁用'}</span>
        </div>
        <p style="font-size:12px;color:var(--muted-foreground);line-height:1.5;min-height:18px">${escapeHtml(p.description || '—')}</p>
      </div>
      <div>
        <div class="meta">
          <span>团队<b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.team || '—')}</b></span>
          <span>卡密总数<b>${Number(p.license_count || 0)}</b></span>
          <span>有效授权<b>${Number(p.active_license_count || 0)}</b></span>
        </div>
        <div class="actions" style="margin-top:12px">
          ${
            p.is_active
              ? `<button class="btn btn-danger btn-sm" onclick="toggleProject(${escapeHtml(jsString(p.code))},false)">停用</button>`
              : `<button class="btn btn-success btn-sm" onclick="toggleProject(${escapeHtml(jsString(p.code))},true)">启用</button>`
          }
          <button class="btn btn-secondary btn-sm" data-code="${escapeHtml(p.code)}" data-name="${escapeHtml(p.name)}" data-team="${escapeHtml(p.team || '')}" data-description="${escapeHtml(p.description || '')}" onclick="openEditProject(this)">编辑</button>
          <button class="btn btn-ghost btn-sm icon-action" style="margin-left:auto;color:var(--destructive)" data-code="${escapeHtml(p.code)}" data-count="${p.license_count}" onclick="confirmProjectDelete(this.dataset.code, Number(this.dataset.count))" title="删除项目"><i data-lucide="trash-2" style="width:13.5px;height:13.5px"></i></button>
        </div>
      </div>
    </div>`
    )
    .join('');
  refreshIcons();
}

async function doCreateProject() {
  const code = $('cProjCode').value.trim();
  const name = $('cProjName').value.trim();
  const team = $('cProjTeam').value.trim();
  const description = $('cProjDesc').value.trim();
  if (!code || !name) {
    toast('项目标识和名称为必填项', 'error');
    return;
  }
  try {
    const r = await api('POST', '/admin/projects', { code, name, team, description });
    if (r.ok) {
      toast('项目已创建', 'success');
      closeModal('createProjModal');
      $('cProjCode').value = '';
      $('cProjName').value = '';
      $('cProjTeam').value = '';
      $('cProjDesc').value = '';
      await refreshAll();
    } else {
      toast(r.detail || '创建失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '创建项目失败，请重试');
  }
}

function openEditProject(el) {
  $('editProjCode').value = el.dataset.code;
  $('editProjCodeDisplay').value = el.dataset.code;
  $('editProjName').value = el.dataset.name;
  $('editProjTeam').value = el.dataset.team;
  $('editProjDesc').value = el.dataset.description;
  openModal('editProjModal');
}

async function doEditProject() {
  const code = $('editProjCode').value;
  const name = $('editProjName').value.trim();
  const team = $('editProjTeam').value.trim();
  const description = $('editProjDesc').value.trim();
  if (!name) {
    toast('项目名称不能为空', 'error');
    return;
  }
  try {
    const r = await api('PUT', '/admin/projects/' + encodeURIComponent(code), { name, team, description });
    if (r.ok) {
      toast('项目已更新', 'success');
      closeModal('editProjModal');
      await refreshAll();
    } else {
      toast(r.detail || '更新失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '更新项目失败，请重试');
  }
}

async function toggleProject(code, active) {
  try {
    const r = await api('PUT', '/admin/projects/' + encodeURIComponent(code), { is_active: active });
    if (r.ok) {
      toast(active ? '已启用' : '已停用', 'success');
      await refreshAll();
    } else {
      toast(r.detail || '项目状态更新失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '项目状态更新失败，请重试');
  }
}

function confirmProjectDelete(code, count) {
  const warning = count
    ? `该项目下有 ${count} 张卡密，删除项目会同时删除这些卡密和设备绑定。`
    : '该项目下暂无卡密。';
  if (!document.createElement && typeof prompt === 'function') {
    const answer = prompt(`${warning}\n请输入项目标识「${code}」确认删除：`);
    if (answer === code) deleteProject(code, count);
    else if (answer !== null) toast('项目标识不匹配，已取消删除', 'error');
    return;
  }
  v2aConfirm({
    title: '删除项目',
    message: `${warning}请输入项目标识「${code}」确认删除：`,
    confirmText: '确认删除',
    danger: true,
    requireInput: code,
  }).then(typed => {
    if (typed === null) return;
    if (typed !== code) {
      toast('项目标识不匹配，已取消删除', 'error');
      return;
    }
    deleteProject(code, count);
  });
}

async function deleteProject(code, expectedCount) {
  try {
    const r = await api(
      'DELETE',
      '/admin/projects/' + encodeURIComponent(code) + '?expected_count=' + encodeURIComponent(expectedCount)
    );
    if (r.ok) {
      toast('已删除', 'success');
      await refreshAll();
    } else {
      toast(r.detail || '删除失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '删除项目失败，请刷新后重试');
  }
}

// ===== Audit =====
const auditLabels = {
  'project.create': '创建项目',
  'project.update': '更新项目',
  'project.delete': '删除项目',
  'license.create': '生成卡密',
  'license.update': '更新卡密',
  'license.revoke': '吊销卡密',
  'license.activate': '启用卡密',
  'license.delete': '删除卡密',
  'device.unbind': '解绑设备',
  'license.lock': '自动锁定卡密',
  'license.unlock': '解锁卡密',
};

async function loadAudit() {
  $('auditTable').setAttribute('aria-busy', 'true');
  $('auditTable').innerHTML = '<tr><td colspan="4" class="empty">加载中…</td></tr>';
  try {
    const r = await api('GET', '/admin/audit?page=' + auditPage + '&page_size=30');
    if (!r.ok) throw new Error(r.detail || '加载失败');
    auditTotal = r.total || 0;
    auditTotalPages = r.total_pages || 1;
    if (auditPage > auditTotalPages) {
      auditPage = auditTotalPages;
      return loadAudit();
    }
    $('auditTable').innerHTML = r.items.length
      ? r.items
          .map(
            item => `<tr>
          <td style="font-size:12px;color:var(--muted-foreground)">${fmtDate(item.created_at)}</td>
          <td><span class="badge badge-project">${escapeHtml(auditLabels[item.action] || item.action)}</span></td>
          <td><code class="mono key-chip">${escapeHtml(item.target)}</code></td>
          <td class="mono" style="font-size:12px;color:var(--muted-foreground)">${escapeHtml(item.actor_ip || '—')}</td>
        </tr>`
          )
          .join('')
      : '<tr><td colspan="4" class="empty">暂无数据</td></tr>';
    $('auditTable').setAttribute('aria-busy', 'false');
    renderAuditPagination();
    refreshIcons();
    return true;
  } catch (e) {
    $('auditTable').setAttribute('aria-busy', 'false');
    $('auditTable').innerHTML = '<tr><td colspan="4" class="empty">加载失败，请点击「刷新」重试</td></tr>';
    showMutationError(e, '审计记录加载失败，请重试');
    return false;
  }
}

function renderAuditPagination() {
  const el = $('auditPagination');
  if (el) el.innerHTML = renderPagerMarkup(auditTotal, auditPage, auditTotalPages, 'auditGoPage');
}

function auditGoPage(page) {
  auditPage = page;
  loadAudit();
}

// ===== Heartbeat Logs & Blacklist =====
async function loadHeartbeatLogs(silent = false) {
  const table = $('heartbeatTable');
  if (!silent) table.innerHTML = '<tr><td colspan="8" class="empty">加载中…</td></tr>';
  const params = new URLSearchParams({
    page: String(heartbeatPage),
    page_size: '50',
  });
  const device = $('heartbeatDeviceInput')?.value.trim();
  const ip = $('heartbeatIpInput')?.value.trim();
  const eventType = $('heartbeatEventFilter')?.value || '';
  if (device) params.set('device_id', device);
  if (ip) params.set('client_ip', ip);
  if (eventType) params.set('event_type', eventType);
  try {
    const r = await api('GET', '/admin/heartbeat-logs?' + params.toString());
    if (!r.ok) throw new Error(r.detail || '加载失败');
    heartbeatTotal = r.total || 0;
    heartbeatTotalPages = r.total_pages || 1;
    if (heartbeatPage > heartbeatTotalPages) {
      heartbeatPage = heartbeatTotalPages;
      return loadHeartbeatLogs(silent);
    }
    const onlineCount = Number(r.online_device_count || 0);
    dashboardStats.bound_devices = onlineCount;
    updateNavCounts();
    renderHeartbeatSummary(r.summary || [], onlineCount, Number(r.online_timeout_seconds || 0));
    renderClientBlacklist(r.blacklist || []);

    table.innerHTML = r.items.length
      ? r.items
          .map(
            item => `<tr>
          <td><span class="badge badge-project project-code" title="${escapeHtml(item.project_code || '-')}">${escapeHtml(item.project_code || '-')}</span></td>
          <td><span class="mono cell-nowrap" title="${escapeHtml(item.license_key || '-')}">${escapeHtml(item.license_key || '-')}</span></td>
          <td><span class="badge ${item.event_type === 'heartbeat' ? 'badge-offline' : 'badge-pending'}">${item.event_type === 'heartbeat' ? '心跳' : '首次验证'}</span></td>
          <td><span class="badge ${item.ok ? 'badge-online' : 'badge-revoked'}" title="${escapeHtml(item.ok ? '成功' : item.error || '失败')}" aria-label="${escapeHtml(item.ok ? '成功' : '失败：' + (item.error || '未知原因'))}">${item.ok ? '成功' : '失败'}</span></td>
          <td><span class="mono cell-nowrap" title="${escapeHtml(item.client_ip || '-')}">${escapeHtml(item.client_ip || '-')}</span></td>
          <td><span class="cell-device" title="${escapeHtml(item.device_info || '-')}">${escapeHtml(item.device_info || '-')}</span></td>
          <td><span class="cell-nowrap" title="${escapeHtml(fmtDate(item.created_at))}">${fmtDate(item.created_at)}</span></td>
          <td><span class="cell-device" title="${escapeHtml(item.license_note || '-')}">${escapeHtml(item.license_note || '-')}</span></td>
        </tr>`
          )
          .join('')
      : '<tr><td colspan="8" class="empty">暂无心跳日志</td></tr>';
    renderHeartbeatPagination();
    refreshIcons();
    return true;
  } catch (e) {
    if (!silent) table.innerHTML = '<tr><td colspan="8" class="empty">加载失败，请点击「刷新」重试</td></tr>';
    if (!silent) showMutationError(e, '心跳日志加载失败，请重试');
    return false;
  }
}

function renderClientBlacklist(items) {
  const el = $('blacklistTable');
  const badge = $('blacklistCountBadge');
  if (badge) {
    badge.textContent = String(items.length);
    badge.style.display = items.length ? 'inline-flex' : 'none';
  }
  if (!el) return;
  el.innerHTML = items.length
    ? items
        .map(
          item => `<tr>
      <td><code class="mono key-chip">${escapeHtml(item.license_identifier)}</code></td>
      <td><span class="badge badge-project">${escapeHtml(item.project_code || '—')}</span></td>
      <td style="color:var(--muted-foreground);font-size:12px">${escapeHtml(item.license_note || '—')}</td>
      <td><span class="badge badge-revoked">${Number(item.failed_attempts || 0)}</span></td>
      <td style="font-size:12px;color:var(--muted-foreground)">${fmtDate(item.blocked_at)}</td>
      <td><button class="btn btn-secondary btn-sm" data-license="${escapeHtml(item.license_identifier)}" onclick="unblockClient(this.dataset.license)">解锁</button></td>
    </tr>`
        )
        .join('')
    : '<tr><td colspan="6" class="empty" style="padding:22px">暂无锁定卡密</td></tr>';
}

async function unblockClient(licenseIdentifier) {
  const ok = await v2aConfirm({
    title: '解锁卡密',
    message: `确认解锁卡密「${licenseIdentifier}」？该卡密的连续错误计数将清零，设备码不受影响。`,
    confirmText: '确认解锁',
    danger: false,
  });
  if (!ok) return;
  try {
    const r = await api('POST', '/admin/blacklist/unblock', { license_identifier: licenseIdentifier });
    if (!r.ok) {
      toast(r.detail || '解除失败', 'error');
      return;
    }
    toast('卡密已解锁', 'success');
    await loadHeartbeatLogs(true);
  } catch (e) {
    showMutationError(e, '解除失败，请重试');
  }
}

function renderHeartbeatSummary(items, onlineCount, timeoutSeconds) {
  const el = $('heartbeatSummary');
  const meta = $('heartbeatSummaryMeta');
  if (!el) return;
  if (meta) meta.textContent = `当前在线 ${onlineCount} 台 · ${timeoutSeconds} 秒无心跳视为离线`;
  el.innerHTML = items.length
    ? items
        .map(
          item => `<div class="heartbeat-summary-item">
        <div style="display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(item.project_code || '—')}">
          <i data-lucide="box" style="width:13px;height:13px;color:var(--muted-foreground);flex-shrink:0"></i>
          <span>${escapeHtml(item.project_code || '—')}</span>
        </div>
        <div style="font-size:11px;color:var(--muted-foreground);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(item.license_note || '—')}">备注：${escapeHtml(item.license_note || '—')}</div>
        <div class="count">${Number(item.count || 0)} 台</div>
      </div>`
        )
        .join('')
    : '<div class="empty" style="grid-column:1/-1;padding:18px">暂无在线设备</div>';
  refreshIcons();
}

function renderHeartbeatPagination() {
  const el = $('heartbeatPagination');
  if (el) el.innerHTML = renderPagerMarkup(heartbeatTotal, heartbeatPage, heartbeatTotalPages, 'heartbeatGoPage', ' · 10s 自动刷新');
}

function heartbeatGoPage(page) {
  heartbeatPage = page;
  loadHeartbeatLogs();
}

function applyHeartbeatFilters() {
  heartbeatPage = 1;
  loadHeartbeatLogs();
}

function resetHeartbeatFilters() {
  $('heartbeatDeviceInput').value = '';
  $('heartbeatIpInput').value = '';
  $('heartbeatEventFilter').value = '';
  heartbeatPage = 1;
  loadHeartbeatLogs();
}

// ===== Devices Modal =====
async function showDevices(key) {
  $('devModalTitle').textContent = '绑定设备';
  $('devModalKey').textContent = mask(key);
  $('deviceList').innerHTML = '<div class="empty">加载中…</div>';
  openModal('deviceModal');
  try {
    const r = await api('GET', '/admin/devices/' + encodeURIComponent(key));
    if (!r.ok) throw new Error(r.detail || '加载设备失败');
    if (!r.devices.length) {
      $('deviceList').innerHTML = '<div class="empty">暂无绑定设备</div>';
      return;
    }
    $('deviceList').innerHTML = r.devices
      .map(
        d => `
      <div class="device-item">
        <div style="min-width:0;flex:1">
          <div style="display:flex;align-items:center;gap:7px;font-size:13px;font-weight:500">
            <i data-lucide="${d.is_online ? 'monitor-check' : 'monitor'}" style="width:14px;height:14px;flex-shrink:0"></i>
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(d.device_info || '未知设备')}</span>
            <span class="badge ${d.is_online ? 'badge-online' : 'badge-offline'}">${d.is_online ? '在线' : '离线'}</span>
          </div>
          <div class="mono" style="font-size:11.5px;color:var(--muted-foreground);margin-top:4px;word-break:break-all">${escapeHtml(d.device_id)}</div>
          <div style="font-size:11px;color:var(--muted-foreground);margin-top:2px">最后 IP: ${escapeHtml(d.last_ip || '—')} · 心跳: ${fmtDate(d.last_heartbeat)}</div>
        </div>
        <button class="btn btn-danger btn-sm" data-key="${escapeHtml(key)}" data-did="${escapeHtml(d.device_id)}" onclick="doUnbind(this.dataset.key,this.dataset.did)">解绑</button>
      </div>`
      )
      .join('');
    refreshIcons();
  } catch (e) {
    $('deviceList').innerHTML = `<div class="empty" style="color:var(--destructive)">${escapeHtml(errorText(e, '加载设备失败，请重试'))}</div>`;
  }
}

async function doUnbind(key, did) {
  const ok = await v2aConfirm({
    title: '解绑设备',
    message: `确定解绑设备「${did}」？`,
    confirmText: '确认解绑',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api('POST', '/admin/unbind', { license_key: key, device_id: did });
    if (r.ok) {
      toast('已解绑', 'success');
      await showDevices(key);
      await refreshAll();
    } else {
      toast(r.detail || '解绑失败', 'error');
    }
  } catch (e) {
    showMutationError(e, '解绑失败，请重试');
  }
}

// ===== Generated Keys Result Modal =====
function showKeysModal(keys) {
  const html = `
    <p style="font-size:12px;color:var(--muted-foreground);line-height:1.5">卡密已加密保存，后续可在卡密列表随时查看和复制。</p>
    <div class="copy-box" id="generatedKeys">${keys.map(escapeHtml).join('\n')}</div>
    <button class="btn btn-primary btn-sm" style="margin-top:12px" onclick="copyGeneratedKeys()"><i data-lucide="copy" style="width:13px;height:13px"></i> 复制全部</button>`;
  generatedKeysCopied = false;
  $('deviceList').innerHTML = html;
  $('devModalTitle').textContent = '生成成功';
  $('devModalKey').textContent = `共 ${keys.length} 个`;
  openModal('deviceModal');
  refreshIcons();
}

// ===== video2api Style AlertDialog =====
function v2aConfirm({ title, message, confirmText = '确认', danger = false, requireInput = null }) {
  if (!document.createElement) {
    if (requireInput && typeof prompt === 'function') return Promise.resolve(prompt(message));
    if (!requireInput && typeof confirm === 'function') return Promise.resolve(confirm(message));
  }
  return new Promise(resolve => {
    const mask = document.createElement('div');
    mask.className = 'modal-mask show';
    mask.style.zIndex = '220';
    mask.innerHTML = `
      <div class="modal v2a-island" role="alertdialog" aria-modal="true" style="max-width:410px">
        <h2 style="margin-bottom:8px">${escapeHtml(title)}</h2>
        <p style="font-size:12.5px;color:var(--muted-foreground);line-height:1.6;margin-bottom:${requireInput ? '12px' : '4px'}">${escapeHtml(message)}</p>
        ${requireInput ? `<input class="input mono" type="text" id="v2aPromptInput" placeholder="${escapeHtml(requireInput)}" autocomplete="off">` : ''}
        <div class="btn-row" style="margin-top:18px">
          <button type="button" class="btn btn-secondary btn-sm" data-act="cancel">取消</button>
          <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'} btn-sm" data-act="ok">${escapeHtml(confirmText)}</button>
        </div>
      </div>`;
    document.body.appendChild(mask);

    const inputEl = mask.querySelector('#v2aPromptInput');
    const okBtn = mask.querySelector('[data-act="ok"]');
    const cancelBtn = mask.querySelector('[data-act="cancel"]');
    setTimeout(() => (inputEl || okBtn)?.focus(), 0);

    const cleanup = val => {
      document.removeEventListener('keydown', onKey, true);
      mask.remove();
      resolve(val);
    };
    const onKey = e => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        cleanup(requireInput ? null : false);
      } else if (e.key === 'Enter' && requireInput) {
        e.preventDefault();
        cleanup(inputEl.value.trim());
      }
    };
    document.addEventListener('keydown', onKey, true);
    mask.addEventListener('click', e => {
      if (e.target === mask) cleanup(requireInput ? null : false);
    });
    cancelBtn.onclick = () => cleanup(requireInput ? null : false);
    okBtn.onclick = () => cleanup(requireInput ? inputEl.value.trim() : true);
  });
}

// ===== Helpers & Modal Focus Trap =====
function $(id) {
  return document.getElementById(id);
}

function jsString(value) {
  return JSON.stringify(String(value == null ? '' : value))
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026');
}

function escapeHtml(value) {
  return String(value == null ? '' : value).replace(
    /[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function mask(k) {
  return k.length > 12 ? k.slice(0, 8) + '····' + k.slice(-4) : k;
}

function isExpired(l) {
  return l.expires_at && new Date(l.expires_at) < new Date();
}

function refreshIcons() {
  if (window.lucide) lucide.createIcons();
}

function newRequestId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'req_' + Date.now() + '_' + Math.random().toString(36).slice(2);
}

function errorText(error, fallback) {
  return error && !(error instanceof TypeError) && error.message && !['401', '403'].includes(error.message)
    ? error.message
    : fallback;
}

function showMutationError(error, fallback) {
  if (error && ['401', '403'].includes(error.message)) return;
  toast(errorText(error, fallback), 'error');
}

function fmtDate(iso) {
  if (!iso) return '<span style="color:var(--muted-foreground)">永久</span>';
  const d = new Date(iso);
  return (
    d.toLocaleDateString('zh-CN') +
    ' ' +
    d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  );
}

function openModal(id) {
  if (id === 'createLicModal') resetCreateLicenseForm();
  const mask = $(id);
  if (!mask) return;
  lastFocusedElement = document.activeElement;
  activeModalId = id;
  $('appPage').inert = true;
  mask.classList.add('show');
  mask.setAttribute('aria-hidden', 'false');
  const dialog = mask.querySelector('.modal');
  if (dialog) {
    setTimeout(() => {
      if (activeModalId !== id) return;
      const first = dialog.querySelector('input,select,button,textarea,[tabindex]:not([tabindex="-1"])');
      (first || dialog).focus();
    }, 0);
  }
}

function closeModal(id, force = false) {
  if (id === 'createLicModal' && createLicenseSubmitting && !force) {
    toast('正在生成卡密，请稍候', 'error');
    return;
  }
  const mask = $(id);
  if (!mask) return;
  mask.classList.remove('show');
  mask.setAttribute('aria-hidden', 'true');
  if (activeModalId === id) {
    activeModalId = null;
    $('appPage').inert = false;
    if (lastFocusedElement && document.contains(lastFocusedElement)) lastFocusedElement.focus();
  }
}

document.addEventListener('keydown', event => {
  if (!activeModalId || document.querySelector('[role="alertdialog"]')) return;
  const mask = $(activeModalId);
  const dialog = mask && mask.querySelector('.modal');
  if (!dialog) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeModal(activeModalId);
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = [
    ...dialog.querySelectorAll(
      'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
    ),
  ];
  if (!focusable.length) {
    event.preventDefault();
    dialog.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!dialog.contains(document.activeElement)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
    return;
  }
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

function copyText(t) {
  navigator.clipboard
    .writeText(t)
    .then(() => toast('已复制', 'success'))
    .catch(() => toast('复制失败', 'error'));
}

function copyGeneratedKeys() {
  const keys = $('generatedKeys');
  if (!keys) return;
  navigator.clipboard
    .writeText(keys.textContent)
    .then(() => {
      generatedKeysCopied = true;
      toast('已复制全部卡密', 'success');
    })
    .catch(() => toast('复制失败，请手动保存完整卡密', 'error'));
}

function toast(msg, type) {
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2500);
}

function updateNavCounts() {
  if ($('navLicCount')) $('navLicCount').textContent = dashboardStats.total_licenses || 0;
  if ($('navProjCount')) $('navProjCount').textContent = allProjects.length;
  if ($('navOnlineCount')) $('navOnlineCount').textContent = dashboardStats.bound_devices || 0;
}

function updateProjectSelects() {
  const opts = allProjects
    .map(p => `<option value="${escapeHtml(p.code)}">${escapeHtml(p.name)} (${escapeHtml(p.code)})</option>`)
    .join('');
  const selectedProject = $('cLicProject').value;
  $('cLicProject').innerHTML = '<option value="">-- 请选择 --</option>' + opts;
  if ([...$('cLicProject').options].some(o => o.value === selectedProject)) {
    $('cLicProject').value = selectedProject;
  }
  const filter = $('licProjectFilter');
  if (filter) {
    const selectedFilter = filter.value;
    filter.innerHTML = '<option value="">全部项目</option>' + opts;
    if ([...filter.options].some(o => o.value === selectedFilter)) {
      filter.value = selectedFilter;
    }
  }
}
