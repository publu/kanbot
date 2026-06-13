// Deckhand front-end — a Kanban control room for CLI agents.
// Vanilla ES module, no build step. Talks to the FastAPI server over REST + WS.

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

// ---- API ----------------------------------------------------------------
const api = {
  async get(path) { const r = await fetch(path); if (!r.ok) throw new Error(await r.text()); return r.json(); },
  async post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null });
    if (!r.ok) throw new Error(await r.text()); return r.json();
  },
  async patch(path, body) {
    const r = await fetch(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await r.text()); return r.json();
  },
  async del(path) { const r = await fetch(path, { method: 'DELETE' }); if (!r.ok) throw new Error(await r.text()); return r.json(); },
};

// ---- state --------------------------------------------------------------
const S = {
  boards: [],
  boardId: null,
  board: null, columns: [], cards: [], tags: [],
  agents: [], agentByName: {}, insightProviders: [],
  runners: [],
  openCardId: null,
  terminals: {},        // session_id -> terminal DOM node (while drawer open)
  sessionsCache: {},    // card_id -> [sessions]
  agentSessions: [],    // discovered claude/codex sessions across runners
  sessionsModalOpen: false,
};

const COLOR_BY_KIND = { info: 'info', backlog: 'backlog', queued: 'queued', running: 'running', review: 'review', done: 'done', custom: 'custom' };

// ---- boot ---------------------------------------------------------------
async function boot() {
  try {
    const health = await api.get('/api/health');
    $('#version').textContent = 'v' + health.version;
  } catch (e) { /* ignore */ }

  const ag = await api.get('/api/agents');
  S.agents = ag.agents; S.insightProviders = ag.insights;
  S.agentByName = Object.fromEntries(ag.agents.map(a => [a.name, a]));

  await refreshRunners();
  await loadBoards();
  await loadAgentSessions();
  connectWS();
  wireGlobalUI();
}

async function loadAgentSessions() {
  try {
    const { sessions } = await api.get('/api/agent-sessions');
    S.agentSessions = sessions;
  } catch (e) { S.agentSessions = []; }
  updateLiveBadge();
  renderColumns();
  if (S.sessionsModalOpen) openSessionsModal();
}

function updateLiveBadge() {
  const active = S.agentSessions.filter(s => s.active).length;
  const badge = $('#liveBadge');
  if (active > 0) { badge.textContent = active; badge.classList.remove('hidden'); }
  else badge.classList.add('hidden');
}

async function loadBoards() {
  const { boards } = await api.get('/api/boards');
  S.boards = boards;
  if (S.boards.length === 0) {
    const created = await api.post('/api/boards', { name: 'My Board' });
    S.boards = [created.board];
  }
  const sel = $('#boardSelect');
  sel.innerHTML = '';
  for (const b of S.boards) { const o = el('option', null, b.name); o.value = b.id; sel.appendChild(o); }
  const target = S.boardId && S.boards.find(b => b.id === S.boardId) ? S.boardId : S.boards[0].id;
  sel.value = target;
  await selectBoard(target);
}

async function selectBoard(boardId) {
  S.boardId = boardId;
  const state = await api.get('/api/boards/' + boardId);
  S.board = state.board; S.columns = state.columns; S.cards = state.cards; S.tags = state.tags;
  renderBoard();
}

async function refreshRunners() {
  try { const { runners } = await api.get('/api/runners'); S.runners = runners; renderRunners(); }
  catch (e) {}
}

// ---- websocket ----------------------------------------------------------
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/web`);
  ws.onmessage = (ev) => { try { handleEvent(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => setTimeout(connectWS, 1500);
}

function handleEvent(msg) {
  switch (msg.type) {
    case 'card.created':
      if (msg.card.board_id === S.boardId) { S.cards.push(msg.card); renderColumns(); }
      break;
    case 'card.updated': {
      if (msg.card.board_id !== S.boardId) break;
      const i = S.cards.findIndex(c => c.id === msg.card.id);
      if (i >= 0) S.cards[i] = msg.card; else S.cards.push(msg.card);
      renderColumns();
      if (S.openCardId === msg.card.id) refreshDrawerHeader(msg.card);
      break;
    }
    case 'card.deleted':
      S.cards = S.cards.filter(c => c.id !== msg.card_id);
      renderColumns();
      if (S.openCardId === msg.card_id) closeDrawer();
      break;
    case 'session.created':
    case 'session.updated':
      if (S.openCardId && msg.session && msg.session.card_id === S.openCardId) reloadSessions();
      break;
    case 'session.event':
      appendTerminal(msg.session_id, msg.event);
      break;
    case 'runner.updated':
      refreshRunners();
      break;
    case 'agent.sessions.updated':
      loadAgentSessions();
      break;
    case 'board.created':
      loadBoards();
      break;
    case 'board.deleted':
      if (msg.board_id === S.boardId) { S.boardId = null; }
      loadBoards();
      break;
  }
}

// ---- render: runners ----------------------------------------------------
function renderRunners() {
  const wrap = $('#runners');
  wrap.innerHTML = '';
  const online = S.runners.filter(r => r.status !== 'offline');
  if (online.length === 0) {
    const pill = el('div', 'runner-pill empty');
    pill.appendChild(el('span', 'dot'));
    pill.appendChild(el('span', null, 'no runners'));
    pill.title = 'Run `deckhand runner` (or `deckhand up`) to attach a worker.';
    wrap.appendChild(pill);
    return;
  }
  for (const r of online) {
    const pill = el('div', 'runner-pill ' + r.status);
    pill.appendChild(el('span', 'dot'));
    pill.appendChild(el('span', null, r.name));
    const caps = el('span', 'caps', (r.capabilities || []).join('·') || '—');
    pill.appendChild(caps);
    pill.title = `${r.name} @ ${r.host} — ${r.active}/${r.max_concurrency} busy\nagents: ${(r.capabilities||[]).join(', ')}`;
    wrap.appendChild(pill);
  }
}

// ---- render: board ------------------------------------------------------
function renderBoard() {
  renderColumns();
}

function renderColumns() {
  const board = $('#board');
  board.innerHTML = '';
  if (!S.board) return;

  // Discovered agent sessions are injected inline by recency: working now ->
  // Running, finished in the last 30 min -> Done, anything older -> Backlog
  // (stale sessions ARE the backlog). No dedicated column.
  const RECENT_DONE_S = 30 * 60;
  const nowS = Date.now() / 1000;
  const byBucket = { backlog: [], running: [], done: [] };
  for (const s of S.agentSessions) {
    if (s.active) byBucket.running.push(s);
    else if ((nowS - (s.mtime || 0)) <= RECENT_DONE_S) byBucket.done.push(s);
    else byBucket.backlog.push(s);
  }
  const CAP = { backlog: 25, done: 15, running: 50 };

  for (const col of S.columns) {
    const column = el('div', 'column');
    column.dataset.colId = col.id;
    column.dataset.kind = col.kind;

    const cards = S.cards.filter(c => c.column_id === col.id).sort((a, b) => a.position - b.position);
    const injected = byBucket[col.kind] || [];
    const cap = CAP[col.kind] || 0;
    const shownSessions = injected.slice(0, cap);

    const head = el('div', 'col-head');
    head.appendChild(el('span', 'kind-dot kind-' + (COLOR_BY_KIND[col.kind] || 'custom')));
    head.appendChild(el('span', 'title', col.name));
    head.appendChild(el('span', 'count', cards.length + injected.length));
    column.appendChild(head);

    const body = el('div', 'col-body');
    if (cards.length + injected.length === 0) body.classList.add('empty-hint');

    if (col.kind === 'backlog') {
      const add = el('div', 'add-card', '+ add task');
      add.onclick = () => openComposer(col.id);
      body.appendChild(add);
    }
    // working sessions sit on top of Running; elsewhere real cards come first
    if (col.kind === 'running') {
      for (const s of shownSessions) body.appendChild(renderSessionCard(s));
      for (const c of cards) body.appendChild(renderCard(c));
    } else {
      for (const c of cards) body.appendChild(renderCard(c));
      for (const s of shownSessions) body.appendChild(renderSessionCard(s));
    }
    if (injected.length > cap && cap > 0) {
      const more = el('div', 'add-card', `+${injected.length - cap} more — ⟳ sessions`);
      more.onclick = openSessionsModal;
      body.appendChild(more);
    }

    // drag & drop
    body.addEventListener('dragover', (e) => { e.preventDefault(); column.classList.add('drag-over'); });
    body.addEventListener('dragleave', (e) => { if (!column.contains(e.relatedTarget)) column.classList.remove('drag-over'); });
    body.addEventListener('drop', (e) => onDrop(e, col, body));

    column.appendChild(body);
    board.appendChild(column);
  }
}

function sessionPreview(s) {
  return s.last_text || s.last_user || s.title || '';
}

function renderSessionCard(s) {
  const card = el('div', 'card sess' + (s.active ? ' s-running' : ''));
  card.style.cursor = 'pointer';
  const top = el('div', 'sess-top');
  top.appendChild(agentBadge(s.agent));
  top.appendChild(el('span', 'sess-name', s.name || 'session'));
  if (s.active) {
    const w = el('span', 'sess-work');
    w.appendChild(el('span', 'spinner'));
    w.appendChild(el('span', null, 'working'));
    top.appendChild(w);
  } else {
    top.appendChild(el('span', 'sess-age', timeAgo(s.mtime)));
  }
  card.appendChild(top);
  const preview = sessionPreview(s);
  if (preview) {
    const pv = el('div', 'sess-preview');
    if (s.last_text || s.last_user) pv.appendChild(el('span', 'sess-pv-tag', s.last_text ? '↩ ' : '› '));
    pv.appendChild(document.createTextNode(preview));
    card.appendChild(pv);
  }
  const foot = el('div', 'sess-foot');
  foot.appendChild(el('span', 'sess-turns', `${s.turns} turns`));
  const rev = el('button', 'sess-revive', s.active ? '⟳ continue' : '⟳ revive');
  rev.onclick = (e) => { e.stopPropagation(); promptRevive(s); };
  foot.appendChild(rev);
  card.appendChild(foot);
  card.onclick = () => promptRevive(s);
  return card;
}

function renderCard(c) {
  const card = el('div', 'card s-' + c.status);
  card.draggable = true;
  card.dataset.cardId = c.id;

  card.appendChild(el('div', 'ctitle', c.title));

  const meta = el('div', 'cmeta');
  meta.appendChild(agentBadge(c.agent));
  if (c.resume_of) meta.appendChild(el('span', 'resume-badge', '⟳ resumed'));
  card.appendChild(meta);

  if (c.tags && c.tags.length) {
    const tags = el('div', 'tags');
    for (const t of c.tags) tags.appendChild(tagChip(t));
    card.appendChild(tags);
  }

  if (c.status === 'running') {
    const sl = el('div', 'status-line');
    sl.appendChild(el('span', 'spinner'));
    sl.appendChild(el('span', null, 'running…'));
    card.appendChild(sl);
  } else if (['queued', 'failed', 'done', 'review', 'cancelled'].includes(c.status)) {
    card.appendChild(el('div', 'status-line', c.status));
  }

  card.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', c.id);
    card.classList.add('dragging');
  });
  card.addEventListener('dragend', () => card.classList.remove('dragging'));
  card.onclick = () => openDrawer(c.id);
  return card;
}

function agentBadge(name) {
  const a = S.agentByName[name];
  const badge = el('span', 'agent-badge');
  const dot = el('span', 'adot');
  dot.style.background = a ? a.color : '#888';
  badge.appendChild(dot);
  badge.appendChild(el('span', null, name === 'auto' ? 'auto' : (a ? a.label : name)));
  return badge;
}

function tagChip(t) {
  const chip = el('span', 'tag-chip');
  chip.style.background = hexA(t.color, .16);
  chip.style.color = t.color;
  if (t.insight) chip.appendChild(el('span', 'insight-mark', '◆'));
  chip.appendChild(el('span', null, t.name));
  return chip;
}

// ---- drag/drop ----------------------------------------------------------
async function onDrop(e, col, body) {
  e.preventDefault();
  document.querySelectorAll('.column').forEach(c => c.classList.remove('drag-over'));
  const cardId = e.dataTransfer.getData('text/plain');
  if (!cardId) return;
  // insertion index based on drop Y position
  const cardsEls = [...body.querySelectorAll('.card')].filter(n => n.dataset.cardId !== cardId);
  let index = cardsEls.length;
  for (let i = 0; i < cardsEls.length; i++) {
    const r = cardsEls[i].getBoundingClientRect();
    if (e.clientY < r.top + r.height / 2) { index = i; break; }
  }
  try {
    await api.post(`/api/cards/${cardId}/move`, { column_id: col.id, position: index });
  } catch (err) { toast('move failed: ' + err.message); }
}

// ---- composer (quick add) ----------------------------------------------
function openComposer(columnId) {
  const m = $('#modal');
  m.innerHTML = '';
  m.appendChild(el('h3', null, 'New task'));

  const title = inputField('Title', 'e.g. Add unit tests for parser');
  const prompt = textareaField('Prompt for the agent', 'Describe the work. This is sent verbatim to the chosen CLI agent.');
  const agentSel = agentSelectField(S.board?.default_agent || 'auto');
  const cwd = inputField('Working directory', S.board?.repo_path || '/path/to/repo');
  cwd.input.value = S.board?.repo_path || '';

  m.appendChild(title.wrap);
  m.appendChild(prompt.wrap);
  const row = el('div', 'row');
  row.appendChild(agentSel.wrap);
  m.appendChild(row);
  m.appendChild(cwd.wrap);

  const actions = el('div', 'modal-actions');
  const cancel = el('button', 'btn ghost', 'Cancel');
  cancel.onclick = closeModal;
  const addBtn = el('button', 'btn', 'Add to backlog');
  const queueBtn = el('button', 'btn primary', '⏵ Queue now');
  const submit = async (queue) => {
    if (!title.input.value.trim()) { toast('title required'); return; }
    const card = await api.post(`/api/boards/${S.boardId}/cards`, {
      title: title.input.value.trim(),
      prompt: prompt.input.value,
      agent: agentSel.select.value,
      cwd: cwd.input.value,
      column_id: columnId,
    });
    closeModal();
    if (queue) { try { await api.post(`/api/cards/${card.id}/run`); } catch (e) { toast(e.message); } }
  };
  addBtn.onclick = () => submit(false);
  queueBtn.onclick = () => submit(true);
  actions.appendChild(cancel); actions.appendChild(addBtn); actions.appendChild(queueBtn);
  m.appendChild(actions);
  openModal();
  setTimeout(() => title.input.focus(), 50);
}

// ---- drawer (card detail) ----------------------------------------------
async function openDrawer(cardId) {
  S.openCardId = cardId;
  S.terminals = {};
  const card = S.cards.find(c => c.id === cardId);
  if (!card) return;
  $('#dTitle').value = card.title;
  $('#drawer').classList.add('open');
  $('#drawerScrim').classList.add('open');
  renderDrawerBody(card);
  reloadSessions();
}

function refreshDrawerHeader(card) {
  if (document.activeElement !== $('#dTitle')) $('#dTitle').value = card.title;
  renderDrawerBody(card);
}

function closeDrawer() {
  S.openCardId = null;
  S.terminals = {};
  $('#drawer').classList.remove('open');
  $('#drawerScrim').classList.remove('open');
}

function renderDrawerBody(card) {
  const body = $('#drawerBody');
  body.innerHTML = '';

  // status + actions
  const actions = el('div', 'drawer-actions');
  const runBtn = el('button', 'btn primary', '⏵ Queue & run');
  runBtn.onclick = async () => { try { await api.post(`/api/cards/${card.id}/run`); toast('queued'); } catch (e) { toast(e.message); } };
  actions.appendChild(runBtn);
  if (card.status === 'running' || card.status === 'queued') {
    const cancelBtn = el('button', 'btn', '◼ Cancel run');
    cancelBtn.onclick = () => cancelActiveSession(card.id);
    actions.appendChild(cancelBtn);
  }
  const del = el('button', 'btn ghost danger', '🗑 Delete');
  del.onclick = async () => { if (confirm('Delete this task?')) await api.del(`/api/cards/${card.id}`); };
  actions.appendChild(del);
  body.appendChild(actions);

  // prompt
  const prompt = textareaField('Prompt', 'Instructions sent to the agent');
  prompt.input.value = card.prompt || '';
  prompt.input.onblur = () => patchCard(card.id, { prompt: prompt.input.value });
  body.appendChild(prompt.wrap);

  // agent + cwd
  const row = el('div', 'row');
  const agentSel = agentSelectField(card.agent);
  agentSel.select.onchange = () => patchCard(card.id, { agent: agentSel.select.value });
  const cwd = inputField('Working directory', '/path/to/repo');
  cwd.input.value = card.cwd || '';
  cwd.input.onblur = () => patchCard(card.id, { cwd: cwd.input.value });
  row.appendChild(agentSel.wrap); row.appendChild(cwd.wrap);
  body.appendChild(row);

  // auto-advance toggle
  const tg = el('label', 'toggle');
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = !!card.auto_advance;
  cb.onchange = () => patchCard(card.id, { auto_advance: cb.checked });
  tg.appendChild(cb); tg.appendChild(el('span', 'track'));
  tg.appendChild(el('span', null, 'Auto-advance to Review on success'));
  body.appendChild(tg);

  // tags
  body.appendChild(renderTagsField(card));

  // insights
  const insightSection = el('div', 'field');
  insightSection.id = 'insightSection';
  const ih = el('div', 'label');
  ih.appendChild(el('span', null, 'Insights'));
  const refresh = el('button', 'btn ghost small', '↻ refresh');
  refresh.style.marginLeft = 'auto';
  refresh.onclick = () => loadInsights(card.id);
  ih.appendChild(refresh);
  insightSection.appendChild(ih);
  const ibox = el('div'); ibox.id = 'insightBox';
  insightSection.appendChild(ibox);
  body.appendChild(insightSection);
  if ((card.tags || []).some(t => t.insight)) loadInsights(card.id);
  else ibox.appendChild(el('div', 'label', 'add an insight tag (◆) to pull live context here'));

  // sessions
  const sessSection = el('div', 'field');
  sessSection.appendChild(el('div', 'label', 'Sessions'));
  const sbox = el('div'); sbox.id = 'sessionsBox'; sbox.style.display = 'flex';
  sbox.style.flexDirection = 'column'; sbox.style.gap = '10px';
  sessSection.appendChild(sbox);
  body.appendChild(sessSection);
}

async function patchCard(id, fields) {
  try { await api.patch('/api/cards/' + id, fields); }
  catch (e) { toast('save failed: ' + e.message); }
}

// ---- tags in drawer -----------------------------------------------------
function renderTagsField(card) {
  const wrap = el('div', 'field');
  wrap.appendChild(el('div', 'label', 'Tags'));
  const list = el('div', 'tag-list');
  const cardTagIds = new Set((card.tags || []).map(t => t.id));

  for (const t of card.tags || []) {
    const chip = el('span', 'tag-pickable');
    chip.style.background = hexA(t.color, .16);
    chip.style.color = t.color;
    chip.style.borderColor = hexA(t.color, .4);
    if (t.insight) chip.appendChild(el('span', null, '◆'));
    chip.appendChild(el('span', null, t.name));
    const rm = el('span', 'rm', '×');
    rm.onclick = async (e) => { e.stopPropagation(); await api.del(`/api/cards/${card.id}/tags/${t.id}`); };
    chip.appendChild(rm);
    list.appendChild(chip);
  }
  wrap.appendChild(list);

  // available tags to add
  const avail = S.tags.filter(t => !cardTagIds.has(t.id));
  const addRow = el('div', 'tag-add');
  if (avail.length) {
    for (const t of avail) {
      const chip = el('span', 'tag-pickable');
      chip.style.color = t.color;
      if (t.insight) chip.appendChild(el('span', null, '◆'));
      chip.appendChild(el('span', null, '+ ' + t.name));
      chip.onclick = async () => { await api.post(`/api/cards/${card.id}/tags`, { tag_id: t.id }); };
      addRow.appendChild(chip);
    }
  }
  const newTag = el('span', 'tag-pickable', '+ new tag');
  newTag.style.color = 'var(--text-faint)';
  newTag.onclick = () => openTagModal(card.id);
  addRow.appendChild(newTag);
  wrap.appendChild(addRow);
  return wrap;
}

// ---- insights -----------------------------------------------------------
async function loadInsights(cardId) {
  const box = $('#insightBox');
  if (!box) return;
  box.innerHTML = '';
  box.appendChild(el('div', 'label', 'loading…'));
  try {
    const { insights } = await api.get(`/api/cards/${cardId}/insights`);
    box.innerHTML = '';
    if (!insights.length) { box.appendChild(el('div', 'label', 'no insight tags on this card')); return; }
    for (const ins of insights) {
      const c = el('div', 'insight-card' + (ins.ok ? '' : ' fail'));
      const head = el('div', 'ihead');
      const dot = el('span', 'adot'); dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:' + (ins.tag?.color || '#888');
      head.appendChild(dot);
      head.appendChild(el('strong', null, ins.title));
      head.appendChild(el('span', 'isum', ins.summary || ''));
      c.appendChild(head);
      if (ins.detail) { const d = el('div', 'idetail', ins.detail); c.appendChild(d); }
      box.appendChild(c);
    }
  } catch (e) { box.innerHTML = ''; box.appendChild(el('div', 'label', 'insight error: ' + e.message)); }
}

// ---- sessions + live terminal ------------------------------------------
async function reloadSessions() {
  if (!S.openCardId) return;
  const cardId = S.openCardId;
  let sessions = [];
  try { const r = await api.get(`/api/sessions?card_id=${cardId}`); sessions = r.sessions; }
  catch (e) { return; }
  const box = $('#sessionsBox');
  if (!box) return;
  box.innerHTML = '';
  S.terminals = {};
  if (!sessions.length) { box.appendChild(el('div', 'label', 'no runs yet — queue this task to start one')); return; }
  for (const s of sessions) box.appendChild(await renderSession(s));
}

async function renderSession(s) {
  const wrap = el('div', 'session');
  const head = el('div', 'session-head');
  head.appendChild(el('span', 'sstatus ss-' + s.status, s.status));
  head.appendChild(agentBadge(s.agent || 'auto'));
  const meta = el('span', 'smeta', timeAgo(s.started_at || s.created_at) + (s.exit_code != null ? ` · exit ${s.exit_code}` : ''));
  head.appendChild(meta);
  wrap.appendChild(head);

  const term = el('div', 'terminal');
  S.terminals[s.id] = term;
  head.onclick = () => { term.style.display = term.style.display === 'none' ? 'block' : 'none'; };
  wrap.appendChild(term);

  // backfill events
  try {
    const { events } = await api.get(`/api/sessions/${s.id}`);
    for (const ev of events) appendTerminal(s.id, ev, true);
  } catch (e) {}

  // collapse finished sessions except the most recent
  return wrap;
}

function appendTerminal(sessionId, ev, noscroll) {
  const term = S.terminals[sessionId];
  if (!term) return;
  const ln = el('span', 'ln ' + (ev.stream || 'stdout'), ev.text);
  term.appendChild(ln);
  if (!noscroll) term.scrollTop = term.scrollHeight;
}

async function cancelActiveSession(cardId) {
  try {
    const { sessions } = await api.get(`/api/sessions?card_id=${cardId}`);
    const active = sessions.find(s => ['pending', 'assigned', 'running'].includes(s.status));
    if (active) { await api.post(`/api/sessions/${active.id}/cancel`); toast('cancelling'); }
    else toast('no active run');
  } catch (e) { toast(e.message); }
}

// ---- modals: new board / new tag ---------------------------------------
function openNewBoardModal() {
  const m = $('#modal'); m.innerHTML = '';
  m.appendChild(el('h3', null, 'New board'));
  const name = inputField('Board name', 'e.g. Backend refactor');
  const repo = inputField('Default repo path (optional)', '/path/to/repo');
  m.appendChild(name.wrap); m.appendChild(repo.wrap);
  const actions = el('div', 'modal-actions');
  const cancel = el('button', 'btn ghost', 'Cancel'); cancel.onclick = closeModal;
  const create = el('button', 'btn primary', 'Create');
  create.onclick = async () => {
    if (!name.input.value.trim()) { toast('name required'); return; }
    const r = await api.post('/api/boards', { name: name.input.value.trim(), repo_path: repo.input.value });
    closeModal();
    await loadBoards();
    $('#boardSelect').value = r.board.id;
    await selectBoard(r.board.id);
  };
  actions.appendChild(cancel); actions.appendChild(create);
  m.appendChild(actions); openModal();
  setTimeout(() => name.input.focus(), 50);
}

const PALETTE = ['#ff9d3d', '#4fd1e0', '#b491f5', '#5fd28a', '#ff6b6b', '#6aa6ff', '#f5c451', '#9aa3b2'];

function openTagModal(attachToCardId) {
  const m = $('#modal'); m.innerHTML = '';
  m.appendChild(el('h3', null, 'New tag'));
  const name = inputField('Tag name', 'e.g. needs-review');
  m.appendChild(name.wrap);

  // color
  const colorField = el('div', 'field');
  colorField.appendChild(el('div', 'label', 'Color'));
  const row = el('div', 'color-row');
  let chosen = PALETTE[0];
  PALETTE.forEach((c, i) => {
    const sw = el('div', 'swatch' + (i === 0 ? ' sel' : '')); sw.style.background = c;
    sw.onclick = () => { chosen = c; [...row.children].forEach(x => x.classList.remove('sel')); sw.classList.add('sel'); };
    row.appendChild(sw);
  });
  colorField.appendChild(row);
  m.appendChild(colorField);

  // insight provider
  const insField = el('div', 'field');
  insField.appendChild(el('div', 'label', 'Insight provider (optional)'));
  const sel = el('select', 'select');
  sel.appendChild(el('option', null, '— plain label —')).value = '';
  for (const p of S.insightProviders) { const o = el('option', null, `◆ ${p.label}`); o.value = p.key; sel.appendChild(o); }
  insField.appendChild(sel);
  const hint = el('div', 'label', ''); insField.appendChild(hint);
  sel.onchange = () => { const p = S.insightProviders.find(x => x.key === sel.value); hint.textContent = p ? p.description : ''; };
  m.appendChild(insField);

  // optional command config for 'command' insight
  const cmd = inputField('Command (for "custom command" insight)', 'e.g. pytest -q || true');
  cmd.wrap.style.display = 'none';
  sel.addEventListener('change', () => { cmd.wrap.style.display = sel.value === 'command' ? 'flex' : 'none'; });
  m.appendChild(cmd.wrap);

  const actions = el('div', 'modal-actions');
  const cancel = el('button', 'btn ghost', 'Cancel'); cancel.onclick = closeModal;
  const create = el('button', 'btn primary', 'Create tag');
  create.onclick = async () => {
    if (!name.input.value.trim()) { toast('name required'); return; }
    const config = sel.value === 'command' ? { command: cmd.input.value } : {};
    const tag = await api.post(`/api/boards/${S.boardId}/tags`, {
      name: name.input.value.trim(), color: chosen, insight: sel.value, config,
    });
    S.tags.push(tag);
    if (attachToCardId) await api.post(`/api/cards/${attachToCardId}/tags`, { tag_id: tag.id });
    closeModal();
  };
  actions.appendChild(cancel); actions.appendChild(create);
  m.appendChild(actions); openModal();
  setTimeout(() => name.input.focus(), 50);
}

function openManageTags() {
  const m = $('#modal'); m.innerHTML = '';
  m.appendChild(el('h3', null, 'Tags on this board'));
  const list = el('div', 'tag-list');
  if (!S.tags.length) list.appendChild(el('div', 'label', 'no tags yet'));
  for (const t of S.tags) {
    const chip = el('span', 'tag-pickable');
    chip.style.color = t.color;
    if (t.insight) chip.appendChild(el('span', null, '◆'));
    chip.appendChild(el('span', null, t.name));
    const rm = el('span', 'rm', '×');
    rm.onclick = async () => { await api.del(`/api/tags/${t.id}`); S.tags = S.tags.filter(x => x.id !== t.id); openManageTags(); };
    chip.appendChild(rm);
    list.appendChild(chip);
  }
  m.appendChild(list);
  const actions = el('div', 'modal-actions');
  const newBtn = el('button', 'btn', '+ new tag'); newBtn.onclick = () => openTagModal(null);
  const close = el('button', 'btn primary', 'Done'); close.onclick = closeModal;
  actions.appendChild(newBtn); actions.appendChild(close);
  m.appendChild(actions); openModal();
}

// ---- sessions browser + revive -----------------------------------------
function openSessionsModal() {
  S.sessionsModalOpen = true;
  const m = $('#modal'); m.innerHTML = '';
  const h = el('h3', null, 'Claude / Codex sessions');
  m.appendChild(h);

  const sessions = S.agentSessions;
  if (!sessions.length) {
    m.appendChild(el('div', 'label',
      'No agent sessions discovered. The runner reports recent Claude (~/.claude) and Codex (~/.codex) sessions; make sure a runner is connected.'));
  } else {
    const browser = el('div', 'sess-browser');
    const active = sessions.filter(s => s.active);
    const recent = sessions.filter(s => !s.active);
    if (active.length) {
      browser.appendChild(el('div', 'sess-section-label', '● working now'));
      for (const s of active) browser.appendChild(sessRow(s));
    }
    if (recent.length) {
      browser.appendChild(el('div', 'sess-section-label', 'recent'));
      for (const s of recent) browser.appendChild(sessRow(s));
    }
    m.appendChild(browser);
  }

  const actions = el('div', 'modal-actions');
  const close = el('button', 'btn primary', 'Done');
  close.onclick = () => { S.sessionsModalOpen = false; closeModal(); };
  actions.appendChild(close);
  m.appendChild(actions);
  openModal();
}

function sessRow(s) {
  const row = el('div', 'sess-row' + (s.active ? ' active' : ''));
  row.appendChild(el('span', s.active ? 'working-dot' : 'idle-dot'));
  const info = el('div', 'sinfo');
  const title = el('div', 'stitle');
  title.appendChild(agentBadge(s.agent));
  title.appendChild(document.createTextNode(' ' + (s.name || 'session')));
  info.appendChild(title);
  const pv = sessionPreview(s);
  const sub = (pv ? pv + ' · ' : '') + `${s.turns} turns · ${timeAgo(s.mtime)} · ${shortCwd(s.cwd)}`;
  info.appendChild(el('div', 'ssub', sub));
  row.appendChild(info);
  const btn = el('button', 'btn small', s.active ? '⟳ continue' : '⟳ revive');
  btn.onclick = () => promptRevive(s);
  row.appendChild(btn);
  return row;
}

function promptRevive(s) {
  const m = $('#modal'); m.innerHTML = '';
  m.appendChild(el('h3', null, (s.active ? 'Continue ' : 'Revive ') + (s.name || s.agent)));
  const sub = el('div', 'ssub', `${s.agent} · ${s.cwd || '?'} · ${s.session_id.slice(0,8)} · ${s.turns} turns · ${timeAgo(s.mtime)}`);
  sub.style.cssText = 'font-family:var(--mono);font-size:11px;color:var(--text-faint);margin-bottom:2px;';
  m.appendChild(sub);

  // terminal "where it left off" preview
  const term = el('div', 'terminal-card');
  const bar = el('div', 'term-bar');
  const dots = el('div', 'term-dots');
  dots.appendChild(el('i')); dots.appendChild(el('i')); dots.appendChild(el('i'));
  bar.appendChild(dots);
  bar.appendChild(el('span', 'term-title', `${s.agent} — ${s.name} · ${s.turns} turns`));
  term.appendChild(bar);
  const body = el('div', 'term-body');
  const userLine = (txt) => {
    const ln = el('span', 'term-line term-user');
    ln.appendChild(el('span', 'term-prompt', '❯'));
    ln.appendChild(document.createTextNode(txt));
    body.appendChild(ln);
  };
  const commentLine = (txt) => body.appendChild(el('span', 'term-line term-comment', txt));
  const outLine = (txt) => body.appendChild(el('span', 'term-line term-out', txt));
  if (s.last_user) { commentLine('# last asked'); userLine(s.last_user); }
  if (s.last_text) { commentLine('# last reply'); outLine(s.last_text); }
  if (!s.last_user && !s.last_text) {
    commentLine('# session started with');
    if (s.title) userLine(s.title); else outLine('(no readable transcript)');
  }
  term.appendChild(body);
  m.appendChild(term);

  const prompt = textareaField('What should the agent do next?',
    'e.g. Now write tests for the change you just made.');
  prompt.input.style.minHeight = '110px';
  m.appendChild(prompt.wrap);
  const actions = el('div', 'modal-actions');
  const back = el('button', 'btn ghost', 'Back');
  back.onclick = openSessionsModal;
  const addBtn = el('button', 'btn', 'Add to backlog');
  const runBtn = el('button', 'btn primary', '⏵ Resume now');
  const submit = async (run) => {
    try {
      await api.post(`/api/boards/${S.boardId}/revive`, {
        runner_id: s.runner_id, agent: s.agent, session_id: s.session_id,
        cwd: s.cwd, title: `↻ ${s.name || s.agent}`.slice(0, 80),
        prompt: prompt.input.value, run,
      });
      S.sessionsModalOpen = false; closeModal();
      toast(run ? 'resuming session…' : 'added to backlog');
    } catch (e) { toast('revive failed: ' + e.message); }
  };
  addBtn.onclick = () => submit(false);
  runBtn.onclick = () => submit(true);
  actions.appendChild(back); actions.appendChild(addBtn); actions.appendChild(runBtn);
  m.appendChild(actions);
  openModal();
  m.classList.add('wide');
  setTimeout(() => prompt.input.focus(), 50);
}

// ---- small field builders ----------------------------------------------
function inputField(label, ph) {
  const wrap = el('div', 'field');
  wrap.appendChild(el('div', 'label', label));
  const input = el('input', 'input'); input.placeholder = ph || '';
  wrap.appendChild(input);
  return { wrap, input };
}
function textareaField(label, ph) {
  const wrap = el('div', 'field');
  wrap.appendChild(el('div', 'label', label));
  const input = el('textarea', 'textarea'); input.placeholder = ph || '';
  wrap.appendChild(input);
  return { wrap, input };
}
function agentSelectField(value) {
  const wrap = el('div', 'field');
  wrap.appendChild(el('div', 'label', 'Agent'));
  const select = el('select', 'select');
  const auto = el('option', null, 'auto (any available)'); auto.value = 'auto'; select.appendChild(auto);
  for (const a of S.agents) { const o = el('option', null, a.label); o.value = a.name; select.appendChild(o); }
  select.value = value || 'auto';
  wrap.appendChild(select);
  return { wrap, select };
}

// ---- modal helpers ------------------------------------------------------
function openModal() { $('#modal').classList.remove('wide'); $('#modalScrim').classList.add('open'); }
function closeModal() { $('#modalScrim').classList.remove('open'); S.sessionsModalOpen = false; }

// ---- global UI ----------------------------------------------------------
function wireGlobalUI() {
  $('#boardSelect').onchange = (e) => selectBoard(e.target.value);
  $('#newBoardBtn').onclick = openNewBoardModal;
  $('#sessionsBtn').onclick = openSessionsModal;
  $('#manageTagsBtn').onclick = openManageTags;
  $('#drawerClose').onclick = closeDrawer;
  $('#drawerScrim').onclick = closeDrawer;
  $('#modalScrim').onclick = (e) => { if (e.target.id === 'modalScrim') closeModal(); };
  $('#dTitle').onblur = () => { if (S.openCardId) patchCard(S.openCardId, { title: $('#dTitle').value }); };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeModal(); closeDrawer(); }
  });
}

// ---- utils --------------------------------------------------------------
function hexA(hex, a) {
  const h = (hex || '#888888').replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map(c => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}
function shortCwd(p) {
  if (!p) return '?';
  const parts = p.split('/').filter(Boolean);
  return parts.length <= 2 ? p : '…/' + parts.slice(-2).join('/');
}
function timeAgo(ts) {
  if (!ts) return 'just now';
  const s = Math.max(1, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function toast(msg) {
  const t = el('div', 'toast', msg);
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

boot();
