// KanBot front-end — a Kanban control room for CLI agents.
// Vanilla ES module, no build step. Talks to the FastAPI server over REST + WS.
// Falls back to a self-contained demo when there's no backend (e.g. on Vercel).

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

// ---- API ----------------------------------------------------------------
const api = {
  async get(path) { if (S.demo) return demoGet(path); const r = await fetch(S.apiBase + path); if (!r.ok) throw new Error(await r.text()); return r.json(); },
  async post(path, body) {
    if (S.demo) return demoMutate();
    const r = await fetch(S.apiBase + path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null });
    if (!r.ok) throw new Error(await r.text()); return r.json();
  },
  async patch(path, body) {
    if (S.demo) return demoMutate();
    const r = await fetch(S.apiBase + path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await r.text()); return r.json();
  },
  async del(path) { if (S.demo) return demoMutate(); const r = await fetch(S.apiBase + path, { method: 'DELETE' }); if (!r.ok) throw new Error(await r.text()); return r.json(); },
};

function demoMutate() {
  toast('Demo mode — install KanBot locally to run agents for real');
  return {};
}
function demoGet(path) {
  if (path.startsWith('/api/agent-sessions')) return { sessions: DEMO.sessions };
  if (path.startsWith('/api/runners')) return { runners: DEMO.runners };
  if (path.includes('/insights')) return { insights: [] };
  if (path.startsWith('/api/sessions')) return { sessions: [], events: [] };
  if (path.startsWith('/api/boards')) return { board: DEMO.board, columns: DEMO.columns, cards: DEMO.cards, tags: [] };
  return {};
}

// ---- state --------------------------------------------------------------
const S = {
  boards: [],
  boardId: null,
  board: null, columns: [], cards: [], tags: [],
  agents: [], agentByName: {}, insightProviders: [], profiles: [],
  runners: [],
  openCardId: null,
  terminals: {},        // session_id -> terminal DOM node (while drawer open)
  sessionsCache: {},    // card_id -> [sessions]
  agentSessions: [],    // discovered claude/codex sessions across runners
  sessionsModalOpen: false,
  dragSession: null,    // session object currently being dragged
  demo: false,          // true when running without a backend (Vercel)
  apiBase: '',          // '' = same origin; or an absolute local server URL
  imageTarget: null,    // the prompt textarea a dropped image should attach to
};

const COLOR_BY_KIND = { info: 'info', backlog: 'backlog', queued: 'queued', running: 'running', review: 'review', done: 'done', custom: 'custom' };

// ---- demo data (used only when there is no backend) ---------------------
const NOW = Math.floor(Date.now() / 1000);
const DEMO = {
  agents: [
    { name: 'claude', label: 'Claude Code', color: '#d97757' },
    { name: 'codex', label: 'Codex', color: '#10a37f' },
    { name: 'gemini', label: 'Gemini CLI', color: '#4285f4' },
    { name: 'glm', label: 'GLM / Z.ai', color: '#2563eb' },
    { name: 'shell', label: 'Shell command', color: '#64748b' },
  ],
  runners: [{ name: 'mac-studio', host: 'mac-studio.local', status: 'online', active: 1,
    max_concurrency: 3, capabilities: ['claude', 'codex', 'glm', 'shell'] }],
  board: { id: 'demo', name: 'KanBot', repo_path: '' },
  columns: [
    { id: 'c-back', kind: 'backlog', name: 'Backlog', position: 0 },
    { id: 'c-run', kind: 'running', name: 'Running', position: 1 },
    { id: 'c-rev', kind: 'review', name: 'Review', position: 2 },
    { id: 'c-done', kind: 'done', name: 'Done', position: 3 },
  ],
  cards: [],  // demo shows only discovered sessions — KanBot is about tracking your TUIs
  sessions: [
    { agent: 'claude', session_id: 'demo-1', runner_id: 'r', runner_name: 'mac-studio', name: 'api-gateway',
      recap: 'Wired the rate limiter into the middleware stack and added 12 tests — all green. Want me to add per-key overrides next?',
      recap_role: 'assistant', turns: 34, duration: 5400, mtime: NOW - 10, active: true,
      tail: [
        { role: 'user', text: 'add a token bucket rate limiter to the api routes' },
        { role: 'assistant', text: 'Added a token-bucket limiter (60 req/min default) as ASGI middleware, with Redis-backed counters and a 429 + Retry-After response.' },
        { role: 'user', text: 'nice, now make sure it has tests' },
        { role: 'assistant', text: 'Wired the rate limiter into the middleware stack and added 12 tests — all green. Want me to add per-key overrides next?' },
      ] },
    { agent: 'codex', session_id: 'demo-2', runner_id: 'r', runner_name: 'mac-studio', name: 'web-dashboard',
      recap: 'Migrated the charts to Recharts and committed (8f1c2a). The dashboard build is ~30% smaller now.',
      recap_role: 'assistant', turns: 21, duration: 2700, mtime: NOW - 240, active: false,
      tail: [
        { role: 'user', text: 'swap the chart library to something lighter' },
        { role: 'assistant', text: 'Migrated the charts to Recharts and committed (8f1c2a). The dashboard build is ~30% smaller now.' },
      ] },
    { agent: 'claude', session_id: 'demo-3', runner_id: 'r', runner_name: 'mac-studio', name: 'ml-pipeline',
      recap: 'Done — the training job now checkpoints every 500 steps and resumes cleanly after a crash.',
      recap_role: 'assistant', turns: 47, duration: 9100, mtime: NOW - 1500, active: false,
      tail: [
        { role: 'user', text: 'make the training resumable' },
        { role: 'assistant', text: 'Done — the training job now checkpoints every 500 steps and resumes cleanly after a crash.' },
      ] },
    { agent: 'glm', session_id: 'demo-4', runner_id: 'r', runner_name: 'mac-studio', name: 'landing-site',
      recap: 'ok ship it', recap_role: 'user', turns: 12, duration: 1800, mtime: NOW - 7200, active: false,
      tail: [
        { role: 'assistant', text: 'Rebuilt the hero section with a cleaner type scale and a subtle gradient. Preview is live.' },
        { role: 'user', text: 'ok ship it' },
      ] },
    { agent: 'codex', session_id: 'demo-5', runner_id: 'r', runner_name: 'mac-studio', name: 'infra-scripts',
      recap: 'Terraform plan is clean; 3 resources to add, 0 to destroy. Apply when ready.',
      recap_role: 'assistant', turns: 9, duration: 900, mtime: NOW - 26000, active: false,
      tail: [
        { role: 'user', text: 'spin up the staging environment' },
        { role: 'assistant', text: 'Terraform plan is clean; 3 resources to add, 0 to destroy. Apply when ready.' },
      ] },
    { agent: 'claude', session_id: 'demo-6', runner_id: 'r', runner_name: 'mac-studio', name: 'docs',
      recap: 'Rewrote the getting-started guide and fixed 7 broken links.', recap_role: 'assistant',
      turns: 16, duration: 3600, mtime: NOW - 90000, active: false,
      tail: [
        { role: 'user', text: 'clean up the docs' },
        { role: 'assistant', text: 'Rewrote the getting-started guide and fixed 7 broken links.' },
      ] },
  ],
};

function enterDemo(showModal = true) {
  S.demo = true;
  document.body.classList.add('demo');
  $('#version').textContent = 'demo';
  S.agents = DEMO.agents;
  S.agentByName = Object.fromEntries(DEMO.agents.map(a => [a.name, a]));
  S.insightProviders = [];
  S.profiles = [{ name: 'lean', label: 'Lean — write the least code', description: 'Reuse-first, YAGNI, minimal-diff working style.' }];
  S.runners = DEMO.runners; renderRunners();
  S.boards = [DEMO.board]; S.boardId = DEMO.board.id; S.board = DEMO.board;
  S.columns = DEMO.columns; S.cards = DEMO.cards; S.tags = [];
  S.agentSessions = DEMO.sessions;
  // demo banner
  const pill = el('div', 'demo-pill');
  pill.innerHTML = 'DEMO · <b>pipx install kanbot</b> · <b>kanbot up</b> to run locally';
  $('#runners').before(pill);
  updateLiveBadge();
  renderColumns();
  wireGlobalUI();
  if (showModal) showOnboarding();
}

function cmdRow(cmd) {
  const row = el('div', 'cmd-row');
  row.appendChild(el('code', 'cmd-text', cmd));
  const btn = el('button', 'cmd-copy', 'copy');
  btn.onclick = async () => {
    try { await navigator.clipboard.writeText(cmd); btn.textContent = 'copied ✓'; setTimeout(() => btn.textContent = 'copy', 1200); }
    catch (e) { toast('copy failed — select & copy manually'); }
  };
  row.appendChild(btn);
  return row;
}

function showConnectModal() {
  const m = $('#modal'); m.innerHTML = '';
  m.appendChild(el('h3', null, 'Connect to your local KanBot'));

  const warn = el('div', 'lna-warn');
  warn.innerHTML =
    '⚠ This website needs <b>Local Network access</b> so it can read the agent ' +
    'sessions on your computer. It connects to KanBot running locally at ' +
    '<code>http://127.0.0.1:8787</code> — your browser will ask permission next. ' +
    '<b>Nothing leaves your machine</b>; the page talks only to your own computer.';
  m.appendChild(warn);

  m.appendChild(el('div', 'label', "Don't have KanBot yet? Copy & run this:"));
  m.appendChild(cmdRow('pipx install kanbot && kanbot up'));
  m.appendChild(el('div', 'label', 'no pipx? install it · or zero-install:'));
  m.appendChild(cmdRow('brew install pipx'));
  m.appendChild(cmdRow('uvx kanbot up'));

  const actions = el('div', 'modal-actions');
  const demo = el('button', 'btn ghost', 'Explore the demo');
  demo.onclick = closeModal;
  const connect = el('button', 'btn primary', 'Allow & connect');
  connect.onclick = async () => {
    connect.textContent = 'Connecting…'; connect.disabled = true;
    const ok = await attemptLocal();
    if (!ok) {
      connect.textContent = 'Allow & connect'; connect.disabled = false;
      toast('No local KanBot reachable — run `kanbot up`, then retry');
    }
  };
  actions.appendChild(demo); actions.appendChild(connect);
  m.appendChild(actions);
  openModal();
}

function showOnboarding() {
  const m = $('#modal'); m.innerHTML = '';
  m.appendChild(el('h3', null, 'Welcome to KanBot 👋'));
  const intro = el('div', null,
    "This is a live demo with sample data — no local KanBot detected on this machine.");
  intro.style.cssText = 'font-size:13.5px;line-height:1.5;color:var(--text-dim);';
  m.appendChild(intro);

  const what = el('div', null,
    "KanBot runs on your machine and shows what your coding-agent TUIs (Claude Code, Codex, …) are doing in real time — and lets you resume any of them from one board.");
  what.style.cssText = 'font-size:13px;line-height:1.55;color:var(--text-dim);';
  m.appendChild(what);

  m.appendChild(el('div', 'label', 'Get your real sessions here — copy & run:'));
  m.appendChild(cmdRow('pipx install kanbot && kanbot up'));
  const tip = el('div', null, 'then reload this page — it auto-connects to your local KanBot.');
  tip.style.cssText = 'font-size:12px;color:var(--text-dim);line-height:1.5;';
  m.appendChild(tip);

  const note = el('div', 'label',
    'This page auto-connects to a local KanBot at http://127.0.0.1:8787 — or open that URL directly.');
  m.appendChild(note);

  const actions = el('div', 'modal-actions');
  const explore = el('button', 'btn ghost', 'Explore the demo');
  explore.onclick = closeModal;
  const connect = el('button', 'btn primary', 'Connect to local');
  connect.onclick = async () => {
    try {
      const r = await fetch(LOCAL_KANBOT + '/api/health', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch (e) {}
    toast('No local KanBot found — run `kanbot up` first, then retry');
  };
  actions.appendChild(explore); actions.appendChild(connect);
  m.appendChild(actions);
  openModal();
}

// ---- boot ---------------------------------------------------------------
const LOCAL_KANBOT = 'http://127.0.0.1:8787';

async function boot() {
  const origin = location.origin.replace(/\/$/, '');
  const onLocalOrigin = /(127\.0\.0\.1|localhost):8787/.test(origin);

  // 1) Backend on this very origin (you opened the local board directly).
  try {
    const r = await fetch(origin + '/api/health', { cache: 'no-store' });
    if (r.ok) {
      const h = await r.json();
      S.apiBase = ''; $('#version').textContent = 'v' + h.version;
      return liveSetup();
    }
  } catch (e) { /* no same-origin backend */ }

  if (onLocalOrigin) { return enterDemo(); }  // local origin but server down

  // 2) Hosted page. Show the demo behind, but DON'T touch the local network
  //    until the user agrees — that fetch is what triggers the browser's
  //    "Local Network Access" prompt, so we warn first.
  enterDemo(false);
  if (localStorage.getItem('kanbot_connect_local') === '1') {
    if (await attemptLocal()) return;          // previously allowed: connect quietly
    localStorage.removeItem('kanbot_connect_local');
  }
  showConnectModal();
}

async function liveSetup() {
  const ag = await api.get('/api/agents');
  S.agents = ag.agents; S.insightProviders = ag.insights; S.profiles = ag.profiles || [];
  S.agentByName = Object.fromEntries(ag.agents.map(a => [a.name, a]));
  await refreshRunners();
  await loadBoards();
  await loadAgentSessions();
  connectWS();
  wireGlobalUI();
}

async function attemptLocal() {
  try {
    const r = await fetch(LOCAL_KANBOT + '/api/health', { cache: 'no-store' });
    if (!r.ok) return false;
    const h = await r.json();
    localStorage.setItem('kanbot_connect_local', '1');
    S.demo = false; S.apiBase = LOCAL_KANBOT;
    document.body.classList.remove('demo');
    const pill = $('.demo-pill'); if (pill) pill.remove();
    $('#version').textContent = 'local · v' + h.version;
    closeModal();
    await liveSetup();
    return true;
  } catch (e) { return false; }
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
  let board = boards[0];
  if (!board) board = (await api.post('/api/boards', { name: 'Deckhand' })).board;
  S.boards = [board];
  await selectBoard(board.id);
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
  const base = S.apiBase || location.origin;
  const url = base.replace(/^http/, 'ws') + '/ws/web';
  let ws;
  try { ws = new WebSocket(url); } catch (e) { return; }
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
    pill.title = 'Run `kanbot up` to attach a worker.';
    wrap.appendChild(pill);
    return;
  }
  for (const r of online) {
    const pill = el('div', 'runner-pill ' + r.status);
    pill.appendChild(el('span', 'dot'));
    pill.appendChild(el('span', null, r.name));
    const caps = el('span', 'caps', (r.capabilities || []).join('·') || '—');
    pill.appendChild(caps);
    const safe = r.auto_approve === 0 || r.auto_approve === false;
    if (safe) { const lock = el('span', 'runner-safe', '🔒 safe'); pill.appendChild(lock); }
    pill.title = `${r.name} @ ${r.host} — ${r.active}/${r.max_concurrency} busy\n`
      + `mode: ${safe ? 'SAFE (no auto-approve)' : 'auto-approve'}\nagents: ${(r.capabilities||[]).join(', ')}`;
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
  // Dedupe: a session adopted as a card (resumed) is represented by that card,
  // so hide its discovered twin. Also hide an active session whose cwd matches a
  // currently-running task card (that's the card's own session writing to disk).
  const adopted = new Set(S.cards.filter(c => c.resume_of).map(c => c.resume_of));
  const runningCwds = new Set(S.cards.filter(c => c.status === 'running' && c.cwd).map(c => c.cwd));
  const byBucket = { backlog: [], running: [], done: [] };
  for (const s of S.agentSessions) {
    if (adopted.has(s.session_id)) continue;
    if (s.active && runningCwds.has(s.cwd)) continue;
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
  // latest activity — never the stale first message
  return s.recap || s.last_text || s.last_user || '';
}

function fmtDur(sec) {
  sec = sec || 0;
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d`;
}

function renderSessionCard(s) {
  const card = el('div', 'card sess' + (s.active ? ' s-running' : ''));
  card.style.cursor = 'grab';
  card.draggable = true;
  card.addEventListener('dragstart', (e) => {
    S.dragSession = s;
    e.dataTransfer.setData('text/plain', 'session:' + s.session_id);
    card.classList.add('dragging');
  });
  card.addEventListener('dragend', () => card.classList.remove('dragging'));
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
    pv.appendChild(el('span', 'sess-pv-tag', s.recap_role === 'user' ? '› ' : '↩ '));
    pv.appendChild(document.createTextNode(preview));
    card.appendChild(pv);
  }
  const foot = el('div', 'sess-foot');
  foot.appendChild(el('span', 'sess-turns', `${s.turns} turns · brewed ${fmtDur(s.duration)}`));
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
  if (c.loop_max > 1) meta.appendChild(el('span', 'resume-badge', `⟳ loop ×${c.loop_max}`));
  if (c.profile) meta.appendChild(el('span', 'resume-badge', `◇ ${c.profile}`));
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

function agentColor(name) {
  const a = S.agentByName[name];
  if (a) return a.color;
  // stable color for any unknown / custom tracker (e.g. hermes, your own agent)
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return `hsl(${h}, 65%, 62%)`;
}
function agentBadge(name) {
  const a = S.agentByName[name];
  const badge = el('span', 'agent-badge');
  const dot = el('span', 'adot');
  dot.style.background = name === 'auto' ? '#888' : agentColor(name);
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
  const data = e.dataTransfer.getData('text/plain');

  // dragging a discovered session onto Running resumes it immediately
  if (S.dragSession && data.startsWith('session:')) {
    const s = S.dragSession; S.dragSession = null;
    if (col.kind !== 'running') return;
    try {
      await api.post(`/api/boards/${S.boardId}/revive`, {
        runner_id: s.runner_id, agent: s.agent, session_id: s.session_id,
        cwd: s.cwd, title: `↻ ${s.name || s.agent}`.slice(0, 80),
        prompt: 'Continue where you left off.', run: true,
      });
      toast('resuming ' + (s.name || s.agent));
    } catch (err) { toast('revive failed: ' + err.message); }
    return;
  }

  const cardId = data;
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

// collapsible loop section — hidden by default (most tasks just run once)
function loopSection(initialMax, initialUntil, onChange) {
  const max = Math.max(1, parseInt(initialMax) || 1);
  const until = initialUntil || '';
  const wrap = el('div', 'field');
  const toggle = el('button', 'loop-toggle');
  const lbody = el('div', 'loop-body');
  lbody.style.display = (max > 1 || until) ? 'block' : 'none';
  const label = () => { toggle.textContent = (lbody.style.display === 'none' ? '▸' : '▾') + ' ⟳ run in a loop'; };
  toggle.onclick = () => { lbody.style.display = lbody.style.display === 'none' ? 'block' : 'none'; label(); };
  label();
  const row = el('div', 'row');
  const loopMax = inputField('Loop iterations', '1');
  loopMax.input.type = 'number'; loopMax.input.min = '1'; loopMax.input.value = String(max);
  const loopUntil = inputField('Loop until (shell exits 0 = stop)', 'e.g. ! grep -q "[ ]" todo.md');
  loopUntil.input.value = until;
  if (onChange) {
    const fire = () => onChange(parseInt(loopMax.input.value) || 1, loopUntil.input.value);
    loopMax.input.onblur = fire; loopUntil.input.onblur = fire;
  }
  row.appendChild(loopMax.wrap); row.appendChild(loopUntil.wrap);
  lbody.appendChild(row);
  wrap.appendChild(toggle); wrap.appendChild(lbody);
  return { wrap, maxInput: loopMax.input, untilInput: loopUntil.input };
}

// ---- composer (quick add) ----------------------------------------------
function openComposer(columnId) {
  const m = $('#modal');
  m.innerHTML = '';
  m.appendChild(el('h3', null, 'New task'));

  const title = inputField('Title', 'e.g. Add unit tests for parser');
  const prompt = textareaField('Prompt for the agent', 'Describe the work. Paste or drop an image to attach it.');
  enableImagePaste(prompt.input);
  const agentSel = agentSelectField(S.board?.default_agent || 'auto');
  const profileSel = profileSelectField('');
  const cwd = inputField('Working directory', S.board?.repo_path || '/path/to/repo');
  cwd.input.value = S.board?.repo_path || '';

  m.appendChild(title.wrap);
  m.appendChild(prompt.wrap);
  const row = el('div', 'row');
  row.appendChild(agentSel.wrap);
  row.appendChild(profileSel.wrap);
  m.appendChild(row);
  m.appendChild(cwd.wrap);

  const loop = loopSection(1, '', null);
  m.appendChild(loop.wrap);

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
      loop_max: parseInt(loop.maxInput.value) || 1,
      loop_until: loop.untilInput.value,
      profile: profileSel.select.value,
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
  enableImagePaste(prompt.input, () => patchCard(card.id, { prompt: prompt.input.value }));
  body.appendChild(prompt.wrap);

  // agent + cwd
  const row = el('div', 'row');
  const agentSel = agentSelectField(card.agent);
  agentSel.select.onchange = () => patchCard(card.id, { agent: agentSel.select.value });
  const cwd = inputField('Working directory', '/path/to/repo');
  cwd.input.value = card.cwd || '';
  cwd.input.onblur = () => patchCard(card.id, { cwd: cwd.input.value });
  const profileSel = profileSelectField(card.profile || '');
  profileSel.select.onchange = () => patchCard(card.id, { profile: profileSel.select.value });
  row.appendChild(agentSel.wrap); row.appendChild(profileSel.wrap); row.appendChild(cwd.wrap);
  body.appendChild(row);

  // loop (collapsed unless the card already loops)
  const loop = loopSection(card.loop_max, card.loop_until,
    (max, until) => patchCard(card.id, { loop_max: max, loop_until: until }));
  body.appendChild(loop.wrap);

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

  // one-step add: type a name, press Enter -> create (if new) + attach
  const input = el('input', 'tag-input');
  input.placeholder = '＋ type a tag, press Enter';
  input.onkeydown = async (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    input.value = '';
    try {
      let tag = S.tags.find(t => t.name.toLowerCase() === name.toLowerCase());
      if (!tag) {
        const h = [...name].reduce((a, c) => a * 31 + c.charCodeAt(0), 7);
        const color = PALETTE[Math.abs(h) % PALETTE.length];
        tag = await api.post(`/api/boards/${S.boardId}/tags`, { name, color });
        if (tag && tag.id) S.tags.push(tag);
      }
      if (tag && tag.id) await api.post(`/api/cards/${card.id}/tags`, { tag_id: tag.id });
    } catch (err) { toast('add tag failed: ' + err.message); }
  };
  wrap.appendChild(input);

  // quick-add existing board tags + full editor (color/insight)
  const avail = S.tags.filter(t => !cardTagIds.has(t.id));
  const addRow = el('div', 'tag-add');
  for (const t of avail) {
    const chip = el('span', 'tag-pickable');
    chip.style.color = t.color;
    if (t.insight) chip.appendChild(el('span', null, '◆'));
    chip.appendChild(el('span', null, '+ ' + t.name));
    chip.onclick = async () => { await api.post(`/api/cards/${card.id}/tags`, { tag_id: t.id }); };
    addRow.appendChild(chip);
  }
  const newTag = el('span', 'tag-pickable', '⚙ tag w/ color & insight');
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

const PALETTE = ['#4f8cff', '#4fd1e0', '#b491f5', '#5fd28a', '#ff6b6b', '#6aa6ff', '#f5c451', '#9aa3b2'];

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

// ---- API reference (so anyone can build their own UI) -------------------
const API_SPEC = `KanBot HTTP + WebSocket API
===========================
KanBot is a local server that turns your coding-agent TUIs (Claude Code, Codex,
and any agent that logs JSONL) into a visual Kanban board. This document is the
complete API — paste it into an LLM (or read it) to build your own UI/client.

BASE URL
  http://127.0.0.1:8787   (the server origin; change host/port as needed)
  Start it with: pip install kanbot && kanbot up
AUTH
  None by default. If the server sets env KANBOT_TOKEN, runners must pass it as
  the ws query param ?token=...; REST is open on localhost.
CONTENT TYPE
  JSON for all request/response bodies.

CORE MODEL
  Board     { id, name, repo_path, created_at }
  Column    { id, board_id, name, kind, position }
            kind in: backlog | running | done   (queue is a status, not a column)
  Card      { id, board_id, column_id, title, prompt, agent, cwd, status,
              position, auto_advance, resume_of, pin_runner, tags[], created_at, updated_at }
            status in: idle | queued | running | done | failed | cancelled
            agent: "auto" or an agent name (claude, codex, gemini, glm, shell, ...)
            resume_of: an external session id this card resumes (optional)
            pin_runner: restrict execution to one runner id (optional)
  Tag       { id, board_id, name, color, insight, config }
            insight in: "" (plain label) | git | files | command
  Session   { id, card_id, board_id, runner_id, runner_name, agent, status,
              prompt, cwd, exit_code, started_at, ended_at, created_at }
            status in: pending | assigned | running | success | failed | cancelled
  Event     { id, session_id, ts, stream, text }   stream in: stdout|stderr|system
  Runner    { id, name, host, capabilities[], status, active, max_concurrency, last_seen }
            status in: online | busy | offline
  AgentSession (a discovered TUI session, the heart of KanBot)
            { agent, session_id, name, cwd, recap, recap_role, last_user, last_text,
              tail:[{role,text}], turns, started_at, mtime, duration, active,
              runner_id, runner_name }
            active=true means its transcript was written in the last ~45s (working now).

REST ENDPOINTS
  GET    /api/health                         -> { ok, version, runners }
  GET    /api/agents                         -> { agents:[{name,label,bin,description,color}], insights:[...] }
  GET    /api/runners                        -> { runners:[Runner] }
  GET    /api/agent-sessions                 -> { sessions:[AgentSession] }   (all discovered TUIs)

  GET    /api/boards                         -> { boards:[Board] }
  POST   /api/boards            {name, repo_path?}      -> { board, columns, cards, tags }
  GET    /api/boards/{id}                     -> { board, columns, cards, tags }
  DELETE /api/boards/{id}                     -> { ok }

  POST   /api/boards/{id}/cards {title, prompt?, agent?, cwd?, column_id?} -> Card
  PATCH  /api/cards/{id}        {title?,prompt?,agent?,cwd?,status?,auto_advance?} -> Card
  POST   /api/cards/{id}/move   {column_id, position}  -> Card   (drop into 'running' kind = queue it)
  POST   /api/cards/{id}/run                  -> Card   (queue this card for a runner now)
  DELETE /api/cards/{id}                      -> { ok }
  GET    /api/cards/{id}/insights             -> { insights:[{ok,title,summary,lines,detail,tag}] }

  POST   /api/boards/{id}/tags  {name,color?,insight?,config?}  -> Tag
  DELETE /api/tags/{id}                       -> { ok }
  POST   /api/cards/{id}/tags   {tag_id}      -> Card
  DELETE /api/cards/{id}/tags/{tag_id}        -> Card

  GET    /api/sessions?board_id=&card_id=     -> { sessions:[Session] }
  GET    /api/sessions/{id}?after=<event_id>  -> { session, events:[Event] }
  POST   /api/sessions/{id}/cancel            -> { ok }

  POST   /api/boards/{id}/revive
         {runner_id, agent, session_id, cwd?, title?, prompt?, run?}  -> Card
         Adopts a discovered AgentSession as a card that RESUMES it
         (claude --resume / codex exec resume). run=true dispatches immediately.

WEBSOCKET (live updates): ws://127.0.0.1:8787/ws/web
  Read-only stream of JSON events. Connect and re-render on each:
    { type:"hello", version }
    { type:"card.created"|"card.updated", card:Card }
    { type:"card.deleted", card_id }
    { type:"session.created"|"session.updated", session:Session }
    { type:"session.event", session_id, event:Event }     (live log line)
    { type:"runner.updated", runner:Runner }
    { type:"agent.sessions.updated" }                       (re-fetch /api/agent-sessions)
    { type:"board.created"|"board.deleted", ... }
    { type:"tag.created"|"tag.deleted", ... }

LIFECYCLE
  Discovered TUIs are bucketed by recency: working now -> Running column,
  finished < 30 min -> Done, older -> Backlog. A card you run goes
  idle -> queued -> running -> (success) review / (fail) failed. A scheduler
  matches queued cards to idle runners by agent capability (and pin_runner).

HOW TO BUILD A UI
  1. GET /api/boards (or create one), render columns + cards.
  2. GET /api/agent-sessions and bucket by .active and (now - .mtime).
  3. Open /ws/web; apply events to your local state.
  4. To run work: POST a card then POST /api/cards/{id}/run; stream its logs via
     GET /api/sessions/{id} then ws session.event lines.
  5. To resume a TUI: POST /api/boards/{id}/revive with the AgentSession fields.
`;

function openApiModal() {
  const m = $('#modal'); m.innerHTML = '';
  m.appendChild(el('h3', null, 'KanBot API — build your own UI'));
  m.appendChild(el('div', 'label', 'Complete REST + WebSocket reference. Copy it into an LLM to scaffold a client.'));
  const pre = el('pre', 'api-spec'); pre.textContent = API_SPEC;
  m.appendChild(pre);
  const actions = el('div', 'modal-actions');
  const copy = el('button', 'btn', 'Copy spec');
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(API_SPEC); copy.textContent = 'Copied ✓'; setTimeout(() => copy.textContent = 'Copy spec', 1500); }
    catch (e) { toast('copy failed — select & copy manually'); }
  };
  const close = el('button', 'btn primary', 'Done'); close.onclick = closeModal;
  actions.appendChild(copy); actions.appendChild(close);
  m.appendChild(actions);
  openModal(); m.classList.add('wide');
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

// ---- minimal markdown -> HTML (for the terminal transcript preview) -----
function mdEscape(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function mdInline(s) {
  // s is already HTML-escaped
  s = s.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
function renderMarkdown(md) {
  const lines = mdEscape(md).split('\n');
  let html = '', i = 0, inCode = false, codeBuf = [], list = null;
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      if (!inCode) { inCode = true; codeBuf = []; }
      else { inCode = false; closeList(); html += '<pre class="md-pre">' + codeBuf.join('\n') + '</pre>'; }
      i++; continue;
    }
    if (inCode) { codeBuf.push(line); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeList();
      const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const head = cells(line); i += 2; const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      html += '<table class="md-table"><thead><tr>' + head.map(h => `<th>${mdInline(h)}</th>`).join('') +
        '</tr></thead><tbody>' + rows.map(r => '<tr>' + r.map(c => `<td>${mdInline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>';
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { closeList(); html += `<div class="md-h md-h${h[1].length}">${mdInline(h[2])}</div>`; i++; continue; }
    const ul = line.match(/^\s*[-*]\s+(.*)$/), ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) { const t = ul ? 'ul' : 'ol'; if (list !== t) { closeList(); list = t; html += `<${t} class="md-list">`; } html += `<li>${mdInline((ul || ol)[1])}</li>`; i++; continue; }
    if (line.trim() === '') { closeList(); html += '<div class="md-sp"></div>'; i++; continue; }
    closeList(); html += `<div class="md-p">${mdInline(line)}</div>`; i++;
  }
  closeList(); if (inCode) html += '<pre class="md-pre">' + codeBuf.join('\n') + '</pre>';
  return html;
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
  const outLine = (txt) => {
    const d = el('div', 'term-line term-out md');
    d.innerHTML = renderMarkdown(txt);
    body.appendChild(d);
  };
  const tail = s.tail || [];
  if (tail.length) {
    commentLine(`# recent transcript — ${s.turns} turns · brewed ${fmtDur(s.duration)}`);
    for (const msg of tail) (msg.role === 'user' ? userLine : outLine)(msg.text);
  } else if (s.recap) {
    outLine(s.recap);
  } else if (s.title) {
    commentLine('# started with'); userLine(s.title);
  } else {
    outLine('(no readable transcript)');
  }
  term.appendChild(body);
  m.appendChild(term);
  // jump to the most recent message
  requestAnimationFrame(() => { body.scrollTop = body.scrollHeight; });

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

// Paste or drag an image into a prompt textarea -> upload -> inject a file
// reference the agent can read, and show a thumbnail.
function hasFiles(dt) { return dt && dt.types && [...dt.types].includes('Files'); }

function enableImagePaste(input, onChange) {
  const strip = el('div', 'img-strip');
  input.after(strip);
  const handle = async (file) => {
    if (!file || !(file.type || '').startsWith('image/')) return;
    if (S.demo) { toast('Image drop works on your local KanBot — connect first'); return; }
    const dataUrl = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file); });
    try {
      const out = await api.post('/api/uploads', { name: file.name || 'pasted.png', data: dataUrl });
      if (!out || !out.path) { toast('upload failed'); return; }
      input.value += (input.value && !input.value.endsWith('\n') ? '\n' : '') + 'Attached image (read this file): ' + out.path;
      if (onChange) onChange();
      const thumb = el('img', 'img-thumb'); thumb.src = (S.apiBase || '') + out.url; thumb.title = out.path;
      strip.appendChild(thumb);
      toast('image attached');
    } catch (e) { toast('upload failed: ' + e.message); }
  };
  // expose so a drop anywhere on the page can route to the active prompt
  input._imgHandle = handle;
  const markActive = () => { S.imageTarget = input; };
  input.addEventListener('focus', markActive);
  markActive();
  input.addEventListener('paste', (e) => {
    for (const it of (e.clipboardData && e.clipboardData.items) || [])
      if (it.type && it.type.startsWith('image/')) { const f = it.getAsFile(); if (f) { e.preventDefault(); handle(f); } }
  });
  input.addEventListener('dragover', (e) => { if (hasFiles(e.dataTransfer)) { e.preventDefault(); input.classList.add('drag-img'); } });
  input.addEventListener('dragleave', () => input.classList.remove('drag-img'));
  input.addEventListener('drop', (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault(); input.classList.remove('drag-img');
    [...e.dataTransfer.files].filter(f => (f.type || '').startsWith('image/')).forEach(handle);
  });
}

// Catch image drops anywhere on the page so the browser never navigates away;
// route them to the prompt box that's currently open.
function installGlobalImageDrop() {
  if (window.__kbDrop) return; window.__kbDrop = true;
  window.addEventListener('dragover', (e) => { if (hasFiles(e.dataTransfer)) e.preventDefault(); });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    const imgs = [...e.dataTransfer.files].filter(f => (f.type || '').startsWith('image/'));
    if (!imgs.length) return;
    const t = S.imageTarget;
    if (t && t.isConnected && t._imgHandle) imgs.forEach(t._imgHandle);
    else toast('Open a task (or + add task) first, then drop the image onto its prompt');
  });
}
function availableAgentNames() {
  // union of capabilities advertised by online runners
  const set = new Set();
  for (const r of S.runners || []) {
    if (r.status === 'offline') continue;
    for (const c of (r.capabilities || [])) set.add(c);
  }
  return set;
}

function agentSelectField(value) {
  const wrap = el('div', 'field');
  wrap.appendChild(el('div', 'label', 'Agent'));
  const select = el('select', 'select');
  const auto = el('option', null, 'auto (any available)'); auto.value = 'auto'; select.appendChild(auto);
  const avail = availableAgentNames();
  const showAll = avail.size === 0;  // no runner connected yet -> don't hide everything
  for (const a of S.agents) {
    // only list agents an online runner actually has (always keep the card's
    // current value so an existing selection still shows correctly)
    if (!showAll && !avail.has(a.name) && a.name !== value) continue;
    const o = el('option', null, a.label); o.value = a.name; select.appendChild(o);
  }
  select.value = value || 'auto';
  wrap.appendChild(select);
  return { wrap, select };
}

function profileSelectField(value) {
  const wrap = el('div', 'field');
  wrap.appendChild(el('div', 'label', 'Prompt mode'));
  const select = el('select', 'select');
  const none = el('option', null, 'none'); none.value = ''; select.appendChild(none);
  for (const p of (S.profiles || [])) {
    const o = el('option', null, p.label); o.value = p.name; o.title = p.description || '';
    select.appendChild(o);
  }
  select.value = value || '';
  wrap.appendChild(select);
  return { wrap, select };
}

// ---- modal helpers ------------------------------------------------------
function openModal() { $('#modal').classList.remove('wide'); $('#modalScrim').classList.add('open'); }
function closeModal() { $('#modalScrim').classList.remove('open'); S.sessionsModalOpen = false; }

// ---- global UI ----------------------------------------------------------
function wireGlobalUI() {
  installGlobalImageDrop();
  $('#sessionsBtn').onclick = openSessionsModal;
  $('#manageTagsBtn').onclick = openManageTags;
  $('#apiBtn').onclick = openApiModal;
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
