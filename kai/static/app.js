// ══════════════════════════════════════════════════════════════════════════════
// Kai — app.js
// Main JavaScript for the tabbed web UI.
// Handles: tab switching, dashboard, chat SSE streaming, settings,
//          memory browser, documents, session history, DM mode, Kai face.
// ══════════════════════════════════════════════════════════════════════════════

// ── 1. Config & Globals ─────────────────────────────────────────────────────

marked.setOptions({ breaks: true, gfm: true });

const $ = id => document.getElementById(id);

const messagesEl = $('messages');
const inputEl    = $('input');
const sendBtn    = $('sendBtn');
const welcomeEl  = $('welcome');

let isStreaming   = false;
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
  if (tabName === 'chat') {
    if (inputEl) inputEl.focus();
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

// ── 3. Dashboard ─────────────────────────────────────────────────────────────

async function loadDashboard() {
  try {
    const stats = await fetch('/dashboard/stats').then(r => r.json());
    if (stats) {
      const df = $('dash-facts');
      const ds = $('dash-sessions');
      const dd = $('dash-documents');
      const dn = $('dash-notes');
      if (df) df.textContent = stats.facts;
      if (ds) ds.textContent = stats.sessions;
      if (dd) dd.textContent = stats.documents;
      if (dn) dn.textContent = stats.notes;
    }
  } catch { /* ignore */ }
  // Also refresh recent sessions
  loadSessions();
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

const FACES = {
  idle:       '( ^ \u203F ^ )',
  blink:      '( - \u203F - )',
  waking:     '( ~ \u1D17 ~ )',
  thinking:   '( \u00B0 ~ \u00B0 )?',
  working:    '( > _ < ) !',
  responding: '( \u30FB\u1D17\u30FB )',
  done:       '( \u25D5 \u203F \u25D5 )',
  error:      '( x _ x )',
};

const COMPACT_FACES = {
  idle:       '^\u203F^',
  blink:      '-\u203F-',
  waking:     '~\u1D17~',
  thinking:   '\u00B0~\u00B0',
  working:    '>_<',
  responding: '\u00B7\u1D17\u00B7',
  done:       '\u25D5\u203F\u25D5',
  error:      'x_x',
};

const faceEl = $('kai-face');
let _blinkTimer   = null;
let _doneTimer    = null;
let currentAvatar = null;   // avatar element of the active response bubble

function setFace(state) {
  if (!faceEl) return;
  faceEl.style.opacity = '0';
  setTimeout(() => {
    faceEl.textContent   = FACES[state] ?? FACES.idle;
    faceEl.style.opacity = '1';
  }, 120);
  // Mirror compact version into the active bubble avatar
  if (currentAvatar) {
    currentAvatar.style.opacity = '0';
    setTimeout(() => {
      if (currentAvatar) {
        currentAvatar.textContent   = COMPACT_FACES[state] ?? COMPACT_FACES.idle;
        currentAvatar.style.opacity = '1';
      }
    }, 120);
  }
}

function startIdleBlink() {
  stopIdleBlink();
  function scheduleBlink() {
    _blinkTimer = setTimeout(() => {
      if (faceEl && faceEl.textContent === FACES.idle) {
        faceEl.textContent = FACES.blink;
        setTimeout(() => {
          if (faceEl && faceEl.textContent === FACES.blink) {
            faceEl.textContent = FACES.idle;
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

// ── Waking up animation ──────────────────────────────────────────────────────

let _wakingActive = false;

const WAKING_FRAMES = [
  ['( \u2500 _ \u2500 )', '\u2500_\u2500'],   // heavy-lidded
  ['( \u2500 _ \u2500 )', '\u2500_\u2500'],   // hold
  ['( - _ - )',           '-_-'],              // eyes shutting
  ['( > _ < )',           '>_<'],              // rubbing
  ['( > ~ < )',           '>~<'],              // really rubbing
  ['( > _ < )',           '>_<'],              // rubbing
  ['( \u00B0 _ \u00B0 )', '\u00B0_\u00B0'],   // eyes snap open
  ['( \u00B0 ~ \u00B0 )', '\u00B0~\u00B0'],   // blinking it off
  ['( ~ \u1D17 ~ )',      '~\u1D17~'],         // almost there
];

async function playWakingAnimation() {
  _wakingActive = true;
  while (_wakingActive) {
    for (const [full, compact] of WAKING_FRAMES) {
      if (!_wakingActive) break;
      if (faceEl) { faceEl.style.opacity = '1'; faceEl.textContent = full; }
      if (currentAvatar) { currentAvatar.style.opacity = '1'; currentAvatar.textContent = compact; }
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
  } catch { /* ignore */ }
}

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

function setStatus(si, text) {
  if (si && si.isConnected) si.querySelector('.status-text').textContent = text;
}

function hideStatus(si) {
  if (si && si.isConnected) si.style.display = 'none';
}

function appendText(content, token) {
  content.textContent += token;
  scrollEnd();
}

function renderMarkdown(content, text) {
  content.innerHTML = marked.parse(text);
  scrollEnd();
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || isStreaming) return;

  isStreaming       = true;
  sendBtn.disabled  = true;
  inputEl.value     = '';
  inputEl.style.height = 'auto';
  thinkStart = Date.now();
  stopIdleBlink();

  const isFirstMsg = messageCount === 0;
  setFace(isFirstMsg ? 'waking' : 'thinking');

  addUserBubble(text);
  const { si, content } = addKaiBubble();

  let fullText      = '';
  let hasTokens     = false;
  let statusLog     = [];
  let pendingReason = null;

  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
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

        } else if (ev.type === 'think') {
          if (!hasTokens) { hideStatus(si); stopWakingAnimation(); }
          const thinkEl = document.createElement('details');
          thinkEl.className = 'think-block';
          thinkEl.innerHTML = `<summary>Reasoning</summary><pre>${esc(ev.text)}</pre>`;
          content.parentNode.insertBefore(thinkEl, content);
          scrollEnd();

        } else if (ev.type === 'token') {
          if (!hasTokens) { hideStatus(si); hasTokens = true; stopWakingAnimation(); setFace('responding'); }
          fullText += ev.text;
          totalTokens++;
          const tokEl = $('s-tokens');
          if (tokEl) tokEl.textContent = totalTokens.toLocaleString();
          appendText(content, ev.text);

        } else if (ev.type === 'done') {
          if (fullText) renderMarkdown(content, fullText);
          else if (!hasTokens) hideStatus(si);
          // Record think time
          if (thinkStart) {
            thinkTimes.push(Date.now() - thinkStart);
            thinkStart = 0;
            const avg = thinkTimes.reduce((a, b) => a + b, 0) / thinkTimes.length;
            const el = $('s-think');
            if (el) el.textContent = (avg / 1000).toFixed(1) + 's';
          }
          faceOnDone(false);
          if (statusLog.length > 0) addActivityLog(content, statusLog);
          if (ev.message_id && fullText) addFeedbackBar(content.closest('.bubble'), ev.message_id, fullText);
          break;

        } else if (ev.type === 'error') {
          hideStatus(si);
          content.textContent = '\u26A0 ' + ev.text;
          faceOnDone(true);
        }
      }
    }
  } catch (err) {
    hideStatus(si);
    content.textContent = '\u26A0 Connection error: ' + err.message;
    faceOnDone(true);
  }

  isStreaming       = false;
  sendBtn.disabled  = false;
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

// ── Suggestion chips ─────────────────────────────────────────────────────────

function useSuggestion(el) {
  if (inputEl) inputEl.value = el.textContent;
  switchTab('chat');
  sendMessage();
}

// Bind suggestion click handlers (no inline onclick in new HTML)
document.querySelectorAll('.suggestion').forEach(chip => {
  chip.addEventListener('click', () => useSuggestion(chip));
});

// ── Clear chat / New chat ────────────────────────────────────────────────────

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
  });
}

// ── Input handling ───────────────────────────────────────────────────────────

if (inputEl) {
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + 'px';
  });
}

if (sendBtn) {
  sendBtn.addEventListener('click', sendMessage);
}

// ── 7. Response Mode ─────────────────────────────────────────────────────────

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

let _dmMode     = false;
let _dmCampaign = null;

async function setMode(mode) {
  const labels = {
    short: 'Short answers', long: 'Long answers',
    chat: 'Just chatting', research: 'Research', dm: 'DM Mode',
  };
  const pill     = $('mode-pill');
  const dropdown = $('mode-dropdown');
  if (pill)     pill.classList.remove('open');
  if (dropdown) dropdown.classList.remove('open');

  if (mode === 'dm') {
    if (!_dmMode) {
      openDmDialog();
    }
    return;
  }

  // Leaving DM mode
  if (_dmMode) await stopDmMode();

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

// ── 8. Think Toggle ──────────────────────────────────────────────────────────

async function loadThink() {
  try {
    const d = await fetch('/settings/think').then(r => r.json());
    _applyThink(d.think);
  } catch { /* keep default ON */ }
}

function _applyThink(on) {
  const btn    = $('think-toggle');
  const status = $('think-status');
  if (!btn) return;
  btn.classList.toggle('active', on);
  if (status) status.textContent = on ? 'ON' : 'OFF';
}

async function toggleThink() {
  try {
    const d = await fetch('/settings/think', { method: 'POST' }).then(r => r.json());
    _applyThink(d.think);
  } catch { /* ignore */ }
}

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

// Memory tab button handlers
document.querySelectorAll('.overlay-tab-btn[data-mem-tab]').forEach(btn => {
  btn.addEventListener('click', () => switchMemTab(btn.dataset.memTab));
});

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
          <div class="bubble"><div class="content">${marked.parse(m.content || '')}</div></div>
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

// ── 11. DM Mode ──────────────────────────────────────────────────────────────

async function loadDmStatus() {
  try {
    const d = await fetch('/dm/status').then(r => r.json());
    _dmMode     = d.dm_mode;
    _dmCampaign = d.campaign;
    _applyDmState();
    if (_dmMode) _applyMode('dm', 'DM Mode');
  } catch { /* ignore */ }
}

function _applyDmState() {
  const btn    = $('dm-toggle');
  const nameEl = $('dm-campaign-name');
  if (!btn) return;
  if (_dmMode && _dmCampaign) {
    btn.classList.add('active');
    if (nameEl) nameEl.textContent = _dmCampaign.name;
  } else {
    btn.classList.remove('active');
    if (nameEl) nameEl.textContent = 'Off';
  }
}

function toggleDmMode() {
  if (_dmMode) {
    stopDmMode();
  } else {
    openDmDialog();
  }
}

// DM toggle button handler
const dmToggleBtn = $('dm-toggle');
if (dmToggleBtn) {
  dmToggleBtn.addEventListener('click', toggleDmMode);
}

async function stopDmMode() {
  await fetch('/dm/stop', { method: 'POST' }).catch(() => {});
  _dmMode     = false;
  _dmCampaign = null;
  _applyDmState();
  // Revert mode pill to short
  try {
    await fetch('/settings/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'short' }),
    });
    _applyMode('short', 'Short answers');
  } catch { /* ignore */ }
}

function openDmDialog() {
  const overlay = $('dm-dialog-overlay');
  if (overlay) overlay.classList.add('open');
  const input = $('dm-campaign-input');
  if (input) { input.value = ''; setTimeout(() => input.focus(), 50); }
}

function closeDmDialog() {
  const overlay = $('dm-dialog-overlay');
  if (overlay) overlay.classList.remove('open');
}

async function confirmDmStart() {
  const name = ($('dm-campaign-input')?.value || '').trim();
  closeDmDialog();
  try {
    const res = await fetch('/dm/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ campaign_name: name }),
    }).then(r => r.json());
    _dmMode     = true;
    _dmCampaign = res.campaign;
    _applyDmState();
    // Sync mode pill to DM
    await fetch('/settings/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'dm' }),
    }).catch(() => {});
    _applyMode('dm', 'DM Mode');
  } catch { /* ignore */ }
}

// Also expose startDmMode as alias for confirmDmStart (used in app.html)
function startDmMode() {
  confirmDmStart();
}

// Enter key in campaign dialog
document.addEventListener('keydown', e => {
  const overlay = $('dm-dialog-overlay');
  if (e.key === 'Enter' && overlay && overlay.classList.contains('open')) {
    e.preventDefault();
    confirmDmStart();
  }
});

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

// ── 13. Escape Key Handler ───────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeMemoryPanel();
    closeHistoryPanel();
    closeDmDialog();
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
loadThink();
loadSessions();
loadDmStatus();
loadDocs();
loadDashboard();

// Start on dashboard tab
switchTab('dashboard');

// Start idle blink (already set up via setTimeout above)
