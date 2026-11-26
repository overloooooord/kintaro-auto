let STATE = {
  accounts: [],
  active_jobs: [],
  watchdog: { enabled: false, cycles: 0, threads: 5 },
  unban_queue: null,
  goal_autopilot: null,
  logs: [],
};
let ACTIVE_VIEW = 'accounts';
let ACTIVE_FILTER = 'all';
let DRAWER_ID = null;

const SELECTED = new Set();
const UP_PICK = new Set();
const UNBAN_PICK = new Set();
const AP_MEDIA_PICK = new Set();
const UP_LIB_PICK = new Set();
let MEDIA_LIBRARY = [];
let UP_FILES = [];
let LAST_SHADOW_ROWS = [];
let previewTimer = null;

const VIEW_META = {
  accounts: {
    title: 'Аккаунты',
    sub: 'Здоровье профилей, входы и массовые действия — в одном спокойном месте.',
  },
  autopilot: {
    title: 'Автопилот 600',
    sub: 'Автономный залив, снятие волн 2048, мониторинг за $0 и умный долив до цели.',
  },
  upload: {
    title: 'Залив',
    sub: 'Кампании с уникализацией, рандомизацией тегов, музыкой и холодным рестартом.',
  },
  unban: {
    title: 'Ограны 2048',
    sub: 'Многопоточная очередь дожима волн ограничений и сортировка по папкам.',
  },
  import: {
    title: 'Импорт',
    sub: 'Пакетное добавление аккаунтов с авто-входом и проверкой прокси.',
  },
  tools: {
    title: 'Инструменты',
    sub: 'Выгрузка постов и проверка тени, прокси-чекер, настройки и утилиты.',
  },
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtDur(sec) {
  const s = Math.max(0, Math.floor(Number(sec || 0)));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const p = n => String(n).padStart(2, '0');
  return h > 0 ? `${p(h)}:${p(m)}:${p(r)}` : `${p(m)}:${p(r)}`;
}

async function copyText(str, okMsg = 'Скопировано') {
  const val = String(str ?? '');
  if (!val.trim()) return toast('Список пуст', 'warn');
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(val);
      toast(okMsg, 'success');
      return true;
    }
  } catch (_) {}
  try {
    const ta = document.createElement('textarea');
    ta.value = val;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    if (ok) { toast(okMsg, 'success'); return true; }
  } catch (_) {}
  toast('Не удалось скопировать автоматически', 'warn');
  return false;
}

function toast(msg, type = 'info') {
  const stack = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (type === 'success' ? 'ok' : type);
  el.textContent = msg;
  stack.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .2s';
    setTimeout(() => el.remove(), 220);
  }, 3600);
}

async function apiJson(url, opts = {}) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}

async function apiPost(url, body = {}) {
  return apiJson(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// ── Theme Toggle (Soft Light default + Optional Warm Dark) ───
function applyTheme(theme) {
  const dark = theme === 'dark';
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  const btn = document.getElementById('btnTheme');
  if (btn) btn.textContent = dark ? '☀️' : '🌙';
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  localStorage.setItem('kintaro_theme', next);
  applyTheme(next);
}

// ── Navigation ───────────────────────────────────────────────
function switchView(name) {
  ACTIVE_VIEW = name;
  Object.keys(VIEW_META).forEach(v => {
    document.getElementById('view-' + v).classList.toggle('hidden', v !== name);
  });
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  const meta = VIEW_META[name] || VIEW_META.accounts;
  document.getElementById('topTitle').textContent = meta.title;
  document.getElementById('heroTitle').textContent = meta.title;
  document.getElementById('heroSub').textContent = meta.sub;

  if (name === 'autopilot') { loadMediaLibrary(); renderAutopilotView(); }
  if (name === 'upload') { loadMediaLibrary(); renderUploadPicker(); }
  if (name === 'unban') renderUnbanView();
  if (name === 'tools') { loadConfig(); loadLastShadow(); }
}

function openSettingsQuick() {
  switchView('tools');
  const cfgBtn = document.querySelector('#view-tools .pill:nth-child(3)');
  if (cfgBtn) switchSubTool('config', cfgBtn);
}

function switchSubTool(name, btn) {
  ['shadow', 'proxy', 'config', 'utils'].forEach(k => {
    document.getElementById('sub-' + k).classList.toggle('hidden', k !== name);
  });
  document.querySelectorAll('#view-tools .pill').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  if (name === 'config') loadConfig();
  if (name === 'shadow') loadLastShadow();
}

function setFilter(f, btn) {
  ACTIVE_FILTER = f;
  document.querySelectorAll('#view-accounts .pill').forEach(p => p.classList.toggle('active', p.dataset.f === f));
  document.querySelectorAll('#view-accounts .kpi-card').forEach(k => k.classList.toggle('active', k.dataset.kpi === f));
  renderAccounts();
}

function jumpFilter(f) {
  switchView('accounts');
  ACTIVE_FILTER = f === 'authed' ? 'authed' : f;
  const pillKey = ACTIVE_FILTER === 'authed' ? 'all' : ACTIVE_FILTER;
  document.querySelectorAll('#view-accounts .pill').forEach(p => p.classList.toggle('active', p.dataset.f === pillKey));
  document.querySelectorAll('#view-accounts .kpi-card').forEach(k => k.classList.toggle('active', k.dataset.kpi === f));
  renderAccounts();
}

async function syncNow() {
  await Promise.all([fetchState(), loadMediaLibrary()]);
  toast('Состояние синхронизировано', 'success');
}

// ── State Sync ───────────────────────────────────────────────
async function fetchState() {
  try {
    STATE = await apiJson('/api/state');
    document.getElementById('connDot').className = 'dot on';
    document.getElementById('connBadge').textContent = 'LIVE';
    renderAll();
  } catch (_) {
    document.getElementById('connDot').className = 'dot warn';
    document.getElementById('connBadge').textContent = 'ОФЛАЙН';
  }
}

function statusBadge(a) {
  if (!a.authenticated) return '<span class="badge b-dim">Без входа</span>';
  if (!a.audit) return '<span class="badge b-cyan">Вход OK</span>';
  const h = a.audit.account_health;
  if (h === 'eligible') return '<span class="badge b-ok">Чистый</span>';
  if (h === 'appealing') return '<span class="badge b-warn">Проверка</span>';
  return '<span class="badge b-err">Огран</span>';
}

function renderAll() {
  const accs = STATE.accounts || [];
  let auth = 0, clean = 0, app = 0, act = 0;
  accs.forEach(a => {
    if (a.authenticated) auth++;
    if (a.audit) {
      if (a.audit.account_health === 'eligible') clean++;
      else if (a.audit.account_health === 'appealing') app++;
      else if (a.audit.account_health === 'action_required') act++;
    }
  });

  document.getElementById('navAccCnt').textContent = accs.length;
  const ogPill = document.getElementById('navOgranCnt');
  ogPill.textContent = act;
  ogPill.classList.toggle('hidden', act === 0);

  // Autopilot sidebar badge
  const ga = STATE.goal_autopilot || {};
  const apPill = document.getElementById('navAutoPill');
  if (ga.running) {
    apPill.textContent = `LIVE · ${ga.counts?.goal_reached || 0}/${ga.counts?.total || 0}`;
    apPill.className = 'nav-pill pulse mono';
  } else if ((ga.counts?.goal_reached || 0) > 0) {
    apPill.textContent = `${ga.counts.goal_reached} ✓`;
    apPill.className = 'nav-pill sage mono';
  } else {
    apPill.textContent = String(ga.config?.target_views || 600);
    apPill.className = 'nav-pill sage mono';
  }

  document.getElementById('stTotal').textContent = accs.length;
  document.getElementById('stAuth').textContent = auth;
  document.getElementById('stClean').textContent = clean;
  document.getElementById('stApp').textContent = app;
  document.getElementById('stAct').textContent = act;

  const hint = document.getElementById('defProxyHint');
  if (hint) hint.textContent = STATE.default_proxy_masked || 'не задан';

  // Job pill
  const jobs = STATE.active_jobs || [];
  const jp = document.getElementById('jobPill');
  const jDot = document.getElementById('jobDot');
  const jSpin = document.getElementById('jobSpin');
  if (jobs.length) {
    const j0 = jobs[0];
    document.getElementById('jobText').textContent = `${j0.label} (${j0.done}/${j0.total})`;
    jp.classList.add('active');
    jDot.classList.add('hidden');
    jSpin.classList.remove('hidden');
  } else if (ga.running) {
    document.getElementById('jobText').textContent = `Автопилот · 600+: ${ga.counts?.goal_reached || 0}/${ga.counts?.total || 0}`;
    jp.classList.add('active');
    jDot.classList.add('hidden');
    jSpin.classList.remove('hidden');
  } else {
    document.getElementById('jobText').textContent = 'Задач нет';
    jp.classList.remove('active');
    jDot.classList.remove('hidden');
    jSpin.classList.add('hidden');
  }

  // Watchdog switch card
  const wd = STATE.watchdog || {};
  const wdSw = document.getElementById('wdSwitch');
  const wdLbl = document.getElementById('wdLabel');
  wdSw.className = 'switch' + (wd.enabled ? ' on' : '');
  if (wd.enabled) {
    wdLbl.textContent = wd.next_in_sec != null
      ? `ON · цикл #${wd.cycles || 1} · ${wd.next_in_sec}с`
      : `ON · опрос #${wd.cycles || 1} (${wd.threads || 5} пот.)`;
    wdLbl.style.color = 'var(--ok)';
  } else {
    wdLbl.textContent = `OFF · циклы ${wd.cycles || 0}`;
    wdLbl.style.color = 'var(--muted)';
  }

  // Prune stale selections
  const idSet = new Set(accs.map(a => a.account_id));
  for (const id of Array.from(SELECTED)) if (!idSet.has(id)) SELECTED.delete(id);
  updateBulkBar();

  renderAccounts();
  if (ACTIVE_VIEW === 'autopilot') renderAutopilotView();
  if (ACTIVE_VIEW === 'upload') renderUploadPicker();
  if (ACTIVE_VIEW === 'unban') renderUnbanView();
  if (DRAWER_ID) {
    if (idSet.has(DRAWER_ID)) renderDrawer();
    else closeDrawer();
  }
}

// ── Accounts Table ───────────────────────────────────────────
function getVisibleAccounts() {
  const q = (document.getElementById('accSearch')?.value || '').trim().toLowerCase();
  const order = { action_required: 0, appealing: 1, eligible: 2 };
  return (STATE.accounts || [])
    .filter(a => {
      if (ACTIVE_FILTER === 'unauth' && a.authenticated) return false;
      if (ACTIVE_FILTER === 'authed' && !a.authenticated) return false;
      if (!['all', 'unauth', 'authed'].includes(ACTIVE_FILTER)) {
        if (!a.authenticated || !a.audit || a.audit.account_health !== ACTIVE_FILTER) return false;
      }
      if (q) {
        const hay = [a.account_id, a.username, a.email, a.audit?.uniq_id, a.audit?.uid].join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    })
    .sort((a, b) => {
      if (a.authenticated !== b.authenticated) return Number(b.authenticated) - Number(a.authenticated);
      const ha = a.audit ? (order[a.audit.account_health] ?? 3) : 4;
      const hb = b.audit ? (order[b.audit.account_health] ?? 3) : 4;
      if (ha !== hb) return ha - hb;
      return (a.username || a.account_id).localeCompare(b.username || b.account_id);
    });
}

function renderAccounts() {
  const tbody = document.getElementById('accountsTbody');
  const total = (STATE.accounts || []).length;
  const list = getVisibleAccounts();
  const cntInfo = document.getElementById('tableCountInfo');
  if (cntInfo) cntInfo.textContent = `Показано ${list.length} из ${total}`;

  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:38px;color:var(--muted);">Нет аккаунтов по выбранному фильтру</td></tr>';
    return;
  }

  tbody.innerHTML = list.map(a => {
    const aud = a.audit;
    const handle = aud?.uniq_id || a.username || a.account_id;
    const sel = SELECTED.has(a.account_id);
    const vios = (aud?.notice_violations || []).filter(v => v.can_appeal || v.status_code === 1).length;
    const revVios = (aud?.notice_violations || []).filter(v => v.status_code === 4).length;
    const failVios = (aud?.notice_violations || []).filter(v => v.status_code === 7).length;
    const audTime = aud?.audited_at ? String(aud.audited_at).slice(0, 5) : '';
    const vioTag = vios
      ? `<span class="mono" style="font-size:11px;color:var(--err);margin-left:5px;">(${vios} орг.)</span>`
      : (failVios
          ? `<span class="mono" style="font-size:11px;color:var(--err);margin-left:5px;" title="Отклонено TikTok">(${failVios} откл.)</span>`
          : (revVios ? `<span class="mono" style="font-size:11px;color:var(--warn);margin-left:5px;" title="На проверке TikTok">(${revVios})</span>` : ''));
    const postsTxt = aud
      ? `<span>${aud.total_posts}</span> <span class="muted">·</span> <strong>${(aud.total_views || 0).toLocaleString()}</strong>`
      : '<span class="muted">—</span>';
    const geoTxt = aud
      ? `${esc(aud.reg_country || '?')}→${esc(aud.current_region || '?')} · ${esc(aud.proxy_country || 'OK')}`
      : esc(a.region);

    return `<tr class="${sel ? 'selected' : ''}">
      <td><input type="checkbox" class="chk" ${sel ? 'checked' : ''} onchange="toggleSelect('${esc(a.account_id)}')" /></td>
      <td>
        <div class="acc-link" onclick="openDrawer('${esc(a.account_id)}')">@${esc(handle)}</div>
        <div class="mono muted" style="font-size:11px;">${esc(a.email || a.account_id)}</div>
      </td>
      <td>
        <div>${statusBadge(a)}${vioTag}</div>
        ${audTime ? `<div class="mono muted" style="font-size:10.5px;margin-top:2px;">обн. ${esc(audTime)}</div>` : ''}
      </td>
      <td class="mono" style="font-size:12.5px;">${postsTxt}</td>
      <td class="mono muted" style="font-size:11.5px;">${geoTxt}</td>
      <td style="text-align:right;white-space:nowrap;">
        ${!a.authenticated
          ? `<button class="btn btn-xs btn-primary" onclick="authAccount('${esc(a.account_id)}')">🔑 Войти</button>`
          : `<button class="btn btn-xs" onclick="auditAccount('${esc(a.account_id)}')" title="Аудит">↻</button>
             <button class="btn btn-xs btn-ok" onclick="appealAccount('${esc(a.account_id)}', false)" title="Снять ограны">⚡</button>
             <button class="btn btn-xs btn-warn" onclick="cleanAccountPosts('${esc(a.account_id)}')" title="Удалить все посты">🗑</button>`
        }
        <button class="btn btn-xs btn-ghost" onclick="openDrawer('${esc(a.account_id)}')" title="Открыть">→</button>
      </td>
    </tr>`;
  }).join('');
}

function toggleSelect(aid) {
  if (SELECTED.has(aid)) SELECTED.delete(aid); else SELECTED.add(aid);
  updateBulkBar();
  renderAccounts();
}
function toggleSelectAll() {
  const vis = getVisibleAccounts().map(a => a.account_id);
  const all = vis.length > 0 && vis.every(id => SELECTED.has(id));
  if (all) vis.forEach(id => SELECTED.delete(id)); else vis.forEach(id => SELECTED.add(id));
  updateBulkBar();
  renderAccounts();
}
function clearSelection() {
  SELECTED.clear();
  const ca = document.getElementById('chkAll');
  if (ca) ca.checked = false;
  updateBulkBar();
  renderAccounts();
}
function updateBulkBar() {
  document.getElementById('bulkCount').textContent = SELECTED.size;
  document.getElementById('bulkBar').classList.toggle('hidden', SELECTED.size === 0);
}

async function runBulk(action, extra = {}) {
  const ids = Array.from(SELECTED);
  if (!ids.length) return;
  if (action === 'delete' && !confirm(`Удалить ${ids.length} аккаунтов из базы?`)) return;
  if (action === 'clean_posts' && !confirm(`Удалить ВСЕ старые посты на ${ids.length} аккаунтах?`)) return;
  try {
    await apiPost('/api/bulk-action', { account_ids: ids, action, hide_rejected: true, threads: 5, ...extra });
    toast(`Запущено (${ids.length} акк.)`, 'success');
    if (action === 'delete') clearSelection();
  } catch (e) { toast(e.message, 'err'); }
}
function promptBulkProxy() {
  const px = prompt('Новый прокси (socks5://... или ip:port:user:pass):');
  if (px && px.trim()) runBulk('set_proxy', { proxy: px.trim() });
}
function sendSelectedToAutopilot() {
  if (!SELECTED.size) return toast('Выберите аккаунты галочками', 'warn');
  switchView('autopilot');
  document.getElementById('apScope').value = 'selected';
  toast(`Выбрано ${SELECTED.size} акк. для Автопилота`, 'success');
}

// ── Media Library (Shared by Autopilot 600 & Upload Studio) ──
async function loadMediaLibrary() {
  try {
    const r = await apiJson('/api/media');
    MEDIA_LIBRARY = (r.files || []).slice().sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
    // Auto-select available library creatives in slide order if nothing chosen yet
    if (AP_MEDIA_PICK.size === 0 && MEDIA_LIBRARY.length > 0) {
      MEDIA_LIBRARY.forEach(f => AP_MEDIA_PICK.add(f.name));
    }
    renderMediaPickers();
  } catch (_) {}
}

async function uploadToMediaLibrary(fileList) {
  if (!fileList || !fileList.length) return;
  const fd = new FormData();
  for (const f of fileList) fd.append('files', f);
  try {
    const r = await apiJson('/api/media/upload', { method: 'POST', body: fd });
    (r.saved || []).forEach(name => AP_MEDIA_PICK.add(name));
    toast(`Загружено в библиотеку: ${(r.saved || []).length}`, 'success');
    await loadMediaLibrary();
  } catch (e) { toast(e.message, 'err'); }
}

function toggleApMedia(name) {
  if (AP_MEDIA_PICK.has(name)) AP_MEDIA_PICK.delete(name); else AP_MEDIA_PICK.add(name);
  renderMediaPickers();
}
function toggleUpLibMedia(name) {
  if (UP_LIB_PICK.has(name)) UP_LIB_PICK.delete(name); else UP_LIB_PICK.add(name);
  renderMediaPickers();
}

function renderMediaPickers() {
  const apCnt = document.getElementById('apMediaCnt');
  const apGrid = document.getElementById('apMediaGrid');
  if (apCnt) apCnt.textContent = AP_MEDIA_PICK.size;
  if (apGrid) {
    apGrid.innerHTML = MEDIA_LIBRARY.length
      ? MEDIA_LIBRARY.map(f => {
          const picked = AP_MEDIA_PICK.has(f.name);
          const url = `/api/media/file/${encodeURIComponent(f.name)}`;
          return `<div class="thumb ${picked ? 'picked' : ''}" onclick="toggleApMedia('${esc(f.name)}')" title="${esc(f.name)}">
            ${f.is_video ? `<video src="${url}" muted></video>` : `<img src="${url}" alt="" />`}
            <div class="thumb-check">${picked ? '✓' : ''}</div>
            <div class="thumb-name">${esc(f.name)}</div>
          </div>`;
        }).join('')
      : '<div class="muted" style="font-size:12px;">Библиотека пуста — нажмите «+ Загрузить»</div>';
  }

  const upCnt = document.getElementById('upLibCnt');
  const upGrid = document.getElementById('upLibThumbs');
  if (upCnt) upCnt.textContent = UP_LIB_PICK.size;
  if (upGrid) {
    upGrid.innerHTML = MEDIA_LIBRARY.length
      ? MEDIA_LIBRARY.map(f => {
          const picked = UP_LIB_PICK.has(f.name);
          const url = `/api/media/file/${encodeURIComponent(f.name)}`;
          return `<div class="thumb ${picked ? 'picked' : ''}" onclick="toggleUpLibMedia('${esc(f.name)}')" title="${esc(f.name)}">
            ${f.is_video ? `<video src="${url}" muted></video>` : `<img src="${url}" alt="" />`}
            <div class="thumb-check">${picked ? '✓' : ''}</div>
            <div class="thumb-name">${esc(f.name)}</div>
          </div>`;
        }).join('')
      : '<div class="muted" style="font-size:12px;">Нет файлов на сервере</div>';
  }
}

// ── Goal Autopilot 600 View ──────────────────────────────────
function phaseBadge(phase) {
  switch (phase) {
    case 'goal_reached': return '<span class="badge b-ok">🎯 Цель 600+</span>';
    case 'uploading': return '<span class="badge b-sage">🚀 Залив</span>';
    case 'appealing': return '<span class="badge b-warn">🛡 Дожим 2048</span>';
    case 'monitoring': return '<span class="badge b-cyan">👁 Мониторинг</span>';
    case 'queued': return '<span class="badge b-dim">В очереди</span>';
    case 'capped': return '<span class="badge b-warn">Лимит раундов</span>';
    case 'error': return '<span class="badge b-err">Ошибка</span>';
    case 'stopped': return '<span class="badge b-dim">Остановлен</span>';
    default: return `<span class="badge b-dim">${esc(phase)}</span>`;
  }
}

function renderAutopilotView() {
  const ga = STATE.goal_autopilot || {};
  const cnt = ga.counts || {};
  const cfg = ga.config || {};
  const target = cfg.target_views || Number(document.getElementById('apTargetViews')?.value || 600);

  document.getElementById('apKpiTarget').textContent = target.toLocaleString();
  document.getElementById('apKpiReached').textContent = cnt.goal_reached || 0;
  document.getElementById('apKpiActive').textContent = ga.active_count || 0;
  document.getElementById('apKpiViews').textContent = (cnt.total_views || 0).toLocaleString();
  document.getElementById('apKpiPosts').textContent = `${cnt.total_posts || 0} / ${cnt.total_appeals || 0}`;

  const stBadge = document.getElementById('apStatusBadge');
  if (ga.running) {
    stBadge.textContent = `РАБОТАЕТ · #${ga.run_id || ''}`;
    stBadge.className = 'badge b-ok mono';
  } else if ((cnt.total || 0) > 0) {
    stBadge.textContent = `ЗАВЕРШЁН · ${cnt.goal_reached || 0}/${cnt.total || 0}`;
    stBadge.className = 'badge b-sage mono';
  } else {
    stBadge.textContent = 'Ожидание';
    stBadge.className = 'badge b-dim mono';
  }

  document.getElementById('apElapsedBadge').textContent = ga.running ? fmtDur(ga.elapsed_sec) : (ga.started_at || '00:00');
  document.getElementById('btnApStart').disabled = !!ga.running;
  document.getElementById('btnApStop').disabled = !ga.running;

  const summary = document.getElementById('apSummaryLine');
  if (ga.running) {
    summary.textContent = `Запущен в ${ga.started_at} · ср. ${ga.avg_views || 0} просм./акк · потоки: ${cfg.threads || 3} · уник: ${cfg.photo_mode || 'anti_unoriginal'}`;
  } else if ((cnt.total || 0) > 0) {
    summary.textContent = `Прогон #${ga.run_id || '—'} · достигли цели: ${cnt.goal_reached || 0}/${cnt.total || 0} (${ga.success_rate_pct || 0}%) · всего просмотров: ${(cnt.total_views || 0).toLocaleString()}`;
  } else {
    summary.textContent = 'Автопилот в режиме ожидания. Настройте параметры слева и нажмите «Запустить».';
  }

  const items = ga.items || [];
  const tbody = document.getElementById('apTbody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:38px;color:var(--muted);">Пока нет активного прогона автопилота</td></tr>';
    return;
  }

  tbody.innerHTML = items.map(it => {
    const tgt = Math.max(1, Number(it.target_views || target || 600));
    const cur = Number(it.views_total || 0);
    const pct = Math.min(100, Math.round((cur / tgt) * 100));
    const barCls = it.phase === 'goal_reached' ? 'reached' : (it.phase === 'error' || it.phase === 'capped' ? 'warn' : '');
    const vel = Number(it.views_velocity || 0);
    const handle = it.username || it.account_id;
    return `<tr>
      <td>
        <div class="acc-link" onclick="openDrawer('${esc(it.account_id)}')">@${esc(handle)}</div>
        <div class="muted" style="font-size:11px;max-width:210px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(it.detail || '')}">${esc(it.detail || '')}</div>
      </td>
      <td>
        <div class="prog-wrap">
          <div class="prog-top mono">
            <strong>${cur.toLocaleString()} <span class="muted" style="font-weight:400;">/ ${tgt.toLocaleString()}</span></strong>
            <span class="${pct >= 100 ? '' : 'muted'}" style="font-size:11px;${pct >= 100 ? 'color:var(--ok);font-weight:700;' : ''}">${pct}%${vel > 0 ? ` · +${vel}/10м` : ''}</span>
          </div>
          <div class="prog-track"><div class="prog-fill ${barCls}" style="width:${pct}%;"></div></div>
        </div>
      </td>
      <td>${phaseBadge(it.phase)}</td>
      <td class="mono" style="font-size:12px;">
        <div>#${it.round_num || 0}/${it.max_rounds || 3} · ${it.posts_published || 0} п.</div>
        <div class="muted" style="font-size:11px;">апелл.: ${it.appeals_sent || 0}</div>
      </td>
      <td class="mono muted" style="font-size:11.5px;">${esc((it.updated_at || '').slice(0, 5) || '—')}</td>
    </tr>`;
  }).join('');
}

async function startGoalAutopilot() {
  const scope = document.getElementById('apScope').value;
  const account_ids = scope === 'selected' ? Array.from(SELECTED) : [];
  if (scope === 'selected' && !account_ids.length) {
    return toast('Сначала выберите аккаунты галочками на вкладке «Аккаунты»', 'warn');
  }
  const media_files = MEDIA_LIBRARY.map(f => f.name).filter(name => AP_MEDIA_PICK.has(name));
  if (!media_files.length) {
    return toast('Выберите хотя бы один креатив из библиотеки', 'warn');
  }
  const btn = document.getElementById('btnApStart');
  btn.disabled = true;
  try {
    await apiPost('/api/autopilot/goal/start', {
      target_views: Number(document.getElementById('apTargetViews').value || 600),
      target_mode: document.getElementById('apTargetMode').value,
      scope: scope === 'selected' ? 'all_authed' : scope,
      account_ids,
      max_accounts: Number(document.getElementById('apMaxAccs').value || 0),
      media_files,
      description: document.getElementById('apDesc').value,
      auto_music_query: document.getElementById('apMusic').value,
      photo_mode: document.getElementById('apPhotoMode').value,
      threads: Number(document.getElementById('apThreads').value || 3),
      copies_per_round: Number(document.getElementById('apCopies').value || 6),
      max_rounds: Number(document.getElementById('apMaxRounds').value || 3),
      reupload_wait_minutes: Number(document.getElementById('apWaitMin').value || 25),
      monitor_interval_sec: Number(document.getElementById('apMonSec').value || 90),
      topup_copies: Number(document.getElementById('apTopup').value || 3),
      protect_views_threshold: Number(document.getElementById('apProtectViews').value || 150),
      region: document.getElementById('apRegion').value || 'US',
      auto_appeal_2048: document.getElementById('apAuto2048').checked,
      auto_appeal_studio: document.getElementById('apAutoStudio').checked,
      auto_refill_pool: document.getElementById('apRefill').checked,
      delete_old_on_first_round: document.getElementById('apDelFirst').checked,
    });
    toast('Автопилот запущен!', 'success');
    await fetchState();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function stopGoalAutopilot() {
  try {
    await apiPost('/api/autopilot/goal/stop');
    toast('Автопилот остановлен', 'warn');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

async function clearGoalAutopilot() {
  try {
    await apiPost('/api/autopilot/goal/clear');
    toast('История прогона очищена', 'info');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

async function copyAutopilot(scope, fmt) {
  try {
    const r = await apiJson(`/api/autopilot/goal/copy?scope=${encodeURIComponent(scope)}&fmt=${encodeURIComponent(fmt)}`);
    if (!r.count) return toast('Пока нет подходящих аккаунтов', 'warn');
    await copyText(r.text, `Скопировано: ${r.count}`);
  } catch (e) { toast(e.message, 'err'); }
}

// ── Account Inspector Drawer ─────────────────────────────────
function openDrawer(aid) {
  DRAWER_ID = aid;
  document.getElementById('drawerBg').classList.remove('hidden');
  renderDrawer();
}
function closeDrawer() {
  DRAWER_ID = null;
  document.getElementById('drawerBg').classList.add('hidden');
}

function renderDrawer() {
  const a = (STATE.accounts || []).find(x => x.account_id === DRAWER_ID);
  if (!a) return;
  const aud = a.audit;
  const handle = aud?.uniq_id || a.username || a.account_id;

  document.getElementById('drawerHeadInfo').innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;">
      <strong style="font-size:15px;">@${esc(handle)}</strong>
      ${statusBadge(a)}
    </div>
    <div class="mono muted" style="font-size:11.5px;margin-top:2px;">${esc(a.email || '—')} · UID ${esc(aud?.uid || '—')}</div>`;

  const vios = aud?.notice_violations || [];
  const posts = aud?.post_penalties || [];

  document.getElementById('drawerBody').innerHTML = `
    <div style="display:flex;gap:6px;flex-wrap:wrap;">
      ${!a.authenticated ? `<button class="btn btn-sm btn-primary" onclick="authAccount('${esc(a.account_id)}')">🔑 Войти</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="auditAccount('${esc(a.account_id)}')">↻ Аудит</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm btn-ok" onclick="appealAccount('${esc(a.account_id)}', false)">⚡ Снять ограны</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="appealAccount('${esc(a.account_id)}', true)">⏳ Ждать волну</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm btn-warn" onclick="cleanAccountPosts('${esc(a.account_id)}')">🗑 Очистить посты</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="restartAccount('${esc(a.account_id)}')">🔄 Рестарт ТТ</button>` : ''}
      ${a.use_outlook_hub ? `<button class="btn btn-sm" onclick="fetchOutlookOtp('${esc(a.account_id)}')">📬 OTP</button>` : ''}
      ${a.authenticated ? `<a class="btn btn-sm" href="/api/accounts/${encodeURIComponent(a.account_id)}/export?mode=cookies">⬇ cookies</a>` : ''}
    </div>

    ${vios.length ? `<div class="sec-box">
      <div class="sec-head"><span>Ограничения Account updates</span><span class="badge b-warn">${vios.length}</span></div>
      <div style="display:flex;flex-direction:column;gap:6px;max-height:170px;overflow-y:auto;">
        ${vios.slice(0, 15).map(v => `<div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;">
          <span>${esc(v.title)} <span class="mono muted">#${esc(v.punish_id.slice(-6))}</span></span>
          <span class="badge ${v.can_appeal || v.status_code === 7 ? 'b-err' : (v.status_code === 6 ? 'b-ok' : 'b-warn')}">${esc(v.status_label.split(' ')[0])}</span>
        </div>`).join('')}
        ${vios.length > 15 ? `<div class="muted" style="font-size:11.5px;">+ ещё ${vios.length - 15}</div>` : ''}
      </div>
    </div>` : ''}

    <div class="sec-box">
      <div class="sec-head">
        <span>Посты в Studio (${aud?.total_posts ?? 0})</span>
        <span class="mono" style="color:var(--sage);font-weight:600;">${(aud?.total_views || 0).toLocaleString()} просм.</span>
      </div>
      ${posts.length ? `<table class="tbl">
        <tbody>
          ${posts.map(p => `<tr>
            <td style="padding:6px 4px;">
              <a href="https://www.tiktok.com/@${esc(handle)}/video/${esc(p.item_id)}" target="_blank" class="mono" style="color:var(--sage);text-decoration:none;font-size:11.5px;font-weight:600;">${esc(p.item_id)} ↗</a>
              <div class="muted" style="font-size:11px;max-width:210px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(p.desc || '—')}</div>
            </td>
            <td class="mono" style="padding:6px 4px;font-size:12px;">${(p.play_count || 0).toLocaleString()}</td>
            <td style="padding:6px 4px;text-align:right;white-space:nowrap;">
              <button class="btn btn-xs" onclick="setPostVis('${esc(a.account_id)}','${esc(p.item_id)}',true)">Private</button>
              <button class="btn btn-xs btn-err" onclick="deleteSinglePost('${esc(a.account_id)}','${esc(p.item_id)}')">🗑</button>
            </td>
          </tr>`).join('')}
        </tbody>
      </table>` : '<div class="muted" style="font-size:12px;">Постов нет</div>'}
    </div>

    <details class="adv" style="margin:0;">
      <summary>Прокси и профиль (Nickname / Bio)</summary>
      <div class="adv-body" style="padding-top:10px;display:flex;flex-direction:column;gap:8px;">
        <div class="mono muted" style="font-size:11.5px;">Текущий: ${esc(aud?.proxy_country ? `${aud.proxy_country} · ${aud.proxy_ip}` : a.proxy_masked)}</div>
        <div style="display:flex;gap:6px;">
          <input type="text" id="drProxyIn" placeholder="Новый прокси..." />
          <button class="btn btn-sm btn-primary" onclick="saveDrawerProxy('${esc(a.account_id)}')">Сохранить</button>
        </div>
        ${a.authenticated ? `
        <div class="row-2" style="margin-top:4px;">
          <input type="text" id="drNick" placeholder="Nickname" value="${esc(aud?.nickname || '')}" />
          <input type="text" id="drBioUrl" placeholder="Ссылка в Bio" />
        </div>
        <div style="display:flex;gap:6px;">
          <input type="text" id="drBio" placeholder="Текст Bio..." />
          <button class="btn btn-sm btn-ok" onclick="saveDrawerProfile('${esc(a.account_id)}')">Обновить</button>
        </div>` : ''}
      </div>
    </details>
  `;
}

// ── Single-Account & Topbar Actions ──────────────────────────
async function auditAll() {
  const b = document.getElementById('btnTopAudit'); b.disabled = true;
  try { await apiPost('/api/audit-all?threads=5'); toast('Аудит завершён', 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); } finally { b.disabled = false; }
}
async function appealAll() {
  const b = document.getElementById('btnTopAppeal'); b.disabled = true;
  try {
    await apiPost('/api/appeal-all', { hide_rejected: true, appeal_studio_posts: false, settle: false, threads: 5 });
    toast('Снятие огранов завершено', 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); } finally { b.disabled = false; }
}
async function authAccount(aid) {
  toast(`Вход @${aid}...`, 'info');
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/auth`); toast(`@${aid} вошёл`, 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}
async function auditAccount(aid) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/audit`); toast(`Аудит @${aid} готов`, 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}
async function appealAccount(aid, settle) {
  toast(settle ? `@${aid}: ждём волну огранов...` : `@${aid}: снимаем ограны...`, 'info');
  try {
    await apiPost(`/api/accounts/${encodeURIComponent(aid)}/appeal`, { hide_rejected: true, appeal_studio_posts: false, settle });
    toast(`@${aid}: готово`, 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}
async function cleanAccountPosts(aid) {
  if (!confirm(`Удалить ВСЕ посты на @${aid}?`)) return;
  toast(`Удаление постов @${aid}...`, 'info');
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/clean-posts`);
    toast(`@${aid}: удалено постов ${r.deleted}`, 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}
async function restartAccount(aid) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/restart`); toast(`@${aid}: TikTok перезапущен`, 'success'); }
  catch (e) { toast(e.message, 'err'); }
}
async function setPostVis(aid, itemId, priv) {
  try { const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/posts/${encodeURIComponent(itemId)}/visibility`, { private: priv }); toast(r.message, 'success'); }
  catch (e) { toast(e.message, 'err'); }
}
async function deleteSinglePost(aid, itemId) {
  if (!confirm(`Удалить пост ${itemId} навсегда?`)) return;
  try {
    await apiJson(`/api/accounts/${encodeURIComponent(aid)}/posts/${encodeURIComponent(itemId)}`, { method: 'DELETE' });
    toast('Пост удалён', 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}
async function fetchOutlookOtp(aid) {
  toast('Читаю код из Outlook...', 'info');
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/outlook-otp`);
    await copyText(r.code, `Код: ${r.code} (скопирован)`);
  } catch (e) { toast(e.message, 'err'); }
}
async function saveDrawerProxy(aid) {
  const px = document.getElementById('drProxyIn').value.trim();
  if (!px) return toast('Введите прокси', 'warn');
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/proxy`, { proxy: px });
    toast(`Прокси OK: ${r.country} · ${r.ip}`, 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}
async function saveDrawerProfile(aid) {
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/profile`, {
      nickname: document.getElementById('drNick').value,
      bio: document.getElementById('drBio').value,
      bio_url: document.getElementById('drBioUrl').value,
    });
    toast(r.message, 'success');
  } catch (e) { toast(e.message, 'err'); }
}
async function toggleWatchdog() {
  const next = !(STATE.watchdog && STATE.watchdog.enabled);
  try {
    await apiPost('/api/watchdog', { enabled: next, hide_rejected: true, threads: 5 });
    toast(next ? 'Сторож включён (5 пот.)' : 'Сторож выключен', next ? 'success' : 'warn');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

async function copyAccounts(scope, fmt = 'lines') {
  try {
    const r = await apiJson(`/api/accounts/copy-export?scope=${encodeURIComponent(scope)}&fmt=${encodeURIComponent(fmt)}`);
    if (!r.count) return toast('Список пуст', 'warn');
    await copyText(r.text, `Скопировано: ${r.count}`);
  } catch (e) { toast(e.message, 'err'); }
}
async function copySelected(fmt = 'lines') {
  const ids = Array.from(SELECTED);
  if (!ids.length) return toast('Выберите аккаунты галочками', 'warn');
  try {
    const r = await apiJson(`/api/accounts/copy-export?scope=selected&fmt=${encodeURIComponent(fmt)}&ids=${encodeURIComponent(ids.join(','))}`);
    if (!r.count) return toast('Список пуст', 'warn');
    await copyText(r.text, `Скопировано выбранных: ${r.count}`);
  } catch (e) { toast(e.message, 'err'); }
}

// ── Upload & Campaign Studio ─────────────────────────────────
function handleMediaFiles(fileList) {
  for (const f of fileList) UP_FILES.push(f);
  renderMediaThumbs();
}
function removeMediaFile(idx) {
  UP_FILES.splice(idx, 1);
  renderMediaThumbs();
}
function renderMediaThumbs() {
  document.getElementById('mediaThumbs').innerHTML = UP_FILES.map((f, i) => {
    const url = URL.createObjectURL(f);
    const isVid = f.name.toLowerCase().endsWith('.mp4');
    return `<div class="thumb picked">
      ${isVid ? `<video src="${url}" muted></video>` : `<img src="${url}" alt="" />`}
      <button class="thumb-rm" onclick="removeMediaFile(${i})">✕</button>
    </div>`;
  }).join('');
}

async function previewUniquifier() {
  const photo = UP_FILES.find(f => !f.name.toLowerCase().endsWith('.mp4'));
  if (!photo) return toast('Сначала добавьте фото с диска', 'warn');
  const fd = new FormData();
  fd.append('file', photo);
  fd.append('photo_mode', document.getElementById('upPhotoMode').value);
  try {
    const r = await apiJson('/api/uniquifier/preview', { method: 'POST', body: fd });
    const box = document.getElementById('uniqPreviewBox');
    box.classList.remove('hidden');
    box.innerHTML = `
      <div><div class="mono muted" style="font-size:11px;margin-bottom:3px;">ДО (${r.before_kb} КБ)</div><img src="${r.before_data_url}" style="width:100%;border-radius:10px;" /></div>
      <div><div class="mono" style="font-size:11px;color:var(--ok);margin-bottom:3px;">ПОСЛЕ (${r.after_kb} КБ)</div><img src="${r.after_data_url}" style="width:100%;border-radius:10px;" /></div>`;
  } catch (e) { toast(e.message, 'err'); }
}

function renderUploadPicker() {
  const q = (document.getElementById('upAccFilter')?.value || '').trim().toLowerCase();
  const accs = (STATE.accounts || [])
    .filter(a => !q || (a.username || a.account_id).toLowerCase().includes(q) || (a.email || '').toLowerCase().includes(q))
    .sort((a, b) => Number(b.authenticated) - Number(a.authenticated));

  document.getElementById('upPickStat').textContent = UP_PICK.size ? `(выбрано ${UP_PICK.size})` : `(пойдут все ${STATE.accounts.length})`;
  document.getElementById('uploadPickList').innerHTML = accs.map(a => {
    const ck = UP_PICK.has(a.account_id);
    return `<label class="pick-row ${ck ? 'checked' : ''}">
      <span style="display:flex;align-items:center;gap:8px;min-width:0;">
        <input type="checkbox" class="chk" ${ck ? 'checked' : ''} onchange="toggleUpPick('${esc(a.account_id)}')" />
        <strong style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">@${esc(a.audit?.uniq_id || a.username || a.account_id)}</strong>
        ${statusBadge(a)}
      </span>
      <span class="mono muted" style="font-size:11px;">${a.audit?.total_posts ?? 0} п.</span>
    </label>`;
  }).join('');
}

function toggleUpPick(aid) {
  if (UP_PICK.has(aid)) UP_PICK.delete(aid); else UP_PICK.add(aid);
  renderUploadPicker();
}
function pickUploadSet(mode) {
  UP_PICK.clear();
  (STATE.accounts || []).forEach(a => {
    if (mode === 'all') UP_PICK.add(a.account_id);
    else if (mode === 'clean' && a.audit?.account_health === 'eligible') UP_PICK.add(a.account_id);
  });
  renderUploadPicker();
}

async function submitCampaign() {
  if (!UP_FILES.length && !UP_LIB_PICK.size) return toast('Добавьте файлы или выберите из библиотеки сервера!', 'warn');
  const fd = new FormData();
  UP_FILES.forEach(f => fd.append('files', f));
  if (!UP_FILES.length && UP_LIB_PICK.size) {
    fd.append('server_media_files', Array.from(UP_LIB_PICK).join(','));
  }
  fd.append('account_ids', Array.from(UP_PICK).join(','));
  fd.append('description', document.getElementById('upDesc').value);
  fd.append('copies_count', document.getElementById('upCopies').value);
  fd.append('threads', document.getElementById('upThreads').value);
  fd.append('photo_mode', document.getElementById('upPhotoMode').value);
  fd.append('auto_music_query', document.getElementById('upMusic').value);
  fd.append('region', document.getElementById('upRegion').value || 'US');
  fd.append('delay_min_sec', document.getElementById('upDelayMin').value);
  fd.append('delay_max_sec', document.getElementById('upDelayMax').value);
  fd.append('restart_every_n', document.getElementById('upRestartN').value);
  fd.append('delete_old_posts', document.getElementById('upDelOld').checked);
  fd.append('auto_appeal_2048', document.getElementById('upAutoAppeal').checked);
  fd.append('defer_post_appeal_to_queue', document.getElementById('upDeferUnban')?.checked ?? true);
  fd.append('appeal_queue_threads', document.getElementById('upQueueThreads')?.value || '3');
  fd.append('delayed_recheck_minutes', document.getElementById('upDelayedRecheck')?.value || '12');
  fd.append('randomize_tags', document.getElementById('upRandTags').checked);
  fd.append('pin_sticky_proxy', document.getElementById('upStickyProxy').checked);

  const btn = document.getElementById('btnRunCampaign'); btn.disabled = true;
  try {
    const r = await apiJson('/api/campaign/run', { method: 'POST', body: fd });
    toast(`Залив запущен на ${r.accounts} акк.!`, 'success');
    document.getElementById('logDock').classList.remove('collapsed');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function submitSingleUpload() {
  if (!UP_FILES.length) return toast('Добавьте видео MP4 или фото с диска!', 'warn');
  const fd = new FormData();
  UP_FILES.forEach(f => fd.append('files', f));
  fd.append('account_ids', Array.from(UP_PICK).join(','));
  fd.append('description', document.getElementById('upDesc').value);
  fd.append('music_search', document.getElementById('upMusic').value);
  fd.append('skip_warmup', 'true');
  fd.append('force_cooldown', 'true');
  fd.append('delete_old_posts', document.getElementById('upDelOld').checked);
  fd.append('appeal_after_upload', document.getElementById('upAutoAppeal').checked);

  const btn = document.getElementById('btnQuick1'); btn.disabled = true;
  try {
    const r = await apiJson('/api/upload', { method: 'POST', body: fd });
    const ok = r.results.filter(x => x.ok).length;
    toast(`Залито: ${ok}/${r.results.length}`, ok ? 'success' : 'err');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

// ── Unban 2048 Queue ─────────────────────────────────────────
function renderUnbanView() {
  const uq = STATE.unban_queue || {};
  const cnt = uq.counts || {};
  const badge = document.getElementById('unbanStatusBadge');
  const doneN = (cnt.done || 0) + (cnt.error || 0) + (cnt.stopped || 0);
  const totN = cnt.total || 0;
  const sched = uq.scheduled_rechecks || 0;
  badge.textContent = uq.running
    ? `Работает (${doneN}/${totN} · ${uq.active_workers || 0} пот.)`
    : (sched > 0 ? `Авто-повтор: ${sched} акк.` : (totN ? `Готово (${cnt.clean || 0}/${totN})` : 'Ожидание'));
  badge.className = 'badge mono ' + (uq.running ? 'b-ok' : (sched > 0 ? 'b-cyan' : 'b-dim'));

  const items = uq.items || [];
  document.getElementById('unbanQueueBox').innerHTML = items.length
    ? `<div style="display:flex;flex-direction:column;gap:6px;max-height:240px;overflow-y:auto;">
        ${items.map(it => {
          const detail = it.status === 'waiting'
            ? `тишина ${it.idle_sec || 0}/${it.quiet_sec || 0}с`
            : (it.folder_title || it.status);
          return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:7px 10px;border-radius:9px;background:var(--surface-subtle);border:1px solid var(--border);font-size:12px;">
            <span class="mono" style="font-weight:600;">@${esc(it.username || it.account_id)}</span>
            <span class="muted" style="font-size:11px;">${esc(detail)}</span>
            <span class="badge ${it.status === 'done' ? (it.folder_key === 'clean' ? 'b-ok' : 'b-warn') : (it.status === 'error' ? 'b-err' : 'b-warn')}">${esc(it.folder_key || it.status)}${it.appeals_sent ? ' · +' + it.appeals_sent : ''}</span>
          </div>`;
        }).join('')}
      </div>`
    : '<div class="muted" style="font-size:12px;">Очередь пуста.</div>';

  const authed = (STATE.accounts || []).filter(a => a.authenticated);
  document.getElementById('unbanPickList').innerHTML = authed.length
    ? authed.map(a => {
        const ck = UNBAN_PICK.has(a.account_id);
        return `<label class="pick-row ${ck ? 'checked' : ''}">
          <span style="display:flex;align-items:center;gap:8px;">
            <input type="checkbox" class="chk" ${ck ? 'checked' : ''} onchange="toggleUnbanPick('${esc(a.account_id)}')" />
            <strong>@${esc(a.audit?.uniq_id || a.username)}</strong>
            ${statusBadge(a)}
          </span>
          <span class="mono muted" style="font-size:11px;">${a.audit?.total_posts ?? 0} п.</span>
        </label>`;
      }).join('')
    : '<div class="muted" style="font-size:12px;">Нет авторизованных аккаунтов.</div>';
}

function toggleUnbanPick(aid) {
  if (UNBAN_PICK.has(aid)) UNBAN_PICK.delete(aid); else UNBAN_PICK.add(aid);
  renderUnbanView();
}
function toggleAllUnbanPick() {
  const ids = (STATE.accounts || []).filter(a => a.authenticated).map(a => a.account_id);
  const all = ids.length > 0 && ids.every(id => UNBAN_PICK.has(id));
  if (all) UNBAN_PICK.clear(); else ids.forEach(id => UNBAN_PICK.add(id));
  renderUnbanView();
}

async function startUnbanQueue() {
  const scope = document.getElementById('unbanScope').value;
  const account_ids = scope === 'selected' ? Array.from(UNBAN_PICK) : [];
  if (scope === 'selected' && !account_ids.length) return toast('Выберите аккаунты справа', 'warn');
  try {
    await apiPost('/api/unban-queue/start', {
      scope: scope === 'selected' ? 'all' : scope,
      account_ids,
      threads: Number(document.getElementById('unbanThreads').value),
      wait_settle: document.getElementById('unbanSettle').checked,
      quiet_seconds: Number(document.getElementById('unbanQuiet').value),
      delayed_recheck_minutes: Number(document.getElementById('unbanRecheckMin')?.value || 0),
      recheck_include_studio: true,
      only_account_ineligible: document.getElementById('unbanOnly2048').checked,
      appeal_studio_posts: !document.getElementById('unbanOnly2048').checked,
      hide_rejected: true,
    });
    toast('Очередь снятия огранов запущена', 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

async function stopUnbanQueue() {
  try { await apiPost('/api/unban-queue/stop'); toast('Остановлено', 'warn'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}

// ── Import ───────────────────────────────────────────────────
function loadTxtFile(ev) {
  const f = ev.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    document.getElementById('impLines').value = reader.result || '';
    scheduleLinePreview();
  };
  reader.readAsText(f);
}
function scheduleLinePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runLinePreview, 250);
}
async function runLinePreview() {
  const lines_text = document.getElementById('impLines').value;
  const proxies_text = document.getElementById('impProxies').value;
  const region = document.getElementById('impRegion').value;
  if (!lines_text.trim()) {
    document.getElementById('previewBadge').textContent = '0';
    document.getElementById('previewBox').innerHTML = 'Вставьте строки аккаунтов слева.';
    return;
  }
  try {
    const r = await apiPost('/api/preview-lines', { lines_text, proxies_text, region });
    document.getElementById('previewBadge').textContent = `${r.valid_count}/${r.rows.length}`;
    document.getElementById('previewBox').innerHTML = `<table class="tbl">
      <tbody>${r.rows.map(x => x.ok
        ? `<tr><td class="mono">${esc(x.account_id)}</td><td><span class="badge b-sage">${esc(x.format)}</span></td><td class="mono muted" style="font-size:11px;">${esc(x.proxy_masked)}</td></tr>`
        : `<tr><td colspan="3" style="color:var(--err);">✕ ${esc(x.error)}</td></tr>`
      ).join('')}</tbody></table>`;
  } catch (_) {}
}
async function submitBatchImport() {
  try {
    const r = await apiPost('/api/import-batch', {
      lines_text: document.getElementById('impLines').value,
      proxies_text: document.getElementById('impProxies').value,
      region: document.getElementById('impRegion').value,
      auto_login: document.getElementById('impAutoLogin').checked,
      auto_clean_posts: document.getElementById('impAutoClean').checked,
      auto_appeal: document.getElementById('impAutoAppeal').checked,
      hide_rejected: true,
      threads: 5,
    });
    toast(`Импортировано: ${r.imported.length}`, 'success');
    document.getElementById('impLines').value = '';
    switchView('accounts');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}
async function submitFolderImport() {
  const folder_path = document.getElementById('folderPathInput').value.trim();
  if (!folder_path) return toast('Укажите путь к папке', 'warn');
  try {
    const r = await apiPost('/api/import-folder', {
      folder_path,
      proxy: document.getElementById('impProxies').value.split('\n')[0] || '',
      region: document.getElementById('impRegion').value,
    });
    toast(`Импортировано сессий: ${r.imported.length}`, 'success');
    switchView('accounts');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

// ── Tools: Clean Dump & Shadow Checker ───────────────────────
async function loadLastShadow() {
  try {
    const d = await apiJson('/api/shadow-checker/last');
    if (d.results && d.results.length) {
      LAST_SHADOW_ROWS = d.results;
      renderShadowTable(d.results);
    }
  } catch (_) {}
}
async function runShadowChecker() {
  const btn = document.getElementById('btnRunShadow'); btn.disabled = true;
  const th = Number(document.getElementById('shThreads')?.value || 5);
  try {
    const d = await apiPost('/api/shadow-checker/run', {
      scope: document.getElementById('shScope').value,
      usernames_text: document.getElementById('shCustomUsers').value,
      threads: th,
      deep_audit: document.getElementById('shDeepAudit')?.checked ?? true,
    });
    LAST_SHADOW_ROWS = d.results || [];
    renderShadowTable(LAST_SHADOW_ROWS);
    toast(`Выгрузка готова: ${LAST_SHADOW_ROWS.length} акк. (${th} пот.)`, 'success');
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}
function copyShadowRow(idx, kind) {
  const r = LAST_SHADOW_ROWS[idx];
  if (!r) return;
  if (kind === 'tg') copyText(r.telegram_text || '', `Выгрузка @${r.username} скопирована`);
  else copyText(r.copy_line || ('@' + r.username), `Строка @${r.username} скопирована`);
}
function copyShadowDump(mode) {
  if (!LAST_SHADOW_ROWS.length) return toast('Сначала нажмите «Выгрузить и проверить»', 'warn');
  let text = '';
  if (mode === 'telegram') text = LAST_SHADOW_ROWS.map(r => r.telegram_text).join('\n\n────────────────\n\n');
  else if (mode === 'usernames') text = LAST_SHADOW_ROWS.map(r => '@' + r.username).join('\n');
  else if (mode === 'urls') text = LAST_SHADOW_ROWS.flatMap(r => r.post_urls || []).join('\n');
  else if (mode === 'clean_lines') text = LAST_SHADOW_ROWS.filter(r => r.verdict === 'clean').map(r => r.copy_line || ('@' + r.username)).join('\n');
  else text = LAST_SHADOW_ROWS.map(r => r.copy_line || ('@' + r.username)).join('\n');
  copyText(text, 'Скопировано в буфер обмена');
}
function renderShadowTable(rows) {
  if (!rows.length) {
    document.getElementById('shadowResultsBox').innerHTML = '<div class="muted">Нет данных</div>';
    return;
  }
  document.getElementById('shadowResultsBox').innerHTML = `<div class="tbl-wrap"><table class="tbl">
    <thead><tr><th>Аккаунт</th><th>Посты</th><th>Просмотры</th><th>Гео</th><th>Тень</th><th>Studio / 2048</th><th style="text-align:right;">Копия</th></tr></thead>
    <tbody>${rows.map((r, idx) => {
      const bCls = r.verdict === 'clean' ? 'b-ok' : (r.verdict === 'partial_shadow' ? 'b-warn' : 'b-err');
      const notes = [];
      if (r.active_2048) notes.push(`<span class="badge b-err">2048: ${r.active_2048}</span>`);
      else if (r.reviewing_2048) notes.push(`<span class="badge b-warn">2048 пров: ${r.reviewing_2048}</span>`);
      if (r.studio_flagged_count) notes.push(`<span class="badge b-dim" title="Метка в TikTok Studio (не тень аккаунта)">Studio: ${r.studio_flagged_count}</span>`);
      if (!notes.length) notes.push('<span class="muted">—</span>');
      const posts = r.posts || [];
      const postsDetail = posts.length
        ? `<details style="margin-top:4px;"><summary class="muted" style="cursor:pointer;font-size:11px;">Показать ${posts.length} постов</summary>
            <div style="margin-top:5px;display:flex;flex-direction:column;gap:4px;">
              ${posts.map((p, pi) => `<div style="font-size:11.5px;padding:5px 8px;border-radius:8px;background:var(--surface-subtle);border:1px solid var(--border);display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;">
                <span><a href="${esc(p.url)}" target="_blank" class="mono" style="color:var(--sage);text-decoration:none;font-weight:600;">#${pi + 1}</a> · 👁 <strong>${p.views}</strong> ❤️ ${p.likes} 💬 ${p.comments} 📣 ${p.shares} 💾 ${p.saves}</span>
                <span>Тень: <strong>${esc(p.shadow_label)}</strong> · ${esc(p.moderation_label)} · ${esc(p.region_flag)} ${esc(p.region || '')}${p.studio_note ? ` · <span class="muted">${esc(p.studio_note)}</span>` : ''}</span>
              </div>`).join('')}
            </div>
          </details>`
        : '';
      return `<tr>
        <td><div class="mono" style="font-weight:600;">@${esc(r.username)}</div>${postsDetail}</td>
        <td class="mono">${r.public_posts_count}/${r.total_posts}</td>
        <td class="mono"><strong>${(r.total_views || 0).toLocaleString()}</strong></td>
        <td class="mono muted">${esc((r.regions || []).join(',') || '—')}</td>
        <td><span class="badge ${bCls}">${esc(r.verdict_label)}</span></td>
        <td>${notes.join(' ')}</td>
        <td style="text-align:right;white-space:nowrap;">
          <button class="btn btn-xs" onclick="copyShadowRow(${idx}, 'line')" title="Скопировать строку">📋</button>
          <button class="btn btn-xs" onclick="copyShadowRow(${idx}, 'tg')" title="Скопировать выгрузку постов">📨</button>
        </td>
      </tr>`;
    }).join('')}</tbody></table></div>`;
}

async function runProxyTest() {
  const proxies_text = document.getElementById('proxyTestInput').value;
  if (!proxies_text.trim()) return toast('Вставьте прокси', 'warn');
  const btn = document.getElementById('btnTestProxies'); btn.disabled = true;
  try {
    const r = await apiPost('/api/proxies/test', { proxies_text });
    document.getElementById('proxyTestResults').innerHTML = `<table class="tbl">
      <thead><tr><th>Прокси</th><th>GEO / IP</th><th>Пинг</th><th>Статус</th></tr></thead>
      <tbody>${r.results.map(x => `<tr>
        <td class="mono" style="font-size:11.5px;">${esc(x.proxy_masked)}</td>
        <td class="mono">${x.ok ? `${esc(x.country)} · ${esc(x.ip)}` : '—'}</td>
        <td class="mono">${x.ok ? x.latency_ms + 'мс' : '—'}</td>
        <td>${x.ok ? '<span class="badge b-ok">OK</span>' : `<span class="badge b-err">${esc(x.error)}</span>`}</td>
      </tr>`).join('')}</tbody></table>`;
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function loadConfig() {
  try {
    const c = await apiJson('/api/config');
    document.getElementById('cfgKeyBadge').textContent = c.has_api_key ? c.api_key_masked : 'Нет ключа';
    document.getElementById('cfgParallel').value = c.max_parallel_accounts;
    document.getElementById('cfgDailyMax').value = c.max_posts_per_account_per_day;
    document.getElementById('cfgAgeMin').value = c.min_session_age_minutes_before_first_post;
    document.getElementById('cfgGapHours').value = c.min_hours_between_posts_per_account;
    document.getElementById('cfgWarmupOn').checked = c.warmup_enabled;
    document.getElementById('cfgUniqOn').checked = c.uniquifier_enabled;
  } catch (_) {}
}
async function saveConfig() {
  try {
    await apiPost('/api/config', {
      api_key: document.getElementById('cfgApiKey').value || null,
      max_parallel_accounts: Number(document.getElementById('cfgParallel').value),
      max_posts_per_account_per_day: Number(document.getElementById('cfgDailyMax').value),
      min_session_age_minutes_before_first_post: Number(document.getElementById('cfgAgeMin').value),
      min_hours_between_posts_per_account: Number(document.getElementById('cfgGapHours').value),
      warmup_enabled: document.getElementById('cfgWarmupOn').checked,
      uniquifier_enabled: document.getElementById('cfgUniqOn').checked,
    });
    document.getElementById('cfgApiKey').value = '';
    toast('Настройки сохранены', 'success');
    await loadConfig();
  } catch (e) { toast(e.message, 'err'); }
}

async function runSpy() {
  const username = document.getElementById('spyUserInput').value.trim();
  if (!username) return toast('Введите @username', 'warn');
  const btn = document.getElementById('btnSpy'); btn.disabled = true;
  try {
    const r = await apiPost('/api/spy', { username });
    const s = r.spy;
    document.getElementById('spyResultBox').innerHTML = `
      <div style="margin-bottom:6px;"><strong>@${esc(s.username)}</strong> · просм.: <strong class="mono">${(s.total_views || 0).toLocaleString()}</strong> · средние: <strong class="mono">${(s.avg_views || 0).toLocaleString()}</strong></div>
      <div class="muted" style="font-size:11.5px;">Теги: ${(s.top_hashtags || []).slice(0, 8).map(t => esc(t.tag)).join(' ')}</div>`;
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}
async function genTotp() {
  const secret = document.getElementById('totpSecretInput').value.trim();
  if (!secret) return;
  try {
    const r = await apiPost('/api/totp/generate', { secret });
    document.getElementById('totpResultBox').textContent = `${r.code} (${r.remaining_sec}с)`;
    await copyText(r.code, 'Код скопирован');
  } catch (e) { toast(e.message, 'err'); }
}

// ── Live Logs & Boot ─────────────────────────────────────────
function appendLog(item) {
  if (!item.text) return;
  document.getElementById('logPreview').textContent = `[${item.time}] ${item.text}`;
  const box = document.getElementById('logBody');
  const div = document.createElement('div');
  const cls = item.level === 'success' ? 'l-ok' : (item.level === 'error' ? 'l-error' : (item.level === 'warn' ? 'l-warn' : 'l-info'));
  div.className = cls;
  div.textContent = `[${item.time}] ${item.text}`;
  box.appendChild(div);
  while (box.children.length > 250) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function bindDrop(id, fn) {
  const el = document.getElementById(id);
  if (!el) return;
  ['dragenter', 'dragover'].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.remove('drag'); }));
  el.addEventListener('drop', e => { if (e.dataTransfer?.files?.length) fn(e.dataTransfer.files); });
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

(async function boot() {
  applyTheme(localStorage.getItem('kintaro_theme') || 'light');
  document.getElementById('connAddr').textContent = window.location.host || '127.0.0.1:8080';
  bindDrop('mediaDropzone', files => handleMediaFiles(files));
  await Promise.all([fetchState(), loadMediaLibrary()]);
  (STATE.logs || []).slice(-60).forEach(appendLog);

  const es = new EventSource('/api/events');
  es.onmessage = ev => {
    try {
      const item = JSON.parse(ev.data);
      appendLog(item);
      if (['audit_updated', 'watchdog', 'jobs_updated', 'unban_queue', 'autopilot'].includes(item.kind)) {
        fetchState();
      }
    } catch (_) {}
  };
  setInterval(fetchState, 5000);
})();
