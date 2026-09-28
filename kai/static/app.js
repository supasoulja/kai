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
let _maxInputChars   = 8000;    // server's chat limit, refreshed from /info
let _greetingActive  = false;   // Kai's opening line is still streaming
let _queuedMessage   = null;    // typed during the greeting; sent when it finishes
let _approveCallId   = null;    // set by a card's Go ahead: the call the next message approves

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
let _caps         = new Set();   // this account's capabilities, from /info

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
  if (tabName === 'dashboard') { loadDashboard(); KaiConsole.start(); }
  else KaiConsole.stop();
  if (tabName === 'study') loadStudy();
  if (tabName === 'settings') settingsBackToHub();
  if (tabName === 'memory') loadMemoryBrowser();
  if (tabName === 'notes') loadNotes();
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

// Quit button — desktop app only. Routes through the native clean-quit path
// (pywebview bridge → app.py _clean_quit: saving overlay + watchdog + ritual),
// so it works even when the tray Quit misbehaves. Hidden in the browser, where
// killing the shared server from a tab isn't wanted.
(function initQuitButton() {
  const btn = $('topbar-quit');
  if (!btn) return;
  const hasBridge = () => window.pywebview && pywebview.api && pywebview.api.close_action;
  const reveal = () => { if (hasBridge()) btn.style.display = ''; };
  // pywebview.api may attach after this script runs; catch it either way.
  window.addEventListener('pywebviewready', reveal);
  let tries = 0;
  const probe = setInterval(() => {
    if (hasBridge()) { reveal(); clearInterval(probe); }
    else if (++tries > 40) clearInterval(probe);  // ~10s; give up (browser mode)
  }, 250);
  btn.addEventListener('click', () => {
    if (!confirm('Quit Kai? It will finish saving the session, then exit.')) return;
    try { pywebview.api.close_action('quit', false); }
    catch (e) { console.error('Quit failed:', e); }
  });
})();

// External links must never hijack the desktop webview — it's a single
// chrome-less window with no Back button, so a chat link to e.g. an arXiv PDF
// would trap the user on that page. Route cross-origin http(s) links to a
// separate pop-up window via the native bridge (app.py _Api.open_link). In the
// browser there's no bridge, so fall back to a normal new tab. Capture phase so
// we win before any bubbling handler and can preventDefault reliably.
document.addEventListener('click', e => {
  const a = e.target.closest && e.target.closest('a[href]');
  if (!a) return;
  let url;
  try { url = new URL(a.href); } catch { return; }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
  if (url.origin === window.location.origin) return;  // in-app nav: leave alone
  e.preventDefault();
  if (window.pywebview && pywebview.api && pywebview.api.open_link) {
    pywebview.api.open_link(a.href);
  } else {
    window.open(a.href, '_blank', 'noopener');
  }
}, true);

// Kai's Computer button
const computerBtn = $('open-computer-btn');
if (computerBtn) {
  computerBtn.addEventListener('click', () => {
    const session = _activeSession || '';
    const boot = session ? 'warm' : 'cold';
    // The GTK webview never handles window.open (no 'create' signal), so the
    // desktop app has to spawn the window through the bridge. open_link only
    // accepts http(s), hence location.origin rather than a bare path.
    const url = `${location.origin}/computer?session=${encodeURIComponent(session)}&boot=${boot}`;
    if (window.pywebview && pywebview.api && pywebview.api.open_link) {
      pywebview.api.open_link(url);
    } else {
      window.open(url, 'kai-computer');
    }
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

  loadModelsCard();
  loadRoutineCard();

  // Developer system stats (only when dev mode is on)
  if (_devMode) loadDevStats();

  // Recent sessions
  loadSessions();
}


// ── Models + Routine cards ───────────────────────────────────────────────────
// Only what a sensor said: unknowns render as unknown, never as zero.

const _ROLE_LABELS = { chat: 'Chat', tools: 'Tools', crew: 'Crew', search_fast: 'Search · fast', search_deep: 'Search · deep' };

function _gb(b) { return `${(b / 2 ** 30).toFixed(1)} GB`; }

// Where a loaded model sits: all on the GPU, all in RAM (run by the CPU), or split.
function _where(m) {
  if (!m.vram_bytes) return `${_gb(m.bytes)} on CPU`;
  if (m.vram_bytes < m.bytes) return `${_gb(m.bytes)}, ${_gb(m.vram_bytes)} on GPU`;
  return _gb(m.bytes);
}

function _roleState(r) {
  switch (r.state) {
    case 'loaded':    return `loaded · ${_where(r)}`;
    case 'installed': return `installed · ${_gb(r.bytes)}`;
    case 'missing':   return 'not installed';
    case 'cpu':       return 'on CPU · built in';
    case 'cloud':     return 'cloud';
    default:          return 'unknown';
  }
}

function _memBar(label, total, used, kaiBytes) {
  const pct = b => `${Math.max(0, Math.min(100, b / total * 100)).toFixed(2)}%`;
  const kai = kaiBytes ? `<div class="dash-mem-kai" style="width:${pct(kaiBytes)}"></div>` : '';
  const other = Math.max(0, used - (kaiBytes || 0));
  return `<div class="dash-mem-bar">
    <div class="dash-mem-top"><span>${label}</span><span class="dash-mem-num">${_gb(used)} / ${_gb(total)}</span></div>
    <div class="dash-mem-track">${kai}<div class="dash-mem-other" style="width:${pct(other)}"></div></div>
  </div>`;
}

async function loadModelsCard() {
  const rolesEl = $('dash-models-roles');
  const machineEl = $('dash-models-machine');
  if (!rolesEl) return;
  let data;
  try {
    data = await fetch('/dashboard/models').then(r => { if (!r.ok) throw r; return r.json(); });
  } catch {
    rolesEl.innerHTML = '<span class="dash-note">Couldn\'t read the models right now.</span>';
    return;
  }
  rolesEl.innerHTML = data.roles.map(r => `<div class="dash-role">
      <span class="dash-stat-key">${_esc(_ROLE_LABELS[r.role] || r.role)}</span>
      <span class="dash-role-model" title="${_esc(r.model)}">${_esc(r.model)}</span>
      <span class="dash-role-state ${_esc(r.state)}"><i></i>${_roleState(r)}</span>
    </div>`).join('');

  const m = data.machine;
  if (!m || !machineEl) { if (machineEl) machineEl.style.display = 'none'; return; }
  const loaded = m.loaded;
  const kaiBytes = loaded ? Object.values(loaded).reduce((a, m) => a + m.vram_bytes, 0) : 0;
  // Ollama doesn't say which card holds a model, so only a lone GPU gets the Kai segment.
  const bars = m.gpus.map(g => _memBar(
    `GPU ${g.index}${g.name ? ' · ' + _esc(g.name) : ''}`,
    g.total_bytes, g.used_bytes, m.gpus.length === 1 ? kaiBytes : 0,
  ));
  if (!m.gpus.length) bars.push('<div class="dash-note" style="margin:0 0 9px">No GPU found — models run on the CPU and use RAM.</div>');
  bars.push(_memBar('RAM', m.ram.total_bytes, m.ram.used_bytes, 0));

  let note;
  if (loaded === null) note = 'Ollama isn\'t reachable — what\'s loaded is unknown.';
  else if (!Object.keys(loaded).length) note = 'Nothing loaded in Ollama right now.';
  else {
    note = 'Loaded: ' + Object.entries(loaded).map(([k, m]) => `${_esc(k)} (${_where(m)})`).join(', ');
    if (m.gpus.length > 1) note += ' — Ollama doesn\'t say which card, so it isn\'t drawn on a bar.';
  }
  machineEl.innerHTML = `<div class="dash-machine-head"><span>This machine</span><span>live</span></div>
    ${bars.join('')}
    <div class="dash-legend">
      <span><i style="background:var(--accent)"></i>Kai's models</span>
      <span><i class="dash-mem-other"></i>everything else</span>
      <span><i style="background:var(--surface);border:1px solid var(--outline-v)"></i>free</span>
    </div>
    <div class="dash-note">${note}</div>`;
  machineEl.style.display = '';
}

async function loadRoutineCard() {
  const strip = $('dash-routine-strip');
  const note = $('dash-routine-note');
  if (!strip) return;
  let d;
  try {
    d = await fetch('/dashboard/routine').then(r => { if (!r.ok) throw r; return r.json(); });
  } catch {
    strip.innerHTML = '<div class="dash-rt-cell unknown"></div>'.repeat(24);
    if (note) note.textContent = 'Couldn\'t read your routine right now.';
    return;
  }
  const summary = `${d.messages} message${d.messages === 1 ? '' : 's'} on ${d.days_seen} day${d.days_seen === 1 ? '' : 's'}, last ${d.days} days`;
  if (!d.hours) {
    strip.innerHTML = Array.from({ length: 24 }, (_, h) =>
      `<div class="dash-rt-cell unknown" title="${String(h).padStart(2, '0')}:00 — unknown"></div>`).join('');
    if (note) note.textContent = `${summary} — needs ${d.min_days} days before the hours mean anything.`;
    return;
  }
  const peak = Math.max(1, ...d.hours);
  strip.innerHTML = d.hours.map((c, h) => {
    const lvl = c === 0 ? 0 : Math.min(3, Math.ceil(3 * c / peak));
    return `<div class="dash-rt-cell l${lvl}" title="${String(h).padStart(2, '0')}:00 — ${c} message${c === 1 ? '' : 's'}"></div>`;
  }).join('');
  if (note) note.textContent = summary;
}

{
  const btn = $('dash-models-refresh');
  if (btn) btn.addEventListener('click', loadModelsCard);
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

// ── Face Tag Stripper ────────────────────────────────────────────────────────
// The model is no longer asked to emit <face:...> tags — the persona's ## Face
// section was removed because it cost ~87 tokens every turn and bought a face
// in only 7.1% of responses (and `amused` was half of those; the compositional
// <face eyes= mouth= flair=> form was never used once). Faces are now derived
// locally by pickFaceFromText() below, at zero model cost.
// This stripper stays as belt-and-braces so a stray tag from an old prompt or
// a fine-tuned checkpoint never renders as literal text in the chat.

const FACE_TAG_RE = /<face(?::(\w+)|(\s+[^>]+))>/g;

function stripFaceTags(text) {
  return text.replace(FACE_TAG_RE, '');
}

// ── Local Face Picker (zero model tokens) ────────────────────────────────────
// Derives an emotional face from Kai's own words. Deliberately CONSERVATIVE:
// returns null (→ neutral) unless the opener clearly supports an emotion, so we
// never stamp a confident-wrong face on a greeting or a status line.
// Validated against 598 stored responses: fires on 4.5%, every pick defensible.

// Emotion lives in the opening beat — a failure word buried deep in a long
// technical answer must not recolour the face. Read the first ~2 sentences.
function _faceOpener(text, n = 220) {
  const head = text.slice(0, n + 80);
  const out = head.split(/(?<=[.!?])\s+/).slice(0, 2).join(' ');
  return out.slice(0, n);
}

function pickFaceFromText(text) {
  if (!text) return null;
  const t = _faceOpener(text).toLowerCase();

  // UI/marker-only turns ("[stopped]") carry no emotion.
  if (/^\s*\[?\s*(stopped|cancelled|canceled|interrupted)\s*\]?\s*$/.test(t)) return null;

  // 1. Kai HERSELF hit a failure — not merely explaining an error code.
  //    The second test suppresses "the error 0x.. means…" / "that code indicates…".
  if (/\b(traceback|segfault|kernel panic|hit (a snag|an error)|i (couldn'?t|could not|failed to|was unable to|wasn'?t able to)\b|i can'?t (find|reach|connect|access|complete|run)\b|(it|that|the command|the operation) failed\b)/.test(t)
      && !/error code|0x[0-9a-f]{3,}|\bmeans\b|\bstands for\b|\bindicates\b/.test(t)) {
    return 'error';
  }
  // 2. Apology / empathy / bad news softened
  if (/\b(sorry|apolog|unfortunately|i'?m afraid|that'?s (frustrating|rough|a pain)|i (know|understand) (that'?s|this is))\b/.test(t)) return 'sympathetic';
  // 3. Completion / verified accomplishment
  if (/\b(all set|all done|done[.!]|shipped|fixed|verified|it works|works now|tests? (pass|are passing)|passing|got it working|up and running|good to go|landed)\b/.test(t)) return 'proud';
  // 4. Genuine enthusiasm
  if (/(!!|\blet'?s\b.*!|\b(awesome|amazing|brilliant|excellent|love (it|this)|so cool|can'?t wait)\b)/.test(t)) return 'excited';
  // 5. Surprise
  if (/\b(whoa|wow|huh[.!]|didn'?t expect|surprising|turns out|wait[,.])/.test(t)) return 'surprised';
  // 6. Annoyance
  if (/\b(ugh|seriously\?|again\?|of course it|classic|yet again|still (broken|failing|not))\b/.test(t)) return 'annoyed';
  // 7. Confusion / uncertainty
  if (/\b(not sure|unclear|hmm+\b|that'?s (odd|strange|weird)|i'?m confused|doesn'?t (make sense|add up)|unexpected|can'?t tell)\b/.test(t)) return 'confused';
  // 8. Low energy
  if (/\b(long day|exhausting|worn out|running on fumes|late (night|here))\b/.test(t)) return 'tired';
  // 9. Wry / amused — hardest to catch from vocabulary; strongest tells only.
  if (/(\bheh\b|\blol\b|;\)|\bironically\b|\bof course\b|\bnaturally\b|\bpredictably\b|\bshocking(ly)?\b)/.test(t)) return 'amused';
  // 10. Plain warmth
  if (/\b(glad|happy to|nice[.!]|great[,.!]|good (call|catch|question)|you'?re welcome|thanks?)\b/.test(t)) return 'happy';

  return null;   // neutral — the common case (~95%)
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

// `text`, when given, is Kai's finished response — the local picker reads it to
// colour the completion face. Falls back to 'done' when nothing clearly applies.
function faceOnDone(hadError, text) {
  stopIdleBlink();
  if (_doneTimer) clearTimeout(_doneTimer);
  setFace(hadError ? 'error' : (pickFaceFromText(text) || 'done'));
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

    // Who is signed in — topbar and Settings → You
    if (d.user && d.user.name) {
      _currentUser = { name: d.user.name, initial: d.user.name[0].toUpperCase() };
      const tu = $('topbar-user');
      if (tu) tu.textContent = d.user.name;
      const su = $('settings-user-name');
      if (su) su.textContent = d.user.name;
    }
    if (d.max_input_chars) _maxInputChars = d.max_input_chars;

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

    // What this account may do comes from the server's permission table
    // (kai/core/permissions.py), never from guessing at the level here.
    const can = new Set(d.capabilities || []);
    _caps = can;

    // People page — owner and admins
    const peopleNav = $('settings-nav-people');
    if (peopleNav) peopleNav.style.display = can.has('accounts.view') ? '' : 'none';

    // System control (restart / shutdown) — owner and admins
    const sysControl = $('system-control-section');
    if (sysControl) sysControl.style.display = can.has('server.control') ? '' : 'none';

    // Server console — owner and admins (also starts polling if dashboard is open)
    KaiConsole.setOwner(can.has('server.control'));
    if (can.has('server.control') && document.getElementById('panel-dashboard')?.classList.contains('active')) {
      KaiConsole.start();
    }
  } catch { /* ignore */ }
}

// ── People: accounts, levels, grants, kicks, audit log ───────────────────────
// Every rule is the server's (kai/core/permissions.py). GET /accounts says which rows this
// account may edit, which levels it may hand out and which grants it may set; this page
// only hides what the server would refuse.

// What each level does today. Only what the server enforces (docs/active/PERMISSIONS.md).
const LEVEL_INFO = {
  owner:  'Everything on this Kai. There is one owner, and ownership moves only by transfer.',
  admin:  'Adds Members and Guests, manages models and recipes, restarts Kai, reads the audit log.',
  member: 'Their own chats, memory, notes and documents.',
  guest:  'Chats are saved, but Kai learns nothing from them.',
};
const GRANT_INFO = {
  'tools.destructive': {
    tag:   '+ destructive tools',
    label: 'May approve destructive tools in their own session',
    note:  'Saved on the account, but nothing checks it yet: the tool gate that reads it is the next permissions increment.',
  },
};
const AUDIT_VERB = {
  'account.create':         'created',
  'account.edit':           'edited',
  'account.grants':         'changed the grants of',
  'account.kick':           'kicked',
  'account.delete':         'deleted the account',
  'account.transfer_owner': 'handed ownership to',
  'server.restart':         'restarted Kai',
  'server.shutdown':        'shut Kai down',
};
const AUDIT_PAGE = 50;

let _people      = { accounts: [], assignable: [], grants: {} };
let _peopleOpen  = null;   // id of the expanded account
let _auditOldest = null;   // smallest audit id on screen, for "Load older"
let _auditSeq    = 0;

const levelName = l => l ? l[0].toUpperCase() + l.slice(1) : '';

function fmtWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const now  = new Date();
  const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return `today ${time}`;
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return `yesterday ${time}`;
  const opts = { month: 'short', day: 'numeric' };
  if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
  return `${d.toLocaleDateString('en-US', opts)} ${time}`;
}

function peopleMsg(text, isErr = false) {
  const el = $('people-msg');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('err', !!isErr);
}

// The response body, or null once the reason it failed is on screen.
async function peopleCall(url, method, body) {
  try {
    const r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      peopleMsg(typeof d.detail === 'string' ? d.detail : 'That didn’t work.', true);
      return null;
    }
    return d;
  } catch {
    peopleMsg('Network error. Please try again.', true);
    return null;
  }
}

async function loadPeople() {
  const list = $('people-list');
  if (!list) return;
  try {
    const r = await fetch('/accounts');
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      list.innerHTML = `<div class="empty-hint">${esc(typeof d.detail === 'string' ? d.detail : 'Could not load accounts.')}</div>`;
      return;
    }
    _people = { accounts: d.accounts || [], assignable: d.assignable_levels || [], grants: d.grants || {} };
  } catch {
    list.innerHTML = '<div class="empty-hint">Could not load accounts.</div>';
    return;
  }
  renderPeople();
}

function renderPeople() {
  const list = $('people-list');
  if (!list) return;
  const count = $('people-count');
  if (count) count.textContent = _people.accounts.length;

  const addBtn = $('people-add-btn');
  if (addBtn) addBtn.style.display = _people.assignable.length ? '' : 'none';
  const addLevel = $('people-add-level');
  if (addLevel) {
    const keep = addLevel.value;
    addLevel.innerHTML = _people.assignable.map(l => `<option value="${esc(l)}">${esc(levelName(l))}</option>`).join('');
    addLevel.value = _people.assignable.includes(keep) ? keep
      : _people.assignable.includes('member') ? 'member' : (_people.assignable[0] || '');
    const hint = $('people-add-hint');
    if (hint) hint.textContent = LEVEL_INFO[addLevel.value] || '';
  }

  if (!_people.accounts.some(a => a.id === _peopleOpen)) _peopleOpen = null;
  const names = new Map(_people.accounts.map(a => [a.id, a.name]));
  list.innerHTML = _people.accounts.map(a => renderPerson(a, names)).join('');
}

// Owner-only actions on someone else's row: kick, remove, hand over ownership.
function _ownerActions(a) {
  return !a.you && (_caps.has('accounts.kick') || _caps.has('accounts.transfer'));
}

function renderPerson(a, names) {
  const open     = _peopleOpen === a.id;
  const editable = a.can_edit || _ownerActions(a);
  const maker    = a.created_by ? `added by ${names.get(a.created_by) || 'a removed account'}` : '';
  const seen     = a.last_seen ? `signed in ${fmtWhen(a.last_seen)}` : 'never signed in';
  const grants   = a.grants.map(g =>
    `<span class="people-grant-tag" title="${esc(GRANT_INFO[g]?.label || g)}">${esc(GRANT_INFO[g]?.tag || g)}</span>`).join('');
  const head = `
      <span class="people-avatar">${esc((a.name[0] || '?').toUpperCase())}</span>
      <span class="people-info">
        <span class="people-name">${esc(a.name)}${a.you ? ' <span class="people-you">(you)</span>' : ''}</span>
        <span class="people-meta">${esc([maker, seen].filter(Boolean).join(' · '))}</span>
      </span>
      <span class="people-tags">${grants}<span class="people-level lv-${esc(a.level)}">${esc(a.level)}</span>${
        editable ? '<span class="material-symbols-outlined people-chev">expand_more</span>' : ''}</span>`;
  const row = editable
    ? `<button class="people-row" data-toggle="${a.id}" aria-expanded="${open}">${head}</button>`
    : `<div class="people-row" title="Only the owner can edit this account.">${head}</div>`;
  return `<div class="people-item${open ? ' open' : ''}" data-id="${a.id}">${row}${open ? renderPersonEdit(a) : ''}</div>`;
}

function renderPersonEdit(a) {
  let html = '';
  if (a.can_edit) {
    const levels = a.level === 'owner' ? [] : _people.assignable;
    const levelField = levels.length ? `
      <label class="people-field">Level
        <select class="model-select" data-f="level">${levels.map(l =>
          `<option value="${esc(l)}"${l === a.level ? ' selected' : ''}>${esc(levelName(l))}</option>`).join('')}</select>
      </label>` : '';
    html += `
      <label class="people-field">Name
        <input class="model-name-input" data-f="name" type="text" maxlength="64" autocomplete="off" value="${esc(a.name)}">
      </label>
      <label class="people-field">New PIN
        <input class="model-name-input" data-f="pin" type="password" inputmode="numeric" maxlength="8"
               placeholder="Leave blank to keep the current PIN" autocomplete="new-password">
      </label>
      ${levelField}
      <div class="people-hint" data-f="level-hint">${esc(LEVEL_INFO[a.level] || '')}</div>
      <div class="people-grants" data-f="grants">${renderGrantBoxes(a, a.level)}</div>
      <div class="model-add-actions">
        <button class="btn btn-sm" data-act="save">Save</button>
        <button class="btn btn-sm btn-muted" data-act="close">Cancel</button>
      </div>`;
  }
  if (_ownerActions(a)) {
    const btns = [];
    if (_caps.has('accounts.kick')) {
      btns.push('<button class="btn btn-sm btn-muted" data-act="signout">Sign out everywhere</button>');
      btns.push('<button class="btn btn-sm btn-danger" data-act="remove">Remove account…</button>');
    }
    if (_caps.has('accounts.transfer')) btns.push('<button class="btn btn-sm btn-muted" data-act="transfer">Make owner…</button>');
    html += `<div class="people-danger"><span class="people-danger-label">Owner only</span>${btns.join('')}</div>`;
  }
  return `<div class="people-edit">${html}</div>`;
}

// Checkboxes for the grants that fit *level*. Only the owner sees any: GET /accounts
// sends the grant table to accounts that may set grants, and {} to everyone else.
function renderGrantBoxes(a, level) {
  return Object.entries(_people.grants).filter(([, fits]) => fits === level).map(([g]) => {
    const info = GRANT_INFO[g] || { label: g };
    return `
      <label class="people-grant">
        <input type="checkbox" data-grant="${esc(g)}"${a.grants.includes(g) ? ' checked' : ''}>
        <span>${esc(info.label)}${info.note ? `<span class="people-hint" style="display:block">${esc(info.note)}</span>` : ''}</span>
      </label>`;
  }).join('');
}

async function savePerson(a, item, btn) {
  const name  = item.querySelector('[data-f="name"]').value.trim();
  const pin   = item.querySelector('[data-f="pin"]').value.trim();
  const level = item.querySelector('[data-f="level"]')?.value;
  const boxes = [...item.querySelectorAll('[data-grant]')];
  if (!name) return peopleMsg('Name is required.', true);
  if (pin && !/^\d{4,8}$/.test(pin)) return peopleMsg('PIN must be 4–8 digits.', true);

  const patch = {};
  if (name !== a.name) patch.name = name;
  if (pin) patch.pin = pin;
  if (level && level !== a.level) patch.level = level;
  const wanted = boxes.filter(b => b.checked).map(b => b.dataset.grant).sort();
  // A level change drops grants that no longer fit, so compare with what the edit left.
  const grantsDiffer = acc => boxes.length > 0 && wanted.join() !== [...acc.grants].sort().join();
  if (!Object.keys(patch).length && !grantsDiffer(a)) return peopleMsg('Nothing to change.');

  btn.disabled = true;
  const done = [];
  let acc = a;
  let ok = true;
  if (Object.keys(patch).length) {
    const d = await peopleCall(`/accounts/${a.id}`, 'PATCH', patch);
    if (d) {
      acc = d.account;
      if (patch.name)  done.push(`renamed from ${a.name}`);
      if (patch.pin)   done.push(a.you ? 'PIN changed' : 'PIN reset, and signed out everywhere');
      if (patch.level) done.push(`now ${levelName(acc.level)}`);
    } else ok = false;
  }
  if (ok && grantsDiffer(acc)) {
    if (await peopleCall(`/accounts/${a.id}/grants`, 'PUT', { grants: wanted })) done.push('grants updated');
    else ok = false;
  }
  btn.disabled = false;
  if (ok) {
    _peopleOpen = null;
    peopleMsg(`${acc.name}: ${done.join(', ')}.`);
  }
  if (done.length) {
    await loadPeople();
    if (a.you) loadInfo();
  }
  loadAudit(true);
}

async function kickPerson(a, remove) {
  if (remove) {
    const typed = prompt(`This permanently removes "${a.name}" and everything Kai remembers for them: chats, memory, notes and documents. Note files already written to disk are left where they are.\n\nType the name to confirm.`);
    if (typed === null) return;
    if (typed.trim() !== a.name) return peopleMsg('The name didn’t match. Nothing was removed.', true);
  } else if (!confirm(`Sign ${a.name} out on every device?\n\nThey can sign straight back in with their PIN. Reset it too if you want to keep them out.`)) {
    return;
  }
  const d = await peopleCall(`/accounts/${a.id}/kick`, 'POST', { delete: remove });
  if (d) {
    const n = d.sessions_ended;
    if (remove && !d.deleted) peopleMsg(`${a.name} was signed out, but the account couldn’t be removed.`, true);
    else peopleMsg(remove ? `${a.name} was removed.` : `${a.name} was signed out of ${n} session${n === 1 ? '' : 's'}.`);
    _peopleOpen = null;
    loadPeople();
  }
  loadAudit(true);
}

async function transferOwner(a) {
  const typed = prompt(`Make ${a.name} the owner of this Kai?\n\nYou become an admin. To get it back, they hand it over, or you run "kai --set-owner" in a terminal on this machine.\n\nType their name to confirm.`);
  if (typed === null) return;
  if (typed.trim() !== a.name) return peopleMsg('The name didn’t match. Ownership didn’t change.', true);
  if (await peopleCall('/accounts/transfer-owner', 'POST', { to_id: a.id })) {
    peopleMsg(`${a.name} now owns this Kai. You’re an admin.`);
    _peopleOpen = null;
    await loadInfo();   // this account's capabilities just changed
    loadPeople();
  }
  loadAudit(true);
}

function auditDetail(e) {
  const d = e.detail;
  if (d == null) return '';
  if (typeof d !== 'object') return String(d);
  if (!e.ok && d.refused) return d.refused;
  const list = xs => (Array.isArray(xs) ? (xs.length ? xs.join(', ') : 'none') : String(xs));
  if (e.action === 'account.grants') return `grants ${list(d.from)} → ${list(d.to)}`;
  const parts = [];
  for (const [k, v] of Object.entries(d)) {
    if (v && typeof v === 'object' && 'from' in v && 'to' in v) parts.push(`${k} ${list(v.from)} → ${list(v.to)}`);
    else if (k === 'pin') parts.push('PIN reset');
    else if (k === 'sessions_ended') parts.push(`${v} session${v === 1 ? '' : 's'} ended`);
    else if (k === 'deleted') { if (v) parts.push('account and its data removed'); }
    else if (k === 'old_owner_is_now') parts.push(`previous owner is now ${v}`);
    else if (k === 'by' || k === 'via' || k === 'from') parts.push(`${k} ${v}`);
    else parts.push(`${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`);
  }
  return parts.join(' · ');
}

function renderAuditEntry(e) {
  const actor  = esc(e.actor_name || (e.actor_id ? `#${e.actor_id}` : 'someone'));
  const target = e.target_name ? ` <b>${esc(e.target_name)}</b>` : '';
  const verb   = e.action === 'account.kick' && e.detail?.deleted ? 'removed' : AUDIT_VERB[e.action];
  const what   = e.ok
    ? `<b>${actor}</b> ${verb ? esc(verb) : `<span class="audit-action">${esc(e.action)}</span>`}${target}`
    : `<b>${actor}</b><span class="audit-refused">refused</span> <span class="audit-action">${esc(e.action)}</span>${target ? ` on${target}` : ''}`;
  const detail = auditDetail(e);
  return `<div class="audit-entry${e.ok ? '' : ' refused'}">
      <span class="audit-time" title="${esc(e.ts)}">${esc(fmtWhen(e.ts))}</span>
      <span class="audit-what">${what}</span>
      ${detail ? `<span class="audit-detail">${esc(detail)}</span>` : ''}
    </div>`;
}

async function loadAudit(reset) {
  const el = $('audit-list');
  if (!el) return;
  const seq    = ++_auditSeq;
  const before = reset ? null : _auditOldest;
  let entries;
  try {
    const r = await fetch(`/accounts/audit?limit=${AUDIT_PAGE}${before ? `&before=${before}` : ''}`);
    if (!r.ok) throw new Error(r.status);
    entries = (await r.json()).entries || [];
  } catch {
    if (reset && seq === _auditSeq) el.innerHTML = '<div class="empty-hint">Could not load the audit log.</div>';
    return;
  }
  if (seq !== _auditSeq) return;   // a newer load replaced this one
  const html = entries.map(renderAuditEntry).join('');
  if (reset) el.innerHTML = html || '<div class="empty-hint">Nothing recorded yet.</div>';
  else el.insertAdjacentHTML('beforeend', html);
  if (reset || entries.length) _auditOldest = entries.length ? entries[entries.length - 1].id : null;
  const more = $('audit-more');
  if (more) more.style.display = entries.length === AUDIT_PAGE ? '' : 'none';
}

(function initPeople() {
  const list = $('people-list');
  if (!list) return;
  const accountIn = el => _people.accounts.find(x => x.id === Number(el?.closest('.people-item')?.dataset.id));

  list.addEventListener('click', e => {
    const toggle = e.target.closest('[data-toggle]');
    if (toggle) {
      const id = Number(toggle.dataset.toggle);
      _peopleOpen = _peopleOpen === id ? null : id;
      peopleMsg('');
      renderPeople();
      return;
    }
    const btn = e.target.closest('[data-act]');
    const a = btn && accountIn(btn);
    if (!a) return;
    const act = btn.dataset.act;
    if (act === 'close') { _peopleOpen = null; peopleMsg(''); renderPeople(); }
    else if (act === 'save') savePerson(a, btn.closest('.people-item'), btn);
    else if (act === 'signout') kickPerson(a, false);
    else if (act === 'remove') kickPerson(a, true);
    else if (act === 'transfer') transferOwner(a);
  });

  // A new level brings its own description and the grants that fit it.
  list.addEventListener('change', e => {
    const sel = e.target.closest('select[data-f="level"]');
    const a = sel && accountIn(sel);
    if (!a) return;
    const item = sel.closest('.people-item');
    item.querySelector('[data-f="level-hint"]').textContent = LEVEL_INFO[sel.value] || '';
    item.querySelector('[data-f="grants"]').innerHTML = renderGrantBoxes(a, sel.value);
  });

  list.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.target.matches('.people-edit input[data-f]')) {
      e.target.closest('.people-edit').querySelector('[data-act="save"]')?.click();
    }
  });

  const form    = $('people-add-form');
  const nameIn  = $('people-add-name');
  const pinIn   = $('people-add-pin');
  const levelIn = $('people-add-level');
  const saveBtn = $('people-save-btn');
  const closeAdd = () => { form.style.display = 'none'; nameIn.value = ''; pinIn.value = ''; };

  $('people-add-btn')?.addEventListener('click', () => {
    peopleMsg('');
    if (form.style.display === 'none') { form.style.display = ''; nameIn.focus(); }
    else closeAdd();
  });
  $('people-cancel-btn')?.addEventListener('click', () => { closeAdd(); peopleMsg(''); });
  levelIn?.addEventListener('change', () => { $('people-add-hint').textContent = LEVEL_INFO[levelIn.value] || ''; });
  saveBtn?.addEventListener('click', async () => {
    const name = nameIn.value.trim();
    const pin  = pinIn.value.trim();
    if (!name) return peopleMsg('Enter a name.', true);
    if (!/^\d{4,8}$/.test(pin)) return peopleMsg('PIN must be 4–8 digits.', true);
    saveBtn.disabled = true;
    const d = await peopleCall('/accounts', 'POST', { name, pin, level: levelIn.value });
    saveBtn.disabled = false;
    if (d) {
      closeAdd();
      peopleMsg(`${d.account.name} can now sign in with their PIN, as ${levelName(d.account.level)}.`);
      loadPeople();
    }
    loadAudit(true);
  });

  $('audit-refresh')?.addEventListener('click', () => loadAudit(true));
  $('audit-more')?.addEventListener('click', () => loadAudit(false));
})();

// System control (owner only) — clean restart / shutdown with progress overlay
(function initSystemControl() {
  const softBtn = $('sys-restart-soft');
  const hardBtn = $('sys-restart-hard');
  const downBtn = $('sys-shutdown');
  if (!softBtn || !hardBtn || !downBtn) return;

  // Ordered phases per mode, so the overlay can show the whole sequence and
  // which step is running. Mirrors kai/core/lifecycle.py's _report() phases.
  const PHASE_LABEL = {
    'starting':    'Preparing',
    'draining':    'Finishing in-flight memory work',
    'sleep-cycle': 'Writing welcome-back note',
    'hq-reembed':  'Embedding new memories',
    'rebuilding':  'Reloading brains & indexes',
    'closing':     'Releasing resources',
    'done':        'Session saved',
  };
  const PHASE_ORDER = {
    'soft-restart': ['draining', 'sleep-cycle', 'rebuilding', 'done'],
    'default':      ['starting', 'draining', 'sleep-cycle', 'hq-reembed', 'closing', 'done'],
  };

  function renderSteps(mode, phase) {
    const box = document.getElementById('kai-admin-steps');
    if (!box) return;
    const order = PHASE_ORDER[mode] || PHASE_ORDER.default;
    const cur = order.indexOf(phase);
    box.innerHTML = order.map((p, i) => {
      const done = phase === 'done' || (cur >= 0 && i < cur);
      const active = i === cur && phase !== 'done';
      const icon = done ? '✓' : (active ? '◉' : '○');
      const color = done ? '#a6e3a1' : (active ? '#e67e22' : '#6c7086');
      const weight = active ? '600' : '400';
      return '<div style="display:flex;align-items:center;gap:8px;font-size:13px;'
        + 'color:' + color + ';font-weight:' + weight + ';padding:2px 0">'
        + '<span style="width:14px;text-align:center">' + icon + '</span>'
        + '<span>' + PHASE_LABEL[p] + '</span></div>';
    }).join('');
  }

  function overlay(title) {
    let o = document.getElementById('kai-admin-overlay');
    if (!o) {
      o = document.createElement('div');
      o.id = 'kai-admin-overlay';
      o.style.cssText = 'position:fixed;inset:0;z-index:99999;display:flex;align-items:center;'
        + 'justify-content:center;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);'
        + 'font-family:system-ui,sans-serif';
      o.innerHTML = '<div style="background:#1e1e2e;color:#cdd6f4;border-radius:12px;'
        + 'padding:28px 36px;min-width:340px;box-shadow:0 20px 60px rgba(0,0,0,.5)">'
        + '<div id="kai-admin-title" style="font-size:17px;font-weight:600;margin-bottom:14px;text-align:center"></div>'
        + '<div id="kai-admin-steps" style="text-align:left;margin-bottom:14px"></div>'
        + '<div id="kai-admin-detail" style="font-size:12px;color:#a6adc8;text-align:center;min-height:16px">Working…</div>'
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
        if (st.phase) renderSteps(st.mode, st.phase);
        if (d) d.textContent = st.detail || '';
        if (bar) {
          // Most phases don't report a pct; fall back to how far through the
          // step sequence we are so the bar still advances steadily.
          const order = PHASE_ORDER[st.mode] || PHASE_ORDER.default;
          const idx = order.indexOf(st.phase);
          const stepPct = idx >= 0 ? Math.round(((idx + 1) / order.length) * 100) : 0;
          const pct = Math.max(typeof st.pct === 'number' ? st.pct : 0, stepPct, 5);
          bar.style.width = pct + '%';
        }
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

// Server console (owner only) — mirrors the `python app.py` terminal output.
// Polls /api/admin/logs incrementally (only lines newer than the last seq we
// saw) and appends them to the dashboard panel. Runs only while the dashboard
// tab is open and the viewer is the owner.
const KaiConsole = (function () {
  const MAX_DOM_LINES = 600;
  let lastSeq = 0;
  let timer = null;
  let booted = false;     // cleared the "Connecting…" placeholder yet?
  let isOwner = false;

  const pre = () => document.getElementById('dash-console');

  function classify(text) {
    if (text.startsWith('[+]')) return 'ln ln-ok';
    if (text.startsWith('[!]')) return 'ln ln-warn';
    if (/\b(error|traceback|exception|failed)\b/i.test(text)) return 'ln ln-err';
    if (text.startsWith('[~]') || text.startsWith('[debug]')) return 'ln ln-dim';
    return 'ln';
  }

  function append(lines) {
    const box = pre();
    if (!box || !lines.length) return;
    const auto = document.getElementById('dash-console-autoscroll');
    const stick = !auto || auto.checked;
    const frag = document.createDocumentFragment();
    for (const ln of lines) {
      const span = document.createElement('span');
      span.className = classify(ln.text);
      span.textContent = ln.text;
      frag.appendChild(span);
    }
    box.appendChild(frag);
    while (box.childElementCount > MAX_DOM_LINES) box.removeChild(box.firstElementChild);
    if (stick) box.scrollTop = box.scrollHeight;
  }

  async function tick() {
    try {
      const r = await fetch('/api/admin/logs?after=' + lastSeq);
      if (!r.ok) return;
      const d = await r.json();
      if (!booted) { const b = pre(); if (b) b.innerHTML = ''; booted = true; }
      if (d.lines && d.lines.length) append(d.lines);
      lastSeq = d.last_seq || lastSeq;
    } catch { /* server may be restarting — keep trying */ }
  }

  return {
    start() {
      if (!isOwner || timer) return;
      tick();
      timer = setInterval(tick, 1200);
    },
    stop() {
      if (timer) { clearInterval(timer); timer = null; }
    },
    setOwner(owner) {
      isOwner = owner;
      const sec = document.getElementById('dash-console-section');
      if (sec) sec.style.display = owner ? '' : 'none';
      if (!owner) this.stop();
    },
  };
})();

// Console clear button — clears the on-screen view only (server keeps logging).
(function initConsoleControls() {
  const clearBtn = document.getElementById('dash-console-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => {
    const box = document.getElementById('dash-console');
    if (box) box.innerHTML = '';
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

function showComposerNotice(text) {
  const t = $('chat-drop-toast');
  if (!t) return;
  t.textContent = text;
  t.className = 'chat-drop-toast show';
  clearTimeout(showComposerNotice._timer);
  showComposerNotice._timer = setTimeout(() => { t.className = 'chat-drop-toast'; }, 6000);
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text) return;
  if (isStreaming) {
    // Kai's greeting is still streaming: hold the message and send it right after.
    if (_greetingActive) {
      _queuedMessage = _queuedMessage ? `${_queuedMessage}\n${text}` : text;  // never drop an earlier one
      inputEl.value = '';
      inputEl.placeholder = 'Sending as soon as Kai finishes saying hello…';
    }
    return;
  }
  if (text.length > _maxInputChars) {
    showComposerNotice(`That message is ${text.length.toLocaleString()} characters; the limit is ${_maxInputChars.toLocaleString()}. Shorten it, or attach it as a document.`);
    return;
  }

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

  const approveId = _approveCallId;  // this message is a card's Go ahead for that call
  _approveCallId = null;

  try {
    const receipts = [];  // tools that actually ran this turn, from tool_result events
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(approveId ? { message: text, confirm: approveId } : { message: text }),
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

        } else if (ev.type === 'tool_result') {
          receipts.push({ ...ev, reason: pendingReason });
          if (ev.call_id) _setCardState(ev.call_id, ev.ok ? 'done' : 'failed', ev);
          pendingReason = null;

        } else if (ev.type === 'confirm_tool') {
          console.log('[confirm_tool] received:', ev.name, ev.label);
          pendingConfirm = { name: ev.name, label: ev.label, args: ev.args || {}, diff: ev.diff || '', call_id: ev.call_id || '' };

        } else if (ev.type === 'token') {
          if (!hasTokens) { hideStatus(si); hasTokens = true; stopWakingAnimation(); setFace('responding'); }
          fullText += ev.text;
          totalTokens++;
          const tokEl = $('s-tokens');
          if (tokEl) tokEl.textContent = totalTokens.toLocaleString();
          // Strip any stray face tag from token text before display
          const cleanToken = stripFaceTags(ev.text);
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
          if (streamError && fullText) {
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
          faceOnDone(!!streamError, fullText);
          if (fullText) playTTS(fullText);
          if (receipts.length) addReceipts(content, receipts);
          if (approveId && !receipts.some(r => r.call_id === approveId)) {
            _setCardState(approveId, streamError ? 'interrupted' : 'stale');
          }
          if (fullText && ev.grounding && !pendingConfirm) addProvenance(content, ev.grounding);
          if (ev.message_id && fullText) addFeedbackBar(content.closest('.bubble'), ev.message_id, fullText);
          if (ev.latency_ms != null && fullText) addLatencyBadge(content.closest('.bubble'), ev.latency_ms);
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
  if (approveId) _setCardState(approveId, 'interrupted');  // only a card still "approved" changes
  syncApprovalCards();
  if (inputEl) inputEl.focus();
  // Refresh sidebar stats after each turn
  loadInfo();
}

// ── Receipts and provenance ──────────────────────────────────────────────────
// Receipts come from tool_result events, so only tools that actually ran appear (T2-23);
// the same data is saved with the message and redrawn when a chat is reopened (T2-28).

function _argText(args) {
  return Object.entries(args || {})
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' ');
}

function addReceipts(content, receipts) {
  const el = document.createElement('div');
  el.className = 'receipts';
  el.innerHTML = receipts.map(r => {
    const ms = Number(r.duration_ms) || 0;
    const dur = ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
    const body = r.ok ? r.output : r.error;
    return `<details class="receipt ${r.ok ? 'ok' : 'failed'}">` +
      `<summary><span class="receipt-glyph">${r.ok ? '\u2713' : '\u2715'}</span>` +
      `<code class="receipt-name">${esc(r.name)}</code>` +
      `<code class="receipt-args">${esc(_argText(r.args))}</code>` +
      `<span class="receipt-dur">${dur}</span></summary>` +
      (r.reason ? `<div class="receipt-reason">${esc(r.reason)}</div>` : '') +
      (body ? `<pre class="receipt-output">${esc(body)}</pre>` : '') +
      `</details>`;
  }).join('');
  content.parentNode.insertBefore(el, content);
  const files = _designFiles(receipts);
  if (files) content.parentNode.insertBefore(files, content);
  scrollEnd();
}

// Files a tool made (design.render, design.spreadsheet): a thumbnail that opens the file,
// and a link per file. Built from receipts, so only files a tool actually made appear.
function _designFiles(receipts) {
  const made = receipts.filter(r => r.ok && Array.isArray(r.files) && r.files.length);
  if (!made.length) return null;
  const el = document.createElement('div');
  el.className = 'design-files';
  el.innerHTML = made.map(r => {
    const main = r.files.filter(f => !f.preview);
    const open = (main[0] || r.files[0]).url;
    const thumbs = r.files.filter(f => f.preview || /\.png(\?|$)/i.test(f.url)).slice(0, 8);
    return thumbs.map(f =>
      `<a class="design-thumb" href="${esc(f.preview ? open : f.url)}" target="_blank" rel="noopener">` +
      `<img src="${esc(f.url)}" alt="${esc(f.name)}" loading="lazy"></a>`).join('') +
      `<div class="design-links">` + main.map(f =>
        `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.name)}</a>`).join('') +
      `</div>`;
  }).join('');
  return el;
}

const _PROVENANCE = {
  grounded:    ['\u2713', 'Grounded', g => `${g.traced} of ${g.checked} numbers trace to tool output.`],
  partial:     ['\u25D0', 'Partly grounded', g => `${g.untraced.length} of ${g.checked} numbers don't appear in any tool output: ${g.untraced.join(', ')}.`],
  ran:         ['\u2713', 'From tool output', () => 'A tool ran; this answer has no numbers to check.'],
  unchecked:   ['\u25CB', 'Not checked this turn', () => 'No tool ran. This comes from memory or earlier in the chat.'],
  tool_failed: ['\u2715', 'Tool failed', g => `${(g.failed || []).join(', ')} didn't succeed, so no result backs this answer.`],
  unbacked:    ['\u26A0', 'Unbacked claim', () => 'This answer describes work that no tool reported doing.'],
};

function addProvenance(content, g) {
  const spec = g && _PROVENANCE[g.state];
  if (!spec) return;
  const el = document.createElement('div');
  el.className = `provenance prov-${g.state}`;
  el.innerHTML = `<span class="prov-badge">${spec[0]} ${spec[1]}</span><span class="prov-detail">${esc(spec[2](g))}</span>`;
  content.parentNode.insertBefore(el, content.nextSibling);
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

// Show how long Kai took to generate this reply, as a subtle badge.
function addLatencyBadge(bubble, ms) {
  if (bubble == null || ms == null) return;
  if (bubble.querySelector('.latency-badge')) return;  // don't double up
  const badge = document.createElement('span');
  badge.className = 'latency-badge';
  const label = ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
  badge.textContent = `⏱ ${label}`;
  badge.title = 'Response time';
  bubble.appendChild(badge);
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

  const argText = Object.entries(confirm.args || {})
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' ');
  const shownArgs = argText.length > 200 ? argText.slice(0, 200) + '…' : argText;
  const bar = document.createElement('div');
  bar.className = 'confirm-bar';
  if (confirm.call_id) bar.dataset.callId = confirm.call_id;
  bar.dataset.label = confirm.label || confirm.name;
  bar.innerHTML = `
    <div class="confirm-what">Waiting for your OK — nothing has run yet: <strong>${esc(confirm.label || confirm.name)}</strong>
      <code>${esc(confirm.name)}${shownArgs ? ' ' + esc(shownArgs) : ''}</code></div>
    <button class="confirm-btn confirm-go">Go ahead</button>
    <button class="confirm-btn confirm-skip">Skip</button>
  `;
  bubble.appendChild(bar);

  bar.querySelector('.confirm-go').addEventListener('click', () => {
    if (isStreaming) {
      showComposerNotice('Kai is still replying. Approve once this reply finishes.');
      return;
    }
    _setCardState(bar, 'approved');
    // Send "go ahead" as a new user message, naming the call this card approves
    _approveCallId = confirm.call_id || null;
    if (inputEl) inputEl.value = 'go ahead';
    sendMessage();
  });
  bar.querySelector('.confirm-skip').addEventListener('click', async () => {
    bar.querySelectorAll('button').forEach(b => { b.disabled = true; });
    let status = 0;
    try {
      status = (await fetch('/chat/confirm/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(confirm.call_id ? { call_id: confirm.call_id } : {}),
      })).status;
    } catch {}
    _setCardState(bar, status === 200 ? 'skipped' : status === 409 ? 'stale' : 'unreachable');
  });
}

// The approval card's states: waiting → approved (running) → done or failed, or skipped.
// "stale" is a card whose call is no longer the one waiting on the server.
function _setCardState(barOrId, state, receipt) {
  const bar = typeof barOrId === 'string'
    ? document.querySelector(`.confirm-bar[data-call-id="${CSS.escape(barOrId)}"]`)
    : barOrId;
  if (!bar) return;
  // A stream that ends without word on the call only settles a card still marked approved.
  if (state === 'interrupted' && !bar.classList.contains('card-approved')) return;
  const label = `<strong>${esc(bar.dataset.label || '')}</strong>`;
  const text = {
    approved:    `Approved — running ${label}…`,
    done:        `\u2713 Done — ${label} ran.`,
    failed:      `\u2715 Failed — ${label}: ${esc((receipt && receipt.error) || 'the tool reported an error')}`,
    skipped:     `Skipped — ${label} did not run.`,
    stale:       `No longer waiting — ${label} did not run.`,
    interrupted: `Approved — the reply stopped before Kai reported whether ${label} ran.`,
    unreachable: 'Could not reach Kai to cancel. Nothing runs unless you say \u201cgo ahead\u201d.',
  }[state];
  if (!text) return;
  bar.className = `confirm-bar card-${state}`;
  bar.innerHTML = `<div class="confirm-what">${text}</div>`;
}

// The server keeps at most one call waiting, and cards follow it. A waiting card whose call was
// dropped (another message was sent) says so; after a reload the waiting call gets its card back.
async function syncApprovalCards({ restore = false } = {}) {
  const waiting = [...document.querySelectorAll('.confirm-bar[data-call-id]:not([class*="card-"])')];
  if (!waiting.length && !restore) return;
  let p;
  try { p = (await fetch('/chat/pending').then(r => r.json())) || {}; } catch { return; }
  for (const bar of waiting) if (bar.dataset.callId !== p.call_id) _setCardState(bar, 'stale');
  if (!restore || !p.call_id || isStreaming) return;
  if (document.querySelector(`.confirm-bar[data-call-id="${CSS.escape(p.call_id)}"]`)) return;
  const { si, content } = addKaiBubble();
  hideStatus(si);
  content.textContent = 'This is still waiting for your OK from before:';
  addConfirmBar(content.closest('.bubble'), p);
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
  _greetingActive = true;
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
          const cleanToken = stripFaceTags(ev.text);
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
          faceOnDone(false, fullText);
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
    _greetingActive = false;
    if (sendBtn) sendBtn.disabled = false;
    startIdleBlink();
    syncApprovalCards({ restore: true });
    if (_queuedMessage && inputEl) {
      inputEl.value = _queuedMessage;
      inputEl.placeholder = 'Message Kai…';
      _queuedMessage = null;
      sendMessage();
    }
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

const ttsIcon = $('ttsIcon');
function _applyTtsState() {
  if (!ttsBtn) return;
  if (_ttsEnabled) {
    ttsBtn.classList.add('tts-on');
    ttsBtn.title = 'Voice output ON — click to disable';
    if (ttsIcon) ttsIcon.textContent = 'volume_up';
  } else {
    ttsBtn.classList.remove('tts-on');
    ttsBtn.title = 'Voice output OFF — click to enable';
    if (ttsIcon) ttsIcon.textContent = 'volume_off';
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
    micIcon.textContent = 'mic_off';
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
        micIcon.textContent = 'mic';
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
      micIcon.textContent = 'mic';
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
  document.querySelectorAll('.mode-option[data-mode]').forEach(btn => {
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

// Mode option buttons. Preset options share the class, so match data-mode (T2-17).
document.querySelectorAll('.mode-option[data-mode]').forEach(btn => {
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
    desc.textContent = !lv.installed
      ? `not installed — run: ollama pull ${lv.model}`
      : (lv.fits === false ? lv.note : lv.blurb);
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
  if (!showing) {
    _fillAdvancedInputs();
    const msg = $('preset-adv-msg');
    if (msg) msg.textContent = '';
  }
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
    const msg = $('preset-adv-msg');
    const clamped = Object.entries(d.clamped || {});
    if (clamped.length) {
      // Keep the panel open so the adjusted value is seen (T2-19).
      _fillAdvancedInputs();
      if (msg) msg.textContent = 'Saved, but out of range: ' +
        clamped.map(([k, v]) => `${PRESET_LABELS[k] || k} was set to ${Number(v).toFixed(2)}`).join(', ') + '.';
      return;
    }
    if (msg) msg.textContent = '';
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

// ── Tools on/off (Settings → Tools) ──────────────────────────────────────────
function _catLabel(cat) {
  const s = String(cat).replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

async function loadToolsSettings() {
  const el = $('tools-list');
  if (!el) return;
  try {
    const d = await fetch('/settings/tools').then(r => r.json());
    renderToolsSettings(d);
  } catch {
    el.innerHTML = '<div class="empty-hint">Couldn\'t load tools.</div>';
  }
}

function renderToolsSettings(d) {
  const el = $('tools-list');
  if (!el) return;
  const groups = d.groups || [];
  const badge = $('tools-count');
  if (badge) badge.textContent = `${d.enabled || 0}/${d.total || 0} on`;

  el.innerHTML = groups.map(g => {
    const on = g.tools.filter(t => t.enabled).length;
    const rows = g.tools.map(t => `
      <label class="tool-row">
        <span class="tool-row-info">
          <span class="tool-row-name">${esc(t.name)}</span>
          <span class="tool-row-desc">${esc(t.label)}</span>
        </span>
        <span class="tool-row-right">
          <span class="tool-risk r-${esc(t.risk)}">${esc(t.risk)}</span>
          <span class="switch">
            <input type="checkbox" class="tool-toggle" data-name="${esc(t.name)}" ${t.enabled ? 'checked' : ''}>
            <span class="slider"></span>
          </span>
        </span>
      </label>`).join('');
    return `<details class="tool-group">
      <summary class="tool-group-hdr">
        <span class="tool-group-name">${esc(_catLabel(g.category))}</span>
        <span class="tool-group-count">${on}/${g.tools.length}</span>
      </summary>
      ${rows}
    </details>`;
  }).join('');

  el.querySelectorAll('.tool-toggle').forEach(cb => {
    cb.addEventListener('change', async () => {
      const name = cb.dataset.name;
      const enabled = cb.checked;
      cb.disabled = true;
      try {
        const r = await fetch('/settings/tools', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, enabled }),
        });
        if (!r.ok) throw new Error('bad status');
        _refreshToolCounts();
      } catch {
        cb.checked = !enabled;   // revert on failure
      } finally {
        cb.disabled = false;
      }
    });
  });
}

function _refreshToolCounts() {
  const el = $('tools-list');
  if (!el) return;
  let total = 0, enabled = 0;
  el.querySelectorAll('.tool-group').forEach(g => {
    const cbs = g.querySelectorAll('.tool-toggle');
    const on = [...cbs].filter(c => c.checked).length;
    total += cbs.length; enabled += on;
    const c = g.querySelector('.tool-group-count');
    if (c) c.textContent = `${on}/${cbs.length}`;
  });
  const badge = $('tools-count');
  if (badge) badge.textContent = `${enabled}/${total} on`;
}

// ── Recipes (chain existing tools into a skill.<name>) ───────────────────────
const _RECIPE_HELP = "One tool per line. Name a step to reuse its output: 'disk = files.disk_usage path=/' then 'files.read path={{disk}}'. Steps that don't reference each other run in parallel.";

function _recipeHint(msg, isError) {
  const e = $('recipe-error');
  if (!e) return;
  e.textContent = msg;
  e.classList.toggle('error', !!isError);
}

function _toggleRecipeForm(show) {
  const f = $('recipe-form');
  if (!f) return;
  f.style.display = show ? 'flex' : 'none';
  if (show) {
    $('recipe-name').value = '';
    $('recipe-desc').value = '';
    $('recipe-triggers').value = '';
    $('recipe-steps').value = '';
    _recipeHint(_RECIPE_HELP, false);
    $('recipe-name').focus();
  }
}

async function loadRecipes() {
  const el = $('recipe-list');
  if (!el) return;
  try {
    const d = await fetch('/settings/recipes').then(r => r.json());
    renderRecipes(d.recipes || []);
  } catch { el.innerHTML = '<div class="empty-hint">Couldn\'t load recipes.</div>'; }
}

function renderRecipes(recipes) {
  const el = $('recipe-list');
  if (!el) return;
  const badge = $('recipes-count');
  if (badge) badge.textContent = recipes.length;
  if (!recipes.length) { el.innerHTML = '<div class="empty-hint">No recipes yet.</div>'; return; }
  el.innerHTML = recipes.map(r => `
    <div class="recipe-item">
      <div class="recipe-item-info">
        <span class="recipe-item-name">${esc(r.name)}</span>
        <span class="recipe-item-steps">${esc((r.steps || []).join(' → '))}</span>
      </div>
      <button class="recipe-del" data-name="${esc(r.name)}" title="Delete recipe">&times;</button>
    </div>`).join('');
  el.querySelectorAll('.recipe-del').forEach(b => {
    b.addEventListener('click', async () => {
      if (!confirm(`Delete recipe "${b.dataset.name}"?`)) return;
      try {
        await fetch(`/settings/recipes/${encodeURIComponent(b.dataset.name)}`, { method: 'DELETE' });
        loadRecipes();
      } catch { /* ignore */ }
    });
  });
}

async function saveRecipe() {
  const name = $('recipe-name').value.trim();
  const description = $('recipe-desc').value.trim();
  const triggers = $('recipe-triggers').value.split(',').map(s => s.trim()).filter(Boolean);
  const steps = $('recipe-steps').value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!name) { _recipeHint('Give the recipe a name.', true); return; }
  if (!description) { _recipeHint('Say what the recipe does. Kai reads it to decide when to use it.', true); return; }
  if (!steps.length) { _recipeHint('Add at least one tool (one per line).', true); return; }
  try {
    const r = await fetch('/settings/recipes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description, triggers, steps }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      _recipeHint(d.detail || 'Could not save recipe.', true);
      return;
    }
    _toggleRecipeForm(false);
    loadRecipes();
  } catch { _recipeHint('Could not save recipe.', true); }
}

// ── Settings hub (drill-down navigation) ─────────────────────────────────────
function openSettingsSub(name) {
  const hub = $('settings-hub');
  if (hub) hub.style.display = 'none';
  document.querySelectorAll('.settings-sub').forEach(s => {
    s.style.display = (s.id === 'settings-sub-' + name) ? 'block' : 'none';
  });
  // Refresh the data the opened page shows.
  if (name === 'brains') { loadModels(); loadToolLevel(); loadToolsSettings(); }
  else if (name === 'skills') loadRecipes();
  else if (name === 'notes') loadNotesSettings();
  else if (name === 'people') { peopleMsg(''); loadPeople(); loadAudit(true); }
  const pane = $('panel-settings');
  if (pane) pane.scrollTop = 0;
}

function settingsBackToHub() {
  document.querySelectorAll('.settings-sub').forEach(s => { s.style.display = 'none'; });
  const hub = $('settings-hub');
  if (hub) hub.style.display = 'flex';
  const pane = $('panel-settings');
  if (pane) pane.scrollTop = 0;
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
        const r = await fetch(`/settings/models/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          alert(d.detail || 'Could not remove the model.');
        }
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
        const replyContent = wrap.querySelector('.content');
        if (m.meta && m.meta.receipts && m.meta.receipts.length) addReceipts(replyContent, m.meta.receipts);
        if (m.meta && m.meta.grounding) addProvenance(replyContent, m.meta.grounding);
        if (m.latency_ms != null) addLatencyBadge(wrap.querySelector('.bubble'), m.latency_ms);
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
_currentUser = { name: localStorage.getItem('kai_last_user') || '' };  // /info fills in the real name
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
$('tools-refresh')?.addEventListener('click', loadToolsSettings);
$('recipe-new-btn')?.addEventListener('click', () => _toggleRecipeForm(true));
$('recipe-cancel-btn')?.addEventListener('click', () => _toggleRecipeForm(false));
$('recipe-save-btn')?.addEventListener('click', saveRecipe);
$('privacy-export-btn')?.addEventListener('click', () => { window.location.href = '/users/account/export'; });
$('privacy-delete-btn')?.addEventListener('click', async () => {
  const msg = $('privacy-msg');
  const name = (_currentUser && _currentUser.name) || '';
  const typed = prompt(`This permanently deletes the account "${name}" and everything Kai remembers for it. Note files already written to disk are left where they are.\n\nType the account name to confirm.`);
  if (typed === null) return;
  if (!name || typed.trim() !== name) { if (msg) msg.textContent = 'The name didn’t match — nothing was deleted.'; return; }
  const r = await fetch('/users/account', { method: 'DELETE' }).catch(() => null);
  if (r && r.ok) window.location.href = '/login';
  else if (msg) msg.textContent = 'Could not delete the account.';
});
document.querySelectorAll('.settings-nav-item').forEach(b =>
  b.addEventListener('click', () => openSettingsSub(b.dataset.sub)));
document.querySelectorAll('.settings-back').forEach(b =>
  b.addEventListener('click', settingsBackToHub));

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


// ── Notes ────────────────────────────────────────────────────────────────────
// Browser for notes Kai saved (notes.save), plus the git repo they mirror into.
// The DB is the listing source; `on_disk` says whether the .md/.txt pair exists,
// which is false for notes saved before file mirroring landed until Export runs.

let _notes = [];
let _noteSel = null;

function _notesMsg(el, text, isErr) {
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('err', !!isErr);
  el.style.display = text ? 'block' : 'none';
}

async function loadNotes() {
  const list = $('notes-list');
  if (!list) return;
  try {
    const data = await fetch('/api/notes').then(r => r.json());
    _notes = data.notes || [];
    const loc = $('notes-loc');
    if (loc) loc.textContent = data.dir || '';
    renderNotesList();
  } catch {
    list.innerHTML = '<div class="notes-empty" style="padding:16px">Could not load notes.</div>';
  }
}

function renderNotesList(filter) {
  const list = $('notes-list');
  if (!list) return;
  const q = (filter || '').trim().toLowerCase();
  const rows = q
    ? _notes.filter(n => ((n.title || '') + ' ' + (n.preview || '')).toLowerCase().includes(q))
    : _notes;

  if (!rows.length) {
    list.innerHTML = `<div class="notes-empty" style="padding:16px">${q ? 'No notes match.' : 'No notes yet. Ask Kai to take one.'}</div>`;
    return;
  }

  list.innerHTML = rows.map(n => {
    const title = esc(n.title || (n.preview || '').slice(0, 40) || 'Untitled');
    const dot = n.on_disk
      ? '<span class="notes-dot" title="Written to disk">&#9679;</span>'
      : '<span class="notes-dot-off" title="Not on disk yet — run Export all">&#9675;</span>';
    const active = n.id === _noteSel ? ' active' : '';
    return `<button class="notes-item${active}" data-id="${esc(n.id)}">
      <div class="notes-item-title">${title}</div>
      <div class="notes-item-meta">${dot} ${esc((n.timestamp || '').slice(0, 10))} · ${n.chars} chars</div>
    </button>`;
  }).join('');

  list.querySelectorAll('.notes-item').forEach(b =>
    b.addEventListener('click', () => openNote(b.dataset.id)));
}

async function openNote(id) {
  const view = $('notes-view');
  if (!view) return;
  _noteSel = id;
  renderNotesList($('notes-search')?.value);
  try {
    const n = await fetch(`/api/notes/${encodeURIComponent(id)}`).then(r => {
      if (!r.ok) throw new Error('not found');
      return r.json();
    });
    const meta = _notes.find(x => x.id === id) || {};
    const ts = (n.timestamp || '').replace('T', ' ').slice(0, 16);
    const files = meta.md
      ? `<div class="notes-view-files">
           <div class="settings-label" style="margin-bottom:6px">Files</div>
           <div class="notes-path">${esc(meta.md)}</div>
           <div class="notes-path" style="margin-top:4px">${esc(meta.txt)}</div>
           ${meta.on_disk ? '' : '<div class="settings-nav-sub" style="margin-top:6px">Not written yet — use “Export all”.</div>'}
         </div>`
      : '';
    view.innerHTML = `
      <div class="notes-view-title">${esc(n.title || 'Untitled')}</div>
      <div class="notes-view-meta">${esc(ts)} · id ${esc(n.id)}</div>
      <div class="notes-view-body">${esc(n.content)}</div>
      ${files}
      <div style="margin-top:12px"><button class="settings-view-btn notes-delete-btn">Delete note</button></div>`;
    view.querySelector('.notes-delete-btn')?.addEventListener('click', async () => {
      if (!confirm('Delete this note? Its .md and .txt files are removed too.')) return;
      const r = await fetch(`/api/notes/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => null);
      if (r && r.ok) {
        _noteSel = null;
        view.innerHTML = '<div class="notes-empty">Note deleted.</div>';
        loadNotes();
      } else {
        view.insertAdjacentHTML('beforeend', '<div class="notes-empty">Could not delete that note.</div>');
      }
    });
  } catch {
    view.innerHTML = '<div class="notes-empty">Could not open that note.</div>';
  }
}

async function _notesPost(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || 'Request failed.');
  return d;
}

async function exportNotes(msgEl) {
  _notesMsg(msgEl, 'Exporting…');
  try {
    const d = await _notesPost('/api/notes/export');
    const failed = d.failed && d.failed.length ? `\n${d.failed.length} failed: ${d.failed[0]}` : '';
    _notesMsg(msgEl, `Wrote ${d.written} note(s) to ${d.dir}${failed}`, !d.ok);
    loadNotes();
  } catch (e) { _notesMsg(msgEl, e.message, true); }
}

async function commitNotes(msgEl) {
  _notesMsg(msgEl, 'Committing…');
  try {
    const d = await _notesPost('/api/notes/commit', {});
    _notesMsg(msgEl, d.result, false);
    loadNotes();
  } catch (e) { _notesMsg(msgEl, e.message, true); }
}

// ── Notes settings page ──────────────────────────────────────────────────────

async function loadNotesSettings() {
  try {
    const d = await fetch('/api/notes/repo/status').then(r => r.json());
    const dir = $('notes-set-dir');
    if (dir) dir.textContent = `${d.notes_dir}  (${d.file_count} files)`;

    const input = $('notes-repo-input');
    if (input) input.value = d.repo || d.configured_path || '';

    const branch = $('notes-set-branch');
    if (branch) {
      branch.textContent = d.repo
        ? `${d.branch || '?'} · ${d.pending ? d.pending + ' uncommitted' : 'clean'}`
        : '';
    }
    _notesMsg($('notes-repo-msg'),
      d.repo_missing ? `Configured repo is missing or no longer a git repo: ${d.configured_path}` : '',
      true);
  } catch { /* settings page is read-only decoration until the user acts */ }
}

async function saveNotesRepo() {
  const msg = $('notes-repo-msg');
  const path = ($('notes-repo-input')?.value || '').trim();
  _notesMsg(msg, 'Checking…');
  try {
    const d = await _notesPost('/api/notes/repo', { path });
    // Refresh first — loadNotesSettings clears the message box, so the
    // confirmation has to be written after it, not before.
    await loadNotesSettings();
    _notesMsg(msg, `Notes will be saved to ${d.notes_dir}`, false);
    loadNotes();
  } catch (e) { _notesMsg(msg, e.message, true); }
}

async function clearNotesRepo() {
  const msg = $('notes-repo-msg');
  try {
    const r = await fetch('/api/notes/repo', { method: 'DELETE' });
    const d = await r.json();
    const input = $('notes-repo-input');
    if (input) input.value = '';
    await loadNotesSettings();
    _notesMsg(msg, `Repo cleared. Notes go to ${d.notes_dir}`, false);
    loadNotes();
  } catch { _notesMsg(msg, 'Could not clear the repo.', true); }
}

$('notes-search')?.addEventListener('input', e => renderNotesList(e.target.value));
$('notes-export-btn')?.addEventListener('click', () => exportNotes($('notes-msg')));
$('notes-commit-btn')?.addEventListener('click', () => commitNotes($('notes-msg')));
$('notes-repo-save')?.addEventListener('click', saveNotesRepo);
$('notes-repo-clear')?.addEventListener('click', clearNotesRepo);
$('notes-set-export')?.addEventListener('click', () => exportNotes($('notes-set-msg')));
$('notes-set-commit')?.addEventListener('click', () => commitNotes($('notes-set-msg')));
