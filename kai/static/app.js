// ══════════════════════════════════════════════════════════════════════════════
// Kai — app.js
// Main JavaScript for the tabbed web UI.
// Handles: tab switching, dashboard, chat SSE streaming, settings,
//          memory browser, documents, session history, DM mode, Kai face.
// ══════════════════════════════════════════════════════════════════════════════

// ── 0. Crash visibility ──────────────────────────────────────────────────────
// app.js runs as one top-level script: a single uncaught error aborts the rest
// (event wiring + data loads never run), so the UI looks "dead" with no message.
// Surface any such error on-screen + in the console so it's never invisible.
window.addEventListener('error', (e) => {
  const where = (e.filename || '').split('/').pop() + ':' + e.lineno;
  const detail = (e.message || String(e.error || 'error')) + '  (' + where + ')';
  console.error('[Kai] Uncaught JS error:', detail);
  try {
    let bar = document.getElementById('kai-js-error');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'kai-js-error';
      bar.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:999999;' +
        'background:#7f1d1d;color:#fecaca;font:12px/1.5 monospace;padding:8px 12px;' +
        'white-space:pre-wrap;border-top:2px solid #ef4444;max-height:40vh;overflow:auto;';
      (document.body || document.documentElement).appendChild(bar);
      bar._seen = new Set();
    }
    if (!bar._seen.has(detail)) {            // accumulate distinct errors
      bar._seen.add(detail);
      const line = document.createElement('div');
      line.textContent = 'Kai JS error: ' + detail;
      bar.appendChild(line);
    }
  } catch (_) { /* never let the reporter itself throw */ }
});

// ── 0b. localStorage safety net ──────────────────────────────────────────────
// Some embedded webviews (WebKitGTK in private/ephemeral mode) don't expose
// localStorage: a bare reference throws "Can't find variable: localStorage" and
// would abort this entire script on the first preference read. Probe it, and if
// it's missing install an in-memory fallback so the app still runs (preferences
// just won't persist across restarts). The real fix is a persistent webview
// store (see app.py private_mode=False); this is the belt-and-suspenders.
(function () {
  let ok = false;
  try {
    window.localStorage.setItem('__kai_probe', '1');
    window.localStorage.removeItem('__kai_probe');
    ok = true;
  } catch (_) { /* unavailable or blocked */ }
  if (!ok) {
    console.warn('[Kai] localStorage unavailable — using in-memory fallback (settings will not persist).');
    const mem = Object.create(null);
    const shim = {
      getItem:    (k) => (k in mem ? mem[k] : null),
      setItem:    (k, v) => { mem[k] = String(v); },
      removeItem: (k) => { delete mem[k]; },
      clear:      () => { for (const k in mem) delete mem[k]; },
      key:        (i) => Object.keys(mem)[i] ?? null,
      get length() { return Object.keys(mem).length; },
    };
    try { Object.defineProperty(window, 'localStorage', { value: shim, configurable: true, writable: true }); }
    catch (_) { try { window.localStorage = shim; } catch (__) { /* give up */ } }
  }
})();

// ── 1. Config & Globals ─────────────────────────────────────────────────────

if (typeof marked !== 'undefined') {
  marked.setOptions({ breaks: true, gfm: true });
} else {
  console.error('[Kai] marked failed to load — markdown will render as plain text.');
}

const $ = id => document.getElementById(id);

const messagesEl = $('messages');
const inputEl    = $('input');
const sendBtn    = $('sendBtn');
const welcomeEl  = $('welcome');

let isStreaming   = false;
let _activeController = null;   // AbortController for the in-flight /chat stream
let _userStopped     = false;   // true when the current abort was a deliberate Stop

// Stop the in-flight turn: tell the backend to abort (stops tool loops + thinking
// loops server-side) and abort the local stream. Whatever was generated is kept.
function stopStreaming() {
  _userStopped = true;
  fetch('/chat/stop', { method: 'POST' }).catch(() => {});
  if (_activeController) { try { _activeController.abort(); } catch {} }
}

// Toggle the send button between Send (↑) and Stop (■).
function setSendStopMode(on) {
  if (!sendBtn) return;
  const icon = sendBtn.querySelector('.material-symbols-outlined');
  sendBtn.disabled = false;   // must stay clickable so Stop works
  if (on) {
    sendBtn.title = 'Stop';
    if (icon) icon.textContent = 'stop';
    sendBtn.classList.add('is-stop');
  } else {
    sendBtn.title = 'Send (Enter)';
    if (icon) icon.textContent = 'arrow_upward';
    sendBtn.classList.remove('is-stop');
  }
}
let messageCount  = 0;
let _currentUser  = null;   // { name, initial }

// Session metrics
const sessionStart = Date.now();
let totalTokens    = 0;
let thinkTimes     = [];   // ms per completed response
let thinkStart     = 0;

// ── Helper functions ─────────────────────────────────────────────────────────

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function scrollEnd() {
  if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function hideWelcome() {
  if (welcomeEl) welcomeEl.style.display = 'none';
}

// ── 2. Tab Switching ─────────────────────────────────────────────────────────

function switchTab(tabName) {
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tabName)
  );
  document.querySelectorAll('.tab-pane').forEach(p =>
    p.classList.toggle('active', p.id === 'panel-' + tabName)
  );
  if (tabName === 'dashboard') loadDashboard();
  if (tabName === 'study') loadStudy();
  if (tabName === 'memory') loadMemoryBrowser();
  if (tabName === 'chat') {
    if (inputEl) inputEl.focus();
    // Refresh goals banner when returning to chat
    fetch('/goals/active').then(r => r.json()).then(_updateGoalsBanner).catch(() => {});
  }
}

// Tab button click handlers
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// Topbar logout
const logoutBtn = $('topbar-logout');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    try {
      await fetch('/users/logout', { method: 'POST' });
    } catch { /* ignore */ }
    localStorage.removeItem('kai_last_user');
    window.location.href = '/login';
  });
}

// Settings panel logout button
const settingsLogoutBtn = $('settings-logout');
if (settingsLogoutBtn) {
  settingsLogoutBtn.addEventListener('click', async () => {
    try {
      await fetch('/users/logout', { method: 'POST' });
    } catch { /* ignore */ }
    localStorage.removeItem('kai_last_user');
    window.location.href = '/login';
  });
}

// Kai's Computer button
const computerBtn = $('open-computer-btn');
if (computerBtn) {
  computerBtn.addEventListener('click', () => {
    const session = _activeSession || '';
    const boot = session ? 'warm' : 'cold';
    window.open(`/computer?session=${encodeURIComponent(session)}&boot=${boot}`, 'kai-computer');
  });
}

// ── 3. Dashboard ─────────────────────────────────────────────────────────────

async function loadDashboard() {
  // Stats
  try {
    const stats = await fetch('/dashboard/stats').then(r => r.json());
    if (stats) {
      const df = $('dash-facts');      if (df) df.textContent = stats.facts;
      const ds = $('dash-sessions');   if (ds) ds.textContent = stats.sessions;
      const dd = $('dash-documents');  if (dd) dd.textContent = stats.documents;
      const dn = $('dash-notes');      if (dn) dn.textContent = stats.notes;
    }
  } catch { /* ignore */ }

  // Briefing card
  try {
    const briefing = await fetch('/briefing/latest').then(r => r.json());
    const wrap = $('dash-briefing-wrap');
    if (wrap && briefing.content) {
      const dateEl = $('dash-briefing-date');
      const bodyEl = $('dash-briefing-body');
      if (dateEl) dateEl.textContent = new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
      if (bodyEl) bodyEl.textContent = briefing.content;
      wrap.style.display = '';
    }
  } catch { /* ignore */ }

  // Cluster nodes
  try {
    const nodes = await fetch('/api/cluster/nodes').then(r => r.json());
    const container = $('dash-nodes');
    if (container && nodes.length) {
      const now = Date.now() / 1000;
      const rows = nodes.map(n => {
        const online = n.last_seen && (now - n.last_seen) < 120;
        const cls = online ? 'online' : 'offline';
        const label = online ? 'online' : _timeAgo(n.last_seen);
        return `<div class="dash-node-row"><span class="dash-dot ${cls}"></span><span style="font-size:13px;color:var(--text)">${_esc(n.label)}</span><span class="dash-node-label">${label}</span></div>`;
      }).join('');
      // Keep the host row, append cluster nodes
      const host = container.querySelector('.dash-node-row');
      container.innerHTML = (host ? host.outerHTML : '') + rows;
    }
  } catch { /* ignore */ }

  // Active goals
  try {
    const goals = await fetch('/goals/active').then(r => r.json());
    const goalsEl = $('dash-goals');
    if (goalsEl) {
      if (!goals.length) {
        goalsEl.innerHTML = '<span style="font-size:12px;color:var(--muted)">No active goals</span>';
      } else {
        goalsEl.innerHTML = goals.map(g => {
          const pct = g.total_steps > 0 ? Math.round((g.current_step / g.total_steps) * 100) : 0;
          const stepLabel = g.total_steps > 0 ? `Step ${g.current_step + 1}/${g.total_steps}` : 'In progress';
          const nextText = g.next_step ? `→ ${g.next_step}` : '';
          return `<div class="dash-goal-item">
            <div class="dash-goal-title">${_esc(g.title)}</div>
            <div class="dash-goal-meta">${stepLabel}${nextText ? ' · ' + _esc(nextText) : ''}</div>
            ${g.total_steps > 0 ? `<div class="dash-goal-bar"><div class="dash-goal-fill" style="width:${pct}%"></div></div>` : ''}
          </div>`;
        }).join('');
      }
      // Also update the chat goals banner
      _updateGoalsBanner(goals);
    }
  } catch { /* ignore */ }

  // Local containers / VMs
  loadContainers();

  // Developer system stats (only when dev mode is on)
  if (_devMode) loadDevStats();

  // Recent sessions
  loadSessions();
}

// ── Containers / VMs (local LXD/Incus) ────────────────────────────────────────
async function loadContainers() {
  const el = $('dash-containers');
  if (!el) return;
  try {
    const data = await fetch('/api/containers').then(r => r.json());
    if (!data.available) {
      el.innerHTML = '<div style="font-size:12px;color:var(--muted)">No container manager installed (LXD/Incus).</div>';
      return;
    }
    const instances = data.instances || [];
    if (!instances.length) {
      el.innerHTML = '<div style="font-size:12px;color:var(--muted)">No containers yet.</div>';
      return;
    }
    el.innerHTML = instances.map(ci => {
      const running = (ci.status || '').toLowerCase() === 'running';
      const dot = running ? 'online' : 'offline';
      const name = _esc(ci.name);
      const ip = ci.ipv4 ? `<span class="dash-ct-ip">${_esc(ci.ipv4)}</span>` : '';
      const toggle = running
        ? `<button class="dash-ct-btn" title="Stop" data-ct-action="stop" data-ct-name="${name}"><span class="material-symbols-outlined" style="font-size:16px">stop_circle</span></button>`
        : `<button class="dash-ct-btn" title="Start" data-ct-action="start" data-ct-name="${name}"><span class="material-symbols-outlined" style="font-size:16px">play_circle</span></button>`;
      const del = `<button class="dash-ct-btn danger" title="Delete" data-ct-action="delete" data-ct-name="${name}"><span class="material-symbols-outlined" style="font-size:16px">delete</span></button>`;
      return `<div class="dash-ct-row">
        <span class="dash-dot ${dot}"></span>
        <span class="dash-ct-name" title="${name}">${name}</span>
        ${ip}
        <span class="dash-ct-actions">${toggle}${del}</span>
      </div>`;
    }).join('');
  } catch {
    el.innerHTML = '<div style="font-size:12px;color:var(--muted)">Failed to load containers.</div>';
  }
}

async function _containerAction(name, action, btn) {
  if (action === 'delete' && !confirm(`Delete container "${name}"? This cannot be undone.`)) return;
  if (btn) btn.disabled = true;
  try {
    await fetch('/api/containers/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, action }),
    });
  } catch { /* ignore — the re-poll below shows ground truth */ }
  // start/stop/delete take a moment to settle; re-poll for the real state.
  setTimeout(loadContainers, 700);
}

// ── Developer mode: live system stats on the dashboard ────────────────────────
// Toggle lives in Settings; the panel lives on the Dashboard. Stats reuse the
// diagnostic tools (subprocess-backed, a few seconds each), so they load on
// demand — on toggle-on, dashboard open, and the refresh button — never polled.
let _devMode = localStorage.getItem('kai_dev_mode') === 'true';

function applyDevMode() {
  const sec = $('dash-dev-section');
  if (sec) sec.style.display = _devMode ? '' : 'none';
  const btn = $('dev-toggle');
  if (btn) {
    btn.classList.toggle('active', _devMode);
    const state = $('dev-state');
    if (state) state.textContent = _devMode ? 'On' : 'Off';
  }
}

async function loadDevStats() {
  const tEl = $('dash-dev-temps'), nEl = $('dash-dev-network'), dEl = $('dash-dev-disk');
  if (!tEl || !nEl || !dEl) return;
  tEl.textContent = nEl.textContent = dEl.textContent = 'Loading…';
  try {
    const s = await fetch('/api/dev/stats').then(r => r.json());
    tEl.textContent = s.temps   || '—';
    nEl.textContent = s.network || '—';
    dEl.textContent = s.disk    || '—';
  } catch {
    tEl.textContent = nEl.textContent = dEl.textContent = 'Failed to load.';
  }
}

function _updateGoalsBanner(goals) {
  const banner = $('goals-banner');
  const textEl = $('goals-banner-text');
  if (!banner || !textEl) return;
  if (!goals || !goals.length) {
    banner.style.display = 'none';
    return;
  }
  const names = goals.slice(0, 2).map(g => g.title).join(' · ');
  const more = goals.length > 2 ? ` +${goals.length - 2} more` : '';
  textEl.textContent = names + more;
  banner.style.display = 'flex';
}

function _timeAgo(ts) {
  if (!ts) return 'never';
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return `${Math.floor(diff/86400)}d ago`;
}

function _esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Memory Browser ────────────────────────────────────────────────────────────
let _memTab = 'facts';
let _memLoaded = { facts: false, timeline: false, goals: false, reflections: false };

async function loadMemoryBrowser(tab) {
  tab = tab || _memTab;
  _memTab = tab;

  // Switch active tab style
  document.querySelectorAll('.mem-tab').forEach(b => b.classList.toggle('active', b.dataset.memtab === tab));
  document.querySelectorAll('.mem-pane').forEach(p => {
    const id = p.id.replace('memtab-', '');
    p.style.display = id === tab ? 'block' : 'none';
  });

  if (_memLoaded[tab]) return;
  _memLoaded[tab] = true;

  if (tab === 'facts') await _loadMemFacts();
  if (tab === 'timeline') await _loadMemTimeline();
  if (tab === 'goals') await _loadMemGoals();
  if (tab === 'reflections') await _loadMemReflections();
}

async function _loadMemFacts(q) {
  const grid = $('mem-facts-grid');
  if (!grid) return;
  try {
    const url = q ? `/memory/search?q=${encodeURIComponent(q)}` : '/memory/facts';
    let facts;
    if (q) {
      const res = await fetch(url).then(r => r.json());
      facts = res.facts || [];
    } else {
      facts = await fetch(url).then(r => r.json());
    }
    if (!facts.length) { grid.innerHTML = '<div style="color:var(--muted);font-size:13px">No facts found.</div>'; return; }
    grid.innerHTML = facts.map(f => `
      <div class="mem-fact-card">
        <div class="mem-fact-key">${_esc(f.key)}</div>
        <div class="mem-fact-value">${_esc(f.value)}</div>
        <div class="mem-fact-meta">
          <span>${_esc(f.source || 'conversation')}</span>
          <span>${f.updated_at || ''}</span>
        </div>
      </div>`).join('');
  } catch { grid.innerHTML = '<div style="color:var(--muted);font-size:13px">Failed to load.</div>'; }
}

async function _loadMemTimeline() {
  const container = $('mem-timeline');
  if (!container) return;
  try {
    const entries = await fetch('/memory/episodic').then(r => r.json());
    if (!entries.length) { container.innerHTML = '<div style="color:var(--muted);font-size:13px">No memory entries yet.</div>'; return; }
    // Group by date
    const groups = {};
    entries.forEach(e => {
      const day = e.timestamp ? e.timestamp.split(' ')[0] : 'Unknown';
      if (!groups[day]) groups[day] = [];
      groups[day].push(e);
    });
    container.innerHTML = Object.entries(groups).map(([day, eps]) => `
      <div class="mem-day-label">${day}</div>
      ${eps.map(e => `<div class="mem-ep-row">
        <span class="mem-ep-type">${_esc(e.entry_type || 'turn')}</span>
        <span class="mem-ep-content">${_esc(e.content)}</span>
        <span class="mem-ep-time">${e.timestamp ? e.timestamp.split(' ')[1] || '' : ''}</span>
      </div>`).join('')}`).join('');
  } catch { container.innerHTML = '<div style="color:var(--muted);font-size:13px">Failed to load.</div>'; }
}

async function _loadMemGoals() {
  try {
    const goals = await fetch('/goals/all').then(r => r.json());
    const byStatus = { active: [], done: [], abandoned: [] };
    goals.forEach(g => { if (byStatus[g.status]) byStatus[g.status].push(g); });
    ['active', 'done', 'abandoned'].forEach(s => {
      const el = document.getElementById(`mem-goals-${s}`);
      if (!el) return;
      if (!byStatus[s].length) { el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:4px 0">None</div>'; return; }
      el.innerHTML = byStatus[s].map(g => {
        const pct = g.total_steps > 0 ? Math.round((g.current_step / g.total_steps) * 100) : 0;
        return `<div class="mem-goal-card">
          <div class="mem-goal-card-title">${_esc(g.title)}</div>
          <div class="mem-goal-card-step">${g.description ? _esc(g.description.slice(0,80)) : (g.steps[g.current_step] || '')}</div>
          ${g.total_steps > 0 ? `<div class="mem-goal-card-bar"><div class="mem-goal-card-fill" style="width:${pct}%"></div></div>` : ''}
        </div>`;
      }).join('');
    });
  } catch {
    ['active', 'done', 'abandoned'].forEach(s => {
      const el = document.getElementById(`mem-goals-${s}`);
      if (el) el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:4px 0">Failed to load.</div>';
    });
  }
}

async function _loadMemReflections() {
  const container = $('mem-reflections');
  if (!container) return;
  try {
    const entries = await fetch('/memory/episodic').then(r => r.json());
    const refs = entries.filter(e => e.entry_type === 'reflection' || e.entry_type === 'learned');
    if (!refs.length) { container.innerHTML = '<div style="color:var(--muted);font-size:13px">No reflections yet.</div>'; return; }
    container.innerHTML = refs.map(e => `
      <div class="mem-reflection-entry">
        ${_esc(e.content)}
        <div class="mem-reflection-date">${e.timestamp || ''} · ${_esc(e.entry_type)}</div>
      </div>`).join('');
  } catch { container.innerHTML = '<div style="color:var(--muted);font-size:13px">Failed to load.</div>'; }
}

// Wire dashboard quick actions
const dashNewChat = $('dash-new-chat');
if (dashNewChat) {
  dashNewChat.addEventListener('click', async () => {
    await fetch('/clear', { method: 'POST' }).catch(() => {});
    if (messagesEl) messagesEl.innerHTML = '';
    if (welcomeEl) {
      messagesEl.appendChild(welcomeEl);
      welcomeEl.style.display = '';
    }
    messageCount = 0;
    switchTab('chat');
  });
}

const dashUploadDoc = $('dash-upload-doc');
if (dashUploadDoc) {
  dashUploadDoc.addEventListener('click', () => {
    switchTab('settings');
    // Give the panel a moment to appear, then open docs panel
    setTimeout(() => openDocsPanel(), 100);
  });
}

// ── 4. Kai Face (full animation system) ──────────────────────────────────────
//
// Three-tier expression system:
//   1. Auto-preset from brain state (idle/thinking/working/error)
//   2. Named shortcuts — Kai writes <face:annoyed> in her response
//   3. Compositional override — <face eyes=smug mouth=smirk flair=sparkle>
//
// Part vocabulary: 8 eyes × 8 mouths × 10 flairs = 640 combinations
// Names on top (for the model), numeric IDs underneath (for animation math)

// ── Part Library ─────────────────────────────────────────────────────────────

const EYES = {
  neutral:     { id: 0, full: '\u00B7', compact: '\u00B7', blink: '-' },
  bright:      { id: 1, full: '\u25D5', compact: '\u25D5', blink: '-' },
  wide:        { id: 2, full: '\u00B0', compact: '\u00B0', blink: '-' },
  smug:        { id: 3, full: '\u00AC', compact: '\u00AC', blink: '-' },
  disapproval: { id: 4, full: '\u0CA0', compact: '\u0CA0', blink: '-' },
  closed:      { id: 5, full: '-',      compact: '-',      blink: '-' },
  dead:        { id: 6, full: 'x',      compact: 'x',      blink: '-' },
  asymmetric:  { id: 7, full: '^',      compact: '^',      blink: '-' },
};

const MOUTHS = {
  flat:    { id: 0, ch: '_' },
  smile:   { id: 1, ch: '\u203F' },
  smirk:   { id: 2, ch: '\u1D17' },
  frown:   { id: 3, ch: '\u2054' },
  open:    { id: 4, ch: 'o' },
  wide:    { id: 5, ch: 'O' },
  grimace: { id: 6, ch: '~' },
  angry:   { id: 7, ch: '\u2038' },
};

const FLAIRS = {
  none:     { id: 0, left: '', right: '' },
  fist:     { id: 1, left: '', right: ' !' },
  wave:     { id: 2, left: '', right: '\uFF89' },
  arms_up:  { id: 3, left: '\u30FD', right: '\uFF89' },
  sparkle:  { id: 4, left: '\u2727', right: '\u2727' },
  sweat:    { id: 5, left: '', right: ';' },
  breath:   { id: 6, left: '', right: '~' },
  zzz:      { id: 7, left: '', right: 'Zz' },
  question: { id: 8, left: '', right: '?' },
  exclaim:  { id: 9, left: '', right: '!' },
};

// ── Composition Engine ───────────────────────────────────────────────────────

function composeFace(eyeName, mouthName, flairName) {
  const eye   = EYES[eyeName]   || EYES.neutral;
  const mouth = MOUTHS[mouthName] || MOUTHS.flat;
  const flair = FLAIRS[flairName] || FLAIRS.none;
  const full    = `${flair.left}( ${eye.full} ${mouth.ch} ${eye.full} )${flair.right}`;
  const compact = `${eye.compact}${mouth.ch}${eye.compact}`;
  const faceId  = eye.id * 80 + mouth.id * 10 + flair.id;
  return { full, compact, faceId };
}

// ── 15 Named Presets ─────────────────────────────────────────────────────────

const FACE_PRESETS = {
  idle:        { eyes: 'asymmetric', mouth: 'smile',   flair: 'none' },
  thinking:    { eyes: 'wide',       mouth: 'grimace', flair: 'question' },
  working:     { eyes: 'closed',     mouth: 'flat',    flair: 'fist' },
  focused:     { eyes: 'neutral',    mouth: 'flat',    flair: 'none' },
  happy:       { eyes: 'bright',     mouth: 'smile',   flair: 'none' },
  amused:      { eyes: 'asymmetric', mouth: 'smirk',   flair: 'none' },
  proud:       { eyes: 'bright',     mouth: 'smirk',   flair: 'sparkle' },
  excited:     { eyes: 'bright',     mouth: 'wide',    flair: 'arms_up' },
  annoyed:     { eyes: 'smug',       mouth: 'flat',    flair: 'none' },
  confused:    { eyes: 'wide',       mouth: 'open',    flair: 'question' },
  surprised:   { eyes: 'wide',       mouth: 'wide',    flair: 'exclaim' },
  sympathetic: { eyes: 'neutral',    mouth: 'frown',   flair: 'none' },
  tired:       { eyes: 'closed',     mouth: 'grimace', flair: 'breath' },
  sleepy:      { eyes: 'closed',     mouth: 'flat',    flair: 'zzz' },
  error:       { eyes: 'dead',       mouth: 'flat',    flair: 'none' },
};

// State-driven presets (brain states that don't come from <face> tags)
const STATE_PRESETS = {
  idle:       'idle',
  blink:      null,       // special — handled by blink animation
  waking:     'thinking',
  thinking:   'thinking',
  working:    'working',
  responding: 'happy',
  done:       'proud',
  error:      'error',
};

function getFace(presetName) {
  const p = FACE_PRESETS[presetName] || FACE_PRESETS.idle;
  return composeFace(p.eyes, p.mouth, p.flair);
}

// Build blink frame for any preset (same mouth+flair, eyes closed)
function getBlinkFrame(presetName) {
  const p = FACE_PRESETS[presetName] || FACE_PRESETS.idle;
  return composeFace('closed', p.mouth, p.flair);
}

// Legacy compat — FACES and COMPACT_FACES as computed objects
const FACES = {};
const COMPACT_FACES = {};
for (const name of Object.keys(FACE_PRESETS)) {
  const f = getFace(name);
  FACES[name] = f.full;
  COMPACT_FACES[name] = f.compact;
}
// Extra states that map to presets
FACES.blink      = composeFace('closed', 'smile', 'none').full;
COMPACT_FACES.blink = composeFace('closed', 'smile', 'none').compact;
FACES.waking     = FACES.thinking;
COMPACT_FACES.waking = COMPACT_FACES.thinking;
FACES.responding = FACES.happy;
COMPACT_FACES.responding = COMPACT_FACES.happy;
FACES.done       = FACES.proud;
COMPACT_FACES.done = COMPACT_FACES.proud;

// ── Face Display ─────────────────────────────────────────────────────────────

const faceEl = $('kai-face');
let _blinkTimer   = null;
let _doneTimer    = null;
let currentAvatar = null;   // avatar element of the active response bubble
let _currentPreset = 'idle';

function setFace(state) {
  if (!faceEl) return;
  const preset = STATE_PRESETS[state] || state;
  if (FACE_PRESETS[preset]) {
    _currentPreset = preset;
  }
  const targetFull    = FACES[state] ?? FACES[preset] ?? FACES.idle;
  const targetCompact = COMPACT_FACES[state] ?? COMPACT_FACES[preset] ?? COMPACT_FACES.idle;

  // 3-stage blink transition: current → blink → target
  const blinkFull    = getBlinkFrame(_currentPreset).full;
  const blinkCompact = getBlinkFrame(_currentPreset).compact;

  faceEl.textContent = blinkFull;
  if (currentAvatar) currentAvatar.textContent = blinkCompact;

  setTimeout(() => {
    if (faceEl) faceEl.textContent = targetFull;
    if (currentAvatar) currentAvatar.textContent = targetCompact;
  }, 150);
}

function setComposedFace(eyeName, mouthName, flairName) {
  if (!faceEl) return;
  const face = composeFace(eyeName, mouthName, flairName);

  // Blink transition
  const blinkFace = composeFace('closed', mouthName, flairName);
  faceEl.textContent = blinkFace.full;
  if (currentAvatar) currentAvatar.textContent = blinkFace.compact;

  setTimeout(() => {
    if (faceEl) faceEl.textContent = face.full;
    if (currentAvatar) currentAvatar.textContent = face.compact;
  }, 150);
}

function startIdleBlink() {
  stopIdleBlink();
  function scheduleBlink() {
    _blinkTimer = setTimeout(() => {
      const idleFull = FACES[_currentPreset] || FACES.idle;
      if (faceEl && faceEl.textContent === idleFull) {
        const blink = getBlinkFrame(_currentPreset);
        faceEl.textContent = blink.full;
        setTimeout(() => {
          if (faceEl && faceEl.textContent === blink.full) {
            faceEl.textContent = idleFull;
          }
          scheduleBlink();
        }, 120);
      } else {
        scheduleBlink();
      }
    }, 3500 + Math.random() * 4000);
  }
  scheduleBlink();
}

function stopIdleBlink() {
  if (_blinkTimer) { clearTimeout(_blinkTimer); _blinkTimer = null; }
}

// ── Face Tag Parser ──────────────────────────────────────────────────────────
// Strips <face:...> tags from Kai's response and applies them.
// Two forms:
//   <face:annoyed>       → named shortcut
//   <face eyes=smug mouth=smirk flair=sparkle> → compositional

const FACE_TAG_RE = /<face(?::(\w+)|(\s+[^>]+))>/g;

function parseFaceTags(text) {
  let cleaned = text;
  let match;
  FACE_TAG_RE.lastIndex = 0;
  while ((match = FACE_TAG_RE.exec(text)) !== null) {
    if (match[1]) {
      // Named shortcut: <face:annoyed>
      const name = match[1];
      if (FACE_PRESETS[name]) {
        setFace(name);
      }
    } else if (match[2]) {
      // Compositional: <face eyes=smug mouth=smirk flair=sparkle>
      const attrs = match[2];
      const eyes  = (attrs.match(/eyes=(\w+)/) || [])[1] || 'neutral';
      const mouth = (attrs.match(/mouth=(\w+)/) || [])[1] || 'flat';
      const flair = (attrs.match(/flair=(\w+)/) || [])[1] || 'none';
      setComposedFace(eyes, mouth, flair);
    }
    cleaned = cleaned.replace(match[0], '');
  }
  return cleaned;
}

// ── Waking up animation ──────────────────────────────────────────────────────

let _wakingActive = false;

const WAKING_FRAMES = [
  { eyes: 'closed',  mouth: 'flat',    flair: 'none' },     // heavy-lidded
  { eyes: 'closed',  mouth: 'flat',    flair: 'none' },     // hold
  { eyes: 'closed',  mouth: 'flat',    flair: 'breath' },   // sigh
  { eyes: 'closed',  mouth: 'grimace', flair: 'none' },     // rubbing
  { eyes: 'closed',  mouth: 'grimace', flair: 'none' },     // still rubbing
  { eyes: 'wide',    mouth: 'flat',    flair: 'none' },     // eyes snap open
  { eyes: 'wide',    mouth: 'grimace', flair: 'none' },     // blinking it off
  { eyes: 'neutral', mouth: 'smirk',   flair: 'none' },     // almost there
];

async function playWakingAnimation() {
  _wakingActive = true;
  while (_wakingActive) {
    for (const frame of WAKING_FRAMES) {
      if (!_wakingActive) break;
      const f = composeFace(frame.eyes, frame.mouth, frame.flair);
      if (faceEl) { faceEl.textContent = f.full; }
      if (currentAvatar) { currentAvatar.textContent = f.compact; }
      await sleep(260);
    }
  }
  _wakingActive = false;
}

function stopWakingAnimation() {
  _wakingActive = false;
}

function faceOnStatus(statusText) {
  stopWakingAnimation();
  const t = statusText.toLowerCase();
  if (t.includes('waking') || t.includes('thinking') || t.includes('loading')) {
    setFace('thinking');
  } else if (t.includes('responding')) {
    setFace('responding');
  } else if (t.includes('analyzing')) {
    setFace('focused');
  } else {
    setFace('working');
  }
}

function faceOnDone(hadError) {
  stopIdleBlink();
  if (_doneTimer) clearTimeout(_doneTimer);
  setFace(hadError ? 'error' : 'done');
  _doneTimer = setTimeout(() => {
    currentAvatar = null;
    setFace('idle');
    _currentPreset = 'idle';
    startIdleBlink();
  }, 2200);
}

// Start idle blinking after initial load delay
setTimeout(() => startIdleBlink(), 2800);

// ── 5. Sidebar Data ──────────────────────────────────────────────────────────

async function loadInfo() {
  try {
    const d = await fetch('/info').then(r => r.json());
    const model = (d.model || '').replace(':latest', '');

    // Model row
    const modelEl = $('s-model');
    if (modelEl) modelEl.textContent = model;

    // Fact count badge
    const badge = $('s-fact-count');
    if (badge) badge.textContent = d.facts ?? 0;

    // Context window
    const ctxEl = $('s-ctx');
    if (ctxEl) ctxEl.textContent = d.context_window ? `${d.context_window.toLocaleString()} tok` : '\u2014';

    // Footer hint
    const hint = document.querySelector('.input-hint');
    if (hint) hint.textContent = `Running locally \u00B7 ${model}`;

    // Memory highlights (settings panel)
    const hl = $('s-highlights');
    if (hl && d.highlights && d.highlights.length) {
      hl.innerHTML = d.highlights.map(h =>
        `<div class="info-row"><span class="info-key">${esc(h.key)}</span><span class="info-val" title="${esc(h.value)}">${esc(h.value)}</span></div>`
      ).join('');
    } else if (hl) {
      hl.innerHTML = '<div class="info-row"><span class="info-key">\u2014</span><span class="info-val">no facts yet</span></div>';
    }

    // Add User section \u2014 owner only
    const addUserSection = $('add-user-section');
    if (addUserSection) addUserSection.style.display = d.is_owner ? '' : 'none';

    // System control (restart / shutdown) \u2014 owner only
    const sysControl = $('system-control-section');
    if (sysControl) sysControl.style.display = d.is_owner ? '' : 'none';
  } catch { /* ignore */ }
}

// Add-user form (owner only)
(function initUserAddForm() {
  const addBtn    = $('user-add-btn');
  const form      = $('user-add-form');
  const cancelBtn = $('user-cancel-btn');
  const saveBtn   = $('user-save-btn');
  const nameIn    = $('user-add-name');
  const pinIn     = $('user-add-pin');
  const msgEl     = $('user-add-msg');

  if (!addBtn || !form) return;

  addBtn.addEventListener('click', () => {
    form.style.display = form.style.display === 'none' ? '' : 'none';
    if (msgEl) msgEl.textContent = '';
  });

  if (cancelBtn) cancelBtn.addEventListener('click', () => {
    form.style.display = 'none';
    nameIn.value = '';
    pinIn.value = '';
    if (msgEl) msgEl.textContent = '';
  });

  if (saveBtn) saveBtn.addEventListener('click', async () => {
    const name = nameIn.value.trim();
    const pin  = pinIn.value;

    if (!name) { if (msgEl) msgEl.textContent = 'Enter a name.'; return; }
    if (pin.length < 4) { if (msgEl) msgEl.textContent = 'PIN must be at least 4 digits.'; return; }

    saveBtn.disabled = true;
    try {
      const resp = await fetch('/users/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, pin }),
      });
      if (!resp.ok) {
        const err = await resp.json();
        if (msgEl) msgEl.textContent = err.detail || 'Failed to add user.';
        return;
      }
      nameIn.value = '';
      pinIn.value = '';
      form.style.display = 'none';
      if (msgEl) msgEl.textContent = `${esc(name)} can now log in with their PIN.`;
    } catch {
      if (msgEl) msgEl.textContent = 'Network error. Please try again.';
    } finally {
      saveBtn.disabled = false;
    }
  });
})();

// System control (owner only) — clean restart / shutdown with progress overlay
(function initSystemControl() {
  const softBtn = $('sys-restart-soft');
  const hardBtn = $('sys-restart-hard');
  const downBtn = $('sys-shutdown');
  if (!softBtn || !hardBtn || !downBtn) return;

  function overlay(title) {
    let o = document.getElementById('kai-admin-overlay');
    if (!o) {
      o = document.createElement('div');
      o.id = 'kai-admin-overlay';
      o.style.cssText = 'position:fixed;inset:0;z-index:99999;display:flex;align-items:center;'
        + 'justify-content:center;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);'
        + 'font-family:system-ui,sans-serif';
      o.innerHTML = '<div style="background:#1e1e2e;color:#cdd6f4;border-radius:12px;'
        + 'padding:28px 36px;text-align:center;min-width:320px;box-shadow:0 20px 60px rgba(0,0,0,.5)">'
        + '<div id="kai-admin-title" style="font-size:17px;font-weight:600;margin-bottom:10px"></div>'
        + '<div id="kai-admin-detail" style="font-size:13px;color:#a6adc8">Working…</div>'
        + '<div style="height:6px;border-radius:3px;background:#313244;margin-top:14px;overflow:hidden">'
        + '<div id="kai-admin-bar" style="height:100%;width:0;background:#e67e22;transition:width .3s"></div>'
        + '</div></div>';
      document.body.appendChild(o);
    }
    o.style.display = 'flex';
    document.getElementById('kai-admin-title').textContent = title;
    return o;
  }

  // Poll /api/admin/shutdown-status. onDone(st) fires when the ritual completes;
  // onGone() when the server stops answering (process exited); onBack() when it
  // answers again after having been gone (hard restart came back up).
  function poll({ onDone, onGone, onBack } = {}) {
    let sawGone = false;
    const tick = async () => {
      let st = null;
      try { const r = await fetch('/api/admin/shutdown-status'); st = r.ok ? await r.json() : null; }
      catch { st = null; }
      if (st) {
        if (sawGone) { if (onBack) return onBack(); }
        const d = document.getElementById('kai-admin-detail');
        const bar = document.getElementById('kai-admin-bar');
        if (d && st.phase) d.textContent = st.detail || st.phase;
        if (bar && typeof st.pct === 'number') bar.style.width = Math.max(5, st.pct) + '%';
        if (st.done && onDone) return onDone(st);
      } else {
        sawGone = true;
        if (onGone && onGone()) return;  // truthy return stops polling
      }
      setTimeout(tick, 800);
    };
    tick();
  }

  async function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : '{}',
    });
  }

  softBtn.addEventListener('click', async () => {
    if (!confirm('Soft restart Kai? In-flight work is saved and brains reload; models stay warm.')) return;
    await post('/api/admin/restart', { mode: 'soft' });
    overlay('Restarting…');
    poll({ onDone: (st) => { if (st.mode === 'soft-restart') setTimeout(() => location.reload(), 600); } });
  });

  hardBtn.addEventListener('click', async () => {
    if (!confirm('Hard restart Kai? It finishes saving the session, then restarts the whole app.')) return;
    await post('/api/admin/restart', { mode: 'hard' });
    overlay('Restarting…');
    poll({ onBack: () => location.reload() });
  });

  downBtn.addEventListener('click', async () => {
    if (!confirm('Shut down Kai? It will finish saving the session (embeddings), then exit.')) return;
    await post('/api/admin/shutdown');
    overlay('Shutting down…');
    poll({ onGone: () => {
      const t = document.getElementById('kai-admin-title');
      const d = document.getElementById('kai-admin-detail');
      const bar = document.getElementById('kai-admin-bar');
      if (t) t.textContent = 'Kai has shut down';
      if (d) d.textContent = 'Session saved. You can close this tab.';
      if (bar) bar.style.width = '100%';
      return true;  // stop polling
    }});
  });
})();

// Uptime ticker
setInterval(() => {
  const secs = Math.floor((Date.now() - sessionStart) / 1000);
  const m = Math.floor(secs / 60), s = secs % 60;
  const el = $('s-uptime');
  if (el) el.textContent = `${m}:${s.toString().padStart(2, '0')}`;
}, 1000);

// ── 6. Chat (core SSE streaming) ─────────────────────────────────────────────

function addUserBubble(text) {
  hideWelcome();
  const wrap = document.createElement('div');
  wrap.className = 'msg-wrap user';
  const initial = _currentUser ? _currentUser.initial : 'U';
  wrap.innerHTML = `
    <div class="avatar">${initial}</div>
    <div class="bubble">${esc(text)}</div>
  `;
  messagesEl.appendChild(wrap);
  scrollEnd();
}

function addKaiBubble() {
  hideWelcome();
  const isFirst    = messageCount === 0;
  const statusText = isFirst ? 'Waking up\u2026' : 'Thinking\u2026';
  const initFace   = COMPACT_FACES[isFirst ? 'waking' : 'thinking'];
  messageCount++;

  const wrap = document.createElement('div');
  wrap.className = 'msg-wrap ai';
  wrap.innerHTML = `
    <div class="avatar">${initFace}</div>
    <div class="bubble">
      <div class="status-bar" id="si">
        <div class="dots"><span></span><span></span><span></span></div>
        <span class="status-text">${statusText}</span>
      </div>
      <div class="content"></div>
    </div>
  `;
  messagesEl.appendChild(wrap);
  scrollEnd();

  const si      = wrap.querySelector('#si');
  const content = wrap.querySelector('.content');
  si.removeAttribute('id');
  currentAvatar = wrap.querySelector('.avatar');

  // Drop-in animation
  currentAvatar.classList.add('dropping');
  currentAvatar.addEventListener('animationend', () =>
    currentAvatar && currentAvatar.classList.remove('dropping'), { once: true });

  // Waking sequence for the first message
  if (isFirst) playWakingAnimation();

  return { si, content };
}

let _statusTimer = null;
let _statusStart = 0;
let _statusBase  = '';

function setStatus(si, text) {
  if (si && si.isConnected) si.querySelector('.status-text').textContent = text;
  _startStatusTimer(si, text);
}

function _startStatusTimer(si, text) {
  _clearStatusTimer();
  _statusBase  = text;
  _statusStart = Date.now();
  _statusTimer = setInterval(() => {
    const elapsed = Math.round((Date.now() - _statusStart) / 1000);
    if (elapsed >= 3 && si && si.isConnected) {
      si.querySelector('.status-text').textContent = `${_statusBase} (${elapsed}s)`;
    }
  }, 1000);
}

function _clearStatusTimer() {
  if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null; }
}

function hideStatus(si) {
  _clearStatusTimer();
  if (si && si.isConnected) si.style.display = 'none';
}

function appendText(content, token) {
  content.textContent += token;
  scrollEnd();
}

function safeMarkdown(text) {
  // Degrade to safe plain text if the vendored libs didn't load, rather than
  // throwing mid-render and breaking the chat stream.
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    return esc(text).replace(/\n/g, '<br>');
  }
  return DOMPurify.sanitize(marked.parse(text));
}

function renderMarkdown(content, text) {
  content.innerHTML = safeMarkdown(text);
  scrollEnd();
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || isStreaming) return;

  isStreaming       = true;
  setSendStopMode(true);
  inputEl.value     = '';
  inputEl.style.height = 'auto';
  thinkStart = Date.now();
  stopIdleBlink();

  const isFirstMsg = messageCount === 0;
  setFace(isFirstMsg ? 'waking' : 'thinking');

  addUserBubble(text);
  const { si, content } = addKaiBubble();

  let fullText      = '';
  let hasTokens      = false;
  let statusLog      = [];
  let pendingReason  = null;
  let pendingConfirm = null;
  let liveThinkPre   = null;   // live-streaming reasoning <pre>, if any
  let streamError    = null;   // set if an 'error' event arrives mid-stream

  const controller = new AbortController();
  _activeController = controller;
  _userStopped = false;
  const streamTimeout = setTimeout(() => controller.abort(), 5 * 60 * 1000); // 5 min ceiling

  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
      signal: controller.signal,
    });

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer    = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }

        if (ev.type === 'status') {
          const skipAsWaking = isFirstMsg && ev.text === 'Thinking...';
          if (!hasTokens && !skipAsWaking) setStatus(si, ev.text);
          if (!skipAsWaking) faceOnStatus(ev.text);
          if (ev.text !== 'Thinking...' && ev.text !== 'Responding...' && ev.text !== 'Compressing memory...') {
            if (pendingReason) {
              statusLog.push({ type: 'reason', text: pendingReason });
              pendingReason = null;
            }
            statusLog.push({ type: 'tool', text: ev.text });
          }

        } else if (ev.type === 'think_step') {
          pendingReason = ev.text;

        } else if (ev.type === 'think_token') {
          // Live reasoning — stream it into an open block so you can watch Kai
          // think (and tell a long thought apart from a stuck loop).
          if (!hasTokens) { hideStatus(si); stopWakingAnimation(); }
          if (!liveThinkPre) {
            const d = document.createElement('details');
            d.className = 'think-block';
            d.open = true;
            d.innerHTML = '<summary>Thinking…</summary><pre></pre>';
            content.parentNode.insertBefore(d, content);
            liveThinkPre = d.querySelector('pre');
          }
          liveThinkPre.textContent += ev.text;
          scrollEnd();

        } else if (ev.type === 'think') {
          if (!hasTokens) { hideStatus(si); stopWakingAnimation(); }
          if (liveThinkPre) {
            // Finalize the live block: full text, relabel, collapse.
            liveThinkPre.textContent = ev.text;
            const det = liveThinkPre.closest('details');
            if (det) { const s = det.querySelector('summary'); if (s) s.textContent = 'Reasoning'; det.open = false; }
            liveThinkPre = null;
          } else {
            const thinkEl = document.createElement('details');
            thinkEl.className = 'think-block';
            thinkEl.innerHTML = `<summary>Reasoning</summary><pre>${esc(ev.text)}</pre>`;
            content.parentNode.insertBefore(thinkEl, content);
          }
          scrollEnd();

        } else if (ev.type === 'confirm_tool') {
          console.log('[confirm_tool] received:', ev.name, ev.label);
          pendingConfirm = { name: ev.name, label: ev.label, diff: ev.diff || '' };

        } else if (ev.type === 'token') {
          if (!hasTokens) { hideStatus(si); hasTokens = true; stopWakingAnimation(); setFace('responding'); }
          fullText += ev.text;
          totalTokens++;
          const tokEl = $('s-tokens');
          if (tokEl) tokEl.textContent = totalTokens.toLocaleString();
          // Parse and strip face tags from token text before display
          const cleanToken = parseFaceTags(ev.text);
          if (cleanToken) appendText(content, cleanToken);

        } else if (ev.type === 'done') {
          _clearStatusTimer();
          // Strip face tags from final text before rendering
          fullText = fullText.replace(FACE_TAG_RE, '');
          if (fullText) {
            // Detect morning briefing prefix — wrap in styled card
            const briefingMatch = fullText.match(/^\[MORNING BRIEFING[^\]]*\]([\s\S]*)/);
            if (briefingMatch) {
              const dateStr = new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
              const bodyHtml = safeMarkdown(briefingMatch[1].trim());
              content.innerHTML = `<div class="briefing-msg-card"><div class="briefing-msg-title">Morning Briefing · ${_esc(dateStr)}</div>${bodyHtml}</div>`;
            } else {
              renderMarkdown(content, fullText);
            }
            // Inject copy buttons into code blocks
            content.querySelectorAll('pre > code').forEach(codeEl => {
              const pre = codeEl.parentElement;
              if (pre.querySelector('.copy-btn')) return;
              const btn = document.createElement('button');
              btn.className = 'copy-btn';
              btn.textContent = 'Copy';
              btn.addEventListener('click', () => {
                navigator.clipboard.writeText(codeEl.textContent).then(() => {
                  btn.textContent = 'Copied!';
                  setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
                });
              });
              pre.style.position = 'relative';
              pre.appendChild(btn);
            });
          } else if (!hasTokens) hideStatus(si);
          // If an error cut the stream short, say so \u2014 don't let a partial
          // response look like a complete, intentional answer.
          if (streamError) {
            const note = document.createElement('div');
            note.className = 'stream-error-note';
            note.textContent = '\u26A0 ' + streamError +
              (fullText ? ' (response may be incomplete)' : '');
            content.parentNode.insertBefore(note, content.nextSibling);
          }
          // Record think time
          if (thinkStart) {
            thinkTimes.push(Date.now() - thinkStart);
            thinkStart = 0;
            const avg = thinkTimes.reduce((a, b) => a + b, 0) / thinkTimes.length;
            const el = $('s-think');
            if (el) el.textContent = (avg / 1000).toFixed(1) + 's';
          }
          faceOnDone(!!streamError);
          if (fullText) playTTS(fullText);
          if (statusLog.length > 0) addActivityLog(content, statusLog);
          if (ev.message_id && fullText) addFeedbackBar(content.closest('.bubble'), ev.message_id, fullText);
          if (pendingConfirm) {
            console.log('[confirm_tool] adding button for:', pendingConfirm.name);
            addConfirmBar(content.closest('.bubble'), pendingConfirm);
            pendingConfirm = null;
          } else {
            console.log('[confirm_tool] no pending confirm at done');
          }
          clearTimeout(streamTimeout);
          break;

        } else if (ev.type === 'error') {
          hideStatus(si);
          streamError = ev.text;
          if (!hasTokens) content.textContent = '\u26A0 ' + ev.text;
          clearTimeout(streamTimeout);
        }
      }
    }
  } catch (err) {
    clearTimeout(streamTimeout);
    hideStatus(si);
    if (err.name === 'AbortError' && _userStopped) {
      // Deliberate Stop \u2014 keep whatever was generated, no error styling.
      const partial = fullText.replace(FACE_TAG_RE, '').trim();
      if (partial) renderMarkdown(content, partial);
      else { const w = content.closest('.msg-wrap'); if (w) w.remove(); messageCount = Math.max(0, messageCount - 1); }
      faceOnDone(false);
    } else {
      const msg = err.name === 'AbortError' ? 'Response timed out (5 min). Try again or simplify your question.' : err.message;
      content.textContent = '\u26A0 ' + msg;
      faceOnDone(true);
    }
  }

  isStreaming       = false;
  _activeController  = null;
  _userStopped       = false;
  setSendStopMode(false);
  if (inputEl) inputEl.focus();
  // Refresh sidebar stats after each turn
  loadInfo();
}

// ── Activity log ─────────────────────────────────────────────────────────────

function addActivityLog(content, steps) {
  const toolCount = steps.filter(s => s.type === 'tool').length;
  const el = document.createElement('details');
  el.className = 'think-block activity-block';
  el.innerHTML = `<summary>${toolCount} action${toolCount !== 1 ? 's' : ''} taken</summary>` +
    steps.map(s => s.type === 'reason'
      ? `<div class="activity-reason">${esc(s.text)}</div>`
      : `<div class="activity-step">${esc(s.text)}</div>`
    ).join('');
  content.parentNode.insertBefore(el, content);
  scrollEnd();
}

// ── Feedback ─────────────────────────────────────────────────────────────────

function addFeedbackBar(bubble, messageId, fullText) {
  const bar = document.createElement('div');
  bar.className = 'feedback-bar';
  bar.innerHTML = `
    <button class="feedback-btn" title="Good response" data-v="1">\uD83D\uDC4D</button>
    <button class="feedback-btn" title="Bad response"  data-v="-1">\uD83D\uDC4E</button>
  `;
  bubble.appendChild(bar);

  bar.querySelectorAll('.feedback-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const v = parseInt(btn.dataset.v, 10);
      bar.querySelectorAll('.feedback-btn').forEach(b => b.classList.remove('active-up', 'active-down'));
      btn.classList.add(v === 1 ? 'active-up' : 'active-down');
      bar.classList.add('voted');
      submitFeedback(messageId, v, fullText);
    });
  });
}

async function submitFeedback(messageId, value, fullText) {
  try {
    await fetch('/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message_id: messageId,
        value,
        snippet: fullText.slice(0, 300),
      }),
    });
  } catch { /* non-critical */ }
}

// ── Confirm bar (tool approval) ──────────────────────────────────────────────

function addConfirmBar(bubble, confirm) {
  // For self-edits (persona.md), show the exact before/after diff so the user
  // reviews precisely what will be written before approving.
  if (confirm.diff) {
    const diffBox = document.createElement('details');
    diffBox.className = 'confirm-diff';
    diffBox.open = true;
    const lines = confirm.diff.split('\n').map(line => {
      let cls = 'diff-ctx';
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-del';
      else if (line.startsWith('@@')) cls = 'diff-hunk';
      return `<span class="${cls}">${esc(line)}</span>`;
    }).join('\n');
    diffBox.innerHTML = `<summary>Proposed change to persona.md</summary><pre class="diff-body">${lines}</pre>`;
    bubble.appendChild(diffBox);
  }

  const bar = document.createElement('div');
  bar.className = 'confirm-bar';
  bar.innerHTML = `
    <button class="confirm-btn confirm-go">Go ahead</button>
    <button class="confirm-btn confirm-skip">Skip</button>
  `;
  bubble.appendChild(bar);

  bar.querySelector('.confirm-go').addEventListener('click', () => {
    bar.remove();
    // Send "go ahead" as a new user message — triggers the pending tool
    if (inputEl) inputEl.value = 'go ahead';
    sendMessage();
  });
  bar.querySelector('.confirm-skip').addEventListener('click', () => {
    bar.remove();
  });
}

// ── Suggestion chips ─────────────────────────────────────────────────────────

function useSuggestion(el) {
  // Grab text from the label span (skips icon text in new Stitch layout)
  const label = el.querySelector('.font-headline') || el;
  if (inputEl) inputEl.value = label.textContent.trim();
  switchTab('chat');
  sendMessage();
}

// Bind suggestion click handlers (no inline onclick in new HTML)
document.querySelectorAll('.suggestion').forEach(chip => {
  chip.addEventListener('click', () => useSuggestion(chip));
});

// ── Clear chat / New chat ────────────────────────────────────────────────────

// ── Kai's opening greeting ─────────────────────────────────────────────────────
// Kai starts the conversation herself. Cold open (page load) uses her welcome-back
// note to pick up where things left off; New Chat gets a fresh clean-start line.
async function streamGreeting(fresh = false) {
  if (isStreaming) return;
  isStreaming = true;
  if (sendBtn) sendBtn.disabled = true;
  stopIdleBlink();
  setFace('waking');

  const { si, content } = addKaiBubble();
  let fullText = '', hasTokens = false;

  try {
    const resp = await fetch('/chat/greeting', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fresh }),
    });
    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer    = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === 'token') {
          if (!hasTokens) { hideStatus(si); hasTokens = true; stopWakingAnimation(); setFace('responding'); }
          fullText += ev.text;
          const cleanToken = parseFaceTags(ev.text);
          if (cleanToken) appendText(content, cleanToken);
        } else if (ev.type === 'done') {
          fullText = fullText.replace(FACE_TAG_RE, '');
          if (fullText) {
            renderMarkdown(content, fullText);
          } else {
            // Empty greeting — remove the bubble and fall back to the static welcome.
            const wrap = content.closest('.msg-wrap');
            if (wrap) wrap.remove();
            messageCount = Math.max(0, messageCount - 1);
            if (messageCount === 0 && welcomeEl) { messagesEl.appendChild(welcomeEl); welcomeEl.style.display = ''; }
          }
          faceOnDone(false);
          break;
        }
      }
    }
  } catch {
    const wrap = content.closest('.msg-wrap');
    if (wrap) wrap.remove();
    messageCount = Math.max(0, messageCount - 1);
  } finally {
    isStreaming = false;
    if (sendBtn) sendBtn.disabled = false;
    startIdleBlink();
  }
}

const newChatBtn = $('newChatBtn');
if (newChatBtn) {
  newChatBtn.addEventListener('click', async () => {
    await fetch('/clear', { method: 'POST' }).catch(() => {});
    if (messagesEl) messagesEl.innerHTML = '';
    if (welcomeEl) {
      messagesEl.appendChild(welcomeEl);
      welcomeEl.style.display = '';
    }
    messageCount = 0;
    switchTab('chat');
    streamGreeting(true);   // fresh clean-start greeting
  });
}

// ── New-capabilities awareness card ─────────────────────────────────────────────
// Tools auto-document into the memory tree and ride in the [TOOLS] block every turn;
// this surfaces only what's NEW since you last acknowledged — one namespace at a time.
// Descriptions come straight from the registry, so nothing here is model-generated.
async function showCapabilityCard() {
  let groups;
  try {
    const data = await fetch('/api/capabilities/new').then(r => r.json());
    groups = data.groups || [];
  } catch { return; }
  if (!groups.length || !messagesEl) return;

  hideWelcome();
  const card = document.createElement('div');
  card.className = 'cap-card';
  card.innerHTML = `
    <div class="cap-card-head">
      <span class="cap-card-title">New capabilities</span>
      <span class="cap-card-progress"></span>
      <button class="cap-card-x" title="Dismiss">&times;</button>
    </div>
    <div class="cap-card-ns"></div>
    <div class="cap-card-tools"></div>
    <div class="cap-card-foot"><button class="cap-card-next"></button></div>
  `;
  messagesEl.insertBefore(card, messagesEl.firstChild);

  const progressEl = card.querySelector('.cap-card-progress');
  const nsEl       = card.querySelector('.cap-card-ns');
  const toolsEl    = card.querySelector('.cap-card-tools');
  const nextBtn    = card.querySelector('.cap-card-next');
  const xBtn       = card.querySelector('.cap-card-x');

  let i = 0;
  function render() {
    const g = groups[i];
    progressEl.textContent = `${i + 1} / ${groups.length}`;
    nsEl.innerHTML = `<code>${_esc(g.namespace)}</code>`;
    toolsEl.innerHTML = g.tools.map(t =>
      `<div class="cap-card-tool"><b>${_esc(t.tool)}</b> — <span>${_esc(t.description || 'No description.')}</span></div>`
    ).join('');
    nextBtn.textContent = (i === groups.length - 1) ? 'Got it' : 'Next';
  }
  async function dismiss() {
    await fetch('/api/capabilities/ack', { method: 'POST' }).catch(() => {});
    card.remove();
  }
  nextBtn.addEventListener('click', () => {
    if (i < groups.length - 1) { i++; render(); }
    else dismiss();
  });
  xBtn.addEventListener('click', dismiss);
  render();
}

// ── Input handling ───────────────────────────────────────────────────────────

if (inputEl) {
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      _primeAudio();
      sendMessage();
    }
  });

  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + 'px';
  });
}

if (sendBtn) {
  sendBtn.addEventListener('click', () => {
    _primeAudio();
    if (isStreaming) stopStreaming();
    else sendMessage();
  });
}

// ── 7. Voice (STT + TTS) ─────────────────────────────────────────────────────

// ── Mic recorder using MediaRecorder API ─────────────────────────────────────
// Records compressed audio (webm/opus) — ffmpeg converts server-side before Whisper.
class MicRecorder {
  constructor() {
    this._stream   = null;
    this._recorder = null;
    this._chunks   = [];
    this._mimeType = '';
  }

  async start() {
    this._chunks = [];
    this._stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    this._mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm']
      .find(t => MediaRecorder.isTypeSupported(t)) || '';
    this._recorder = new MediaRecorder(this._stream, this._mimeType ? { mimeType: this._mimeType } : {});
    this._recorder.ondataavailable = (e) => { if (e.data.size > 0) this._chunks.push(e.data); };
    this._recorder.start();
  }

  stop() {
    return new Promise((resolve) => {
      if (!this._recorder) { resolve(new Blob([], { type: 'audio/webm' })); return; }
      this._recorder.onstop = () => {
        const blob = new Blob(this._chunks, { type: this._recorder.mimeType || 'audio/webm' });
        this._chunks = [];
        resolve(blob);
      };
      this._recorder.stop();
      if (this._stream) { this._stream.getTracks().forEach(t => t.stop()); this._stream = null; }
    });
  }
}

// ── State ──────────────────────────────────────────────────────────────────────
const micBtn   = $('micBtn');
const micIcon  = $('micIcon');
const ttsBtn    = $('ttsBtn');
const ttsVoiceEl = $('ttsVoice');
const _recorder = new MicRecorder();
let _micActive  = false;   // tap-to-toggle state
let _pttActive  = false;   // push-to-talk state
let _ttsEnabled = localStorage.getItem('kai_tts') !== 'false';  // default on
let _ttsVoice   = localStorage.getItem('kai_tts_voice') || 'am_onyx';
let _ttsSources = [];      // queued/playing AudioBufferSourceNodes for the current reply
let _ttsNextStart = 0;     // ctx.currentTime offset for gapless chunk scheduling
let _ttsCtx     = null;    // shared AudioContext for TTS playback

function _getTtsCtx() {
  if (!_ttsCtx || _ttsCtx.state === 'closed') _ttsCtx = new AudioContext();
  if (_ttsCtx.state === 'suspended') _ttsCtx.resume();
  return _ttsCtx;
}

// Call on any user gesture so the AudioContext is already running before TTS fires.
function _primeAudio() { _getTtsCtx(); }

function _applyTtsState() {
  if (!ttsBtn) return;
  if (_ttsEnabled) {
    ttsBtn.classList.add('tts-on');
    ttsBtn.title = 'Voice output ON — click to disable';
  } else {
    ttsBtn.classList.remove('tts-on');
    ttsBtn.title = 'Voice output OFF — click to enable';
  }
}
_applyTtsState();

if (ttsBtn) {
  ttsBtn.addEventListener('click', () => {
    _ttsEnabled = !_ttsEnabled;
    localStorage.setItem('kai_tts', _ttsEnabled);
    _applyTtsState();
    if (!_ttsEnabled) _stopTts();
  });
}

// ── Voice picker — populate from /voice/voices, remember the choice ──────────
async function _loadTtsVoices() {
  if (!ttsVoiceEl) return;
  try {
    const r = await fetch('/voice/voices');
    const { voices } = await r.json();
    if (!voices || !voices.length) { ttsVoiceEl.innerHTML = '<option value="">(no voices)</option>'; return; }
    ttsVoiceEl.innerHTML = voices.map(v => `<option value="${v}">${v}</option>`).join('');
    ttsVoiceEl.value = voices.includes(_ttsVoice) ? _ttsVoice : voices[0];
    _ttsVoice = ttsVoiceEl.value;
  } catch (e) {
    console.error('[voice] failed to load voice list:', e);
  }
}
_loadTtsVoices();

if (ttsVoiceEl) {
  ttsVoiceEl.addEventListener('change', () => {
    _ttsVoice = ttsVoiceEl.value;
    localStorage.setItem('kai_tts_voice', _ttsVoice);
  });
}

// ── Transcription: send audio blob → get text → fill input ───────────────────
async function _transcribeAndFill(audioBlob) {
  if (!micBtn) return;
  micBtn.classList.remove('recording');
  micBtn.classList.add('processing');
  micIcon.textContent = 'hourglass_empty';

  try {
    const resp = await fetch('/voice/transcribe', {
      method: 'POST',
      body: audioBlob,
      headers: { 'Content-Type': audioBlob.type || 'audio/webm' },
    });
    const { text } = await resp.json();
    if (text && text.trim()) {
      const el = $('input');
      if (el) {
        el.value = text.trim();
        el.style.height = Math.min(el.scrollHeight, 180) + 'px';
        // Auto-send
        sendMessage();
      }
    }
  } catch (e) {
    console.error('[voice] transcribe error:', e);
  } finally {
    micBtn.classList.remove('processing');
    micIcon.textContent = 'mic';
  }
}

// ── TTS: play Kai's response ───────────────────────────────────────────────────
//
// Kokoro reads raw text — markdown syntax like "**Architecture:**" garbles its
// phonemizer (literal asterisks/headers/code fences slur into nonsense like
// "Ascurus"). Strip formatting down to clean prose before it ever reaches TTS;
// the visible bubble keeps its markdown rendering untouched.
function stripMarkdownForSpeech(text) {
  return text
    .replace(/```[\s\S]*?```/g, ' ')        // fenced code blocks
    .replace(/`([^`]+)`/g, '$1')            // inline code
    .replace(/^#{1,6}\s+/gm, '')            // headers
    .replace(/\*\*\*([^*]+)\*\*\*/g, '$1')  // bold+italic
    .replace(/\*\*([^*]+)\*\*/g, '$1')      // bold
    .replace(/\*([^*]+)\*/g, '$1')          // italic (asterisk)
    .replace(/__([^_]+)__/g, '$1')          // bold (underscore)
    .replace(/_([^_]+)_/g, '$1')            // italic (underscore)
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // links — keep label, drop URL
    .replace(/^\s*[-*+]\s+/gm, '')          // bullet list markers
    .replace(/^\s*\d+\.\s+/gm, '')          // numbered list markers
    .replace(/^>\s?/gm, '')                 // blockquote markers
    .trim();
}

let _audioPipelineTested = false;
async function _testAudioPipeline() {
  if (_audioPipelineTested) return;
  _audioPipelineTested = true;
  try {
    const r = await fetch('/voice/test');
    if (!r.ok) { console.error('[tts] pipeline test: server error', r.status); return; }
    const ab = await r.arrayBuffer();
    const ctx = _getTtsCtx();
    if (ctx.state === 'suspended') await ctx.resume();
    const buf = await ctx.decodeAudioData(ab);
    const src = ctx.createBufferSource();
    src.buffer = buf; src.connect(ctx.destination); src.start(0);
    console.log('[tts] pipeline test OK — you should hear a 1s beep');
  } catch (e) {
    console.error('[tts] pipeline test FAILED:', e);
  }
}

// Stop and clear any in-flight/queued TTS playback for the current reply.
function _stopTts() {
  for (const src of _ttsSources) { try { src.stop(); } catch (_) {} }
  _ttsSources = [];
  _ttsNextStart = 0;
}

// Schedule a decoded chunk to play immediately after whatever's already queued,
// so back-to-back sentence chunks sound like one continuous utterance.
function _scheduleTtsChunk(ctx, audioBuf) {
  const src = ctx.createBufferSource();
  src.buffer = audioBuf;
  src.connect(ctx.destination);
  const startAt = Math.max(_ttsNextStart, ctx.currentTime);
  src.start(startAt);
  _ttsNextStart = startAt + audioBuf.duration;
  _ttsSources.push(src);
  src.onended = () => { _ttsSources = _ttsSources.filter(s => s !== src); };
}

// The server streams the reply as repeated [4-byte big-endian length][WAV bytes]
// frames — one per sentence — so we can start playing the first sentence while
// later ones are still being synthesized, instead of waiting on the whole reply.
async function playTTS(text) {
  if (!_ttsEnabled || !text || !text.trim()) return;
  const speech = stripMarkdownForSpeech(text);
  if (!speech) return;

  _stopTts();

  try {
    const resp = await fetch('/voice/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: speech, voice: _ttsVoice }),
    });
    if (!resp.ok || !resp.body) return;

    const ctx = _getTtsCtx();
    if (ctx.state === 'suspended') await ctx.resume();
    _ttsNextStart = ctx.currentTime;

    const reader = resp.body.getReader();
    let buffered = new Uint8Array(0);

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value && value.length) {
        const merged = new Uint8Array(buffered.length + value.length);
        merged.set(buffered);
        merged.set(value, buffered.length);
        buffered = merged;
      }

      while (buffered.length >= 4) {
        const len = new DataView(buffered.buffer, buffered.byteOffset, 4).getUint32(0, false);
        if (buffered.length < 4 + len) break;
        const wavBytes = buffered.slice(4, 4 + len);   // copy → own ArrayBuffer
        buffered = buffered.slice(4 + len);
        try {
          const audioBuf = await ctx.decodeAudioData(wavBytes.buffer);
          _scheduleTtsChunk(ctx, audioBuf);
        } catch (e) {
          console.error('[voice] TTS chunk decode error:', e);
        }
      }
    }
  } catch (e) {
    console.error('[voice] TTS error:', e);
  }
}

// ── Mic button — tap-to-toggle ────────────────────────────────────────────────
if (micBtn) {
  micBtn.addEventListener('click', async (e) => {
    _primeAudio();  // prime AudioContext while we have a user gesture
    if (_pttActive) return;  // PTT takes priority
    if (_micActive) {
      // Stop
      _micActive = false;
      const blob = await _recorder.stop();
      await _transcribeAndFill(blob);
    } else {
      // Start
      try {
        await _recorder.start();
        _micActive = true;
        micBtn.classList.add('recording');
        micIcon.textContent = 'stop';
      } catch (err) {
        console.error('[voice] mic start error:', err);
        alert('Microphone access denied or unavailable.');
      }
    }
  });

  // Push-to-talk — hold the button
  const _pttStart = async (e) => {
    if (_micActive) return;  // tap-to-toggle is active, ignore PTT
    e.preventDefault();
    try {
      await _recorder.start();
      _pttActive = true;
      micBtn.classList.add('recording');
      micIcon.textContent = 'stop';
    } catch (err) {
      console.error('[voice] PTT start error:', err);
    }
  };

  const _pttStop = async (e) => {
    if (!_pttActive) return;
    _pttActive = false;
    const blob = await _recorder.stop();
    await _transcribeAndFill(blob);
  };

  micBtn.addEventListener('mousedown',  _pttStart);
  micBtn.addEventListener('mouseup',    _pttStop);
  micBtn.addEventListener('mouseleave', _pttStop);
  micBtn.addEventListener('touchstart', _pttStart, { passive: false });
  micBtn.addEventListener('touchend',   _pttStop);
}

// ── Theme ─────────────────────────────────────────────────────────────────────

const THEMES = ['nebula', 'terminal', 'slate'];

function _applyTheme(name) {
  if (!THEMES.includes(name)) name = 'nebula';
  document.documentElement.setAttribute('data-theme', name);
  document.querySelectorAll('.theme-swatch').forEach(el => {
    el.classList.toggle('active', el.dataset.themePick === name);
  });
  localStorage.setItem('kai_theme', name);
}

// Apply saved theme immediately (before paint)
_applyTheme(localStorage.getItem('kai_theme') || 'nebula');

document.querySelectorAll('.theme-swatch').forEach(el => {
  el.addEventListener('click', () => _applyTheme(el.dataset.themePick));
});

// ── 8. Response Mode ─────────────────────────────────────────────────────────

async function loadMode() {
  try {
    const d = await fetch('/settings/mode').then(r => r.json());
    _applyMode(d.mode, d.label);
  } catch { /* keep default */ }
}

function _applyMode(mode, label) {
  const labelEl = $('mode-label');
  if (labelEl) labelEl.textContent = label;
  document.querySelectorAll('.mode-option').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
}

function toggleModeDropdown() {
  const pill     = $('mode-pill');
  const dropdown = $('mode-dropdown');
  if (!pill || !dropdown) return;
  const isOpen   = dropdown.classList.contains('open');
  pill.classList.toggle('open', !isOpen);
  dropdown.classList.toggle('open', !isOpen);
}

async function setMode(mode) {
  const labels = {
    short: 'Short answers', long: 'Long answers',
    chat: 'Just chatting', research: 'Research',
  };
  const pill     = $('mode-pill');
  const dropdown = $('mode-dropdown');
  if (pill)     pill.classList.remove('open');
  if (dropdown) dropdown.classList.remove('open');

  try {
    await fetch('/settings/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    _applyMode(mode, labels[mode]);
  } catch { /* ignore */ }
}

// Mode option buttons
document.querySelectorAll('.mode-option').forEach(btn => {
  btn.addEventListener('click', () => setMode(btn.dataset.mode));
});

// Close mode dropdown when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('.mode-selector-wrap')) {
    const pill     = $('mode-pill');
    const dropdown = $('mode-dropdown');
    if (pill)     pill.classList.remove('open');
    if (dropdown) dropdown.classList.remove('open');
  }
});

// ── 8. Generation Mode (thinking + temperature) ───────────────────────────────

let _presets       = [];          // [{key,label,think,temp,default_temp}]
let _activePreset  = 'normal';
const PRESET_LABELS = { thinking: 'Thinking', normal: 'Normal', creative: 'Creative', crazy: 'Crazy' };

async function loadPreset() {
  try {
    const d = await fetch('/settings/preset').then(r => r.json());
    _presets = d.presets || [];
    _applyPreset(d.preset, d.temperature);
  } catch { /* keep default */ }
}

function _applyPreset(key, temp) {
  _activePreset = key;
  const labelEl = $('preset-label');
  if (labelEl) labelEl.textContent = PRESET_LABELS[key] || key;
  document.querySelectorAll('.preset-option').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.preset === key);
  });
  if (typeof temp === 'number') _setTempUI(temp);
}

function _setTempUI(temp) {
  const slider = $('temp-slider');
  const val    = $('temp-value');
  if (slider) slider.value = temp;
  if (val)    val.textContent = Number(temp).toFixed(2);
}

function togglePresetDropdown() {
  const pill = $('preset-pill'), dd = $('preset-dropdown');
  if (!pill || !dd) return;
  const open = dd.classList.contains('open');
  pill.classList.toggle('open', !open);
  dd.classList.toggle('open', !open);
}

async function setPreset(key) {
  const pill = $('preset-pill'), dd = $('preset-dropdown');
  if (pill) pill.classList.remove('open');
  if (dd)   dd.classList.remove('open');
  try {
    const d = await fetch('/settings/preset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset: key }),
    }).then(r => r.json());
    _applyPreset(d.preset ?? key, d.temp);
  } catch { /* ignore */ }
}

document.querySelectorAll('.preset-option').forEach(btn => {
  btn.addEventListener('click', () => setPreset(btn.dataset.preset));
});

// Close preset dropdown when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('#preset-pill') && !e.target.closest('#preset-dropdown')) {
    const pill = $('preset-pill'), dd = $('preset-dropdown');
    if (pill) pill.classList.remove('open');
    if (dd)   dd.classList.remove('open');
  }
});

// ── Tool model level — which model runs tool-call rounds ─────────────────────
let _toolLevels = [];   // [{key,label,model,blurb,installed}]

async function loadToolLevel() {
  try {
    const d = await fetch('/settings/tool-level').then(r => r.json());
    _toolLevels = d.levels || [];
    _renderToolLevels(d.level);
  } catch { /* keep default */ }
}

function _renderToolLevels(activeKey) {
  const dd = $('toollevel-dropdown');
  if (!dd) return;
  dd.innerHTML = '';
  for (const lv of _toolLevels) {
    const btn = document.createElement('button');
    btn.className = 'mode-option' + (lv.key === activeKey ? ' active' : '');
    const strong = document.createElement('strong');
    strong.textContent = lv.label;
    const desc = document.createElement('span');
    desc.className = 'mode-option-desc';
    desc.textContent = lv.installed
      ? lv.blurb
      : `not installed — run: ollama pull ${lv.model}`;
    btn.append(strong, desc);
    btn.addEventListener('click', () => setToolLevel(lv.key));
    dd.appendChild(btn);
  }
  const labelEl = $('toollevel-label');
  const active  = _toolLevels.find(l => l.key === activeKey);
  if (labelEl && active) labelEl.textContent = active.label;
}

function toggleToolLevelDropdown() {
  const pill = $('toollevel-pill'), dd = $('toollevel-dropdown');
  if (!pill || !dd) return;
  const open = dd.classList.contains('open');
  pill.classList.toggle('open', !open);
  dd.classList.toggle('open', !open);
}

async function setToolLevel(key) {
  const pill = $('toollevel-pill'), dd = $('toollevel-dropdown');
  if (pill) pill.classList.remove('open');
  if (dd)   dd.classList.remove('open');
  try {
    const d = await fetch('/settings/tool-level', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level: key }),
    }).then(r => r.json());
    if (d.levels) _toolLevels = d.levels;
    _renderToolLevels(d.key ?? key);
  } catch { /* ignore */ }
}

// Close tool-level dropdown when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('#toollevel-pill') && !e.target.closest('#toollevel-dropdown')) {
    const pill = $('toollevel-pill'), dd = $('toollevel-dropdown');
    if (pill) pill.classList.remove('open');
    if (dd)   dd.classList.remove('open');
  }
});

// Temperature slider — live, this thread only (debounced POST)
let _tempTimer = null;
(function initTempSlider() {
  const slider = $('temp-slider');
  if (!slider) return;
  slider.addEventListener('input', () => {
    const t   = parseFloat(slider.value);
    const val = $('temp-value');
    if (val) val.textContent = t.toFixed(2);
    clearTimeout(_tempTimer);
    _tempTimer = setTimeout(() => {
      fetch('/settings/temperature', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ temperature: t }),
      }).catch(() => {});
    }, 250);
  });
})();

// Advanced — edit + save each preset's temperature (persisted per user)
function togglePresetAdvanced() {
  const adv = $('preset-advanced');
  if (!adv) return;
  const showing = adv.style.display !== 'none';
  adv.style.display = showing ? 'none' : 'block';
  if (!showing) _fillAdvancedInputs();
}

function _fillAdvancedInputs() {
  _presets.forEach(p => {
    const inp = $('adv-' + p.key);
    if (inp) inp.value = Number(p.temp).toFixed(2);
  });
}

async function savePresetTemps() {
  const temps = {};
  ['thinking', 'normal', 'creative', 'crazy'].forEach(k => {
    const inp = $('adv-' + k);
    if (inp && inp.value !== '') temps[k] = parseFloat(inp.value);
  });
  try {
    const d = await fetch('/settings/preset-temps', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ temps }),
    }).then(r => r.json());
    _presets = d.presets || _presets;
    const active = _presets.find(p => p.key === _activePreset);
    if (active) _setTempUI(active.temp);
    const adv = $('preset-advanced');
    if (adv) adv.style.display = 'none';
  } catch { /* ignore */ }
}

// ── 8b. Model Manager ────────────────────────────────────────────────────────

let _models = [];

async function loadModels() {
  try {
    const d = await fetch('/settings/models').then(r => r.json());
    _models = d.models || [];
    renderModelList();
  } catch { /* ignore */ }
}

function renderModelList() {
  const el = $('model-list');
  if (!el) return;
  if (!_models.length) { el.innerHTML = '<div class="empty-hint">No models configured</div>'; return; }

  el.innerHTML = _models.map(m => {
    const active  = m.active ? ' model-active' : '';
    const builtin = m.builtin ? ' model-builtin' : '';
    const think   = m.think ? '<span class="model-tag">think</span>' : '';
    const badge   = m.active ? '<span class="model-tag model-tag-active">active</span>' : '';
    const del     = m.builtin ? '' : `<button class="model-del" data-name="${esc(m.name)}">&times;</button>`;
    return `<div class="model-row${active}${builtin}" data-name="${esc(m.name)}">
      <div class="model-row-main">
        <span class="model-name">${esc(m.name)}</span>
        <span class="model-id">${esc(m.ollama_id)}</span>
        ${think}${badge}
      </div>
      <div class="model-row-actions">${del}</div>
    </div>`;
  }).join('');

  // Click row to activate
  el.querySelectorAll('.model-row').forEach(row => {
    row.addEventListener('click', async (e) => {
      if (e.target.closest('.model-del')) return;
      const name = row.dataset.name;
      try {
        await fetch('/settings/models/active', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        await loadModels();
        loadInfo();
      } catch { /* ignore */ }
    });
  });

  // Delete buttons
  el.querySelectorAll('.model-del').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const name = btn.dataset.name;
      if (!confirm(`Remove model "${name}"?`)) return;
      try {
        await fetch(`/settings/models/${encodeURIComponent(name)}`, { method: 'DELETE' });
        await loadModels();
      } catch { /* ignore */ }
    });
  });
}

// Add-model form
(function initModelForm() {
  const addBtn    = $('model-add-btn');
  const form      = $('model-add-form');
  const cancelBtn = $('model-cancel-btn');
  const saveBtn   = $('model-save-btn');
  const selectEl  = $('model-add-ollama');

  if (!addBtn || !form) return;

  addBtn.addEventListener('click', async () => {
    form.style.display = form.style.display === 'none' ? '' : 'none';
    if (form.style.display !== 'none' && selectEl) {
      // Populate dropdown with installed Ollama models
      try {
        const d = await fetch('/settings/models/available').then(r => r.json());
        const models = d.models || [];
        selectEl.innerHTML = '<option value="">Select an Ollama model...</option>' +
          models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
      } catch {
        selectEl.innerHTML = '<option value="">Could not load models</option>';
      }
    }
  });

  if (cancelBtn) cancelBtn.addEventListener('click', () => {
    form.style.display = 'none';
    $('model-add-name').value = '';
    $('model-add-think').checked = false;
  });

  if (saveBtn) saveBtn.addEventListener('click', async () => {
    const ollama_id = selectEl ? selectEl.value : '';
    const name      = ($('model-add-name') || {}).value?.trim() || '';
    const think     = ($('model-add-think') || {}).checked || false;

    if (!ollama_id) { alert('Pick a model from the dropdown'); return; }
    if (!name)      { alert('Give it a friendly name'); return; }

    try {
      const resp = await fetch('/settings/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, ollama_id, think }),
      });
      if (!resp.ok) {
        const err = await resp.json();
        alert(err.detail || 'Failed to add model');
        return;
      }
      form.style.display = 'none';
      $('model-add-name').value = '';
      $('model-add-think').checked = false;
      await loadModels();
    } catch { alert('Failed to save model'); }
  });
})();

// ── 9. Memory Browser ────────────────────────────────────────────────────────

let _memFacts     = [];
let _delPending   = null;
let _activeMemTab = 'facts';

function openMemoryPanel() {
  const panel = $('memory-panel');
  if (panel) panel.classList.add('open');
  const filterEl = $('mem-filter');
  if (filterEl) filterEl.value = '';
  switchMemTab(_activeMemTab);
}

function closeMemoryPanel() {
  const panel = $('memory-panel');
  if (panel) panel.classList.remove('open');
  _delPending = null;
}

function switchMemTab(tab) {
  _activeMemTab = tab;
  // Support both old id-based tabs and new data-mem-tab tabs
  document.querySelectorAll('.mem-tab, .overlay-tab-btn[data-mem-tab]').forEach(b => {
    const tabId = b.id === 'tab-' + tab || b.dataset.memTab === tab;
    b.classList.toggle('active', tabId);
  });
  const filterEl = $('mem-filter');
  if (filterEl) filterEl.style.display = tab === 'facts' ? '' : 'none';
  if (tab === 'facts') {
    loadMemoryFacts();
  } else {
    loadMemorySummaries();
  }
}

// Memory tab button handlers (overlay slide-over — legacy)
document.querySelectorAll('.overlay-tab-btn[data-mem-tab]').forEach(btn => {
  btn.addEventListener('click', () => switchMemTab(btn.dataset.memTab));
});

// Memory browser panel tab buttons (new full panel)
document.querySelectorAll('.mem-tab[data-memtab]').forEach(btn => {
  btn.addEventListener('click', () => {
    _memLoaded[btn.dataset.memtab] = false; // force reload on explicit tab click
    loadMemoryBrowser(btn.dataset.memtab);
  });
});

// Memory search input
const memSearchEl = $('mem-search');
if (memSearchEl) {
  let _memSearchTimer = null;
  memSearchEl.addEventListener('input', () => {
    clearTimeout(_memSearchTimer);
    _memSearchTimer = setTimeout(() => {
      const q = memSearchEl.value.trim();
      _memLoaded.facts = false;
      _loadMemFacts(q || undefined);
    }, 350);
  });
}

// Goals banner dismiss
const goalsBannerClose = $('goals-banner-dismiss');
if (goalsBannerClose) {
  goalsBannerClose.addEventListener('click', () => {
    const banner = $('goals-banner');
    if (banner) banner.style.display = 'none';
  });
}

// Goals banner "view" link → switch to memory goals tab
const goalsBannerLink = $('goals-banner-view');
if (goalsBannerLink) {
  goalsBannerLink.addEventListener('click', (e) => {
    e.preventDefault();
    switchTab('memory');
    loadMemoryBrowser('goals');
  });
}

// Dashboard "open memory browser" button
const dashOpenMemory = $('dash-open-memory');
if (dashOpenMemory) {
  dashOpenMemory.addEventListener('click', () => switchTab('memory'));
}

// Dashboard node refresh button
const dashNodesRefresh = $('dash-nodes-refresh');
if (dashNodesRefresh) {
  dashNodesRefresh.addEventListener('click', () => {
    // Force reload cluster nodes
    fetch('/api/cluster/nodes').then(r => r.json()).then(nodes => {
      const container = $('dash-nodes');
      if (!container || !nodes.length) return;
      const now = Date.now() / 1000;
      const hostRow = container.querySelector('.dash-node-row');
      const rows = nodes.map(n => {
        const online = n.last_seen && (now - n.last_seen) < 120;
        const cls = online ? 'online' : 'offline';
        const label = online ? 'online' : _timeAgo(n.last_seen);
        return `<div class="dash-node-row"><span class="dash-dot ${cls}"></span><span style="font-size:13px;color:var(--text)">${_esc(n.label)}</span><span class="dash-node-label">${label}</span></div>`;
      }).join('');
      container.innerHTML = (hostRow ? hostRow.outerHTML : '') + rows;
    }).catch(() => {});
  });
}

// Container panel: refresh button + delegated start/stop/delete actions
const dashContainersRefresh = $('dash-containers-refresh');
if (dashContainersRefresh) {
  dashContainersRefresh.addEventListener('click', loadContainers);
}
const dashContainersEl = $('dash-containers');
if (dashContainersEl) {
  dashContainersEl.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-ct-action]');
    if (!btn) return;
    _containerAction(btn.dataset.ctName, btn.dataset.ctAction, btn);
  });
}

// Developer mode: settings toggle + dashboard stats refresh
const devToggleBtn = $('dev-toggle');
if (devToggleBtn) {
  devToggleBtn.addEventListener('click', () => {
    _devMode = !_devMode;
    localStorage.setItem('kai_dev_mode', _devMode ? 'true' : 'false');
    applyDevMode();
    if (_devMode) loadDevStats();
  });
}
const devRefreshBtn = $('dash-dev-refresh');
if (devRefreshBtn) {
  devRefreshBtn.addEventListener('click', loadDevStats);
}
applyDevMode();

async function loadMemoryFacts() {
  const listEl = $('mem-list');
  if (!listEl) return;
  listEl.innerHTML = '<div class="mem-empty">Loading\u2026</div>';
  try {
    _memFacts = await fetch('/memory/facts').then(r => r.json());
    renderMemoryFacts(_memFacts);
  } catch {
    listEl.innerHTML = '<div class="mem-empty">Failed to load facts.</div>';
  }
}

async function loadMemorySummaries() {
  const listEl = $('mem-list');
  if (!listEl) return;
  listEl.innerHTML = '<div class="mem-empty">Loading\u2026</div>';
  try {
    const entries = await fetch('/memory/episodic').then(r => r.json());
    if (!entries.length) {
      listEl.innerHTML = '<div class="mem-empty">No summaries yet. Summaries are created every ' +
        '4 turns or when you clear the chat.</div>';
      return;
    }
    listEl.innerHTML = entries.map(e => {
      const typeColor = e.entry_type === 'summary' ? 'var(--accent)' : 'var(--muted)';
      const typeLabel = e.entry_type === 'turn' ? 'turn (unsummarized)' : e.entry_type;
      return `
        <div class="mem-row" style="align-items:flex-start;gap:8px">
          <div style="min-width:90px;font-size:0.72em;color:var(--muted);padding-top:2px;flex-shrink:0">
            ${esc(e.timestamp)}<br>
            <span style="color:${typeColor}">${esc(typeLabel)}</span>
          </div>
          <div style="font-size:0.82em;color:var(--text);line-height:1.5;flex:1">${esc(e.content)}</div>
        </div>`;
    }).join('');
  } catch {
    listEl.innerHTML = '<div class="mem-empty">Failed to load summaries.</div>';
  }
}

function filterMemory(query) {
  const q = query.toLowerCase();
  const filtered = q
    ? _memFacts.filter(f => f.key.toLowerCase().includes(q) || f.value.toLowerCase().includes(q))
    : _memFacts;
  renderMemoryFacts(filtered);
}

// Filter input handler
const memFilterEl = $('mem-filter');
if (memFilterEl) {
  memFilterEl.addEventListener('input', () => filterMemory(memFilterEl.value));
}

function renderMemoryFacts(facts) {
  const listEl = $('mem-list');
  if (!listEl) return;
  if (!facts.length) {
    listEl.innerHTML = '<div class="mem-empty">No facts found.</div>';
    return;
  }
  listEl.innerHTML = facts.map(f => `
    <div class="mem-row" data-key="${esc(f.key)}">
      <div class="mem-key" title="${esc(f.key)}">${esc(f.key)}</div>
      <div class="mem-val-wrap">
        <div class="mem-val" title="Click to edit">${esc(f.value)}</div>
      </div>
      <div class="mem-src">${esc(f.source)}</div>
      <div class="mem-actions">
        <button class="btn-mem-del" data-key="${esc(f.key)}" title="Delete">\u2715</button>
      </div>
    </div>
  `).join('');

  // Bind click handlers for edit and delete
  listEl.querySelectorAll('.mem-val').forEach(valEl => {
    valEl.addEventListener('click', () => {
      const key = valEl.closest('.mem-row').dataset.key;
      startEditFact(key, valEl);
    });
  });
  listEl.querySelectorAll('.btn-mem-del').forEach(btn => {
    btn.addEventListener('click', () => deleteFact(btn));
  });
}

function startEditFact(key, valEl) {
  const wrap    = valEl.parentElement;
  const current = valEl.textContent;
  const input   = document.createElement('input');
  input.className = 'mem-val-input';
  input.value     = current;
  wrap.innerHTML  = '';
  wrap.appendChild(input);
  input.focus();
  input.select();

  const commit = async () => {
    const newVal = input.value.trim();
    if (newVal && newVal !== current) {
      try {
        await fetch(`/memory/facts/${encodeURIComponent(key)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: newVal }),
        });
        const fact = _memFacts.find(f => f.key === key);
        if (fact) fact.value = newVal;
      } catch { /* restore on error */ }
    }
    const displayVal = newVal || current;
    wrap.innerHTML = `<div class="mem-val" title="Click to edit">${esc(displayVal)}</div>`;
    // Rebind click
    wrap.querySelector('.mem-val').addEventListener('click', function() {
      startEditFact(key, this);
    });
    loadInfo();
  };

  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { input.value = current; input.blur(); }
  });
}

async function deleteFact(btn) {
  const key = btn.dataset.key;
  // Two-click confirm
  if (_delPending !== key) {
    if (_delPending) {
      const prev = $('mem-list').querySelector('.btn-mem-del.confirming');
      if (prev) prev.classList.remove('confirming');
    }
    _delPending = key;
    btn.classList.add('confirming');
    btn.title = 'Click again to confirm';
    return;
  }
  // Confirmed
  _delPending = null;
  try {
    await fetch(`/memory/facts/${encodeURIComponent(key)}`, { method: 'DELETE' });
    _memFacts = _memFacts.filter(f => f.key !== key);
    const q = $('mem-filter');
    filterMemory(q ? q.value : '');
    const badge = $('s-fact-count');
    if (badge) badge.textContent = _memFacts.length;
    loadInfo();
  } catch {
    btn.classList.remove('confirming');
  }
}

// ── 10. Session History ──────────────────────────────────────────────────────

let _sessions      = [];
let _activeSession = null;

function openHistoryPanel() {
  const panel = $('history-panel');
  if (panel) panel.classList.add('open');
  loadSessions();
}

function closeHistoryPanel() {
  const panel = $('history-panel');
  if (panel) panel.classList.remove('open');
}

async function loadSessions() {
  try {
    const res = await fetch('/sessions');
    _sessions = await res.json();
    renderSessionList($('hist-list'), _sessions);
    renderRecentSessions(_sessions.slice(0, 3));
    // Also update dashboard recent sessions
    renderDashRecentSessions(_sessions.slice(0, 5));
  } catch {
    const histList = $('hist-list');
    if (histList) histList.innerHTML = '<div class="mem-empty">Could not load history.</div>';
  }
}

function renderRecentSessions(sessions) {
  const el = $('s-recent-sessions');
  if (!el) return;
  if (!sessions.length) {
    el.innerHTML = '<div class="info-row"><span class="info-key">\u2014</span><span class="info-val">no sessions yet</span></div>';
    return;
  }
  el.innerHTML = sessions.map(s => {
    const date = new Date(s.last_active).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    return `<div class="info-row sidebar-session-row" data-sid="${esc(s.id)}" style="cursor:pointer">
      <span class="info-key" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:110px" title="${esc(s.title)}">${esc(s.title)}</span>
      <span class="info-val">${date}</span>
    </div>`;
  }).join('');
  // Bind click handlers
  el.querySelectorAll('.sidebar-session-row').forEach(row => {
    row.addEventListener('click', () => loadSessionIntoChat(row.dataset.sid));
  });
}

function renderDashRecentSessions(sessions) {
  const el = $('dash-recent-sessions');
  if (!el) return;
  if (!sessions.length) {
    el.textContent = 'No recent sessions';
    return;
  }
  el.innerHTML = sessions.map(s => {
    const date = new Date(s.last_active).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    return `<div class="hist-item dash-session-row" data-sid="${esc(s.id)}">
      <div class="hist-item-body">
        <div class="hist-title">${esc(s.title)}</div>
        <div class="hist-meta">${date} \u00B7 ${s.message_count} messages</div>
      </div>
    </div>`;
  }).join('');
  el.querySelectorAll('.dash-session-row').forEach(row => {
    row.addEventListener('click', () => loadSessionIntoChat(row.dataset.sid));
  });
}

function renderSessionList(listEl, sessions) {
  if (!listEl) return;
  if (!sessions.length) {
    listEl.innerHTML = '<div class="mem-empty">No past sessions yet.</div>';
    return;
  }
  listEl.innerHTML = sessions.map(s => {
    const date   = new Date(s.last_active).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    const active = s.id === _activeSession ? ' active' : '';
    return `<div class="hist-item${active}" data-sid="${esc(s.id)}">
      <div class="hist-item-body">
        <div class="hist-title">${esc(s.title)}</div>
        <div class="hist-meta">${date} \u00B7 ${s.message_count} messages</div>
      </div>
    </div>`;
  }).join('');
  // Bind click handlers
  listEl.querySelectorAll('.hist-item').forEach(item => {
    item.addEventListener('click', () => loadSessionIntoChat(item.dataset.sid));
  });
}

async function loadSessionIntoChat(sessionId) {
  try {
    // Restore session on backend
    await fetch(`/sessions/${encodeURIComponent(sessionId)}/load`, { method: 'POST' });

    // Fetch messages
    const res  = await fetch(`/sessions/${encodeURIComponent(sessionId)}/messages`);
    const msgs = await res.json();

    // Clear current chat, render messages
    if (messagesEl) messagesEl.innerHTML = '';
    hideWelcome();
    messageCount = msgs.filter(m => m.role === 'assistant').length;

    const userInitial = _currentUser ? _currentUser.initial : 'U';

    for (const m of msgs) {
      if (m.role === 'user') {
        const wrap = document.createElement('div');
        wrap.className = 'msg-wrap user';
        wrap.innerHTML = `
          <div class="avatar">${userInitial}</div>
          <div class="bubble">${esc(m.content || '')}</div>
        `;
        messagesEl.appendChild(wrap);
      } else if (m.role === 'assistant') {
        const wrap = document.createElement('div');
        wrap.className = 'msg-wrap ai';
        wrap.innerHTML = `
          <div class="avatar">${COMPACT_FACES.done}</div>
          <div class="bubble"><div class="content">${safeMarkdown(m.content || '')}</div></div>
        `;
        messagesEl.appendChild(wrap);
      }
    }
    _activeSession = sessionId;

    // Re-render history list with active highlight
    renderSessionList($('hist-list'), _sessions);
    closeHistoryPanel();
    switchTab('chat');
    if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
  } catch (err) {
    console.error('Failed to load session:', err);
  }
}

// ── 12. Documents ────────────────────────────────────────────────────────────

const _TYPE_ICONS = { pdf: '\uD83D\uDCC4', docx: '\uD83D\uDCDD', doc: '\uD83D\uDCDD', txt: '\uD83D\uDCC3', md: '\uD83D\uDCC3', py: '\uD83D\uDC0D', json: '\u2699\uFE0F', csv: '\uD83D\uDCCA' };

function openDocsPanel() {
  const panel = $('docs-panel');
  if (panel) panel.classList.add('open');
  loadDocs();
}

function closeDocsPanel() {
  const panel = $('docs-panel');
  if (panel) panel.classList.remove('open');
}

async function loadDocs() {
  try {
    const docs = await fetch('/docs/list').then(r => r.json());
    renderDocList(docs);
    updateDocsSidebar(docs);
  } catch {
    const docsList = $('docs-list');
    if (docsList) docsList.innerHTML = '<div class="mem-empty">Could not load documents.</div>';
  }
}

function updateDocsSidebar(docs) {
  const badge = $('s-doc-count');
  if (badge) badge.textContent = docs.length;
  const preview = $('s-docs-preview');
  if (!preview) return;
  if (!docs.length) {
    preview.innerHTML = '<div class="info-row"><span class="info-key">\u2014</span><span class="info-val">no docs yet</span></div>';
    return;
  }
  preview.innerHTML = docs.slice(0, 3).map(d =>
    `<div class="info-row">
      <span class="info-key" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:110px" title="${esc(d.filename)}">${esc(d.filename)}</span>
      <span class="info-val">${esc(d.file_type)}</span>
    </div>`
  ).join('');
}

function renderDocList(docs) {
  const list = $('docs-list');
  if (!list) return;
  if (!docs.length) {
    list.innerHTML = '<div class="mem-empty">No documents uploaded yet.</div>';
    return;
  }
  list.innerHTML = docs.map(d => {
    const icon = _TYPE_ICONS[d.file_type] || '\uD83D\uDCCE';
    const kb   = Math.round(d.char_count / 1000);
    const date = d.uploaded_at.slice(0, 10);
    return `<div class="doc-row" data-doc-id="${esc(d.doc_id)}">
      <span class="doc-icon">${icon}</span>
      <div class="doc-info">
        <div class="doc-name" title="${esc(d.filename)}">${esc(d.filename)}</div>
        <div class="doc-meta">${esc(d.file_type)} \u00B7 ~${kb}k chars \u00B7 ${d.chunk_count} chunks \u00B7 ${date}</div>
      </div>
      <button class="btn-doc-del" title="Delete">\u2715</button>
    </div>`;
  }).join('');
  // Bind delete handlers
  list.querySelectorAll('.btn-doc-del').forEach(btn => {
    btn.addEventListener('click', () => {
      const row   = btn.closest('.doc-row');
      const docId = row.dataset.docId;
      deleteDoc(docId, btn);
    });
  });
}

async function deleteDoc(docId, btn) {
  if (!confirm('Delete this document and all its chunks?')) return;
  btn.disabled = true;
  try {
    await fetch(`/docs/${encodeURIComponent(docId)}`, { method: 'DELETE' });
    await loadDocs();
  } catch {
    btn.disabled = false;
  }
}

// Upload helpers
function _setUploadStatus(text, cls, targetId) {
  const el = $(targetId || 'docs-upload-status');
  if (!el) return;
  el.textContent = text;
  el.className = 'docs-upload-status' + (cls ? ' ' + cls : '');
}

async function _uploadFiles(files, statusTarget) {
  if (!files || !files.length) return;
  for (const file of files) {
    _setUploadStatus(`Uploading ${file.name}\u2026`, '', statusTarget);
    const form = new FormData();
    form.append('file', file);
    try {
      const res  = await fetch('/docs/upload', { method: 'POST', body: form });
      const data = await res.json();
      if (res.ok) _setUploadStatus(`\u2713 ${file.name}  (${data.chunk_count} chunks)`, 'ok', statusTarget);
      else        _setUploadStatus(`Error: ${data.detail || 'upload failed'}`, 'err', statusTarget);
    } catch (e) {
      _setUploadStatus(`Upload failed: ${e.message}`, 'err', statusTarget);
    }
    await loadDocs();
  }
  setTimeout(() => _setUploadStatus('', '', statusTarget), 4000);
}

// Settings panel file input + drop zone
const docsFileInput = $('docs-file-input');
const docsDropZone  = $('docs-drop-zone');
if (docsFileInput) {
  docsFileInput.addEventListener('change', () => _uploadFiles(docsFileInput.files, 'docs-upload-status'));
}
if (docsDropZone) {
  docsDropZone.addEventListener('dragover', e => { e.preventDefault(); docsDropZone.classList.add('drag-over'); });
  docsDropZone.addEventListener('dragleave', () => docsDropZone.classList.remove('drag-over'));
  docsDropZone.addEventListener('drop', e => {
    e.preventDefault();
    docsDropZone.classList.remove('drag-over');
    _uploadFiles(e.dataTransfer.files, 'docs-upload-status');
  });
}

// Docs panel file input + drop zone (separate elements in overlay)
const docsPanelFileInput = $('docs-panel-file-input');
const docsPanelDropZone  = $('docs-panel-drop-zone');
if (docsPanelFileInput) {
  docsPanelFileInput.addEventListener('change', () => _uploadFiles(docsPanelFileInput.files, 'docs-panel-upload-status'));
}
if (docsPanelDropZone) {
  docsPanelDropZone.addEventListener('dragover', e => { e.preventDefault(); docsPanelDropZone.classList.add('drag-over'); });
  docsPanelDropZone.addEventListener('dragleave', () => docsPanelDropZone.classList.remove('drag-over'));
  docsPanelDropZone.addEventListener('drop', e => {
    e.preventDefault();
    docsPanelDropZone.classList.remove('drag-over');
    _uploadFiles(e.dataTransfer.files, 'docs-panel-upload-status');
  });
}

// Drop files anywhere on the chat panel — same ingestion pipeline as the docs
// drop zones, but surfaced as a transient toast so it doesn't interrupt the chat.
const chatPanel       = $('panel-chat');
const chatDropOverlay = $('chat-drop-overlay');
const chatDropToast   = $('chat-drop-toast');

function _showChatDropToast(text, cls) {
  if (!chatDropToast) return;
  chatDropToast.textContent = text;
  chatDropToast.className = 'chat-drop-toast' + (text ? ' show' : '') + (cls ? ' ' + cls : '');
}

async function _uploadFilesToChat(files) {
  if (!files || !files.length) return;
  for (const file of files) {
    _showChatDropToast(`Uploading ${file.name}…`);
    const form = new FormData();
    form.append('file', file);
    try {
      const res  = await fetch('/docs/upload', { method: 'POST', body: form });
      const data = await res.json();
      if (res.ok) _showChatDropToast(`✓ ${file.name} added — ${data.chunk_count} chunks`, 'ok');
      else        _showChatDropToast(`Error: ${data.detail || 'upload failed'}`, 'err');
    } catch (e) {
      _showChatDropToast(`Upload failed: ${e.message}`, 'err');
    }
  }
  setTimeout(() => _showChatDropToast('', ''), 4000);
}

if (chatPanel && chatDropOverlay) {
  // Counter, not a boolean — dragenter/dragleave fire on every child element
  // as the pointer crosses them, so only the panel-level balance tells us
  // whether the drag has actually left the whole area.
  let _chatDragDepth = 0;
  const _isFileDrag = e => Array.from(e.dataTransfer?.types || []).includes('Files');

  chatPanel.addEventListener('dragenter', e => {
    if (!_isFileDrag(e)) return;
    e.preventDefault();
    _chatDragDepth++;
    chatDropOverlay.classList.add('active');
  });
  chatPanel.addEventListener('dragover', e => {
    if (!_isFileDrag(e)) return;
    e.preventDefault();
  });
  chatPanel.addEventListener('dragleave', e => {
    if (!_isFileDrag(e)) return;
    _chatDragDepth = Math.max(0, _chatDragDepth - 1);
    if (_chatDragDepth === 0) chatDropOverlay.classList.remove('active');
  });
  chatPanel.addEventListener('drop', e => {
    if (!_isFileDrag(e)) return;
    e.preventDefault();
    _chatDragDepth = 0;
    chatDropOverlay.classList.remove('active');
    _uploadFilesToChat(e.dataTransfer.files);
  });
}

// ── 13. Escape Key Handler ───────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeMemoryPanel();
    closeHistoryPanel();
    closeDocsPanel();
  }
});

// ── 14. Init ─────────────────────────────────────────────────────────────────

// Set user info from localStorage
_currentUser = { name: localStorage.getItem('kai_last_user') || 'User' };
_currentUser.initial = _currentUser.name[0]?.toUpperCase() || 'U';

const topbarUser = $('topbar-user');
if (topbarUser) topbarUser.textContent = _currentUser.name;

const settingsUserName = $('settings-user-name');
if (settingsUserName) settingsUserName.textContent = _currentUser.name;

// Load everything
loadInfo();
loadMode();
loadPreset();
loadToolLevel();
loadModels();
loadSessions();
loadDocs();
loadDashboard();

// Start on dashboard tab
switchTab('dashboard');

// Cold open: surface any new capabilities (top of chat), then Kai greets you
// herself (uses her welcome-back note if she left one).
showCapabilityCard();
streamGreeting(false);

// Start idle blink (already set up via setTimeout above)

// ── Study Mode ─────────────────────────────────────────────────────────────────

let _studyLoaded = false;
let _studyFilter = 'all';
let _studyActiveTab = 'results';
let _epubBook = null;
let _epubRendition = null;

function loadStudy() {
  if (!_studyLoaded) {
    _studyLoaded = true;
    loadStudyCollections();
    loadStudyLibrary();
    _initStudyUI();
  }
}

function _initStudyUI() {
  const input = document.getElementById('study-search-input');
  const btn = document.getElementById('study-search-btn');
  if (input) {
    input.addEventListener('keydown', e => { if (e.key === 'Enter') doStudySearch(); });
  }
  if (btn) btn.addEventListener('click', doStudySearch);

  // filter chips
  document.querySelectorAll('.study-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.study-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      _studyFilter = chip.dataset.filter;
    });
  });

  // inner tabs
  document.querySelectorAll('.study-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.study-tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _studyActiveTab = btn.dataset.studyTab;
      document.getElementById('study-pane-results').style.display =
        _studyActiveTab === 'results' ? '' : 'none';
      document.getElementById('study-pane-library').style.display =
        _studyActiveTab === 'library' ? '' : 'none';
      document.getElementById('study-pane-ask').style.display =
        _studyActiveTab === 'ask' ? '' : 'none';
    });
  });
  _initStudyAskUI();
}

async function loadStudyCollections() {
  try {
    const res = await fetch('/study/collections');
    if (!res.ok) return;
    const data = await res.json();
    _renderStudyCollections(data.collections || {});
  } catch (e) {
    console.warn('Study collections load failed:', e);
  }
}

function _renderStudyCollections(collections) {
  const el = document.getElementById('study-collections-list');
  if (!el) return;
  let html = '';
  for (const [cat, sources] of Object.entries(collections)) {
    html += `
      <button class="study-cat-btn" onclick="toggleStudyCat(this)">
        <span>${cat}</span>
        <span class="material-symbols-outlined" style="font-size:16px;transition:transform .2s">expand_more</span>
      </button>
      <div class="study-cat-items">
        ${sources.map(s => `
          <div class="study-source-item" title="${s.desc}">
            <a href="${s.url}" target="_blank" rel="noopener">${s.name}</a>
            <div style="font-size:10px;opacity:.6;margin-top:1px;">${s.desc}</div>
          </div>
        `).join('')}
      </div>
    `;
  }
  el.innerHTML = html;
}

function toggleStudyCat(btn) {
  const items = btn.nextElementSibling;
  const icon = btn.querySelector('.material-symbols-outlined');
  const isOpen = items.classList.toggle('open');
  btn.classList.toggle('active', isOpen);
  if (icon) icon.style.transform = isOpen ? 'rotate(180deg)' : '';
}

async function doStudySearch() {
  const query = (document.getElementById('study-search-input')?.value || '').trim();
  if (!query) return;
  const resultsEl = document.getElementById('study-pane-results');
  if (!resultsEl) return;

  resultsEl.innerHTML = '<div class="study-empty">Searching open-access sources&hellip;</div>';

  // switch to results tab
  document.querySelectorAll('.study-tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelector('[data-study-tab="results"]')?.classList.add('active');
  document.getElementById('study-pane-results').style.display = '';
  document.getElementById('study-pane-library').style.display = 'none';

  try {
    const res = await fetch('/study/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query, filter: _studyFilter }),
    });
    const data = await res.json();
    _renderStudyResults(data.results || '', resultsEl);
  } catch (e) {
    resultsEl.innerHTML = `<div class="study-empty">Search failed: ${e.message}</div>`;
  }
}

function _renderStudyResults(text, container) {
  if (!text || !text.trim()) {
    container.innerHTML = '<div class="study-empty">No results found.</div>';
    return;
  }

  // Parse the text format from the tools
  const blocks = text.split(/\n(?=\d+\. \[)/).filter(Boolean);
  if (blocks.length <= 1 && !text.includes('[')) {
    container.innerHTML = `<div class="study-empty" style="text-align:left;white-space:pre-wrap;">${text}</div>`;
    return;
  }

  let html = '';
  for (const block of blocks) {
    const sourceMatch = block.match(/\[([^\]]+)\]/);
    const source = sourceMatch ? sourceMatch[1] : 'Unknown';

    // title line after [Source]
    const titleMatch = block.match(/\[[^\]]+\]\s+(.+)/);
    const title = titleMatch ? titleMatch[1].trim() : '';

    const authorMatch = block.match(/Authors?:\s+(.+)/);
    const authors = authorMatch ? authorMatch[1] : '';

    const abstractMatch = block.match(/Authors?:[^\n]*\n\s+([^\n]+)/);
    const abstract = abstractMatch ? abstractMatch[1] : '';

    const pdfMatch = block.match(/PDF:\s+(https?:\/\/\S+)/);
    const pdfUrl = pdfMatch ? pdfMatch[1] : '';
    const pdfIsReal = pdfUrl && pdfUrl !== '(not available)';

    const pageMatch = block.match(/Page:\s+(https?:\/\/\S+)/);
    const pageUrl = pageMatch ? pageMatch[1] : '';

    const downloadMatch = block.match(/Download:\s+(https?:\/\/\S+)/);
    const downloadUrl = downloadMatch ? downloadMatch[1] : '';

    const borrowMatch = block.match(/Borrow:\s+(https?:\/\/\S+)/);
    const borrowUrl = borrowMatch ? borrowMatch[1] : '';

    const statusMatch = block.match(/—\s+(\w[\w ]+)\n/);
    const status = statusMatch ? statusMatch[1] : '';
    const isFree = /public domain|open|free/i.test(status) || pdfIsReal || downloadUrl;

    if (!title) continue;

    html += `
      <div class="study-result-card">
        <div>
          <span class="study-result-source">${source}</span>
          ${isFree ? '<span class="study-badge-free" style="margin-left:6px;">Free</span>' :
                     '<span class="study-badge-unknown" style="margin-left:6px;">Check</span>'}
        </div>
        <div class="study-result-title">${title}</div>
        ${authors ? `<div class="study-result-meta">${authors}</div>` : ''}
        ${abstract ? `<div class="study-result-abstract">${abstract}</div>` : ''}
        <div class="study-result-actions">
          ${pdfIsReal ? `<button class="btn btn-sm" onclick="studySaveItem('${encodeURIComponent(pdfUrl)}','${encodeURIComponent(title)}','${encodeURIComponent(authors)}','${encodeURIComponent(source)}','pdf')">Save PDF</button>` : ''}
          ${downloadUrl && downloadUrl.includes('.epub') ? `<button class="btn btn-sm" onclick="studySaveItem('${encodeURIComponent(downloadUrl)}','${encodeURIComponent(title)}','${encodeURIComponent(authors)}','${encodeURIComponent(source)}','epub')">Save epub</button>` : ''}
          ${downloadUrl && !downloadUrl.includes('.epub') ? `<button class="btn btn-sm" onclick="studySaveItem('${encodeURIComponent(downloadUrl)}','${encodeURIComponent(title)}','${encodeURIComponent(authors)}','${encodeURIComponent(source)}','pdf')">Download</button>` : ''}
          ${pageUrl ? `<a href="${pageUrl}" target="_blank" rel="noopener" class="btn btn-sm btn-muted">View Page</a>` : ''}
          ${borrowUrl && !downloadUrl ? `<a href="${borrowUrl}" target="_blank" rel="noopener" class="btn btn-sm btn-muted">Borrow</a>` : ''}
          ${!pdfIsReal && !downloadUrl ? `<button class="btn btn-sm btn-muted" onclick="studyFindFree('','${encodeURIComponent(title)}')">Find Free Copy</button>` : ''}
        </div>
      </div>
    `;
  }
  container.innerHTML = html || '<div class="study-empty">Could not parse results.</div>';
}

async function studyFindFree(doi, encodedTitle) {
  const title = decodeURIComponent(encodedTitle);
  const resultsEl = document.getElementById('study-pane-results');
  const notice = document.createElement('div');
  notice.className = 'study-result-card';
  notice.style.borderColor = 'var(--accent)';
  notice.innerHTML = `<div style="font-size:13px;color:var(--muted)">Searching Unpaywall for free version of "${title}"&hellip;</div>`;
  resultsEl?.prepend(notice);

  try {
    const res = await fetch('/study/find_free', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ doi, title }),
    });
    const data = await res.json();
    const text = data.result || 'No result.';
    const pdfMatch = text.match(/PDF:\s+(https?:\/\/\S+)/);
    const pdfUrl = pdfMatch ? pdfMatch[1] : '';
    notice.innerHTML = `
      <div style="font-size:13px;white-space:pre-wrap;">${text}</div>
      ${pdfUrl ? `<div style="margin-top:8px;"><button class="btn btn-sm" onclick="studySaveItem('${encodeURIComponent(pdfUrl)}','${encodeURIComponent(title)}','','Unpaywall','pdf')">Save Free PDF</button></div>` : ''}
    `;
  } catch (e) {
    notice.innerHTML = `<div style="color:var(--muted);font-size:13px;">Unpaywall lookup failed: ${e.message}</div>`;
  }
}

async function studySaveItem(encodedUrl, encodedTitle, encodedAuthor, encodedSource, format) {
  const url = decodeURIComponent(encodedUrl);
  const title = decodeURIComponent(encodedTitle);
  const author = decodeURIComponent(encodedAuthor);
  const source = decodeURIComponent(encodedSource);

  try {
    const res = await fetch('/study/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url, title, author, source, format }),
    });
    if (!res.ok) {
      const err = await res.json();
      alert('Download failed: ' + (err.detail || res.status));
      return;
    }
    const data = await res.json();
    loadStudyLibrary();
    // Switch to library tab
    document.querySelectorAll('.study-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('[data-study-tab="library"]')?.classList.add('active');
    document.getElementById('study-pane-results').style.display = 'none';
    document.getElementById('study-pane-library').style.display = '';
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

async function loadStudyLibrary() {
  try {
    const res = await fetch('/study/library');
    if (!res.ok) return;
    const data = await res.json();
    _renderStudyLibrary(data.items || []);
  } catch (e) {
    console.warn('Study library load failed:', e);
  }
}

function _renderStudyLibrary(items) {
  const listEl = document.getElementById('study-library-list');
  const emptyEl = document.getElementById('study-library-empty');
  if (!listEl) return;
  if (!items.length) {
    listEl.innerHTML = '';
    if (emptyEl) emptyEl.style.display = '';
    return;
  }
  if (emptyEl) emptyEl.style.display = 'none';
  listEl.innerHTML = items.map(item => `
    <div class="study-result-card">
      <div>
        <span class="study-result-source">${item.source || 'Saved'}</span>
        <span class="study-result-source" style="background:rgba(72,199,142,.1);color:#48c78e;margin-left:6px;">${item.format.toUpperCase()}</span>
      </div>
      <div class="study-result-title">${item.title}</div>
      ${item.author ? `<div class="study-result-meta">${item.author}</div>` : ''}
      <div class="study-result-meta" style="font-size:11px;">${item.created_at}</div>
      <div class="study-result-actions">
        <button class="btn btn-sm" onclick="openStudyReader(${item.id}, '${encodeURIComponent(item.title)}', '${item.format}')">Open</button>
        <button class="btn btn-sm btn-muted" onclick="deleteStudyItem(${item.id})">Remove</button>
      </div>
    </div>
  `).join('');
}

async function deleteStudyItem(itemId) {
  if (!confirm('Remove this item from your library?')) return;
  try {
    await fetch(`/study/library/${itemId}`, { method: 'DELETE' });
    loadStudyLibrary();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

function openStudyReader(itemId, encodedTitle, format) {
  const title = decodeURIComponent(encodedTitle);
  const overlay = document.getElementById('study-reader-overlay');
  const titleEl = document.getElementById('reader-title');
  const epubViewer = document.getElementById('epub-viewer');
  const pdfFrame = document.getElementById('pdf-viewer-frame');
  const epubNav = document.getElementById('epub-nav');

  if (!overlay) return;
  if (titleEl) titleEl.textContent = title;
  overlay.classList.add('open');

  const url = `/study/read/${itemId}`;

  if (format === 'epub') {
    if (pdfFrame) pdfFrame.style.display = 'none';
    if (epubViewer) epubViewer.style.display = '';
    if (epubNav) epubNav.style.display = '';

    // Clean up previous epub
    if (_epubRendition) { try { _epubRendition.destroy(); } catch(e){} _epubRendition = null; }
    if (_epubBook) { try { _epubBook.destroy(); } catch(e){} _epubBook = null; }

    if (typeof ePub === 'undefined') {
      epubViewer.innerHTML = `<div style="padding:40px;color:#888;">epub.js not loaded. <a href="${url}" target="_blank">Download file</a></div>`;
      return;
    }

    _epubBook = ePub(url);
    _epubRendition = _epubBook.renderTo('epub-viewer', {
      width: '100%', height: '100%', spread: 'none',
    });
    _epubRendition.display();

    document.getElementById('epub-prev')?.addEventListener('click', () => _epubRendition?.prev());
    document.getElementById('epub-next')?.addEventListener('click', () => _epubRendition?.next());

  } else {
    if (epubViewer) { epubViewer.style.display = 'none'; epubViewer.innerHTML = ''; }
    if (epubNav) epubNav.style.display = 'none';
    if (pdfFrame) {
      pdfFrame.style.display = '';
      pdfFrame.src = url;
    }
  }
}

// ── Ask Library ────────────────────────────────────────────────────────────────

function _initStudyAskUI() {
  const input = document.getElementById('study-ask-input');
  const btn = document.getElementById('study-ask-btn');
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') doStudyAsk(); });
  if (btn) btn.addEventListener('click', doStudyAsk);
}

async function doStudyAsk() {
  const question = (document.getElementById('study-ask-input')?.value || '').trim();
  if (!question) return;
  const resultsEl = document.getElementById('study-ask-results');
  if (!resultsEl) return;
  resultsEl.innerHTML = '<div class="study-empty">Searching your library&hellip;</div>';
  try {
    const res = await fetch('/study/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    const text = data.result || 'No result.';
    // Render as formatted cards
    const sections = text.split(/\n(?=── From:)/);
    if (sections.length <= 1) {
      resultsEl.innerHTML = `<div class="study-empty" style="text-align:left;white-space:pre-wrap;font-size:13px;">${text}</div>`;
      return;
    }
    let html = `<div style="font-size:12px;color:var(--muted);margin-bottom:12px;">${sections[0].trim()}</div>`;
    for (const section of sections.slice(1)) {
      const headerMatch = section.match(/^── From: (.+?) ──/);
      const header = headerMatch ? headerMatch[1] : 'Unknown source';
      const body = section.replace(/^── From: .+ ──\n?/, '').trim();
      html += `
        <div class="study-result-card">
          <div class="study-result-source">${header}</div>
          <div style="font-size:13px;line-height:1.6;color:var(--text);margin-top:6px;white-space:pre-wrap;">${body}</div>
        </div>
      `;
    }
    resultsEl.innerHTML = html;
  } catch (e) {
    resultsEl.innerHTML = `<div class="study-empty">Ask failed: ${e.message}</div>`;
  }
}

function closeStudyReader() {
  const overlay = document.getElementById('study-reader-overlay');
  if (overlay) overlay.classList.remove('open');
  const pdfFrame = document.getElementById('pdf-viewer-frame');
  if (pdfFrame) pdfFrame.src = '';
  if (_epubRendition) { try { _epubRendition.destroy(); } catch(e){} _epubRendition = null; }
  if (_epubBook) { try { _epubBook.destroy(); } catch(e){} _epubBook = null; }
  const epubViewer = document.getElementById('epub-viewer');
  if (epubViewer) epubViewer.innerHTML = '';
}
