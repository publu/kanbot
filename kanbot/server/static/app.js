// KanBot console: every agent on every runner, live, typeable. One file, no build.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {board:null, cards:[], panes:[], sessions:[], agents:[], selected:null, selPane:null,
               activeSession:null, events:[], preview:null, runnerOnline:false, runnerId:'', ws:null, attached:null};
const api = async (path, opts={}) => {const r = await fetch(path, {...opts, headers:{'Content-Type':'application/json', ...(opts.headers||{})}}); if(!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.statusText); return r.json()};
const post = (path, body={}) => api(path, {method:'POST', body:JSON.stringify(body)});
const del = path => api(path, {method:'DELETE'});
const b64 = {enc: s => btoa(String.fromCharCode(...new TextEncoder().encode(s))),
             dec: s => Uint8Array.from(atob(s), c => c.charCodeAt(0))};
function toast(msg){const n=$('#toast'); n.textContent=msg; n.classList.add('show'); setTimeout(()=>n.classList.remove('show'), 2400)}
function age(ts){if(!ts) return 'now'; let s=Math.max(0, Date.now()/1000-ts); if(s<60) return `${Math.floor(s)}s`; if(s<3600) return `${Math.floor(s/60)}m`; if(s<86400) return `${Math.floor(s/3600)}h`; return `${Math.floor(s/86400)}d`}
const base = p => (p||'').split('/').filter(Boolean).pop() || '';
const MARK = {blocked:'◆', working:'●', idle:'○', done:'✓', unknown:'?'};

// ---- terminal (xterm.js) ----------------------------------------------------
let term = null, fit = null;
function ensureTerm(){
  if(term) return term;
  term = new window.Terminal({cursorBlink:true, fontFamily:"'DM Mono', Menlo, monospace", fontSize:12.5, lineHeight:1.25,
    theme:{background:'#10120f', foreground:'#d9ddd2', cursor:'#c9f35b', selectionBackground:'#3a4d1a'}, scrollback:5000, allowProposedApi:true});
  fit = new window.FitAddon.FitAddon(); term.loadAddon(fit);
  const host=$('#terminal'); host.innerHTML=''; term.open(host); fit.fit();
  term.onData(d => { if(state.attached) send({type:'pane.input', pane_id:state.attached, data:b64.enc(d)}) });
  new ResizeObserver(()=>{ if(!term) return; try{fit.fit()}catch{} if(state.attached) send({type:'pane.resize', pane_id:state.attached, rows:term.rows, cols:term.cols}) }).observe(host);
  return term;
}
function send(m){ if(state.ws && state.ws.readyState===1) state.ws.send(JSON.stringify(m)) }
function attach(paneId){
  const t=ensureTerm(); $('#terminal').classList.remove('hidden'); $('#logview').classList.add('hidden');
  if(state.attached!==paneId){ t.reset(); state.attached=paneId; try{fit.fit()}catch{} send({type:'pane.attach', pane_id:paneId, rows:t.rows, cols:t.cols}) }
  const p=state.panes.find(x=>x.id===paneId); $('#typeInput').disabled=$('#typeSend').disabled=!(p&&p.alive); t.focus();
}
function detach(){ if(state.attached){ send({type:'pane.detach'}); state.attached=null } }

// ---- boot / data ------------------------------------------------------------
async function boot(){
  let boards=(await api('/api/boards')).boards;
  if(!boards.length) await post('/api/boards',{name:'Agents', repo_path:''}).then(x=>boards=[x.board]);
  const x=await api(`/api/boards/${boards[0].id}`); Object.assign(state, x);
  try{ state.agents=(await api('/api/agents')).agents }catch{}
  await Promise.all([loadRunners(), loadPanes(), loadExternal()]);
  connect(); render();
  const want=new URLSearchParams(location.hash.slice(1)).get('pane');   // deep link: /#pane=<id>
  const p=want && state.panes.find(x=>x.id===want || x.id.startsWith(want));
  if(p) selectPane(p); else if(!state.panes.length && !state.cards.length) openComposer();
}
async function loadRunners(){
  const x=await api('/api/runners'); const online=x.runners.filter(r=>r.status!=='offline');
  state.runnerOnline=online.length>0; state.runnerId=online[0]?.id||''; state.runners=online;
  const el=$('#runner'); el.classList.toggle('warn', !online.length);
  el.textContent=online.length ? (online.length===1 ? `● ${online[0].name}` : `● ${online.length} runners`) : '⚠ No runner — run: kanbot up';
  fillAgentSelect();
}
function fillAgentSelect(){
  const sel=$('#taskAgent'); const caps=new Set((state.runners||[]).flatMap(r=>r.capabilities||[]));
  const list=state.agents.filter(a=>caps.has(a.name)); const cur=sel.value;
  sel.innerHTML=(list.length?list:state.agents).map(a=>`<option value="${esc(a.name)}">${esc(a.label)}${a.interactive?'':' (headless only)'}</option>`).join('');
  if([...sel.options].some(o=>o.value===cur)) sel.value=cur;
}
async function loadPanes(){ try{ state.panes=(await api('/api/panes')).panes }catch{ state.panes=[] } render(); updateTitle() }
async function loadExternal(){ try{ state.sessions=(await api('/api/agent-sessions')).sessions.slice(0,14) }catch{} render() }
function updateTitle(){ const n=state.panes.filter(p=>p.state==='blocked').length; document.title=(n?`◆ ${n} need${n>1?'':'s'} you · `:'')+'KanBot' }

// ---- rail -------------------------------------------------------------------
function render(){
  const lanes=$('#lanes'); lanes.innerHTML='';
  const live=state.panes.filter(p=>p.alive), blocked=live.filter(p=>p.state==='blocked'), rest=live.filter(p=>p.state!=='blocked');
  const finished=state.panes.filter(p=>!p.alive).sort((a,b)=>(b.ended_at||0)-(a.ended_at||0)).slice(0,6);
  const lane=(name, nodes, extra)=>{ if(!nodes.length && !extra) return; const l=document.createElement('section'); l.className='lane';
    l.innerHTML=`<div class="lane-title"><span>${name}</span><b>${nodes.length}</b></div>`; nodes.forEach(n=>l.append(n)); if(extra) l.append(extra); lanes.append(l) };
  lane('NEEDS YOU', blocked.map(paneNode));
  lane('AGENTS', rest.map(paneNode), !rest.length && !blocked.length ? hintNode(state.runnerOnline ? 'No agents running. Press n, or: kanbot agent start claude "…"' : 'Start a runner: kanbot up') : null);
  const queued=state.cards.filter(c=>['queued','idle'].includes(c.status) && !c.resume_of || c.status==='review');
  lane('QUEUE', queued.map(cardNode));
  lane('FINISHED', [...finished.map(paneNode), ...state.cards.filter(c=>['done','failed','cancelled'].includes(c.status) && !finished.some(p=>p.card_id===c.id)).sort((a,b)=>b.updated_at-a.updated_at).slice(0,4).map(cardNode)]);
  lane('PICK UP A SESSION', state.sessions.filter(s=>!live.some(p=>p.session_id===s.session_id)).slice(0,8).map(sessionNode));
}
function hintNode(t){ const d=document.createElement('div'); d.className='rail-hint'; d.textContent=t; return d }
function paneNode(p){
  const n=document.createElement('article'); n.className=`card pane st-${p.state}`+(state.selPane===p.id?' selected':'');
  const card=state.cards.find(c=>c.id===p.card_id);
  n.innerHTML=`<div class="card-top"><i class="dot"></i><h3>${esc(card?card.title:p.title)}</h3><span class="badge ${esc(p.state)}">${esc(p.state)}${p.exit_code!=null&&p.exit_code!==0?' · exit '+p.exit_code:''}</span></div>
    <div class="card-foot"><span>${esc(p.agent)}${p.interactive?'':' · headless'} · ${esc(base(p.cwd))||'~'} · ${age(p.started_at)}</span><span class="rn">${esc(p.runner_name||'')}</span></div>`;
  n.onclick=()=>selectPane(p); return n;
}
function cardNode(c){
  const n=document.createElement('article'); n.className='card'+(state.selected===c.id?' selected':'');
  const label=c.status==='review'?'plan ready':c.status;
  n.innerHTML=`<div class="card-top"><h3>${esc(c.title)}</h3><span class="badge ${esc(c.status)}">${esc(label)}</span></div><p>${esc(c.prompt)}</p><div class="card-foot"><span>${esc(c.agent==='auto'?'claude':c.agent)}${c.interactive?' · ⌨':''} · ${age(c.updated_at)}</span>${c.plan_mode?'<span class="plan">◇ PLAN FIRST</span>':''}</div>`;
  if(c.status==='review'){const a=document.createElement('div');a.className='review-actions';a.innerHTML='<button class="approve">Approve & run</button><button class="discard">Discard</button>';
    a.querySelector('.approve').onclick=async e=>{e.stopPropagation();await post(`/api/cards/${c.id}/approve`);toast('Plan approved — execution queued')};
    a.querySelector('.discard').onclick=async e=>{e.stopPropagation();if(confirm('Discard this task and its plan?'))await del(`/api/cards/${c.id}`)};n.append(a)}
  if(c.branch||c.merge_state==='merged'){const w=document.createElement('div');w.className='wt';const ms=c.merge_state;
    const st=ms==='merged'?'<span class="wt-ok">✓ merged</span>':ms==='merging'?'<span class="wt-dim">merging…</span>':ms==='conflict'?'<span class="wt-bad">⚠ conflict</span>':ms==='error'?'<span class="wt-bad">⚠ error</span>':'';
    w.innerHTML=`<div class="wt-head"><code>${c.branch?`⑃ ${esc(c.branch)}`:'⑃ isolated branch'}</code>${st}</div>${c.diffstat?`<pre class="wt-diff">${esc(c.diffstat)}</pre>`:''}`;
    if(c.branch&&ms!=='merged'){const b=document.createElement('button');b.className='wt-merge';b.textContent=ms==='conflict'?'Retry merge':'Merge branch →';b.disabled=ms==='merging';
      b.onclick=async e=>{e.stopPropagation();try{await post(`/api/cards/${c.id}/merge`);toast('Merging '+c.branch)}catch(err){toast(err.message)}};w.append(b)}n.append(w)}
  n.onclick=()=>selectCard(c); return n;
}
function sessionNode(s){
  const n=document.createElement('article'); n.className='card session'+(state.preview?.session_id===s.session_id?' selected':'');
  n.innerHTML=`<div class="card-top"><h3>${s.active?'● ':''}${esc(s.name||s.agent+' session')}</h3><span class="badge">${esc(s.agent)}</span></div><p>${esc(s.recap||s.title||'')}</p><div class="card-foot"><span>${esc(base(s.cwd))||'—'} · ${s.turns||0} turns · ${age(s.mtime)}</span></div>`;
  n.onclick=()=>previewSession(s); return n;
}

// ---- selection ----------------------------------------------------------------
function head(title, path, pane){
  $('#termTitle').textContent=title; $('#termPath').textContent=path||'';
  const b=$('#stateBadge'); b.classList.toggle('hidden', !pane); if(pane){ b.className=`state-badge ${pane.state}`; b.textContent=`${MARK[pane.state]||''} ${pane.state}${pane.state_source==='hook'?' · hooks':''}` }
  $('#keys').classList.toggle('hidden', !(pane&&pane.alive)); $('#attachHint').classList.toggle('hidden', !pane);
  $('#stopBtn').classList.toggle('hidden', !(pane&&pane.alive)); $('#resumeBtn').classList.add('hidden');
  if(pane){ $('#stopBtn').onclick=async()=>{ if(confirm('Stop this agent?')){ await post(`/api/panes/${pane.id}/kill`); toast('Stopping…') } };
    $('#attachHint').onclick=()=>{ navigator.clipboard?.writeText(`kanbot attach ${pane.id}`); toast(`copied: kanbot attach ${pane.id}`) } }
}
function selectPane(p){
  state.selPane=p.id; state.selected=p.card_id||null; state.preview=null; render(); history.replaceState(null,'',`#pane=${p.id}`);
  const card=state.cards.find(c=>c.id===p.card_id);
  head(card?card.title:p.title, `${p.cwd||''}${p.runner_name?'  ·  '+p.runner_name:''}  ·  ${p.id}`, p);
  attach(p.id);
}
async function selectCard(c){
  const pane=state.panes.find(p=>p.card_id===c.id && p.alive) || state.panes.filter(p=>p.card_id===c.id).sort((a,b)=>b.started_at-a.started_at)[0];
  if(pane) return selectPane(pane);
  state.selected=c.id; state.selPane=null; state.preview=null; detach(); render();
  head(c.title, c.cwd||state.board.repo_path||'default working directory', null);
  const runs=(await api(`/api/sessions?card_id=${c.id}`)).sessions;
  $('#stopBtn').classList.toggle('hidden', !['running','queued'].includes(c.status));
  $('#stopBtn').onclick=async()=>{const s=runs.find(x=>['pending','assigned','running'].includes(x.status)); if(s){await post(`/api/sessions/${s.id}/cancel`);toast('Cancellation requested')}};
  if(!runs.length) return terminalMessage(c.status!=='queued' ? 'No run output yet' : state.runnerOnline ? 'Queued — waiting for a runner slot' : 'Queued — no runner yet. Run “kanbot up”.');
  const x=await api(`/api/sessions/${runs[0].id}`); state.activeSession=runs[0].id; state.events=x.events; renderLog();
}
function previewSession(s){
  state.selected=null; state.selPane=null; state.preview=s; detach(); render();
  head(s.name||'session', s.cwd||'no working directory', null);
  const btn=$('#resumeBtn'); btn.classList.remove('hidden'); btn.textContent=`Open ${s.agent} here →`; btn.onclick=()=>resumeSession(s);
  state.activeSession=null; state.events=(s.tail||[]).map((m,i)=>({id:i, stream:m.role==='assistant'?'stdout':'system', text:`${m.role==='assistant'?s.agent:'❯'}  ${m.text||''}`}));
  state.events.length ? renderLog() : terminalMessage('No transcript preview — “Open here” resumes it in a live pane.');
}
async function resumeSession(s){
  try{ const p=await post('/api/panes/start', {agent:s.agent, cwd:s.cwd||'', resume:s.session_id, title:`↻ ${s.name||s.agent}`, runner_id:s.runner_id||'', interactive:true});
    state.panes.push(p); selectPane(p); toast('Resumed in a live pane') }catch(err){ toast(err.message) }
}
function renderLog(){ detach(); $('#terminal').classList.add('hidden'); const v=$('#logview'); v.classList.remove('hidden'); $('#typeInput').disabled=$('#typeSend').disabled=true;
  v.innerHTML=state.events.length ? state.events.map(e=>`<span class="line ${esc(e.stream)}">${esc(e.text)}</span>`).join('\n') : ''; v.scrollTop=v.scrollHeight }
function terminalMessage(s){ detach(); $('#terminal').classList.add('hidden'); const v=$('#logview'); v.classList.remove('hidden'); v.innerHTML=`<div class="empty"><span>❯_</span><h2>${esc(s)}</h2></div>` }

// ---- realtime -----------------------------------------------------------------
function connect(){
  const proto=location.protocol==='https:'?'wss':'ws'; const ws=new WebSocket(`${proto}://${location.host}/ws/web`); state.ws=ws;
  ws.onopen=()=>{ $('#dot').className='online'; $('#connection').textContent='live'; if(state.attached){ const id=state.attached; state.attached=null; attach(id) } };
  ws.onclose=()=>{ $('#dot').className=''; $('#connection').textContent='reconnecting'; setTimeout(connect,1200) };
  ws.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='pane.data'){ if(m.pane_id===state.attached && term){ if(m.replay) term.reset(); term.write(b64.dec(m.data)) } return }
    if(m.type==='pane.updated'){ const i=state.panes.findIndex(p=>p.id===m.pane.id); i<0?state.panes.push(m.pane):state.panes[i]=m.pane; render(); updateTitle();
      if(m.pane.id===state.selPane){ const card=state.cards.find(c=>c.id===m.pane.card_id); head(card?card.title:m.pane.title, `${m.pane.cwd||''}${m.pane.runner_name?'  ·  '+m.pane.runner_name:''}  ·  ${m.pane.id}`, m.pane); $('#typeInput').disabled=$('#typeSend').disabled=!m.pane.alive } return }
    if(m.type==='panes.updated') return loadPanes();
    if(m.type==='agent.blocked') return notifyBlocked(m.pane);
    if(m.type==='pane.gone'){ toast('That agent is gone'); return loadPanes() }
    if(m.type==='card.created'){ if(!state.cards.find(c=>c.id===m.card.id)) state.cards.push(m.card) }
    if(m.type==='card.updated'){ const i=state.cards.findIndex(c=>c.id===m.card.id); i<0?state.cards.push(m.card):state.cards[i]=m.card }
    if(m.type==='card.deleted') state.cards=state.cards.filter(c=>c.id!==m.card_id);
    if(m.type.startsWith('card.')) render();
    if(m.type==='session.event' && m.session_id===state.activeSession){ state.events.push(m.event); renderLog() }
    if(m.type==='session.created' && m.session.card_id===state.selected && !state.selPane) setTimeout(()=>{ const c=state.cards.find(c=>c.id===state.selected); if(c) selectCard(c) }, 600);
    if(m.type==='runner.updated') loadRunners();
    if(m.type==='agent.sessions.updated') loadExternal();
  };
}
function notifyBlocked(p){
  const card=state.cards.find(c=>c.id===p.card_id); const title=card?card.title:p.title;
  toast(`◆ ${p.agent} needs you: ${title}`);
  if('Notification' in window && Notification.permission==='granted'){ const n=new Notification('Agent needs you', {body:`${p.agent}: ${title}`, tag:p.id}); n.onclick=()=>{ window.focus(); selectPane(p) } }
}

// ---- input ----------------------------------------------------------------------
$('#keys').onclick=e=>{ const b=e.target.closest('button'); if(!b||!state.attached) return; send({type:'pane.input', pane_id:state.attached, keys:JSON.parse(b.dataset.keys)}); term?.focus() };
$('#typebar').onsubmit=e=>{ e.preventDefault(); const t=$('#typeInput').value; if(!state.attached) return; send({type:'pane.input', pane_id:state.attached, text:t, enter:true}); $('#typeInput').value='' };

// ---- composer -------------------------------------------------------------------
function openComposer(){ const d=$('#composer'); $('#taskCwd').value=state.board?.repo_path||''; fillAgentSelect(); syncComposer(); d.showModal(); setTimeout(()=>$('#taskTitle').focus(),50);
  if('Notification' in window && Notification.permission==='default') Notification.requestPermission().catch(()=>{}) }
function closeComposer(){ $('#composer').close(); $('#taskForm').reset(); $('#interactive').checked=true; $('#taskCwd').value=state.board?.repo_path||''; $('#autoRow').classList.add('hidden'); syncComposer() }
function syncComposer(){ const live=$('#interactive').checked; const a=state.agents.find(x=>x.name===$('#taskAgent').value);
  $('#planRow').classList.toggle('dim', live); if(live){ $('#planMode').checked=false; $('#planAuto').checked=false; $('#autoRow').classList.add('hidden') }
  if(a && !a.interactive && live){ $('#interactive').checked=false; $('#planRow').classList.remove('dim') } }
$('#composeBtn').onclick=openComposer; $('#closeComposer').onclick=closeComposer; $('#cancelComposer').onclick=closeComposer;
$('#interactive').onchange=syncComposer; $('#taskAgent').onchange=syncComposer;
$('#planMode').onchange=e=>{ if(e.target.checked) $('#interactive').checked=false; $('#autoRow').classList.toggle('hidden', !e.target.checked); if(!e.target.checked) $('#planAuto').checked=false; syncComposer() };
$('#taskForm').onsubmit=async e=>{
  e.preventDefault(); const title=$('#taskTitle').value.trim(), prompt=$('#taskPrompt').value.trim(); if(!title) return;
  try{
    const c=await post(`/api/boards/${state.board.id}/cards`, {title, prompt, cwd:$('#taskCwd').value.trim(), agent:$('#taskAgent').value||'claude',
      plan_mode:$('#planMode').checked, plan_auto:$('#planAuto').checked, isolate:$('#isolate').checked, interactive:$('#interactive').checked});
    await post(`/api/cards/${c.id}/run`); closeComposer(); state.selected=c.id; state.selPane=null; render();
    head(title, 'starting…', null); terminalMessage(state.runnerOnline ? 'Starting the agent…' : 'Queued — no runner yet. Run “kanbot up”.');
    toast(state.runnerOnline ? 'Agent starting' : 'Queued until a runner connects');
  }catch(err){ toast(err.message) }
};
document.addEventListener('keydown', e=>{
  if((e.metaKey||e.ctrlKey) && e.key==='Enter' && $('#composer').open) return $('#taskForm').requestSubmit();
  const typing=['INPUT','TEXTAREA'].includes(document.activeElement.tagName) || document.activeElement.classList.contains('xterm-helper-textarea');
  if(typing || $('#composer').open) return;
  if(e.key==='n') openComposer();
  if(e.key==='j'||e.key==='k'){ const live=state.panes.filter(p=>p.alive); if(!live.length) return; let i=live.findIndex(p=>p.id===state.selPane); i=(i+(e.key==='j'?1:-1)+live.length)%live.length; selectPane(live[i]) }
});
setInterval(()=>{ if(!$('#composer').open) render() }, 15000);   // ages tick
boot().catch(e=>{ terminalMessage(e.message); toast(e.message) });
