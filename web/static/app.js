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
  activeCase: localStorage.getItem('sherlock_active_case') || 'all',
  uploadJobs: {},
  csrfToken: null,
  chatHistory: [],  // last N turns for follow-up rewriting (ungated chat only)
};

// ── API helpers ───────────────────────────────────────────────────────────────

function getCsrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : (localStorage.getItem('sherlock_csrf_token') || state.csrfToken || '');
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

  // Restore last active view on refresh
  var savedView = localStorage.getItem('sherlock_view');
  if (savedView && VIEWS.indexOf(savedView) !== -1 && savedView !== 'chat') {
    setTimeout(function() { showView(savedView); }, 0);
  }

  document.getElementById('userPill').textContent = state.user.display_name || state.user.username;

  if (state.user.role === 'admin') {
    document.getElementById('nav-admin').classList.remove('hidden');
    document.getElementById('nav-config').classList.remove('hidden');
    document.getElementById('nav-logs').classList.remove('hidden');
  }

  initTheme();
  loadCaseSelector();
  loadMatters();
  loadCases();
  loadHistory();
  checkNasStatus();
  setInterval(checkNasStatus, 5 * 60 * 1000);

  // Restore persisted query mode preferences
  _applyQueryType(state.queryType);
  _applyVerbosityRole(state.verbosityRole);

  // Check research mode availability
  checkResearchStatus();

  // Citation footnote clicks — open file in preview panel
  document.getElementById('chatMessages').addEventListener('click', e => {
    const cite = e.target.closest('.cite-ref[data-src-path]');
    if (cite) openPreview(cite.dataset.srcPath, cite.dataset.srcFile);
  });
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

// ── Logs Page ─────────────────────────────────────────────────────────────────

let _logRefreshTimer = null;
let _logDebounceTimer = null;
let _logInterval = 2000;
let _logActivePill = null;
let _logAllEntries = [];

function initLogsView() {
  loadLogs();
  _logBindKeys();
}

function debounceLoadLogs() {
  clearTimeout(_logDebounceTimer);
  _logDebounceTimer = setTimeout(loadLogs, 350);
}

async function loadLogs(opts = {}) {
  const stream = document.getElementById('logStream')?.value || 'app';
  const level  = _logActivePill || document.getElementById('logLevel')?.value || '';
  const search = document.getElementById('logSearch')?.value || '';

  let url = `/api/admin/logs?stream=${encodeURIComponent(stream)}&lines=2000`;
  if (level)  url += `&level=${encodeURIComponent(level)}`;
  if (search) url += `&search=${encodeURIComponent(search)}`;

  let entries;
  try {
    const resp = await api('GET', url);
    entries = Array.isArray(resp) ? resp : (resp?.entries ?? resp);
  } catch (e) {
    if (!opts.silent) toast('Log fetch error: ' + e.message, 'error');
    return;
  }
  if (!entries) entries = [];

  // Time range filter (client-side)
  const range = document.getElementById('logTimeRange')?.value;
  if (range) {
    const now = Date.now();
    const ms = { '5m': 5*60e3, '15m': 15*60e3, '1h': 60*60e3, '6h': 6*60*60e3, '24h': 24*60*60e3 }[range];
    if (ms) {
      const cutoff = now - ms;
      entries = entries.filter(e => {
        if (!e.ts) return true;
        const t = new Date(e.ts).getTime();
        return isNaN(t) || t >= cutoff;
      });
    }
  }

  _logAllEntries = entries;
  _logUpdateSummary(entries);
  _logRenderTable(entries);

  // Auto-scroll to bottom if live
  if (document.getElementById('logLive')?.checked) {
    const scroll = document.getElementById('logScroll');
    if (scroll) requestAnimationFrame(() => { scroll.scrollTop = scroll.scrollHeight; });
  }
}

function _logUpdateSummary(entries) {
  const counts = { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 };
  entries.forEach(e => {
    const lvl = (e.level || 'INFO').toUpperCase();
    if (counts[lvl] !== undefined) counts[lvl]++;
  });

  const totalEl = document.getElementById('logTotal');
  if (totalEl) totalEl.textContent = entries.length.toLocaleString() + ' entries';

  ['DEBUG','INFO','WARNING','ERROR','CRITICAL'].forEach(lvl => {
    const pill = document.getElementById('logPill' + lvl);
    if (pill) {
      pill.textContent = counts[lvl] + ' ' + lvl;
      pill.classList.toggle('active', _logActivePill === lvl);
    }
  });
}

function _logRenderTable(entries) {
  const inner = document.getElementById('logScrollInner');
  if (!inner) return;

  if (!entries.length) {
    inner.innerHTML = '<div class="lv-empty">No log entries match.</div>';
    return;
  }

  const skip = new Set(['ts','level','logger','msg','message','rid','exc_info']);
  let html = '';

  entries.forEach((entry, i) => {
    const lvl = (entry.level || 'INFO').toUpperCase();
    const ts  = (entry.ts || '').replace('T',' ').replace(/\.\d+/,'').replace('Z','');
    const msg = escHtml(entry.msg || entry.message || '');
    const src = escHtml(entry.logger || '');
    const extras = Object.entries(entry)
      .filter(([k]) => !skip.has(k))
      .map(([k,v]) => `${escHtml(k)}=${escHtml(String(v))}`)
      .join('  ');

    html += `<div class="lv-row" data-level="${lvl}" data-idx="${i}" data-extras="${extras.replace(/"/g, '&quot;')}" onclick="_logToggleDetail(this)">
      <span class="lv-cell lv-cell-ts">${ts}</span>
      <span class="lv-cell lv-cell-lvl">${lvl}</span>
      <span class="lv-cell lv-cell-src">${src}</span>
      <span class="lv-cell lv-cell-msg">${msg}</span>
    </div>`;
  });

  inner.innerHTML = html;

  // Jump button visibility
  const scroll = document.getElementById('logScroll');
  const fab = document.getElementById('logJumpBottom');
  if (scroll && fab) {
    scroll.onscroll = () => {
      const atBot = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
      fab.classList.toggle('hidden', atBot || entries.length < 30);
    };
  }
}

function _logToggleDetail(row) {
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('lv-detail')) {
    existing.remove();
    return;
  }
  // Remove any other expanded
  row.closest('.lv-scroll-inner')?.querySelectorAll('.lv-detail').forEach(el => el.remove());

  const idx = parseInt(row.dataset.idx);
  const entry = _logAllEntries[idx];
  if (!entry) return;

  const detail = document.createElement('div');
  detail.className = 'lv-detail';

  const json = JSON.stringify(entry, null, 2)
    .replace(/("(?:[^"\\]|\\.)*")\s*:/g, '<span class="lv-json-key">$1</span>:')
    .replace(/:\s*("(?:[^"\\]|\\.)*")/g, ': <span class="lv-json-str">$1</span>')
    .replace(/:\s*(\d+\.?\d*)/g, ': <span class="lv-json-num">$1</span>')
    .replace(/:\s*(true|false)/g, ': <span class="lv-json-bool">$1</span>')
    .replace(/:\s*(null)/g, ': <span class="lv-json-null">$1</span>');

  detail.innerHTML =
    `<pre class="lv-detail-json">${json}</pre>` +
    `<div class="lv-detail-actions">` +
    `<button onclick="navigator.clipboard.writeText(JSON.stringify(_logAllEntries[${idx}],null,2));toast('Copied','success')">Copy JSON</button>` +
    `<button onclick="navigator.clipboard.writeText(_logAllEntries[${idx}].msg||_logAllEntries[${idx}].message||'');toast('Copied','success')">Copy Message</button>` +
    `</div>`;

  row.after(detail);
}

function filterLogByPill(level) {
  _logActivePill = _logActivePill === level ? null : level;
  document.getElementById('logLevel').value = '';
  loadLogs();
}

function toggleLogLive() {
  const cb  = document.getElementById('logLive');
  const dot = document.getElementById('logLiveDot');
  const lbl = document.getElementById('logLiveLabel');
  if (cb?.checked) {
    loadLogs();
    _logRefreshTimer = setInterval(() => loadLogs({ silent: true }), _logInterval);
    lbl?.classList.add('active');
    if (dot) dot.style.display = 'inline-block';
  } else {
    clearInterval(_logRefreshTimer);
    _logRefreshTimer = null;
    lbl?.classList.remove('active');
    if (dot) dot.style.display = 'none';
  }
}

function changeLogInterval() {
  _logInterval = parseInt(document.getElementById('logLiveInterval')?.value || '2000');
  if (_logRefreshTimer) {
    clearInterval(_logRefreshTimer);
    _logRefreshTimer = setInterval(() => loadLogs({ silent: true }), _logInterval);
  }
}

function logJumpToBottom() {
  const scroll = document.getElementById('logScroll');
  if (scroll) scroll.scrollTop = scroll.scrollHeight;
  document.getElementById('logJumpBottom')?.classList.add('hidden');
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

// Keyboard shortcuts for logs page
function _logKeyHandler(e) {
  const logsView = document.getElementById('view-logs');
  if (!logsView || logsView.classList.contains('hidden')) return;
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

  if (e.key === '/' ) { e.preventDefault(); document.getElementById('logSearch')?.focus(); }
  if (e.key === ' ') {
    e.preventDefault();
    const cb = document.getElementById('logLive');
    if (cb) { cb.checked = !cb.checked; toggleLogLive(); }
  }
}
function _logBindKeys()   { document.addEventListener('keydown', _logKeyHandler); }
function _logUnbindKeys() { document.removeEventListener('keydown', _logKeyHandler); }


// ── View switching ────────────────────────────────────────────────────────────

const VIEWS = ['chat', 'upload', 'outputs', 'dictations', 'admin', 'config', 'logs'];

function showView(name) {
  // Stop log live-refresh when leaving logs view
  if (name !== 'logs' && _logRefreshTimer) {
    clearInterval(_logRefreshTimer);
    _logRefreshTimer = null;
    const cb = document.getElementById('logLive');
    if (cb) cb.checked = false;
    document.getElementById('logLiveLabel')?.classList.remove('active');
  }

  localStorage.setItem('sherlock_view', name);
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
  if (name === 'logs') initLogsView();
  if (name === 'dictations') loadDictations();
}


// ── Dictations ────────────────────────────────────────────────────────────


var _assigneeList = [];

async function loadAssignees() {
  try {
    _assigneeList = await api('GET', '/api/dictations/assignees');
  } catch (e) { _assigneeList = []; }
}

function buildAssigneeOptions(current) {
  var known = _assigneeList.filter(function(a) { return a.active; }).map(function(a) { return a.name; });
  // Always include the current value even if not in the managed list
  if (current && known.indexOf(current) === -1) known.push(current);
  known.sort();
  var opts = '<option value="">(unassigned)</option>';
  known.forEach(function(name) {
    opts += '<option value="' + escHtml(name) + '"' + (name === current ? ' selected' : '') + '>' + escHtml(name) + '</option>';
  });
  return opts;
}

async function updateDictTaskField(taskId, field, value) {
  try {
    var body = {};
    body[field] = value;
    await api('PATCH', '/api/dictations/tasks/' + taskId, body);
    toast('Task updated', 'success');
  } catch (e) {
    toast('Update failed: ' + e.message, 'error');
  }
}

function openAssigneeManager() {
  var modal = document.getElementById('assigneeModal');
  if (!modal) return;
  modal.classList.remove('hidden');
  renderAssigneeList();
}

function closeAssigneeModal() {
  var modal = document.getElementById('assigneeModal');
  if (modal) modal.classList.add('hidden');
}

function renderAssigneeList() {
  var el = document.getElementById('assigneeListBody');
  if (!el) return;
  var html = '';
  _assigneeList.forEach(function(a) {
    html += '<tr>' +
      '<td>' + escHtml(a.name) + '</td>' +
      '<td>' + escHtml(a.role || '') + '</td>' +
      '<td>' + (a.active ? 'Active' : '<span class="muted">Inactive</span>') + '</td>' +
      '<td style="text-align:right;">' +
        '<button class="btn" style="font-size:11px;padding:2px 8px;margin-right:4px;" onclick="toggleAssigneeActive(' + a.id + ',' + (a.active ? 'false' : 'true') + ')">' + (a.active ? 'Deactivate' : 'Activate') + '</button>' +
        '<button class="btn btn-danger" style="font-size:11px;padding:2px 8px;" onclick="deleteAssignee(' + a.id + ')">Delete</button>' +
      '</td></tr>';
  });
  if (!html) html = '<tr><td colspan="4" class="muted" style="text-align:center;">No assignees configured. Add one below.</td></tr>';
  el.innerHTML = html;
}

async function addAssignee() {
  var nameEl = document.getElementById('newAssigneeName');
  var roleEl = document.getElementById('newAssigneeRole');
  var name = nameEl.value.trim();
  var role = roleEl.value.trim();
  if (!name) { toast('Enter a name', 'error'); return; }
  try {
    await api('POST', '/api/dictations/assignees', { name: name, role: role });
    nameEl.value = '';
    roleEl.value = '';
    await loadAssignees();
    renderAssigneeList();
    toast('Assignee added', 'success');
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function toggleAssigneeActive(id, active) {
  try {
    await api('PATCH', '/api/dictations/assignees/' + id, { active: active });
    await loadAssignees();
    renderAssigneeList();
  } catch (e) { toast('Failed: ' + e.message, 'error'); }
}

async function deleteAssignee(id) {
  if (!confirm('Delete this assignee?')) return;
  try {
    await api('DELETE', '/api/dictations/assignees/' + id);
    await loadAssignees();
    renderAssigneeList();
    toast('Assignee deleted', 'success');
  } catch (e) { toast('Failed: ' + e.message, 'error'); }
}

async function loadDictations() {
  try {
    if (!_assigneeList.length) await loadAssignees();
    const data = await api('GET', '/api/dictations');
    const el = document.getElementById('dictationsList');
    const sum = data.summary;
    document.getElementById('dictationSummary').textContent =
      sum.total_files + ' recordings, ' + sum.total_tasks + ' tasks, ' + sum.pending + ' pending';

    // Build assignee filter (merge task assignees + managed list)
    const assignees = new Set();
    _assigneeList.filter(function(a) { return a.active; }).forEach(function(a) { assignees.add(a.name); });
    data.dictations.forEach(d => d.tasks.forEach(t => assignees.add(t.assignee)));
    const sel = document.getElementById('dictFilterAssignee');
    const curVal = sel.value;
    sel.innerHTML = '<option value="">All Assignees</option>' +
      [...assignees].sort().map(a => '<option value="' + a + '"' + (a === curVal ? ' selected' : '') + '>' + a + '</option>').join('');

    const filterAssignee = curVal;

    if (!data.dictations.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#127908;</div><p>No dictations analyzed yet.</p></div>';
      return;
    }

    let html = '';
    data.dictations.forEach(d => {
      const tasks = filterAssignee ? d.tasks.filter(t => t.assignee === filterAssignee) : d.tasks;
      if (filterAssignee && !tasks.length) return;
      const date = d.recorded_at ? new Date(d.recorded_at).toLocaleString() : 'Unknown';
      const dur = d.duration_secs ? d.duration_secs + 's' : '';
      const hasUrgent = tasks.some(t => t.priority === 'urgent');

      let taskRows = '';
      tasks.forEach(t => {
        const cls = (t.status === 'completed' ? ' task-done' : '') + (t.priority === 'urgent' ? ' task-urgent' : '');
        const pri = t.priority === 'urgent' ? '<span class="tag tag-urgent">URGENT</span>' : '<span class="tag">normal</span>';
        taskRows += '<tr class="' + cls + '">' +
          '<td>' + t.order + '</td>' +
          '<td><select onchange="updateDictTaskField(' + t.id + ', \'assignee\', this.value)" style="font-size:11px;padding:2px 4px;border-radius:4px;border:1px solid var(--border);background:var(--surface);font-weight:600;">' + buildAssigneeOptions(t.assignee) + '</select></td>' +
          '<td>' + escHtml(t.action) + '</td>' +
          '<td class="muted">' + (t.case_folder ? '<a href="#" class="case-link" onclick="event.preventDefault();browseCase(\x27' + escHtml(t.case_folder) + '\x27)" title="' + escHtml(t.case_folder) + '">' + escHtml(t.client_or_case) + ' &#128206;</a>' : escHtml(t.client_or_case || '\u2014')) + '</td>' +
          '<td>' + pri + '</td>' +
          '<td class="muted">' + escHtml(t.due_hint || '\u2014') + '</td>' +
          '<td><select onchange="updateDictTask(' + t.id + ', this.value)" style="font-size:11px;padding:2px 4px;border-radius:4px;border:1px solid var(--border);background:var(--surface);">' +
            '<option value="pending"' + (t.status === 'pending' ? ' selected' : '') + '>Pending</option>' +
            '<option value="in_progress"' + (t.status === 'in_progress' ? ' selected' : '') + '>In Progress</option>' +
            '<option value="completed"' + (t.status === 'completed' ? ' selected' : '') + '>Completed</option>' +
            '<option value="dismissed"' + (t.status === 'dismissed' ? ' selected' : '') + '>Dismissed</option>' +
          '</select></td></tr>';
      });

      html += '<div class="dict-card">' +
        '<div class="dict-header" data-action="toggle-dict">' +
          '<div class="dict-meta">' +
            '<button class="btn dict-play-btn" data-audio="' + encodeURIComponent(d.file_name) + '" onclick="event.stopPropagation();playDictation(this)" title="Play audio">&#9654;</button> ' +
            '<span class="dict-date">' + date + '</span> ' +
            '<span class="dict-dur">' + dur + '</span> ' +
            '<span class="dict-count">' + tasks.length + ' task' + (tasks.length !== 1 ? 's' : '') + '</span> ' +
            (hasUrgent ? '<span class="tag tag-urgent">URGENT</span>' : '') +
          '</div>' +
          '<div class="dict-fname muted">' + escHtml(d.file_name) + '</div>' +
        '</div>' +
        '<div class="dict-body">' +
          '<div class="dict-audio-player" id="audio-' + d.id + '"></div>' +
          '<div class="dict-transcript"><strong>Transcript:</strong> <span class="dict-transcript-text">' + escHtml(d.transcript || '') + '</span>' +
          '<div style="margin-top:6px;text-align:right;">' +
          '<button class="btn" style="font-size:11px;padding:3px 10px;margin-right:6px;" onclick="reprocessDictation(' + d.id + ')">\u21bb Re-process</button>' +
          '<button class="btn" style="font-size:11px;padding:3px 10px;" onclick="correctTranscript(' + d.id + ')">Correct Selection</button></div></div>' +
          '<table class="dict-task-table"><thead><tr>' +
            '<th>#</th><th>Assignee</th><th>Task</th><th>Case/Client</th><th>Priority</th><th>Due</th><th>Status</th>' +
          '</tr></thead><tbody>' + taskRows + '</tbody></table>' +
        '</div></div>';
    });
    el.innerHTML = html;
  } catch (e) {
    toast('Failed to load dictations: ' + e.message, 'error');
  }
}

async function updateDictTask(taskId, newStatus) {
  try {
    await api('PATCH', '/api/dictations/tasks/' + taskId, { status: newStatus });
    loadDictations();
  } catch (e) {
    toast('Update failed: ' + e.message, 'error');
  }
}

async function scanDictations() {
  const btn = document.getElementById('dictScanBtn');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  try {
    await api('POST', '/api/dictations/scan');
    toast('Dictation scan started', 'success');
    setTimeout(function() {
      btn.disabled = false;
      btn.textContent = 'Scan Now';
      loadDictations();
    }, 10000);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Scan Now';
    toast('Scan failed: ' + e.message, 'error');
  }
}



document.addEventListener('click', function(e) {
  var hdr = e.target.closest('[data-action="toggle-dict"]');
  if (hdr) hdr.parentElement.classList.toggle('expanded');
});


function playDictation(btn) {
  var fname = decodeURIComponent(btn.dataset.audio);
  var card = btn.closest('.dict-card');
  var player = card.querySelector('.dict-audio-player');
  if (player.querySelector('audio')) {
    var audio = player.querySelector('audio');
    if (audio.paused) { audio.play(); btn.innerHTML = '&#9646;&#9646;'; }
    else { audio.pause(); btn.innerHTML = '&#9654;'; }
    return;
  }
  var audio = document.createElement('audio');
  audio.controls = true;
  audio.autoplay = true;
  audio.style.cssText = 'width:100%;margin-bottom:8px;';
  var source = document.createElement('source');
  source.src = '/api/dictations/audio/' + encodeURIComponent(fname);
  source.type = 'audio/mp4';
  audio.appendChild(source);
  audio.addEventListener('play', function() { btn.innerHTML = '\u23F8'; });
  audio.addEventListener('pause', function() { btn.innerHTML = '\u25B6'; });
  audio.addEventListener('ended', function() { btn.innerHTML = '\u25B6'; });
  player.innerHTML = '';
  player.appendChild(audio);
  btn.innerHTML = '\u23F8';
  if (!card.classList.contains('expanded')) card.classList.add('expanded');
}

function browseCase(folderPath) {
  toast('Case folder: ' + folderPath, 'info');
}

function correctTranscript(dictId) {
  var sel = window.getSelection();
  var wrong = sel.toString().trim();
  if (!wrong) {
    toast('Select the incorrect word(s) in the transcript first, then click Correct', 'info');
    return;
  }
  // Capture the card and transcript span BEFORE prompt() steals focus/selection
  var node = sel.anchorNode;
  var card = node ? (node.closest ? node.closest('.dict-card') : node.parentElement ? node.parentElement.closest('.dict-card') : null) : null;
  var span = card ? card.querySelector('.dict-transcript-text') : null;

  var correct = prompt('Correct "' + wrong + '" to:');
  if (!correct || !correct.trim()) return;
  var corrected = correct.trim();
  api('POST', '/api/dictations/vocab', {
    wrong: wrong,
    correct: corrected,
    dictation_id: dictId
  }).then(function(r) {
    toast('"' + wrong + '" corrected to "' + corrected + '" — Sherlock will remember this', 'success');
    // Update transcript text in-place without collapsing the card
    if (span) {
      span.textContent = span.textContent.split(wrong).join(corrected);
    }
  }).catch(function(e) {
    toast('Correction failed: ' + e.message, 'error');
  });
}

async function reprocessDictation(dictId) {
  if (!confirm('Re-process this dictation? It will be re-transcribed and re-analyzed.')) return;
  try {
    toast('Re-processing dictation...', 'info');
    await api('POST', '/api/dictations/' + dictId + '/reprocess');
    toast('Re-processing started. Refresh in ~30s to see updated results.', 'success');
    setTimeout(function() { loadDictations(); }, 5000);
  } catch (e) {
    toast('Re-process failed: ' + e.message, 'error');
  }
}





// ── Case Selector ─────────────────────────────────────────────────────────

var _caseList = []; // cached for filtering

async function loadCaseSelector() {
  try {
    var data = await api('GET', '/api/catalog/clients?limit=2000');
    _caseList = (data.clients || []).map(function(c) {
      return {
        key: (c.category || 'Other') + '/' + c.client_folder,
        label: c.client_folder,
        category: c.category || 'Other',
        file_count: c.file_count
      };
    }).sort(function(a, b) { return a.label.localeCompare(b.label); });

    // Restore input display
    var input = document.getElementById('caseSearchInput');
    if (state.activeCase && state.activeCase !== 'all') {
      var match = _caseList.find(function(c) { return c.key === state.activeCase; });
      if (match) input.value = match.label;
      else input.value = '';
    } else {
      input.value = '';
      input.placeholder = 'All Cases (' + _caseList.length + ' clients) — type to search...';
    }

    updateCaseMeta();
  } catch (e) {
    console.warn('Failed to load case selector:', e);
  }
}

function openCaseDropdown() {
  filterCaseDropdown(document.getElementById('caseSearchInput').value);
  document.getElementById('caseDropdown').classList.remove('hidden');
}

function closeCaseDropdown() {
  setTimeout(function() {
    document.getElementById('caseDropdown').classList.add('hidden');
  }, 200);
}

// Close dropdown when clicking outside
document.addEventListener('click', function(e) {
  if (!e.target.closest('#caseSearchWrap')) {
    document.getElementById('caseDropdown').classList.add('hidden');
  }
});

function filterCaseDropdown(query) {
  var dd = document.getElementById('caseDropdown');
  var q = (query || '').toLowerCase();

  var html = '<div class="case-dd-item" onclick="selectCase(\x27all\x27)"><strong>All Cases</strong> <span class="muted">(' + _caseList.length + ' clients)</span></div>';

  // Group filtered results by category
  var byCategory = {};
  _caseList.forEach(function(c) {
    if (q && c.label.toLowerCase().indexOf(q) === -1 && c.category.toLowerCase().indexOf(q) === -1) return;
    if (!byCategory[c.category]) byCategory[c.category] = [];
    byCategory[c.category].push(c);
  });

  var categories = Object.keys(byCategory).sort();
  var totalShown = 0;
  categories.forEach(function(cat) {
    html += '<div class="case-dd-category">' + escHtml(cat) + '</div>';
    byCategory[cat].forEach(function(c) {
      if (totalShown >= 50) return; // limit visible items
      var active = (c.key === state.activeCase) ? ' case-dd-active' : '';
      html += '<div class="case-dd-item' + active + '" onclick="selectCase(\x27' + c.key.replace(/'/g, "\\'") + '\x27)">' +
        escHtml(c.label) + ' <span class="muted">(' + c.file_count + ')</span></div>';
      totalShown++;
    });
  });

  if (totalShown === 0 && q) {
    html += '<div class="case-dd-empty">No cases matching "' + escHtml(q) + '"</div>';
  }

  dd.innerHTML = html;
  dd.classList.remove('hidden');
}

function selectCase(key) {
  var input = document.getElementById('caseSearchInput');
  if (key === 'all') {
    input.value = '';
    input.placeholder = 'All Cases (' + _caseList.length + ' clients) — type to search...';
  } else {
    var match = _caseList.find(function(c) { return c.key === key; });
    input.value = match ? match.label : key;
  }
  document.getElementById('caseDropdown').classList.add('hidden');
  onCaseSelected(key);
}

function onCaseSelected(value) {
  state.activeCase = value;
  localStorage.setItem('sherlock_active_case', value);
  updateCaseMeta();

  // Update scope automatically
  if (value === 'all') {
    setScope('all');
  } else {
    setScope('case');
  }
}

function updateCaseMeta() {
  var meta = document.getElementById('caseSelectorMeta');
  var title = document.getElementById('chatMatterTitle');
  if (state.activeCase === 'all') {
    meta.textContent = 'Searching all indexed documents';
    if (title && !state.activeMatterId) title.textContent = 'All Cases';
  } else {
    var parts = state.activeCase.split('/');
    var cat = parts[0];
    var client = parts.slice(1).join('/');
    meta.textContent = cat + ' — ' + client;
    if (title && !state.activeMatterId) title.textContent = client;
  }
}

function getActiveCaseClient() {
  // Returns {category, client_folder} or null
  if (!state.activeCase || state.activeCase === 'all') return null;
  var parts = state.activeCase.split('/');
  return { category: parts[0], client_folder: parts.slice(1).join('/') };
}

function openNewCaseModal() {
  document.getElementById('newCaseClient').value = '';
  document.getElementById('newCaseError').classList.add('hidden');
  document.getElementById('newCaseModal').classList.remove('hidden');
}

async function createNewCase() {
  var client = document.getElementById('newCaseClient').value.trim();
  var category = document.getElementById('newCaseCategory').value;
  var errEl = document.getElementById('newCaseError');
  if (!client) {
    errEl.textContent = 'Client name is required';
    errEl.classList.remove('hidden');
    return;
  }
  try {
    var result = await api('POST', '/api/catalog/create-case', {
      client_name: client, category: category
    });
    closeModal('newCaseModal');
    if (result.created) {
      toast('Case folder created: ' + category + '/' + client, 'success');
    } else {
      toast('Folder already exists — selecting it', 'info');
    }
    // Reload and select the new case
    await loadCaseSelector();
    var key = category + '/' + client;
    document.getElementById('caseSelector').value = key;
    onCaseSelected(key);
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
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
  state.chatHistory = [];
  state.activeMatterId = id;
  loadMatterFiles(id);
  const matter = state.matters.find(m => m.id === id);
  renderMatters();

  document.getElementById('chatMatterTitle').textContent = matter?.name || 'All Indexed Files';
  document.getElementById('editTaskBtn').classList.toggle('hidden', !matter);
  document.getElementById('exportBtn').classList.toggle('hidden', !matter);

  // Case context bar (elements may not exist if Cases tab was removed)
  const ctxBar = document.getElementById('caseCtxBar');
  const caseScopeBtn = document.getElementById('scopeCaseBtn');

  if (matter?.case_id) {
    const ctxName = document.getElementById('caseCtxName');
    if (ctxName) ctxName.textContent = matter.case_name || `Case #${matter.case_id}`;
    const metaParts = [];
    if (matter.case_number) metaParts.push(matter.case_number);
    if (matter.case_type)   metaParts.push(matter.case_type);
    if (matter.client_name) metaParts.push(`Client: ${matter.client_name}`);
    if (matter.opposing_party) metaParts.push(`vs. ${matter.opposing_party}`);
    const ctxMeta = document.getElementById('caseCtxMeta');
    if (ctxMeta) ctxMeta.textContent = metaParts.join('  ·  ');
    if (ctxBar) ctxBar.classList.remove('hidden');
    if (caseScopeBtn) caseScopeBtn.classList.remove('hidden');

    setScope('case');
  } else {
    if (ctxBar) ctxBar.classList.add('hidden');
    if (caseScopeBtn) caseScopeBtn.classList.add('hidden');
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

  const bubbleHtml = msg.role === 'assistant' && msg.content
    ? linkifyCitations(renderMd(msg.content), msg.sources || [])
    : escHtml(msg.content);

  return `
    <div class="msg ${msg.role}" id="msg-${msg.id}">
      <div class="msg-bubble">${bubbleHtml}</div>
      ${sourcesHtml}
      ${actionsHtml}
    </div>`;
}

// ── New matter modal ──────────────────────────────────────────────────────────


// -- Pending matter-from-case flow --
let _pendingMatterFromCase = false;
let _pendingMatterName = '';

function openNewMatterModal() {
  document.getElementById('newMatterName').value = '';
  document.getElementById('newMatterError').classList.add('hidden');

  // Populate case dropdown
  const sel = document.getElementById('newMatterCaseId');
  sel.innerHTML = '<option value="">— No case —</option>' +
    state.cases
      .filter(c => c.status === 'active')
      .map(c => `<option value="${c.id}">${escHtml(c.case_name)}${c.case_number ? ` (${escHtml(c.case_number)})` : ''}</option>`)
      .join('') +
    '<option value="__new__">+ Create New Case...</option>';

  // Listen for "+ Create New Case" selection
  sel.onchange = function() {
    if (sel.value === '__new__') {
      sel.value = '';
      _pendingMatterFromCase = true;
      _pendingMatterName = document.getElementById('newMatterName').value;
      closeModal('newMatterModal');
      openNewCaseModal();
    }
  };

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

  // If coming from New Task flow, update cancel to return to matter modal
  const cancelBtn = document.querySelector('#newCaseModal .btn[onclick*="closeModal"]');
  if (_pendingMatterFromCase && cancelBtn) {
    cancelBtn.onclick = function() {
      _pendingMatterFromCase = false;
      _pendingMatterName = '';
      closeModal('newCaseModal');
      openNewMatterModal();
    };
  } else if (cancelBtn) {
    cancelBtn.onclick = function() { closeModal('newCaseModal'); };
  }

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

    // If we came from the New Task dialog, reopen it with the new case selected
    if (_pendingMatterFromCase) {
      _pendingMatterFromCase = false;
      openNewMatterModal();
      document.getElementById('newMatterName').value = _pendingMatterName || '';
      document.getElementById('newMatterCaseId').value = String(newCase.id);
      _pendingMatterName = '';
    }
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
  scrollToBottom(true);

  try {
    const chatUrl = state.activeMatterId
      ? `/api/matters/${state.activeMatterId}/chat`
      : '/api/chat';
    if (!state.activeMatterId) state.chatHistory.push({ role: 'user', content: text });
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
        history: state.activeMatterId ? [] : state.chatHistory.slice(-6),
        case_filter: getActiveCaseClient(),
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
                `<span title="LLM latency">${((ts.latency_llm_ms || 0) / 1000).toFixed(1)}s</span>` +
                (ts.source === 'cloud'
                  ? `<span class="badge-cloud" title="${ts.cloud_provider}/${ts.cloud_model} · ${ts.entities_scrubbed || 0} entities scrubbed · $${(ts.cost_usd || 0).toFixed(4)}">☁️ Cloud</span>`
                  : `<span class="badge-local" title="Processed locally on-premise">🔒 Local</span>`);
              aiDiv.appendChild(statsDiv);
            }
          }
        } catch {}
      }
      scrollToBottom();
    }

    // Final render: convert markdown + linkify citation footnotes
    if (fullText) {
      bubble.innerHTML = linkifyCitations(renderMd(fullText), sources);
      if (!state.activeMatterId) state.chatHistory.push({ role: 'assistant', content: fullText.slice(0, 600) });
    }

    // Only show sources the LLM actually cited in the response
    const citedSources = sources.filter(s => {
      if (!s.file) return false;
      const name = s.file.toLowerCase();
      const text = fullText.toLowerCase();
      // Match [filename] citation in response
      return text.includes('[' + name) || text.includes(name.replace(/\.[^.]+$/, ''));
    });

    // Add sources + action buttons
    if (citedSources.length) {
      const srcDiv = document.createElement('div');
      srcDiv.className = 'sources-list';
      srcDiv.innerHTML = `<div class="sources-title">Sources</div>` +
        citedSources.map((s, i) => renderSourceItem(s, i)).join('');
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

function scrollToBottom(force = false) {
  const el = document.getElementById('chatMessages');
  // Only auto-scroll if user is near the bottom (within 150px) or forced
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 150;
  if (force || nearBottom) {
    el.scrollTop = el.scrollHeight;
  }
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
  if (files.length > 20) {
    toast('Maximum 20 files at a time. Please upload in smaller batches.', 'error');
    return;
  }
  const names = files.map(f => f.name);
  toast(`Uploading ${names.length} file${names.length > 1 ? 's' : ''}...`);

  // Upload all files and collect job IDs
  const jobs = [];
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    if (state.activeMatterId) fd.append('matter_id', state.activeMatterId);
    _addUploadProgress(file.name);
    try {
      const result = await apiUpload('/api/upload', fd);
      if (result.duplicate) {
        _updateUploadProgress(file.name, 'Duplicate');
        toast(`${file.name} already uploaded — skipped`, 'info');
      } else {
        jobs.push({ job_id: result.job_id, filename: result.filename || file.name });
        toast(`Uploaded ${file.name}, indexing...`);
      }
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
            _updateUploadProgress(job.filename, 'Ready');
            toast(`${job.filename} indexed (${status.indexed} chunks)`, 'success');
          } else {
            _updateUploadProgress(job.filename, 'Error');
            toast(`Indexing failed for ${job.filename}`, 'error');
          }
        }
      } catch { /* retry next poll */ }
    }
  }

  // Refresh file panel with real data
  if (state.activeMatterId) loadMatterFiles(state.activeMatterId);

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
  if (files.length > 20) {
    toast('Maximum 20 files at a time. Please upload in smaller batches.', 'error');
    return;
  }
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
          <h3>&#128193; NAS Paths <span class="config-hint">(global index — edit sherlock.conf to change)</span></h3>
          ${cfg.nas.paths.length
            ? `<div class="nas-path-list">${cfg.nas.paths.map(p => `<div class="nas-path-item"><code>${escHtml(p)}</code></div>`).join('')}</div>`
            : `<p class="config-none">No global NAS paths configured. Add <code>NAS_PATHS=</code> to sherlock.conf and restart.</p>`
          }
        </div>

        <div class="config-section">
          <h3>&#128202; Index Stats</h3>
          <div class="config-table">
            <div class="config-row"><span>Active Cases</span><code>${cfg.stats.active_cases}</code></div>
            <div class="config-row"><span>Total Indexed Files</span><code>${cfg.stats.indexed_files.toLocaleString()}</code></div>
          </div>
        </div>

      </div>
      <p class="config-footer">To change settings, edit <code>~/Sherlock/sherlock.conf</code> and restart the web service.</p>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Failed to load config: ${escHtml(e.message)}</p></div>`;
  }
}

// ── Admin ─────────────────────────────────────────────────────────────────────

async function loadAdmin() {
  if (state.user.role !== 'admin') return;
  loadSystemStatus();
  loadUsers();
  loadLogs();
  loadUsage(7);
  loadFilterRules();
  loadCatalogStatus();
  loadTextStatus();
  loadEmbedStatus();
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
        <td style="display:flex;gap:4px;flex-wrap:wrap;">
          <button class="btn" style="font-size:11px;padding:3px 8px;" onclick="toggleUserActive(${u.id}, ${u.active})">
            ${u.active ? 'Deactivate' : 'Activate'}
          </button>
          <button class="btn" style="font-size:11px;padding:3px 8px;" data-uid="${u.id}" data-uname="${escHtml(u.username)}" onclick="openResetPasswordModal(+this.dataset.uid, this.dataset.uname)">
            Reset Pwd
          </button>
          <button class="btn btn-danger" style="font-size:11px;padding:3px 8px;" data-uid="${u.id}" data-uname="${escHtml(u.username)}" onclick="confirmDeleteUser(+this.dataset.uid, this.dataset.uname)">
            Delete
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

function openResetPasswordModal(userId, username) {
  const newPwd = prompt(`Enter new password for "${username}":`);
  if (!newPwd) return;
  if (newPwd.length < 4) { toast('Password must be at least 4 characters', 'error'); return; }
  resetUserPassword(userId, username, newPwd);
}

async function resetUserPassword(userId, username, newPassword) {
  try {
    await api('PATCH', `/api/admin/users/${userId}`, { new_password: newPassword });
    toast(`Password reset for ${username}`, 'success');
  } catch (e) {
    toast('Password reset failed: ' + e.message, 'error');
  }
}

async function confirmDeleteUser(userId, username) {
  if (!confirm(`Are you sure you want to permanently delete user "${username}"?\n\nThis cannot be undone.`)) return;
  try {
    await api('DELETE', `/api/admin/users/${userId}`);
    toast(`User "${username}" deleted`, 'success');
    loadUsers();
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
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

// ── Markdown renderer ─────────────────────────────────────────────────────────

function renderMd(text) {
  // HTML-escape first so inline patterns operate on safe text
  let s = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Fenced code blocks (``` ... ```)
  s = s.replace(/```[\s\S]*?```/g, m => {
    const inner = m.slice(3, -3).replace(/^\w*\n/, '');
    return `<pre><code>${inner}</code></pre>`;
  });

  // Inline code
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');

  // Headers
  s = s.replace(/^### (.+)$/gm, '<h4>$1</h4>');
  s = s.replace(/^## (.+)$/gm,  '<h3>$1</h3>');
  s = s.replace(/^# (.+)$/gm,   '<h2>$1</h2>');

  // Bold + italic
  s = s.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*(.+?)\*\*/g,     '<strong>$1</strong>');
  s = s.replace(/\*([^*\n]+?)\*/g,    '<em>$1</em>');

  // Lists — bullet then numbered
  s = s.replace(/^[-•*] (.+)$/gm,  '<li>$1</li>');
  s = s.replace(/^\d+\. (.+)$/gm,  '<li>$1</li>');
  // Wrap runs of <li> in <ul>
  s = s.replace(/(<li>[\s\S]+?<\/li>(\n|$))+/g, m => `<ul>${m}</ul>`);

  // Paragraphs — split on blank lines; skip block elements
  s = s.split(/\n\n+/).map(para => {
    para = para.trim();
    if (!para) return '';
    if (/^<(?:h[2-4]|ul|ol|pre)/.test(para)) return para;
    return `<p>${para.replace(/\n/g, '<br>')}</p>`;
  }).join('');

  return s;
}

// ── Citation linkifier ────────────────────────────────────────────────────────

function linkifyCitations(html, sources) {
  if (!sources?.length) return html;

  // Build lookup keyed by lowercased filename for case-insensitive matching
  const fileMap = {};
  sources.forEach((s, i) => {
    if (s.file) fileMap[s.file.toLowerCase()] = { idx: i, path: s.path || '', file: s.file };
  });

  // Normalize raw citation text to a bare filename key:
  //   [Doc: Smith.txt]  → "smith.txt"
  //   [Smith.txt, p.5]  → "smith.txt"
  //   [Smith.txt]       → "smith.txt"
  function normName(raw) {
    let s = raw.trim();
    s = s.replace(/^Doc:\s*/i, '');    // strip "Doc: " prefix
    s = s.replace(/,.*$/, '').trim();  // strip ", location/date" suffix
    return s.toLowerCase();
  }

  return html.replace(/\[([^\]<>\n]{1,120})\]/g, (match, name) => {
    if (/^Web:/i.test(name.trim())) return match; // leave [Web: ...] as plain text
    const key   = normName(name);
    const entry = fileMap[key];
    if (!entry) return match;
    const num       = entry.idx + 1;
    const safePath  = (entry.path || '').replace(/"/g, '&quot;');
    const safeFile  = (entry.file || '').replace(/"/g, '&quot;');
    const safeTitle = (entry.file || name).replace(/"/g, '&quot;');
    if (entry.path) {
      return `<sup class="cite-ref" data-src-path="${safePath}" data-src-file="${safeFile}" title="${safeTitle}">[${num}]</sup>`;
    }
    return `<sup class="cite-ref" title="${safeTitle}">[${num}]</sup>`;
  });
}


// ── Matter Files Panel ────────────────────────────────────────────────────────

async function loadMatterFiles(matterId) {
  const panel = document.getElementById('matterFilesPanel');
  const list = document.getElementById('matterFilesList');
  const count = document.getElementById('matterFileCount');
  if (!matterId) {
    panel.classList.add('hidden');
    return;
  }
  try {
    const files = await api('GET', `/api/matters/${matterId}/files`) || [];
    count.textContent = files.length;
    if (files.length === 0) {
      panel.classList.add('hidden');
      return;
    }
    panel.classList.remove('hidden');
    list.innerHTML = files.map(f => {
      const size = f.size_bytes ? (f.size_bytes < 1048576
        ? (f.size_bytes / 1024).toFixed(0) + ' KB'
        : (f.size_bytes / 1048576).toFixed(1) + ' MB') : '';
      const pages = f.page_count ? `${f.page_count}p` : '';
      const meta = [size, pages].filter(Boolean).join(' / ');
      const statusCls = f.status || 'pending';
      const retryBtn = f.status === 'error'
        ? `<button class="matter-file-retry" onclick="event.stopPropagation(); retryIndex(${f.upload_id})" title="Retry indexing">&#8635;</button>`
        : '';
      return `<div class="matter-file-item" title="${escHtml(f.filename)}"
                   onclick="downloadMatterFile(${f.upload_id}, '${escHtml(f.filename)}')">
        <span class="matter-file-name">${escHtml(f.filename)}</span>
        <span class="matter-file-meta">${meta}</span>
        <span class="matter-file-status ${statusCls}">${statusCls}</span>
        ${retryBtn}
        <button class="matter-file-detach" onclick="event.stopPropagation(); detachFile(${matterId}, ${f.upload_id})" title="Remove from task">&times;</button>
      </div>`;
    }).join('');
  } catch (e) {
    panel.classList.add('hidden');
  }
}

function toggleFilesPanel() {
  const panel = document.getElementById('matterFilesPanel');
  const btn = panel.querySelector('.matter-files-toggle');
  panel.classList.toggle('collapsed');
  btn.textContent = panel.classList.contains('collapsed') ? '\u25BC' : '\u25B2';
}


async function retryIndex(uploadId) {
  try {
    const result = await api('POST', `/api/files/${uploadId}/retry`);
    if (result.job_id) {
      toast('Retrying indexing...', 'info');
      // Poll until done
      const maxWait = 60000;
      const start = Date.now();
      while (Date.now() - start < maxWait) {
        await new Promise(r => setTimeout(r, 2000));
        const status = await api('GET', `/api/upload/${result.job_id}/status`);
        if (status.done) {
          if (status.status === 'error') toast('Retry failed', 'error');
          else toast('File indexed successfully', 'success');
          break;
        }
      }
    } else {
      toast('File already indexed', 'info');
    }
  } catch (e) {
    toast('Retry failed: ' + e.message, 'error');
  }
  if (state.activeMatterId) loadMatterFiles(state.activeMatterId);
}

async function detachFile(matterId, uploadId) {
  if (!confirm('Remove this file from the task? (The file itself is not deleted.)')) return;
  await api('DELETE', `/api/matters/${matterId}/files/${uploadId}`);
  loadMatterFiles(matterId);
}

function downloadMatterFile(uploadId, filename) {
  downloadWithAuth(`/api/files/${uploadId}/download`, filename);
}

function _addUploadProgress(filename) {
  const panel = document.getElementById('matterFilesPanel');
  const list = document.getElementById('matterFilesList');
  panel.classList.remove('hidden');
  const el = document.createElement('div');
  el.className = 'matter-file-progress';
  el.id = `upload-prog-${filename.replace(/[^a-zA-Z0-9]/g, '_')}`;
  el.innerHTML = `
    <span class="matter-file-name">${escHtml(filename)}</span>
    <div class="progress-bar"><div class="progress-bar-fill"></div></div>
    <span class="upload-status-text">Uploading...</span>
  `;
  list.appendChild(el);
  const count = document.getElementById('matterFileCount');
  count.textContent = list.children.length;
}

function _updateUploadProgress(filename, statusText) {
  const el = document.getElementById(`upload-prog-${filename.replace(/[^a-zA-Z0-9]/g, '_')}`);
  if (el) {
    const txt = el.querySelector('.upload-status-text');
    if (txt) txt.textContent = statusText;
  }
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
    ? `onclick="openPreview(this.dataset.srcPath, this.dataset.srcFile)"`
    : '';

  const icon = hasPath ? ' <span class="source-link-icon">&#128279;</span>' : '';
  const actions = hasPath ? `
    <span class="source-actions">
      <button onclick="event.stopPropagation(); openPreview(this.closest('[data-src-path]').dataset.srcPath, this.closest('[data-src-path]').dataset.srcFile)" title="Open in default app">Open</button>
      <button onclick="event.stopPropagation(); downloadSource(this.closest('[data-src-path]').dataset.srcPath, this.closest('[data-src-path]').dataset.srcFile)" title="Download">&#8595;</button>
    </span>` : '';

  const excerpt = s.excerpt ? `<br><span class="source-excerpt">${escHtml(s.excerpt.substring(0, 150))}…</span>` : '';
  // Build location label: "Page X", "Pages X-Y", "Lines X-Y", or ""
  let locLabel = '';
  if (s.page_start && s.page_start > 0) {
    locLabel = s.page_start === s.page_end ? `p.${s.page_start}` : `pp.${s.page_start}-${s.page_end}`;
  } else if (s.line_start && s.line_start > 0) {
    locLabel = s.line_start === s.line_end ? `ln ${s.line_start}` : `ln ${s.line_start}-${s.line_end}`;
  }
  const locBadge = locLabel ? ` <span style="opacity:0.5;font-size:10px;margin-left:4px;">${locLabel}</span>` : '';
  const score = s.score && s.score < 1.0 ? ` <span style="opacity:0.4;font-size:10px;">${Math.round(s.score * 100)}%</span>` : '';

  return `<div class="${cls}" ${dataAttrs} ${rowHandler}>
    <span class="src-badge">${i + 1}</span><strong>${isWeb ? '&#127760; ' : ''}${escHtml(s.file)}</strong>${locBadge}${score}${icon}${actions}${excerpt}
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

// ── Index Filters ─────────────────────────────────────────────────────────────

async function loadFilterRules() {
  const res = await fetch('/api/admin/filters', { headers: authHeaders() });
  if (!res.ok) return;
  const rules = await res.json();
  const el = document.getElementById('filterRulesList');
  if (!el) return;
  if (!rules.length) {
    el.innerHTML = '<div style="font-size:12px;color:var(--text-muted);font-style:italic;">No filter rules defined — all supported files will be indexed.</div>';
    return;
  }
  el.innerHTML = rules.map(r => {
    const conditions = [];
    if (r.filename_pattern) conditions.push(`filename matches <code>${r.filename_pattern}</code>`);
    if (r.created_before)   conditions.push(`created &gt; ${r.created_before} ago`);
    if (r.created_after)    conditions.push(`created &lt; ${r.created_after} ago`);
    if (r.modified_before)  conditions.push(`not modified in ${r.modified_before}`);
    if (r.modified_after)   conditions.push(`modified within ${r.modified_after}`);
    if (r.size_gt != null)  conditions.push(`size &gt; ${(r.size_gt/1024/1024).toFixed(1)} MB`);
    if (r.size_lt != null)  conditions.push(`size &lt; ${(r.size_lt/1024/1024).toFixed(1)} MB`);
    const badge = r.action === 'exclude'
      ? '<span style="background:#e55;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;text-transform:uppercase;">Exclude</span>'
      : '<span style="background:#3a8;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;text-transform:uppercase;">Include</span>';
    const enabledToggle = r.enabled
      ? `<button class="btn btn-sm" onclick="toggleFilter('${r.id}',false)" style="font-size:10px;padding:1px 6px;">Disable</button>`
      : `<button class="btn btn-primary-sm" onclick="toggleFilter('${r.id}',true)" style="font-size:10px;padding:1px 6px;opacity:0.6;">Enable</button>`;
    return `<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--surface2);border-radius:5px;margin-bottom:6px;${r.enabled ? '' : 'opacity:0.5;'}">
      ${badge}
      <span style="font-size:12px;font-weight:600;flex:1;">${r.name}</span>
      <span style="font-size:11px;color:var(--text-muted);">${conditions.join(' &amp; ') || '(no conditions set)'}</span>
      ${enabledToggle}
      <button class="btn btn-sm" onclick="editFilter(${JSON.stringify(r).replace(/"/g,'&quot;')})" style="font-size:10px;padding:1px 6px;">Edit</button>
      <button class="btn btn-sm" onclick="deleteFilter('${r.id}')" style="font-size:10px;padding:1px 6px;color:#e55;">Delete</button>
    </div>`;
  }).join('');
}

function showFilterForm(rule) {
  document.getElementById('filterForm').style.display = 'block';
  document.getElementById('fError').style.display = 'none';
  document.getElementById('fPreviewResult').textContent = '';
  if (!rule) {
    ['fEditId','fName','fFilename','fCreatedBefore','fCreatedAfter','fModifiedBefore','fModifiedAfter'].forEach(id => {
      document.getElementById(id).value = '';
    });
    document.getElementById('fAction').value = 'exclude';
  }
}

function hideFilterForm() {
  document.getElementById('filterForm').style.display = 'none';
}

function editFilter(rule) {
  showFilterForm(rule);
  document.getElementById('fEditId').value = rule.id || '';
  document.getElementById('fName').value = rule.name || '';
  document.getElementById('fAction').value = rule.action || 'exclude';
  document.getElementById('fFilename').value = rule.filename_pattern || '';
  document.getElementById('fCreatedBefore').value = rule.created_before || '';
  document.getElementById('fCreatedAfter').value = rule.created_after || '';
  document.getElementById('fModifiedBefore').value = rule.modified_before || '';
  document.getElementById('fModifiedAfter').value = rule.modified_after || '';
}

function _buildFilterPayload() {
  const p = {
    name:    document.getElementById('fName').value.trim(),
    action:  document.getElementById('fAction').value,
  };
  const add = (key, id) => { const v = document.getElementById(id).value.trim(); if (v) p[key] = v; };
  add('filename_pattern', 'fFilename');
  add('created_before',   'fCreatedBefore');
  add('created_after',    'fCreatedAfter');
  add('modified_before',  'fModifiedBefore');
  add('modified_after',   'fModifiedAfter');
  return p;
}

async function saveFilterRule() {
  const payload = _buildFilterPayload();
  if (!payload.name) { document.getElementById('fError').textContent = 'Name is required.'; document.getElementById('fError').style.display='block'; return; }
  const editId = document.getElementById('fEditId').value;
  const url    = editId ? `/api/admin/filters/${editId}` : '/api/admin/filters';
  const method = editId ? 'PUT' : 'POST';
  const res = await fetch(url, { method, headers: { ...authHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    document.getElementById('fError').textContent = err.detail || 'Save failed.';
    document.getElementById('fError').style.display = 'block';
    return;
  }
  hideFilterForm();
  loadFilterRules();
  loadCatalogStatus();
  loadTextStatus();
  loadEmbedStatus();
}

async function deleteFilter(id) {
  if (!confirm('Delete this filter rule?')) return;
  await fetch(`/api/admin/filters/${id}`, { method: 'DELETE', headers: authHeaders() });
  loadFilterRules();
  loadCatalogStatus();
  loadTextStatus();
  loadEmbedStatus();
}

async function toggleFilter(id, enabled) {
  await fetch(`/api/admin/filters/${id}`, {
    method: 'PUT',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  loadFilterRules();
  loadCatalogStatus();
  loadTextStatus();
  loadEmbedStatus();
}

async function previewFilterRule() {
  const payload = _buildFilterPayload();
  const btn = document.getElementById('fPreviewBtn');
  const out = document.getElementById('fPreviewResult');
  btn.disabled = true;
  out.textContent = 'Scanning...';
  const res = await fetch('/api/admin/filters/preview', {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ rule: payload, paths: [] }),
  });
  btn.disabled = false;
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    out.textContent = '⚠ ' + (err.detail || 'Preview failed.');
    return;
  }
  const d = await res.json();
  out.textContent = `${d.total_files} files scanned → ${d.would_exclude} would be excluded, ${d.would_keep} kept${d.example_files.length ? ' (e.g. ' + d.example_files.slice(0,3).join(', ') + ')' : ''}`;
}

// Load filters when admin panel opens
const _origLoadAdmin2 = typeof loadAdmin === 'function' ? loadAdmin : null;
document.addEventListener('DOMContentLoaded', () => {
  const adminTab = document.querySelector('[onclick*="showAdmin"], [onclick*="admin"]');
});

// Hook into admin section load
const _filterObserver = new MutationObserver(() => {
  const el = document.getElementById('filterRulesList');
  if (el && el.innerHTML === '') loadFilterRules();
  loadCatalogStatus();
  loadTextStatus();
  loadEmbedStatus();
});
document.addEventListener('DOMContentLoaded', () => {
  const target = document.getElementById('filtersSection');
  if (target) _filterObserver.observe(target, { attributes: true, attributeFilter: ['style'] });
});

// Also load on admin panel display
const _origShowSection = window.showAdminSection;


// ── NAS Folder Browser ──────────────────────────────────────────────────────

async function openNasBrowser(targetInputId) {
  state._nasBrowserTarget = targetInputId;
  state._nasBrowserPath = '';
  await loadNasFolders('');
  document.getElementById('nasBrowserModal').classList.remove('hidden');
}

async function loadNasFolders(path) {
  const modal = document.getElementById('nasBrowserModal');
  const listEl = document.getElementById('nasBrowserList');
  const pathEl = document.getElementById('nasBrowserPath');
  const countEl = document.getElementById('nasBrowserFileCount');
  listEl.innerHTML = '<div style="padding:12px;color:var(--text-muted);">Loading...</div>';

  try {
    const params = path ? `?path=${encodeURIComponent(path)}` : '';
    const data = await api('GET', `/api/nas/browse${params}`);
    state._nasBrowserPath = data.path || '';
    pathEl.textContent = data.path || 'NAS Root';
    countEl.textContent = data.file_count != null ? `${data.file_count} files in this folder` : '';

    let html = '';
    // Back button
    if (data.parent) {
      html += `<div class="nas-folder-item nas-back" onclick="loadNasFolders('${data.parent.replace(/'/g, "\\'")}')">&#8592; Back</div>`;
    }
    if (data.folders.length === 0) {
      html += '<div style="padding:12px;color:var(--text-muted);">No subfolders</div>';
    }
    for (const f of data.folders) {
      const escapedPath = f.path.replace(/'/g, "\\'");
      html += `<div class="nas-folder-item" onclick="loadNasFolders('${escapedPath}')">
        <span class="nas-folder-icon">&#128193;</span> ${escHtml(f.name)}
      </div>`;
    }
    listEl.innerHTML = html;
  } catch (e) {
    listEl.innerHTML = `<div style="padding:12px;color:var(--error);">Error: ${escHtml(e.message)}</div>`;
  }
}

function selectNasFolder() {
  const path = state._nasBrowserPath;
  if (path && state._nasBrowserTarget) {
    document.getElementById(state._nasBrowserTarget).value = path;
  }
  closeModal('nasBrowserModal');
}



// ── NAS Catalog ───────────────────────────────────────────────────────────────

let _catalogPoller = null;

async function loadCatalogStatus() {
  try {
    const s = await api('GET', '/api/catalog/status');
    const stats = await api('GET', '/api/catalog/stats');
    const el = document.getElementById('catalogStatus');
    if (!el) return;

    const totalFiles = stats.total_files ? stats.total_files.toLocaleString() : '0';
    const totalSize = stats.total_size_bytes ? formatBytes(stats.total_size_bytes) : '0 B';
    const clients = stats.unique_clients || 0;

    let statusHtml = `
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-value">${totalFiles}</div><div class="stat-label">Files Cataloged</div></div>
        <div class="stat-card"><div class="stat-value">${totalSize}</div><div class="stat-label">Total Size</div></div>
        <div class="stat-card"><div class="stat-value">${clients}</div><div class="stat-label">Client Folders</div></div>
      </div>
    `;

    if (s.active) {
      const found = (s.total_found || 0).toLocaleString();
      const inserted = (s.total_inserted || 0).toLocaleString();
      const skipped = (s.total_skipped || 0).toLocaleString();
      const elapsed = s.elapsed_s ? Math.round(s.elapsed_s) + 's' : '';
      statusHtml += `
        <div class="catalog-scan-progress">
          <span class="scan-badge active">&#9679; Scanning</span>
          <span class="muted" style="font-size:12px;">
            Stage: ${s.stage} &bull; Found: ${found} &bull; New: ${inserted} &bull; Skipped: ${skipped} ${elapsed ? '&bull; ' + elapsed : ''}
          </span>
        </div>
      `;
      // Poll while active
      if (!_catalogPoller) {
        _catalogPoller = setInterval(loadCatalogStatus, 5000);
      }
    } else {
      if (_catalogPoller) { clearInterval(_catalogPoller); _catalogPoller = null; }
    }

    el.innerHTML = statusHtml;

    // Populate client dropdown (only once)
    if (stats.by_category && !_catalogClientsLoaded) {
      loadCatalogClients();
    }
  } catch (e) {
    const el = document.getElementById('catalogStatus');
    if (el) el.innerHTML = '<span class="muted">Catalog not available</span>';
  }
}

var _catalogClientsLoaded = false;
var _catalogClientsList = [];
var _catalogClientValue = '';

async function loadCatalogClients() {
  try {
    const resp = await api('GET', '/api/catalog/clients?limit=500');
    if (!resp.clients) return;
    _catalogClientsList = resp.clients;
    _catalogClientsLoaded = true;
    // Set initial display
    const inp = document.getElementById('catalogClientInput');
    if (inp && !inp.value && !_catalogClientValue) {
      inp.placeholder = 'All Clients (' + _catalogClientsList.length + ')';
    }
  } catch (e) { /* silent */ }
}

function openCatalogClientDD() {
  const dd = document.getElementById('catalogClientDD');
  if (!dd) return;
  dd.classList.remove('hidden');
  filterCatalogClientDD();
}

function filterCatalogClientDD() {
  const inp = document.getElementById('catalogClientInput');
  const dd = document.getElementById('catalogClientDD');
  if (!dd) return;
  const q = (inp ? inp.value : '').toLowerCase();
  const filtered = q
    ? _catalogClientsList.filter(c => c.client_folder.toLowerCase().indexOf(q) !== -1)
    : _catalogClientsList;
  if (!filtered.length) {
    dd.innerHTML = '<div class="case-dd-empty">No matches</div>';
    return;
  }
  dd.innerHTML = '<div class="case-dd-item" onclick="selectCatalogClient(\'\')">' +
    '<strong>All Clients</strong> <span class="muted">(' + _catalogClientsList.length + ')</span></div>' +
    filtered.slice(0, 100).map(c =>
      '<div class="case-dd-item' + (c.client_folder === _catalogClientValue ? ' active' : '') +
      '" onclick="selectCatalogClient(\'' + escHtml(c.client_folder).replace(/'/g, "\\'") + '\')">' +
      escHtml(c.client_folder) + ' <span class="muted">(' + c.file_count + ')</span></div>'
    ).join('');
}

function selectCatalogClient(val) {
  _catalogClientValue = val;
  const inp = document.getElementById('catalogClientInput');
  if (inp) {
    inp.value = val || '';
    inp.placeholder = val ? val : 'All Clients (' + _catalogClientsList.length + ')';
  }
  document.getElementById('catalogClientDD').classList.add('hidden');
  clearCatalogResults();
}

function closeCatalogClientDD(e) {
  setTimeout(function() {
    var dd = document.getElementById('catalogClientDD');
    if (dd) dd.classList.add('hidden');
  }, 200);
}

function clearCatalogResults() {
  var el = document.getElementById('catalogResults');
  if (el) { el.innerHTML = ''; el.style.display = 'none'; }
}

function clearTextResults() {
  var el = document.getElementById('textResults');
  if (el) { el.innerHTML = ''; el.style.display = 'none'; }
  var inp = document.getElementById('textSearchInput');
  if (inp) inp.value = '';
}

async function triggerCatalogScan(full) {
  const btn = full ? document.getElementById('catalogFullScanBtn')
                   : document.getElementById('catalogScanBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }
  try {
    await api('POST', `/api/catalog/scan?incremental=${!full}`);
    toast(full ? 'Full catalog rescan started' : 'Incremental catalog scan started');
    // Start polling
    if (!_catalogPoller) {
      _catalogPoller = setInterval(loadCatalogStatus, 5000);
    }
    setTimeout(loadCatalogStatus, 1000);
  } catch (e) {
    toast('Catalog scan failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = full ? '\ud83d\uddd1 Full Rescan' : '\u21bb Incremental Scan'; }
  }
}

async function searchCatalog() {
  const q = document.getElementById('catalogSearchInput').value.trim();
  const client = _catalogClientValue;
  const category = document.getElementById('catalogCategoryFilter').value;
  const el = document.getElementById('catalogResults');
  if (!el) return;

  if (!q && !client && !category) {
    clearCatalogResults();
    return;
  }

  el.style.display = 'block';
  el.innerHTML = '<span class="muted">Searching...</span>';

  try {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (client) params.set('client', client);
    if (category) params.set('category', category);
    params.set('limit', '50');

    const data = await api('GET', `/api/catalog/search?${params}`);
    if (!data.results || data.results.length === 0) {
      el.innerHTML = '<span class="muted">No files found</span>';
      return;
    }

    el.innerHTML = `
      <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;">${data.total} results${data.total > 50 ? ' (showing first 50)' : ''}</div>
      <table class="catalog-table">
        <thead>
          <tr><th>Filename</th><th>Client</th><th>Type</th><th>Size</th><th>Modified</th></tr>
        </thead>
        <tbody>
          ${data.results.map(r => `
            <tr title="${escHtml(r.file_path)}">
              <td class="catalog-filename">${escHtml(r.filename)}</td>
              <td class="muted">${escHtml(r.client_folder || '—')}</td>
              <td><span class="tag">${escHtml(r.extension || '?')}</span></td>
              <td class="muted">${formatBytes(r.size_bytes)}</td>
              <td class="muted">${r.mtime_date || '—'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  } catch (e) {
    el.innerHTML = `<span class="error">Search failed: ${escHtml(e.message)}</span>`;
  }
}

// formatBytes defined above


// Catalog search on Enter key
document.addEventListener('DOMContentLoaded', () => {
  const inp = document.getElementById('catalogSearchInput');
  if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') searchCatalog(); });
});


// ── Full-Text Search (Tier 2) ────────────────────────────────────────────────

let _textPoller = null;

async function loadTextStatus() {
  try {
    const s = await api('GET', '/api/text/status');
    const stats = await api('GET', '/api/text/stats');
    const el = document.getElementById('textStatus');
    if (!el) return;

    const total = stats.total_files ? stats.total_files.toLocaleString() : '0';
    const ok = (stats.by_status && stats.by_status.ok) || 0;
    const charsMB = stats.total_chars_mb || 0;

    let statusHtml = `<div class="stat-grid">
      <div class="stat-card"><div class="stat-value">${total}</div><div class="stat-label">Files Processed</div></div>
      <div class="stat-card"><div class="stat-value">${ok.toLocaleString()}</div><div class="stat-label">Text Extracted</div></div>
      <div class="stat-card"><div class="stat-value">${charsMB} MB</div><div class="stat-label">Text Content</div></div>
    </div>`;

    if (s.active) {
      const pct = s.total_queued > 0 ? Math.round((s.processed / s.total_queued) * 100) : 0;
      statusHtml += `<div class="catalog-scan-progress">
        <span class="scan-badge active">&#9679; Extracting</span>
        <span class="muted" style="font-size:12px;">
          ${pct}% (${s.processed.toLocaleString()}/${s.total_queued.toLocaleString()})
          &bull; OK: ${s.extracted_ok} &bull; Empty: ${s.extracted_empty} &bull; Errors: ${s.errors}
          ${s.current_file ? '&bull; ' + s.current_file : ''}
        </span>
      </div>`;
      if (!_textPoller) _textPoller = setInterval(loadTextStatus, 5000);
    } else {
      if (_textPoller) { clearInterval(_textPoller); _textPoller = null; }
    }

    el.innerHTML = statusHtml;
  } catch (e) {
    const el = document.getElementById('textStatus');
    if (el) el.innerHTML = '<span class="muted">Text extraction not available</span>';
  }
}

async function triggerTextExtract() {
  const btn = document.getElementById('textExtractBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }
  try {
    await api('POST', '/api/text/extract');
    toast('Text extraction started');
    if (!_textPoller) _textPoller = setInterval(loadTextStatus, 5000);
    setTimeout(loadTextStatus, 1000);
  } catch (e) {
    toast('Text extraction failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '\u21bb Extract Text'; }
  }
}

async function searchFullText() {
  const q = document.getElementById('textSearchInput').value.trim();
  const el = document.getElementById('textResults');
  if (!el || !q) { if (el) el.style.display = 'none'; return; }

  el.style.display = 'block';
  el.innerHTML = '<span class="muted">Searching...</span>';

  try {
    const data = await api('GET', '/api/text/search?q=' + encodeURIComponent(q) + '&limit=30');
    if (!data.results || data.results.length === 0) {
      el.innerHTML = '<span class="muted">No results found</span>';
      return;
    }

    el.innerHTML = `
      <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;">${data.total} results${data.total > 30 ? ' (showing first 30)' : ''}</div>
      <div class="text-results-list">
        ${data.results.map(r => `
          <div class="text-result-item">
            <div class="text-result-filename">${escHtml(r.filename || r.file_path)}
              <span class="tag">${escHtml(r.extension || '')}</span>
              ${r.client_folder ? '<span class="muted">' + escHtml(r.client_folder) + '</span>' : ''}
            </div>
            <div class="text-result-snippet">${r.snippet || ''}</div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    el.innerHTML = '<span class="error">Search failed: ' + escHtml(e.message) + '</span>';
  }
}


document.addEventListener('DOMContentLoaded', () => {
  const inp = document.getElementById('textSearchInput');
  if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') searchFullText(); });
});


// ── Smart Embedding (Tier 3) ─────────────────────────────────────────────────

let _embedPoller = null;

async function loadEmbedStatus() {
  try {
    const s = await api('GET', '/api/embed/status');
    const stats = await api('GET', '/api/embed/stats');
    const el = document.getElementById('embedStatus');
    if (!el) return;

    const embeddable = stats.embeddable_files ? stats.embeddable_files.toLocaleString() : '0';
    const embedded = stats.already_embedded ? stats.already_embedded.toLocaleString() : '0';

    let statusHtml = `<div class="stat-grid">
      <div class="stat-card"><div class="stat-value">${embeddable}</div><div class="stat-label">Embeddable Files</div></div>
      <div class="stat-card"><div class="stat-value">${embedded}</div><div class="stat-label">Already Embedded</div></div>
    </div>`;

    if (s.active) {
      const pct = s.total_queued > 0 ? Math.round((s.processed / s.total_queued) * 100) : 0;
      statusHtml += `<div class="catalog-scan-progress">
        <span class="scan-badge active">&#9679; Embedding</span>
        <span class="muted" style="font-size:12px;">
          ${pct}% (${s.processed}/${s.total_queued})
          &bull; OK: ${s.embedded_ok} &bull; Skip: ${s.skipped} &bull; Err: ${s.errors}
          ${s.current_file ? '&bull; ' + s.current_file : ''}
        </span>
      </div>`;
      if (!_embedPoller) _embedPoller = setInterval(loadEmbedStatus, 5000);
    } else {
      if (_embedPoller) { clearInterval(_embedPoller); _embedPoller = null; }
    }

    el.innerHTML = statusHtml;
  } catch (e) {
    const el = document.getElementById('embedStatus');
    if (el) el.innerHTML = '<span class="muted">Embedding not available</span>';
  }
}

async function triggerEmbed(limit) {
  const btn = document.getElementById('embedBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }
  try {
    await api('POST', `/api/embed/start?limit=${limit}`);
    toast(`Embedding started (up to ${limit} files)`);
    if (!_embedPoller) _embedPoller = setInterval(loadEmbedStatus, 5000);
    setTimeout(loadEmbedStatus, 2000);
  } catch (e) {
    toast('Embedding failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '\u26a1 Embed Top 200'; }
  }
}

