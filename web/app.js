/* Sherlock — Frontend App Logic */

// ── State ─────────────────────────────────────────────────────────────────────

const state = {
  token: localStorage.getItem('sherlock_token'),
  user: JSON.parse(localStorage.getItem('sherlock_user') || 'null'),
  matters: [],
  cases: [],
  activeMatterId: null,
  caseFilter: 'active',
  scope: 'all',
  queryType: localStorage.getItem('sherlock_query_type') || 'auto',
  verbosityRole: localStorage.getItem('sherlock_verbosity_role') || 'attorney',
  researchMode: false,
  researchAvailable: false,
  streaming: false,
  uploadJobs: {},
  csrfToken: null,
};

// ── API helpers ───────────────────────────────────────────────────────────────

function getCsrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : (state.csrfToken || '');
}

async function api(method, path, body = null) {
  const opts = {
    method,
    headers: {
      'Authorization': `Bearer ${state.token}`,
      'Content-Type': 'application/json',
      'X-CSRF-Token': getCsrfToken(),
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (resp.status === 401) { logout(); return null; }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

async function downloadWithAuth(path, filename) {
  try {
    const resp = await fetch(path, {
      headers: { 'Authorization': `Bearer ${state.token}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast('Download failed: ' + e.message, 'error');
  }
}

async function apiUpload(path, formData) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${state.token}`,
      'X-CSRF-Token': getCsrfToken(),
    },
    body: formData,
  });
  if (resp.status === 401) { logout(); return null; }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// ── Init ──────────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', () => {
  if (!state.token || !state.user) { window.location.href = '/login'; return; }

  document.getElementById('userPill').textContent = state.user.display_name || state.user.username;

  if (state.user.role === 'admin') {
    document.getElementById('nav-admin').classList.remove('hidden');
    document.getElementById('nav-config').classList.remove('hidden');
  }

  initTheme();
  loadMatters();
  loadCases();
  loadHistory();
  checkNasStatus();
  setInterval(checkNasStatus, 5 * 60 * 1000);
  pollIndexerStatus();
  setInterval(pollIndexerStatus, 5000);

  // Restore persisted query mode preferences
  _applyQueryType(state.queryType);
  _applyVerbosityRole(state.verbosityRole);

  // Check research mode availability
  checkResearchStatus();
});

// ── Theme ──────────────────────────────────────────────────────────────────────

const THEMES = ['formal', 'tron'];
const THEME_LABELS = { formal: '◈ Formal', tron: '⬡ Tron' };

function initTheme() {
  const saved = localStorage.getItem('sherlock_theme') || 'formal';
  applyTheme(saved);
}

function applyTheme(name) {
  document.documentElement.setAttribute('data-theme', name);
  localStorage.setItem('sherlock_theme', name);
  const btn = document.getElementById('themeToggle');
  if (btn) btn.textContent = THEME_LABELS[name] || name;
  const favicon = document.getElementById('favicon');
  if (favicon) favicon.href = name === 'tron' ? '/static/favicon-tron.svg' : '/static/favicon-formal.svg';
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'formal';
  const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
  applyTheme(next);
}

// ── NAS status banner ─────────────────────────────────────────────────────────

let _nasBannerDismissed = false;

async function checkNasStatus() {
  if (_nasBannerDismissed) return;
  try {
    const s = await api('GET', '/api/nas/status');
    const banner = document.getElementById('nasBanner');
    if (!banner) return;
    if (s.all_ok || !s.paths.length) {
      banner.classList.add('hidden');
    } else {
      const down = s.paths.filter(p => !p.accessible).map(p => p.path);
      document.getElementById('nasBannerText').textContent =
        `⚠ NAS path${down.length > 1 ? 's' : ''} not accessible: ${down.join(', ')} — indexed data may be stale.`;
      banner.classList.remove('hidden');
    }
  } catch { /* non-fatal */ }
}

function dismissNasBanner() {
  _nasBannerDismissed = true;
  document.getElementById('nasBanner').classList.add('hidden');
}

// ── Indexer status banner ─────────────────────────────────────────────────────

async function pollIndexerStatus() {
  try {
    const s = await api('GET', '/api/indexer/live-status');
    if (!s) return;
    const banner  = document.getElementById('indexBanner');
    const text    = document.getElementById('indexBannerText');
    const bar     = document.getElementById('indexBannerBar');
    const pct     = document.getElementById('indexBannerPct');
    if (s.active) {
      const done   = s.indexed || 0;
      const total  = s.total   || 0;
      const pctVal = total > 0 ? Math.round((done / total) * 100) : 0;
      const stageLabel = {
        scanning:   'Scanning files',
        extracting: 'Extracting text',
        embedding:  'Embedding chunks',
        queued:     'Queued',
      }[s.stage] || 'Indexing';
      text.textContent = total > 0
        ? `${stageLabel}… ${done} / ${total}`
        : `${stageLabel}…`;
      bar.style.width  = (total > 0 ? pctVal : 40) + '%';
      pct.textContent  = total > 0 ? pctVal + '%' : '';
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
  } catch {}
}

// ── Query toolbar ─────────────────────────────────────────────────────────────

function setQueryType(type) {
  state.queryType = type;
  localStorage.setItem('sherlock_query_type', type);
  _applyQueryType(type);
}

function _applyQueryType(type) {
  document.querySelectorAll('.qtype-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.type === type);
  });
}

function setVerbosityRole(role) {
  state.verbosityRole = role;
  localStorage.setItem('sherlock_verbosity_role', role);
  _applyVerbosityRole(role);
}

function _applyVerbosityRole(role) {
  const sel = document.getElementById('verbositySelect');
  if (sel) sel.value = role;
}

async function checkResearchStatus() {
  try {
    const s = await api('GET', '/api/research/status');
    state.researchAvailable = s.available;
    const btn = document.getElementById('researchBtn');
    if (!btn) return;
    if (!s.available) {
      btn.classList.add('research-unavailable');
      btn.title = 'Research mode unavailable — SearXNG not running. Start with: docker compose up searxng -d';
    } else {
      btn.classList.remove('research-unavailable');
      btn.title = 'Toggle internet research mode (SearXNG)';
    }
  } catch { /* non-fatal */ }
}

function toggleResearch() {
  if (!state.researchAvailable) {
    toast('Research mode requires SearXNG. Run: docker compose up searxng -d', 'error');
    return;
  }
  state.researchMode = !state.researchMode;
  const btn = document.getElementById('researchBtn');
  if (btn) btn.classList.toggle('research-active', state.researchMode);
  toast(state.researchMode ? 'Research mode ON — web results will supplement documents.' : 'Research mode OFF.', state.researchMode ? 'success' : '');
}

function clearChatScreen() {
  const container = document.getElementById('chatMessages');
  container.innerHTML = `
    <div class="empty-state">
      <div class="empty-icon">&#128269;</div>
      <p>Chat cleared.<br><span style="font-size:12px;color:var(--text-muted);">Conversation history is preserved — Sherlock still remembers the context.</span></p>
    </div>`;
}

// ── Log Viewer ────────────────────────────────────────────────────────────────

let _logRefreshTimer = null;
let _logDebounceTimer = null;

function debounceLoadLogs() {
  clearTimeout(_logDebounceTimer);
  _logDebounceTimer = setTimeout(loadLogs, 350);
}

function toggleLogRefresh() {
  const cb    = document.getElementById('logLive');
  const label = document.getElementById('logLiveLabel');
  const dot   = document.getElementById('logLiveDot');
  if (!cb) return;
  if (cb.checked) {
    loadLogs();
    _logRefreshTimer = setInterval(loadLogs, 3000);
    label?.classList.add('active');
    if (dot) dot.style.display = 'inline-block';
  } else {
    clearInterval(_logRefreshTimer);
    _logRefreshTimer = null;
    label?.classList.remove('active');
    if (dot) dot.style.display = 'none';
  }
}

async function loadLogs() {
  const stream = document.getElementById('logStream')?.value || 'app';
  const level  = document.getElementById('logLevel')?.value  || '';
  const search = document.getElementById('logSearch')?.value || '';
  const viewer = document.getElementById('logViewer');
  if (!viewer) return;

  let url = `/api/admin/logs?stream=${encodeURIComponent(stream)}&lines=500`;
  if (level)  url += `&level=${encodeURIComponent(level)}`;
  if (search) url += `&search=${encodeURIComponent(search)}`;

  let entries;
  try {
    const resp = await api('GET', url);
    entries = Array.isArray(resp) ? resp : (resp?.entries ?? resp);
  } catch (e) {
    viewer.innerHTML = `<div class="log-empty">Error: ${escHtml(e.message)}</div>`;
    return;
  }

  if (!entries || !entries.length) {
    viewer.innerHTML = '<div class="log-empty" style="padding:16px;">No log entries match.</div>';
    return;
  }

  // Apply sort
  const sort = viewer._logSort || { col: null, dir: 1 };
  if (sort.col) {
    entries = [...entries].sort((a, b) => {
      let va, vb;
      if (sort.col === 'ts')  { va = a.ts || ''; vb = b.ts || ''; }
      else if (sort.col === 'lvl') { va = a.level || ''; vb = b.level || ''; }
      else if (sort.col === 'msg') { va = a.msg || a.message || ''; vb = b.msg || b.message || ''; }
      else { va = ''; vb = ''; }
      return va < vb ? -sort.dir : va > vb ? sort.dir : 0;
    });
  }

  const atBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 60;
  const skip = new Set(['ts', 'level', 'logger', 'msg', 'message', 'rid', 'exc_info']);

  function sortIcon(col) {
    if (sort.col !== col) return '<span class="log-sort-icon">⇅</span>';
    return `<span class="log-sort-icon active">${sort.dir === 1 ? '↑' : '↓'}</span>`;
  }

  // Header is a separate sticky element OUTSIDE the grid
  let html = `<div class="log-table-head" id="logTableHead">
    <span data-col="ts">Time ${sortIcon('ts')}</span>
    <span data-col="lvl">Level ${sortIcon('lvl')}</span>
    <span data-col="msg">Message ${sortIcon('msg')}</span>
    <span data-col="detail">Details</span>
  </div>
  <div class="log-table" id="logTable">`;

  entries.forEach((entry, i) => {
    const lvl = (entry.level || 'INFO').toUpperCase();
    const ts  = entry.ts ? entry.ts.replace('T', ' ').replace(/\.\d+/, '').replace('Z', '') : '';
    const msg = escHtml(entry.msg || entry.message || '');

    const extras = Object.entries(entry)
      .filter(([k]) => !skip.has(k))
      .map(([k, v]) => `${escHtml(k)}=${escHtml(String(v))}`)
      .join('  ');

    html += `<div class="log-entry log-level-${lvl}" data-log-idx="${i}" onclick="toggleLogDetail(this)">
      <span class="log-ts">${ts}</span>
      <span class="log-badge">${lvl}</span>
      <span class="log-msg">${msg}</span>
      <span class="log-detail-col">${escHtml(extras)}</span>
    </div>`;
  });

  html += '</div>';
  viewer.innerHTML = html;

  // Store entries for detail expansion
  viewer._logEntries = entries;

  if (atBottom) viewer.scrollTop = viewer.scrollHeight;

  // Column sort click handlers
  viewer.querySelectorAll('.log-table-head > span[data-col]').forEach(span => {
    const col = span.dataset.col;
    if (col === 'detail') return; // details not sortable
    span.addEventListener('click', (e) => {
      const cur = viewer._logSort || { col: null, dir: 1 };
      viewer._logSort = { col, dir: cur.col === col ? -cur.dir : 1 };
      loadLogs();
    });
  });

  // Set up column resize
  initLogColResize();
}

function toggleLogDetail(row) {
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('log-detail-expanded')) {
    existing.remove();
    return;
  }
  // Remove any other expanded detail
  row.closest('.log-table')?.querySelectorAll('.log-detail-expanded').forEach(el => el.remove());

  const viewer = document.getElementById('logViewer');
  const entries = viewer?._logEntries;
  const idx = parseInt(row.dataset.logIdx);
  if (!entries || isNaN(idx)) return;

  const entry = entries[idx];
  const detail = document.createElement('div');
  detail.className = 'log-detail-expanded';

  let content = '';
  for (const [k, v] of Object.entries(entry)) {
    content += `<span class="log-detail-key">${escHtml(k)}:</span> <span class="log-detail-val">${escHtml(String(v))}</span>\n`;
  }
  detail.innerHTML = content;
  row.parentNode.insertBefore(detail, row.nextSibling);
}

function initLogColResize() {
  const head  = document.getElementById('logTableHead');
  const table = document.getElementById('logTable');
  if (!head || !table) return;
  const heads = head.querySelectorAll('span[data-col]');
  const colNames = ['--log-col-ts', '--log-col-lvl', '--log-col-msg', '--log-col-detail'];

  heads.forEach((h, i) => {
    h.style.cursor = 'pointer';
    h.addEventListener('mousedown', (e) => {
      // Only start a resize drag when clicking within 8px of the right edge
      const rect = h.getBoundingClientRect();
      if (e.clientX < rect.right - 8) return;

      e.preventDefault();
      const startX = e.clientX;
      const startW = h.offsetWidth;
      let moved = false;

      function onMove(ev) {
        moved = true;
        const diff = ev.clientX - startX;
        const newW = Math.max(40, startW + diff);
        const viewer = document.getElementById('logViewer');
        viewer?.style.setProperty(colNames[i], newW + 'px');
      }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
}

function downloadLog() {
  const stream = document.getElementById('logStream')?.value || 'app';
  const a = document.createElement('a');
  a.href = `/api/admin/logs/download?stream=${encodeURIComponent(stream)}`;
  a.download = `sherlock-${stream}.log`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ── View switching ────────────────────────────────────────────────────────────

const VIEWS = ['chat', 'cases', 'upload', 'outputs', 'admin', 'config'];

function showView(name) {
  // Stop log live-refresh when leaving admin view
  if (name !== 'admin' && _logRefreshTimer) {
    clearInterval(_logRefreshTimer);
    _logRefreshTimer = null;
    const cb = document.getElementById('logLive');
    if (cb) cb.checked = false;
    document.getElementById('logLiveLabel')?.classList.remove('active');
  }

  VIEWS.forEach(v => {
    document.getElementById(`view-${v}`)?.classList.toggle('hidden', v !== name);
    document.getElementById(`nav-${v}`)?.classList.toggle('active', v === name);
  });
  document.getElementById('sidebar').style.display = name === 'chat' ? '' : 'none';

  if (name === 'upload') loadFileList();
  if (name === 'outputs') loadOutputsList();
  if (name === 'admin') loadAdmin();
  if (name === 'cases') renderCases();
  if (name === 'config') loadConfig();
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function logout() {
  localStorage.removeItem('sherlock_token');
  localStorage.removeItem('sherlock_user');
  window.location.href = '/login';
}

// ── Matters ───────────────────────────────────────────────────────────────────

async function loadMatters() {
  try {
    state.matters = await api('GET', '/api/matters') || [];
    renderMatters();
    if (state.matters.length > 0 && !state.activeMatterId) {
      selectMatter(state.matters[0].id);
    }
  } catch (e) {
    toast('Failed to load tasks: ' + e.message, 'error');
  }
}

function renderMatters() {
  const el = document.getElementById('matterList');
  if (!state.matters.length) {
    el.innerHTML = '<div class="empty-state" style="padding:24px 16px;"><p style="font-size:12px;">No tasks yet.<br>Create one to get started.</p></div>';
    return;
  }

  function matterRow(m) {
    return `<div class="matter-item ${m.id === state.activeMatterId ? 'active' : ''}" onclick="selectMatter(${m.id})">
      <span style="font-size:13px;">&#128220;</span>
      <span class="matter-name">${escHtml(m.name)}</span>
      <span class="billable-badge" title="Billable hours" onclick="event.stopPropagation();editBillable(${m.id})">${(m.billable_time || 0).toFixed(1)}h</span>
    </div>`;
  }

  // Group matters by linked case
  const grouped = {};
  const ungrouped = [];
  for (const m of state.matters) {
    if (m.case_id) {
      if (!grouped[m.case_id]) {
        grouped[m.case_id] = { case_name: m.case_name || `Case #${m.case_id}`, matters: [] };
      }
      grouped[m.case_id].matters.push(m);
    } else {
      ungrouped.push(m);
    }
  }

  let html = '';
  for (const [caseId, g] of Object.entries(grouped)) {
    html += `<div class="case-group-header" onclick="goToCase(${caseId})" title="Open case: ${escHtml(g.case_name)}" style="cursor:pointer;">
      <span style="font-size:11px;opacity:0.6;">&#128193;</span>
      <span class="case-group-label">${escHtml(g.case_name)}</span>
      <span class="case-group-count">${g.matters.length}</span>
    </div>
    <div class="case-group-matters">${g.matters.map(matterRow).join('')}</div>`;
  }
  if (ungrouped.length) {
    if (Object.keys(grouped).length) html += `<div class="ungrouped-header">Other Tasks</div>`;
    html += ungrouped.map(matterRow).join('');
  }
  el.innerHTML = html;
}

function goToCase(caseId) {
  showView('cases');
  // Wait for renderCases() to finish, then scroll to and highlight the card
  setTimeout(() => {
    const card = document.getElementById(`case-${caseId}`);
    if (card) {
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.add('case-card-highlight');
      setTimeout(() => card.classList.remove('case-card-highlight'), 1800);
    }
  }, 80);
}

async function editBillable(matterId) {
  const m = state.matters.find(x => x.id === matterId);
  const val = prompt('Billable hours:', (m?.billable_time || 0).toFixed(2));
  if (val === null) return;
  const hours = parseFloat(val);
  if (isNaN(hours) || hours < 0) { toast('Enter a valid number of hours.', 'error'); return; }
  try {
    await api('PATCH', `/api/matters/${matterId}`, { billable_time: hours });
    m.billable_time = hours;
    renderMatters();
    toast(`Billable time updated: ${hours.toFixed(2)}h`);
  } catch (e) { toast('Failed: ' + e.message, 'error'); }
}

function openEditTask() {
  const m = state.matters.find(x => x.id === state.activeMatterId);
  if (!m) { toast('Select a task first.', 'error'); return; }

  document.getElementById('editTaskName').value = m.name;
  document.getElementById('editTaskBillable').value = (m.billable_time || 0).toFixed(2);
  document.getElementById('editTaskError').classList.add('hidden');

  // Populate case dropdown
  const sel = document.getElementById('editTaskCaseId');
  sel.innerHTML = '<option value="">— No case —</option>';
  if (state.cases) {
    state.cases.forEach(c => {
      sel.innerHTML += `<option value="${c.id}" ${c.id === m.case_id ? 'selected' : ''}>${escHtml(c.case_name)}</option>`;
    });
  }

  document.getElementById('editTaskModal').classList.remove('hidden');
  setTimeout(() => document.getElementById('editTaskName').focus(), 50);
}

async function saveTaskEdits() {
  const m = state.matters.find(x => x.id === state.activeMatterId);
  if (!m) return;

  const name = document.getElementById('editTaskName').value.trim();
  if (!name) {
    document.getElementById('editTaskError').textContent = 'Task name is required.';
    document.getElementById('editTaskError').classList.remove('hidden');
    return;
  }

  const caseVal = document.getElementById('editTaskCaseId').value;
  const billable = parseFloat(document.getElementById('editTaskBillable').value) || 0;

  try {
    const case_id = caseVal ? parseInt(caseVal) : 0;
    await api('PATCH', `/api/matters/${m.id}`, {
      name,
      billable_time: billable,
      case_id,
    });

    // Update local state
    m.name = name;
    m.billable_time = billable;
    m.case_id = case_id || null;

    renderMatters();
    document.getElementById('chatMatterTitle').textContent = name;
    closeModal('editTaskModal');
    toast('Task updated.');
  } catch (e) {
    document.getElementById('editTaskError').textContent = e.message;
    document.getElementById('editTaskError').classList.remove('hidden');
  }
}

async function archiveTask() {
  const m = state.matters.find(x => x.id === state.activeMatterId);
  if (!m) return;
  if (!confirm(`Archive "${m.name}"? It will be hidden from the sidebar.`)) return;

  try {
    await api('PATCH', `/api/matters/${m.id}`, { archived: true });
    state.matters = state.matters.filter(x => x.id !== m.id);
    state.activeMatterId = state.matters.length ? state.matters[0].id : null;
    renderMatters();
    closeModal('editTaskModal');
    if (state.activeMatterId) {
      selectMatter(state.activeMatterId);
    } else {
      document.getElementById('chatMatterTitle').textContent = 'All Indexed Files';
      document.getElementById('chatMessages').innerHTML = '<div class="empty-state"><div class="empty-icon">&#128269;</div><p>No tasks. Create one to get started.</p></div>';
    }
    toast('Task archived.');
  } catch (e) { toast('Archive failed: ' + e.message, 'error'); }
}

async function exportTasksCsv() {
  const isAdmin = state.user?.role === 'admin';
  const url = isAdmin ? '/api/admin/tasks/export' : '/api/tasks/export';
  const filename = isAdmin ? 'all-tasks.csv' : 'my-tasks.csv';
  await downloadWithAuth(url, filename);
}

function selectMatter(id) {
  state.activeMatterId = id;
  const matter = state.matters.find(m => m.id === id);
  renderMatters();

  document.getElementById('chatMatterTitle').textContent = matter?.name || 'All Indexed Files';
  document.getElementById('editTaskBtn').classList.toggle('hidden', !matter);
  document.getElementById('exportBtn').classList.toggle('hidden', !matter);

  // Case context bar
  const ctxBar = document.getElementById('caseCtxBar');
  const caseScopeBtn = document.getElementById('scopeCaseBtn');

  if (matter?.case_id) {
    // Populate context bar
    document.getElementById('caseCtxName').textContent = matter.case_name || `Case #${matter.case_id}`;
    const metaParts = [];
    if (matter.case_number) metaParts.push(matter.case_number);
    if (matter.case_type)   metaParts.push(matter.case_type);
    if (matter.client_name) metaParts.push(`Client: ${matter.client_name}`);
    if (matter.opposing_party) metaParts.push(`vs. ${matter.opposing_party}`);
    document.getElementById('caseCtxMeta').textContent = metaParts.join('  ·  ');
    ctxBar.classList.remove('hidden');

    // Old scope toggle — keep in sync
    caseScopeBtn.classList.remove('hidden');

    // Auto-scope to this case when entering a case-linked matter
    setScope('case');
  } else {
    ctxBar.classList.add('hidden');
    caseScopeBtn.classList.add('hidden');
    if (state.scope === 'case') setScope('all');
  }

  loadMessages(id);
}

async function loadMessages(matterId) {
  try {
    const messages = await api('GET', `/api/matters/${matterId}/messages`) || [];
    const container = document.getElementById('chatMessages');
    if (!messages.length) {
      container.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128172;</div><p>No messages yet. Ask your first question.</p></div>';
      return;
    }
    container.innerHTML = messages.map(m => renderMessage(m)).join('');
    scrollToBottom();
  } catch (e) {
    toast('Failed to load messages: ' + e.message, 'error');
  }
}

function renderMessage(msg) {
  const sourcesHtml = msg.sources?.length ? `
    <div class="sources-list">
      <div class="sources-title">Sources</div>
      ${msg.sources.map((s, i) => renderSourceItem(s, i)).join('')}
    </div>` : '';

  const actionsHtml = msg.role === 'assistant' ? `
    <div class="msg-actions">
      <button class="btn" style="font-size:11px;padding:3px 10px;" onclick="copyText(${msg.id})">Copy</button>
      <button class="btn" style="font-size:11px;padding:3px 10px;" onclick="saveOutput(${msg.id})">Save to Outputs</button>
      <button class="msg-export-btn" onclick="exportMemo(${msg.id})" title="Export as Word memo">&#128196;</button>
    </div>` : '';

  return `
    <div class="msg ${msg.role}" id="msg-${msg.id}">
      <div class="msg-bubble">${escHtml(msg.content)}</div>
      ${sourcesHtml}
      ${actionsHtml}
    </div>`;
}

// ── New matter modal ──────────────────────────────────────────────────────────

function openNewMatterModal() {
  document.getElementById('newMatterName').value = '';
  document.getElementById('newMatterError').classList.add('hidden');

  // Populate case dropdown
  const sel = document.getElementById('newMatterCaseId');
  sel.innerHTML = '<option value="">— No case —</option>' +
    state.cases
      .filter(c => c.status === 'active')
      .map(c => `<option value="${c.id}">${escHtml(c.case_name)}${c.case_number ? ` (${escHtml(c.case_number)})` : ''}</option>`)
      .join('');

  document.getElementById('newMatterModal').classList.remove('hidden');
  setTimeout(() => document.getElementById('newMatterName').focus(), 50);
}

async function createMatter() {
  const name = document.getElementById('newMatterName').value.trim();
  if (!name) return;
  const caseVal = document.getElementById('newMatterCaseId').value;
  const case_id = caseVal ? parseInt(caseVal) : null;
  try {
    const matter = await api('POST', '/api/matters', { name, case_id });
    state.matters.unshift(matter);
    closeModal('newMatterModal');
    showView('chat');
    selectMatter(matter.id);
    renderMatters();
  } catch (e) {
    document.getElementById('newMatterError').textContent = e.message;
    document.getElementById('newMatterError').classList.remove('hidden');
  }
}

// ── Cases ─────────────────────────────────────────────────────────────────────

async function loadCases() {
  try {
    state.cases = await api('GET', '/api/cases') || [];
    renderCases();
  } catch (e) {
    // Non-fatal — cases just won't be shown
  }
}

function filterCases(status) {
  state.caseFilter = status;
  document.querySelectorAll('.case-filter-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.status === status);
  });
  renderCases();
}

function renderCases() {
  const el = document.getElementById('caseList');
  if (!el) return;

  const filtered = state.caseFilter === 'all'
    ? state.cases
    : state.cases.filter(c => c.status === state.caseFilter);

  if (!filtered.length) {
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">&#128214;</div><p>No ${state.caseFilter === 'all' ? '' : state.caseFilter + ' '}cases found.</p></div>`;
    return;
  }

  el.innerHTML = filtered.map(c => {
    const statusClass = c.status === 'active' ? 'ready' : c.status === 'closed' ? 'pending' : 'indexing';
    const lastIdx = c.last_indexed ? `Last indexed ${formatDate(c.last_indexed)}` : 'Not yet indexed';
    const indexedCount = c.indexed_count ? `${c.indexed_count.toLocaleString()} files` : '';
    return `
    <div class="case-card" id="case-${c.id}">
      <div class="case-card-header">
        <div class="case-card-title">
          <span class="case-name">${escHtml(c.case_name)}</span>
          ${c.case_number ? `<span class="case-number">${escHtml(c.case_number)}</span>` : ''}
        </div>
        <span class="status-badge ${statusClass}">${c.status}</span>
      </div>
      <div class="case-card-meta">
        ${c.case_type ? `<span class="case-meta-tag">&#9654; ${escHtml(c.case_type)}</span>` : ''}
        ${c.client_name ? `<span class="case-meta-tag">&#128100; ${escHtml(c.client_name)}</span>` : ''}
        ${c.assigned_to ? `<span class="case-meta-tag">&#128084; ${escHtml(c.assigned_to)}</span>` : ''}
        ${c.jurisdiction ? `<span class="case-meta-tag">&#127981; ${escHtml(c.jurisdiction)}</span>` : ''}
      </div>
      ${c.nas_path ? `<div class="case-nas-path">&#128193; ${escHtml(c.nas_path)}</div>` : ''}
      <div class="case-card-footer">
        <span class="case-index-info">${lastIdx}${indexedCount ? ' &bull; ' + indexedCount : ''}</span>
        <div class="case-actions">
          ${c.nas_path ? `<button class="btn" style="font-size:11px;padding:3px 10px;" id="reindex-btn-${c.id}" onclick="triggerCaseReindex(${c.id})">&#8635; Index Now</button>` : ''}
          <button class="btn" style="font-size:11px;padding:3px 10px;" onclick="openEditCase(${c.id})">&#9998; Edit</button>
          <button class="btn" style="font-size:11px;padding:3px 10px;" onclick="openNewMatterForCase(${c.id})">+ New Task</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

function openNewCaseModal() {
  ['nc_case_name','nc_case_number','nc_client_name','nc_opposing_party',
   'nc_jurisdiction','nc_assigned_to','nc_date_opened','nc_nas_path','nc_description'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('nc_case_type').value = '';
  document.getElementById('newCaseError').classList.add('hidden');
  document.getElementById('newCaseModal').classList.remove('hidden');
  setTimeout(() => document.getElementById('nc_case_name').focus(), 50);
}

async function createCase() {
  const case_name = document.getElementById('nc_case_name').value.trim();
  if (!case_name) {
    const e = document.getElementById('newCaseError');
    e.textContent = 'Case name is required.';
    e.classList.remove('hidden');
    return;
  }

  const body = {
    case_name,
    case_number:    document.getElementById('nc_case_number').value.trim() || null,
    case_type:      document.getElementById('nc_case_type').value || null,
    client_name:    document.getElementById('nc_client_name').value.trim() || null,
    opposing_party: document.getElementById('nc_opposing_party').value.trim() || null,
    jurisdiction:   document.getElementById('nc_jurisdiction').value.trim() || null,
    assigned_to:    document.getElementById('nc_assigned_to').value.trim() || null,
    date_opened:    document.getElementById('nc_date_opened').value.trim() || null,
    nas_path:       document.getElementById('nc_nas_path').value.trim() || null,
    description:    document.getElementById('nc_description').value.trim() || null,
  };

  try {
    const newCase = await api('POST', '/api/cases', body);
    state.cases.unshift(newCase);
    closeModal('newCaseModal');
    toast(`Case "${newCase.case_name}" created.`, 'success');
    renderCases();
  } catch (e) {
    document.getElementById('newCaseError').textContent = e.message;
    document.getElementById('newCaseError').classList.remove('hidden');
  }
}

function openEditCase(caseId) {
  const c = state.cases.find(x => x.id === caseId);
  if (!c) return;
  document.getElementById('ec_id').value = c.id;
  document.getElementById('ec_case_name').value = c.case_name || '';
  document.getElementById('ec_case_number').value = c.case_number || '';
  document.getElementById('ec_case_type').value = c.case_type || '';
  document.getElementById('ec_client_name').value = c.client_name || '';
  document.getElementById('ec_opposing_party').value = c.opposing_party || '';
  document.getElementById('ec_jurisdiction').value = c.jurisdiction || '';
  document.getElementById('ec_assigned_to').value = c.assigned_to || '';
  document.getElementById('ec_date_opened').value = c.date_opened || '';
  document.getElementById('ec_nas_path').value = c.nas_path || '';
  document.getElementById('ec_description').value = c.description || '';
  document.getElementById('ec_status').value = c.status || 'active';
  document.getElementById('editCaseError').classList.add('hidden');
  document.getElementById('editCaseModal').classList.remove('hidden');
  setTimeout(() => document.getElementById('ec_case_name').focus(), 50);
}

async function saveEditCase() {
  const caseId = parseInt(document.getElementById('ec_id').value);
  const case_name = document.getElementById('ec_case_name').value.trim();
  if (!case_name) {
    const e = document.getElementById('editCaseError');
    e.textContent = 'Case name is required.';
    e.classList.remove('hidden');
    return;
  }
  const body = {
    case_name,
    case_number:    document.getElementById('ec_case_number').value.trim() || null,
    case_type:      document.getElementById('ec_case_type').value || null,
    client_name:    document.getElementById('ec_client_name').value.trim() || null,
    opposing_party: document.getElementById('ec_opposing_party').value.trim() || null,
    jurisdiction:   document.getElementById('ec_jurisdiction').value.trim() || null,
    assigned_to:    document.getElementById('ec_assigned_to').value.trim() || null,
    date_opened:    document.getElementById('ec_date_opened').value.trim() || null,
    nas_path:       document.getElementById('ec_nas_path').value.trim() || null,
    description:    document.getElementById('ec_description').value.trim() || null,
    status:         document.getElementById('ec_status').value || 'active',
  };
  try {
    const updated = await api('PATCH', `/api/cases/${caseId}`, body);
    const idx = state.cases.findIndex(x => x.id === caseId);
    if (idx !== -1) state.cases[idx] = { ...state.cases[idx], ...updated };
    closeModal('editCaseModal');
    toast(`Case "${updated.case_name}" updated.`, 'success');
    renderCases();
    loadMatters(); // refresh sidebar in case name changed
  } catch (e) {
    document.getElementById('editCaseError').textContent = e.message;
    document.getElementById('editCaseError').classList.remove('hidden');
  }
}

function openNewMatterForCase(caseId) {
  openNewMatterModal();
  document.getElementById('newMatterCaseId').value = String(caseId);
  showView('chat');
}

async function triggerCaseReindex(caseId) {
  const btn = document.getElementById(`reindex-btn-${caseId}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Indexing…'; }
  try {
    const { job_id } = await api('POST', `/api/cases/${caseId}/reindex`);
    toast('Case re-index started…');
    pollCaseIndex(job_id, caseId, btn);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '↺ Index Now'; }
    toast('Re-index failed: ' + e.message, 'error');
  }
}

function pollCaseIndex(jobId, caseId, btn) {
  const interval = setInterval(async () => {
    try {
      const s = await api('GET', `/api/cases/${caseId}/index-status/${jobId}`);
      if (s.done) {
        clearInterval(interval);
        if (btn) { btn.disabled = false; btn.textContent = '↺ Index Now'; }
        toast(`Re-index complete: ${s.indexed} files indexed, ${s.skipped} unchanged.`, 'success');
        // Refresh cases to update indexed_count / last_indexed
        loadCases();
      }
    } catch {
      clearInterval(interval);
      if (btn) { btn.disabled = false; btn.textContent = '↺ Index Now'; }
    }
  }, 3000);
}

// ── Chat ──────────────────────────────────────────────────────────────────────

function setScope(scope) {
  state.scope = scope;
  // Sync both the header scope-btn strip and the context bar buttons
  document.querySelectorAll('.scope-btn, .ctx-scope-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.scope === scope);
  });
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

async function sendMessage(overrideText = null) {
  if (state.streaming) return;
  const input = document.getElementById('chatInput');
  const text = overrideText ?? input.value.trim();
  if (!text) return;

  if (!overrideText) {
    input.value = '';
    input.style.height = 'auto';
  }

  state.streaming = true;
  state.abortController = new AbortController();
  document.getElementById('sendBtn').classList.add('hidden');
  document.getElementById('stopBtn').classList.remove('hidden');

  const container = document.getElementById('chatMessages');

  // Remove empty state
  const emptyState = container.querySelector('.empty-state');
  if (emptyState) emptyState.remove();

  // Append user bubble
  const userDiv = document.createElement('div');
  userDiv.className = 'msg user';
  userDiv.innerHTML = `<div class="msg-bubble">${escHtml(text)}</div>`;
  container.appendChild(userDiv);

  // Typing indicator
  const typingDiv = document.createElement('div');
  typingDiv.className = 'msg assistant';
  typingDiv.id = 'typing';
  typingDiv.innerHTML = `<div class="typing-indicator">
    <div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>
  </div>`;
  container.appendChild(typingDiv);
  scrollToBottom();

  try {
    const chatUrl = state.activeMatterId
      ? `/api/matters/${state.activeMatterId}/chat`
      : '/api/chat';
    const resp = await fetch(chatUrl, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${state.token}`,
        'Content-Type': 'application/json',
        'X-CSRF-Token': getCsrfToken(),
      },
      body: JSON.stringify({
        message: text,
        scope: state.scope,
        query_type: state.queryType,
        verbosity_role: state.verbosityRole,
        research_mode: state.researchMode,
      }),
      signal: state.abortController.signal,
    });

    if (!resp.ok) throw new Error('Chat request failed');

    // Replace typing indicator with response bubble
    typingDiv.remove();
    const aiDiv = document.createElement('div');
    aiDiv.className = 'msg assistant';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    aiDiv.appendChild(bubble);
    container.appendChild(aiDiv);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullText = '';
    let sources = [];
    let messageId = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.token) {
            fullText += data.token;
            bubble.textContent = fullText;
            if (data.sources?.length) sources = data.sources;
          }
          if (data.done) {
            messageId = data.message_id;
            if (data.token_stats) {
              const ts = data.token_stats;
              const statsDiv = document.createElement('div');
              statsDiv.className = 'token-stats';
              statsDiv.innerHTML =
                `<span title="Prompt tokens">${ts.prompt_tokens || 0} in</span>` +
                `<span title="Completion tokens">${ts.completion_tokens || 0} out</span>` +
                `<span title="Total tokens">${ts.total_tokens || 0} total</span>` +
                `<span title="Generation speed">${ts.tokens_per_sec || 0} tok/s</span>` +
                `<span title="LLM latency">${((ts.latency_llm_ms || 0) / 1000).toFixed(1)}s</span>`;
              aiDiv.appendChild(statsDiv);
            }
          }
        } catch {}
      }
      scrollToBottom();
    }

    // Add sources + action buttons
    if (sources.length) {
      const srcDiv = document.createElement('div');
      srcDiv.className = 'sources-list';
      srcDiv.innerHTML = `<div class="sources-title">Sources</div>` +
        sources.map((s, i) => renderSourceItem(s, i)).join('');
      aiDiv.appendChild(srcDiv);
    }

    if (messageId) {
      const actDiv = document.createElement('div');
      actDiv.className = 'msg-actions';
      actDiv.innerHTML = `
        <button class="btn" style="font-size:11px;padding:3px 10px;" onclick="copyBubble(this)">Copy</button>
        <button class="btn" style="font-size:11px;padding:3px 10px;" onclick="saveOutput(${messageId})">Save to Outputs</button>
        <button class="msg-export-btn" onclick="exportMemo(${messageId})" title="Export as Word memo">&#128196;</button>
      `;
      aiDiv.appendChild(actDiv);
    }

    scrollToBottom();
    // Refresh history sidebar after each exchange
    if (state.activeMatterId) loadHistory();
  } catch (e) {
    if (e.name === 'AbortError') {
      // User clicked Stop — keep partial response visible
      typingDiv.remove();
      toast('Stopped.', '');
    } else {
      typingDiv.remove();
      toast('Error: ' + e.message, 'error');
    }
  } finally {
    state.streaming = false;
    state.abortController = null;
    document.getElementById('stopBtn').classList.add('hidden');
    document.getElementById('sendBtn').classList.remove('hidden');
    document.getElementById('chatInput').focus();
  }
}

function stopChat() {
  if (state.abortController) {
    state.abortController.abort();
  }
}

function scrollToBottom() {
  const el = document.getElementById('chatMessages');
  el.scrollTop = el.scrollHeight;
}

function copyBubble(btn) {
  const bubble = btn.closest('.msg').querySelector('.msg-bubble');
  navigator.clipboard.writeText(bubble.textContent).then(() => toast('Copied!'));
}

async function saveOutput(messageId) {
  const matter = state.matters.find(m => m.id === state.activeMatterId);
  try {
    const result = await api('POST', '/api/outputs', { message_id: messageId, matter_name: matter?.name || '' });
    toast('Saved to Outputs — downloading now.', 'success');
    // Trigger immediate browser download with auth
    if (result && result.download_url) {
      await downloadWithAuth(result.download_url, result.filename || 'output.txt');
    }
  } catch (e) {
    toast('Save failed: ' + e.message, 'error');
  }
}

// ── File attach (from chat input) ─────────────────────────────────────────────

function triggerFileAttach() {
  document.getElementById('fileAttachInput').click();
}

async function attachFiles(input) {
  const files = input.files;
  if (!files || !files.length) return;
  input.value = '';
  await _uploadAndQueryFiles([...files]);
}

// ── Chat drag-and-drop ───────────────────────────────────────────────────────

let _chatDragCounter = 0;

function chatDragEnter(e) {
  e.preventDefault();
  _chatDragCounter++;
  document.getElementById('chatDropOverlay').classList.remove('hidden');
}

function chatDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
}

function chatDragLeave(e) {
  e.preventDefault();
  _chatDragCounter--;
  if (_chatDragCounter <= 0) {
    _chatDragCounter = 0;
    document.getElementById('chatDropOverlay').classList.add('hidden');
  }
}

function chatDrop(e) {
  e.preventDefault();
  _chatDragCounter = 0;
  document.getElementById('chatDropOverlay').classList.add('hidden');
  const files = e.dataTransfer.files;
  if (files && files.length) _uploadAndQueryFiles([...files]);
}

async function _uploadAndQueryFiles(files) {
  const names = files.map(f => f.name);
  toast(`Uploading ${names.length} file${names.length > 1 ? 's' : ''}…`);

  // Upload all files and collect job IDs
  const jobs = [];
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    try {
      const result = await apiUpload('/api/upload', fd);
      jobs.push({ job_id: result.job_id, filename: result.filename || file.name });
      toast(`Uploaded ${file.name}, indexing…`);
    } catch (e) {
      toast(`Failed: ${file.name} — ${e.message}`, 'error');
    }
  }

  if (!jobs.length) return;

  // Poll until ALL jobs are done
  const pending = new Set(jobs.map(j => j.job_id));
  const indexed = [];
  const maxWait = 120000;  // 2 minutes max
  const start = Date.now();

  while (pending.size > 0 && Date.now() - start < maxWait) {
    await new Promise(r => setTimeout(r, 2000));
    for (const jid of [...pending]) {
      try {
        const status = await api('GET', `/api/upload/${jid}/status`);
        if (status.done) {
          pending.delete(jid);
          const job = jobs.find(j => j.job_id === jid);
          if (status.status !== 'error') {
            indexed.push(job.filename);
            toast(`${job.filename} indexed (${status.indexed} chunks)`, 'success');
          } else {
            toast(`Indexing failed for ${job.filename}`, 'error');
          }
        }
      } catch { /* retry next poll */ }
    }
  }

  if (!indexed.length) {
    toast('No files were indexed successfully.', 'error');
    return;
  }

  // Show interactive prompt asking what to do with the files
  _showFilePrompt(indexed);
}

function _showFilePrompt(filenames) {
  const container = document.getElementById('chatMessages');
  const emptyState = container.querySelector('.empty-state');
  if (emptyState) emptyState.remove();

  const fileList = filenames.map(f => `"${f}"`).join(', ');
  const card = document.createElement('div');
  card.className = 'msg assistant';

  const pills = [
    { label: 'Summarize', query: filenames.length === 1
        ? `Summarize the document ${fileList}. Focus on key provisions, parties, dates, and any red flags.`
        : `Summarize each of these ${filenames.length} documents: ${fileList}. For each, identify key provisions, parties, dates, and red flags.` },
    { label: 'Compare', query: `Compare and contrast these documents: ${fileList}. Highlight key differences, conflicts, and commonalities.` },
    { label: 'Timeline', query: `Extract a chronological timeline of all dates, deadlines, and events from: ${fileList}.` },
    { label: 'Risk Review', query: `Perform a risk review of ${fileList}. Identify potential legal risks, ambiguities, missing clauses, and red flags.` },
  ];

  const chipHtml = filenames.map(f => `<span class="file-prompt-chip">${escHtml(f)}</span>`).join('');
  const pillHtml = pills.map((p, i) =>
    `<button class="file-prompt-pill" data-pill-idx="${i}">${p.label}</button>`
  ).join('');

  card.innerHTML = `<div class="file-prompt-card">
    <div class="file-prompt-files">${chipHtml}</div>
    <div class="file-prompt-label">What would you like me to do with ${filenames.length === 1 ? 'this file' : 'these files'}?</div>
    <div class="file-prompt-pills">${pillHtml}</div>
    <div class="file-prompt-input-row">
      <input type="text" class="file-prompt-input" placeholder="Or type your own instruction…">
      <button class="file-prompt-send">Go</button>
    </div>
  </div>`;

  container.appendChild(card);
  scrollToBottom();

  const input = card.querySelector('.file-prompt-input');
  const sendBtn = card.querySelector('.file-prompt-send');

  function submitFileQuery(query) {
    // Disable the card after use
    card.querySelectorAll('button, input').forEach(el => el.disabled = true);
    input.value = query;
    sendMessage(query);
  }

  // Pill clicks pre-fill and send
  card.querySelectorAll('.file-prompt-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.pillIdx);
      submitFileQuery(pills[idx].query);
    });
  });

  // Go button
  sendBtn.addEventListener('click', () => {
    const text = input.value.trim();
    if (!text) return;
    // Prepend file context if user typed a custom instruction
    const query = `Regarding the documents I just uploaded (${fileList}): ${text}`;
    submitFileQuery(query);
  });

  // Enter key
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      sendBtn.click();
    }
  });

  input.focus();
}

// ── Audio upload ──────────────────────────────────────────────────────────────

function triggerAudioUpload() {
  document.getElementById('audioInput').click();
}

async function uploadAudio(input) {
  const file = input.files[0];
  if (!file) return;
  input.value = '';

  toast('Transcribing audio…');
  const fd = new FormData();
  fd.append('file', file);

  try {
    const { job_id } = await apiUpload('/api/audio', fd);
    pollAudioJob(job_id);
  } catch (e) {
    toast('Audio upload failed: ' + e.message, 'error');
  }
}

function pollAudioJob(jobId) {
  const interval = setInterval(async () => {
    try {
      const status = await api('GET', `/api/audio/${jobId}/status`);
      if (status.done) {
        clearInterval(interval);
        if (status.status === 'error') {
          toast('Transcription failed: ' + status.error, 'error');
        } else {
          showAudioConfirm(status.transcript);
        }
      }
    } catch {
      clearInterval(interval);
    }
  }, 2000);
}

function showAudioConfirm(transcript) {
  document.getElementById('audioTranscript').value = transcript;
  document.getElementById('audioModal').classList.remove('hidden');
}

function submitAudioQuery() {
  const text = document.getElementById('audioTranscript').value.trim();
  closeModal('audioModal');
  if (text) {
    sendMessage(text);
  }
}

// ── File upload ───────────────────────────────────────────────────────────────

function onDragOver(e) {
  e.preventDefault();
  document.getElementById('dropZone').classList.add('drag-over');
}
function onDragLeave() {
  document.getElementById('dropZone').classList.remove('drag-over');
}
function onDrop(e) {
  e.preventDefault();
  document.getElementById('dropZone').classList.remove('drag-over');
  uploadFiles(e.dataTransfer.files);
}

async function uploadFiles(files) {
  for (const file of files) {
    await uploadSingleFile(file);
  }
  loadFileList();
}

async function uploadSingleFile(file) {
  const fd = new FormData();
  fd.append('file', file);
  try {
    const { upload_id, job_id, filename } = await apiUpload('/api/upload', fd);
    toast(`Indexing ${filename}…`);
    pollUploadJob(job_id, filename);
  } catch (e) {
    toast(`Upload failed: ${e.message}`, 'error');
  }
}

function pollUploadJob(jobId, filename) {
  const interval = setInterval(async () => {
    try {
      const status = await api('GET', `/api/upload/${jobId}/status`);
      if (status.done) {
        clearInterval(interval);
        if (status.status === 'error') {
          toast(`Indexing failed for ${filename}`, 'error');
        } else {
          toast(`${filename} ready (${status.indexed} chunks indexed)`, 'success');
        }
        loadFileList();
      }
    } catch {
      clearInterval(interval);
    }
  }, 2500);
}

async function loadFileList() {
  try {
    const files = await api('GET', '/api/files') || [];
    const el = document.getElementById('fileList');
    if (!files.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128196;</div><p>No files uploaded yet.</p></div>';
      return;
    }
    el.innerHTML = files.map(f => `
      <div class="file-item" id="file-${f.id}">
        <div class="file-icon">${fileIcon(f.file_type)}</div>
        <div class="file-info">
          <div class="file-name">${escHtml(f.filename)}</div>
          <div class="file-meta">${formatBytes(f.size_bytes)} &bull; ${formatDate(f.uploaded_at)}</div>
        </div>
        <span class="status-badge ${f.status}">${f.status}</span>
        <button class="btn-icon" title="Delete" onclick="deleteFile(${f.id})">&#128465;</button>
      </div>
    `).join('');
  } catch (e) {
    toast('Failed to load files: ' + e.message, 'error');
  }
}

async function deleteFile(id) {
  if (!confirm('Remove this file from your index?')) return;
  try {
    await api('DELETE', `/api/files/${id}`);
    document.getElementById(`file-${id}`)?.remove();
    toast('File removed.');
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────

async function loadOutputsList() {
  try {
    const outputs = await api('GET', '/api/outputs') || [];
    const el = document.getElementById('outputsList');
    if (!outputs.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128196;</div><p>No saved outputs yet.<br>Click "Save to Outputs" on any response.</p></div>';
      return;
    }
    el.innerHTML = `
      <div style="text-align:right;margin-bottom:10px;">
        <button class="btn" style="font-size:12px;padding:5px 14px;" onclick="downloadAllOutputs()">&#128230; Download All (.zip)</button>
      </div>` + outputs.map(o => `
      <div class="output-item">
        <div class="output-info">
          <div class="output-name">&#128196; ${escHtml(o.filename)}</div>
          <div class="output-meta">${formatDate(o.saved_at)}</div>
        </div>
        <button class="btn" style="font-size:12px;padding:5px 12px;" onclick="downloadWithAuth('/api/outputs/${o.id}/download','${escHtml(o.filename)}')">Download</button>
      </div>
    `).join('');
  } catch (e) {
    toast('Failed to load outputs: ' + e.message, 'error');
  }
}

async function downloadAllOutputs() {
  toast('Preparing zip…');
  await downloadWithAuth('/api/outputs/download-all', 'sherlock-outputs.zip');
}

// ── Config ────────────────────────────────────────────────────────────────────

async function loadConfig() {
  if (state.user.role !== 'admin') return;
  const el = document.getElementById('configContent');
  try {
    const cfg = await api('GET', '/api/admin/config');
    el.innerHTML = `
      <div class="config-grid">

        <div class="config-section">
          <h3>&#127968; System</h3>
          <div class="config-table">
            <div class="config-row"><span>System Name</span><code>${escHtml(cfg.system.name)}</code></div>
            <div class="config-row"><span>Hostname</span><code>${escHtml(cfg.system.hostname)}</code></div>
            <div class="config-row"><span>Database</span><code>${escHtml(cfg.system.db_path)}</code></div>
            <div class="config-row"><span>Uploads Dir</span><code>${escHtml(cfg.system.uploads_dir)}</code></div>
            <div class="config-row"><span>Outputs Dir</span><code>${escHtml(cfg.system.outputs_dir)}</code></div>
            <div class="config-row"><span>Whisper Models</span><code>${escHtml(cfg.system.whisper_model_dir)}</code></div>
          </div>
        </div>

        <div class="config-section">
          <h3>&#9881; Services</h3>
          <div class="config-table">
            <div class="config-row"><span>Ollama URL</span><code>${escHtml(cfg.services.ollama_url)}</code></div>
            <div class="config-row"><span>ChromaDB URL</span><code>${escHtml(cfg.services.chroma_url)}</code></div>
          </div>
        </div>

        <div class="config-section">
          <h3>&#129302; Models</h3>
          <div class="config-table">
            <div class="config-row"><span>LLM Model</span><code>${escHtml(cfg.models.llm)}</code></div>
            <div class="config-row"><span>Embedding Model</span><code>${escHtml(cfg.models.embed)}</code></div>
            <div class="config-row"><span>Whisper Model</span><code>${escHtml(cfg.models.whisper)}</code></div>
          </div>
        </div>

        <div class="config-section">
          <h3>&#128269; RAG Settings</h3>
          <div class="config-table">
            <div class="config-row"><span>Top-N Results</span><code>${cfg.rag.top_n}</code></div>
            <div class="config-row"><span>Max Upload Size</span><code>${cfg.rag.max_upload_mb} MB</code></div>
            <div class="config-row"><span>Global Collection</span><code>${escHtml(cfg.rag.global_collection)}</code></div>
            <div class="config-row"><span>Session Duration</span><code>${cfg.rag.jwt_expiry_hours}h</code></div>
          </div>
        </div>

        <div class="config-section config-section-wide">
          <h3>&#128193; NAS Paths <span class="config-hint">(one path per line)</span></h3>
          <textarea id="nasPathsInput" rows="4" style="width:100%;font-family:monospace;font-size:13px;padding:8px;box-sizing:border-box;border:1px solid var(--border);border-radius:4px;background:var(--bg-secondary);color:var(--text-primary);resize:vertical;">${cfg.nas.paths.join('\n')}</textarea>
          <div style="margin-top:8px;display:flex;align-items:center;gap:12px;">
            <button class="btn btn-primary" onclick="saveNasPaths()">Save &amp; Apply</button>
            <span id="nasPathsStatus" style="font-size:13px;color:var(--text-secondary);"></span>
          </div>
        </div>

        <div class="config-section">
          <h3>&#128202; Index Stats</h3>
          <div class="config-table">
            <div class="config-row"><span>Active Cases</span><code>${cfg.stats.active_cases}</code></div>
            <div class="config-row"><span>Total Indexed Files</span><code>${cfg.stats.indexed_files.toLocaleString()}</code></div>
          </div>
        </div>

      </div>
      <p class="config-footer">To change other settings (models, URLs, auth), edit <code>~/Sherlock/sherlock.conf</code> and restart.</p>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load config: ${escHtml(e.message)}</p></div>`;
  }
}

async function saveNasPaths() {
  const raw = document.getElementById('nasPathsInput').value;
  const paths = raw.split('\n').map(p => p.trim()).filter(Boolean);
  const status = document.getElementById('nasPathsStatus');
  status.textContent = 'Saving…';
  try {
    await api('POST', '/api/admin/nas-paths', { nas_paths: paths });
    status.style.color = 'var(--success, #2ecc71)';
    status.textContent = `✓ Saved ${paths.length} path${paths.length !== 1 ? 's' : ''}. Changes are live — no restart needed.`;
  } catch (e) {
    status.style.color = 'var(--danger, #e74c3c)';
    status.textContent = `✗ ${e.message}`;
  }
}

// ── Admin ─────────────────────────────────────────────────────────────────────

async function loadAdmin() {
  if (state.user.role !== 'admin') return;
  loadSystemStatus();
  loadUsers();
  loadLogs();
  loadUsage(7);
  resumeActiveReindex();
  checkForUpdate(true); // silent background check on load
}

// ── Update / Upgrade ──────────────────────────────────────────────────────────

let _upgradePoller = null;

async function checkForUpdate(silent = false) {
  const btn = document.getElementById('checkUpdateBtn');
  const badge = document.getElementById('updateBadge');
  const vbadge = document.getElementById('versionBadge');
  const notes = document.getElementById('updateNotes');
  const msg = document.getElementById('updateStatusMsg');
  const upgradeNow = document.getElementById('upgradeNowBtn');
  const upgrade3am = document.getElementById('upgrade3amBtn');

  if (!silent && btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  try {
    const d = await api('GET', '/api/admin/update/check');
    if (vbadge) vbadge.textContent = `v${d.current}`;
    if (d.error && !silent) { toast('Update check failed: ' + d.error, 'error'); return; }
    if (d.update_available) {
      badge?.classList.remove('hidden');
      upgradeNow?.classList.remove('hidden');
      upgrade3am?.classList.remove('hidden');
      if (msg) msg.textContent = `v${d.latest} available`;
      if (notes && d.release_notes) {
        notes.textContent = d.release_notes;
        notes.classList.remove('hidden');
      }
      // Store latest version for apply call
      if (upgradeNow) upgradeNow.dataset.version = d.latest;
      if (!silent) toast(`Update available: ${d.latest}`, 'info');
    } else {
      badge?.classList.add('hidden');
      upgradeNow?.classList.add('hidden');
      upgrade3am?.classList.add('hidden');
      if (!silent && msg) msg.textContent = 'Already up to date.';
    }
  } catch (e) {
    if (!silent) toast('Update check failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⟳ Check for Update'; }
  }
}

async function applyUpdate() {
  if (!confirm('Apply update now? Sherlock will restart briefly.')) return;
  const msg = document.getElementById('updateStatusMsg');
  const log = document.getElementById('upgradeLog');
  if (msg) msg.textContent = 'Starting upgrade…';
  if (log) { log.textContent = ''; log.classList.remove('hidden'); }

  try {
    await api('POST', '/api/admin/update/apply');
    toast('Upgrade started — watch the log below.', 'info');
    _startUpgradePoller();
  } catch (e) {
    toast('Upgrade failed: ' + e.message, 'error');
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}

function _startUpgradePoller() {
  if (_upgradePoller) return;
  _upgradePoller = setInterval(async () => {
    try {
      const s = await api('GET', '/api/admin/update/status');
      const log = document.getElementById('upgradeLog');
      const msg = document.getElementById('updateStatusMsg');
      const vbadge = document.getElementById('versionBadge');

      if (log) log.textContent = s.log.join('\n');
      if (vbadge && s.version) vbadge.textContent = `v${s.version}`;

      if (!s.running) {
        clearInterval(_upgradePoller);
        _upgradePoller = null;
        if (s.error) {
          if (msg) msg.textContent = '✗ ' + s.error;
          toast('Upgrade failed: ' + s.error, 'error');
        } else {
          if (msg) msg.textContent = `✓ Upgraded to ${s.version}`;
          toast(`Upgrade complete: ${s.version}`, 'success');
          checkForUpdate(true);
        }
      }
    } catch (_) {}
  }, 2000);
}

async function scheduleUpdate(timeStr) {
  const input = prompt('Schedule upgrade at time (HH:MM 24h):', timeStr || '03:00');
  if (!input) return;
  try {
    const d = await api('POST', '/api/admin/update/schedule', { time: input });
    const msg = document.getElementById('updateStatusMsg');
    if (msg) msg.textContent = `Upgrade scheduled at ${input} (via ${d.method})`;
    toast(`Upgrade scheduled at ${input}`, 'success');
  } catch (e) {
    toast('Schedule failed: ' + e.message, 'error');
  }
}

async function loadSystemStatus() {
  try {
    const s = await api('GET', '/api/admin/status');
    document.getElementById('statGrid').innerHTML = `
      <div class="stat-card"><div class="stat-value stat-status ${s.ollama}">${s.ollama === 'up' ? '●' : '○'}</div><div class="stat-label">Ollama</div><div class="stat-status ${s.ollama}">${s.ollama}</div></div>
      <div class="stat-card"><div class="stat-value stat-status ${s.chroma}">${s.chroma === 'up' ? '●' : '○'}</div><div class="stat-label">ChromaDB</div><div class="stat-status ${s.chroma}">${s.chroma}</div></div>
      <div class="stat-card"><div class="stat-value">${s.users}</div><div class="stat-label">Users</div></div>
      <div class="stat-card"><div class="stat-value">${s.indexed_files.toLocaleString()}</div><div class="stat-label">Indexed Files</div></div>
      <div class="stat-card"><div class="stat-value">${s.outputs}</div><div class="stat-label">Outputs Saved</div></div>
    `;
  } catch (e) {
    toast('Status check failed: ' + e.message, 'error');
  }
}

async function loadUsers() {
  try {
    const users = await api('GET', '/api/admin/users') || [];
    document.getElementById('userTableBody').innerHTML = users.map(u => `
      <tr>
        <td>${escHtml(u.username)}</td>
        <td>${escHtml(u.display_name || '—')}</td>
        <td><span class="tag">${u.role}</span></td>
        <td>${u.active ? '&#9679;' : '<span style="color:var(--text-muted)">&#9675;</span>'}</td>
        <td class="muted">${u.last_login ? formatDate(u.last_login) : 'Never'}</td>
        <td>
          <button class="btn" style="font-size:11px;padding:3px 8px;" onclick="toggleUserActive(${u.id}, ${u.active})">
            ${u.active ? 'Deactivate' : 'Activate'}
          </button>
        </td>
      </tr>
    `).join('');
  } catch (e) {
    toast('Failed to load users: ' + e.message, 'error');
  }
}

function openNewUserModal() {
  ['newUsername','newDisplayName','newUserPass'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('newUserError').classList.add('hidden');
  document.getElementById('newUserModal').classList.remove('hidden');
}

async function createUser() {
  const username = document.getElementById('newUsername').value.trim();
  const display_name = document.getElementById('newDisplayName').value.trim();
  const password = document.getElementById('newUserPass').value;
  const role = document.getElementById('newUserRole').value;
  const errEl = document.getElementById('newUserError');
  if (!username || !password) { errEl.textContent = 'Username and password required.'; errEl.classList.remove('hidden'); return; }
  try {
    await api('POST', '/api/admin/users', { username, display_name, password, role });
    closeModal('newUserModal');
    toast('User created.', 'success');
    loadUsers();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
}

async function toggleUserActive(userId, currentlyActive) {
  try {
    await api('PATCH', `/api/admin/users/${userId}`, { active: !currentlyActive });
    loadUsers();
  } catch (e) {
    toast('Update failed: ' + e.message, 'error');
  }
}

async function triggerReindex() {
  const btn = document.getElementById('reindexBtn');
  btn.disabled = true;
  btn.textContent = 'Re-indexing…';
  try {
    const { job_id } = await api('POST', '/api/admin/reindex');
    toast('Re-index started. This may take a while for large NAS volumes.');
    pollReindex(job_id, btn);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '↺ Re-index NAS';
    toast('Reindex failed: ' + e.message, 'error');
  }
}

async function resumeActiveReindex() {
  try {
    const s = await api('GET', '/api/admin/reindex/active');
    if (!s.active) return;
    const btn = document.getElementById('reindexBtn');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = `Indexing… (${s.indexed || 0} indexed, ${s.skipped || 0} skipped)`;
    pollReindex(s.job_id, btn);
  } catch { /* ignore */ }
}

function pollReindex(jobId, btn) {
  const interval = setInterval(async () => {
    try {
      const s = await api('GET', `/api/admin/reindex/${jobId}/status`);
      btn.textContent = `Indexing… (${s.indexed} indexed, ${s.skipped} skipped, ${s.errors} errors)`;
      if (s.done) {
        clearInterval(interval);
        btn.disabled = false;
        btn.textContent = '↺ Re-index NAS';
        toast(`Re-index complete: ${s.indexed} files indexed, ${s.skipped} unchanged.`, 'success');
        loadSystemStatus();
      }
    } catch {
      clearInterval(interval);
      btn.disabled = false;
      btn.textContent = '↺ Re-index NAS';
    }
  }, 3000);
}

// ── Document preview ─────────────────────────────────────────────────────────

const _NATIVE_PREVIEW = new Set(['.pdf','.txt','.md','.csv','.jpg','.jpeg','.png','.gif','.tiff','.tif','.bmp']);

function openPreview(filePath, filename) {
  const panel    = document.getElementById('previewPanel');
  const backdrop = document.getElementById('previewBackdrop');
  const title    = document.getElementById('previewTitle');
  const body     = document.getElementById('previewBody');
  const dlLink   = document.getElementById('previewDownloadLink');

  title.textContent = filename || 'Document';

  const encoded = encodeURIComponent(filePath);
  const previewUrl  = `/api/preview?path=${encoded}`;
  const textUrl     = `/api/preview/text?path=${encoded}`;
  const ext         = (filePath.match(/\.[^.]+$/) || [''])[0].toLowerCase();

  dlLink.href = previewUrl;

  if (ext === '.pdf') {
    body.innerHTML = `<embed src="${previewUrl}" type="application/pdf" class="preview-embed">`;
  } else if (['.jpg','.jpeg','.png','.gif','.bmp','.tiff','.tif'].includes(ext)) {
    body.innerHTML = `<img src="${previewUrl}" class="preview-img" alt="${escHtml(filename)}">`;
  } else {
    // DOCX, XLSX, PPTX, TXT, etc — show extracted text
    body.innerHTML = `<iframe src="${textUrl}" class="preview-iframe" title="${escHtml(filename)}"></iframe>`;
  }

  panel.classList.remove('hidden');
  backdrop.classList.remove('hidden');
  document.body.classList.add('preview-open');
}

function closePreview() {
  document.getElementById('previewPanel').classList.add('hidden');
  document.getElementById('previewBackdrop').classList.add('hidden');
  document.body.classList.remove('preview-open');
  document.getElementById('previewBody').innerHTML = '';
}

// ── Matter export ─────────────────────────────────────────────────────────────

function exportMatter() {
  if (!state.activeMatterId) return;
  window.open(`/api/matters/${state.activeMatterId}/export`, '_blank');
}

// ── Deadline Extractor ────────────────────────────────────────────────────────

async function openDeadlines() {
  const mid = state.activeMatterId;
  if (!mid) { toast('Open a task first.', 'error'); return; }
  document.getElementById('deadlineModal').classList.remove('hidden');
  document.getElementById('deadlineList').innerHTML = '<div class="dl-loading">Loading…</div>';
  // Try cached first
  try {
    const data = await api('GET', `/api/matters/${mid}/deadlines`);
    renderDeadlines(data);
  } catch(e) {
    document.getElementById('deadlineList').innerHTML = '<div class="dl-empty">No deadlines extracted yet. Click "Extract" to run.</div>';
  }
}

async function extractDeadlines() {
  const mid = state.activeMatterId;
  if (!mid) return;
  const btn = document.getElementById('dlExtractBtn');
  btn.disabled = true; btn.textContent = 'Extracting…';
  try {
    const data = await api('POST', `/api/matters/${mid}/deadlines/extract`);
    renderDeadlines(data.deadlines);
    toast(`Extracted ${data.extracted} deadline${data.extracted !== 1 ? 's' : ''}.`, 'success');
  } catch(e) {
    toast('Extraction failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = '⚡ Extract';
  }
}

function renderDeadlines(deadlines) {
  const el = document.getElementById('deadlineList');
  if (!deadlines || !deadlines.length) {
    el.innerHTML = '<div class="dl-empty">No deadlines found in indexed documents.</div>';
    return;
  }
  // Sort: critical first, then by date
  const urgencyOrder = {critical:0, high:1, normal:2};
  deadlines.sort((a,b) => (urgencyOrder[a.urgency]||2) - (urgencyOrder[b.urgency]||2) || (a.date_str||'').localeCompare(b.date_str||''));
  el.innerHTML = deadlines.map(d => `
    <div class="dl-row dl-urgency-${d.urgency || 'normal'}">
      <div class="dl-date">${escHtml(d.date_str || 'Date unknown')}</div>
      <div class="dl-badge dl-type-${d.dl_type || 'other'}">${escHtml((d.dl_type||'other').replace(/_/g,' '))}</div>
      <div class="dl-desc">${escHtml(d.description)}</div>
      <div class="dl-src">${escHtml(d.source_file || '')}</div>
    </div>
  `).join('');
}

function exportDeadlinesCSV() {
  const rows = document.querySelectorAll('.dl-row');
  if (!rows.length) return;
  const lines = ['Date,Type,Description,Source,Urgency'];
  rows.forEach(r => {
    const cells = r.querySelectorAll('.dl-date,.dl-badge,.dl-desc,.dl-src');
    const urgency = [...r.classList].find(c=>c.startsWith('dl-urgency-'))?.replace('dl-urgency-','') || '';
    lines.push([...cells].map(c => `"${c.textContent.replace(/"/g,'""')}"`).join(',') + `,"${urgency}"`);
  });
  const blob = new Blob([lines.join('\n')], {type:'text/csv'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `deadlines_matter_${state.activeMatterId}.csv`; a.click();
}

// ── Matter Auto-Brief ─────────────────────────────────────────────────────────

async function openBrief() {
  const mid = state.activeMatterId;
  if (!mid) { toast('Open a task first.', 'error'); return; }
  document.getElementById('briefModal').classList.remove('hidden');
  const briefEl = document.getElementById('briefContent');
  const risksEl = document.getElementById('briefRisks');
  const metaEl  = document.getElementById('briefMeta');
  briefEl.innerHTML = '<div class="brief-loading">Loading…</div>';
  risksEl.innerHTML = '';

  try {
    const data = await api('GET', `/api/matters/${mid}/brief`);
    if (data.has_brief) {
      renderBrief(data);
      if (data.stale) metaEl.innerHTML = '⚠ Brief may be stale — click "Regenerate" to refresh.';
      else metaEl.innerHTML = `Generated ${new Date(data.generated_at).toLocaleString()}`;
    } else {
      briefEl.innerHTML = '<div class="brief-loading">No brief yet. Click "Generate" to create one.</div>';
      metaEl.innerHTML = '';
    }
  } catch(e) {
    briefEl.innerHTML = `<div class="brief-loading">Error: ${escHtml(e.message)}</div>`;
  }
}

async function generateBrief() {
  const mid = state.activeMatterId;
  if (!mid) return;
  const btn = document.getElementById('briefGenBtn');
  btn.disabled = true; btn.textContent = 'Generating…';
  const briefEl = document.getElementById('briefContent');
  const risksEl = document.getElementById('briefRisks');
  briefEl.innerHTML = '<div class="brief-loading">⚡ Sherlock is reading the documents…</div>';
  risksEl.innerHTML = '';
  try {
    const data = await api('POST', `/api/matters/${mid}/brief/generate`);
    renderBrief(data);
    document.getElementById('briefMeta').innerHTML = `Generated ${new Date(data.generated_at).toLocaleString()}`;
    toast('Brief generated.', 'success');
  } catch(e) {
    briefEl.innerHTML = `<div class="brief-loading">Error: ${escHtml(e.message)}</div>`;
    toast('Brief failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = '⚡ Generate';
  }
}

function renderBrief(data) {
  document.getElementById('briefContent').innerHTML = mdToHtml(data.brief_md || '');
  document.getElementById('briefRisks').innerHTML = data.risks_md ? '<h4>⚠ Risks & Deadlines</h4>' + mdToHtml(data.risks_md) : '';
}

function mdToHtml(md) {
  if (!md) return '';
  return md
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g,'<em>$1</em>')
    .replace(/^### (.+)$/gm,'<h5>$1</h5>')
    .replace(/^## (.+)$/gm,'<h4>$1</h4>')
    .replace(/^# (.+)$/gm,'<h3>$1</h3>')
    .replace(/^[-•→] (.+)$/gm,'<li>$1</li>')
    .replace(/(<li>.*<\/li>)/gs,'<ul>$1</ul>')
    .replace(/\n\n/g,'</p><p>')
    .replace(/^(.+)$/gm, (l) => l.startsWith('<') ? l : `<p>${l}</p>`);
}

// ── Export Memo ───────────────────────────────────────────────────────────────

async function exportMemo(messageId) {
  try {
    const url = `/api/export/memo?message_id=${messageId}&format=docx`;
    const resp = await fetch(url, { headers: { 'Authorization': `Bearer ${state.token}` } });
    if (!resp.ok) throw new Error('Export failed');
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = resp.headers.get('content-disposition')?.match(/filename="(.+)"/)?.[1] || 'sherlock_memo.docx';
    a.click();
    toast('Memo exported.', 'success');
  } catch(e) {
    toast('Export failed: ' + e.message, 'error');
  }
}

// ── Usage Dashboard ────────────────────────────────────────────────────────────

async function loadUsage(days = 7) {
  const el = document.getElementById('usageDashboard');
  if (!el) return;
  el.innerHTML = '<div class="usage-loading">Loading usage data…</div>';
  try {
    const data = await api('GET', `/api/admin/usage?days=${days}`);
    renderUsage(data);
  } catch(e) {
    el.innerHTML = `<div class="usage-loading">Error: ${escHtml(e.message)}</div>`;
  }
}

function _fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return String(n);
}

function renderUsage(data) {
  const el = document.getElementById('usageDashboard');
  const maxDaily = Math.max(...(data.daily.map(d=>d.count)), 1);
  const typeColors = {auto:'#c9a84c',summary:'#4caf7a',timeline:'#6c8ef0',risk:'#e05c5c',drafting:'#c47fd5'};
  const tk = data.tokens || {};
  const maxUserTok = Math.max(...data.per_user.map(u=>u.total_tokens||0), 1);

  el.innerHTML = `
    <div class="usage-stat-row">
      <div class="usage-stat"><div class="usage-stat-val">${data.total_queries}</div><div class="usage-stat-lbl">Queries (${data.days}d)</div></div>
      <div class="usage-stat"><div class="usage-stat-val">${data.per_user.length}</div><div class="usage-stat-lbl">Active Users</div></div>
      <div class="usage-stat"><div class="usage-stat-val">${_fmtTokens(tk.total_tokens)}</div><div class="usage-stat-lbl">Total Tokens</div></div>
      <div class="usage-stat"><div class="usage-stat-val">${tk.avg_tokens_per_sec || 0}</div><div class="usage-stat-lbl">Avg tok/s</div></div>
    </div>

    <div class="usage-section">
      <div class="usage-section-title">Token Breakdown</div>
      <div class="usage-stat-row" style="gap:20px;justify-content:flex-start;">
        <div class="usage-stat"><div class="usage-stat-val" style="color:#6c8ef0">${_fmtTokens(tk.prompt_tokens)}</div><div class="usage-stat-lbl">Prompt Tokens</div></div>
        <div class="usage-stat"><div class="usage-stat-val" style="color:#4caf7a">${_fmtTokens(tk.completion_tokens)}</div><div class="usage-stat-lbl">Completion Tokens</div></div>
      </div>
    </div>

    <div class="usage-section">
      <div class="usage-section-title">By Source</div>
      <div class="usage-type-pills">
        ${Object.entries(data.by_source || {}).map(([src, s])=>{
          const color = src === 'user' ? '#c9a84c' : src.includes('embed') ? '#6c8ef0' : src.includes('brief') ? '#4caf7a' : src.includes('deadline') ? '#e05c5c' : '#888';
          return `<span class="usage-type-pill" style="border-color:${color};color:${color}" title="${s.queries} calls, ${_fmtTokens(s.prompt_tokens)} in, ${_fmtTokens(s.completion_tokens)} out">${src}: ${_fmtTokens(s.total_tokens)}</span>`;
        }).join('')}
      </div>
    </div>

    <div class="usage-section">
      <div class="usage-section-title">Daily Volume</div>
      <div class="usage-bar-chart">
        ${data.daily.map(d=>`
          <div class="usage-bar-col">
            <div class="usage-bar" style="height:${Math.round((d.count/maxDaily)*80)}px" title="${d.day}: ${d.count}"></div>
            <div class="usage-bar-label">${d.day.slice(5)}</div>
          </div>
        `).join('')}
      </div>
    </div>

    <div class="usage-section">
      <div class="usage-section-title">By User — Queries &amp; Tokens</div>
      ${data.per_user.map(u=>`
        <div class="usage-user-row">
          <span class="usage-user-name">${escHtml(u.display_name||u.username)}</span>
          <div class="usage-user-bar-wrap" title="${_fmtTokens(u.total_tokens||0)} tokens">
            <div class="usage-user-bar" style="width:${Math.round(((u.total_tokens||0)/maxUserTok)*100)}%"></div>
          </div>
          <span class="usage-user-count" title="${u.queries} queries, ${_fmtTokens(u.total_tokens||0)} tokens">${_fmtTokens(u.total_tokens||0)}</span>
        </div>
      `).join('')}
    </div>

    <div class="usage-section">
      <div class="usage-section-title">Query Types</div>
      <div class="usage-type-pills">
        ${Object.entries(data.by_type).map(([t,c])=>`
          <span class="usage-type-pill" style="border-color:${typeColors[t]||'#888'};color:${typeColors[t]||'#888'}">${t}: ${c}</span>
        `).join('')}
      </div>
    </div>
  `;
}

// ── Recent query history ──────────────────────────────────────────────────────

let _historyExpanded = true;

async function loadHistory() {
  try {
    const history = await api('GET', '/api/history?limit=15') || [];
    const el = document.getElementById('historyList');
    if (!el) return;

    if (!history.length) {
      el.innerHTML = '<div class="history-empty">No queries yet.</div>';
      return;
    }

    el.innerHTML = history.map(h => `
      <div class="history-item" onclick="jumpToHistory(${h.matter_id}, ${JSON.stringify(h.query)})" title="${escHtml(h.query)}">
        <span class="history-query">${escHtml(h.query)}</span>
        <span class="history-matter">${escHtml(h.matter_name)}</span>
      </div>
    `).join('');
  } catch {
    // Non-fatal — history just won't show
  }
}

function toggleHistorySection() {
  _historyExpanded = !_historyExpanded;
  document.getElementById('historyList').style.display = _historyExpanded ? '' : 'none';
  document.getElementById('historyToggle').textContent = _historyExpanded ? '▲' : '▼';
}

/* ── Sidebar resizer ──────────────────────────────────────────────────────────*/
(function initSidebarResizer() {
  const STORAGE_KEY = 'sherlock_sidebar_tasks_pct';
  const sidebar     = document.getElementById('sidebar');
  const resizer     = document.getElementById('sidebarResizer');
  if (!sidebar || !resizer) return;

  function applyPct(pct) {
    pct = Math.min(85, Math.max(10, pct));
    sidebar.style.setProperty('--sidebar-tasks-h', pct + '%');
  }

  // Restore persisted split
  const saved = parseFloat(localStorage.getItem(STORAGE_KEY));
  if (!isNaN(saved)) applyPct(saved);

  resizer.addEventListener('mousedown', function (e) {
    e.preventDefault();
    resizer.classList.add('dragging');

    const startY     = e.clientY;
    const footer     = sidebar.querySelector('.sidebar-footer');
    const footerH    = footer ? footer.offsetHeight : 0;
    const matterList = document.getElementById('matterList');
    const startPx    = matterList.offsetHeight;

    function usableH() {
      return sidebar.getBoundingClientRect().height - resizer.offsetHeight - footerH;
    }

    function onMouseMove(ev) {
      const delta  = ev.clientY - startY;
      const newPct = ((startPx + delta) / usableH()) * 100;
      applyPct(newPct);
    }

    function onMouseUp() {
      resizer.classList.remove('dragging');
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup',   onMouseUp);
      const pct = (matterList.offsetHeight / usableH()) * 100;
      localStorage.setItem(STORAGE_KEY, pct.toFixed(1));
    }

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup',   onMouseUp);
  });
})();

function jumpToHistory(matterId, query) {
  selectMatter(matterId);
  document.getElementById('chatInput').value = query;
  document.getElementById('chatInput').focus();
}

// ── Modals ────────────────────────────────────────────────────────────────────

function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-overlay:not(.hidden)').forEach(m => m.classList.add('hidden'));
});

// ── Toast ─────────────────────────────────────────────────────────────────────

let _toastTimeout;
function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type ? 'toast-' + type : ''}`;
  clearTimeout(_toastTimeout);
  _toastTimeout = setTimeout(() => el.classList.remove('show'), 3500);
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderSourceItem(s, i) {
  const isWeb = !!s.web;
  const hasPath = !!s.path && !isWeb;
  const clickable = isWeb || hasPath;
  const cls = `source-item${isWeb ? ' source-web' : ''}${clickable ? ' source-clickable' : ''}`;

  // Use data attributes to avoid quote-escaping issues in onclick
  const dataAttrs = hasPath
    ? `data-src-path="${escHtml(s.path || '')}" data-src-file="${escHtml(s.file || '')}"`
    : isWeb
    ? `data-src-url="${escHtml(s.path || '')}"`
    : '';

  const rowHandler = isWeb
    ? `onclick="window.open(this.dataset.srcUrl,'_blank')"`
    : hasPath
    ? `onclick="openInOS(this.dataset.srcPath)"`
    : '';

  const icon = hasPath ? ' <span class="source-link-icon">&#128279;</span>' : '';
  const actions = hasPath ? `
    <span class="source-actions">
      <button onclick="event.stopPropagation(); openInOS(this.closest('[data-src-path]').dataset.srcPath)" title="Open in default app">Open</button>
      <button onclick="event.stopPropagation(); downloadSource(this.closest('[data-src-path]').dataset.srcPath, this.closest('[data-src-path]').dataset.srcFile)" title="Download">&#8595;</button>
    </span>` : '';

  const excerpt = s.excerpt ? `<br><span class="source-excerpt">${escHtml(s.excerpt.substring(0, 150))}…</span>` : '';
  const score = s.score && s.score < 1.0 ? ` <span style="opacity:0.4;font-size:10px;">${Math.round(s.score * 100)}%</span>` : '';

  return `<div class="${cls}" ${dataAttrs} ${rowHandler}>
    <strong>[${i + 1}] ${isWeb ? '&#127760; ' : ''}${escHtml(s.file)}</strong>${score}${icon}${actions}${excerpt}
  </div>`;
}

async function openInOS(filePath) {
  try {
    const resp = await fetch(`/api/open?path=${encodeURIComponent(filePath)}`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${state.token}`,
        'X-CSRF-Token': getCsrfToken(),
      },
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      toast(err.detail || 'Could not open file', 'error');
    }
  } catch (e) {
    toast('Could not open file: ' + e.message, 'error');
  }
}

async function downloadSource(filePath, filename) {
  try {
    const resp = await fetch(`/api/preview?path=${encodeURIComponent(filePath)}`, {
      headers: { 'Authorization': `Bearer ${state.token}` }
    });
    if (!resp.ok) { toast('Download failed', 'error'); return; }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'document';
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast('Download failed: ' + e.message, 'error');
  }
}

function formatBytes(n) {
  if (!n) return '?';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n/1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n/1048576).toFixed(1)} MB`;
  return `${(n/1073741824).toFixed(2)} GB`;
}

function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fileIcon(type) {
  const icons = { pdf:'📄', docx:'📝', doc:'📝', txt:'📃', xlsx:'📊', xls:'📊', pptx:'📑', ppt:'📑',
    jpg:'🖼', jpeg:'🖼', png:'🖼', tiff:'🖼', mp3:'🎵', wav:'🎵', m4a:'🎵', eml:'📧' };
  return icons[type] || '📁';
}

// ── Tron cursor glow ───────────────────────────────────────────────────────────

(function initTronCursor() {
  const glow = document.createElement('div');
  glow.id = 'tron-cursor-glow';
  document.body.appendChild(glow);

  let active = false;

  document.addEventListener('mousemove', e => {
    if (document.documentElement.getAttribute('data-theme') !== 'tron') {
      glow.style.opacity = '0';
      return;
    }
    glow.style.left = e.clientX + 'px';
    glow.style.top  = e.clientY + 'px';
    if (active) glow.style.opacity = '1';
  });

  document.addEventListener('mouseover', e => {
    if (document.documentElement.getAttribute('data-theme') !== 'tron') return;
    const el = e.target.closest('button, a, [onclick], .matter-item, .source-item, .case-group-header, select, input, textarea, label');
    if (el) {
      active = true;
      glow.style.opacity = '1';
    }
  });

  document.addEventListener('mouseout', e => {
    const el = e.target.closest('button, a, [onclick], .matter-item, .source-item, .case-group-header, select, input, textarea, label');
    if (el) {
      active = false;
      glow.style.opacity = '0';
    }
  });
})();
