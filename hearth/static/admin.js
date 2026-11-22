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
  // A `str` holding a path on the machine running Hearth gets Browse: the browser's own
  // file dialog cannot supply a server path, so this is the only way to avoid typing
  // /home/…/build/bin/llama-server by hand. Rule comes from the server (spec.is_path).
  const browse = (s.type==='str' && s.is_path)
    ? `<button type="button" data-browse="${s.key}" title="Choose a file or folder on the machine running Hearth"
         class="shrink-0 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-2 py-1 text-xs">📁 Browse</button>` : '';
  const inputWrap = browse ? `<div class="flex items-stretch gap-1.5 min-w-0">${input}${browse}</div>` : input;
  return `<div class="grid grid-cols-1 md:grid-cols-3 gap-2 py-3 border-b border-slate-800 items-start">
    <div><label class="text-sm font-medium">${s.label} ${reload}</label></div>
    <div>${inputWrap}</div>
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

// Browse buttons are built inside HTML strings, so they can only be wired once the
// string is in the DOM. Rather than make every tab remember, the tab dispatcher
// wires them after EVERY render (see the .tab click handler near TABS) — which also
// covers the Project tab's hand-built folder box.
function wireBrowseButtons(root){
  (root||document).querySelectorAll('[data-browse]').forEach(b=>{
    b.onclick = ()=> openPathBrowser(b.dataset.browse, b);
  });
}

// The path a Browse dialog should open at: the current value's folder if there is
// one, otherwise nothing (the server then offers drives / filesystem root).
// Passing the whole value is deliberate — the server resolves a file to its parent,
// so "start where I left off" works whether the box holds a .gguf or a folder.
// `__proj_target` is the Project tab's hand-built folder box, which has no setting
// key; it is addressed by element id instead.
function browseStartValue(key){
  const el = key === '__proj_target' ? $('projTarget')
                                  : document.querySelector(`[data-key="${key}"]`);
  return (el && typeof el.value === 'string') ? el.value.trim() : '';
}

async function renderModel(){
  // Both backends in one listing. /api/admin/models returns the chat model plus the audio
  // captioner, each with its own reachable/configured/hint fields, so a second loaded model
  // can never be invisible here (the old panel could only ever speak about one).
  let models = null;
  try {
    const d = await (await api('/api/admin/models')).json();
    models = d.models || [];
  } catch(e){ models = null; }
  // Fall back to the single-model endpoint if the combined one is unavailable, so a fault in
  // the captioner probe can never leave the Model tab blank.
  const st = models && models.length ? models[0]
        : await (await api('/api/admin/model/status')).json();
  const capModel = (models || []).find(m=>m.id==='captioner') || null;

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

  // ---- Loaded models: every backend Hearth can talk to, side by side --------
  // The point of this block is honesty about two things the tiles above cannot express:
  // which models exist at all, and whether each is CONFIGURED or actually RUNNING. A
  // captioner that is set up but not started must look different from one that was never
  // configured, because the fix is a different click in each case.
  function modelRow(m){
    const isCap = m.id === 'captioner';
    const isDif = m.kind === 'diffusion' || m.id === 'diffusion';
    // A diffusion "server" is never resident — sd-cli runs per call and exits — so
    // `reachable` (its binary exists) must not read as "running". Say what is true, or an
    // admin hunts for a process that is supposed to come and go.
    const live = isDif ? false : !!(m.reachable ?? m.server_reachable);
    const cfgd = !!(m.configured ?? m.paths_configured);
    const disabled = (isCap || isDif) && m.enabled === false;
    const state = disabled ? 'off'
                  : isDif ? (cfgd ? 'ready (runs per call)' : 'not configured')
                  : (live ? 'running' : (cfgd ? 'configured, not running' : 'not configured'));
    const col = disabled ? 'text-slate-500' : live ? 'text-emerald-400'
                  : isDif && cfgd ? 'text-emerald-400'
                  : cfgd ? 'text-amber-400' : 'text-slate-400';
    // What is loaded, from the server's own /props — not what Admin says is configured.
    const loaded = m.loaded_model || m.model_path || '';
    const short = loaded.split('/').pop();
    return `<div class="rounded-lg border ${live?'border-emerald-700/50 bg-emerald-900/10':'border-slate-700 bg-slate-800/30'} p-3">
      <div class="flex items-center gap-2 flex-wrap">
        <span aria-hidden="true" class="${col}">${live?'●':disabled?'◌':(isDif&&cfgd?'◆':'○')}</span>
        <span class="font-medium text-sm">${esc(m.role||m.id)}</span>
        <span class="text-[11px] ${col}">${esc(state)}</span>
        ${(isCap||isDif) && m.backend ? `<span class="text-[11px] px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 font-mono">${esc(m.backend)}</span>`:''}
        ${isCap && m.cpu_only ? `<span class="text-[11px] px-1.5 py-0.5 rounded bg-sky-900/40 border border-sky-800 text-sky-300" title="weights stay in system RAM">CPU offload</span>`:''}
        ${isCap && m.mmproj_path ? `<span class="text-[11px] px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-300" title="multimodal projector set">mmproj</span>`:''}
        ${isDif ? `<button class="tab text-[11px] px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-sky-300 hover:bg-slate-700" data-tab="images">Images settings →</button>`:''}
      </div>
      <div class="mt-1 text-[11px] text-slate-400 break-all">${short?esc(short):'<span class=\'text-slate-600\'>no model path set</span>'}</div>
      <div class="mt-0.5 text-[11px] text-slate-500 flex flex-wrap gap-x-3">
        ${m.base_url?`<span title="endpoint">${esc(m.base_url)}</span>`:''}
        ${m.n_ctx?`<span title="context window">ctx ${esc(String(m.n_ctx))}</span>`:''}
        ${isCap && !m.capabilities?.ffmpeg?'<span class="text-amber-400" title="no ffmpeg: files are sent unmodified, long recordings are not split">no ffmpeg</span>':''}
      </div>
      ${(isDif && m.hint)?`<div class="mt-1 text-[11px] text-amber-300/90">⚠ ${esc(String(m.hint).slice(0,240))}</div>`:''}
      ${(isCap && m.hint)?`<div class="mt-1 text-[11px] text-amber-300/90">⚠ ${esc(String(m.hint).slice(0,240))}</div>`:''}
    </div>`;
  }

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

    <h3 class="font-medium text-sm mb-2 text-slate-200">Loaded models</h3>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-2 mb-4 text-sm" id="loadedModels">
      ${models ? models.map(modelRow).join('') : modelRow({id:'chat', role:'Main chat model', ...st})}
    </div>
    <div class="-mt-2 mb-4 flex flex-wrap items-center gap-2 text-xs">
      <button id="startCap" class="${BTN} bg-emerald-700 hover:bg-emerald-600 text-xs px-3 py-1.5">▶ Start captioner</button>
      <button id="restartCap" class="${BTN} bg-amber-700 hover:bg-amber-600 text-xs px-3 py-1.5">⟳ Restart captioner</button>
      <button id="stopCap" class="${BTN} bg-slate-700 hover:bg-slate-600 text-xs px-3 py-1.5">■ Stop captioner</button>
      <button id="testCap" class="${BTN} bg-sky-800 hover:bg-sky-700 border border-sky-700 text-xs px-3 py-1.5"
              title="Caption a 2-second test tone to verify the whole chain (ffmpeg → audio encoder → description).">🔊 Test captioner</button>
      <span class="text-slate-500">the captioner is a second, independent model — starting or stopping it never touches the chat model</span>
    </div>
    <div id="capTest" class="hidden mb-4 rounded-lg border border-slate-700 bg-black/40 p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-48 overflow-auto"></div>

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
    renderModel().then(wireBrowseButtons);
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
      setTimeout(()=>renderModel().then(wireBrowseButtons), 900);
    }, {status:'modelBusy', elapsed:elapsedMsg, detail:detailMsg, keepElapsed:1000});
  };
  wireOp($('startModel'), '/api/admin/model/start', {},
      'loading weights can take a few minutes — watch runtime/llama-server.log',
      'Starting llama-server', 'Starting…');
  wireOp($('restartModel'), '/api/admin/model/start', {restart:true},
      'reloads the model with the saved settings, give it a few minutes',
      'Restarting llama-server', 'Restarting…');
  wireOp($('stopModel'), '/api/admin/model/stop', {}, null, null, 'Stopping…');

  // Captioner controls — same single-flight/busy treatment as the chat model's, its own
  // endpoints, and a re-render so the Loaded-models row reflects the new truth.
  const capBtns = ['startCap','restartCap','stopCap'];
  const wireCap = (btn, path, body, label, detail)=>{
    if(!btn) return;
    btn.onclick = beginOp(btn, label, async ()=>{
      let out = null;
      try { out = await (await api(path,{method:'POST',body:JSON.stringify(body||{})})).json(); }
      catch(e){ out = {hint:String(e)}; }
      const box = $('capTest');
      if(box && out && out.hint){ box.classList.remove('hidden'); box.textContent = '⚠ '+out.hint; }
      setTimeout(()=>renderModel().then(wireBrowseButtons), 900);
    }, {elapsed:detail||'loading captioner weights', keepElapsed:1000});
  };
  wireCap($('startCap'), '/api/admin/captioner/start', {}, 'Starting captioner…',
          'loading the captioner model — can take minutes');
  wireCap($('restartCap'), '/api/admin/captioner/start', {restart:true}, 'Restarting captioner…',
          'reloading the captioner with the saved settings');
  wireCap($('stopCap'), '/api/admin/captioner/stop', {}, 'Stopping captioner…', null);
  const testBtn = $('testCap');
  if (testBtn) testBtn.onclick = beginOp(testBtn, 'Testing…', async ()=>{
    const box = $('capTest');
    let d = null;
    try { d = await (await api('/api/admin/captioner/test')).json(); }
    catch(e){ d = {ok:false, note:String(e)}; }
    if(box && d){
      box.classList.remove('hidden');
      box.textContent = (d.ok ? `✓ described in ${d.seconds}s (${d.backend}, ${d.chunks} chunk)\n\n`
                              : `✗ could not caption the test tone:\n`)
                      + (d.caption || d.note || '');
    }
  }, {elapsed:'captioning a 2-second test tone', keepElapsed:1000});

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
  const groups = [['tools-fs','Filesystem'],['tools-project','Linked project folder'],['tools-web','Web'],['tools-media','Media (PDF / video)'],['tools-audio','Audio'],['tools-image','Image generation & display'],['tools-sys','System & exec'],['tools-device','Device']];
  let html = `<p class="text-xs text-slate-400 mb-3">Each tool has its own enable switch and approval mode ('auto' runs silently, 'ask' shows an approve/deny card per call). Legacy coarse toggles stay as fallbacks for older stored settings.</p>`;
  for (const [g,label] of groups){
    const specs = SPECS.filter(s=>s.group===g);
    if(!specs.length) continue;
    html += `<h3 class="font-medium text-sm mt-4 mb-1">${label}</h3>` + fieldsSafe(specs);
    if (g==='tools-project'){
      html += `<div class="text-[11px] text-slate-500 mt-1">A linked folder is copied into the workspace under <code>project/</code>, where the ordinary file tools edit it. "Apply" is the only action that writes to the real folder — it always asks first, refuses if the user changed a file meanwhile, and backs up everything it overwrites. Link folders under <b>Project</b>.</div>`;
    }
    if (g==='tools-image'){
      html += `<div class="text-[11px] text-slate-500 mt-1">Needs the binary + model set under <b>Images</b>. Each call loads the model, draws/edits, and exits — no server, but a cold load costs time, which is why approval defaults to <code>ask</code>. Pictures land in <code>generated/</code> and are shown back to the model. <b>Generate</b> draws from text (Qwen Image); <b>Edit</b> transforms an existing picture by instruction (Qwen Image Edit) — a separate model slot, set up under Images.</div>` +
              `<div class="text-[11px] text-slate-500 mt-1"><b>Display media in chat</b> is the other half: it puts a file that already exists in the workspace on screen for the <i>user</i> (image, audio or video, with full screen and download). Read-only and approval <code>auto</code>, so switching it off only removes the ability to show things — nothing else breaks.</div>`;
    }
    if (g==='tools-device'){
      html += `<div class="text-[11px] text-slate-500 mt-1">Device tools need BOTH a helper URL above and the user's "Allow device access" button in the chat header — unless that requirement is switched off under Agent. User uploads and downloads are unaffected by either.</div>`;
    }
  }
  $('panel').innerHTML = html + `<button id="saveTools" class="mt-4 rounded-lg bg-sky-600 px-4 py-2 text-sm">Save</button>`;
  const allToolSpecs = SPECS.filter(s=>s.group.startsWith('tools'));
  $('saveTools').onclick = beginOp($('saveTools'), 'Saving…', ()=> saveSettings(allToolSpecs));
}

// ---- Audio captioner tab ----------------------------------------------------
// A second model with its own settings group, so it gets its own tab rather than being
// appended to the ~30-row Model tab. Rendered defensively: the status probe can fail (bad
// path, wedged port) and that must not blank the settings form, since fixing the paths is
// exactly what an admin needs to do when the probe fails.
async function renderAudio(){
  const specs = SPECS.filter(s=>s.group==='captioner');
  let cs = null;
  try {
    const d = await (await api('/api/admin/models')).json();
    cs = (d.models||[]).find(m=>m.id==='captioner') || null;
  } catch(e){ cs = null; }

  const live = !!(cs && cs.reachable);
  const ffmpegOk = !!(cs && cs.capabilities && cs.capabilities.ffmpeg);
  const on = !!(cs && cs.enabled);
  // The captioner has no binary setting of its own — it always runs the chat model's
  // llama-server (see captioner._main_bin), so this tile reports that shared path rather
  // than offering a second one to fill in.
  const binPath = (cs && cs.paths && cs.paths.bin) || '';
  const stateHtml = `
    <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-2 mb-4 text-sm">
      <div class="rounded-lg border p-2.5 ${live?'border-emerald-700/60 bg-emerald-900/20':'border-slate-700 bg-slate-800/40'}">
        <div class="stat-k">Captioner server</div>
        <div class="mt-1 flex items-center gap-2 font-medium ${live?'text-emerald-400':'text-slate-300'}">
          <span aria-hidden="true">${live?'●':'○'}</span>${live?'listening':'not running'}
        </div>
        <div class="text-[11px] text-slate-400 mt-0.5 truncate" title="${esc((cs&&cs.base_url)||'')}">${esc((cs&&cs.base_url)||'')}</div>
      </div>
      <div class="rounded-lg border p-2.5">
        <div class="stat-k">Switch</div>
        <div class="mt-1 font-medium ${on?'text-emerald-400':'text-slate-400'}">${on?'enabled':'disabled'}</div>
        <div class="text-[11px] text-slate-400 mt-0.5">route audio to it</div>
      </div>
      <div class="rounded-lg border p-2.5">
        <div class="stat-k">Backend</div>
        <div class="mt-1 font-medium">${esc((cs&&cs.backend)||'?')}</div>
        <div class="text-[11px] text-slate-400 mt-0.5">${(cs&&cs.backend)==='cli'?'reloads per caption':'weights stay loaded'}</div>
      </div>
      <div class="rounded-lg border p-2.5">
        <div class="stat-k">ffmpeg</div>
        <div class="mt-1 font-medium ${ffmpegOk?'text-emerald-400':'text-amber-400'}">${ffmpegOk?'available':'not found'}</div>
        <div class="text-[11px] text-slate-400 mt-0.5">resample + chunking</div>
      </div>
      <div class="rounded-lg border p-2.5 ${binPath?'':'border-amber-700/60 bg-amber-900/20'}">
        <div class="stat-k">llama-server binary</div>
        <div class="mt-1 font-medium ${binPath?'text-slate-200':'text-amber-400'}">${binPath?'shared with chat model':'not set'}</div>
        <div class="text-[11px] text-slate-400 mt-0.5 truncate" title="${esc(binPath||'set it in Admin → Model')}">${esc(binPath||'Admin → Model → llama-server binary path')}</div>
      </div>
    </div>
    ${(cs && cs.hint) ? `<div class="mb-4 rounded-lg border border-amber-700/60 bg-amber-900/20 p-3 text-xs text-amber-300">⚠ ${esc(cs.hint)}</div>`:''}
    ${(!ffmpegOk && cs) ? `<div class="mb-4 rounded-lg border border-slate-700 bg-slate-800/40 p-3 text-xs text-slate-300">
      Without <code>ffmpeg</code> Hearth sends recordings unmodified and cannot split long ones, so a 40-minute file goes to the model whole. Install it (Debian/Ubuntu: <code>sudo apt install ffmpeg</code>) for 16 kHz mono conversion and timed chunks.</div>`:''}`;

  const BTN = 'rounded-lg px-3 py-1.5 text-xs disabled:opacity-50 disabled:cursor-not-allowed';
  $('panel').innerHTML = `
    <p class="text-xs text-slate-400 mb-3">A second, independent llama.cpp model that listens to audio and describes it as text — voices, timbre, emotion, music, ambience — so the chat model can reason about a recording without needing audio support itself. Typically a Qwen-Omni-class MoE model; CPU offload is the default so it does not compete with the chat model for VRAM. It runs <strong>the same llama-server binary as the chat model</strong> (Admin → Model) — only its weights differ, so there is nothing to configure twice.</p>
    ${stateHtml}
    <div class="flex flex-wrap items-center gap-2 mb-4">
      <button id="saveAudio" class="${BTN} bg-sky-600 hover:bg-sky-500 text-sm px-4 py-2">Save</button>
      <button id="startCap2" class="${BTN} bg-emerald-700 hover:bg-emerald-600">▶ Start captioner</button>
      <button id="restartCap2" class="${BTN} bg-amber-700 hover:bg-amber-600">⟳ Restart</button>
      <button id="stopCap2" class="${BTN} bg-slate-700 hover:bg-slate-600">■ Stop</button>
      <button id="testCap2" class="${BTN} bg-sky-800 hover:bg-sky-700 border border-sky-700">🔊 Test with a tone</button>
    </div>
    <div id="modelBusy" class="hidden text-xs text-sky-300 mb-3"></div>
    <div id="capTest" class="hidden mb-4 rounded-lg border border-slate-700 bg-black/40 p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-48 overflow-auto"></div>
    ${fieldsSafe(specs)}
    <button id="saveAudio2" class="mt-4 rounded-lg bg-sky-600 px-4 py-2 text-sm">Save</button>`;

  const save = beginOp($('saveAudio'), 'Saving…', ()=> saveSettings(specs), {status:'modelBusy'});
  $('saveAudio').onclick = save;
  $('saveAudio2').onclick = beginOp($('saveAudio2'), 'Saving…', ()=> saveSettings(specs), {status:'modelBusy'});

  const wire = (btn, path, body, label)=>{
    if(!btn) return;
    btn.onclick = beginOp(btn, label, async ()=>{
      const b = $('modelBusy'); let out=null;
      try { out = await (await api(path,{method:'POST',body:JSON.stringify(body||{})})).json(); }
      catch(e){ out = {hint:String(e)}; }
      if(b && out && out.hint){ b.classList.remove('hidden'); b.textContent = '⚠ '+out.hint; }
      setTimeout(()=>renderAudio().then(wireBrowseButtons), 900);
    }, {elapsed:'loading captioner weights — can take minutes', keepElapsed:1000});
  };
  wire($('startCap2'), '/api/admin/captioner/start', {}, 'Starting…');
  wire($('restartCap2'), '/api/admin/captioner/start', {restart:true}, 'Restarting…');
  wire($('stopCap2'), '/api/admin/captioner/stop', {}, 'Stopping…');

  const t = $('testCap2');
  if (t) t.onclick = beginOp(t, 'Testing…', async ()=>{
    const box = $('capTest'); let d=null;
    try { d = await (await api('/api/admin/captioner/test')).json(); } catch(e){ d={ok:false,note:String(e)}; }
    if(box && d){
      box.classList.remove('hidden');
      box.textContent = (d.ok ? `✓ described in ${d.seconds}s (${d.backend}, ${d.chunks} chunk)\n\n`
                              : `✗ could not caption the test tone:\n`) + (d.caption||d.note||'');
    }
  }, {elapsed:'captioning a 2-second test tone', keepElapsed:1000});
}

// ---- Images tab (Qwen Image via stable-diffusion.cpp) ------------------------
// Two model slots share this tab and the same sd-cli binary/runner: Qwen Image for DRAWING
// and Qwen Image Edit for EDITING. Neither has a server (`sd-cli` runs per call and exits), so
// what an admin needs is whether each required file is present (one tile per file, since
// "model: missing" never said WHICH) and whether one run actually produces a PNG — which is
// what the test buttons prove. The two profiles are configured independently; only the CLI
// binary is shared (editing falls back to the generation path when its own is blank).
const IMG_TILE = (k, val, sub, col) => `
  <div class="rounded-lg border p-2.5 ${col==='ok'?'border-emerald-700/60 bg-emerald-900/20':'border-slate-700 bg-slate-800/40'}">
    <div class="stat-k">${esc(k)}</div>
    <div class="mt-1 flex items-center gap-2 font-medium ${col==='ok'?'text-emerald-400':col==='warn'?'text-amber-400':'text-slate-300'}">
      <span aria-hidden="true">${col==='ok'?'●':col==='warn'?'◍':'○'}</span>${esc(val)}
    </div>
    <div class="text-[11px] text-slate-400 mt-0.5 break-all">${sub}</div>
  </div>`;

// One tile per file a profile declares, named by role and saying WHERE it came from (an admin
// who set explicit paths then moved the files needs to know which source is stale). Roles in
// `required` that are missing read as amber; an unset OPTIONAL role (edit's llm_vision) shows
// grey "optional" rather than a warning, because leaving it blank is correct for most checkpoints.
function imgFileTiles(ds, required){
  const present = (ds && ds.present) || {};
  const paths = (ds && ds.paths) || {};
  const sources = (ds && ds.sources) || {};
  const labels = (ds && ds.labels) || {};
  const req = new Set(required || Object.keys(labels));
  return Object.keys(labels).map(role=>{
    const ok = !!present[role];
    const p = paths[role] || '';
    const isReq = req.has(role);
    const src = sources[role]==='folder' ? 'found in the model folder'
              : sources[role]==='setting' ? 'your explicit path'
              : sources[role]==='env' ? 'from the environment'
              : sources[role]==='legacy' ? 'from an older Hearth setting'
              : (isReq ? 'not set' : 'optional — not set');
    const col = ok ? 'ok' : (isReq ? 'warn' : '');
    const val = ok ? 'found' : (p ? 'missing' : (isReq ? 'not set' : 'optional'));
    return IMG_TILE(labels[role] || role, val,
                    p ? `<span title="${esc(p)}">${esc(p.split('/').pop())}</span> · ${esc(src)}`
                      : esc(src),
                    col);
  }).join('');
}

// Render one profile's status tiles + hint + action buttons. `cfg` carries the endpoint paths,
// the intro sentence, and unique element-id prefixes so both profiles can coexist in one panel.
function imgProfileBlock(cfg){
  const ds = cfg.ds || {};
  const binOk = !!(ds.capabilities && ds.capabilities.cli_present);
  const allOk = !!ds.configured;
  const on = !!ds.enabled;
  const busy = !!(ds.capabilities && ds.capabilities.busy);
  const BTN = 'rounded-lg px-3 py-1.5 text-xs disabled:opacity-50 disabled:cursor-not-allowed';
  const idp = cfg.idPrefix;
  return `
    <div class="grid grid-cols-2 md:grid-cols-3 gap-2 mb-3 text-sm">
      ${IMG_TILE('Switch', on?'enabled':'disabled', 'the tool is offered at all', on?'ok':'')}
      ${IMG_TILE('sd-cli binary', binOk?'found':'not found', cfg.binHint, binOk?'ok':'warn')}
      ${IMG_TILE('Now', busy?'busy':'idle', 'one run at a time (shared)', busy?'warn':'')}
    </div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-2 mb-4 text-sm">${imgFileTiles(ds, cfg.requiredRoles)}</div>
    ${(ds.hint) ? `<div class="mb-4 rounded-lg border border-amber-700/60 bg-amber-900/20 p-3 text-xs text-amber-300">⚠ ${esc(ds.hint)}</div>`:''}
    ${(ds.model_dir && !ds.hint) ? `<div class="mb-4 rounded-lg border border-emerald-800/50 bg-emerald-900/10 p-3 text-xs text-emerald-300">✓ Ready. Model folder: <span class="font-mono break-all">${esc(ds.model_dir)}</span></div>`:''}
    <div class="flex flex-wrap items-center gap-2 mb-4">
      <button id="${idp}Test" class="${BTN} bg-sky-800 hover:bg-sky-700 border border-sky-700" ${allOk?'':'title="Set the model files first"'}>${cfg.testLabel}</button>
      <button id="${idp}Copy" class="${BTN} bg-slate-800 hover:bg-slate-700 border border-slate-700">Copy the sd-cli command</button>
    </div>`;
}

async function renderImages(){
  const genSpecs = SPECS.filter(s=>s.group==='diffusion');
  const editSpecs = SPECS.filter(s=>s.group==='image_edit');
  // Ordered so each profile's settings read top-to-bottom in the order that matters:
  // enable → binary → folder → the model files → sampling → limits.
  const GEN_ORDER = ['diffusion.enabled','diffusion.cli_bin','diffusion.model_dir',
                     'diffusion.dit_path','diffusion.llm_path','diffusion.vae_path'];
  const EDIT_ORDER = ['image_edit.enabled','image_edit.cli_bin','image_edit.model_dir',
                      'image_edit.dit_path','image_edit.llm_path','image_edit.vae_path',
                      'image_edit.llm_vision_path','image_edit.zero_cond_t'];
  const byOrder = (specs, order) => specs.slice().sort((a,b)=>{
    const ai = order.indexOf(a.key), bi = order.indexOf(b.key);
    return (ai<0?99:ai)-(bi<0?99:bi);
  });

  let genDs=null, editDs=null;
  try { genDs = await (await api('/api/admin/diffusion/status')).json(); } catch(e){ genDs = null; }
  try { editDs = await (await api('/api/admin/image_edit/status')).json(); } catch(e){ editDs = null; }

  $('panel').innerHTML = `
    <p class="text-xs text-slate-400 mb-3">Hearth draws and edits pictures with stable-diffusion.cpp's <code>sd-cli</code>, run as a subprocess. There is no server to keep running: each call loads the model, works, and exits — so it never competes with the chat model for a port, but a cold load costs time. The finished image comes back to the chat model, which is how it can tell you what it actually made. Sampling flags (euler, flash attention) are fixed to values proven to work.</p>

    <h3 class="font-medium text-sm mt-2 mb-1">🎨 Image generation — Qwen Image / 2.1</h3>
    <p class="text-[11px] text-slate-500 mb-3">Draws from a text prompt. Needs the CLI path and three files (DiT + Qwen3-VL encoder + VAE).</p>
    ${imgProfileBlock({ds: genDs, idPrefix:'gen', requiredRoles:['dit','llm','vae'],
                       binHint:'the generation CLI path below', testLabel:'🖼 Draw a test image'})}

    <hr class="border-slate-700 my-5"/>

    <h3 class="font-medium text-sm mb-1">✂️ Image editing — Qwen Image Edit</h3>
    <p class="text-[11px] text-slate-500 mb-3">Edits an existing picture by instruction (add/remove/change, restyle, edit text) while keeping the rest. A separate model slot with its own files (Edit DiT + Qwen2.5-VL encoder + VAE; optional vision mmproj for 2509). Uses the same sd-cli binary — leave its CLI path blank to reuse the generation one.</p>
    ${imgProfileBlock({ds: editDs, idPrefix:'edit', requiredRoles:['dit','llm','vae'],
                       binHint:'blank reuses the generation CLI path', testLabel:'✂️ Test an edit'})}

    <div id="imgBusy" class="hidden text-xs text-sky-300 mb-3"></div>
    <div id="imgOut" class="hidden mb-4 rounded-lg border border-slate-700 bg-black/40 p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-48 overflow-auto"></div>

    ${fieldsSafe(byOrder(genSpecs, GEN_ORDER))}
    <hr class="border-slate-700 my-5"/>
    ${fieldsSafe(byOrder(editSpecs, EDIT_ORDER))}
    <button id="saveImg" class="mt-4 rounded-lg bg-sky-600 px-4 py-2 text-sm">Save</button>`;

  const allSpecs = genSpecs.concat(editSpecs);
  $('saveImg').onclick = beginOp($('saveImg'), 'Saving…', ()=> saveSettings(allSpecs), {status:'imgBusy'});

  // Generation test — draws a tiny picture.
  const gt = $('genTest');
  if (gt) gt.onclick = beginOp(gt, 'Drawing…', async ()=>{
    const box = $('imgOut'); let d=null;
    try { d = await (await api('/api/admin/diffusion/test',{method:'POST',body:'{}'})).json(); }
    catch(e){ d={ok:false, note:String(e)}; }
    if(box && d){
      box.classList.remove('hidden');
      box.textContent = (d.ok ? `✓ drew ${d.images} image(s) in ${d.seconds}s`
                              : '✗ could not generate:\n') + (d.note || JSON.stringify(d));
    }
  }, {elapsed:'loading Qwen Image and drawing a small test picture', keepElapsed:1000});

  const gc = $('genCopy');
  if (gc) gc.onclick = beginOp(gc, 'Loading…', async ()=>{
    const box = $('imgOut'); let d=null;
    try { d = await (await api('/api/admin/diffusion/command')).json(); } catch(e){ d={ok:false, note:String(e)};}
    if(box && d){ box.classList.remove('hidden'); box.textContent = d.ok ? d.command : ('⚠ '+(d.note||'no command')); }
  });

  // Edit test — edits a tiny synthetic image (needs Pillow on the server).
  const et = $('editTest');
  if (et) et.onclick = beginOp(et, 'Editing…', async ()=>{
    const box = $('imgOut'); let d=null;
    try { d = await (await api('/api/admin/image_edit/test',{method:'POST',body:'{}'})).json(); }
    catch(e){ d={ok:false, note:String(e)}; }
    if(box && d){
      box.classList.remove('hidden');
      box.textContent = (d.ok ? `✓ edited ${d.images} image(s) in ${d.seconds}s`
                              : '✗ could not edit:\n') + (d.note || JSON.stringify(d));
    }
  }, {elapsed:'loading Qwen Image Edit and editing a small test picture', keepElapsed:1000});

  const ec = $('editCopy');
  if (ec) ec.onclick = beginOp(ec, 'Loading…', async ()=>{
    const box = $('imgOut'); let d=null;
    try { d = await (await api('/api/admin/image_edit/command')).json(); } catch(e){ d={ok:false, note:String(e)};}
    if(box && d){ box.classList.remove('hidden'); box.textContent = d.ok ? d.command : ('⚠ '+(d.note||'no command')); }
  });
}

async function renderAgent(){  const specs = SPECS.filter(s=>s.group==='agent');
  // Uploads live in their own group so the size cap is not buried among agent levers;
  // it used to be a source constant with no admin surface at all (AUDIT #8).
  const upSpecs = SPECS.filter(s=>s.group==='uploads');
  $('panel').innerHTML = `
    <p class="text-xs text-slate-400 mb-3">How much leeway one chat turn gets. The tool-step cap is the main guard against a model that keeps calling tools instead of answering.</p>`
    + fieldsSafe(specs)
    + (upSpecs.length ? `<h3 class="font-medium text-sm mt-5 mb-1">Uploads</h3>
      <div class="text-[11px] text-slate-500 mb-2">Applies to every file attached in chat; the chat UI reads this limit rather than carrying its own copy.</div>`
      + fieldsSafe(upSpecs) : '')
    + `<button id="saveAgent" class="mt-4 rounded-lg bg-sky-600 px-4 py-2 text-sm">Save</button>`;
  $('saveAgent').onclick = beginOp($('saveAgent'), 'Saving…', ()=> saveSettings(specs.concat(upSpecs)));
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

const TABS = {model:renderModel, audio:renderAudio, images:renderImages, agent:renderAgent, tools:renderTools, project:renderProject, users:renderUsers, docs:renderDocs, log:renderLog, audit:renderAudit};

// Panel draws are awaited. They used to be fire-and-forget, which meant the click
// handler returned while the panel was still empty — and anything done after it (wiring
// Browse buttons) ran against a DOM that did not exist yet. It appeared to work only
// because renderModel() is fast enough on some tabs and never on Project/Audio/Images.
async function showTab(name){
  const fn = TABS[name];
  if (!fn) return;
  await loadSettings();
  await fn();
  wireBrowseButtons();   // needs the drawn panel, hence the awaits above
}

document.querySelectorAll('.tab').forEach(b=> b.onclick=async ()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('bg-slate-800'));
  b.classList.add('bg-slate-800');
  stopLogTimer();   // the log tab re-arms it; other tabs must not keep polling
  await showTab(b.dataset.tab);
});
(async()=>{ await showTab('model'); })();

// ---- Linked project folder (DEC-049) ---------------------------------------
// Admin-facing control for the one linked folder. The agent never uses this screen: it
// works on the copy under project/ with the normal file tools, and asks for approval to
// apply. Everything here is operator setup + an honest diff readout.
async function renderProject(){
  let st;
  try { st = await (await api('/api/admin/project')).json(); }
  catch(e){ $('panel').innerHTML = `<p class="text-sm text-red-400">Could not load project status: ${esc(e.message)}</p>`; return; }

  let html = `<p class="text-xs text-slate-400 mb-3">Link one real folder on the server. Hearth copies it into your workspace under <code>${esc(st.prefix||'project')}/</code>, where the assistant edits it with the normal file tools. Only an approved <b>apply</b> writes back — conflicts are refused, never merged, and every overwritten file is backed up first.</p>`;

  html += `<div class="flex flex-wrap gap-2 items-end mb-4">
    <label class="flex-1 min-w-[260px]"><span class="text-xs text-slate-400">Folder path (absolute, on the machine running Hearth)</span>
      <input id="projTarget" class="mt-1 w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2 text-sm" placeholder="/home/you/projects/myapp" value="${esc(st.target_abs||'')}"/></label>
    <button id="projBrowse" data-browse="__proj_target" class="rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-3 py-2 text-xs">📁 Browse</button>
    <button id="projLink" class="rounded-lg bg-sky-600 px-4 py-2 text-sm">Link</button>
    ${st.linked?`<button id="projImport" class="rounded-lg bg-emerald-700 px-4 py-2 text-sm">${st.files?'Re-import':'Import'}</button>
      <button id="projUnlink" class="rounded-lg bg-slate-700 px-4 py-2 text-sm">Unlink</button>`:''}
  </div><div id="projMsg" class="text-xs mb-3"></div>`;

  if (st.linked){
    const alive = st.exists ? '' : `<div class="text-xs text-red-400 mb-3">The linked folder is not reachable right now — re-link or check the path.</div>`;
    const enc = st.encryption_blocks_apply ? `<div class="text-xs text-amber-400 mb-3">Workspace encryption at rest is enabled, so apply-back is refused (it would write ciphertext into your project). Import and preview still work.</div>` : '';
    const stub = st.backend === 'stub' ? `<div class="text-xs text-amber-400 mb-3">No usable write backend was detected on this system, so apply is disabled. Copy-in and preview still work.</div>` : '';
    const counts = st.diff_error
      ? `<div class="text-xs text-red-400 mb-3">${esc(st.diff_error)}</div>`
      : `<div class="flex flex-wrap gap-2 text-xs mb-3">
           <span class="px-2 py-1 rounded bg-slate-800">${st.files||0} imported</span>
           <span class="px-2 py-1 rounded bg-emerald-900/40">+${st.added||0} new</span>
           <span class="px-2 py-1 rounded bg-sky-900/40">~${st.modified||0} modified</span>
           <span class="px-2 py-1 rounded ${ (st.conflicts||0) ? 'bg-red-900/50 text-red-300' : 'bg-slate-800'}">${st.conflicts||0} conflict(s)</span>
           <span class="px-2 py-1 rounded bg-slate-800">${st.unchanged||0} unchanged</span>
         </div>`;
    html += alive + enc + stub + counts;
    if (st.imported_at) html += `<div class="text-[11px] text-slate-500 mb-3">Last import: ${new Date(st.imported_at*1000).toLocaleString()}</div>`;

    if ((st.backups||[]).length){
      html += `<h3 class="font-medium text-sm mt-4 mb-1">Backups (each apply backs up what it overwrote)</h3><div class="space-y-1">`;
      for (const b of st.backups)
        html += `<div class="flex items-center gap-3 text-xs bg-slate-800/60 rounded px-3 py-2">
          <code class="flex-1">${esc(b.id)}</code><span class="text-slate-400">${b.files} file(s), ${fmtBytes(b.bytes)}</span>
          <button class="revertBtn rounded bg-slate-700 px-2 py-1" data-b="${esc(b.id)}">Revert to this</button></div>`;
      html += `</div><div class="text-[11px] text-slate-500 mt-1">Revert restores exactly the files that apply replaced.</div>`;
    }
  } else {
    html += `<div class="text-xs text-slate-500">No folder linked. Enter a path above and press Link, then Import.</div>`;
  }

  $('panel').innerHTML = html;
  const msg = (t, bad)=>{ const m=$('projMsg'); if(m){ m.textContent=t; m.className='text-xs mb-3 '+(bad?'text-red-400':'text-emerald-400'); } };

  if ($('projLink')) $('projLink').onclick = beginOp($('projLink'), 'Linking…', async ()=>{
    const r = await api('/api/admin/project/link',{method:'POST',body:JSON.stringify({target:$('projTarget').value})});
    const d = await r.json();
    if(!r.ok){ msg(d.detail||'Could not link that folder.', true); return; }
    msg(d.linked?'Linked. Now press Import to copy it in.':'Unlinked.'); renderProject().then(wireBrowseButtons);
  });
  if ($('projImport')) $('projImport').onclick = beginOp($('projImport'), 'Importing…', async ()=>{
    const r = await api('/api/admin/project/import',{method:'POST',body:'{}'});
    const d = await r.json();
    if(!r.ok){ msg(d.detail||'Import failed.', true); renderProject().then(wireBrowseButtons); return; }
    msg(`Imported ${d.copied} of ${d.files} file(s)` + (d.truncated?' (hit the size/file cap — raise it under Tools → Linked project folder)':''));
    renderProject().then(wireBrowseButtons);
  });
  if ($('projUnlink')) $('projUnlink').onclick = async ()=>{
    const del = confirm('Unlink this folder?\n\nOK also deletes the workspace copy (project/). Cancel keeps the copy so you can still read what the assistant changed.');
    await api('/api/admin/project/unlink',{method:'POST',body:JSON.stringify({delete_copy:del})});
    renderProject().then(wireBrowseButtons);
  };
  document.querySelectorAll('.revertBtn').forEach(b=> b.onclick = beginOp(b, 'Reverting…', async ()=>{
    const r = await api('/api/admin/project/revert',{method:'POST',body:JSON.stringify({backup_id:b.dataset.b})});
    const d = await r.json();
    msg(r.ok ? (d.note||'Reverted.') : (d.detail||'Revert failed.'), !r.ok);
    renderProject().then(wireBrowseButtons);
  }));
}

function fmtBytes(n){ if(n==null) return ''; if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(1)+' MB'; }

// ---- Server-side path browser (DEC-080) ------------------------------------
// Every binary/model setting asks for an absolute path on the machine running Hearth,
// and until now the only way to fill one in was to type or paste it. The browser's own
// file dialog cannot help: <input type=file> hands back a fake name inside the browser's
// sandbox, never a real server path — so this walks the *server's* directories instead
// (GET /api/admin/browse-path, admin-only and audited).
//
// It opens wherever the field already points: put the folder of the current value up, so
// choosing the sibling .gguf you meant is two clicks. That "start where I left off"
// behaviour is the whole point — a dialog that always began at / would just move the
// typing somewhere slightly nicer.
//
// PATHPICK.dir = the folder actually being shown; entries are kept in .all and the
// name filter runs CLIENT-side (see drawPathEntries). Sending `q` to the server would
// re-request the directory on every keystroke — slower, and it flickers when someone
// pauses mid-word. The endpoint still accepts q for direct/manual use.
let PATHPICK = null;   // {key, dir, all} while open; null when closed
let PATH_SEQ = 0;      // guards against an older reply landing after a newer one

async function openPathBrowser(key){
  const start = browseStartValue(key);   // the current value → its folder (DEC-080)
  closePathBrowser();
  PATHPICK = {key: key, dir: '', all: []};
  document.body.insertAdjacentHTML('beforeend', PATH_BROWSER_SHELL);
  wirePathBrowserShell();
  renderPathStatus('<span class="spinner"></span> Loading…');
  await loadPathBrowse(start || '');
}

// GET /api/admin/browse-path. `path` may be a folder, a file (the server lists its
// parent and says so in parent_note), empty (drives/root), or half-typed.
async function loadPathBrowse(path){
  if (!PATHPICK) return;
  const seq = ++PATH_SEQ;
  let d = null;
  try {
    d = await (await api(`/api/admin/browse-path?path=${encodeURIComponent(path||'')}`)).json();
  } catch(e){
    if (seq !== PATH_SEQ) return;   // superseded by a newer navigation
    renderPathStatus(`<div class="text-xs text-red-400 p-3">Could not read this folder: ${esc(String(e))}</div>`);
    return;
  }
  if (seq !== PATH_SEQ || !PATHPICK) return;   // a newer response already drew

  // Where "here" is: the server's parent_note when it redirected file→folder, else the
  // directory it listed. Everything else in the dialog (↑, filter) works off this.
  PATHPICK.dir = d.parent_note || (d.ok ? d.path : '');
  PATHPICK.all = d.entries || [];

  const note = $('pathPickNote');
  if (note) note.innerHTML = d.parent_note
    ? `Showing the folder containing <span class="font-mono">${esc(path)}</span>` : '';
  const addr = $('pathPickAddr');
  if (addr && document.activeElement !== addr) addr.value = PATHPICK.dir || '';
  const err = $('pathPickErr');
  if (err){
    if (d.error){ err.classList.remove('hidden'); err.textContent = '⚠ ' + d.error; }
    else err.classList.add('hidden');
  }
  drawPathEntries(d);
}

// Swap the list area for a transient message (loading / an error), or re-create the
// list container. #pathPickBody's innerHTML is replaced wholesale — it starts out with
// no #pathPickList at all, so drawing must always create it rather than assume one is
// there (an earlier version did, and every listing silently vanished into a null).
function renderPathStatus(html){
  const host = $('pathPickBody');
  if (!host) return;
  host.innerHTML = `<div class="text-xs">${html}</div>`;
}

function drawPathEntries(d){
  const host = $('pathPickBody');
  if (!host) return;
  let list = $('pathPickList');
  if (!list){
    host.innerHTML = '<div id="pathPickList" class="text-xs"></div>';
    list = $('pathPickList');
  }
  const f = ($('pathPickFilter') && $('pathPickFilter').value || '').trim().toLowerCase();
  const entries = f ? (PATHPICK.all || []).filter(e=> e.name.toLowerCase().includes(f))
                    : (PATHPICK.all || []);
  if (!entries.length){
    list.innerHTML = `<div class="text-xs text-slate-500 p-3">${
      f ? `Nothing here matches “${esc(f)}”.` : 'This folder is empty.'}</div>`;
    return;
  }
  const row = (e)=>{
    const sz = e.is_dir ? '' : `<span class="text-slate-500 shrink-0 ml-2">${fmtBytes(e.size)}</span>`;
    // Paths go through esc() for the visible text and encodeURIComponent for the
    // attribute payload (decoded back at click time) — esc() alone does not escape a
    // quote, so a filename containing one would otherwise break out of the attribute.
    const nav = encodeURIComponent(e.path);
    return `<button type="button" data-nav="${nav}" ${e.is_dir?'':`data-pick="${nav}"`}
      class="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-slate-800 border-b border-slate-800/60 text-xs font-mono">
      <span class="shrink-0 w-4 text-center ${e.is_dir?'text-sky-400':'text-slate-500'}">${e.is_dir?'📁':'·'}</span>
      <span class="flex-1 truncate" title="${esc(e.path)}">${esc(e.name)}</span>${sz}</button>`;
  };
  list.innerHTML = entries.map(row).join('') + (d.truncated
    ? `<div class="text-[11px] text-amber-400 p-3">Showing the first ${entries.length} of ${d.total} entries — `
      + `type in the filter above to narrow it.</div>` : '');
  // A folder navigates; a file picks itself and closes. One handler per row, bound
  // after every draw since the list is replaced wholesale.
  list.querySelectorAll('[data-nav]').forEach(b=>{
    b.onclick = ()=> loadPathBrowse(decodeURIComponent(b.dataset.nav));
  });
  list.querySelectorAll('[data-pick]').forEach(b=>{
    b.onclick = ()=> choosePath(decodeURIComponent(b.dataset.pick));
  });
}

// Write the chosen path into the settings box. Nothing is saved here — the value sits
// in the input exactly as if it had been typed, so the admin still presses Save like
// any other edit. That keeps one save path and one audit event instead of two.
function choosePath(p){
  if (!PATHPICK) return;
  const el = PATHPICK.key === '__proj_target' ? $('projTarget')
                                              : document.querySelector(`[data-key="${PATHPICK.key}"]`);
  if (el){
    el.value = p;
    el.focus();
    // A field the admin just changed should look changed: every tab's Save reads
    // live DOM values, so this is honest even though VALUES still holds the old one.
    el.classList.add('ring-1','ring-sky-500');
  }
  closePathBrowser();
}

function closePathBrowser(){
  PATHPICK = null;
  const shell = $('pathPickShell');
  if (shell) shell.remove();
  document.removeEventListener('keydown', pathBrowserKeys);
}

function pathBrowserKeys(ev){
  if (!PATHPICK) return;
  if (ev.key === 'Escape'){ ev.preventDefault(); closePathBrowser(); }
  // Enter in the address box means "go to that path"; a non-empty filter box just
  // applies itself immediately, so Enter there has nothing left to do.
  if (ev.key === 'Enter' && ev.target.id === 'pathPickAddr'){
    ev.preventDefault();
    const addr = $('pathPickAddr');
    loadPathBrowse(addr && addr.value.trim() ? addr.value : PATHPICK.dir);
  }
}

const PATH_BROWSER_SHELL = `
<div id="pathPickShell" class="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-3" role="dialog" aria-modal="true">
  <div class="w-full max-w-2xl max-h-[85vh] flex flex-col rounded-xl border border-slate-700 bg-slate-900 shadow-2xl">
    <div class="px-4 py-3 border-b border-slate-800">
      <div class="flex items-center gap-2">
        <h3 class="font-medium text-sm">Choose on the machine running Hearth</h3>
        <button type="button" id="pathPickClose" class="ml-auto rounded-lg bg-slate-800 hover:bg-slate-700 px-2.5 py-1 text-xs">✕ Close</button>
      </div>
      <p class="text-[11px] text-slate-400 mt-1">Folders open with a click; a file is chosen and the dialog closes. Nothing is saved until you press <b>Save</b>.</p>
      <div id="pathPickNote" class="text-[11px] text-sky-300 mt-1"></div>
      <div class="flex items-stretch gap-1.5 mt-2">
        <button type="button" id="pathPickUp" title="One folder up"
          class="rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-2.5 text-sm">↑</button>
        <button type="button" id="pathPickRoots" title="Filesystem root (drives on Windows)"
          class="rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-2.5 py-1 text-xs">⌂ root</button>
        <button type="button" id="pathPickHome" title="Home folder of the account running Hearth"
          class="rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-2.5 py-1 text-xs">⌂ ~</button>
        <input id="pathPickAddr" placeholder="/home/you/models — paste a path and press Enter"
          class="flex-1 min-w-0 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs font-mono"/>
      </div>
      <input id="pathPickFilter" placeholder="Filter names in this folder…"
        class="mt-1.5 w-full bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs"/>
      <div id="pathPickErr" class="hidden mt-1.5 rounded-lg border border-amber-700/60 bg-amber-900/20 p-2 text-[11px] text-amber-300"></div>
    </div>
    <div id="pathPickBody" class="flex-1 min-h-0 overflow-auto"></div>
  </div>
</div>`;

function wirePathBrowserShell(){
  $('pathPickClose').onclick = closePathBrowser;
  // Click the dim backdrop to dismiss, like every other dialog.
  $('pathPickShell').onclick = (ev)=>{ if (ev.target.id === 'pathPickShell') closePathBrowser(); };
  $('pathPickUp').onclick = ()=> loadPathBrowse(PATHPICK.dir ? parentDir(PATHPICK.dir) : '');
  $('pathPickRoots').onclick = ()=> loadPathBrowse('');
  // ~ is resolved by the SERVER (it is Hearth's home, not the browser machine's), via
  // pathbrowse.resolve()'s expanduser — so no client-side guesswork about who we are.
  $('pathPickHome').onclick = ()=> loadPathBrowse('~');
  // Filtering is local: re-draw the entries already in hand, no round trip.
  $('pathPickFilter').oninput = ()=>{ if (PATHPICK) drawPathEntries({entries: PATHPICK.all}); };
  document.addEventListener('keydown', pathBrowserKeys);
}

// Client-side "up one". The server already returns `parent`, but after a file→folder
// redirect the dialog's own notion of "where I am" is PATHPICK.dir, so trim that
// rather than re-deriving it from whatever the last response happened to say.
function parentDir(p){
  const s = String(p||'').replace(/[\\/]+$/,'');
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  if (i <= 0) return '';           // reached the top: let the server show drives/root
  return s.slice(0, i);
}
