'use strict';

const S = {
  me: null,
  perms: {},
  brand: null,
  view: 'home',
  tab: null,
  doors: [],
  blocked: '',
  installEvent: null,
};

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function count(n, one, many) {
  return `${n} ${t(n === 1 ? one : many)}`;
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------- api ----------

class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

async function api(method, url, body) {
  const opt = { method, credentials: 'same-origin', headers: { 'X-Requested-With': 'doorcontrol' } };
  if (body instanceof FormData) {
    opt.body = body;
  } else if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url, opt);
  } catch (e) {
    throw new ApiError(t('offline'), 0);
  }
  let data = {};
  try { data = await r.json(); } catch (e) { /* not json */ }
  if (r.status === 401 && url !== '/api/login') {
    S.me = null;
    render();
    throw new ApiError(t('session_expired'), 401);
  }
  if (!r.ok) throw new ApiError(data.error || `HTTP ${r.status}`, r.status);
  return data;
}

// ---------- small ui helpers ----------

let toastTimer;
function toast(msg, kind) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'show ' + (kind || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, kind === 'err' ? 4500 : 2500);
}

function buzz(ms) {
  try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* ignore */ }
}

function applyBrand(b) {
  if (!b) return;
  S.brand = b;
  setLang(b.language);
  document.documentElement.lang = b.language;
  document.documentElement.style.setProperty('--accent', b.accent);
  document.documentElement.style.setProperty('--accent-ink', readableOn(b.accent));
  $('meta[name="theme-color"]').setAttribute('content', b.accent);
  $('meta[name="apple-mobile-web-app-title"]').setAttribute('content', b.app_name);
  document.title = b.app_name;
  const v = b.icon_version;
  $('link[rel="apple-touch-icon"]').href = '/apple-touch-icon.png?v=' + v;
  $('link[rel="icon"]').href = '/favicon.png?v=' + v;
}

function readableOn(hex) {
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.62 ? '#111827' : '#ffffff';
}

function brandHead(size) {
  const b = S.brand || {};
  if (b.logo) return `<img class="logo ${size || ''}" src="${esc(b.logo)}" alt="${esc(b.app_name)}">`;
  return `<span class="logo-mark ${size || ''}">${icon('door')}</span>`;
}

function isIOS() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

// ---------- sheets (bottom panels for forms) ----------

function openSheet(title, bodyHtml, opts) {
  opts = opts || {};
  const root = $('#sheet-root');
  root.innerHTML = `
    <div class="sheet-backdrop"></div>
    <div class="sheet" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="sheet-grip"></div>
      <div class="sheet-head">
        <h2>${esc(title)}</h2>
        <button class="icon-btn" data-close aria-label="${t('close')}">${icon('close')}</button>
      </div>
      <div class="sheet-body">${bodyHtml}</div>
    </div>`;
  document.body.classList.add('sheet-open');
  requestAnimationFrame(() => root.classList.add('open'));
  const close = () => closeSheet();
  $('.sheet-backdrop', root).addEventListener('click', close);
  $('[data-close]', root).addEventListener('click', close);
  if (opts.wide) $('.sheet', root).classList.add('wide');
  return $('.sheet', root);
}

function closeSheet() {
  const root = $('#sheet-root');
  root.classList.remove('open');
  document.body.classList.remove('sheet-open');
  setTimeout(() => { if (!root.classList.contains('open')) root.innerHTML = ''; }, 250);
}

function confirmSheet(title, text, okLabel, danger) {
  return new Promise(resolve => {
    const sh = openSheet(title, `
      <p class="muted">${esc(text)}</p>
      <div class="row-btns">
        <button class="btn ghost" data-no>${t('cancel')}</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-yes>${esc(okLabel)}</button>
      </div>`);
    $('[data-no]', sh).onclick = () => { closeSheet(); resolve(false); };
    $('[data-yes]', sh).onclick = () => { closeSheet(); resolve(true); };
  });
}

function formData(form) {
  const out = {};
  $$('input, select, textarea', form).forEach(el => {
    if (!el.name) return;
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else out[el.name] = el.value;
  });
  return out;
}

async function busy(btn, fn) {
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add('loading');
  try { return await fn(); }
  finally { btn.disabled = false; btn.classList.remove('loading'); }
}

function field(label, inner, hint) {
  return `<label class="field"><span class="field-label">${label}</span>${inner}${hint ? `<span class="hint">${hint}</span>` : ''}</label>`;
}

function toggle(name, label, checked, hint) {
  return `<label class="toggle"><span><span class="toggle-label">${label}</span>${hint ? `<span class="hint">${hint}</span>` : ''}</span>
    <input type="checkbox" name="${name}" ${checked ? 'checked' : ''}><span class="switch"></span></label>`;
}

// ---------- boot ----------

async function boot() {
  try {
    applyBrand(await api('GET', '/api/branding'));
  } catch (e) { /* offline, keep defaults */ }
  try {
    const me = await api('GET', '/api/me');
    S.me = me.user;
    S.perms = me.perms;
    applyBrand(me.branding);
  } catch (e) {
    S.me = null;
  }
  const h = location.hash.replace('#', '');
  if (S.me && h.startsWith('settings')) {
    S.view = 'settings';
    S.tab = h.split('/')[1] || null;
  }
  render();
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
}

window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  S.installEvent = e;
  const b = $('#install-btn');
  if (b) b.hidden = false;
});

window.addEventListener('hashchange', () => {
  if (!S.me) return;
  const h = location.hash.replace('#', '');
  if (h.startsWith('settings')) {
    S.view = 'settings';
    S.tab = h.split('/')[1] || S.tab;
  } else {
    S.view = 'home';
  }
  render();
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && S.me && S.view === 'home') loadDoors();
});

function render() {
  closeSheet();
  if (!S.me && S.brand && S.brand.needs_setup) return renderSetup();
  if (!S.me) return renderLogin();
  if (S.view === 'settings') return renderSettings();
  return renderHome();
}

// ---------- login ----------

function renderLogin() {
  const b = S.brand || {};
  $('#app').innerHTML = `
    <main class="login">
      <div class="login-card">
        <div class="login-brand">
          ${brandHead('big')}
          <h1>${esc(b.app_name || 'Door Control')}</h1>
          ${b.subtitle ? `<p class="muted">${esc(b.subtitle)}</p>` : ''}
        </div>
        <form id="login-form" autocomplete="on">
          ${field(t('username'), '<input name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required>')}
          ${field(t('password'), '<input name="password" type="password" autocomplete="current-password" required>')}
          ${toggle('remember', t('stay_logged_in'), true)}
          <p class="form-error" hidden></p>
          <button class="btn primary block" type="submit">${t('log_in')}</button>
        </form>
      </div>
      <p class="foot muted">v${esc(b.version || '')}</p>
    </main>`;
  const form = $('#login-form');
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const err = $('.form-error', form);
    err.hidden = true;
    await busy($('button[type=submit]', form), async () => {
      try {
        await api('POST', '/api/login', formData(form));
        const me = await api('GET', '/api/me');
        S.me = me.user;
        S.perms = me.perms;
        applyBrand(me.branding);
        S.view = 'home';
        history.replaceState(null, '', '#');
        render();
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
        $('input[name=password]', form).select();
      }
    });
  });
}

// ---------- first run ----------

function renderSetup() {
  const b = S.brand || {};
  $('#app').innerHTML = `
    <main class="login">
      <div class="login-card">
        <div class="login-brand">
          ${brandHead('big')}
          <h1>${t('setup_title')}</h1>
          <p class="muted">${t('setup_help')}</p>
        </div>
        <form id="setup-form" autocomplete="off">
          ${field(t('language'), `<select name="language"><option value="en" ${b.language === 'en' ? 'selected' : ''}>English</option><option value="nl" ${b.language === 'nl' ? 'selected' : ''}>Nederlands</option></select>`)}
          ${field(t('app_name'), `<input name="app_name" value="${esc(b.app_name || '')}" maxlength="40" placeholder="Door Control">`, t('setup_name_hint'))}
          ${field(t('full_name'), '<input name="name" maxlength="80" autocomplete="name">')}
          ${field(t('username'), '<input name="username" value="admin" required autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false">')}
          ${field(t('password'), '<input name="password" type="password" minlength="6" required autocomplete="new-password">')}
          ${field(t('repeat_password'), '<input name="password2" type="password" minlength="6" required autocomplete="new-password">')}
          <p class="form-error" hidden></p>
          <button class="btn primary block" type="submit">${t('setup_go')}</button>
        </form>
      </div>
    </main>`;
  const form = $('#setup-form');
  form.language.onchange = () => {
    const keep = formData(form);
    S.brand.language = form.language.value;
    setLang(form.language.value);
    renderSetup();
    const f = $('#setup-form');
    ['app_name', 'name', 'username'].forEach(k => { f[k].value = keep[k]; });
  };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const err = $('.form-error', form);
    err.hidden = true;
    const data = formData(form);
    if (data.password !== data.password2) {
      err.textContent = t('passwords_differ');
      err.hidden = false;
      return;
    }
    await busy($('button[type=submit]', form), async () => {
      try {
        await api('POST', '/api/setup', data);
        const me = await api('GET', '/api/me');
        S.me = me.user;
        S.perms = me.perms;
        applyBrand(me.branding);
        S.view = 'settings';
        S.tab = 'look';
        history.replaceState(null, '', '#settings/look');
        render();
        toast(t('setup_done'), 'ok');
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
        if (ex.status === 403) setTimeout(() => location.reload(), 1500);
      }
    });
  });
}

// ---------- home ----------

function canManage() {
  const p = S.perms;
  return p.is_admin || p.manage_users || p.manage_doors || p.manage_settings || p.view_log;
}

function renderHome() {
  const b = S.brand;
  $('#app').innerHTML = `
    <header class="topbar">
      <div class="brand">${brandHead()}<div class="brand-text"><strong>${esc(b.app_name)}</strong>${b.subtitle ? `<span>${esc(b.subtitle)}</span>` : ''}</div></div>
      <div class="top-actions">
        ${canManage() ? `<a class="icon-btn" href="#settings" aria-label="${t('settings')}">${icon('settings')}</a>` : ''}
        <button class="icon-btn" id="account-btn" aria-label="${t('account')}">${icon('user')}</button>
      </div>
    </header>
    <main class="home">
      <div id="notice"></div>
      <div id="doors"><div class="skeleton-grid">${'<div class="door skeleton"></div>'.repeat(4)}</div></div>
      <div id="install-hint"></div>
    </main>`;
  $('#account-btn').onclick = accountSheet;
  loadDoors();
  installHint();
}

async function loadDoors() {
  try {
    const d = await api('GET', '/api/doors');
    S.doors = d.doors;
    S.blocked = d.blocked;
    drawDoors();
  } catch (e) {
    const box = $('#doors');
    if (box && e.status !== 401) box.innerHTML = `<div class="empty">${icon('wifi_off', 'big')}<p>${esc(e.message)}</p><button class="btn ghost" id="retry">${t('retry')}</button></div>`;
    const r = $('#retry');
    if (r) r.onclick = loadDoors;
  }
}

function drawDoors() {
  const box = $('#doors');
  if (!box) return;
  $('#notice').innerHTML = S.blocked
    ? `<div class="notice warn">${icon('clock')}<span>${t('blocked_now')}: ${esc(S.blocked)}</span></div>` : '';
  if (!S.doors.length) {
    box.innerHTML = `<div class="empty">${icon('door', 'big')}<p>${t('no_doors')}</p>
      ${S.perms.manage_doors ? `<a class="btn primary" href="#settings/doors">${t('add_first_door')}</a>` : ''}</div>`;
    return;
  }
  const groups = [];
  const idx = {};
  S.doors.forEach(d => {
    const key = d.section || '';
    if (!(key in idx)) { idx[key] = groups.length; groups.push({ name: key, doors: [] }); }
    groups[idx[key]].doors.push(d);
  });
  box.innerHTML = groups.map(g => `
    <section class="door-group">
      ${g.name ? `<h3 class="group-title">${esc(g.name)}</h3>` : ''}
      <div class="door-grid">
        ${g.doors.map(d => `
          <button class="door c-${esc(d.color || 'accent')}" data-id="${d.id}" ${S.blocked ? 'disabled' : ''}>
            <span class="door-icon">${icon(d.icon)}<svg class="ring" viewBox="0 0 64 64"><circle cx="32" cy="32" r="29"/></svg></span>
            <span class="door-name">${esc(d.name)}</span>
            <span class="door-state">${d.hold_to_open ? t('hold_to_open') : t('tap_to_open')}</span>
          </button>`).join('')}
      </div>
    </section>`).join('');
  $$('.door', box).forEach(el => bindDoor(el, S.doors.find(d => d.id === +el.dataset.id)));
}

function bindDoor(el, door) {
  const HOLD_MS = 900;
  let holdTimer = null;

  const fire = async () => {
    if (el.classList.contains('busy')) return;
    el.classList.remove('ok', 'err', 'holding');
    el.classList.add('busy');
    $('.door-state', el).textContent = t('opening');
    buzz(15);
    try {
      await api('POST', `/api/doors/${door.id}/open`);
      el.classList.remove('busy');
      el.classList.add('ok');
      $('.door-state', el).textContent = t('opened');
      buzz([30, 40, 30]);
      toast(`${door.name}: ${t('opened')}`, 'ok');
    } catch (e) {
      el.classList.remove('busy');
      el.classList.add('err');
      $('.door-state', el).textContent = t('failed');
      buzz([80, 60, 80]);
      toast(`${door.name}: ${e.message}`, 'err');
    }
    setTimeout(() => {
      el.classList.remove('ok', 'err');
      $('.door-state', el).textContent = door.hold_to_open ? t('hold_to_open') : t('tap_to_open');
    }, 3500);
  };

  if (!door.hold_to_open) {
    el.addEventListener('click', fire);
    return;
  }
  const start = e => {
    if (el.classList.contains('busy') || el.disabled) return;
    e.preventDefault();
    el.classList.add('holding');
    holdTimer = setTimeout(() => { holdTimer = null; el.classList.remove('holding'); fire(); }, HOLD_MS);
  };
  const stop = () => {
    if (holdTimer) {
      clearTimeout(holdTimer);
      holdTimer = null;
      el.classList.remove('holding');
      $('.door-state', el).textContent = t('keep_holding');
      setTimeout(() => { if (!el.classList.contains('busy') && !el.classList.contains('ok')) $('.door-state', el).textContent = t('hold_to_open'); }, 1500);
    }
  };
  el.addEventListener('pointerdown', start);
  el.addEventListener('pointerup', stop);
  el.addEventListener('pointerleave', stop);
  el.addEventListener('pointercancel', stop);
  el.addEventListener('contextmenu', e => e.preventDefault());
  el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); } });
}

function installHint() {
  const box = $('#install-hint');
  if (!box || isStandalone()) return;
  let dismissed = false;
  try { dismissed = localStorage.getItem('dc_hint_done') === '1'; } catch (e) { /* ignore */ }
  if (dismissed) return;
  const msg = isIOS() ? t('install_ios') : t('install_android');
  box.innerHTML = `<div class="install-card">${icon(isIOS() ? 'share' : 'download')}<p>${msg}</p>
    <div class="row-btns"><button class="btn ghost small" id="hint-close">${t('got_it')}</button>
    <button class="btn primary small" id="install-btn" ${S.installEvent ? '' : 'hidden'}>${t('install')}</button></div></div>`;
  $('#hint-close').onclick = () => {
    try { localStorage.setItem('dc_hint_done', '1'); } catch (e) { /* ignore */ }
    box.innerHTML = '';
  };
  $('#install-btn').onclick = async () => {
    if (!S.installEvent) return;
    S.installEvent.prompt();
    await S.installEvent.userChoice;
    S.installEvent = null;
    box.innerHTML = '';
  };
}

function accountSheet() {
  const u = S.me;
  const sh = openSheet(t('account'), `
    <div class="account-head">${icon('user', 'big')}<div><strong>${esc(u.name || u.username)}</strong><span class="muted">${esc(u.username)} · ${esc(u.role)}</span></div></div>
    <form id="pw-form" class="stack">
      <h3>${t('change_password')}</h3>
      ${field(t('current_password'), '<input type="password" name="current" autocomplete="current-password" required>')}
      ${field(t('new_password'), '<input type="password" name="new" autocomplete="new-password" minlength="6" required>')}
      <button class="btn primary block" type="submit">${t('save')}</button>
    </form>
    <button class="btn ghost block" id="logout">${icon('logout')} ${t('log_out')}</button>`);
  $('#pw-form', sh).onsubmit = async e => {
    e.preventDefault();
    await busy($('#pw-form button', sh), async () => {
      try {
        await api('POST', '/api/me/password', formData(e.target));
        closeSheet();
        toast(t('saved'), 'ok');
      } catch (ex) { toast(ex.message, 'err'); }
    });
  };
  $('#logout', sh).onclick = async () => {
    try { await api('POST', '/api/logout'); } catch (e) { /* ignore */ }
    S.me = null;
    history.replaceState(null, '', '#');
    render();
  };
}

// ---------- settings ----------

function settingsTabs() {
  const p = S.perms;
  const tabs = [];
  if (p.manage_doors) tabs.push(['doors', 'door', t('tab_doors')], ['intercoms', 'chip', t('tab_intercoms')]);
  if (p.manage_users) tabs.push(['users', 'users', t('tab_users')]);
  if (p.is_admin) tabs.push(['ranks', 'shield', t('tab_ranks')]);
  if (p.manage_settings) tabs.push(['look', 'palette', t('tab_look')], ['https', 'lock', 'HTTPS']);
  if (p.view_log) tabs.push(['log', 'list', t('tab_log')]);
  return tabs;
}

function renderSettings() {
  const tabs = settingsTabs();
  if (!tabs.length) { S.view = 'home'; return renderHome(); }
  if (!tabs.find(x => x[0] === S.tab)) S.tab = tabs[0][0];
  $('#app').innerHTML = `
    <header class="topbar">
      <div class="brand"><a class="icon-btn" href="#" aria-label="${t('back')}">${icon('back')}</a><div class="brand-text"><strong>${t('settings')}</strong><span>${esc(S.brand.app_name)}</span></div></div>
    </header>
    <nav class="tabs" role="tablist">
      ${tabs.map(x => `<a role="tab" href="#settings/${x[0]}" class="tab ${x[0] === S.tab ? 'on' : ''}">${icon(x[1])}<span>${x[2]}</span></a>`).join('')}
    </nav>
    <main class="settings" id="pane"><div class="loading-pane"><span class="spinner"></span></div></main>`;
  const on = $('.tab.on');
  if (on) on.scrollIntoView({ inline: 'center', block: 'nearest' });
  ({ doors: paneDoors, intercoms: paneIntercoms, users: paneUsers, ranks: paneRanks, look: paneLook, https: paneHttps, log: paneLog })[S.tab]()
    .catch(e => { if (e.status !== 401) $('#pane').innerHTML = `<div class="empty"><p>${esc(e.message)}</p></div>`; });
}

function paneHead(title, sub, addLabel) {
  return `<div class="pane-head"><div><h2>${title}</h2>${sub ? `<p class="muted">${sub}</p>` : ''}</div>
    ${addLabel ? `<button class="btn primary" id="add">${icon('plus')}<span>${addLabel}</span></button>` : ''}</div>`;
}

// ----- doors -----

async function paneDoors() {
  const [bd, dd] = await Promise.all([api('GET', '/api/admin/buttons'), api('GET', '/api/admin/devices')]);
  const devs = dd.devices;
  const devName = id => (devs.find(d => d.id === id) || {}).name || '?';
  const pane = $('#pane');
  pane.innerHTML = paneHead(t('tab_doors'), t('doors_help'), t('add_door')) + (bd.buttons.length ? `
    <ul class="list">
      ${bd.buttons.map((b, i) => `
        <li class="item ${b.enabled ? '' : 'off'}" data-id="${b.id}">
          <span class="item-icon c-${esc(b.color || 'accent')}">${icon(b.icon)}</span>
          <button class="item-main" data-edit>
            <strong>${esc(b.name)}</strong>
            <span class="muted">${b.section ? esc(b.section) + ' · ' : ''}${b.steps.length ? b.steps.map(s => esc(devName(s.device_id))).join(', ') : `<em class="warn-text">${t('no_action')}</em>`}${b.enabled ? '' : ' · ' + t('hidden')}</span>
          </button>
          <span class="order">
            <button class="icon-btn small" data-up ${i === 0 ? 'disabled' : ''} aria-label="up">${icon('up')}</button>
            <button class="icon-btn small" data-down ${i === bd.buttons.length - 1 ? 'disabled' : ''} aria-label="down">${icon('down')}</button>
          </span>
        </li>`).join('')}
    </ul>` : `<div class="empty">${icon('door', 'big')}<p>${devs.length ? t('no_doors_yet') : t('add_intercom_first')}</p></div>`);

  const ids = bd.buttons.map(b => b.id);
  const move = async (id, dir) => {
    const i = ids.indexOf(id);
    const j = i + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    await api('POST', '/api/admin/buttons/order', { ids });
    paneDoors();
  };
  $$('.item', pane).forEach(li => {
    const b = bd.buttons.find(x => x.id === +li.dataset.id);
    $('[data-edit]', li).onclick = () => doorSheet(b, bd, devs);
    $('[data-up]', li).onclick = () => move(b.id, -1);
    $('[data-down]', li).onclick = () => move(b.id, 1);
  });
  $('#add').onclick = () => {
    if (!devs.length) { toast(t('add_intercom_first'), 'err'); location.hash = '#settings/intercoms'; return; }
    doorSheet(null, bd, devs);
  };
}

function stepRow(s, devs, actions) {
  s = s || { device_id: devs[0] && devs[0].id, kind: 'switch', value: '1', action: 'trigger' };
  return `<div class="step">
    <div class="step-grid">
      ${field(t('intercom'), `<select data-k="device_id">${devs.map(d => `<option value="${d.id}" ${d.id === s.device_id ? 'selected' : ''}>${esc(d.name)} (${esc(d.host)})</option>`).join('')}</select>`)}
      ${field(t('how'), `<select data-k="kind">
        <option value="switch" ${s.kind === 'switch' ? 'selected' : ''}>${t('kind_switch')}</option>
        <option value="trigger" ${s.kind === 'trigger' ? 'selected' : ''}>${t('kind_trigger')}</option>
        <option value="path" ${s.kind === 'path' ? 'selected' : ''}>${t('kind_path')}</option></select>`)}
      <label class="field" data-show="switch"><span class="field-label">${t('switch_no')}</span>
        <select data-k="value_switch">${[1, 2, 3, 4].map(n => `<option ${String(n) === String(s.value) ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      <label class="field" data-show="switch"><span class="field-label">${t('switch_action')}</span>
        <select data-k="action">${actions.map(a => `<option value="${a}" ${a === s.action ? 'selected' : ''}>${t('act_' + a)}</option>`).join('')}</select></label>
      <label class="field wide" data-show="trigger"><span class="field-label">${t('trigger_name')}</span>
        <input data-k="value_trigger" value="${s.kind === 'trigger' ? esc(s.value) : ''}" placeholder="open" autocapitalize="none"><span class="hint">${t('trigger_hint')}</span></label>
      <label class="field wide" data-show="path"><span class="field-label">${t('path')}</span>
        <input data-k="value_path" value="${s.kind === 'path' ? esc(s.value) : ''}" placeholder="/api/io/ctrl?port=relay1&action=on" autocapitalize="none"></label>
    </div>
    <button type="button" class="icon-btn small step-del" aria-label="${t('delete')}">${icon('trash')}</button>
  </div>`;
}

function bindStep(el) {
  const kind = $('[data-k=kind]', el);
  const sync = () => $$('[data-show]', el).forEach(f => { f.hidden = f.dataset.show !== kind.value; });
  kind.onchange = sync;
  sync();
  $('.step-del', el).onclick = () => el.remove();
}

function readSteps(root) {
  return $$('.step', root).map(el => {
    const kind = $('[data-k=kind]', el).value;
    return {
      device_id: +$('[data-k=device_id]', el).value,
      kind,
      value: $(`[data-k=value_${kind}]`, el).value,
      action: $('[data-k=action]', el).value,
    };
  });
}

function doorSheet(b, bd, devs) {
  const isNew = !b;
  b = b || { name: '', section: '', icon: 'door', color: '', hold_to_open: 0, enabled: 1, steps: [null] };
  const sections = [...new Set(bd.buttons.map(x => x.section).filter(Boolean))];
  const sh = openSheet(isNew ? t('add_door') : b.name, `
    <form class="stack" id="door-form">
      ${field(t('name'), `<input name="name" value="${esc(b.name)}" maxlength="40" required placeholder="${t('door_name_ph')}">`)}
      ${field(t('section'), `<input name="section" value="${esc(b.section)}" maxlength="40" list="sections" placeholder="${t('section_ph')}"><datalist id="sections">${sections.map(s => `<option value="${esc(s)}">`).join('')}</datalist>`, t('section_hint'))}
      <div class="field"><span class="field-label">${t('icon')}</span>
        <div class="icon-pick">${bd.icons.map(i => `<label><input type="radio" name="icon" value="${i}" ${i === b.icon ? 'checked' : ''}><span>${icon(i)}</span></label>`).join('')}</div></div>
      <div class="field"><span class="field-label">${t('color')}</span>
        <div class="color-pick">${bd.colors.map(c => `<label><input type="radio" name="color" value="${c}" ${c === b.color ? 'checked' : ''}><span class="c-${c || 'accent'}" title="${c || t('app_color')}"></span></label>`).join('')}</div></div>
      ${toggle('hold_to_open', t('hold_to_open'), b.hold_to_open, t('hold_hint'))}
      ${toggle('enabled', t('show_on_home'), b.enabled)}
      <div class="field"><span class="field-label">${t('what_happens')}</span>
        <span class="hint">${t('steps_hint')}</span>
        <div id="steps">${b.steps.map(s => stepRow(s, devs, bd.actions)).join('')}</div>
        <button type="button" class="btn ghost small" id="add-step">${icon('plus')} ${t('add_action')}</button>
      </div>
      <div class="row-btns">
        ${isNew ? '' : `<button type="button" class="btn ghost danger-text" id="del">${icon('trash')}</button><button type="button" class="btn ghost" id="test">${t('test_now')}</button>`}
        <button class="btn primary grow" type="submit">${t('save')}</button>
      </div>
    </form>`);
  $$('.step', sh).forEach(bindStep);
  $('#add-step', sh).onclick = () => {
    $('#steps', sh).insertAdjacentHTML('beforeend', stepRow(null, devs, bd.actions));
    bindStep($('#steps', sh).lastElementChild);
  };
  const form = $('#door-form', sh);
  form.onsubmit = async e => {
    e.preventDefault();
    const data = {
      name: form.name.value,
      section: form.section.value,
      icon: (form.querySelector('[name=icon]:checked') || {}).value || 'door',
      color: (form.querySelector('[name=color]:checked') || {}).value || '',
      hold_to_open: form.hold_to_open.checked,
      enabled: form.enabled.checked,
      steps: readSteps(sh),
    };
    if (!data.steps.length) { toast(t('need_action'), 'err'); return; }
    await busy($('button[type=submit]', form), async () => {
      try {
        await api(isNew ? 'POST' : 'PUT', isNew ? '/api/admin/buttons' : `/api/admin/buttons/${b.id}`, data);
        closeSheet();
        toast(t('saved'), 'ok');
        paneDoors();
      } catch (ex) { toast(ex.message, 'err'); }
    });
  };
  if (!isNew) {
    $('#test', sh).onclick = e => busy(e.currentTarget, async () => {
      try {
        await api('POST', `/api/admin/buttons/${b.id}/test`);
        toast(`${b.name}: ${t('opened')}`, 'ok');
      } catch (ex) { toast(ex.message, 'err'); }
    });
    $('#del', sh).onclick = async () => {
      if (!await confirmSheet(t('delete_door'), b.name, t('delete'), true)) return;
      try {
        await api('DELETE', `/api/admin/buttons/${b.id}`);
        toast(t('deleted'), 'ok');
        paneDoors();
      } catch (ex) { toast(ex.message, 'err'); }
    };
  }
}

// ----- intercoms -----

async function paneIntercoms() {
  const dd = await api('GET', '/api/admin/devices');
  const pane = $('#pane');
  pane.innerHTML = paneHead(t('tab_intercoms'), t('intercoms_help'), t('add_intercom')) + (dd.devices.length ? `
    <ul class="list">
      ${dd.devices.map(d => `
        <li class="item" data-id="${d.id}">
          <span class="item-icon">${icon('chip')}</span>
          <button class="item-main" data-edit>
            <strong>${esc(d.name)}</strong>
            <span class="muted mono">${esc(d.scheme)}://${esc(d.host)}${d.username ? ' · ' + esc(d.username) : ''}</span>
            <span class="muted small" data-status>${count(d.buttons, 'door_lc', 'doors_lc')}</span>
          </button>
          <button class="btn ghost small" data-test>${t('test')}</button>
        </li>`).join('')}
    </ul>` : `<div class="empty">${icon('chip', 'big')}<p>${t('no_intercoms')}</p></div>`);
  $$('.item', pane).forEach(li => {
    const d = dd.devices.find(x => x.id === +li.dataset.id);
    $('[data-edit]', li).onclick = () => intercomSheet(d);
    $('[data-test]', li).onclick = e => testIntercom(d, e.currentTarget, $('[data-status]', li));
  });
  $('#add').onclick = () => intercomSheet(null);
}

async function testIntercom(d, btn, out) {
  await busy(btn, async () => {
    try {
      const r = await api('POST', `/api/admin/devices/${d.id}/test`);
      if (!r.ok) {
        out.innerHTML = `<span class="bad">● ${esc(r.error)}</span>`;
      } else if (r.reachable_only) {
        out.innerHTML = `<span class="meh">● ${t('reachable_only')} (${esc(r.error)})</span>`;
      } else {
        const i = r.info;
        const sw = i.switches.filter(s => s.enabled).map(s => s.switch).join(', ');
        out.innerHTML = `<span class="good">● ${esc(i.model || '2N')}${i.firmware ? ' · fw ' + esc(i.firmware) : ''}${sw ? ' · ' + t('switches') + ' ' + esc(sw) : ''}</span>`;
      }
    } catch (ex) { out.innerHTML = `<span class="bad">● ${esc(ex.message)}</span>`; }
  });
}

function intercomSheet(d) {
  const isNew = !d;
  d = d || { name: '', host: '', scheme: 'https', username: '', has_password: false, verify_tls: 0 };
  const sh = openSheet(isNew ? t('add_intercom') : d.name, `
    <form class="stack" id="dev-form">
      ${field(t('name'), `<input name="name" value="${esc(d.name)}" maxlength="60" required placeholder="${t('intercom_name_ph')}">`)}
      <div class="two">
        ${field(t('connection'), `<select name="scheme"><option value="http" ${d.scheme === 'http' ? 'selected' : ''}>http</option><option value="https" ${d.scheme === 'https' ? 'selected' : ''}>https</option></select>`)}
        ${field(t('ip_address'), `<input name="host" value="${esc(d.host)}" required inputmode="url" autocapitalize="none" placeholder="192.168.1.50">`)}
      </div>
      <p class="hint">${t('api_user_hint')}</p>
      <div class="two">
        ${field(t('api_user'), `<input name="username" value="${esc(d.username)}" autocapitalize="none" autocomplete="off">`)}
        ${field(t('api_password'), `<input name="password" type="password" autocomplete="new-password" placeholder="${d.has_password ? '••••••' : ''}">`, d.has_password ? t('keep_password') : '')}
      </div>
      ${toggle('verify_tls', t('check_cert'), d.verify_tls, t('check_cert_hint'))}
      <div class="test-out muted small" id="test-out"></div>
      <div class="row-btns">
        ${isNew ? '' : `<button type="button" class="btn ghost danger-text" id="del">${icon('trash')}</button><button type="button" class="btn ghost" id="test">${t('test')}</button>`}
        <button class="btn primary grow" type="submit">${t('save')}</button>
      </div>
    </form>`);
  const form = $('#dev-form', sh);
  form.onsubmit = async e => {
    e.preventDefault();
    await busy($('button[type=submit]', form), async () => {
      try {
        await api(isNew ? 'POST' : 'PUT', isNew ? '/api/admin/devices' : `/api/admin/devices/${d.id}`, formData(form));
        closeSheet();
        toast(t('saved'), 'ok');
        paneIntercoms();
      } catch (ex) { toast(ex.message, 'err'); }
    });
  };
  if (!isNew) {
    $('#test', sh).onclick = e => testIntercom(d, e.currentTarget, $('#test-out', sh));
    $('#del', sh).onclick = async () => {
      const msg = d.buttons ? t('intercom_in_use').replace('{n}', d.buttons) : d.name;
      if (!await confirmSheet(t('delete_intercom'), msg, t('delete'), true)) return;
      try {
        await api('DELETE', `/api/admin/devices/${d.id}`);
        toast(t('deleted'), 'ok');
        paneIntercoms();
      } catch (ex) { toast(ex.message, 'err'); }
    };
  }
}

// ----- users -----

async function paneUsers() {
  const [ud, rd] = await Promise.all([api('GET', '/api/admin/users'), api('GET', '/api/admin/roles')]);
  const pane = $('#pane');
  pane.innerHTML = paneHead(t('tab_users'), t('users_help'), t('add_user')) + `
    <input class="search" type="search" placeholder="${t('search')}" id="user-q">
    <ul class="list" id="user-list">
      ${ud.users.map(u => `
        <li class="item ${u.active ? '' : 'off'}" data-id="${u.id}" data-q="${esc((u.username + ' ' + u.name + ' ' + u.role_name).toLowerCase())}">
          <span class="avatar">${esc((u.name || u.username).slice(0, 1).toUpperCase())}</span>
          <button class="item-main" data-edit ${u.editable ? '' : 'disabled'}>
            <strong>${esc(u.name || u.username)}${u.id === S.me.id ? ` <span class="muted">(${t('you')})</span>` : ''}</strong>
            <span class="muted">${esc(u.username)}${u.active ? '' : ' · ' + t('disabled')}${u.last_login ? ' · ' + t('last_login') + ' ' + esc(u.last_login.slice(0, 10)) : ''}</span>
          </button>
          <span class="badge">${esc(u.role_name)}</span>
        </li>`).join('')}
    </ul>`;
  $('#user-q').oninput = e => {
    const q = e.target.value.toLowerCase();
    $$('#user-list .item').forEach(li => { li.hidden = q && !li.dataset.q.includes(q); });
  };
  $$('.item', pane).forEach(li => {
    const u = ud.users.find(x => x.id === +li.dataset.id);
    $('[data-edit]', li).onclick = () => userSheet(u, rd.roles);
  });
  $('#add').onclick = () => userSheet(null, rd.roles);
}

function userSheet(u, roles) {
  const isNew = !u;
  const self = u && u.id === S.me.id;
  const usable = roles.filter(r => S.perms.is_admin || r.rank > S.me.rank);
  u = u || { username: '', name: '', role_id: (usable.find(r => !r.is_admin) || usable[0] || {}).id, active: 1 };
  const sh = openSheet(isNew ? t('add_user') : (u.name || u.username), `
    <form class="stack" id="user-form">
      ${field(t('full_name'), `<input name="name" value="${esc(u.name)}" maxlength="80" placeholder="Jan Peeters">`)}
      ${field(t('username'), `<input name="username" value="${esc(u.username)}" maxlength="64" required autocapitalize="none" autocorrect="off" spellcheck="false">`)}
      ${self ? `<input type="hidden" name="role_id" value="${u.role_id}">` : field(t('rank'), `<select name="role_id">${usable.map(r => `<option value="${r.id}" ${r.id === u.role_id ? 'selected' : ''}>${esc(r.name)}</option>`).join('')}</select>`, t('rank_hint'))}
      ${field(isNew ? t('password') : t('new_password'), `<div class="pw-row"><input name="password" type="text" autocomplete="off" autocapitalize="none" ${isNew ? 'required minlength="6"' : ''} placeholder="${isNew ? '' : t('leave_empty')}"><button type="button" class="btn ghost small" id="gen">${t('generate')}</button></div>`)}
      ${self ? '' : toggle('active', t('can_log_in'), u.active)}
      <div class="row-btns">
        ${isNew || self ? '' : `<button type="button" class="btn ghost danger-text" id="del">${icon('trash')}</button>`}
        <button class="btn primary grow" type="submit">${t('save')}</button>
      </div>
    </form>`);
  const form = $('#user-form', sh);
  $('#gen', sh).onclick = () => {
    const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    const arr = new Uint32Array(10);
    crypto.getRandomValues(arr);
    form.password.value = Array.from(arr, n => chars[n % chars.length]).join('');
  };
  form.onsubmit = async e => {
    e.preventDefault();
    const data = formData(form);
    if (self) data.active = true;
    await busy($('button[type=submit]', form), async () => {
      try {
        await api(isNew ? 'POST' : 'PUT', isNew ? '/api/admin/users' : `/api/admin/users/${u.id}`, data);
        closeSheet();
        toast(t('saved'), 'ok');
        paneUsers();
      } catch (ex) { toast(ex.message, 'err'); }
    });
  };
  const del = $('#del', sh);
  if (del) {
    del.onclick = async () => {
      if (!await confirmSheet(t('delete_user'), u.username, t('delete'), true)) return;
      try {
        await api('DELETE', `/api/admin/users/${u.id}`);
        toast(t('deleted'), 'ok');
        paneUsers();
      } catch (ex) { toast(ex.message, 'err'); }
    };
  }
}

// ----- ranks -----

const DAYS = ['1', '2', '3', '4', '5', '6', '7'];

async function paneRanks() {
  const [rd, bd] = await Promise.all([api('GET', '/api/admin/roles'), api('GET', '/api/admin/buttons')]);
  const pane = $('#pane');
  const summary = r => {
    if (r.is_admin) return t('everything');
    const bits = [];
    if (r.manage_users) bits.push(t('p_users'));
    if (r.manage_doors) bits.push(t('p_doors'));
    if (r.manage_settings) bits.push(t('p_settings'));
    if (r.view_log) bits.push(t('p_log'));
    bits.push(r.all_doors ? t('all_doors') : count(r.buttons.length, 'door_lc', 'doors_lc'));
    if (r.days !== '1234567') bits.push(r.days.split('').map(d => t('day' + d)).join(' '));
    if (r.time_from) bits.push(`${r.time_from}-${r.time_to}`);
    return bits.join(' · ');
  };
  pane.innerHTML = paneHead(t('tab_ranks'), t('ranks_help'), t('add_rank')) + `
    <ul class="list">
      ${rd.roles.map(r => `
        <li class="item" data-id="${r.id}">
          <span class="rank-no">${r.rank}</span>
          <button class="item-main" data-edit>
            <strong>${esc(r.name)}</strong>
            <span class="muted">${esc(summary(r))}</span>
          </button>
          <span class="badge">${count(r.users, 'user_lc', 'users_lc')}</span>
        </li>`).join('')}
    </ul>`;
  $$('.item', pane).forEach(li => {
    const r = rd.roles.find(x => x.id === +li.dataset.id);
    $('[data-edit]', li).onclick = () => rankSheet(r, bd.buttons);
  });
  $('#add').onclick = () => rankSheet(null, bd.buttons);
}

function rankSheet(r, buttons) {
  const isNew = !r;
  r = r || { name: '', rank: 10, is_admin: 0, manage_users: 0, manage_doors: 0, manage_settings: 0, view_log: 0, all_doors: 1, days: '1234567', time_from: '', time_to: '', buttons: [] };
  const locked = !!r.is_admin;
  const sh = openSheet(isNew ? t('add_rank') : r.name, `
    <form class="stack" id="rank-form">
      <div class="two">
        ${field(t('name'), `<input name="name" value="${esc(r.name)}" maxlength="40" required placeholder="${t('rank_name_ph')}">`)}
        ${field(t('rank_no'), `<input name="rank" type="number" min="${locked ? 1 : 2}" max="99" value="${r.rank}" ${locked ? 'readonly' : ''}>`, t('rank_no_hint'))}
      </div>
      ${locked ? `<p class="notice">${icon('shield')}<span>${t('admin_locked')}</span></p>` : `
      <h3>${t('may_do')}</h3>
      ${toggle('manage_users', t('p_users'), r.manage_users, t('p_users_hint'))}
      ${toggle('manage_doors', t('p_doors'), r.manage_doors, t('p_doors_hint'))}
      ${toggle('manage_settings', t('p_settings'), r.manage_settings, t('p_settings_hint'))}
      ${toggle('view_log', t('p_log'), r.view_log)}
      <h3>${t('which_doors')}</h3>
      ${toggle('all_doors', t('all_doors'), r.all_doors, t('all_doors_hint'))}
      <div class="checks" id="door-checks">${buttons.length ? buttons.map(b => `<label class="check"><input type="checkbox" value="${b.id}" ${r.buttons.includes(b.id) ? 'checked' : ''}><span>${esc(b.name)}${b.section ? ` <em class="muted">${esc(b.section)}</em>` : ''}</span></label>`).join('') : `<p class="muted">${t('no_doors_yet')}</p>`}</div>
      <h3>${t('when')}</h3>
      <div class="days">${DAYS.map(d => `<label><input type="checkbox" value="${d}" ${r.days.includes(d) ? 'checked' : ''}><span>${t('day' + d)}</span></label>`).join('')}</div>
      <div class="two">
        ${field(t('from'), `<input name="time_from" type="time" value="${esc(r.time_from)}">`)}
        ${field(t('until'), `<input name="time_to" type="time" value="${esc(r.time_to)}">`)}
      </div>
      <p class="hint">${t('time_hint')}</p>`}
      <div class="row-btns">
        ${isNew || locked ? '' : `<button type="button" class="btn ghost danger-text" id="del">${icon('trash')}</button>`}
        <button class="btn primary grow" type="submit">${t('save')}</button>
      </div>
    </form>`);
  const form = $('#rank-form', sh);
  const checks = $('#door-checks', sh);
  if (checks) {
    const sync = () => { checks.hidden = form.all_doors.checked; };
    form.all_doors.onchange = sync;
    sync();
  }
  form.onsubmit = async e => {
    e.preventDefault();
    const data = formData(form);
    if (!locked) {
      data.buttons = $$('#door-checks input:checked', sh).map(i => +i.value);
      data.days = $$('.days input:checked', sh).map(i => i.value).join('');
      if (!data.days) { toast(t('pick_a_day'), 'err'); return; }
    } else {
      data.days = r.days;
    }
    await busy($('button[type=submit]', form), async () => {
      try {
        await api(isNew ? 'POST' : 'PUT', isNew ? '/api/admin/roles' : `/api/admin/roles/${r.id}`, data);
        closeSheet();
        toast(t('saved'), 'ok');
        paneRanks();
      } catch (ex) { toast(ex.message, 'err'); }
    });
  };
  const del = $('#del', sh);
  if (del) {
    del.onclick = async () => {
      if (!await confirmSheet(t('delete_rank'), r.name, t('delete'), true)) return;
      try {
        await api('DELETE', `/api/admin/roles/${r.id}`);
        toast(t('deleted'), 'ok');
        paneRanks();
      } catch (ex) { toast(ex.message, 'err'); }
    };
  }
}

// ----- look (branding + general) -----

const PRESETS = ['#2563eb', '#0f766e', '#16a34a', '#ea580c', '#dc2626', '#7c3aed', '#db2777', '#111827'];

async function paneLook() {
  const sd = await api('GET', '/api/admin/settings');
  const s = sd.settings;
  const pane = $('#pane');
  pane.innerHTML = paneHead(t('tab_look'), t('look_help')) + `
    <div class="cards">
      <section class="card">
        <h3>${t('logo')}</h3>
        <div class="logo-box">
          <div class="logo-preview">${S.brand.logo ? `<img src="${esc(S.brand.logo)}" alt="">` : `<span class="muted">${t('no_logo')}</span>`}</div>
          <div class="stack">
            <label class="btn primary file-btn">${icon('plus')} ${t('upload_logo')}<input type="file" id="logo-file" accept="image/png,image/jpeg,image/webp,image/gif"></label>
            ${sd.has_logo ? `<button class="btn ghost" id="logo-del">${t('remove_logo')}</button>` : ''}
            <span class="hint">${t('logo_hint')}</span>
          </div>
        </div>
        <div class="icon-preview"><img src="/icon-192.png?v=${esc(S.brand.icon_version)}" alt=""><span class="hint">${t('icon_preview')}</span></div>
      </section>
      <form class="card stack" id="look-form">
        <h3>${t('general')}</h3>
        ${field(t('app_name'), `<input name="app_name" value="${esc(s.app_name)}" maxlength="40" required>`, t('app_name_hint'))}
        ${field(t('subtitle'), `<input name="subtitle" value="${esc(s.subtitle)}" maxlength="80" placeholder="${t('subtitle_ph')}">`)}
        <div class="field"><span class="field-label">${t('app_color')}</span>
          <div class="color-row">
            ${PRESETS.map(c => `<button type="button" class="swatch" data-c="${c}" style="background:${c}" aria-label="${c}"></button>`).join('')}
            <input type="color" name="accent" value="${esc(s.accent)}">
          </div></div>
        <div class="two">
          ${field(t('language'), `<select name="language"><option value="en" ${s.language === 'en' ? 'selected' : ''}>English</option><option value="nl" ${s.language === 'nl' ? 'selected' : ''}>Nederlands</option></select>`)}
          ${field(t('timezone'), `<select name="timezone">${sd.timezones.map(z => `<option ${z === s.timezone ? 'selected' : ''}>${esc(z)}</option>`).join('')}</select>`)}
        </div>
        <div class="two">
          ${field(t('session_days'), `<input name="session_days" type="number" min="1" max="3650" value="${esc(s.session_days)}">`, t('session_days_hint'))}
          ${field(t('log_days'), `<input name="log_days" type="number" min="1" max="3650" value="${esc(s.log_days)}">`)}
        </div>
        <button class="btn primary block" type="submit">${t('save')}</button>
      </form>
      ${S.perms.is_admin ? `<section class="card" id="update-card"><h3>${t('updates')}</h3><p class="muted">...</p></section>` : ''}
      <section class="card">
        <h3>${t('backup')}</h3>
        <p class="muted">${t('backup_help')}</p>
        <a class="btn ghost" href="/api/admin/backup" download>${icon('download')} ${t('download_backup')}</a>
      </section>
    </div>`;
  if (S.perms.is_admin) updateCard();
  const form = $('#look-form');
  $$('.swatch', pane).forEach(b => { b.onclick = () => { form.accent.value = b.dataset.c; }; });
  form.onsubmit = async e => {
    e.preventDefault();
    await busy($('button[type=submit]', form), async () => {
      try {
        const r = await api('PUT', '/api/admin/settings', formData(form));
        applyBrand(r.branding);
        toast(t('saved'), 'ok');
        renderSettings();
      } catch (ex) { toast(ex.message, 'err'); }
    });
  };
  $('#logo-file').onchange = async e => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('logo', f);
    try {
      const r = await api('POST', '/api/admin/logo', fd);
      applyBrand(r.branding);
      toast(t('saved'), 'ok');
      renderSettings();
    } catch (ex) { toast(ex.message, 'err'); }
  };
  const del = $('#logo-del');
  if (del) {
    del.onclick = async () => {
      try {
        const r = await api('DELETE', '/api/admin/logo');
        applyBrand(r.branding);
        renderSettings();
      } catch (ex) { toast(ex.message, 'err'); }
    };
  }
}

// ----- updates -----

function updateResult(r) {
  return ({ 'up to date': t('upd_up_to_date'), updated: t('upd_updated'), updating: t('upd_updating'),
    'could not reach GitHub': t('upd_no_github'), 'update failed, previous version kept': t('upd_failed') })[r] || r || '';
}

async function updateCard() {
  const box = $('#update-card');
  if (!box) return;
  let u;
  try { u = await api('GET', '/api/admin/update'); } catch (e) { box.hidden = true; return; }
  const last = u.last || {};
  const when = last.checked ? new Date(last.checked).toLocaleString() : '';
  box.innerHTML = `
    <h3>${t('updates')}</h3>
    <dl class="facts">
      <dt>${t('upd_version')}</dt><dd>${esc(u.version)}${u.commit ? ` <span class="muted mono">${esc(u.commit)}</span>` : ''}</dd>
      ${when ? `<dt>${t('upd_checked')}</dt><dd>${esc(when)}<br><span class="${last.result && last.result.includes('failed') ? 'bad' : 'muted'}">${esc(updateResult(last.result))}</span></dd>` : ''}
    </dl>
    ${u.managed ? `
      <form class="stack" id="upd-form">${toggle('auto', t('upd_auto'), u.auto, t('upd_auto_hint'))}</form>
      <button class="btn ghost" id="upd-now" ${u.requested ? 'disabled' : ''}>${u.requested ? t('upd_busy') : t('upd_now')}</button>`
      : `<p class="muted small">${t('upd_manual')}</p>`}`;
  const f = $('#upd-form');
  if (f) {
    f.auto.onchange = async () => {
      try { await api('PUT', '/api/admin/update', { auto: f.auto.checked }); toast(t('saved'), 'ok'); }
      catch (ex) { toast(ex.message, 'err'); }
    };
  }
  const b = $('#upd-now');
  if (b) {
    b.onclick = async () => {
      try {
        await api('POST', '/api/admin/update');
        b.disabled = true;
        b.textContent = t('upd_busy');
        waitForUpdate(u.commit);
      } catch (ex) { toast(ex.message, 'err'); }
    };
  }
}

function waitForUpdate(oldCommit) {
  // the server restarts when there is something new; reload once it is back
  let tries = 0;
  const tick = async () => {
    tries++;
    try {
      const u = await api('GET', '/api/admin/update');
      if (!u.requested && u.last && u.last.result !== 'updating') {
        if (u.commit !== oldCommit) { toast(t('upd_updated'), 'ok'); setTimeout(() => location.reload(), 800); return; }
        toast(updateResult(u.last.result), u.last.result.includes('failed') ? 'err' : 'ok');
        updateCard();
        return;
      }
    } catch (e) { /* restarting */ }
    if (tries < 90) setTimeout(tick, 2000);
  };
  setTimeout(tick, 3000);
}

// ----- https -----

async function paneHttps() {
  const h = await api('GET', '/api/admin/https');
  const c = h.cert;
  const pane = $('#pane');
  let status;
  if (!c) status = `<div class="notice">${icon('alert')}<span>${t('https_none')}</span></div>`;
  else if (!h.running) status = `<div class="notice bad">${icon('alert')}<span>${t('https_not_running')}${h.error ? ': ' + esc(h.error) : ''}</span></div>`;
  else status = `<div class="notice good">${icon('check')}<span>${t('https_running')} <a href="${esc(h.url)}">${esc(h.url || ('port ' + h.port))}</a></span></div>`;
  const warn = [];
  if (c) {
    if (c.self_signed) warn.push(t('cert_self_signed'));
    if (!c.chain_ok) warn.push(t('cert_no_chain'));
    if (c.domain_ok === false) warn.push(t('cert_wrong_domain'));
    if (c.days_left < 30) warn.push(t('cert_expires_soon').replace('{n}', c.days_left));
  }
  pane.innerHTML = paneHead('HTTPS', t('https_help')) + `
    <div class="cards">
      ${status}
      ${c ? `<section class="card">
        <h3>${t('cert_current')}</h3>
        <dl class="facts">
          <dt>${t('cert_for')}</dt><dd>${c.names.map(esc).join(', ')}</dd>
          <dt>${t('cert_by')}</dt><dd>${esc(c.issuer)}</dd>
          <dt>${t('cert_until')}</dt><dd>${esc(c.not_after)} <span class="muted">(${c.days_left} ${t('days')})</span></dd>
        </dl>
        ${warn.map(w => `<p class="warn-text small">${icon('alert')} ${esc(w)}</p>`).join('')}
        <button class="btn ghost danger-text" id="cert-del">${icon('trash')} ${t('cert_remove')}</button>
      </section>` : ''}
      <form class="card stack" id="cert-form">
        <h3>${c ? t('cert_replace') : t('cert_import')}</h3>
        ${field(t('domain'), `<input name="domain" value="${esc(h.domain)}" placeholder="door.company.be" autocapitalize="none" autocorrect="off" spellcheck="false" inputmode="url">`, t('domain_hint'))}
        ${field(t('cert_files'), '<input type="file" name="files" multiple accept=".pfx,.p12,.crt,.cer,.pem,.key">', t('cert_files_hint'))}
        ${field(t('cert_password'), '<input type="password" name="password" autocomplete="off">', t('cert_password_hint'))}
        <button class="btn primary block" type="submit">${t('cert_upload')}</button>
      </form>
      ${c ? `<form class="card stack" id="https-form">
        <h3>${t('https_use')}</h3>
        ${field(t('domain'), `<input name="domain" value="${esc(h.domain)}" autocapitalize="none" autocorrect="off" spellcheck="false" inputmode="url">`)}
        ${toggle('redirect', t('https_redirect'), h.redirect, t('https_redirect_hint'))}
        <button class="btn primary block" type="submit">${t('save')}</button>
      </form>` : ''}
      <section class="card">
        <h3>${t('https_how')}</h3>
        <ol class="howto"><li>${t('https_step1')}</li><li>${t('https_step2')}</li><li>${t('https_step3')}</li><li>${t('https_step4')}</li></ol>
      </section>
    </div>`;

  $('#cert-form').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    if (!f.files.files.length) { toast(t('cert_pick'), 'err'); return; }
    const fd = new FormData();
    Array.from(f.files.files).forEach(x => fd.append('files', x));
    fd.append('password', f.password.value);
    fd.append('domain', f.domain.value);
    await busy($('button[type=submit]', f), async () => {
      try {
        await api('POST', '/api/admin/https', fd);
        toast(t('saved'), 'ok');
        paneHttps();
      } catch (ex) { toast(ex.message, 'err'); }
    });
  };
  const hf = $('#https-form');
  if (hf) {
    hf.onsubmit = async e => {
      e.preventDefault();
      await busy($('button[type=submit]', hf), async () => {
        try {
          const r = await api('PUT', '/api/admin/https', formData(hf));
          toast(t('saved'), 'ok');
          if (r.redirect && !r.secure_now && r.url) location.href = r.url + '/#settings/https';
          else paneHttps();
        } catch (ex) { toast(ex.message, 'err'); }
      });
    };
  }
  const del = $('#cert-del');
  if (del) {
    del.onclick = async () => {
      if (!await confirmSheet(t('cert_remove'), t('cert_remove_q'), t('delete'), true)) return;
      try {
        await api('DELETE', '/api/admin/https');
        toast(t('deleted'), 'ok');
        if (location.protocol === 'https:') location.href = 'http://' + location.hostname + ':8080/#settings/https';
        else paneHttps();
      } catch (ex) { toast(ex.message, 'err'); }
    };
  }
}

// ----- log -----

async function paneLog() {
  const pane = $('#pane');
  pane.innerHTML = paneHead(t('tab_log'), t('log_help')) + `
    <input class="search" type="search" placeholder="${t('search_log')}" id="log-q">
    <ul class="log" id="log-list"></ul>
    <button class="btn ghost block" id="more" hidden>${t('load_more')}</button>`;
  let last = 0;
  let q = '';
  const load = async reset => {
    if (reset) { last = 0; $('#log-list').innerHTML = ''; }
    const d = await api('GET', `/api/admin/log?q=${encodeURIComponent(q)}${last ? '&before=' + last : ''}`);
    $('#log-list').insertAdjacentHTML('beforeend', d.log.map(r => `
      <li class="${r.ok ? '' : 'bad-row'}">
        <span class="log-dot ${r.ok ? 'good' : 'bad'}">${icon(r.ok ? 'check' : 'alert')}</span>
        <div><strong>${esc(r.event)}${r.target ? ': ' + esc(r.target) : ''}</strong>
        <span class="muted small">${esc(r.ts)} · ${esc(r.username || '-')}${r.ip ? ' · ' + esc(r.ip) : ''}</span>
        ${r.detail ? `<span class="small ${r.ok ? 'muted' : 'bad'}">${esc(r.detail)}</span>` : ''}</div>
      </li>`).join(''));
    if (d.log.length) last = d.log[d.log.length - 1].id;
    $('#more').hidden = d.log.length < 100;
    if (!$('#log-list').children.length) $('#log-list').innerHTML = `<li class="muted">${t('nothing_found')}</li>`;
  };
  let timer;
  $('#log-q').oninput = e => { clearTimeout(timer); timer = setTimeout(() => { q = e.target.value.trim(); load(true); }, 300); };
  $('#more').onclick = () => load(false);
  await load(true);
}

boot();
