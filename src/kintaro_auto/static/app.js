let STATE = { accounts: [], active_jobs: [], shadows: [], watchdog: { enabled: false }, logs: [] };
let ACTIVE_VIEW = 'fleet';
let ACTIVE_FILTER = 'all';
let VIEW_MODE = 'cards'; // 'cards' | 'table'
let LOG_LEVEL = 'all';
let DRAWER_ACCOUNT_ID = null;

const SELECTED_IDS = new Set();
const UPLOAD_TARGETS = new Set();
const SERVER_MEDIA_SELECTED = new Set();
let UPLOAD_FILES = [];
let CAMPAIGN_PRESETS = [];
let ALL_LOGS = [];
let previewTimer = null;
let spintaxTimer = null;

const VIEW_TITLES = {
  fleet: 'Флот аккаунтов, папки и снятие огранов 2048',
  upload: 'Массовый Анти-Тень Залив (5 потоков · minimal_snow)',
  import: 'Пакетный импорт (Outlook Hub / IMAP / TOTP / x-device)',
  spy: 'Competitor Spy, клонирование крео и Комменты в ТОП #1',
  tools: 'Взаимный Буст постов и Массовый Оформитель профилей',
  analytics: 'Аналитика Studio, Радар теней и Авто-Пилот',
  settings: 'Тестер прокси и живой config.toml',
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTs(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

function toast(msg, type = 'info') {
  const stack = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (type === 'success' ? 'ok' : type);
  el.textContent = msg;
  stack.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .25s'; setTimeout(() => el.remove(), 260); }, 4200);
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

// ── Navigation & Views ───────────────────────────────────────
function switchView(name) {
  ACTIVE_VIEW = name;
  Object.keys(VIEW_TITLES).forEach(v => {
    document.getElementById('view-' + v).classList.toggle('hidden', v !== name);
  });
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  document.getElementById('topTitle').textContent = VIEW_TITLES[name] || '';
  if (name === 'fleet') loadSortedFolders();
  if (name === 'analytics') { renderAnalytics(); loadAutoPilotJobs(); }
  if (name === 'settings') loadConfig();
  if (name === 'upload') { renderUploadTargets(); loadPresets(); loadMediaLibrary(); }
  if (name === 'spy') populateSpySeedAccounts();
}

function setFilter(f, btn) {
  ACTIVE_FILTER = f;
  document.querySelectorAll('#view-fleet .chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  renderFleet();
}

function toggleViewMode() {
  VIEW_MODE = VIEW_MODE === 'cards' ? 'table' : 'cards';
  document.getElementById('btnViewMode').textContent = VIEW_MODE === 'cards' ? '☰ Таблица' : '⊞ Карточки';
  renderFleet();
}

// ── State Sync & Rendering ───────────────────────────────────
async function fetchState() {
  STATE = await apiJson('/api/state');
  renderAll();
}

function renderAll() {
  const accs = STATE.accounts || [];
  let authed = 0, elig = 0, app = 0, act = 0, views = 0, posts = 0;
  accs.forEach(a => {
    if (a.authenticated) authed++;
    if (a.audit) {
      views += (a.audit.total_views || 0);
      posts += (a.audit.total_posts || 0);
      if (a.audit.account_health === 'eligible') elig++;
      else if (a.audit.account_health === 'appealing') app++;
      else if (a.audit.account_health === 'action_required') act++;
    }
  });

  document.getElementById('kpiTotal').textContent = accs.length;
  document.getElementById('kpiAuthedSub').textContent = `${authed} авторизовано`;
  document.getElementById('kpiEligible').textContent = elig;
  document.getElementById('kpiAppealing').textContent = app;
  document.getElementById('kpiAction').textContent = act;
  document.getElementById('kpiViews').textContent = views.toLocaleString('ru-RU');
  document.getElementById('kpiPostsSub').textContent = `${posts} постов в Studio`;
  document.getElementById('navFleetCnt').textContent = accs.length;
  document.getElementById('navViewsPill').textContent = views > 999 ? (views / 1000).toFixed(1) + 'k' : views;
  document.getElementById('defProxyHint').textContent = STATE.default_proxy_masked || 'не задан';

  // Active Jobs Pill
  const jobs = STATE.active_jobs || [];
  const jb = document.getElementById('jobsBadge');
  if (jobs.length) {
    const j0 = jobs[0];
    document.getElementById('jobsText').textContent = `${j0.label} (${j0.done}/${j0.total})` + (jobs.length > 1 ? ` +${jobs.length - 1}` : '');
    jb.classList.remove('hidden');
  } else {
    jb.classList.add('hidden');
  }

  // Watchdog Pill
  const wd = STATE.watchdog || {};
  document.getElementById('wdDot').className = 'dot' + (wd.enabled ? ' on' : '');
  document.getElementById('wdStateTxt').textContent = wd.enabled ? `ON · #${wd.cycles}` : 'OFF';
  document.getElementById('wdBtn').textContent = wd.enabled ? 'Остановить сторожа' : 'Включить сторожа';

  // Clean deleted IDs from selection
  const existingSet = new Set(accs.map(a => a.account_id));
  for (const id of Array.from(SELECTED_IDS)) if (!existingSet.has(id)) SELECTED_IDS.delete(id);
  updateBulkBar();

  renderFleet();
  if (ACTIVE_VIEW === 'upload') renderUploadTargets();
  if (ACTIVE_VIEW === 'analytics') renderAnalytics();
  if (DRAWER_ACCOUNT_ID) {
    if (existingSet.has(DRAWER_ACCOUNT_ID)) renderDrawer();
    else closeDrawer();
  }
}

function getFilteredAccounts() {
  const q = (document.getElementById('fleetSearch')?.value || '').trim().toLowerCase();
  const sort = document.getElementById('fleetSort')?.value || 'views_desc';
  let arr = (STATE.accounts || []).filter(a => {
    if (ACTIVE_FILTER === 'unauth' && a.authenticated) return false;
    if (ACTIVE_FILTER !== 'all' && ACTIVE_FILTER !== 'unauth') {
      if (!a.authenticated || !a.audit || a.audit.account_health !== ACTIVE_FILTER) return false;
    }
    if (q) {
      const hay = [a.account_id, a.username, a.email, a.audit?.uid, a.audit?.uniq_id, a.audit?.idc_gateway, a.audit?.reg_country, a.audit?.current_region]
        .join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const healthOrder = { action_required: 0, appealing: 1, eligible: 2 };
  arr.sort((a, b) => {
    if (sort === 'views_desc') return (b.audit?.total_views || 0) - (a.audit?.total_views || 0);
    if (sort === 'health') {
      const ha = a.audit ? (healthOrder[a.audit.account_health] ?? 3) : 4;
      const hb = b.audit ? (healthOrder[b.audit.account_health] ?? 3) : 4;
      return ha - hb;
    }
    return (a.username || a.account_id).localeCompare(b.username || b.account_id);
  });
  return arr;
}

function healthBadgeHtml(a) {
  if (!a.authenticated) return `<span class="badge b-err">● Требуется вход</span>`;
  if (!a.audit) return `<span class="badge b-neutral">Не проверен</span>`;
  if (a.audit.account_health === 'eligible') return `<span class="badge b-ok">● ELIGIBLE</span>`;
  if (a.audit.account_health === 'appealing') return `<span class="badge b-warn">● APPEALING</span>`;
  return `<span class="badge b-err">● ACTION NEEDED</span>`;
}

function renderFleet() {
  const box = document.getElementById('fleetContainer');
  const list = getFilteredAccounts();
  if (!list.length) {
    box.innerHTML = `<div class="panel" style="text-align:center;color:var(--muted);padding:38px;">
      Нет аккаунтов по текущему фильтру. Нажми «↻ HTTP-аудит всех» или добавь строки во вкладке «Импорт и Вход».
    </div>`;
    return;
  }

  if (VIEW_MODE === 'table') {
    box.innerHTML = `<div class="panel" style="padding:0;overflow-x:auto;">
      <table class="data-tbl">
        <thead>
          <tr>
            <th style="width:34px;"></th>
            <th>Аккаунт</th>
            <th>Статус</th>
            <th>Гео / Кластер</th>
            <th>Прокси Exit</th>
            <th>Посты / Views</th>
            <th style="text-align:right;">Действия</th>
          </tr>
        </thead>
        <tbody>
          ${list.map(a => {
            const aud = a.audit;
            const handle = aud?.uniq_id || a.username || a.account_id;
            const checked = SELECTED_IDS.has(a.account_id) ? 'checked' : '';
            return `<tr>
              <td><input type="checkbox" class="chk" ${checked} onchange="toggleSelect('${esc(a.account_id)}')" /></td>
              <td>
                <div style="font-weight:700;cursor:pointer;" onclick="openDrawer('${esc(a.account_id)}')">@${esc(handle)}</div>
                <div class="mono" style="font-size:11px;color:var(--muted)">${esc(aud?.uid || a.email || '—')}</div>
              </td>
              <td>${healthBadgeHtml(a)}</td>
              <td class="mono" style="font-size:11.5px;">${aud ? `${esc(aud.reg_country||'?')}→${esc(aud.current_region||'?')} · ${esc(aud.idc_gateway.replace('https://api16-normal-',''))}` : esc(a.region)}</td>
              <td class="mono" style="font-size:11.5px;">${aud?.proxy_country ? `<span class="badge b-ok">${esc(aud.proxy_country)} · ${esc(aud.proxy_ip)}</span>` : esc(a.proxy_masked)}</td>
              <td class="mono">${aud ? `${aud.total_posts} · <strong>${aud.total_views.toLocaleString()}</strong>` : '—'}</td>
              <td style="text-align:right;white-space:nowrap;">
                <button class="btn btn-sm" onclick="openDrawer('${esc(a.account_id)}')">Инспектор</button>
                ${a.authenticated ? `<button class="btn btn-sm" onclick="auditAccount('${esc(a.account_id)}')">↻</button>` : `<button class="btn btn-sm btn-primary" onclick="authAccount('${esc(a.account_id)}')">Войти</button>`}
                ${a.authenticated ? `<button class="btn btn-sm btn-emerald" onclick="appealAccount('${esc(a.account_id)}')">⚡</button>` : ''}
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;
    return;
  }

  box.innerHTML = `<div class="cards-grid">${list.map(a => {
    const aud = a.audit;
    const handle = aud?.uniq_id || a.username || a.account_id;
    const sel = SELECTED_IDS.has(a.account_id);
    const avHtml = aud?.avatar_url ? `<img src="${esc(aud.avatar_url)}" alt="" />` : esc(handle.slice(0, 2).toUpperCase());
    const idc = aud?.idc_gateway ? aud.idc_gateway.replace('https://api16-normal-', '') : '—';
    const flagged = aud ? (aud.post_penalties || []).filter(p => p.appeal_status !== 2 || p.v2_can_appeal).length : 0;

    let statusBanner = '';
    if (aud) {
      const vios = aud.notice_violations || [];
      if (vios.length) {
        const appealableCnt = vios.filter(x => x.can_appeal || x.status_code === 1).length;
        statusBanner = vios.slice(0, 4).map(v => `<div class="alert-strip ${v.can_appeal ? 'alert-err' : 'alert-warn'}">
          <div>
            <strong>${esc(v.title)}</strong> <span class="mono" style="opacity:.8">#${esc(v.punish_id)}</span><br/>
            <span>${esc(v.status_label)} · Source: <code class="mono">${esc(v.punish_source || '—')}</code> ${v.appeal_id && v.appeal_id !== '0' ? '· AppealId: ' + esc(v.appeal_id) : ''}</span>
          </div>
          ${v.can_appeal ? `<button class="btn btn-sm btn-primary" onclick="appealAccount('${esc(a.account_id)}', false)">Снять (${appealableCnt})</button>` : ''}
        </div>`).join('');
        if (vios.length > 4) {
          statusBanner += `<div class="alert-strip alert-warn" style="padding:6px 11px;font-size:11.5px;">
            <span>+ ещё <strong>${vios.length - 4}</strong> ограничений в этом же аккаунте (всего ${vios.length}, кнопка «Снять» подаст на все сразу).</span>
          </div>`;
        }
      } else if ((aud.restored_notices || []).length) {
        const r0 = aud.restored_notices[0];
        statusBanner = `<div class="alert-strip alert-ok">
          <span>✓ Огран снят (${aud.restored_notices.length}): <strong>${esc(r0.title)}</strong> (${fmtTs(r0.create_time)})</span>
        </div>`;
      }
      if (aud.region_warning_present) {
        statusBanner += `<div class="alert-strip alert-warn" style="padding:6px 11px;font-size:11.5px;">
          <span>⚠️ Архивная плашка <strong>Update your account region</strong> в инбоксе (сейчас трафик строго заперт в прокси).</span>
        </div>`;
      }
    }

    return `<div class="acc-card ${sel ? 'selected' : ''}">
      <div class="acc-head">
        <div class="acc-id">
          <input type="checkbox" class="chk" ${sel ? 'checked' : ''} onchange="toggleSelect('${esc(a.account_id)}')" />
          <div class="avatar" onclick="openDrawer('${esc(a.account_id)}')">${avHtml}</div>
          <div>
            <div class="acc-handle" onclick="openDrawer('${esc(a.account_id)}')">
              <span class="handle-txt">@${esc(handle)}</span>
              ${healthBadgeHtml(a)}
              ${a.use_outlook_hub ? '<span class="badge b-cyan">Outlook Hub</span>' : ''}
              ${a.has_totp ? '<span class="badge b-neutral">TOTP</span>' : ''}
            </div>
            <div class="acc-sub mono">
              <span>UID: ${esc(aud?.uid || '—')}</span> ·
              <span>${esc(idc)}</span> ·
              <span>Гео: ${aud ? `${esc(aud.reg_country||'?')}→${esc(aud.current_region||'?')}` : esc(a.region)}</span>
            </div>
          </div>
        </div>
        <button class="btn btn-sm" onclick="openDrawer('${esc(a.account_id)}')">Открыть ↗</button>
      </div>

      ${aud ? `<div class="tele-row">
        <div class="tele-box">
          <div class="tele-lbl">Exit Proxy</div>
          <div class="mono" style="color:var(--ok);font-weight:600;">${aud.proxy_country ? `${esc(aud.proxy_country)} · ${esc(aud.proxy_ip)}` : 'OK'}</div>
        </div>
        <div class="tele-box">
          <div class="tele-lbl">TikTok Studio</div>
          <div>Постов: <strong>${aud.total_posts}</strong> · Views: <strong class="mono">${aud.total_views.toLocaleString()}</strong></div>
        </div>
        <div class="tele-box">
          <div class="tele-lbl">Флаги постов</div>
          <div style="font-weight:600;color:${flagged ? 'var(--err)' : 'var(--ok)'}">${flagged ? flagged + ' с флагом FYP' : 'Чисто (0)'}</div>
        </div>
      </div>` : ''}

      ${statusBanner}

      <div class="card-foot">
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          ${!a.authenticated ? `<button class="btn btn-sm btn-primary" onclick="authAccount('${esc(a.account_id)}')">🔑 Войти</button>` : ''}
          ${a.authenticated ? `<button class="btn btn-sm" onclick="auditAccount('${esc(a.account_id)}')">↻ Аудит</button>` : ''}
          ${a.authenticated ? `<button class="btn btn-sm btn-emerald" onclick="appealAccount('${esc(a.account_id)}', false)">⚡ Снять ограны</button>` : ''}
          ${a.authenticated ? `<button class="btn btn-sm" onclick="appealAccount('${esc(a.account_id)}', true)" title="Ждать все 0..10 огранов после залива и снять">⏳ Ждать волну</button>` : ''}
          ${a.authenticated ? `<button class="btn btn-sm" onclick="warmupAccount('${esc(a.account_id)}')">🔥 Прогрев</button>` : ''}
        </div>
        <span class="mono" style="font-size:11px;color:var(--muted);">${aud?.audited_at ? 'обновлено ' + aud.audited_at : ''}</span>
      </div>
    </div>`;
  }).join('')}</div>`;
}

// ── Multi-Select & Bulk Actions ──────────────────────────────
function toggleSelect(aid) {
  if (SELECTED_IDS.has(aid)) SELECTED_IDS.delete(aid); else SELECTED_IDS.add(aid);
  updateBulkBar();
  renderFleet();
}
function toggleSelectAll() {
  const visible = getFilteredAccounts().map(a => a.account_id);
  const allSelected = visible.every(id => SELECTED_IDS.has(id));
  if (allSelected) visible.forEach(id => SELECTED_IDS.delete(id));
  else visible.forEach(id => SELECTED_IDS.add(id));
  updateBulkBar();
  renderFleet();
}
function clearSelection() {
  SELECTED_IDS.clear();
  updateBulkBar();
  renderFleet();
}
function updateBulkBar() {
  const bar = document.getElementById('bulkBar');
  document.getElementById('bulkCount').textContent = SELECTED_IDS.size;
  bar.classList.toggle('hidden', SELECTED_IDS.size === 0);
}
async function runBulk(action, extra = {}) {
  const ids = Array.from(SELECTED_IDS);
  if (!ids.length) return;
  if (action === 'delete' && !confirm(`Удалить ${ids.length} выбранных аккаунтов?`)) return;
  if (action === 'clean_posts' && !confirm(`Безвозвратно удалить ВСЕ прошлые посты на ${ids.length} выбранных аккаунтах?`)) return;
  try {
    await apiPost('/api/bulk-action', {
      account_ids: ids,
      action,
      hide_rejected: document.getElementById('chkHideRejected').checked,
      appeal_studio_posts: document.getElementById('chkIncludeStudio').checked,
      ...extra,
    });
    toast(`Пакетная задача «${action}» запущена (${ids.length} акк.)`, 'success');
    if (action === 'delete') clearSelection();
  } catch (e) { toast(e.message, 'err'); }
}
function promptBulkProxy() {
  const px = prompt('Введи новый прокси для выбранных аккаунтов (socks5://... или ip:port:user:pass):');
  if (px && px.trim()) runBulk('set_proxy', { proxy: px.trim() });
}

// ── Slide-Over Account Inspector Drawer ──────────────────────
function openDrawer(aid) {
  DRAWER_ACCOUNT_ID = aid;
  document.getElementById('drawerBackdrop').classList.remove('hidden');
  renderDrawer();
}
function closeDrawer() {
  DRAWER_ACCOUNT_ID = null;
  document.getElementById('drawerBackdrop').classList.add('hidden');
}

function renderDrawer() {
  const a = (STATE.accounts || []).find(x => x.account_id === DRAWER_ACCOUNT_ID);
  if (!a) return;
  const aud = a.audit;
  const handle = aud?.uniq_id || a.username || a.account_id;

  document.getElementById('drawerTitleGroup').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;">
      <strong style="font-size:16px;">@${esc(handle)}</strong>
      ${healthBadgeHtml(a)}
    </div>
    <div class="mono" style="font-size:11.5px;color:var(--muted);margin-top:3px;">
      UID: ${esc(aud?.uid || '—')} · ${esc(a.email || 'без email')} · Кластер: ${esc(aud?.idc_gateway || '—')}
    </div>`;

  const maxViews = Math.max(1, ...(aud?.post_penalties || []).map(p => p.play_count || 0));

  document.getElementById('drawerBody').innerHTML = `
    <!-- Quick Actions -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      ${!a.authenticated ? `<button class="btn btn-sm btn-primary" onclick="authAccount('${esc(a.account_id)}')">🔑 Авторизоваться</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="auditAccount('${esc(a.account_id)}')">↻ Обновить аудит</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm btn-emerald" onclick="appealAccount('${esc(a.account_id)}', false)">⚡ Снять все ограны</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="appealAccount('${esc(a.account_id)}', true)">⏳ Дождаться волны (0..10)</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="warmupAccount('${esc(a.account_id)}')">🔥 Прогреть ленту</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="restartAccountApp('${esc(a.account_id)}')">🔄 Рестарт ТТ</button>` : ''}
      ${a.authenticated ? `<button class="btn btn-sm" onclick="cleanAccountPosts('${esc(a.account_id)}')">🗑 Очистить посты</button>` : ''}
      <button class="btn btn-sm" onclick="syncGeo('${esc(a.account_id)}','US')">🌎 Sync US SIM</button>
      ${a.use_outlook_hub ? `<button class="btn btn-sm btn-amber" onclick="fetchOutlookOtp('${esc(a.account_id)}')">📬 Прочитать Outlook OTP</button>` : ''}
      ${a.authenticated ? `<a class="btn btn-sm" href="/api/accounts/${encodeURIComponent(a.account_id)}/export?mode=cookies" style="text-decoration:none;">⬇ cookies.json</a>` : ''}
      ${a.authenticated ? `<a class="btn btn-sm" href="/api/accounts/${encodeURIComponent(a.account_id)}/export?mode=session" style="text-decoration:none;">⬇ x-device.json</a>` : ''}
    </div>

    <!-- Proxy Shield Editor -->
    <div class="sec-box">
      <div class="sec-title">
        <span>Прокси и Exit-Node</span>
        <span class="mono" style="color:var(--ok)">${aud?.proxy_country ? `${esc(aud.proxy_country)} · ${esc(aud.proxy_ip)}` : esc(a.proxy_masked)}</span>
      </div>
      <div style="display:flex;gap:8px;">
        <input type="text" id="drProxyIn" placeholder="Заменить прокси (socks5://... или ip:port:user:pass)" />
        <button class="btn btn-sm btn-primary" style="white-space:nowrap;" onclick="saveDrawerProxy('${esc(a.account_id)}')">Проверить и сохранить</button>
      </div>
    </div>

    <!-- Profile / Bio Editor -->
    ${a.authenticated ? `<div class="sec-box">
      <div class="sec-title"><span>Редактор профиля TikTok (Kintaro API)</span></div>
      <div class="grid-2" style="margin-bottom:8px;">
        <input type="text" id="drNick" placeholder="Новый Nickname (опц.)" value="${esc(aud?.nickname || '')}" />
        <input type="text" id="drBioUrl" placeholder="Ссылка в Bio (опц.)" />
      </div>
      <div style="display:flex;gap:8px;">
        <input type="text" id="drBio" placeholder="Текст нового Bio..." />
        <button class="btn btn-sm btn-emerald" style="white-space:nowrap;" onclick="saveDrawerProfile('${esc(a.account_id)}')">Обновить Bio</button>
      </div>
    </div>` : ''}

    <!-- Studio Posts Table -->
    <div class="sec-box">
      <div class="sec-title">
        <span>Посты в TikTok Studio (${aud?.total_posts || 0})</span>
        <span class="mono" style="color:var(--accent-2)">Всего: ${(aud?.total_views || 0).toLocaleString()} views</span>
      </div>
      ${aud && (aud.post_penalties || []).length ? `<div style="overflow-x:auto;">
        <table class="data-tbl">
          <thead><tr><th>Пост</th><th>Просмотры</th><th>Статус FYP</th><th style="text-align:right">Видимость</th></tr></thead>
          <tbody>
            ${aud.post_penalties.map(p => {
              const pct = Math.max(4, Math.round(((p.play_count || 0) / maxViews) * 100));
              const isOk = p.appeal_status === 2 && !p.v2_can_appeal;
              return `<tr>
                <td>
                  <a href="https://www.tiktok.com/@${esc(handle)}/video/${esc(p.item_id)}" target="_blank" class="mono" style="color:#93c5fd;text-decoration:none;font-size:11.5px;">${esc(p.item_id)} ↗</a>
                  <div style="font-size:11px;color:var(--muted);max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(p.desc || '<без описания>')}</div>
                </td>
                <td style="min-width:110px;">
                  <div class="mono" style="font-weight:600;font-size:12px;">${(p.play_count || 0).toLocaleString()}</div>
                  <div class="bar-track" style="height:5px;margin-top:4px;"><div class="bar-fill" style="width:${pct}%"></div></div>
                </td>
                <td>${isOk ? '<span class="badge b-ok">OK</span>' : `<span class="badge b-err">${esc(p.appeal_status_label)} ${esc((p.reason_labels||[]).join(','))}</span>`}</td>
                <td style="text-align:right;white-space:nowrap;">
                  <button class="btn btn-sm" onclick="setPostVis('${esc(a.account_id)}','${esc(p.item_id)}',true)">Private</button>
                  <button class="btn btn-sm btn-ghost" onclick="setPostVis('${esc(a.account_id)}','${esc(p.item_id)}',false)">Public</button>
                  <button class="btn btn-sm btn-rose" title="Удалить пост навсегда" onclick="deleteSinglePost('${esc(a.account_id)}','${esc(p.item_id)}')">🗑</button>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>` : `<div style="color:var(--muted);font-size:12px;">Нажми «Обновить аудит», чтобы загрузить список постов из TikTok Studio.</div>`}
    </div>

    <!-- Recent Logins Timeline -->
    ${aud && (aud.recent_device_logins || []).length ? `<div class="sec-box">
      <div class="sec-title"><span>История входов из системных уведомлений</span></div>
      <div style="display:flex;flex-direction:column;gap:7px;font-size:12px;color:var(--text-dim);">
        ${aud.recent_device_logins.map(lg => `<div style="padding-left:10px;border-left:2px solid var(--border-strong);">${esc(lg)}</div>`).join('')}
      </div>
    </div>` : ''}
  `;
}

// ── Single-Account Actions ───────────────────────────────────
async function auditAll() {
  const b = document.getElementById('btnAuditAll'); b.disabled = true;
  try { await apiPost('/api/audit-all'); toast('HTTP-аудит всех аккаунтов завершён', 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); } finally { b.disabled = false; }
}
async function appealAll(settle = false) {
  const b = document.getElementById('btnAppealAll');
  const bs = document.getElementById('btnSettleAll');
  b.disabled = true; if (bs) bs.disabled = true;
  if (settle) toast('Ожидание всей волны огранов (до 4 мин)...', 'info');
  try {
    await apiPost('/api/appeal-all', {
      hide_rejected: document.getElementById('chkHideRejected').checked,
      appeal_studio_posts: document.getElementById('chkIncludeStudio').checked,
      settle,
    });
    toast('Снятие огранов завершено', 'success');
    await fetchState();
  }
  catch (e) { toast(e.message, 'err'); } finally { b.disabled = false; if (bs) bs.disabled = false; }
}
async function auditAccount(aid) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/audit`); toast(`Аудит @${aid} обновлён`, 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}
async function appealAccount(aid, settle = false) {
  if (settle) toast(`@${aid}: ждём прихода всех 0..10 огранов волны...`, 'info');
  try {
    await apiPost(`/api/accounts/${encodeURIComponent(aid)}/appeal`, {
      hide_rejected: document.getElementById('chkHideRejected').checked,
      appeal_studio_posts: document.getElementById('chkIncludeStudio').checked,
      settle,
    });
    toast(`Снятие огранов @${aid} завершено`, 'success');
    await fetchState();
  }
  catch (e) { toast(e.message, 'err'); }
}
async function authAccount(aid) {
  toast(`Запуск входа @${aid}...`, 'info');
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/auth`); toast(`@${aid} успешно авторизован!`, 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}
async function warmupAccount(aid) {
  toast(`Запуск прогрева @${aid}...`, 'info');
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/warmup`); toast(`Прогрев @${aid} завершён`, 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}
async function syncGeo(aid, reg) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/sync-geo`, { region: reg }); toast(`SIM/GEO @${aid} → ${reg}`, 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}
async function saveDrawerProxy(aid) {
  const px = document.getElementById('drProxyIn').value.trim();
  if (!px) return toast('Введи строку прокси', 'warn');
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/proxy`, { proxy: px });
    toast(`Прокси проверен: ${r.country} · ${r.ip} (${r.latency_ms}мс)`, 'success');
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
async function fetchOutlookOtp(aid) {
  toast(`Чтение кода из Outlook Hub для @${aid}...`, 'info');
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/outlook-otp`);
    navigator.clipboard?.writeText(r.code).catch(() => {});
    toast(`Код TikTok: ${r.code} (скопирован в буфер!)`, 'success');
  } catch (e) { toast(e.message, 'err'); }
}
async function setPostVis(aid, itemId, priv) {
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/posts/${encodeURIComponent(itemId)}/visibility`, { private: priv });
    toast(r.message, 'success');
  } catch (e) { toast(e.message, 'err'); }
}
async function deleteSinglePost(aid, itemId) {
  if (!confirm(`Удалить пост ${itemId} с аккаунта @${aid} навсегда?`)) return;
  try {
    const r = await apiJson(`/api/accounts/${encodeURIComponent(aid)}/posts/${encodeURIComponent(itemId)}`, { method: 'DELETE' });
    toast(r.message || `Пост ${itemId} удалён`, 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}
async function toggleWatchdog() {
  const next = !(STATE.watchdog && STATE.watchdog.enabled);
  await apiPost('/api/watchdog', { enabled: next, hide_rejected: document.getElementById('chkHideRejected').checked });
  toast(next ? 'Сторож 24/7 запущен' : 'Сторож остановлен', next ? 'success' : 'warn');
  await fetchState();
}

// ── Smart Batch Import + Live Parser Preview ─────────────────
function loadTxtFile(ev) {
  const f = ev.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    document.getElementById('impLines').value = reader.result || '';
    scheduleLinePreview();
    toast(`Загружен файл ${f.name}`, 'success');
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
    document.getElementById('previewCountBadge').textContent = '0 валидных';
    document.getElementById('previewTableBox').innerHTML = 'Вставь строки слева для мгновенного разбора.';
    return;
  }
  try {
    const r = await apiPost('/api/preview-lines', { lines_text, proxies_text, region });
    document.getElementById('previewCountBadge').textContent = `${r.valid_count} / ${r.rows.length} валидных`;
    document.getElementById('previewTableBox').innerHTML = `<table class="data-tbl">
      <thead><tr><th>Аккаунт</th><th>Формат</th><th>2FA</th><th>Прокси</th></tr></thead>
      <tbody>${r.rows.map(x => x.ok
        ? `<tr><td class="mono">${esc(x.account_id)}</td><td><span class="badge b-cyan">${esc(x.format)}</span></td><td>${x.has_2fa ? '✓' : '—'}</td><td class="mono" style="font-size:11px;">${esc(x.proxy_masked)}</td></tr>`
        : `<tr><td colspan="4" style="color:var(--err)">✕ ${esc(x.raw)} — ${esc(x.error)}</td></tr>`
      ).join('')}</tbody>
    </table>`;
  } catch (e) { /* ignore transient typing */ }
}

async function submitBatchImport() {
  try {
    const r = await apiPost('/api/import-batch', {
      lines_text: document.getElementById('impLines').value,
      proxies_text: document.getElementById('impProxies').value,
      region: document.getElementById('impRegion').value,
      auto_login: document.getElementById('impAutoLogin').checked,
      auto_clean_posts: document.getElementById('impAutoCleanPosts')?.checked || false,
      auto_appeal: document.getElementById('impAutoAppeal').checked,
      hide_rejected: document.getElementById('chkHideRejected').checked,
    });
    toast(`Импортировано ${r.imported.length} акк. Конвейер запущен в фоне!`, 'success');
    document.getElementById('impLines').value = '';
    switchView('fleet');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

async function submitFolderImport() {
  const folder_path = document.getElementById('folderPathInput').value.trim();
  if (!folder_path) return toast('Укажи путь к папке сессий', 'warn');
  try {
    const r = await apiPost('/api/import-folder', {
      folder_path,
      proxy: document.getElementById('impProxies').value.split('\n')[0] || '',
      region: document.getElementById('impRegion').value,
    });
    toast(`Импортировано готовых сессий: ${r.imported.length}`, 'success');
    switchView('fleet');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

// ── Upload Studio: Media Dropzone + Spintax + Fan-Out ────────
function handleMediaFiles(fileList) {
  for (const f of fileList) UPLOAD_FILES.push(f);
  renderMediaThumbs();
}
function removeMediaFile(idx) {
  UPLOAD_FILES.splice(idx, 1);
  renderMediaThumbs();
}
function renderMediaThumbs() {
  const box = document.getElementById('mediaThumbs');
  box.innerHTML = UPLOAD_FILES.map((f, i) => {
    const url = URL.createObjectURL(f);
    const isVid = f.name.toLowerCase().endsWith('.mp4');
    const kb = (f.size / 1024).toFixed(0) + ' КБ';
    return `<div class="thumb">
      ${isVid ? `<video src="${url}" muted></video>` : `<img src="${url}" alt="" />`}
      <button class="thumb-rm" onclick="removeMediaFile(${i})">✕</button>
      <div class="thumb-meta mono">${isVid ? 'MP4' : 'IMG'} · ${kb}</div>
    </div>`;
  }).join('');
}

function scheduleSpintaxPreview() {
  clearTimeout(spintaxTimer);
  spintaxTimer = setTimeout(async () => {
    const t = document.getElementById('upDesc').value;
    if (!t.trim()) { document.getElementById('spintaxBox').innerHTML = ''; return; }
    try {
      const r = await apiPost('/api/spintax-preview', { template: t });
      document.getElementById('spintaxBox').innerHTML = r.variants.map((v, i) =>
        `<div style="font-size:11.5px;padding:6px 10px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid var(--border);">
          <span class="mono" style="color:var(--muted)">#${i + 1}</span> ${esc(v.text)}
        </div>`
      ).join('');
    } catch (_) {}
  }, 220);
}

function renderUploadTargets() {
  const allAccs = (STATE.accounts || []).slice().sort((a, b) => Number(b.authenticated) - Number(a.authenticated));
  const box = document.getElementById('uploadTargetsList');
  if (!allAccs.length) {
    box.innerHTML = `<div style="color:var(--muted);font-size:12px;">Нет аккаунтов в базе.</div>`;
    return;
  }
  box.innerHTML = allAccs.map(a => {
    const checked = UPLOAD_TARGETS.has(a.account_id) ? 'checked' : '';
    return `<label style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-radius:11px;background:rgba(8,11,19,0.7);border:1px solid var(--border);cursor:pointer;">
      <span style="display:flex;align-items:center;gap:9px;">
        <input type="checkbox" class="chk" ${checked} onchange="toggleUploadTarget('${esc(a.account_id)}')" />
        <strong>@${esc(a.audit?.uniq_id || a.username)}</strong>
        ${healthBadgeHtml(a)}
      </span>
      <span class="mono" style="font-size:11px;color:var(--muted);">${esc(a.region)} · постов: ${a.audit?.total_posts ?? a.uploads_count}</span>
    </label>`;
  }).join('');
}
function toggleUploadTarget(aid) {
  if (UPLOAD_TARGETS.has(aid)) UPLOAD_TARGETS.delete(aid); else UPLOAD_TARGETS.add(aid);
}
function toggleAllUploadTargets() {
  const allIds = (STATE.accounts || []).map(a => a.account_id);
  const all = allIds.length > 0 && allIds.every(id => UPLOAD_TARGETS.has(id));
  if (all) UPLOAD_TARGETS.clear(); else allIds.forEach(id => UPLOAD_TARGETS.add(id));
  renderUploadTargets();
}

async function submitUpload() {
  if (!UPLOAD_FILES.length) return toast('Перетащи или выбери MP4 / фото!', 'warn');
  const fd = new FormData();
  UPLOAD_FILES.forEach(f => fd.append('files', f));
  fd.append('account_ids', Array.from(UPLOAD_TARGETS).join(','));
  fd.append('description', document.getElementById('upDesc').value);
  fd.append('music_search', document.getElementById('upMusic').value);
  fd.append('skip_warmup', document.getElementById('upSkipWarmup').checked);
  fd.append('force_cooldown', document.getElementById('upForceCooldown').checked);
  fd.append('delete_old_posts', document.getElementById('upDelOld')?.checked || false);
  fd.append('appeal_after_upload', document.getElementById('upAutoAppeal')?.checked || false);

  const btn = document.getElementById('btnUpload'); btn.disabled = true;
  try {
    const r = await apiJson('/api/upload', { method: 'POST', body: fd });
    const okCnt = r.results.filter(x => x.ok).length;
    toast(`Залито успешно: ${okCnt} / ${r.results.length}`, okCnt ? 'success' : 'err');
    UPLOAD_FILES = [];
    renderMediaThumbs();
    await fetchState();
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function previewUniquifier() {
  const photo = UPLOAD_FILES.find(f => !f.name.toLowerCase().endsWith('.mp4'));
  if (!photo) return toast('Сначала добавь фото-слайд для предпросмотра уникализатора!', 'warn');
  const fd = new FormData();
  fd.append('file', photo);
  fd.append('photo_mode', document.getElementById('upPhotoMode').value);
  try {
    const r = await apiJson('/api/uniquifier/preview', { method: 'POST', body: fd });
    const box = document.getElementById('uniqPreviewBox');
    box.classList.remove('hidden');
    box.innerHTML = `
      <div><div class="mono" style="font-size:11px;color:var(--muted);margin-bottom:4px;">ДО (${r.before_kb} КБ)</div><img src="${r.before_data_url}" style="width:100%;border-radius:8px;border:1px solid var(--border);" /></div>
      <div><div class="mono" style="font-size:11px;color:var(--ok);margin-bottom:4px;">ПОСЛЕ · ${esc(r.mode)} (${r.after_kb} КБ)</div><img src="${r.after_data_url}" style="width:100%;border-radius:8px;border:1px solid var(--ok);" /></div>`;
    toast(`Предпросмотр ${r.mode} готов`, 'success');
  } catch (e) { toast(e.message, 'err'); }
}

async function submitCampaign() {
  if (!UPLOAD_FILES.length && !SERVER_MEDIA_SELECTED.size) {
    return toast('Добавь 1–10 фото-слайдов, MP4 видео или выбери файлы из state/media/!', 'warn');
  }
  const fd = new FormData();
  UPLOAD_FILES.forEach(f => fd.append('files', f));
  fd.append('server_media_files', Array.from(SERVER_MEDIA_SELECTED).join(','));
  fd.append('account_ids', Array.from(UPLOAD_TARGETS).join(','));
  fd.append('description', document.getElementById('upDesc').value);
  fd.append('photo_mode', document.getElementById('upPhotoMode').value);
  fd.append('auto_music_query', document.getElementById('upMusic').value);
  fd.append('threads', document.getElementById('upThreads').value || '5');
  fd.append('copies_count', document.getElementById('upCopies').value || '6');
  fd.append('restart_every_n', document.getElementById('upRestartN').value || '3');
  fd.append('delay_min_sec', document.getElementById('upDelayMin').value || '14');
  fd.append('delay_max_sec', document.getElementById('upDelayMax').value || '20');
  fd.append('region', (document.getElementById('upRegion').value || 'US').toUpperCase());
  fd.append('randomize_tags', document.getElementById('upRandTags').checked);
  fd.append('delete_old_posts', document.getElementById('upDelOld').checked);
  fd.append('auto_appeal_2048', document.getElementById('upAutoAppeal').checked);
  fd.append('pin_sticky_proxy', document.getElementById('upStickyProxy').checked);

  const btn = document.getElementById('btnRunCampaign'); btn.disabled = true;
  try {
    const r = await apiJson('/api/campaign/run', { method: 'POST', body: fd });
    toast(`🚀 Кампания запущена в фоне на ${r.accounts} акк.! Следи за Live Console.`, 'success');
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function loadSortedFolders() {
  const grid = document.getElementById('sortedFoldersGrid');
  if (!grid) return;
  try {
    const r = await apiJson('/api/folders');
    const icons = { clean: '✅', ogran_na_proverke: '⏳', ogran_ne_snyat: '🚨', errors: '❌' };
    grid.innerHTML = (r.folders || []).map(f => `
      <div style="background:rgba(15,23,42,0.55);border:1px solid var(--border);border-radius:10px;padding:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <span style="font-size:12px;font-weight:600;">${icons[f.key] || '📁'} ${esc(f.title)}</span>
          <strong class="mono" style="font-size:15px;">${f.count}</strong>
        </div>
        <div style="display:flex;gap:6px;">
          <a class="btn btn-sm" href="/api/folders/${encodeURIComponent(f.key)}/export?fmt=txt" style="text-decoration:none;flex:1;text-align:center;">⬇ TXT</a>
          <a class="btn btn-sm" href="/api/folders/${encodeURIComponent(f.key)}/export?fmt=zip" style="text-decoration:none;flex:1;text-align:center;">📦 ZIP</a>
        </div>
      </div>`).join('');
  } catch (_) {}
}

async function loadPresets() {
  const sel = document.getElementById('campPresetSelect');
  if (!sel) return;
  try {
    const r = await apiJson('/api/presets');
    CAMPAIGN_PRESETS = r.campaign_presets || [];
    sel.innerHTML = '<option value="">⚡ Выбрать пресет...</option>' +
      CAMPAIGN_PRESETS.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  } catch (_) {}
}

function applySelectedPreset() {
  const pid = document.getElementById('campPresetSelect').value;
  const p = CAMPAIGN_PRESETS.find(x => x.id === pid);
  if (!p) return;
  if (p.description != null) document.getElementById('upDesc').value = p.description;
  if (p.photo_mode) document.getElementById('upPhotoMode').value = p.photo_mode;
  if (p.auto_music_query != null) document.getElementById('upMusic').value = p.auto_music_query;
  if (p.threads) document.getElementById('upThreads').value = p.threads;
  if (p.copies_count) document.getElementById('upCopies').value = p.copies_count;
  if (p.restart_every_n != null) document.getElementById('upRestartN').value = p.restart_every_n;
  if (p.delay_min_sec != null) document.getElementById('upDelayMin').value = p.delay_min_sec;
  if (p.delay_max_sec != null) document.getElementById('upDelayMax').value = p.delay_max_sec;
  if (p.region) document.getElementById('upRegion').value = p.region;
  document.getElementById('upRandTags').checked = Boolean(p.randomize_tags);
  document.getElementById('upDelOld').checked = Boolean(p.delete_old_posts);
  document.getElementById('upAutoAppeal').checked = Boolean(p.auto_appeal_2048);
  toast(`Применён пресет: ${p.name}`, 'success');
}

async function saveCurrentPreset() {
  const name = prompt('Название нового пресета кампании:', 'Мой US пресет (minimal_snow)');
  if (!name) return;
  const payload = {
    name,
    description: document.getElementById('upDesc').value,
    photo_mode: document.getElementById('upPhotoMode').value,
    auto_music_query: document.getElementById('upMusic').value,
    threads: parseInt(document.getElementById('upThreads').value || '5', 10),
    copies_count: parseInt(document.getElementById('upCopies').value || '6', 10),
    restart_every_n: parseInt(document.getElementById('upRestartN').value || '3', 10),
    delay_min_sec: parseFloat(document.getElementById('upDelayMin').value || '14'),
    delay_max_sec: parseFloat(document.getElementById('upDelayMax').value || '20'),
    region: (document.getElementById('upRegion').value || 'US').toUpperCase(),
    randomize_tags: document.getElementById('upRandTags').checked,
    delete_old_posts: document.getElementById('upDelOld').checked,
    auto_appeal_2048: document.getElementById('upAutoAppeal').checked,
  };
  try {
    await apiPost('/api/presets/save', payload);
    toast('💾 Пресет сохранён!', 'success');
    await loadPresets();
  } catch (e) { toast(e.message, 'err'); }
}

async function loadMediaLibrary() {
  const grid = document.getElementById('serverMediaGrid');
  if (!grid) return;
  try {
    const r = await apiJson('/api/media');
    const files = r.files || [];
    if (!files.length) {
      grid.innerHTML = '<span style="font-size:11.5px;color:var(--muted);">Пока пусто — загрузи файлы выше и нажми «В библиотеку» или склонируй из Шпиона.</span>';
      return;
    }
    grid.innerHTML = files.map(f => {
      const sel = SERVER_MEDIA_SELECTED.has(f.name);
      return `<div onclick="toggleServerMedia('${esc(f.name)}')" style="cursor:pointer;padding:4px 8px;border-radius:7px;font-size:11.5px;border:1px solid ${sel ? 'var(--accent)' : 'var(--border)'};background:${sel ? 'rgba(99,102,241,0.22)' : 'rgba(15,23,42,0.5)'};display:flex;align-items:center;gap:6px;">
        <span>${f.kind === 'video' ? '🎬' : '🖼'} ${esc(f.name)} <span style="color:var(--muted);">(${f.size_kb}КБ)</span></span>
        <span onclick="event.stopPropagation();deleteServerMedia('${esc(f.name)}')" style="color:var(--err);font-weight:700;">×</span>
      </div>`;
    }).join('');
  } catch (_) {}
}

function toggleServerMedia(name) {
  if (SERVER_MEDIA_SELECTED.has(name)) SERVER_MEDIA_SELECTED.delete(name);
  else SERVER_MEDIA_SELECTED.add(name);
  loadMediaLibrary();
}

async function saveFilesToLibrary() {
  if (!UPLOAD_FILES.length) return toast('Сначала перетащи файлы в зону загрузки!', 'warn');
  const fd = new FormData();
  UPLOAD_FILES.forEach(f => fd.append('files', f));
  try {
    const r = await apiJson('/api/media/upload', { method: 'POST', body: fd });
    (r.saved || []).forEach(n => SERVER_MEDIA_SELECTED.add(n));
    toast(`💾 Сохранено в библиотеку: ${(r.saved || []).length} шт.`, 'success');
    await loadMediaLibrary();
  } catch (e) { toast(e.message, 'err'); }
}

async function deleteServerMedia(name) {
  try {
    await apiJson(`/api/media/${encodeURIComponent(name)}`, { method: 'DELETE' });
    SERVER_MEDIA_SELECTED.delete(name);
    await loadMediaLibrary();
  } catch (e) { toast(e.message, 'err'); }
}

async function submitRawSessionsImport() {
  const raw = (document.getElementById('rawSessInput').value || '').trim();
  if (!raw) return toast('Вставь JSON / JSONL с x-device!', 'warn');
  try {
    const r = await apiPost('/api/sessions/import-raw', {
      raw_text: raw,
      default_proxy: (document.getElementById('impProxies')?.value || '').split('\n')[0] || '',
      default_region: document.getElementById('impRegion')?.value || 'US',
    });
    toast(`📥 Импортировано сессий: ${(r.imported || []).length}`, 'success');
    document.getElementById('rawSessInput').value = '';
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

function populateSpySeedAccounts() {
  const sel = document.getElementById('cmtSeedAccount');
  if (!sel) return;
  const authed = (STATE.accounts || []).filter(a => a.authenticated);
  sel.innerHTML = authed.map(a => `<option value="${esc(a.account_id)}">@${esc(a.username || a.account_id)}</option>`).join('');
}

async function cloneSpyCreative(awemeId) {
  try {
    const r = await apiPost('/api/spy/download', { target_post: awemeId });
    (r.saved_files || []).forEach(n => SERVER_MEDIA_SELECTED.add(n));
    toast(`🕵️ Склонировано без водянок: ${(r.saved_files || []).join(', ')} (добавлено в библиотеку!)`, 'success');
  } catch (e) { toast(e.message, 'err'); }
}

async function loadPostComments() {
  const tp = (document.getElementById('cmtPostInput').value || '').trim();
  if (!tp) return toast('Вставь ссылку на пост или AWEME ID', 'warn');
  try {
    const r = await apiPost('/api/comments/list', { target_post: tp, count: 25 });
    document.getElementById('cmtResultBox').innerHTML = (r.comments || []).slice(0, 10).map(c => `
      <div style="padding:5px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;gap:8px;">
        <div><strong>@${esc(c.username)}</strong>: ${esc(c.text)} <span class="mono" style="color:var(--muted);">(${c.likes} ❤️)</span></div>
        <button class="btn btn-sm btn-ghost" onclick="document.getElementById('cmtExistingCid').value='${esc(c.cid)}'">Выбрать CID</button>
      </div>`).join('') || 'Комментариев нет.';
  } catch (e) { toast(e.message, 'err'); }
}

async function submitCommentBoost() {
  const tp = (document.getElementById('cmtPostInput').value || '').trim();
  if (!tp) return toast('Вставь ссылку на пост или AWEME ID', 'warn');
  const btn = document.getElementById('btnBoostCmt'); btn.disabled = true;
  try {
    const r = await apiPost('/api/comments/boost', {
      target_post: tp,
      seed_account_id: document.getElementById('cmtSeedAccount').value || '',
      existing_cid: document.getElementById('cmtExistingCid').value || '',
      lead_comment_text: document.getElementById('cmtLeadText').value || '',
      reply_templates: document.getElementById('cmtReplies').value || '',
      reply_prob_pct: 40,
      threads: 4,
    });
    toast(`💬 Коммент ${r.cid} забущен: ${r.likes_ok} лайков, ${r.replies_ok} ответов!`, 'success');
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function submitEngagementBoost() {
  const btn = document.getElementById('btnRunBoost'); btn.disabled = true;
  try {
    const r = await apiPost('/api/boost/run', {
      account_ids: Array.from(SELECTED_IDS),
      target_post: document.getElementById('boostPostInput').value || '',
      target_username: document.getElementById('boostUserInput').value || '',
      do_view: document.getElementById('boostView').checked,
      do_like: document.getElementById('boostLike').checked,
      do_collect: document.getElementById('boostCollect').checked,
      do_repost: document.getElementById('boostRepost').checked,
      comments_text: document.getElementById('boostComments').value || '',
      threads: 4,
    });
    toast(`🔥 Буст выполнен: ${r.success}/${r.total}`, 'success');
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function submitBulkBranding() {
  const btn = document.getElementById('btnRunBrand'); btn.disabled = true;
  try {
    let avatarDataUrl = '';
    const f = document.getElementById('brAvatarFile').files?.[0];
    if (f) {
      avatarDataUrl = await new Promise((res, rej) => {
        const rd = new FileReader(); rd.onload = () => res(rd.result); rd.onerror = rej; rd.readAsDataURL(f);
      });
    }
    const r = await apiPost('/api/branding/run', {
      account_ids: Array.from(SELECTED_IDS),
      avatar_data_url: avatarDataUrl,
      uniquify_avatar: document.getElementById('brUniqAv').checked,
      nickname_template: document.getElementById('brNick').value || '',
      username_prefix: document.getElementById('brUnamePrefix').value || '',
      bio_template: document.getElementById('brBio').value || '',
      bio_url: document.getElementById('brBioUrl').value || '',
      new_password: document.getElementById('brNewPass').value || '',
      enable_totp: document.getElementById('brEnableTotp').checked,
      threads: 3,
    });
    toast(`🎨 Оформлено профилей: ${r.success}/${r.total}`, 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function loadAutoPilotJobs() {
  const box = document.getElementById('autopilotList');
  if (!box) return;
  try {
    const r = await apiJson('/api/autopilot');
    const jobs = r.jobs || [];
    box.innerHTML = jobs.length ? jobs.map(j => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);">
        <div><strong class="mono">${esc(j.action)}</strong> · каждые ${j.interval_minutes}м · след. через ${j.next_run_in_sec}с · запусков: ${j.runs_count}</div>
        <button class="btn btn-sm btn-rose" onclick="deleteAutoPilotJob('${esc(j.id)}')">✕</button>
      </div>`).join('') : 'Задач Авто-Пилота пока нет.';
  } catch (_) {}
}

async function addAutoPilotJob() {
  try {
    await apiPost('/api/autopilot/add', {
      action: document.getElementById('apAction').value,
      interval_minutes: parseInt(document.getElementById('apInterval').value || '30', 10),
    });
    toast('⏰ Задача Авто-Пилота добавлена', 'success');
    await loadAutoPilotJobs();
  } catch (e) { toast(e.message, 'err'); }
}

async function deleteAutoPilotJob(jid) {
  try {
    await apiJson(`/api/autopilot/${encodeURIComponent(jid)}`, { method: 'DELETE' });
    await loadAutoPilotJobs();
  } catch (e) { toast(e.message, 'err'); }
}

async function restartAccountApp(aid) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/restart`); toast(`🔄 TikTok перезапущен на @${aid}`, 'success'); await fetchState(); }
  catch (e) { toast(e.message, 'err'); }
}

async function cleanAccountPosts(aid) {
  if (!confirm(`Удалить все старые посты аккаунта ${aid}?`)) return;
  try {
    const r = await apiPost(`/api/accounts/${encodeURIComponent(aid)}/clean-posts`);
    toast(`🗑 Удалено старых постов: ${r.deleted}`, 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); }
}

async function runCompetitorSpy() {
  const uname = (document.getElementById('spyUserInput').value || '').trim();
  if (!uname) return toast('Введи @username конкурента', 'warn');
  const btn = document.getElementById('btnSpy'); btn.disabled = true;
  try {
    const r = await apiPost('/api/spy', { username: uname, count: 18 });
    const s = r.spy;
    document.getElementById('spyResultBox').innerHTML = `
      <div style="margin-bottom:8px;"><strong>@${esc(s.username)}</strong> · Подписчики: <span class="mono">${s.followers}</span> · Постов: <span class="mono">${s.posts_scanned}</span></div>
      <div class="mono" style="margin-bottom:8px;color:var(--accent-2);">Всего: ${s.total_views.toLocaleString()} · Средние: ${s.avg_views.toLocaleString()} · Макс: ${s.max_views.toLocaleString()}</div>
      <div style="margin-bottom:8px;"><strong>Топ теги:</strong> ${(s.top_hashtags || []).slice(0, 8).map(t => `<span class="badge b-neutral">${esc(t.tag)} (${t.count})</span>`).join(' ')}</div>
      <div style="margin-bottom:10px;"><strong>Топ музыка:</strong> ${(s.top_music || []).slice(0, 4).map(m => `<div class="mono" style="font-size:11px;">• ${esc(m.label)} (${esc(m.music_id)}) ×${m.count}</div>`).join('')}</div>
      <div><strong>Посты (1 клик — скачать без водянок в библиотеку):</strong>
        ${(s.posts || []).slice(0, 8).map(p => `
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);">
            <div style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
              <span class="mono" style="color:var(--ok);">${p.views.toLocaleString()}</span> · ${p.kind === 'photo' ? `🖼×${p.slides_count}` : '🎬'} ${esc(p.desc || p.aweme_id)}
            </div>
            <button class="btn btn-sm btn-emerald" style="white-space:nowrap;" onclick="cloneSpyCreative('${esc(p.aweme_id)}')">⬇ Клонировать</button>
          </div>`).join('')}
      </div>`;
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function generateTotpUi() {
  const sec = (document.getElementById('totpSecretInput').value || '').trim();
  if (!sec) return toast('Вставь Base32 2FA ключ', 'warn');
  try {
    const r = await apiPost('/api/totp/generate', { secret: sec });
    document.getElementById('totpResultBox').textContent = `${r.code}  (осталось ${r.remaining_sec}с)`;
  } catch (e) { toast(e.message, 'err'); }
}

// ── Analytics & Shadowban Radar ──────────────────────────────
function renderAnalytics() {
  const accs = (STATE.accounts || []).filter(a => a.audit);
  const maxV = Math.max(1, ...accs.map(a => a.audit.total_views || 0));
  document.getElementById('viewsBarChart').innerHTML = accs.length
    ? accs.sort((a, b) => (b.audit.total_views || 0) - (a.audit.total_views || 0)).map(a => {
        const pct = Math.max(2, Math.round(((a.audit.total_views || 0) / maxV) * 100));
        return `<div class="bar-row">
          <div class="mono" style="overflow:hidden;text-overflow:ellipsis;">@${esc(a.audit.uniq_id || a.username)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
          <div class="mono" style="text-align:right;font-weight:600;">${(a.audit.total_views || 0).toLocaleString()}</div>
        </div>`;
      }).join('')
    : '<div style="color:var(--muted);font-size:12px;">Сначала запусти HTTP-аудит для построения графиков.</div>';

  // Pure SVG Donut for Health Distribution
  let elig = 0, app = 0, act = 0, un = 0;
  (STATE.accounts || []).forEach(a => {
    if (!a.authenticated || !a.audit) un++;
    else if (a.audit.account_health === 'eligible') elig++;
    else if (a.audit.account_health === 'appealing') app++;
    else act++;
  });
  const total = Math.max(1, elig + app + act + un);
  const r = 56, C = 2 * Math.PI * r;
  const segs = [
    { val: elig, color: '#10b981', label: 'Eligible' },
    { val: app, color: '#f59e0b', label: 'Appealing' },
    { val: act, color: '#f43f5e', label: 'Action Needed' },
    { val: un, color: '#475569', label: 'Ожидают' },
  ];
  let offset = 0;
  const circles = segs.filter(s => s.val > 0).map(s => {
    const len = (s.val / total) * C;
    const svg = `<circle cx="72" cy="72" r="${r}" fill="none" stroke="${s.color}" stroke-width="15" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-offset}" transform="rotate(-90 72 72)" />`;
    offset += len;
    return svg;
  }).join('');

  document.getElementById('healthDonutChart').innerHTML = `
    <svg width="144" height="144" viewBox="0 0 144 144">
      <circle cx="72" cy="72" r="${r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="15" />
      ${circles}
      <text x="72" y="70" text-anchor="middle" fill="#f1f5f9" font-size="22" font-weight="700">${(STATE.accounts || []).length}</text>
      <text x="72" y="88" text-anchor="middle" fill="#64748b" font-size="10.5">ACCOUNTS</text>
    </svg>
    <div style="display:flex;flex-direction:column;gap:7px;font-size:12px;">
      ${segs.map(s => `<div style="display:flex;align-items:center;gap:8px;">
        <span style="width:10px;height:10px;border-radius:3px;background:${s.color};display:inline-block;"></span>
        <span style="color:var(--text-dim)">${s.label}:</span> <strong>${s.val}</strong>
      </div>`).join('')}
    </div>`;

  const sh = STATE.shadows || [];
  if (sh.length) {
    document.getElementById('shadowsTableBox').innerHTML = `<table class="data-tbl">
      <thead><tr><th>Аккаунт</th><th>Aweme ID</th><th>Возраст (мин)</th><th>Просмотры</th><th>Публичен</th><th>Вердикт</th></tr></thead>
      <tbody>${sh.map(x => `<tr>
        <td>@${esc(x.account_id)}</td>
        <td class="mono">${esc(x.aweme_id)}</td>
        <td class="mono">${Number(x.age_minutes).toFixed(1)}</td>
        <td class="mono">${x.play_count ?? '—'}</td>
        <td>${x.found_in_public_get ? 'Да' : 'Нет'}</td>
        <td><span class="badge ${x.suspected_shadowban ? 'b-err' : 'b-ok'}">${esc(x.verdict)}</span></td>
      </tr>`).join('')}</tbody>
    </table>`;
  }
}

async function runShadowCheck() {
  const btn = document.getElementById('btnCheckShadows'); btn.disabled = true;
  try {
    await apiPost('/api/shadows/check');
    toast('Радар теней завершён', 'success');
    await fetchState();
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

// ── Proxy Tester & Config Editor ─────────────────────────────
async function runProxyTest() {
  const proxies_text = document.getElementById('proxyTestInput').value;
  if (!proxies_text.trim()) return toast('Вставь хотя бы один прокси', 'warn');
  const btn = document.getElementById('btnTestProxies'); btn.disabled = true;
  try {
    const r = await apiPost('/api/proxies/test', { proxies_text });
    document.getElementById('proxyTestResults').innerHTML = `<table class="data-tbl">
      <thead><tr><th>Прокси</th><th>GEO / IP</th><th>Пинг</th><th>Статус</th></tr></thead>
      <tbody>${r.results.map(x => `<tr>
        <td class="mono" style="font-size:11px;">${esc(x.proxy_masked)}</td>
        <td class="mono">${x.ok ? `${esc(x.country)} · ${esc(x.ip)}` : '—'}</td>
        <td class="mono">${x.ok ? x.latency_ms + ' мс' : '—'}</td>
        <td>${x.ok ? '<span class="badge b-ok">OK</span>' : `<span class="badge b-err">${esc(x.error)}</span>`}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
}

async function loadConfig() {
  try {
    const c = await apiJson('/api/config');
    document.getElementById('cfgKeyBadge').textContent = c.has_api_key ? `API Key: ${c.api_key_masked}` : 'API Key не задан';
    document.getElementById('cfgParallel').value = c.max_parallel_accounts;
    document.getElementById('cfgDailyMax').value = c.max_posts_per_account_per_day;
    document.getElementById('cfgAgeMin').value = c.min_session_age_minutes_before_first_post;
    document.getElementById('cfgGapHours').value = c.min_hours_between_posts_per_account;
    document.getElementById('cfgWarmupOn').checked = c.warmup_enabled;
    document.getElementById('cfgUniqOn').checked = c.uniquifier_enabled;
  } catch (e) { toast(e.message, 'err'); }
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
    toast('config.toml успешно обновлён!', 'success');
    await loadConfig();
  } catch (e) { toast(e.message, 'err'); }
}

// ── Command Palette (Ctrl+K) & Keyboard Shortcuts ────────────
function openCmdK() {
  document.getElementById('cmdkBackdrop').classList.remove('hidden');
  const inp = document.getElementById('cmdkInput');
  inp.value = '';
  inp.focus();
  renderCmdK();
}
function closeCmdK() { document.getElementById('cmdkBackdrop').classList.add('hidden'); }

function renderCmdK() {
  const q = (document.getElementById('cmdkInput').value || '').trim().toLowerCase();
  const actions = [
    { label: '↻ Запустить HTTP-аудит всех аккаунтов', hint: '0$ Kintaro', run: () => auditAll() },
    { label: '⚡ Снять все ограны на всех аккаунтах', hint: '3-Channel', run: () => appealAll() },
    { label: '🛡️ Переключить фонового сторожа Watchdog', hint: '95–165s', run: () => toggleWatchdog() },
    { label: '📥 Открыть пакетный импорт / Outlook Hub', hint: 'View', run: () => switchView('import') },
    { label: '🚀 Открыть студию анти-тень залива', hint: 'View', run: () => switchView('upload') },
    { label: '📊 Открыть аналитику и радар теней', hint: 'View', run: () => switchView('analytics') },
    { label: '⚙️ Открыть тестер прокси и config.toml', hint: 'View', run: () => switchView('settings') },
  ];
  (STATE.accounts || []).forEach(a => {
    const h = a.audit?.uniq_id || a.username || a.account_id;
    actions.push({ label: `Инспектор аккаунта @${h}`, hint: a.audit?.account_health || 'account', run: () => { switchView('fleet'); openDrawer(a.account_id); } });
  });
  const matches = actions.filter(x => !q || x.label.toLowerCase().includes(q) || x.hint.toLowerCase().includes(q));
  document.getElementById('cmdkList').innerHTML = matches.map((m, idx) =>
    `<button class="cmdk-item" data-idx="${idx}"><span>${esc(m.label)}</span><span class="mono" style="font-size:11px;color:var(--muted)">${esc(m.hint)}</span></button>`
  ).join('');
  document.querySelectorAll('.cmdk-item').forEach((el, i) => {
    el.onclick = () => { closeCmdK(); matches[i].run(); };
  });
}

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openCmdK(); }
  else if (e.key === 'Escape') { closeCmdK(); closeDrawer(); }
  else if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
    e.preventDefault(); switchView('fleet'); document.getElementById('fleetSearch').focus();
  }
});

// ── Filterable Live SSE Console ──────────────────────────────
function setLogLevel(lvl, btn) {
  LOG_LEVEL = lvl;
  document.querySelectorAll('#consoleDock .chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  renderConsole();
}
function clearConsole() { ALL_LOGS = []; renderConsole(); }
function renderConsole() {
  const box = document.getElementById('consoleBody');
  const shown = ALL_LOGS.filter(x => LOG_LEVEL === 'all' || x.level === LOG_LEVEL);
  box.innerHTML = shown.map(e =>
    `<div class="log-line"><span class="log-ts">[${esc(e.time)}]</span><span class="log-${esc(e.level || 'info')}">${esc(e.text)}</span></div>`
  ).join('');
  box.scrollTop = box.scrollHeight;
}

// ── Drag & Drop Bindings + Boot ──────────────────────────────
function bindDropzone(elId, onFiles) {
  const el = document.getElementById(elId);
  if (!el) return;
  ['dragenter', 'dragover'].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.remove('drag'); }));
  el.addEventListener('drop', e => { if (e.dataTransfer?.files?.length) onFiles(e.dataTransfer.files); });
}

(async function boot() {
  bindDropzone('mediaDropzone', files => handleMediaFiles(files));
  bindDropzone('txtDropzone', files => loadTxtFile({ target: { files } }));
  await fetchState();
  loadSortedFolders();
  ALL_LOGS = (STATE.logs || []).slice();
  renderConsole();

  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    try {
      const item = JSON.parse(ev.data);
      if (item.text) {
        ALL_LOGS.push(item);
        if (ALL_LOGS.length > 500) ALL_LOGS = ALL_LOGS.slice(-500);
        if (LOG_LEVEL === 'all' || item.level === LOG_LEVEL) {
          const box = document.getElementById('consoleBody');
          const div = document.createElement('div');
          div.className = 'log-line';
          div.innerHTML = `<span class="log-ts">[${esc(item.time)}]</span><span class="log-${esc(item.level || 'info')}">${esc(item.text)}</span>`;
          box.appendChild(div);
          box.scrollTop = box.scrollHeight;
        }
      }
      if (item.kind === 'audit_updated' || item.kind === 'watchdog' || item.kind === 'jobs_updated') fetchState();
    } catch (_) {}
  };
})();
