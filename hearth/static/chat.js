// Hearth chat controller. Owns conversation state, renders messages, streams a
// turn via SSE-over-POST, and handles gated actions (exec approval / ask_user).
//
// Sidebar layout: an icon rail switches between panels (chats / files / prompt /
// skills / settings) — see NAV below. The composer is deliberately minimal: no
// agent-mode or force-respond buttons (tools are always available; gated tools
// still ask per call), only Retry next to it.

const S = {
  convId: null, busy: false, attachments: [], artScope: 'conv',
  abortCtrl: null,            // AbortController for the in-flight stream
  lastUserText: '',           // for Retry when there is nothing to retry against
  activityEl: null,           // live "doing X" chip while a turn runs
};
const $ = (id) => document.getElementById(id);
const msgsEl = $('messages'), dbgEl = $('debugPanel');

function dbg(t){ const d=$('dbgToggle'); if(!d || !d.checked) return; dbgEl.classList.remove('hidden'); dbgEl.textContent += t + "\n"; dbgEl.scrollTop = dbgEl.scrollHeight; }

function addMsg(role, html, ts){
  const wrap = document.createElement('div');
  const isUser = role === 'user';
  wrap.className = isUser ? 'flex justify-end' : 'flex justify-start';
  // Server clock when available (history rows), browser clock otherwise (live).
  wrap.setAttribute('data-ts', String(ts || Math.floor(Date.now()/1000)));
  const bubble = document.createElement('div');
  bubble.className = (isUser ? 'bg-sky-700/60' : 'bg-slate-800') + ' max-w-[85%] rounded-2xl px-4 py-2 text-sm leading-relaxed';
  bubble.innerHTML = html;
  wrap.appendChild(bubble);
  if(PREFS.show_timestamps) stampTime(bubble, parseInt(wrap.getAttribute('data-ts'),10));
  msgsEl.appendChild(wrap); scrollToBottom();
  return bubble;
}

// A finished reasoning block from history (or a live one is built inline in handleEvent).
// Same shape as the live block so a reload does not restyle the transcript. Content goes in
// as textContent, never innerHTML: reasoning is model output and can contain markup.
// The wrap must carry `text-[12px]` exactly like the live block (handleEvent 'reasoning'):
// the vendored Tailwind build only ships utilities it has seen at least once, so a class
// present in just one of the two builders is styled on the page that generated it and falls
// back to the inherited 14px bubble text on the other — which is why reasoning blobs grew
// after returning from Admin (or after any re-render). Keep both builders' classes in sync.
// Both builders open the block (`d.open = true`): thinking is expanded by default and the
// user collapses what they do not want to see. Height cap lives in base.css.
function addReasoningBlock(text, ts){
  const wrap = document.createElement('div');
  wrap.className = 'flex justify-start w-full text-[12px]';
  if(ts) wrap.setAttribute('data-ts', String(ts));
  const d = document.createElement('details');
  d.open = true;   // expanded by default — the thinking is worth reading, click to tidy
  d.className = 'mb-1 rounded-lg border border-slate-800 bg-slate-900/40';
  const sum = document.createElement('summary');
  sum.className = 'cursor-pointer px-2 py-1 text-slate-500 select-none';
  sum.textContent = I18N.t('chat.reasoning','Reasoning');
  const body = document.createElement('div');
  // `bubble-body` caps the height at ~10 lines (base.css) with an inner scrollbar:
  // long thinking scrolls inside the block instead of pushing the answer away.
  body.className = 'bubble-body px-3 pb-2 pt-1 text-slate-400 italic whitespace-pre-wrap break-words';
  body.textContent = text || '';
  d.appendChild(sum); d.appendChild(body); wrap.appendChild(d);
  msgsEl.appendChild(wrap);
  return body;
}

// A finished audio caption from history — the persisted twin of the live caption_chunk
// block (DEC-061). Same indigo styling as the streaming one so a reload does not restyle
// the transcript, but collapsed: on a long chat, opening every past description at once
// would push the conversation away. The summary says whether it succeeded, because the
// honest distinction between "no description" and "description failed" is the part users
// could not see before.
function addCaptionBlockStatic(text, info, ts){
  const wrap = document.createElement('div');
  wrap.className = 'flex justify-start w-full text-[12px]';
  if(ts) wrap.setAttribute('data-ts', String(ts));
  const d = document.createElement('details');
  d.className = 'mb-1 rounded-lg border border-indigo-900/60 bg-slate-900/40';
  const sum = document.createElement('summary');
  sum.className = 'cursor-pointer px-2 py-1 text-indigo-300/80 select-none';
  const T = (k, fb)=> (window.I18N && I18N.t(k)) || fb;
  const ok = info && info.ok !== false;
  sum.textContent = `🎧 ${T('chat.caption_streaming','Audio model')}` +
    (info && info.name ? ` · ${info.name}` : '') +
    (ok ? '' : ` · ${T('chat.caption_failed_short','could not describe')}`);
  const body = document.createElement('div');
  // `bubble-body` replaces the old inline max-h-[60vh]: one cap for every transcript
  // block, defined once in base.css (≈10 lines, scrolls inside, never clipped).
  body.className = 'bubble-body px-3 pb-2 pt-1 text-slate-400 italic whitespace-pre-wrap break-words';
  // textContent, never innerHTML: a caption is model output and can contain markup.
  body.textContent = text || '';
  d.appendChild(sum); d.appendChild(body); wrap.appendChild(d);
  msgsEl.appendChild(wrap);
  return body;
}

// ---- Auto-scroll (pref: auto_scroll) ----------------------------------------
// The transcript follows output while streaming when the pref is on. When it's
// off — or the user has scrolled up to read something — nothing yanks them back
// down mid-turn (they were reading, not watching). scrollToBottom(force=true)
// overrides for moments where following IS the point: a new message you sent,
// or opening a conversation.
function nearBottom(){
  return msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 80;
}
function scrollToBottom(force){
  if(force || (PREFS.auto_scroll && nearBottom())) msgsEl.scrollTop = msgsEl.scrollHeight;
}

// ---- Live activity ("what is it doing right now") -------------------------
// The model can spend a long time in tools before it says anything (writing a
// file, zipping, searching). Without feedback that looks like a hang, so every
// tool_call renders a chip that stays visible while the tool runs and is then
// completed in place by the matching tool_result.
// `force=true` shows the line even when idle: abort feedback must appear whether
// or not a turn is technically still flagged busy (the local safety-net timer).
function activity(msg, force){
  if(!S.busy && !force) return;
  if(!S.activityEl){
    const el = document.createElement('div');
    el.className = 'text-[11px] font-mono text-slate-400 max-w-[85%] animate-pulse';
    msgsEl.appendChild(el); S.activityEl = el;
  }
  S.activityEl.textContent = msg;
  scrollToBottom();
}

function clearActivity(){
  if(!S.activityEl) return;
  S.activityEl.classList.remove('animate-pulse');
  S.activityEl = null;
}

// One tool result in the transcript. The whole bubble is a <details>: the header
// (tool name + ✓/✗) is the collapse affordance, and the body is capped at ~10 lines
// by base.css (`.bubble-body`) and scrolls inside — so nothing is ever cut short, but
// a long file read cannot dominate the transcript either. Expanded by default; click
// the header to tidy it away. Height lives in CSS, not Tailwind utilities — see the
// note in base.css about the JIT build and first paint.
function toolChip(name, ok, preview, live){
  const el = document.createElement('details');
  el.open = true;
  el.className = 'tool-chip text-[11px] font-mono bg-slate-900 border border-slate-700 rounded-lg max-w-[85%]';
  // The body used to be `max-h-32` — a ~5-line window on text the user needed to read,
  // which is what made results look "cut off despite the scrollbar" (DEC-061). It now
  // grows with the content and only scrolls past the shared cap.
  el.innerHTML = `<summary class="cursor-pointer select-none">` +
    `<span class="text-slate-400">${esc(labelTool())}:</span> <b class="text-slate-200">${esc(name)}</b> ` +
    `<span class="${ok?'text-emerald-400':'text-red-400'}">${ok?'✓':'✗'}</span></summary>` +
    `<div class="bubble-body border-t border-slate-700/60">` +
    `<pre class="text-slate-300 whitespace-pre-wrap">${esc(preview||'')}</pre></div>`;
  msgsEl.appendChild(el); scrollToBottom();
  return el;
}

// The "tool:" prefix in a chip header. Kept as a function so the label is resolved
// at render time (I18N loads per page, and switching language re-renders the transcript).
function labelTool(){ return (window.I18N && I18N.t('chat.tool')) || 'tool'; }

// ---- Web sources (search results / fetched pages) --------------------------
// The web tools return structured meta: search -> meta.web.results[{title,url,
// snippet}], fetch -> meta.web.url (+links). Rendering them as clickable links
// (not just model-facing text in the chip preview) is what lets a family member
// actually CHECK a source behind an answer. CSP-safe: anchors are navigations,
// not subresource loads; rel=noopener noreferrer keeps the target page from
// touching this window or learning the referrer.
function hostOf(u){ try { return new URL(u).hostname.replace(/^www\./,''); } catch(e){ return u; } }
function safeHref(u){ return (typeof u === 'string' && /^https?:\/\//i.test(u)) ? u : null; }

function webSourcesHtml(web){
  if(!web) return '';
  const T = (k, fb)=> (window.I18N && I18N.t(k)) || fb;
  const badge = web.backend ? `<span class="text-slate-500"> · ${esc(T('chat.web_mode','via'))} ${esc(web.backend)}</span>` : '';
  if(Array.isArray(web.results) && web.results.length){
    const q = web.query ? ` “${esc(web.query)}”` : '';
    const items = web.results.slice(0,8).map(r=>{
      const href = safeHref(r.url); if(!href) return '';
      return `<li class="leading-snug"><a href="${esc(href)}" target="_blank" rel="noopener noreferrer" ` +
        `class="text-sky-400 hover:underline break-all">${esc(r.title || hostOf(r.url))}</a>` +
        `<span class="text-slate-500"> · ${esc(hostOf(r.url))}</span>` +
        (r.snippet ? `<div class="text-slate-400 pl-1 mt-0.5 web-src-snippet">${esc(r.snippet)}</div>` : '') + `</li>`;
    }).join('');
    if(!items) return '';
    return `<div class="mt-1 text-[11px] font-sans"><span class="text-slate-400">🔎 ${esc(T('chat.web_sources','Sources'))}${q}</span>${badge}` +
      `<ul class="list-disc pl-4 mt-1 space-y-1">${items}</ul></div>`;
  }
  if(web.url){
    const href = safeHref(web.url); if(!href) return '';
    let links = '';
    if(Array.isArray(web.links) && web.links.length){
      const items = web.links.slice(0,6).map(l=>{
        const h = safeHref(l.url); if(!h) return '';
        return `<li class="truncate"><a href="${esc(h)}" target="_blank" rel="noopener noreferrer" ` +
          `class="text-sky-400/80 hover:underline">${esc(l.text || hostOf(l.url))}</a></li>`;
      }).join('');
      if(items) links = `<ul class="list-disc pl-4 mt-1 space-y-0.5">${items}</ul>`;
    }
    return `<div class="mt-1 text-[11px] font-sans"><span class="text-slate-400">🌐 ` +
      `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer" class="text-sky-400 hover:underline">${esc(T('chat.web_read','read page'))}: ${esc(hostOf(web.url))}</a>${badge}</span>${links}</div>`;
  }
  return '';
}

// Attach a rendered web-sources block to an existing chip/bubble element. The list has
// no max-height of its own: the chip body is the single scroll window (base.css), and a
// nested scroller inside a scroller is how content used to look "cut off".
function attachWebSources(el, web){
  const html = webSourcesHtml(web);
  if(!html || !el) return;
  const div = document.createElement('div');
  div.innerHTML = html;
  el.appendChild(div.firstChild || div);
  scrollToBottom();
}

// A tool call that is running (or was interrupted before its result arrived).
function pendingChip(name, summary){
  const el = document.createElement('div');
  el.dataset.tool = name;   // used to match this chip to its tool_result event
  el.className = 'chip-transient text-[11px] font-mono bg-slate-900/70 border border-sky-800 rounded-lg px-3 py-2 max-w-[85%]';
  el.innerHTML = `<span class="text-sky-400 animate-pulse">●</span> <b>${esc(name)}</b> ` +
    `<span class="text-slate-400">${esc(summary||'')}</span>`;
  msgsEl.appendChild(el); scrollToBottom();
  return el;
}

// Live progress while the model is still GENERATING a tool call (its arguments
// stream in token by token). Without this, composing a big fs_write takes tens of
// seconds with nothing but "thinking…" on screen — it looks like a hang. The chip
// shows how many bytes of the file have been composed so far, plus the path as
// soon as the model has streamed it. `state.prog` maps tool name -> chip state.
function fmtBytes(n){ if(n==null || n<0) return ''; if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(1)+' MB'; }

// The byte counter must visibly MOVE. llama-server sends a hint early (first ~32 B)
// and then only every ~256 B of new arguments, so a small file can still arrive as
// just a couple of updates. We therefore also ease towards an estimated target size —
// the largest tool call seen so far in this page session, ratcheting up when a bigger
// one shows up and decaying back after a quiet period so one huge zip can't pin
// every later chip at "almost done". It is an estimate: it only smooths the bar and
// never claims anything factual about the file being written.
const GROW = { est: 6 * 1024, lastChars: -1, t0: 0, lastAt: 0 };

function growEstimate(chars){
  const now = performance.now();
  // Idle > 30 s: forget a target that was inflated by some earlier huge call.
  if(GROW.lastAt && now - GROW.lastAt > 30_000 && GROW.est > 6 * 1024){
    GROW.est = Math.max(6 * 1024, GROW.est * 0.5);
  }
  const freshCall = GROW.lastChars < 0 || chars <= GROW.lastChars;
  if(chars > GROW.est) GROW.est = chars;         // never shrink mid-call: no running backwards
  if(freshCall || !GROW.t0) GROW.t0 = now;       // first sample of a call restarts the easing
  GROW.lastChars = chars;
  GROW.lastAt = now;

  const shown = Math.min(GROW.est, chars);
  const gap = GROW.est - shown;
  if(gap <= 0) return { pct: 1, shown };
  // Ease towards the target over ~6 s so it always looks alive but never claims to
  // be finished; the extra term guarantees visible movement even if no further hint
  // ever arrives for this call.
  const ease = Math.min(1, (now - GROW.t0) / 6000);
  const v = shown + gap * ease * 0.92 + Math.min(gap * 0.04, 96);
  return { pct: Math.max(0.03, Math.min(0.97, v / GROW.est)), shown };
}

function toolProgress(ev, state){
  if(!ev.name) return;
  const T = (k, fb)=> (window.I18N && I18N.t(k)) || fb;
  state.prog = state.prog || {};
  // Keyed by name + agent-loop round: the same tool can be re-composed in a later
  // round of one turn, and those are different pieces of work (a chip retired by a
  // previous round must never be resurrected or shared).
  const key = ev.name + '|' + (ev.round || 'r0');
  let p = state.prog[key];
  if(p && p.done){ delete state.prog[key]; return; }
  const isWrite = ev.name === 'fs_write' || ev.name === 'fs_append';
  let label = T('chat.generating','generating');
  if(isWrite) label = T('chat.writing','writing');
  else if(ev.name === 'exec') label = T('chat.composing_cmd','composing command');
  // The server sends the path as soon as its JSON value has fully streamed in.
  const path = ev.path_hint || '';
  // Show the RAW byte count — never a smoothed number, or the counter can read as if it
  // overshot the finished file. `g.pct` (an estimate) only drives the bar's motion.
  const g = growEstimate(ev.chars);
  const sizeTxt = fmtBytes(ev.chars) + (g.pct < 0.97 ? ' · ~' + Math.round(g.pct*100) + '%' : '');
  const summary = `${label} ${path ? esc(path) + ' ·' : ''} ${sizeTxt}`;
  if(!p){
    const el = document.createElement('div');
    el.dataset.tool = ev.name;
    el.className = 'chip-transient text-[11px] font-mono bg-slate-900/70 border border-sky-800 rounded-lg px-3 py-2 max-w-[85%]';
    const span = document.createElement('span');
    span.className = 'text-slate-400';
    el.innerHTML = `<span class="text-sky-400 animate-pulse">✎</span> <b>${esc(ev.name)}</b> `;
    el.appendChild(span);
    const bar = document.createElement('div');
    bar.className = 'mt-1 h-1 rounded bg-slate-700 overflow-hidden';
    bar.innerHTML = '<div class="h-full bg-sky-500" style="width:3%"></div>';
    el.appendChild(bar);
    p = {el, span, fill: bar.firstChild, lastChars: -1};
    state.prog[key] = p;
    msgsEl.appendChild(el);
  }
  // Repaint whenever the buffer grew OR the eased estimate moved on (so a stalled
  // hint stream still shows visible movement).
  const pctNow = Math.round(g.pct * 100);
  if(ev.chars > p.lastChars || pctNow !== p.lastPct){
    p.lastChars = ev.chars; p.lastPct = pctNow;
    p.span.textContent = summary;
    p.fill.style.width = (g.pct*100).toFixed(1) + '%';
    scrollToBottom();
  }
}

// Retire progress chips. With a `round`, only that agent-loop round's chips go away —
// the same tool may legitimately be composing again in the next round. Without one,
// every chip for that tool name is retired (used when a result/abort lands and the
// round bookkeeping may not match, e.g. a resumed gated call).
function finishProgress(name, state, round){
  const prog = state.prog || {};
  const suffix = round ? '|' + round : null;
  for(const k of Object.keys(prog)){
    const p = prog[k];
    const mine = suffix ? (k.endsWith(suffix) && k.slice(0, -suffix.length) === name)
                        : (k.split('|')[0] === name);
    if(!mine) continue;
    p.done = true; p.el.remove(); delete prog[k];
  }
}

// ---- Conversation list rows + per-row action menu -------------------------
// Each conversation row is a button carrying data-cid. A "⋮" affordance (also
// opened by right-click / long-press) shows Rename/Delete for that specific
// conversation, so these actions work on any row — not just the active one —
// and no longer clutter the header. See openConvMenu / renameConversation.
// Note: the inner ⋮ is a <span role="button">, not a nested <button>, because
// nested interactive elements are invalid HTML (rows themselves are buttons).
function convRowHtml(title, cid){
  return `<div class="flex items-center gap-1">` +
    `<span class="flex-1 min-w-0">${title}</span>` +
    `<span role="button" tabindex="-1" data-menu="${esc(cid)}" title="…" class="shrink-0 rounded px-1 text-slate-500 hover:text-white opacity-40 hover:opacity-100">⋮</span>` +
    `</div>`;
}

function convRowBaseClass(active){
  return 'w-full text-left text-sm px-3 py-1.5 rounded-lg hover:bg-slate-800' + (active?' bg-slate-800 text-sky-300':'');
}

async function loadConversations(){
  const r = await api('/api/chat/conversations'); const list = await r.json();
  S.convTitles = {};
  for(const c of list) S.convTitles[c.id] = c.title;
  setConvTitleLabel(S.convTitles[S.convId]);
  const nav = $('convList'); nav.innerHTML = '';
  for (const c of list){
    const a = document.createElement('button');
    const active = c.id === S.convId;
    a.setAttribute('data-cid', c.id);
    a.className = convRowBaseClass(active);
    a.innerHTML = convRowHtml(esc(c.title), c.id);
    a.onclick = (e) => { if(!e.target.closest('[data-menu]')){ openConversation(c.id); closeSidebar(); } };
    nav.appendChild(a);
  }
}

async function openConversation(id){
  S.convId = id; msgsEl.innerHTML = ''; clearActivity();
  S.lastUserText = '';
  setConvTitleLabel((S.convTitles || {})[id]);
  scrollToBottom(true); refreshDeviceHint(); loadSteerQueue();
  const r = await api(`/api/chat/conversations/${id}`); const hist = await r.json();  // History rows carry no event meta (a tool result's download link is not stored), so
  // downloads are re-resolved from the artifact store by filename: one ⬇ per file, and
  // only for `upload`/`zip_pack` rows — the live-stream policy of DEC-045. Scoped to this
  // conversation; if the file moved to another chat since, the Artifacts panel still has it.
  let arts = [];
  try { arts = await (await api(`/api/artifacts?conversation_id=${S.convId||''}`)).json(); } catch(e){ arts = []; }
  const byName = {};
  for(const a of arts){ if(!byName[a.filename]) byName[a.filename] = a; }
  const shownDl = new Set();
  for (const m of hist){
    if (m.role === 'user') {
      addMsg('user', mdLite(m.content), m.created_at);
      // A caption produced for this message's recording, restored after a refresh
      // (DEC-061). It used to vanish entirely — the description lived only in an
      // in-process cache and one transient SSE block. Rendered right after the user's
      // bubble so it sits where it appeared live, collapsed by default (a reload of a
      // long chat should not open every past caption at once).
      const caps = m.captions || {};
      for(const rel of Object.keys(caps)){
        const info = caps[rel] || {};
        let text = '';
        try { const mt = m.meta ? JSON.parse(m.meta) : null;
              const st = mt && mt.captions && mt.captions[rel];
              text = (st && st.text) || ''; } catch(e){}
        if(text) addCaptionBlockStatic(text, info, m.created_at);
      }
      S.lastUserText = m.content; continue;
    }
    if (m.role === 'assistant'){
      // Stored reasoning (meta.reasoning) renders as the same collapsed block the live stream
      // uses, ABOVE the answer. The server already applied the retention policy (Admin →
      // Generation → Reasoning retention), so a missing key means "policy hid it", not "the
      // model never thought" — either way there is nothing to render.
      let rs = null;
      try { const mt = m.meta ? JSON.parse(m.meta) : null; rs = mt && mt.reasoning; } catch(e){}
      if (rs) addReasoningBlock(rs, m.created_at);
      if (m.content) addMsg('assistant', mdLite(m.content), m.created_at); continue; }
    if (m.role !== 'tool') continue;
    let metaObj = {};
    try { metaObj = JSON.parse(m.meta) || {}; } catch(e){}
    const nm = metaObj.tool || 'tool';
    const chip = toolChip(nm, m.ok !== false, m.content);
    // Web tools persist their structured sources in meta.web — re-render the
    // clickable links here so sources stay checkable after a page reload.
    if(metaObj.web) attachWebSources(chip, metaObj.web);
    // Only file-offering tools draw a button; fs_write rows never do.
    if((nm === 'upload' || nm === 'zip_pack') && m.ok !== false){
      const mm = /(?:ready for download|packed .*? into)\s+(\S+)/i.exec(m.content || '');
      const name = mm && mm[1];
      const art = name && byName[name];
      if(art && !shownDl.has(name)){ shownDl.add(name); addDownload({url: art.download_url, name}); }
    }
  }
  loadArtifacts(); loadContextUsage();
}

// Move the active highlight to the currently-selected conversation without a
// full re-fetch. Each conv button carries data-cid (see loadConversations/runSearch).
function highlightActive(){
  document.querySelectorAll('#convList [data-cid]').forEach(el=>{
    const on = el.getAttribute('data-cid') === S.convId;
    el.classList.toggle('bg-slate-800', on);
    el.classList.toggle('text-sky-300', on);
  });
}

async function newConversation(){
  const r = await api('/api/chat/conversations', {method:'POST'}); const d = await r.json();
  await loadConversations(); await openConversation(d.id);
  setConvTitleLabel((S.convTitles || {})[d.id]);   // "New chat" until first message titles it
}

function setBusy(b){
  S.busy = b;
  const send = $('sendBtn'), abort = $('abortBtn');
  if(send){
    send.disabled = b;
    send.textContent = b ? '…' : ((window.I18N && I18N.t('chat.send')) || 'Send');
    send.classList.toggle('opacity-40', b);
  }
  if(abort){
    abort.classList.toggle('hidden', !b);
    abort.disabled = false;
    abort.textContent = (window.I18N && I18N.t('chat.abort')) || 'Stop';
  }
  // Retry is meaningless (and racy) mid-turn.
  const rb = $('retryBtn'); if(rb) rb.disabled = b;
  if(!b) clearActivity();
}

// Abort the running turn: tell the server (so it stops generating and releases
// the conversation) AND drop the HTTP stream locally so the UI reacts at once.
async function abortTurn(){
  const conv = S.convId;
  const btn = $('abortBtn');
  if(btn){ btn.disabled = true; btn.textContent = I18N.t('chat.aborting','Stopping…'); }
  activity(I18N.t('chat.aborting','Stopping…'), true);
  try { await api('/api/chat/abort', {method:'POST', body: JSON.stringify({conversation_id: conv})}); }
  catch(e){ /* server may already be gone — the local abort below still applies */ }
  if(S.abortCtrl){ try { S.abortCtrl.abort(); } catch(e){} }
  // Safety net: if no `aborted`/`done` event arrives (dead connection, server busy),
  // re-enable Send ourselves rather than leaving the composer stuck on "…".
  setTimeout(()=>{ if(S.busy) finishTurnLocally(); }, 2500);
}

// Bring the composer back to idle without a server round-trip.
function finishTurnLocally(){
  clearActivity(); setBusy(false);
}

async function retryLastTurn(){
  if(!S.convId || S.busy) return;
  setBusy(true);
  const state = {};
  activity(I18N.t('chat.retrying','retrying…'));
  S.abortCtrl = new AbortController();
  await streamPost('/api/chat/retry', {conversation_id: S.convId},
                   (ev)=>handleEvent(ev, state), {signal: S.abortCtrl.signal, aborted: ()=>state.aborted});
  S.abortCtrl = null;
  loadConversations(); loadContextUsage();
  if(!state.awaiting) setBusy(false);
}

// ---- Context-window usage bar --------------------------------------------
function fmtTok(n){ if(n==null) return '—'; if(n>=1000000) return (n/1000000).toFixed(1)+'M'; if(n>=1000) return (n/1000).toFixed(n>=10000?0:1)+'k'; return String(Math.round(n)); }

// Runs on every conversation open, so the endpoint it calls must be side-effect
// free: /api/chat/context-usage used to resolve the provider (and thereby launch
// llama-server), which meant entering the Chat UI restarted a model that had just
// been stopped in Admin. It reads manager.probe() now — see DEC-048.
async function loadContextUsage(){
  let d = null;
  try { const r = await api(`/api/chat/context-usage?conversation_id=${S.convId||''}`); if(!r || !r.ok) return; d = await r.json(); }
  catch(e){ return; }
  // A brand-new conversation has no stored usage yet, so the server's chars/4 estimate
  // (message text only — no system prompt, workspace listing, or tool traffic) is far
  // BELOW what this turn already measured. Never let a fresh page load erase a real
  // reading; keep the larger figure and label it with whichever source won.
  const floor = S.ctxFloor && S.ctxFloor.conv === (S.convId||'') ? S.ctxFloor.val : 0;
  if(floor && (d.prompt_tokens || 0) < floor){
    d.prompt_tokens = floor;
    if(d.source !== 'usage') d.source = 'estimate';
  }
  renderCtx({used: d.prompt_tokens, n_ctx: d.n_ctx, tokens_per_sec: d.tokens_per_sec,
             source: d.source, warning: d.warning});
}

// Human speed string for a usage payload. Prefers llama.cpp's measured rates
// (decode_tps / prefill_tps, present only when the server reported them) and falls back
// to Hearth's wall-clock tokens_per_sec marked with '~'. One function so the badge, the
// context-bar tooltip and the debug line can never disagree about what a number means.
function perfSpeedText(d){
  if(!d) return '';
  const parts = [];
  if(d.decode_tps) parts.push(`${I18N.t('chat.decode','decode')} ${Number(d.decode_tps).toFixed(1)} t/s`);
  if(d.prefill_tps) parts.push(`${I18N.t('chat.prefill', 'prefill')} ${Math.round(d.prefill_tps)} t/s`);
  if(parts.length) return parts.join(' · ');
  return d.tokens_per_sec ? `~${d.tokens_per_sec} tok/s` : '';
}

// Paint the header context bar. Shared by loadContextUsage() (server truth) and
// the live `usage` event handler — without that second path the bar only ever
// showed what was stored BEFORE the current turn, i.e. nothing at all on a fresh
// chat, which read as "always 0".
function renderCtx(d){
  const bar = $('ctxBar'), label = $('ctxLabel');
  if(!bar || !label) return;
  const nctx = d.n_ctx || 0;
  let used = d.used || 0;
  // High-water mark per conversation. Context only ever grows within a chat (nothing
  // auto-compacts yet), so a reading that drops means it came from a weaker source —
  // e.g. an estimated round after a measured one. Holding the peak stops the bar
  // flickering down mid-turn, which previously read as "the budget never updates".
  const key = S.convId || '';
  if(!S.ctxFloor || S.ctxFloor.conv !== key) S.ctxFloor = {conv: key, val: 0};
  if(used > S.ctxFloor.val) S.ctxFloor.val = used; else if(S.ctxFloor.val) used = S.ctxFloor.val;
  const pct = nctx ? Math.min(1, used/nctx) : 0;
  bar.style.width = Math.max(pct>0?1:0, pct*100).toFixed(1)+'%';
  bar.className = 'block h-full ' + (pct>=0.9 ? 'bg-red-500' : pct>=0.7 ? 'bg-amber-400' : 'bg-emerald-500');
  label.textContent = nctx ? `${fmtTok(used)} / ${fmtTok(nctx)}` : fmtTok(d.used);
  // Speed text: llama.cpp's OWN measured rates when the server reported them, our
  // wall-clock figure only as the fallback — and labelled as an estimate so a heuristic
  // never impersonates a measurement. perf.py parses, this only formats.
  const spd = perfSpeedText(d);
  $('ctxWrap').title = I18N.t('chat.ctx_title',
    'Tokens the model received on the last turn / configured window') + (spd ? ` · ${spd}` : '') +
    (d.source==='estimate' ? ` (${I18N.t('chat.ctx_est','estimated')})` : '') +
    (d.warning ? ` — ${I18N.t('chat.ctx_warn','Context is nearly full: start a new chat or shorten files.')}` : '');
  label.classList.toggle('text-red-400', !!d.warning);
}

// Keep the desktop + mobile model badges showing the same thing (the mobile one
// is just an icon whose title tooltip carries the detail).
function setModelBadge(text, title){
  for(const id of ['modelBadge','modelBadgeShort']){
    const el = $(id); if(el){ el.textContent = text; if(title) el.title = title; }
  }
}

// Initial model badge from /healthz — a read-only liveness probe. It reports only
// whether a llama-server answers right now, never which backend chats use: the
// old version called status(), which resolved the configured provider and thereby
// launched llama-server on every chat page load (DEC-048). A passive page view
// must not start a model; if nothing is up, say so and let Admin → Model start it.
async function renderModelBadge(){
  const label = I18N.t('chat.model') + ': ';
  let s = null;
  try{ s = await (await api('/healthz')).json(); }catch(e){}
  if(s && s.model && s.model.server_reachable)
    setModelBadge(label + 'llama-server', I18N.t('chat.model_up_title', 'a llama-server is answering on the configured port'));
  else
    setModelBadge(label + I18N.t('chat.model_none', 'not running'),
                  I18N.t('chat.model_down_title', 'no llama-server is running — start it under Admin → Model'));
}

async function handleEvent(ev, state){
  switch(ev.type){
    case 'token':
      activity(null);   // real output started; the chip was only for the quiet gaps
      if(!state.bubble) state.bubble = addMsg('assistant','');
      state.text = (state.text || '') + ev.text; state.bubble.innerHTML = mdLite(state.text); scrollToBottom(); break;
    case 'reasoning': {
      // The model's thinking, in its own collapsible block ABOVE the answer bubble and never
      // merged into it. Rendered raw-escaped (no mdLite): reasoning text routinely contains
      // half-written code and angle brackets that a markdown pass would mangle or that could
      // inject markup. Created once per round and appended to, like the answer bubble.
      if(!state.reasoningEl){
        const wrap = document.createElement('div');
        // Same wrapper classes as addReasoningBlock() — see the note there about keeping
        // both in step so the vendored JIT build styles both identically.
        wrap.className = 'flex justify-start w-full text-[12px]';
        wrap.innerHTML = `<details open class="mb-1 rounded-lg border border-slate-800 bg-slate-900/40">
          <summary class="cursor-pointer px-2 py-1 text-slate-500 select-none">${esc(I18N.t('chat.reasoning','Reasoning'))}</summary>
          <div class="reasoning-body bubble-body px-3 pb-2 pt-1 text-slate-400 italic whitespace-pre-wrap break-words"></div></details>`;
        msgsEl.appendChild(wrap);
        state.reasoningEl = wrap.querySelector('.reasoning-body');
      }
      state.reasoningText = (state.reasoningText || '') + ev.text;
      state.reasoningEl.textContent = state.reasoningText;
      scrollToBottom(); break; }
    case 'progress': {
      // Live prefill/decode telemetry from llama.cpp's own log lines (see perf.py +
      // manager.poll_log_perf). A `decode` event has no percentage — never draw a bar for
      // a number the server did not report.
      const isPrefill = (ev.data.phase||'') === 'prefill';
      if(!isPrefill){ dbg(`perf ${JSON.stringify(ev.data)}`); break; }
      $('progressWrap').classList.remove('hidden');
      const pct = Math.round((ev.data.pct||0)*100);
      $('progressBar').style.width = pct+'%';
      // ETA only when the server gave a rate AND enough of the prompt is measured — the
      // server omits it otherwise, and an invented one is worse than none.
      const eta = ev.data.eta ? ` · ≈${ev.data.eta} ${I18N.t('chat.left','left')}` : '';
      const rate = ev.data.rate_tps ? ` · ${Math.round(ev.data.rate_tps)} t/s` : '';
      $('progressLabel').textContent =
        `${I18N.t('chat.prefill','prefill')}… ${pct}%` +
        (ev.data.prompt_total ? ` (${fmtTok(ev.data.prompt_tokens)}/${fmtTok(ev.data.prompt_total)} tok)`
                              : (ev.data.prompt_tokens?` (${fmtTok(ev.data.prompt_tokens)} tok)`:'')) +
        rate + eta;
      if(ev.data.pct>=1) setTimeout(()=>$('progressWrap').classList.add('hidden'), 400);
      activity(`${I18N.t('chat.prefill','prefill')}… ${pct}%${eta}`);
      dbg(`progress ${JSON.stringify(ev.data)}`); break; }
    case 'usage': {
      // `~` marks an estimated figure (a tool round whose provider sent no usage) so a
      // heuristic never silently impersonates a measured number. Speed text prefers
      // llama.cpp's measured rates — see perfSpeedText().
      const spd = perfSpeedText(ev.data);
      setModelBadge(`model: ${spd || '?'} · in ${ev.data.prompt_tokens||'?'}${ev.data.estimated?' ~':''}`, JSON.stringify(ev.data));
      // Feed the context bar live: this is the first moment a brand-new chat has
      // any token count at all (the persisted value only lands when the turn ends).
      if(ev.data.prompt_tokens || ev.data.n_ctx){
        renderCtx({used: ev.data.prompt_tokens, n_ctx: ev.data.n_ctx,
                   tokens_per_sec: ev.data.tokens_per_sec, estimated: ev.data.estimated,
                   decode_tps: ev.data.decode_tps, prefill_tps: ev.data.prefill_tps});
      }
      dbg(`usage ${JSON.stringify(ev.data)}`); break; }
    case 'tool_progress': {
      // The model is still composing a tool call (args streaming in) — show the
      // growing write buffer so long fs_writes never look like a hang.
      toolProgress(ev, state); break; }
    case 'notice': {
      // Server-side progress that is not a tool call: describing an attached recording with
      // the captioner model takes real time (ffmpeg + listening), and without any signal it
      // is indistinguishable from a hang. The start/done pair is one activity line; the
      // streamed caption text (caption_chunk) grows its own collapsible block, like reasoning,
      // so the user can watch the audio model work rather than guess. Never a transcript
      // bubble per file — that would spam the chat for something that succeeded.
      //
      // Keyed PER FILE (state.captions), not one shared element: DEC-061 added a second
      // source of these events (the describe_audio TOOL, alongside attachment captioning), so
      // two recordings in one turn would otherwise interleave their text into a single block.
      if(ev.kind === 'caption_start') activity(`🎧 ${esc(ev.text || '')}`);
      else if(ev.kind === 'caption_chunk'){
        const key = ev.file || '_';
        state.captions = state.captions || {};
        let c = state.captions[key];
        if(!c){
          const wrap = document.createElement('div');
          // Same wrapper classes as addCaptionBlockStatic() — keep both in step.
          wrap.className = 'flex justify-start w-full text-[12px]';
          wrap.innerHTML = `<details open class="mb-1 rounded-lg border border-indigo-900/60 bg-slate-900/40">
            <summary class="cursor-pointer px-2 py-1 text-indigo-300/80 select-none">🎧 ${esc(I18N.t('chat.caption_streaming','Audio model'))}${ev.file ? ' · ' + esc(ev.file) : ''}</summary>
            <div class="caption-body bubble-body px-3 pb-2 pt-1 text-slate-400 italic whitespace-pre-wrap break-words"></div></details>`;
          msgsEl.appendChild(wrap);
          c = state.captions[key] = {el: wrap.querySelector('.caption-body'), text: ''};
        }
        // Server sends the (throttled) accumulated pieces as deltas, so append — same shape
        // as reasoning. textContent, never innerHTML: model output can contain markup.
        c.text += (ev.text || '');
        c.el.textContent = c.text;
        activity(`🎧 ${esc(I18N.t('chat.listening','listening to the audio…'))}`);
        scrollToBottom();
      }
      else if(ev.kind === 'caption_done'){
        // Collapse now that it is finished: the answer bubble carries the real result, and a
        // permanently-open transcript block would push the assistant's reply off screen.
        const key = ev.file || '_';
        const c = (state.captions || {})[key];
        if(c){ const d = c.el.closest('details'); if(d) d.open = false; }
        activity(null);
        dbg(`caption ${esc(ev.file||'')} ok=${ev.ok}`);
        if(!ev.ok) addMsg('assistant', `<span class="text-amber-400">${esc(I18N.t('chat.caption_failed','I could not listen to that file — see its note in the message.'))}</span>`);
      } else dbg(`notice ${JSON.stringify(ev)}`);
      break; }
    case 'tool_call': {
      // Show what it is doing, and keep showing it until the result lands —
      // a long fs_write/zip/exec must never look like a frozen UI.
      const step = ev.step && ev.of ? ` (${ev.step}/${ev.of})` : '';
      activity(`⚙ ${I18N.t('chat.tool')}: ${ev.name}${step} — ${ev.summary||''}`);
      // This round's live progress chip is superseded by the real one (only this
      // round's — an earlier round's chip was already retired by its own result).
      finishProgress(ev.name, state, ev.round);
      // Several calls can arrive before any result, so keep one chip per call and
      // match them back up by name (FIFO) when results stream in.
      state.pending = state.pending || [];
      state.pending.push(pendingChip(ev.name, (ev.summary||'') + step +
                                    (ev.awaiting_approval ? ` · ${I18N.t('chat.await_approval','awaiting approval')}` : '')));
      dbg(`tool_call ${ev.name} ${JSON.stringify(ev.args)}`); break; }
    case 'tool_result': {
      const chip = toolChip(ev.name, ev.ok, ev.preview);
      // Web tools: render clickable source links under the chip so the user can
      // verify what the model actually read (meta.web — see webSourcesHtml).
      if(ev.meta && ev.meta.web) attachWebSources(chip, ev.meta.web);
      const i = (state.pending || []).findIndex(p => p.dataset.tool === ev.name);
      if(i >= 0){ (state.pending.splice(i,1)[0]).remove(); }
      finishProgress(ev.name, state, ev.round);
      // Only file-offering tools draw a button: `upload` and `zip_pack`. fs_write /
      // fs_append / fs_edit save the file and register it in the Artifacts panel but
      // deliberately carry no meta.download, so writing something never stacks an
      // unrequested ⬇ into the transcript (operator policy, DEC-045 — reverses
      // DEC-040's "button per file"). The per-turn Set stays as a belt in case an
      // older server replays write metas.
      const dl = ev.meta && ev.meta.download;
      if(dl){
        const key = `${dl.artifact_id || ''}|${dl.name}`;
        state.downloads = state.downloads || new Set();
        if(!state.downloads.has(key)){
          state.downloads.add(key);
          addDownload(dl);
        }
      }
      loadArtifacts();
      // Keep the pulse alive only while another round can still run: saying
      // "thinking…" after the last step of a turn is a lie that reads as a hang.
      const more = ev.of && ev.step < ev.of;
      activity(more ? `⚙ ${ev.name} ${ev.ok?'✓':'✗'} — ${I18N.t('chat.thinking','thinking…')}` : null);
      break; }
    case 'action_required': renderActionRequired(ev); break;
    case 'aborted':
      state.aborted = true;
      clearPending(state);
      // Collapse any caption block that was mid-stream when Stop hit (partial, and the turn
      // is over, so leaving them expanded would push the "Turn aborted." line off screen).
      // All of them: DEC-061 means there can be more than one now.
      Object.values(state.captions || {}).forEach(c => {
        const d = c.el.closest('details'); if(d) d.open = false;
      });
      activity(null);
      addMsg('assistant', `<span class="text-amber-400">${esc(I18N.t('chat.aborted','Turn aborted.'))}</span>`);
      break;
    case 'error': activity(null); clearPending(state);
      addMsg('assistant', `<span class="text-red-400">${esc(ev.text)}</span>`); break;
    case 'steered':
      // A message the user sent WHILE this turn was running, now delivered at the
      // between-rounds seam. It becomes an ordinary user bubble at that point in the
      // transcript — which is exactly what it was: speech the model read before continuing.
      addMsg('user', mdLite(ev.text || ((ev.attachments||[]).join(', '))));
      loadSteerQueue();
      break;

    case 'done': if(ev.awaiting){ state.awaiting = true; } else { clearPending(state); } setBusy(false); loadSteerQueue(); break;
  }
}

// Drop any leftover "running" chips (turn ended, was aborted, or errored out).
function clearPending(state){
  (state.pending || []).forEach(el => el.remove());
  state.pending = [];
  if(state.prog) Object.values(state.prog).forEach(p => p.el.remove());
  state.prog = {};
}

function addDownload(dl){
  const el = document.createElement('a');
  el.href = dl.url; el.className='inline-flex items-center gap-2 text-sm bg-emerald-700/50 hover:bg-emerald-600/50 border border-emerald-600 rounded-lg px-3 py-1.5 max-w-[85%]';
  el.innerHTML = `⬇ ${esc(dl.name)}`; msgsEl.appendChild(el); scrollToBottom(true);
}

function renderActionRequired(ev){
  const T = (k, fb) => (window.I18N && I18N.t(k)) || fb;
  const card = document.createElement('div');
  card.className = 'bg-amber-900/30 border border-amber-600 rounded-xl p-4 max-w-[85%] text-sm';
  const promptText = ev.prompt || `Allow ${ev.kind}?`;
  let inner = `<div class="font-medium mb-2">${mdLite(promptText)}</div>`;
  if (ev.kind === 'ask_user' && ev.args && ev.args.choices && ev.args.choices.length){
    inner += '<div class="flex flex-wrap gap-2 mt-2">' + ev.args.choices.map((c,i)=>`<button data-choice="${esc(c)}" class="choice px-3 py-1 rounded-lg bg-slate-700 hover:bg-slate-600 text-xs">${esc(c)}</button>`).join('') + '</div>';
  }
  if (ev.kind === 'ask_user'){
    inner += `<div class="mt-2 flex gap-2"><input data-answer class="flex-1 rounded-lg bg-slate-800 border border-slate-700 px-3 py-1.5 text-sm" placeholder="${esc(T('chat.reply','Reply'))}…"/><button data-sendanswer class="rounded-lg bg-sky-600 px-3 py-1.5 text-sm">${esc(T('chat.reply','Reply'))}</button></div>`;
  } else {
    inner += `<div class="flex gap-2 mt-2"><button data-approve class="rounded-lg bg-emerald-600 px-4 py-1.5">${esc(T('chat.approve','Approve'))}</button><button data-deny class="rounded-lg bg-slate-700 px-4 py-1.5">${esc(T('chat.deny','Deny'))}</button></div>`;
  }
  card.innerHTML = inner; msgsEl.appendChild(card); scrollToBottom(true);

  const resolve = async (payload)=>{ card.remove(); await runResolve(Object.assign({pending_id: ev.pending_id}, payload)); };
  const ap = card.querySelector('[data-approve]'); if(ap) ap.onclick = ()=>resolve({approved:true});
  const dn = card.querySelector('[data-deny]'); if(dn) dn.onclick = ()=>resolve({approved:false});
  const sa = card.querySelector('[data-sendanswer]'); if(sa) sa.onclick = ()=>{ const v=card.querySelector('[data-answer]').value; resolve({answer:v}); };
  card.querySelectorAll('.choice').forEach(b=> b.onclick = ()=> resolve({answer: b.getAttribute('data-choice')}));
}

async function runResolve(payload){
  setBusy(true); const state = {};
  S.abortCtrl = new AbortController();
  await streamPost('/api/chat/resolve', payload, (ev)=>handleEvent(ev, state),
                   {signal: S.abortCtrl.signal, aborted: ()=>state.aborted});
  S.abortCtrl = null;
  loadContextUsage();
  if(!state.awaiting) setBusy(false);
}

// ---- Artifacts panel ------------------------------------------------------
function fmtSize(n){ if(n==null) return ''; if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(1)+' MB'; }

async function loadArtifacts(){
  const ul = $('artifactList'); if(!ul) return;
  // 'all' shows every artifact the user owns; 'conv' scopes to the current chat.
  const scopeAll = (S.artScope === 'all');
  const conv = (!scopeAll && S.convId) ? S.convId : '';
  let items = [];
  try { items = await (await api(`/api/artifacts${conv?`?conversation_id=${conv}`:''}`)).json(); } catch(e){ return; }
  ul.innerHTML = '';
  if(!items.length){
    const li = document.createElement('li'); li.className='text-[11px] text-slate-500';
    li.textContent = (window.I18N && I18N.t('chat.artifacts.empty')) || 'No files yet.'; ul.appendChild(li); return;
  }
  for(const a of items){
    const li = document.createElement('li'); li.className='flex items-center gap-2 text-sm';
    const kindIcon = a.kind==='uploaded' ? '⬆' : '📄';
    // No separate ⬇ button any more: the filename already downloads, and every
    // other action lives in the ⋮ menu (openFileMenu) — attach to next message,
    // preview, copy link, delete. Keeps the row quiet on narrow phones while
    // still exposing everything; long-press / right-click open the same menu.
    li.innerHTML = `<span class="opacity-60">${kindIcon}</span>` +
      `<a href="${a.download_url}" class="truncate flex-1 text-sky-400 hover:underline" download title="${esc(a.rel_path||a.filename)}" data-i18n-title="chat.art_open_dl">${esc(a.filename)}</a>` +
      `<span class="text-[10px] text-slate-500 shrink-0">${fmtSize(a.size)}</span>` +
      `<button type="button" data-art-menu="${esc(a.id)}" title="More options" data-i18n-title="chat.more_options" ` +
      `class="shrink-0 rounded px-1.5 py-0.5 text-slate-500 hover:text-white opacity-60 hover:opacity-100">⋮</button>`;
    ul.appendChild(li);
  }
}

// The artifact list is re-rendered wholesale by loadArtifacts(), so its click and
// long-press handlers are delegated once on the <ul> (see wiring below) rather than
// per row. findArtifact() recovers what the menu needs from the row itself — the
// panel already renders name + download URL, so nothing has to be cached at load time.
function findArtifact(id){
  const li = document.querySelector(`#artifactList [data-art-menu="${CSS.escape(id)}"]`);
  if(!li) return null;
  const row = li.closest('li'); if(!row) return null;
  const a = row.querySelector('a[href]');
  const sizeEl = row.querySelector('span.text-slate-500');
  return { id, name: a ? a.textContent.trim() : '', url: a ? a.getAttribute('href') : '',
           size: sizeEl ? sizeEl.textContent.trim() : '' };
}

// ---- Uploads (staged; sent with the next message) -------------------------
// Files are uploaded immediately to get an artifact_id, but held in S.attachments
// and only referenced by the message when the user hits Send. This prevents the
// model from reacting to each file separately.
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

async function stageFiles(fileList){
  const files = Array.from(fileList||[]); if(!files.length) return;
  if(!S.convId) await newConversation();
  for(const f of files){
    // Client-side size pre-check for a clear message before hitting the server.
    if(f.size > MAX_UPLOAD_BYTES){
      addMsg('assistant', `<span class="text-red-400">${esc(f.name)}: ${I18N.t('chat.upload_too_big','File too large (over 25 MB)')}</span>`);
      continue;
    }
    const fd = new FormData();
    fd.append('conversation_id', S.convId || '');
    // Upload-destination toggle: files normally land in uploads/; with the toggle on they
    // go into the linked project copy so sync_apply can write them to the real folder.
    // The server validates this value against the caller's own link — it is a hint, not
    // a path, and an unknown value is rejected with 400.
    if (S.uploadToProject && S.proj && S.proj.linked) fd.append('prefix', S.proj.prefix);
    fd.append('files', f, f.name);
    try {
      const r = await fetch('/api/upload', { method:'POST', credentials:'same-origin',
        headers: {'X-CSRF-Token': getCookie('hearth_csrf')}, body: fd });
      if(!r.ok){
        let msg = 'Upload failed';
        try { const e = await r.json(); msg = (e && (e.detail||e.error)) || msg; } catch(_) {}
        addMsg('assistant', `<span class="text-red-400">${esc(msg)}</span>`); continue;
      }
      const arts = (await r.json()).artifacts || [];
      for(const a of arts){
        S.attachments.push({ artifact_id:a.id, name:a.filename, mime:a.mime });
      }
      renderAttachStrip(); loadArtifacts();
    } catch(err){ addMsg('assistant', `<span class="text-red-400">${esc(String(err))}</span>`); }
  }
}

function renderAttachStrip(){
  const strip = $('attachStrip'); if(!strip) return;
  if(!S.attachments.length){ strip.classList.add('hidden'); strip.innerHTML=''; return; }
  strip.classList.remove('hidden'); strip.classList.add('flex');
  strip.innerHTML = S.attachments.map((a,i)=>`<span class="inline-flex items-center gap-1 text-xs bg-slate-800 border border-slate-700 rounded-lg px-2 py-1">
      ${esc(a.name)} <button data-rmatt="${i}" class="text-red-400 hover:text-red-300 ml-1" title="remove">✕</button></span>`).join('');
  strip.querySelectorAll('[data-rmatt]').forEach(b=> b.onclick = ()=>{ S.attachments.splice(parseInt(b.getAttribute('data-rmatt')),1); renderAttachStrip(); });
}

// Stage an EXISTING artifact (Files panel → "Attach to message") into the composer.
// No upload happens: the file is already in the workspace with an owned artifact row,
// and /api/chat/stream re-resolves artifact ids by owner (DEC-024), so referencing it
// is all that "send as attachment" needs. Deduped by id so pressing the item twice
// does not stack two identical chips. The chat must exist first, because attachments
// are always scoped to a conversation on the server.
async function stageArtifact(art){
  if(!art || !art.id) return;
  if(!S.convId) await newConversation();
  if(S.attachments.some(a=> a.artifact_id === art.id)){ flashAttachHint(I18N.t('chat.art_already_attached','Already attached.')); return; }
  S.attachments.push({ artifact_id: art.id, name: art.name, mime: '' });
  renderAttachStrip();
  flashAttachHint(I18N.t('chat.art_attached','Attached to the next message.'));
}

// Brief confirmation next to the composer that staging worked — the strip chip alone
// is easy to miss when the sidebar drawer is open on a phone.
function flashAttachHint(text){
  const hint = $('turnHint'); if(!hint) return;
  hint.textContent = text;
  clearTimeout(flashAttachHint._t);
  flashAttachHint._t = setTimeout(()=>{ hint.textContent = ''; }, 2600);
}

// ---- Files panel ⋮ menu ---------------------------------------------------
// Actions on one file. Everything lives here instead of as buttons per row: the
// filename is already a download link, so a second ⬇ was pure noise (and ate width
// a truncated name needs). The item list is data-driven like ATTACH_ITEMS, so a new
// action is one entry — `visible` hides it when it cannot apply.
const FILE_MENU_ITEMS = [
  { act: 'download', icon: '⬇', label: 'chat.download' },
  { act: 'attach',   icon: '📎', label: 'chat.art_attach' },
  { act: 'preview',  icon: '👁', label: 'chat.art_preview',
    visible: (a)=> !!a.url && /\.(png|jpe?g|gif|webp|bmp|svg|avif|heic)$/i.test(a.name) },
  { act: 'copylink', icon: '⧉', label: 'chat.art_copy_link' },
  { act: 'delete',   icon: '🗑', label: 'chat.delete', danger: true },
];

function fileMenuItemHtml(it){
  return `<button type="button" data-act="${it.act}" class="w-full text-left px-3 py-2 hover:bg-slate-700 flex items-center gap-2 ${it.danger ? 'text-red-300 hover:bg-red-900/60' : ''}">` +
         `<span class="shrink-0">${it.icon}</span><span data-i18n="${it.label}">${esc(I18N.t(it.label))}</span></button>`;
}

function openFileMenu(art, x, y){
  closeConvMenu();
  closeFileMenu();
  if(!art) return;
  const menu = document.createElement('div');
  menu.id = 'fileMenu';
  menu.className = 'fixed z-50 min-w-[190px] rounded-lg border border-slate-700 bg-slate-800 shadow-xl py-1 text-sm';
  const items = FILE_MENU_ITEMS.filter(it=> !it.visible || it.visible(art));
  // A subheading with the file name: several rows look alike once truncated, and a
  // menu that says only "Delete" gives no chance to notice you picked the wrong one.
  menu.innerHTML = `<div class="px-3 py-1.5 text-[11px] text-slate-400 border-b border-slate-700/70 truncate" title="${esc(art.name)}">${esc(art.name)}</div>` +
                   items.map(it=> fileMenuItemHtml(it)).join('');
  document.body.appendChild(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth  - r.width  - 8)) + 'px';
  menu.style.top  = Math.max(4, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
  for(const it of items){
    const b = menu.querySelector(`[data-act="${it.act}"]`); if(!b) continue;
    b.onclick = ()=>{ closeFileMenu(); fileMenuAction(it.act, art); };
  }
  // Same deferred outside-close trick as openConvMenu: the opening click's own
  // mousedown is still dispatching, so attaching now would close it immediately.
  setTimeout(()=> document.addEventListener('mousedown', _fileMenuAway, true), 0);
}

function closeFileMenu(){
  const m = document.getElementById('fileMenu');
  if(m) m.remove();
  document.removeEventListener('mousedown', _fileMenuAway, true);
}

function _fileMenuAway(e){
  const m = document.getElementById('fileMenu');
  if(m && !m.contains(e.target) && !e.target.closest('[data-art-menu]')) closeFileMenu();
}

async function fileMenuAction(act, art){
  if(act === 'download'){ downloadArtifact(art); return; }
  if(act === 'attach'){ await stageArtifact(art); return; }
  if(act === 'preview'){ window.open(art.url, '_blank', 'noopener'); return; }
  if(act === 'copylink'){ await copyArtifactLink(art); return; }
  if(act === 'delete'){ await deleteArtifactFromPanel(art); }
}

// Trigger the download from JS rather than reusing an <a download> click: inside a
// detached menu that has already been removed, a synthetic anchor click is dropped by
// some browsers (Safari especially). A real navigation always goes through.
function downloadArtifact(art){
  if(!art || !art.url) return;
  const a = document.createElement('a');
  a.href = art.url; a.download = art.name || ''; a.rel = 'noopener';
  document.body.appendChild(a); a.click(); a.remove();
}

async function copyArtifactLink(art){
  const url = art.url ? new URL(art.url, location.origin).href : '';
  try { await navigator.clipboard.writeText(url); flashAttachHint(I18N.t('chat.art_link_copied','Link copied.')); }
  catch(e){ flashAttachHint(url || I18N.t('chat.art_copy_failed','Could not copy the link.')); }
}

// Delete = remove the artifact record. The endpoint also deletes the workspace file
// when the body says `purge:true` (it used to drop only the row, leaving the bytes in
// the workspace with nothing left to point at them). Destructive, so it asks: plain
// confirm here, mirroring the conversation ⋮ menu's delete.
async function deleteArtifactFromPanel(art){
  if(!confirm(I18N.t('chat.art_delete_confirm','Delete this file? It is removed from your workspace too.'))) return;
  let ok = false;
  try {
    const r = await api(`/api/artifact/${encodeURIComponent(art.id)}/delete`,
                        {method:'POST', body: JSON.stringify({purge:true})});
    ok = !!(r && r.ok);
  } catch(e){ ok = false; }
  if(!ok) flashAttachHint(I18N.t('chat.art_delete_failed','Could not delete the file.'));
  await loadArtifacts();
}

function wireDragDrop(){
  const dz = $('dropzone'); let depth = 0;
  const show = ()=>{ if(dz) dz.classList.remove('hidden'); };
  const hide = ()=>{ if(dz) dz.classList.add('hidden'); };
  window.addEventListener('dragenter', (e)=>{ e.preventDefault(); depth++; show(); });
  window.addEventListener('dragover', (e)=>{ e.preventDefault(); });
  window.addEventListener('dragleave', (e)=>{ e.preventDefault(); depth=Math.max(0,depth-1); if(depth===0) hide(); });
  window.addEventListener('drop', (e)=>{ e.preventDefault(); depth=0; hide(); if(e.dataTransfer && e.dataTransfer.files.length) stageFiles(e.dataTransfer.files); });
}

// ---- Device access (server-side, per conversation) ------------------------
// The UI no longer carries an "Allow device access" switch: the grant API still
// exists (/api/chat/device-access), and device tools keep enforcing it. This is
// just a note in Settings — shown only when a helper IS paired but this chat has
// no grant, i.e. exactly when it can matter. Uploads/downloads are never device
// access and need nothing here.
async function refreshDeviceHint(){
  const el = $('deviceHint'); if(!el) return;
  let d = null;
  try { const r = await api(`/api/chat/device-access?conversation_id=${S.convId||''}`); if(r && r.ok) d = await r.json(); }
  catch(e){ return; }
  if(d && d.required && d.helper_configured && !d.granted){
    el.classList.remove('hidden');
    el.textContent = I18N.t('chat.device_hint',
      'A device helper is paired. Device tools (folder listing, screenshots) need per-chat access — ask an admin for details.');
  } else {
    el.classList.add('hidden'); el.textContent = '';
  }
}

// ---- Language selector ----------------------------------------------------
async function setLanguage(lang){
  const fd = new FormData(); fd.append('language', lang);
  try { await fetch('/api/language', { method:'POST', credentials:'same-origin', headers:{'X-CSRF-Token': getCookie('hearth_csrf')}, body: fd }); } catch(e){}
  document.documentElement.lang = lang;
  await I18N.load(); I18N.apply(); loadArtifacts();
}

// ---- Mobile sidebar drawer -------------------------------------------------
// Below md: the sidebar is an off-canvas drawer (#sidebar gets .open, #scrim a
// backdrop). From md: up the template's classes pin it visible regardless.
function openSidebar(){
  const sb = $('sidebar'), sc = $('scrim');
  if(sb) sb.classList.add('open');
  if(sc) sc.classList.remove('hidden');
}
function closeSidebar(){
  const sb = $('sidebar'), sc = $('scrim');
  if(sb) sb.classList.remove('open');
  if(sc) sc.classList.add('hidden');
}
if($('menuBtn')){
  $('menuBtn').onclick = openSidebar;
  const cs = $('closeSidebar'); if(cs) cs.onclick = closeSidebar;
  $('scrim').onclick = closeSidebar;
}
// The header's chat-name pill also opens the drawer (it IS the conversation
// list on mobile); label it with the active conversation's title.
if($('convTitleBtn')) $('convTitleBtn').onclick = openSidebar;
function setConvTitleLabel(title){
  const el = $('convTitleLabel'); if(el) el.textContent = title || 'Hearth';
}

// ---- Sidebar navigation (icon rail -> panels) ------------------------------
// One panel visible at a time inside #panelHost. Panels that fetch data do so
// lazily on first activation (see PANEL_INIT). `hidden` toggling relies on the
// base.css rule `#panelHost > section:not(.hidden){display:flex}` so Tailwind's
// .hidden always wins when set.
let CURRENT_PANEL = 'chats';
const PANEL_INIT = {
  prompt: loadSystemPrompt,
  skills: loadSkills,
  settings: refreshDeviceHint,
};

function showPanel(name){
  CURRENT_PANEL = name;
  document.querySelectorAll('#rail button').forEach(b=>{
    const on = b.getAttribute('data-panel') === name;
    b.classList.toggle('on', on);
  });
  document.querySelectorAll('#panelHost > section').forEach(s=>{
    s.classList.toggle('hidden', s.getAttribute('data-panel') !== name);
  });
  const init = PANEL_INIT[name];
  if(init && !showPanel['_ran:'+name]){ showPanel['_ran:'+name] = true; init(); }
}
document.querySelectorAll('#rail button').forEach(b=>{
  b.onclick = ()=>{ showPanel(b.getAttribute('data-panel')); };
});

// ---- Prompt panel (system prompt editor) -----------------------------------
// The text here IS the model's system prompt (personal instructions, applied to
// every chat from the next turn). Clearing it falls back to the admin default doc
// or the built-in persona. Replaces the old per-conversation dropdown that only
// ever offered "— default —".
let SP_LOADED = '';     // last loaded/saved text, for dirty-checking
let SP_DEFAULT = '';    // built-in persona, for the Restore button

async function loadSystemPrompt(){
  const ta = $('spText'); if(!ta) return;
  try {
    const r = await api('/api/chat/system-prompt'); if(!r || !r.ok) return;
    const d = await r.json();
    // The server returns the text actually in effect (personal prompt, else admin
    // default doc, else built-in persona), so the editor never looks empty while a
    // prompt is active. Editing/saving replaces it entirely — this IS the prompt.
    ta.value = d.content || '';
    SP_DEFAULT = d.default || '';
    ta.placeholder = (d.default ? I18N.t('prompt.placeholder_base','Your system prompt…') : 'Your system prompt…');
    SP_LOADED = ta.value;
  } catch(e){ /* keep whatever is in the textarea */ }
}

async function saveSystemPrompt(){
  const ta = $('spText'), status = $('spStatus');
  try {
    const r = await api('/api/chat/system-prompt', {method:'POST', body: JSON.stringify({content: ta.value})});
    if(!r || !r.ok){ throw new Error((r && (r.detail||r.statusText)) || 'save failed'); }
    SP_LOADED = ta.value;
    if(status) status.textContent = I18N.t('prompt.saved','Saved ✓');
  } catch(e){
    if(status) status.textContent = I18N.t('prompt.save_failed','Could not save');
    return;
  }
  setTimeout(()=>{ if(status && ta.value === SP_LOADED) status.textContent = ''; }, 2500);
}

if($('spSave')) $('spSave').onclick = saveSystemPrompt;
// Clear reverts to the built-in default prompt server-side (use_default), rather
// than saving an empty string that would leave the model with no persona at all.
if($('spReset')) $('spReset').onclick = async ()=>{
  const ta = $('spText'), status = $('spStatus');
  try {
    const r = await api('/api/chat/system-prompt', {method:'POST', body: JSON.stringify({use_default:true})});
    if(!r || !r.ok){ throw new Error('reset failed'); }
    SP_LOADED = ta.value = SP_DEFAULT;
    if(status) status.textContent = I18N.t('prompt.saved','Saved ✓');
  } catch(e){
    if(status) status.textContent = I18N.t('prompt.save_failed','Could not save');
    return;
  }
  setTimeout(()=>{ if(status && ta.value === SP_LOADED) status.textContent = ''; }, 2500);
};

// ---- Skills panel (admin-managed protocols, read-only) ---------------------
// The old composer flow ("attach a protocol to the next message") was removed;
// this panel just shows what an admin has provided.
let DOCS = [];

async function loadSkills(){
  const listEl = $('skillList'); if(!listEl) return;
  try{ DOCS = await (await api('/api/chat/docs')).json(); }catch(e){ return; }
  const protocols = (DOCS || []).filter(d=>d.kind==='protocol');
  if(!protocols.length){
    listEl.innerHTML = `<div class="text-xs text-slate-500">${esc(I18N.t('skills.none','Nothing here yet.'))}</div>`;
    return;
  }
  listEl.innerHTML = protocols.map(d=>`<div class="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-2 text-sm">📜 <b>${esc(d.name)}</b></div>`).join('');
}

// ---- Attach menu (composer) -----------------------------------------------
// One 📎 button replaces the old standalone 📎/📷 buttons; its items live in a
// popup so future actions (project tools, etc.) extend ATTACH_ITEMS instead of
// growing the input row. The panel is positioned fixed above the button and
// closes on outside click / Escape / scroll / resize — same pattern as the
// conversation ⋮ menu (openConvMenu).
//
// Each item: label i18n key, optional icon, `checkable` renders a ✓ state box
// bound to S.uploadToProject, `visible` decides whether it shows at all.
const ATTACH_ITEMS = [
  { act: 'files',   icon: '📎', label: 'chat.upload' },
  // Camera/gallery only makes sense while a project link is active (matches the
  // previous behavior, where the capture input was only clickable then). Gated
  // here via visible() instead of by un-hiding the raw <input type=file>.
  { act: 'camera',  icon: '📷', label: 'chat.am_camera',
    visible: ()=> !!(S.proj && S.proj.linked) },
  // Upload destination toggle (DEC-049): shown only when a project folder is
  // linked. Off = files attach to the chat as always (uploads/). On = files land
  // in the project copy so they can be applied to the real folder. The server
  // validates this value against the caller's own link; it is never a path.
  { act: 'proj_into', icon: '⬥', label: 'chat.am_proj_into', title: 'chat.am_proj_hint',
    checkable: true, visible: ()=> !!(S.proj && S.proj.linked) },
];

function attachItemHtml(it){
  const box = it.checkable
    ? `<span class="shrink-0 w-4 text-center ${S.uploadToProject ? 'text-emerald-400' : 'text-slate-600'}">${S.uploadToProject ? '✓' : '○'}</span>`
    : '';
  return `<button type="button" data-am="${it.act}" class="w-full text-left px-3 py-2 hover:bg-slate-700 flex items-center gap-2">` +
         `${box}<span class="shrink-0">${it.icon}</span><span data-i18n="${it.label}">${esc(I18N.t(it.label))}</span></button>`;
}

// Positioning lives in base.css (#attachWrap/#attachMenu anchor the panel to
// the button in pure CSS). Why not JS coordinates: the vendored Tailwind build
// compiles styles asynchronously (MutationObserver-driven), so the layout can
// shift AFTER we measure — a one-shot getBoundingClientRect() then places the
// popup against a transient position, i.e. off to the side or off-screen
// ("menu doesn't show"). All that's left here is a horizontal nudge for narrow
// phones: if the panel overflows the right edge, shift it left by the overlap.
// It reads only the menu's own rect and runs again after Tailwind settles.
function clampAttachMenu(){
  const menu = $('attachMenu');
  if(!menu || menu.classList.contains('hidden')) return;
  const mr = menu.getBoundingClientRect();
  if(!mr.width) return;
  const overflow = mr.right - (window.innerWidth - 8);
  if(overflow > 0){
    // left is CSS `0` against the wrapper; move via a negative margin so the
    // CSS anchor stays the single source of truth for vertical placement.
    const cur = parseFloat(menu.style.getPropertyValue('--am-shift')) || 0;
    menu.style.setProperty('--am-shift', (cur - overflow) + 'px');
    menu.style.left = 'var(--am-shift, 0px)';
  }
}

// Re-render the open panel (check state / project link changed).
function refreshAttachMenu(){
  const menu = $('attachMenu');
  if(!menu || menu.classList.contains('hidden')) return;
  renderAttachMenu();
}

// Rebuild the panel from ATTACH_ITEMS. Labels keep data-i18n attributes so a
// language switch while the menu is open re-translates them via I18N.apply().
// No per-item handler wiring here — clicks go through wireAttachMenu's
// delegation on #attachMenu, which survives these innerHTML rebuilds.
function renderAttachMenu(){
  const menu = $('attachMenu'); if(!menu) return;
  menu.innerHTML = ATTACH_ITEMS.filter(it=> !it.visible || it.visible())
    .map(attachItemHtml).join('');
  for(const it of ATTACH_ITEMS){
    const b = menu.querySelector(`[data-am="${it.act}"]`); if(!b) continue;
    if(it.title) b.setAttribute('title', I18N.t(it.title));
  }
}

// Toggle items re-render in place and KEEP the menu open (the checkmark should
// respond without a reopen). Handlers are wired once at boot (wireAttachMenu)
// via delegation — rebuilding innerHTML on every render would otherwise drop
// them, which is exactly what made earlier versions do nothing on click.
function attachMenuAction(act){
  if(act === 'proj_into'){ S.uploadToProject = !S.uploadToProject; refreshAttachMenu(); return; }
  closeAttachMenu();
  // The hidden pickers do the actual work; programmatic click() works on
  // display:none inputs, so #cameraInput stays permanently hidden.
  if(act === 'files'){ const el=$('fileInput'); if(el) el.click(); return; }
  if(act === 'camera'){ const el=$('cameraInput'); if(el) el.click(); return; }
}

// One-time wiring (runs at boot, after the menu button exists in the DOM).
function wireAttachMenu(){
  const btn = $('attachMenuBtn'), menu = $('attachMenu');
  if(!btn || !menu) return;
  btn.onclick = ()=>{
    if(!menu.classList.contains('hidden')) closeAttachMenu(); else openAttachMenu();
  };
  // Delegation: survives every innerHTML rebuild of the panel.
  menu.addEventListener('click', (e)=>{
    const b = e.target.closest('[data-am]'); if(!b) return;
    attachMenuAction(b.getAttribute('data-am'));
  });
}

function openAttachMenu(){
  renderAttachMenu();
  const btn = $('attachMenuBtn'); if(btn) btn.setAttribute('aria-expanded', 'true');
  const menu = $('attachMenu'); if(menu){ menu.classList.remove('hidden'); menu.style.left = ''; }
  clampAttachMenu();
  // The vendored Tailwind build restyles asynchronously; re-clamp once it has
  // settled so a late layout shift can't leave the panel hanging off-screen.
  requestAnimationFrame(()=>clampAttachMenu());
  setTimeout(()=>{ if(!menu.classList.contains('hidden')) clampAttachMenu(); }, 150);
  // Defer attaching the outside-close listener by a macrotask: click handlers
  // dispatch synthetic events, and a same-task addEventListener would let the
  // opening click's own bubbling mousedown close the menu again immediately.
  setTimeout(()=> document.addEventListener('mousedown', _attachMenuAway, true), 0);
}

function closeAttachMenu(){
  const menu = $('attachMenu'); if(!menu || menu.classList.contains('hidden')) return;
  menu.classList.add('hidden');
  const btn = $('attachMenuBtn'); if(btn) btn.setAttribute('aria-expanded', 'false');
  document.removeEventListener('mousedown', _attachMenuAway, true);
}

function _attachMenuAway(e){
  if(e.target.closest('#attachMenuBtn')) return;   // its own handler toggles
  if(!e.target.closest('#attachMenu')) closeAttachMenu();
}

// ---- Wire up ----
$('newChat').onclick = ()=>{ closeSidebar(); newConversation(); };
$('logoutBtn').onclick = async ()=>{ await api('/auth/logout',{method:'POST'}); location.href='/login'; };
const dbgToggleEl = $('dbgToggle');
if(dbgToggleEl) dbgToggleEl.onchange = (e)=>{ dbgEl.classList.toggle('hidden', !e.target.checked); };
$('refreshArtifacts').onclick = loadArtifacts;
$('abortBtn').onclick = abortTurn;
$('retryBtn').onclick = retryLastTurn;
wireAttachMenu();
$('fileInput').onchange = (e) => { stageFiles(e.target.files); e.target.value=''; };
$('cameraInput').onchange = (e) => { stageFiles(e.target.files); e.target.value=''; };
$('langSel').onchange = (e) => setLanguage(e.target.value);
wireDragDrop();

// First-run hint about the file/download flow (see chat.html #introNote). It is part of
// the transcript area, so it disappears as soon as the first exchange starts; while it
// is visible it also gets a dismiss button. Once dismissed it never comes back.
(function wireIntroNote(){
  const note = $('introNote'); if(!note) return;
  const gone = ()=>{ try { localStorage.setItem('hearth_intro_dismissed', '1'); } catch(e){}
                     note.remove(); };
  try { if(localStorage.getItem('hearth_intro_dismissed') === '1'){ note.remove(); return; } } catch(e){}
  note.classList.remove('hidden');
  const btn = document.createElement('button');
  btn.id = 'introDismiss';
  btn.className = 'block mt-2 text-[11px] text-slate-500 hover:text-slate-300';
  btn.textContent = (window.I18N && I18N.t('chat.intro_dismiss')) || 'Got it';
  btn.onclick = gone;
  note.appendChild(btn);
  // The hint explains files before there is any traffic; once a message arrives it is
  // noise, so retire it (and remember that) rather than leaving it above the thread.
  new MutationObserver(()=>{ if(msgsEl.children.length > 1) gone(); })
    .observe(msgsEl, {childList: true});
})();

$('composer').addEventListener('submit', async (e)=>{
  e.preventDefault();
  // While a turn is running the Send button is disabled, but Enter on mobile
  // keyboards can still submit the form — route that to Stop instead of queueing.
  if(S.busy){ abortTurn(); return; }
  const text = $('input').value.trim();
  const atts = S.attachments.slice();
  if((!text && !atts.length) || S.busy) return;   // allow attachments-only messages
  if(!S.convId) await newConversation();
  // Render the user's message with attachment chips.
  let userHtml = esc(text);
  const chips = [];
  for(const a of atts) chips.push(`📎 ${esc(a.name)}`);
  if(chips.length){
    userHtml = (userHtml? userHtml+'<br/>':'') +
      chips.map(c=>`<span class="inline-flex items-center gap-1 text-[11px] bg-slate-700/60 rounded px-1.5 py-0.5">${c}</span>`).join(' ');
  }
  addMsg('user', userHtml); $('input').value=''; setBusy(true); scrollToBottom(true);
  S.lastUserText = text;
  const payload = {conversation_id:S.convId, content:text, attachments:atts};
  S.attachments = []; renderAttachStrip();   // clear staged now that they're sent
  activity(I18N.t('chat.thinking','thinking…'));
  const state = {};
  S.abortCtrl = new AbortController();
  await streamPost('/api/chat/stream', payload, (ev)=>handleEvent(ev, state),
                   {signal: S.abortCtrl.signal, aborted: ()=>state.aborted,
                    // A turn is already running here: the server queued this message as
                    // steering instead of starting a second stream. It answers with JSON, not
                    // SSE, so streamPost hands it to onQueued rather than the event handler.
                    onQueued: (j)=>{ state.queued = true; renderSteerQueue(j.queued||[]); }});
  S.abortCtrl = null;
  // Queued means nothing was streamed for THIS message — it will appear as a user bubble at
  // its delivery seam (the `steered` event). So: no conversation refresh, and Stay busy only
  // if a turn is genuinely still running (it is — that is why we queued).
  if(!state.queued){ loadConversations(); loadContextUsage(); }
  // Refresh the project chip after each turn: the agent may have edited project/, and an
  // approved sync_apply may already have written. On-demand only (hashes the tree).
  if(!state.awaiting) loadProjectSummary();
  if(!state.awaiting) setBusy(false);
});

// ---- Conversation search / rename / delete --------------------------------
let _searchTimer = null;
$('searchBox').addEventListener('input', function(){
  clearTimeout(_searchTimer); const q = this.value.trim();
  _searchTimer = setTimeout(()=> q ? runSearch(q) : loadConversations(), 250);
});

async function runSearch(q){
  let items = [];
  try { items = await (await api(`/api/chat/search?q=${encodeURIComponent(q)}`)).json(); } catch(e){ return; }
  const nav = $('convList'); nav.innerHTML = '';
  if(!items.length){ const d=document.createElement('div'); d.className='text-[11px] text-slate-500 px-2'; d.textContent='—'; nav.appendChild(d); return; }
  for(const c of items){
    const row = document.createElement('button');
    row.setAttribute('data-cid', c.id);
    const active = c.id === S.convId;
    row.className = convRowBaseClass(active);
    row.innerHTML = convRowHtml(
      `<div class="truncate">${esc(c.title)}</div>` +
      (c.match==='content' && c.snippet ? `<div class="text-[11px] text-slate-500 truncate">…${esc(c.snippet)}</div>` : ''),
      c.id);
    row.onclick = (e) => { if(!e.target.closest('[data-menu]')) openConversation(c.id); };
    nav.appendChild(row);
  }
}

// Rename / delete for ANY conversation row (opened via the ⋮ button, a
// long-press, or the native right-click context menu on #convList).
let _pressTimer = null, _suppressClick = false;

async function renameConversation(cid){
  const row = document.querySelector(`#convList [data-cid="${cid}"]`);
  const current = row ? row.querySelector('span')?.textContent.trim() || '' : '';
  const name = prompt(I18N.t('chat.rename_prompt'), current);
  if(!name || !name.trim()) return;
  await api(`/api/chat/conversations/${cid}/rename`, {method:'POST', body: JSON.stringify({title:name.trim()})});
  // Refresh whichever list is showing (search results or full list).
  const q = $('searchBox').value.trim();
  if(q) runSearch(q); else loadConversations();
}

async function deleteConversation(cid){
  if(!confirm(I18N.t('chat.delete_confirm'))) return;
  const alsoFiles = confirm(I18N.t('chat.delete_files_q'));
  await api(`/api/chat/conversations/${cid}/delete`, {method:'POST', body: JSON.stringify({delete_artifacts: alsoFiles})});
  if(cid === S.convId){
    S.attachments = []; renderAttachStrip();
    S.convId = null; msgsEl.innerHTML = '';
  }
  const q = $('searchBox').value.trim();
  if(q) await runSearch(q); else await loadConversations();
  const list = $('convList');
  if(!list.children.length){ await newConversation(); }
  else if(!S.convId){ list.firstChild.click(); }
}

function closeConvMenu(){
  const m = document.getElementById('convMenu');
  if(m) m.remove();
  document.removeEventListener('mousedown', _menuAway, true);
}

function _menuAway(e){
  const m = document.getElementById('convMenu');
  if(m && !m.contains(e.target) && !e.target.closest('[data-menu]')) closeConvMenu();
}

function openConvMenu(cid, x, y){
  closeConvMenu();
  const menu = document.createElement('div');
  menu.id = 'convMenu';
  menu.className = 'fixed z-50 min-w-[140px] rounded-lg border border-slate-700 bg-slate-800 shadow-xl py-1 text-sm';
  menu.innerHTML = `<button data-act="rename" class="w-full text-left px-3 py-1.5 hover:bg-slate-700">${esc(I18N.t('chat.rename'))}</button>` +
                   `<button data-act="delete" class="w-full text-left px-3 py-1.5 hover:bg-red-900/60 text-red-300">${esc(I18N.t('chat.delete'))}</button>`;
  document.body.appendChild(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth  - r.width  - 8)) + 'px';
  menu.style.top  = Math.max(4, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
  menu.querySelector('[data-act="rename"]').onclick = ()=>{ closeConvMenu(); renameConversation(cid); };
  menu.querySelector('[data-act="delete"]').onclick = ()=>{ closeConvMenu(); deleteConversation(cid); };
  // Close on any subsequent click outside the menu (capture phase, so it beats
  // row handlers). The activating click's own mousedown already happened, and
  // each item handler closes the menu before its confirm/prompt dialog opens —
  // by then the listener is gone, so dialogs never trigger a stray close.
  setTimeout(()=> document.addEventListener('mousedown', _menuAway, true), 0);
}

$('convList').addEventListener('click', (e)=>{
  const mb = e.target.closest('[data-menu]');
  if(!mb) return;
  e.preventDefault(); e.stopPropagation();
  if(_suppressClick) return;   // long-press already opened the menu
  const r = mb.getBoundingClientRect();
  openConvMenu(mb.getAttribute('data-menu'), r.right - 140, r.bottom + 4);
});

// Touch: long-press a row opens the menu and swallows the synthetic click.
$('convList').addEventListener('touchstart', (e)=>{
  const row = e.target.closest('[data-cid]'); if(!row) return;
  const t = e.touches[0];
  _pressTimer = setTimeout(()=>{
    _suppressClick = true;
    openConvMenu(row.getAttribute('data-cid'), t.clientX, t.clientY);
    setTimeout(()=>{ _suppressClick = false; }, 700);
  }, 550);
}, {passive:true});

// Right-click anywhere on a row → same custom menu (native one suppressed).
// On touch, the long-press timer above owns the menu, so don't double-open.
$('convList').addEventListener('contextmenu', (e)=>{
  const row = e.target.closest('[data-cid]');
  if(!row) return;
  if(e.pointerType === 'touch' || _pressTimer) return;
  e.preventDefault();
  openConvMenu(row.getAttribute('data-cid'), e.clientX, e.clientY);
});

$('convList').addEventListener('touchend',    ()=> clearTimeout(_pressTimer));
$('convList').addEventListener('touchmove',   ()=> clearTimeout(_pressTimer));
$('convList').addEventListener('touchcancel', ()=> clearTimeout(_pressTimer));

// ---- Files panel ⋮ menu wiring --------------------------------------------
// Delegation on the <ul>: loadArtifacts() rebuilds every row, so per-row handlers
// would have to be re-attached on each refresh (and were, before, implicitly by
// innerHTML — which is why an explicit button used to be the only way in).
const _artList = $('artifactList');
if(_artList){
  let artPress = null, artSuppress = false;
  const artOpen = (id, x, y)=>{ const a = findArtifact(id); if(a) openFileMenu(a, x, y); };
  _artList.addEventListener('click', (e)=>{
    const mb = e.target.closest('[data-art-menu]');
    if(!mb) return;
    e.preventDefault(); e.stopPropagation();
    if(artSuppress){ artSuppress = false; return; }   // long-press already opened it
    const r = mb.getBoundingClientRect();
    artOpen(mb.getAttribute('data-art-menu'), r.right - 190, r.bottom + 4);
  });
  // Right-click the row (not just the ⋮) → same menu.
  _artList.addEventListener('contextmenu', (e)=>{
    const mb = e.target.closest('[data-art-menu]'); if(!mb) return;
    if(e.pointerType === 'touch' || artPress) return;
    e.preventDefault();
    artOpen(mb.getAttribute('data-art-menu'), e.clientX, e.clientY);
  });
  // Touch: long-press opens the menu and swallows the synthetic click that follows.
  _artList.addEventListener('touchstart', (e)=>{
    const mb = e.target.closest('[data-art-menu]'); if(!mb) return;
    const t = e.touches[0]; const id = mb.getAttribute('data-art-menu');
    artPress = setTimeout(()=>{
      artSuppress = true; artOpen(id, t.clientX, t.clientY);
      setTimeout(()=>{ artSuppress = false; }, 700);
    }, 550);
  }, {passive:true});
  ['touchend','touchmove','touchcancel'].forEach(ev=> _artList.addEventListener(ev, ()=> clearTimeout(artPress)));
}

document.addEventListener('scroll', closeConvMenu, true);
window.addEventListener('resize', closeConvMenu);
document.addEventListener('scroll', closeFileMenu, true);
window.addEventListener('resize', closeFileMenu);
// The attach menu is anchored to the composer button, so it must go away with
// scroll/resize too (same treatment as the conv ⋮ menu).
document.addEventListener('scroll', closeAttachMenu, true);
window.addEventListener('resize', closeAttachMenu);

document.querySelectorAll('input[name="artScope"]').forEach(r=> r.onchange = ()=>{ if(r.checked){ S.artScope = r.value; loadArtifacts(); } });

// auto-grow textarea + Enter-to-send
$('input').addEventListener('input', function(){ this.style.height='auto'; this.style.height=Math.min(this.scrollHeight,160)+'px'; });
// Enter behavior follows the user pref (Settings → "Enter sends the message"):
//  enter_sends=true : Enter = send, Shift+Enter = newline (default, chat-app style)
//  enter_sends=false: Enter = newline; send with the Send button (or Ctrl/Cmd+Enter)
$('input').addEventListener('keydown', (e)=>{
  if(e.key !== 'Enter') return;
  const ctrl = e.ctrlKey || e.metaKey;
  if(PREFS.enter_sends){
    if(e.shiftKey) return;            // newline
    e.preventDefault(); $('composer').requestSubmit();
  } else {
    if(ctrl){ e.preventDefault(); $('composer').requestSubmit(); }
    // plain Enter falls through to a newline in the textarea
  }
});

// Escape closes overlays, in dismiss-hierarchy order: conversation ⋮ menu first,
// then the composer attach menu, then the mobile drawer (standard behavior;
// otherwise a stray Stop fires behind an open overlay).
document.addEventListener('keydown', (e)=>{
  if(e.key !== 'Escape') return;
  if(document.getElementById('convMenu')) return;   // convList's own handler closes it
  if(document.getElementById('fileMenu')){ closeFileMenu(); return; }
  const am = $('attachMenu');
  if(am && !am.classList.contains('hidden')){ closeAttachMenu(); return; }
  const sb = $('sidebar');
  if(sb && sb.classList.contains('open')){ closeSidebar(); return; }
  if(S.busy){ e.preventDefault(); abortTurn(); }
});

// ---- Steering queue (messages sent while a turn owns the transcript) -------
// The server holds the queue; this only displays it and offers Withdraw. Rows are
// re-fetched rather than trusted from local state, because delivery happens server-side at
// the between-rounds seam and the page cannot know when that was.
function renderSteerQueue(items){
  const wrap = $('steerWrap'), list = $('steerList');
  if(!wrap || !list) return;
  list.innerHTML = '';
  if(!items || !items.length){ wrap.classList.add('hidden'); return; }
  wrap.classList.remove('hidden');
  items.forEach((txt, i)=>{
    const li = document.createElement('li');
    li.className = 'flex items-start gap-2';
    const span = document.createElement('span');
    span.className = 'flex-1 text-slate-300 break-words';
    span.textContent = txt;                       // textContent, never innerHTML
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'shrink-0 text-[10px] uppercase tracking-wide text-slate-400 hover:text-red-300';
    btn.textContent = I18N.t('chat.steer_withdraw', 'Withdraw');
    btn.onclick = async ()=>{
      await api('/api/chat/steer/withdraw', {method:'POST',
        body: JSON.stringify({conversation_id:S.convId, index:i})});
      loadSteerQueue();
    };
    li.appendChild(span); li.appendChild(btn); list.appendChild(li);
  });
}

async function loadSteerQueue(){
  if(!S.convId){ renderSteerQueue([]); return; }
  let d = null;
  try { const r = await api(`/api/chat/steer?conversation_id=${S.convId}`); if(r && r.ok) d = await r.json(); }
  catch(e){ return; }
  if(d && d.ok) renderSteerQueue(d.queued || []);
}


// server knows whether this conversation still has a turn streaming.
async function syncRunningState(){
  if(!S.convId) return;
  let d = null;
  try { const r = await api(`/api/chat/running?conversation_id=${S.convId}`); if(r && r.ok) d = await r.json(); }
  catch(e){ return; }
  if(d && d.running && !S.busy){
    setBusy(true);
    activity(I18N.t('chat.still_running','a turn is still running in this chat — Stop to end it'));
  }
}
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) syncRunningState(); });
// The queue lives server-side, so a reload or tab wake must re-read it (a message queued in
// another tab is otherwise invisible here).
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) loadSteerQueue(); });

// ---- Mobile viewport height -------------------------------------------------
// The layout is h-full; on mobile browsers the URL bar makes 100vh taller than
// the visible area, pushing the composer off-screen. Expose the *UI chrome*
// height (window.innerHeight — deliberately NOT visualViewport.height, which
// shrinks when the keyboard opens) as a CSS var so typing never collapses the
// transcript; dvh is the fallback before JS runs / on desktop.
function setVh(){
  document.documentElement.style.setProperty('--app-h', window.innerHeight + 'px');
}
setVh();
window.addEventListener('resize', setVh);

// If the viewport grows to desktop width while the mobile drawer is open, close
// it (the sidebar becomes a pinned column; a stale backdrop would darken the UI).
window.matchMedia('(min-width: 768px)').addEventListener('change', (e)=>{
  if(e.matches) closeSidebar();
});

(async function init(){
  const r = await api('/auth/me'); if(!r || !r.ok) return;
  const me = await r.json();
  // Load + apply user's language before rendering chrome.
  await I18N.load(); if (me.language) I18N.lang = me.language; I18N.apply();
  const langSel = $('langSel'); if(langSel) langSel.value = me.language || 'en';
  // Behavioral preferences (Enter-behavior etc.) before any composer wiring runs.
  // Guarded: prefs are an enhancement — a hiccup here must never take the rest
  // of init down with it (defaults in app.js apply unchanged).
  try { applyPrefs(me.prefs); syncSettingsInputs(); } catch(e){ /* defaults stand */ }
  await loadConversations();
  const list = $('convList');
  if(list.children.length===0){ await newConversation(); } else { list.firstChild.click(); }
  // model badge — passive probe only; never triggers a model launch (DEC-048)
  renderModelBadge();
  // Linked project chip (DEC-049): one cheap status call at boot, then on-demand summaries.
  loadProjectStatus();
  const pchip = $('projChip'); if(pchip) pchip.onclick = projectChipClicked;
  showPanel('chats');   // mark the rail; prompt/skills load lazily on first click
})();

// ---- Settings panel (behavioral prefs) --------------------------------------
function syncSettingsInputs(){
  const map = { enter_sends:'prefEnterSends', auto_scroll:'prefAutoScroll',
                show_timestamps:'prefTimestamps' };
  for(const [k,id] of Object.entries(map)){ const el=$(id); if(el) el.checked = !!PREFS[k]; }
}

// Each toggle saves immediately; timestamp visibility is re-applied live because
// existing bubbles were stamped (or not) under the old setting.
function wirePrefToggle(id, key, after){
  const el = $(id); if(!el) return;
  el.onchange = async ()=>{ await savePref(key, el.checked); if(after) after(el.checked); };
}
wirePrefToggle('prefEnterSends','enter_sends');
wirePrefToggle('prefAutoScroll','auto_scroll');
wirePrefToggle('prefTimestamps','show_timestamps', ()=> renderTimestamps());

// ---- Timestamps (optional, pref-gated) --------------------------------------
// Applied post-hoc via data-ts attributes so re-rendering or toggling the pref
// never needs message history back. Bubbles created while the pref is off get
// stamped when it turns on.
function stampTime(bubble, ts){
  if(!bubble || bubble.querySelector('.ts-chip')) return;
  const chip = document.createElement('div');
  chip.className = 'ts-chip text-[9px] text-slate-500 mt-1 select-none';
  chip.textContent = new Date(ts*1000).toLocaleTimeString();
  bubble.appendChild(chip);
}
function renderTimestamps(){
  document.querySelectorAll('#messages [data-ts]').forEach(w=>{
    // The bubble is the wrapper's direct div child (addMsg always creates one).
    const bubble = w.querySelector(':scope > div');
    if(!bubble) return;
    if(PREFS.show_timestamps) stampTime(bubble, parseInt(w.getAttribute('data-ts'),10));
    else { const c = bubble.querySelector('.ts-chip'); if(c) c.remove(); }
  });
}

// ---- Linked project chip (DEC-049) -----------------------------------------
// The chat-side half of the linked-folder feature. Deliberately on-demand: /api/project/summary
// hashes the whole copy and every baseline target, so it is called at turn end, on manual
// refresh, and right before an apply — never from a poll timer.
let PROJ = { linked:false };

async function loadProjectStatus(){
  try {
    const d = await (await api('/api/project/status')).json();
    PROJ = d || {linked:false};
    S.proj = PROJ;
    // Camera availability is driven by PROJ.linked through the attach menu's
    // visible() predicate — never by toggling `hidden` on #cameraInput. That
    // class is display control only, and removing it exposed Firefox's native
    // file-picker chrome ("Durchsuchen… Keine Datei ausgewählt") in the composer.
    if(!PROJ.linked){
      $('projWrap') && $('projWrap').classList.add('hidden'); return;
    }
    $('projWrap') && $('projWrap').classList.remove('hidden');
    $('projWrap') && $('projWrap').classList.add('flex');
    setProjChip({state:'linked'});
    await loadProjectSummary();
  } catch(e){ /* no chip is better than a wrong one */ }
}

async function loadProjectSummary(){
  if(!PROJ.linked) return;
  const btn = $('projChip'); if(btn) btn.disabled = true;
  try {
    const d = await (await api('/api/project/summary')).json();
    PROJ.summary = d; S.proj = PROJ;
    setProjChip(d);
  } catch(e){
    setProjChip({state:'error'});
  } finally { if(btn) btn.disabled = false; }
}

function setProjChip(d){
  const el = $('projChip'); if(!el) return;
  const T = (k,fb)=> (window.I18N && I18N.t(k)) || fb;
  if(d.state === 'error'){
    el.textContent = '⚠ project'; el.className = 'shrink-0 rounded-lg px-2 py-1 text-xs bg-red-900/40 text-red-300 hover:bg-red-900/60';
    el.title = T('chat.proj_err','Could not read the linked project'); return;
  }
  if(!d.added && !d.modified && !(d.conflicts||[]).length){
    el.textContent = '⬥ project';
    el.className = 'shrink-0 rounded-lg px-2 py-1 text-xs bg-slate-800 text-slate-400 hover:bg-slate-700';
    el.title = T('chat.proj_clean','Linked project: no pending changes (click to refresh)');
    return;
  }
  const bits = [];
  if(d.added) bits.push(`+${d.added}`);
  if(d.modified) bits.push(`~${d.modified}`);
  if((d.conflicts||[]).length) bits.push(`!${(d.conflicts||[]).length}`);
  const conflict = (d.conflicts||[]).length > 0;
  el.textContent = `⬥ project ${bits.join(' ')}`;
  el.className = 'shrink-0 rounded-lg px-2 py-1 text-xs ' +
    (conflict ? 'bg-amber-900/50 text-amber-300 hover:bg-amber-900/70'
              : 'bg-emerald-900/50 text-emerald-300 hover:bg-emerald-900/70');
  el.title = conflict
    ? T('chat.proj_conflict','Changes are blocked: files changed in the real folder since import. Re-import or discard your edits (Admin → Project).')
    : T('chat.proj_pending','Pending project changes — click to review and apply');
}

// Clicking the chip opens a review card rather than applying straight away, so the user
// always sees the file list before anything is written outside their workspace.
async function projectChipClicked(){
  const msgs = document.getElementById('messages');
  if(!msgs) return;
  await loadProjectSummary();
  const d = PROJ.summary || {};
  const T = (k,fb)=> (window.I18N && I18N.t(k)) || fb;
  const card = document.createElement('div');
  card.className = 'bg-slate-800/60 border border-slate-600 rounded-xl p-4 max-w-[85%] text-sm space-y-2';

  if(!d.linked){ card.remove(); return; }
  const conflicts = d.conflicts || [];
  const will = d.will_write || [];
  let inner = `<div class="font-medium">${esc(T('chat.proj_review','Project changes'))} · ${esc(d.prefix||'project')}/</div>`;
  inner += `<div class="text-xs text-slate-300">+${d.added||0} new · ~${d.modified||0} modified · ${conflicts.length} conflict(s) · ${d.unchanged||0} unchanged</div>`;
  if(will.length){
    inner += `<ul class="text-xs text-slate-400 max-h-40 overflow-auto list-disc pl-4">` +
             will.slice(0,60).map(p=>`<li><code>${esc(p)}</code></li>`).join('') +
             (will.length>60?`<li>… ${will.length-60} more</li>`:'') + `</ul>`;
  }
  if(conflicts.length){
    inner += `<div class="text-xs text-amber-300">${esc(T('chat.proj_blocked','Blocked — these changed in the real folder since import:'))}<ul class="list-disc pl-4 mt-1">`+
             conflicts.slice(0,12).map(c=>`<li>${esc(c)}</li>`).join('')+`</ul>
             <div class="mt-1 text-slate-400">${esc(T('chat.proj_blocked_hint','Re-import (Admin → Project) to adopt the folder’s version, or undo your edits. Nothing can be applied while a conflict exists.'))}</div></div>`;
  }
  if(!d.apply_enabled){
    inner += `<div class="text-xs text-amber-300">${esc(T('chat.proj_disabled','Apply is unavailable on this system (no write backend, or workspace encryption at rest is on).'))}</div>`;
  }
  if(!will.length && !conflicts.length){
    inner += `<div class="text-xs text-slate-400">${esc(T('chat.proj_nothing','Nothing to apply.'))}</div>`;
  }
  inner += `<div class="flex gap-2 pt-1">
      <button data-papply class="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs ${((!will.length||!d.apply_enabled)?'opacity-50 pointer-events-none':'')}">${esc(T('chat.proj_apply','Apply to folder'))}</button>
      <button data-pclose class="rounded-lg bg-slate-700 px-3 py-1.5 text-xs">${esc(T('chat.dismiss','Dismiss'))}</button></div>`;
  card.innerHTML = inner;
  msgs.appendChild(card); msgs.scrollTop = msgs.scrollHeight;

  card.querySelector('[data-pclose]').onclick = ()=>card.remove();
  card.querySelector('[data-papply]').onclick = async (ev)=>{
    const b = ev.currentTarget;
    if(!confirm(`${T('chat.proj_confirm','Write these changes to the real folder?')} `+
                `${d.added||0} new, ${d.modified||0} modified. ${T('chat.proj_confirm2','Overwritten files are backed up first.')}`)) return;
    b.disabled = true; b.textContent = T('chat.proj_writing','Writing…');
    try {
      const r = await api('/api/project/apply', {method:'POST', body:'{}'});
      const res = await r.json();
      const line = document.createElement('div');
      line.className = 'text-xs ' + (res.ok ? 'text-emerald-400' : 'text-red-400');
      line.textContent = (res.note || (res.detail || 'Failed')) +
        (res.backup_id ? ` — ${T('chat.proj_backup','backup')}: ${res.backup_id}` : '');
      card.appendChild(line);
      await loadProjectSummary();
      b.remove();
    } catch(e){
      b.disabled=false; b.textContent = T('chat.proj_apply','Apply to folder');
      const line=document.createElement('div'); line.className='text-xs text-red-400';
      line.textContent = T('chat.proj_failed','Apply failed.'); card.appendChild(line);
    }
  };
}
