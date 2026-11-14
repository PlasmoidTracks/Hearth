// Hearth admin controller. Renders settings with inline help text, model status,
// users CRUD, protocol/system-prompt docs, and audit log. CSRF handled by api().

const $ = (id)=>document.getElementById(id);
let SPECS = [], VALUES = {};

async function loadSettings(){ const r = await api('/api/admin/settings'); const d = await r.json(); SPECS=d.specs; VALUES=d.values; }

// ---- Async-button feedback -------------------------------------------------
// A button that fires a request must change *visibly* the instant it is pressed:
// waiting for the response and then re-rendering leaves several seconds (or, for a
// model load, minutes) of nothing, which reads as "the click was lost" and invites
// double-clicks. So every async control goes through these helpers: spinner +
// label swap + disabled while in flight, restored by the caller's next render.
function setBusy(btn, label){
  if(!btn) return;
  if(btn.dataset.busy === '1') return;   // re-entrant press: ignore, never stack
  btn.dataset.busy = '1';
  btn.dataset.label = btn.textContent;
  btn.disabled = true;
  btn.classList.add('cursor-not-allowed','opacity-60');
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span><span class="ml-2">${esc(label)}</span>`;
}

// Undo setBusy. Called on the paths that do NOT re-render the panel (the model
// panel's own buttons are instead rebuilt by renderModel, which is simpler than
// tracking both). Also clears aria-busy for screen readers.
function clearBusy(btn){
  if(!btn || btn.dataset.busy !== '1') return;
  btn.disabled = false;
  btn.classList.remove('cursor-not-allowed','opacity-60');
  btn.removeAttribute('aria-busy');
  if(btn.dataset.label != null) btn.textContent = btn.dataset.label;
  delete btn.dataset.busy;
}

// Run an async click handler with the button visibly busy, plus a live elapsed-time
// line so "nothing happened" is provably false. opts.elapsed = wording for that
// readout (shown immediately, then ticked every second); opts.detail = an extra
// explanatory line; opts.keepElapsed = how long it stays after the request settles
// (the model panel re-renders ~0.9 s later, which replaces it either way).
function beginOp(btn, label, fn, opts = {}){
  return async ()=>{
    if(!btn || btn.dataset.busy === '1') return;   // single-flight guard
    setBusy(btn, label);
    btn.setAttribute('aria-busy','true');
    const started = Date.now();
    let tick = null;
    const el = opts.status ? $(opts.status) : null;
    if(el && opts.elapsed){
      el.classList.remove('hidden');
      const paint = ()=>{
        el.textContent = `${opts.elapsed} (${Math.round((Date.now()-started)/1000)} s elapsed)`
                       + (opts.detail ? ` — ${opts.detail}` : '');
      };
      paint(); tick = setInterval(paint, 1000);
    }
    try { await fn(); }
    finally {
      const settle = ()=>{ if(tick){ clearInterval(tick); tick = null; } };
      if(opts.keepElapsed){ setTimeout(settle, opts.keepElapsed); } else { settle(); clearBusy(btn); }
    }
  };
}

function field(s){
  const v = VALUES[s.key];
  let input;
  if (s.type==='bool') input = `<input type="checkbox" data-key="${s.key}" ${v?'checked':''} class="accent-sky-500"/>`;
  else if (s.type==='choice') input = `<select data-key="${s.key}" class="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-sm">${s.choices.map(c=>`<option ${c===v?'selected':''}>${c}</option>`).join('')}</select>`;
  else if (s.type==='str') input = `<input type="text" data-key="${s.key}" value="${v??''}" class="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1 text-sm font-mono"/>`;
  else input = `<input type="${s.type==='int'||s.type==='float'?'number':'text'}" data-key="${s.key}" value="${v??''}" class="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-sm w-40"/>`;
  const range = (s.min!=null||s.max!=null)?` <span class="text-slate-500">[range ${s.min??'-∞'}…${s.max??'∞'}]</span>`:'';
  const reload = s.requires_reload?` <span class="text-amber-400" title="requires model reload">⟳</span>`:'';
  return `<div class="grid grid-cols-1 md:grid-cols-3 gap-2 py-3 border-b border-slate-800 items-start">
    <div><label class="text-sm font-medium">${s.label} ${reload}</label></div>
    <div>${input}</div>
    <div class="text-xs text-slate-400 leading-relaxed">${s.description}${range}</div>
  </div>`;
}

// Defensive: a single broken spec must never blank the whole panel — render what
// works and show the offender inline instead.
function fieldsSafe(specs){
  let html = '';
  for (const s of specs){
    try { html += field(s); }
    catch(e){ html += `<div class="py-2 text-xs text-red-400 border-b border-slate-800">⚠ could not render setting <code>${esc(s&&s.key||'?')}</code>: ${esc(String(e))}</div>`; }
  }
  return html;
}

async function renderModel(){
  const st = await (await api('/api/admin/model/status')).json();
  const modelSpecs = SPECS.filter(s=>s.group==='model');   // 'generation' is its own section below
  const running = st.proc_running || st.server_reachable;   // controls Restart+Stop vs Start
  // Settings arrive as one flat 'model' group; present them under headings so a
  // long list is navigable instead of a wall of identical rows. Anything not
  // matched below still renders (in OTHERS) — a new spec must never silently
  // disappear from the panel because its name was not listed here.
  const SECTIONS = [
    ['Server & model paths', s=>/_path$|server_bin/.test(s.key)],
    ['Performance tuning',   s=>['threads','batch_size','ubatch_size'].some(k=>s.key.endsWith('.'+k))],
    ['Memory / fit',         s=>['kv_cache_type','device','load_mode'].some(k=>s.key.endsWith('.'+k))],
    ['Reasoning / thinking', s=>s.key.endsWith('.reasoning')],
  ];
  // Catch-all: anything not claimed by a section above stays visible.
  const FALLBACK = ['Backend & context', s=>!SECTIONS.some(([,f])=>f(s))];
  const GEN_LABEL = 'Generation (applies immediately — no restart)';

  // Status as separate tiles, not one sentence of key·value pairs separated by
  // middots: at phone width that line wrapped unpredictably and every value
  // carried the same visual weight, so nothing could be scanned at a glance.
  const on = !!st.server_reachable;
  const stateTile = `<div class="rounded-lg border p-2.5 ${on?'border-emerald-700/60 bg-emerald-900/20':'border-slate-700 bg-slate-800/40'}">
      <div class="stat-k">Server</div>
      <div class="mt-1 flex items-center gap-2 font-medium ${on?'text-emerald-400':'text-slate-300'}">
        <span aria-hidden="true">${on?'●':'○'}</span>${on ? 'running' : 'stopped'}
      </div>
      <div class="text-[11px] text-slate-400 mt-0.5 truncate" title="${esc(st.base_url||'')}">${esc(st.base_url||'')}</div>
    </div>`;
  const effectiveTile = `<div class="rounded-lg border p-2.5">
      <div class="stat-k">Chats use</div>
      <div class="mt-1 font-medium ${st.effective==='stub'?'text-amber-400':'text-emerald-400'}">${esc(st.effective)}</div>
      <div class="text-[11px] text-slate-400 mt-0.5">Backend: ${esc(st.backend)}</div>
    </div>`;
  const ctxTile = `<div class="rounded-lg border p-2.5">
      <div class="stat-k">Context</div>
      <div class="mt-1 font-medium tabular-nums">${st.n_ctx}</div>
      <div class="text-[11px] text-slate-400 mt-0.5">${st.slot_n_ctx && st.slot_n_ctx !== st.n_ctx ? `per slot ${st.slot_n_ctx}` : 'per slot = total'}</div>
    </div>`;
  const pathsTile = `<div class="rounded-lg border p-2.5">
      <div class="stat-k">Paths</div>
      <div class="mt-1 font-medium ${st.paths_configured?'text-emerald-400':'text-red-400'}">${st.paths_configured?'set':'missing'}</div>
      <div class="text-[11px] text-slate-400 mt-0.5">binary + .gguf</div>
    </div>`;
  const visionTile = `<div class="rounded-lg border p-2.5">
      <div class="stat-k">Vision</div>
      <div class="mt-1 font-medium ${st.vision?'text-emerald-400':'text-slate-400'}">${st.vision?'mmproj set':'text-only'}</div>
      <div class="text-[11px] text-slate-400 mt-0.5">image attachments</div>
    </div>`;

  // One obvious control instead of a "Reload" that quietly did nothing: Start
  // launches llama-server with the saved paths, Restart relaunches it so changed
  // settings (n_ctx, GPU layers…) apply. Both can take minutes while weights load.
  const BTN = 'rounded-lg px-4 py-2 text-sm disabled:opacity-50 disabled:cursor-not-allowed';
  let btns = `<button id="startModel" class="${BTN} bg-emerald-600 hover:bg-emerald-500">▶ Start llama-server</button>`;
  if (running){
    btns = `<button id="restartModel" class="${BTN} bg-amber-600 hover:bg-amber-500">⟳ Restart llama-server</button>
            <button id="stopModel" class="${BTN} bg-slate-700 hover:bg-slate-600">■ Stop</button>`;
  }
  // Sectioned settings. The Model tab is ~30 rows long, which is exactly why the
  // action buttons belonged at the top: rendered after the fields, they were off
  // screen on any phone. Build each group's markup here, then splice them into the
  // panel body below.
  const renderSection = (title, specs)=> specs.length
    ? `<h3 class="font-medium text-sm mt-5 mb-1 text-slate-200">${title}</h3>` + fieldsSafe(specs)
    : '';
  let sectionsHtml = renderSection(FALLBACK[0], modelSpecs.filter(FALLBACK[1]));
  for (const [label, pred] of SECTIONS) sectionsHtml += renderSection(label, modelSpecs.filter(pred));
  const genSpecs = SPECS.filter(s=>s.group==='generation');
  sectionsHtml += `<h3 class="font-medium text-sm mt-5 mb-1 text-slate-200">${GEN_LABEL}</h3>` + fieldsSafe(genSpecs);

  // "Server running but chats still use the stub" was a recurring trap: Start
  // only makes llama-server reachable — chats follow Backend. Warn loudly and
  // offer a one-click flip (save model.backend + refresh, same path Save uses).
  const backendMismatch = st.server_reachable && st.backend !== 'llama-server';
  $('panel').innerHTML = `
    <!-- Action bar pinned to the top of the scroll container (the document): the
         Model tab is ~30 rows, so buttons rendered after the fields were off screen
         on any phone. Tab strip sits at top:0 with z-20, so this starts below it. -->
    <div class="sticky top-[41px] md:top-[37px] z-10 -mx-4 px-4 py-2 -mt-4 mb-3 bg-slate-950/95 backdrop-blur border-b border-slate-800">
      <div class="flex flex-wrap items-center gap-2">
        <button id="saveModel" class="${BTN} bg-sky-600 hover:bg-sky-500">Save</button>
        ${btns}
        <button id="copyLaunchCmd" class="ml-auto rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-3 py-1.5 text-xs"
                title="Copy the exact llama-server command Hearth launches, to run it yourself and compare.">⧉ Copy launch command</button>
      </div>
      <div id="modelBusy" class="hidden text-xs text-sky-300 mt-2"></div>
      <pre id="launchCmd" class="hidden mt-2 text-[11px] font-mono leading-relaxed bg-black/50 border border-slate-800 rounded-lg p-2 overflow-auto whitespace-pre-wrap break-all select-all max-h-40"></pre>
    </div>

    <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-2 mb-4 text-sm">
      ${stateTile}${effectiveTile}${ctxTile}${pathsTile}${visionTile}
    </div>

    ${backendMismatch ? `<div class="mb-4 rounded-lg border border-amber-600 bg-amber-900/30 p-3 text-sm text-amber-300">
      ⚠ llama-server is <b>running</b>, but chats still use the built-in <b>stub</b>, because Backend is <code>${st.backend}</code>.
      <button id="useLlamaBackend" class="mt-1 rounded-lg bg-emerald-700 hover:bg-emerald-600 px-3 py-1 text-xs">Use llama-server now</button>
    </div>` : ''}
    ${st.hint?`<div class="mb-4 rounded-lg border border-amber-700/60 bg-amber-900/20 p-3 text-xs text-amber-300">⚠ ${esc(st.hint)}</div>`:''}

    <div id="modelSections">${sectionsHtml}</div>

    <div class="mt-4 flex flex-wrap items-center gap-3 rounded-xl bg-slate-900 border border-slate-800 p-3 text-xs text-slate-400">
      <span class="basis-full lg:basis-auto">Fill in the binary + model paths, press <b>Save</b>, then <b>Start llama-server</b> (both buttons are pinned to the top of this tab). Hearth launches and supervises it. Set Backend to <code>llama-server</code> so chats use it — with Backend=<code>stub</code> chats keep using the built-in fake model. If you already run your own server, point HEARTH_LLAMA_BASE_URL at it and Hearth will just use it.</span>
      <button id="toTop" class="rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-3 py-1.5 lg:ml-auto">↑ Back to top</button>
    </div>`;
  $('saveModel').onclick = beginOp($('saveModel'), 'Saving…', ()=> saveSettings(modelSpecs), {status:'modelBusy'});
  $('toTop').onclick = ()=> window.scrollTo({top:0, behavior:'smooth'});

  // One-click fix for the mismatch above: flip Backend and re-render. Settings
  // save immediately; chats pick up the real provider on their next message.
  const useLlama = $('useLlamaBackend');
  if (useLlama) useLlama.onclick = async ()=>{
    useLlama.disabled = true; useLlama.textContent = 'Switching…';
    try { await api('/api/admin/settings',{method:'POST',body:JSON.stringify({'model.backend':'llama-server'})}); }
    catch(e){ /* status refresh below shows the truth either way */ }
    renderModel();
  };

  // Long launch → immediate, visible feedback plus live status, then refresh. The
  // request itself can take minutes (weights), so the press must gray out the
  // button and say what is happening right away; beginOp also makes it single-flight
  // so a second click cannot queue a second launch.
  const wireOp = (btn, path, body, detailMsg, elapsedMsg, label)=>{
    if(!btn) return;
    btn.onclick = beginOp(btn, label, async ()=>{
      const b = $('modelBusy');
      // Disable the sibling controls too: starting while a stop is in flight (or
      // vice versa) would race the supervisor for no good reason.
      ['startModel','restartModel','stopModel'].forEach(id=>{ const e=$(id); if(e && e!==btn) e.disabled = true; });
      let out = null;
      try { const r = await api(path,{method:'POST',body:JSON.stringify(body||{})}); out = await r.json(); }
      catch(e){ out = {hint: String(e)}; }
      if(b){
        b.classList.toggle('text-emerald-300', !!out.server_reachable);
        b.classList.toggle('text-sky-300', !out.server_reachable);
        b.textContent = out.server_reachable ? '✓ llama-server is up.'
                                             : '⚠ ' + (out.hint || 'Not reachable.');
      }
      setTimeout(()=>renderModel(), 900);
    }, {status:'modelBusy', elapsed:elapsedMsg, detail:detailMsg, keepElapsed:1000});
  };
  wireOp($('startModel'), '/api/admin/model/start', {},
      'loading weights can take a few minutes — watch runtime/llama-server.log',
      'Starting llama-server', 'Starting…');
  wireOp($('restartModel'), '/api/admin/model/start', {restart:true},
      'reloads the model with the saved settings, give it a few minutes',
      'Restarting llama-server', 'Restarting…');
  wireOp($('stopModel'), '/api/admin/model/stop', {}, null, null, 'Stopping…');

  // Copy launch command: fetch the exact argv Hearth builds and show it in a
  // selectable box + copy to clipboard. Debug aid for "works elsewhere, not here".
  const copyBtn = $('copyLaunchCmd');
  if (copyBtn) copyBtn.onclick = beginOp(copyBtn, 'Loading…', async ()=>{
    const pre = $('launchCmd');
    let d = null;
    try { d = await (await api('/api/admin/model/command')).json(); }
    catch(e){ d = {ok:false, note:'Could not load command: '+e}; }
    if (!d || !d.ok){
      clearBusy(copyBtn);
      if (pre){ pre.classList.remove('hidden'); pre.textContent = (d && d.note) || 'No command available.'; }
      return;
    }
    const cmd = d.command;
    if (pre){ pre.classList.remove('hidden'); pre.textContent = cmd; }
    const prev = copyBtn.dataset.label || copyBtn.textContent;
    const done = (msg)=>{ clearBusy(copyBtn); copyBtn.textContent = msg; setTimeout(()=>{ copyBtn.textContent = prev; }, 1600); };
    // navigator.clipboard needs a secure context; LAN over plain HTTP is not one,
    // so fall back to selecting the text + execCommand('copy').
    try {
      await navigator.clipboard.writeText(cmd); done('✓ Copied');
    } catch(e){
      let ok = false;
      try {
        if (pre){ const sel = window.getSelection(); sel.removeAllRanges(); const r = document.createRange(); r.selectNodeContents(pre); sel.addRange(r); }
        ok = document.execCommand && document.execCommand('copy');
      } catch(_) { ok = false; }
      done(ok ? '✓ Copied' : '⚠ Select & copy');
    }
  });
}

async function renderTools(){
  const groups = [['tools-fs','Filesystem'],['tools-web','Web'],['tools-sys','System & exec'],['tools-device','Device']];
  let html = `<p class="text-xs text-slate-400 mb-3">Each tool has its own enable switch and approval mode ('auto' runs silently, 'ask' shows an approve/deny card per call). Legacy coarse toggles stay as fallbacks for older stored settings.</p>`;
  for (const [g,label] of groups){
    const specs = SPECS.filter(s=>s.group===g);
    if(!specs.length) continue;
    html += `<h3 class="font-medium text-sm mt-4 mb-1">${label}</h3>` + fieldsSafe(specs);
    if (g==='tools-device'){
      html += `<div class="text-[11px] text-slate-500 mt-1">Device tools need BOTH a helper URL above and the user's "Allow device access" button in the chat header — unless that requirement is switched off under Agent. User uploads and downloads are unaffected by either.</div>`;
    }
  }
  $('panel').innerHTML = html + `<button id="saveTools" class="mt-4 rounded-lg bg-sky-600 px-4 py-2 text-sm">Save</button>`;
  const allToolSpecs = SPECS.filter(s=>s.group.startsWith('tools'));
  $('saveTools').onclick = beginOp($('saveTools'), 'Saving…', ()=> saveSettings(allToolSpecs));
}

async function renderAgent(){
  const specs = SPECS.filter(s=>s.group==='agent');
  $('panel').innerHTML = `
    <p class="text-xs text-slate-400 mb-3">How much leeway one chat turn gets. The tool-step cap is the main guard against a model that keeps calling tools instead of answering.</p>`
    + fieldsSafe(specs)
    + `<button id="saveAgent" class="mt-4 rounded-lg bg-sky-600 px-4 py-2 text-sm">Save</button>`;
  $('saveAgent').onclick = beginOp($('saveAgent'), 'Saving…', ()=> saveSettings(specs));
}

async function saveSettings(specs){
  const body = {};
  for (const s of specs){
    const el = document.querySelector(`[data-key="${s.key}"]`); if(!el) continue;
    body[s.key] = s.type==='bool' ? el.checked : el.value;
  }
  const r = await api('/api/admin/settings',{method:'POST',body:JSON.stringify(body)});
  const d = await r.json();
  alert(d.needs_reload ? 'Saved. Some model settings need a Reload to take effect.' : 'Saved.');
}

async function renderUsers(){
  const users = await (await api('/api/admin/users')).json();
  $('panel').innerHTML = `
    <div class="mb-4 flex flex-wrap gap-2 text-sm">
      <input id="nu_name" placeholder="username" autocapitalize="none" class="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 md:py-1"/>
      <input id="nu_pw" placeholder="password (>=8)" type="password" autocomplete="new-password" class="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 md:py-1"/>
      <label class="flex items-center gap-1 text-xs"><input type="checkbox" id="nu_admin"/>admin</label>
      <button id="addUser" class="rounded-lg bg-sky-600 px-3 py-1">Add</button>
    </div>
    <table class="w-full text-sm"><thead><tr class="text-slate-400 text-left"><th>User</th><th>Role</th><th>Status</th><th>2FA</th><th></th></tr></thead>
    <tbody>${users.map(u=>`<tr class="border-t border-slate-800">
      <td>${esc(u.username)}</td>
      <td>${u.is_admin?'admin':'user'}</td>
      <td>${u.is_active?'<span class="text-emerald-400">active</span>':'<span class="text-red-400">disabled</span>'}</td>
      <td>${u.has_totp?'on':'—'}</td>
      <td class="space-x-2 text-xs">
        <button data-toggle="${u.id}" class="underline">${u.is_active?'disable':'enable'}</button>
        <button data-admin="${u.id}" data-val="${!u.is_admin}" class="underline">${u.is_admin?'demote':'promote'}</button>
        <button data-pw="${u.id}" class="underline">reset pw</button>
      </td></tr>`).join('')}</tbody></table>`;
  $('addUser').onclick = async ()=>{ await api('/api/admin/users',{method:'POST',body:JSON.stringify({username:$('nu_name').value,password:$('nu_pw').value,is_admin:$('nu_admin').checked})}); renderUsers(); };
  document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{await api(`/api/admin/users/${b.dataset.toggle}/toggle`,{method:'POST'});renderUsers();});
  document.querySelectorAll('[data-admin]').forEach(b=>b.onclick=async()=>{await api(`/api/admin/users/${b.dataset.admin}/set_admin`,{method:'POST',body:JSON.stringify({is_admin:b.dataset.val==='true'})});renderUsers();});
  document.querySelectorAll('[data-pw]').forEach(b=>b.onclick=async()=>{const p=prompt('New password');if(p)await api(`/api/admin/users/${b.dataset.pw}/reset_pw`,{method:'POST',body:JSON.stringify({password:p})});});
}

async function renderDocs(){
  const docs = await (await api('/api/admin/docs')).json();
  window.__docs = {}; // id -> doc, avoids embedding JSON in HTML attributes
  const protocols = docs.filter(d=>d.kind==='protocol'), prompts = docs.filter(d=>d.kind==='system_prompt');
  docs.forEach(d=>window.__docs[d.id]=d);
  const row = d => `<div class="border border-slate-800 rounded-lg p-3 mb-2">
     <div class="flex items-center gap-2 text-sm"><b>${esc(d.name)}</b>${d.is_default?'<span class="text-xs text-emerald-400">default</span>':''}
       <button data-edit="${d.id}" class="ml-auto underline text-xs">edit</button>
       <button data-del="${d.id}" class="underline text-xs text-red-400">del</button></div>
     <div class="text-[11px] text-slate-500 mt-1 whitespace-pre-wrap max-h-24 overflow-auto">${esc(d.content.slice(0,400))}</div></div>`;
  $('panel').innerHTML = `
    <div class="grid md:grid-cols-2 gap-6">
      <div><h3 class="font-medium mb-2">System prompts</h3>${prompts.map(row).join('')||'<p class=text-slate-500>none</p>'}
        <button data-new="system_prompt" class="mt-2 rounded-lg bg-sky-600 px-3 py-1 text-sm">+ New</button></div>
      <div><h3 class="font-medium mb-2">Protocols (.md)</h3>${protocols.map(row).join('')||'<p class=text-slate-500>none</p>'}
        <button data-new="protocol" class="mt-2 rounded-lg bg-sky-600 px-3 py-1 text-sm">+ New</button></div>
    </div>`;
  document.querySelectorAll('[data-new]').forEach(b=>b.onclick=()=>editDoc({kind:b.dataset.new,name:'',content:'',is_default:false,visible_to_users:true}));
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editDoc(window.__docs[b.dataset.edit]));
  document.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{await api(`/api/admin/docs/${b.dataset.del}/delete`,{method:'POST'});renderDocs();});
}

function editDoc(d){
  const isNew = !d.id;
  $('panel').innerHTML = `<div class="max-w-2xl space-y-3">
    <input id="d_name" value="${esc(d.name)}" placeholder="name" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"/>
    <textarea id="d_content" rows="16" placeholder="markdown…" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 font-mono text-sm">${esc(d.content)}</textarea>
    <label class="text-sm flex items-center gap-2"><input type="checkbox" id="d_default" ${d.is_default?'checked':''}/> set as default</label>
    <div class="flex gap-2"><button id="d_save" class="rounded-lg bg-sky-600 px-4 py-2 text-sm">Save</button>
      <button id="d_cancel" class="rounded-lg bg-slate-700 px-4 py-2 text-sm">Cancel</button></div></div>`;
  $('d_save').onclick = async ()=>{ await api('/api/admin/docs',{method:'POST',body:JSON.stringify({id:d.id,kind:d.kind,name:$('d_name').value,content:$('d_content').value,is_default:$('d_default')?$('d_default').checked:false,visible_to_users:true})}); renderDocs(); };
  $('d_cancel').onclick = renderDocs;
}

async function renderAudit(){
  const rows = await (await api('/api/admin/audit?limit=150')).json();
  $('panel').innerHTML = `<table class="w-full text-xs font-mono"><thead><tr class="text-slate-400 text-left"><th>time</th><th>user</th><th>action</th><th>detail</th></tr></thead>
    <tbody>${rows.map(r=>`<tr class="border-t border-slate-800 align-top"><td class="whitespace-nowrap pr-2">${new Date(r.ts*1000).toLocaleTimeString()}</td><td>${esc((r.user_id||'').slice(0,8))}</td><td>${esc(r.action)}</td><td class="text-slate-400 break-all">${esc(r.detail)}</td></tr>`).join('')}</tbody></table>`;
}

// ---- Server log (raw llama-server stdout/stderr) ---------------------------
// The launch log is the only place a failed/huge model load explains itself
// (VRAM errors, KV allocation, GGML asserts). Tails the file server-side
// (/api/admin/model/log reads the END), auto-refreshes while enabled, and
// keeps your scroll position unless you're already pinned to the bottom.
// One timer per page: switching tabs clears it (see the tab click handler), so
// re-entering this tab never stacks intervals.
let LOG_TIMER = null;
function stopLogTimer(){ if(LOG_TIMER){ clearInterval(LOG_TIMER); LOG_TIMER = null; } }
async function renderLog(){
  stopLogTimer();   // never stack intervals when re-entering the tab
  const wrap = $('panel');
  wrap.innerHTML = `
    <div class="mb-2 flex flex-wrap items-center gap-3 text-xs text-slate-400">
      <label class="flex items-center gap-1"><input type="checkbox" id="logAuto" checked/> auto-refresh (3 s)</label>
      <button id="logRefresh" class="rounded-lg bg-slate-800 hover:bg-slate-700 px-3 py-1">Refresh</button>
      <span id="logInfo"></span>
    </div>
    <pre id="logPre" class="text-[11px] font-mono leading-relaxed bg-black/50 border border-slate-800 rounded-xl p-3 overflow-auto whitespace-pre-wrap break-all max-h-[70vh]">loading…</pre>`;
  const draw = (d)=>{
    const pre = $('logPre'); if(!pre) return;
    if(!d.ok){ pre.textContent = d.note || 'Log unavailable.'; }
    else {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      pre.textContent = (d.truncated ? `[… ${Math.floor(d.offset/1024)} KB earlier lines not shown …]\n` : '') + d.text;
      if(atBottom) pre.scrollTop = pre.scrollHeight;
    }
    const info = $('logInfo');
    if(info) info.textContent = d.ok
      ? `${d.path} · ${(d.size/1024).toFixed(1)} KB${d.truncated?' (tail only)':''}`
      : '';
  };
  const fetchLog = async ()=>{
    try { draw(await (await api('/api/admin/model/log?tail=131072')).json()); }
    catch(e){ const pre=$('logPre'); if(pre) pre.textContent = 'Could not load log: '+e; }
  };
  await fetchLog();
  $('logRefresh').onclick = fetchLog;
  $('logAuto').onchange = ()=>{
    stopLogTimer();
    if($('logAuto').checked) LOG_TIMER = setInterval(fetchLog, 3000);
  };
  if($('logAuto').checked && !LOG_TIMER) LOG_TIMER = setInterval(fetchLog, 3000);
}

const TABS = {model:renderModel, agent:renderAgent, tools:renderTools, users:renderUsers, docs:renderDocs, log:renderLog, audit:renderAudit};
document.querySelectorAll('.tab').forEach(b=> b.onclick=async ()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('bg-slate-800'));
  b.classList.add('bg-slate-800');
  stopLogTimer();   // the log tab re-arms it; other tabs must not keep polling
  await loadSettings(); TABS[b.dataset.tab]();
});
(async()=>{ await loadSettings(); renderModel(); })();
