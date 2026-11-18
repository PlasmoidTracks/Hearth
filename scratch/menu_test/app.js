// Hearth client helpers: CSRF-aware fetch + SSE-over-POST reader.
// We use fetch() streaming (not EventSource) because our turn endpoint is POST
// and must send a CSRF header + JSON body. See DEC-010.

function getCookie(name) {
  const m = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
  return m ? decodeURIComponent(m[2]) : '';
}

async function api(path, opts = {}) {
  const headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
  if (!['GET', 'HEAD'].includes((opts.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = getCookie('hearth_csrf');
  }
  const r = await fetch(path, Object.assign({credentials: 'same-origin'}, opts, {headers}));
  // A 401 mid-turn (expired session) must not navigate away while we're streaming;
  // and AbortError never reaches here at all.
  if (r.status === 401 && !path.startsWith('/auth/')) { location.href = '/login'; return; }
  return r;
}

// Read an SSE stream from a POST response and dispatch parsed events.
// opts.signal: an AbortSignal — aborting it cancels the request, which makes the
// server see the disconnect and stop generating (see chat.js → S.abortCtrl).
async function streamPost(path, body, onEvent, opts = {}) {
  let r;
  try {
    r = await api(path, {method: 'POST', body: JSON.stringify(body), signal: opts.signal});
  } catch (e) {
    if ((e && e.name === 'AbortError') || opts.aborted) return;   // user pressed Stop
    onEvent({type: 'error', text: String(e)});
    return;
  }
  if (!r.ok || !r.body) {
    let msg = 'Request failed';
    try { const j = await r.json(); msg = j.error || j.detail || msg; } catch (e) {}
    onEvent({type: 'error', text: msg});
    return;
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch (e) {
      if ((e && e.name === 'AbortError') || opts.aborted) return;   // mid-stream Stop
      onEvent({type: 'error', text: String(e)});
      return;
    }
    if (chunk.done) break;
    buf += decoder.decode(chunk.value, {stream: true});
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunkStr = buf.slice(0, idx); buf = buf.slice(idx + 2);
      for (const line of chunkStr.split('\n')) {
        if (line.startsWith('data: ')) {
          try { onEvent(JSON.parse(line.slice(6))); } catch (e) {}
        }
      }
    }
  }
}

// Minimal, safe markdown-ish renderer: escape then apply bold/code/newlines.
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function mdLite(s) {
  s = esc(s);
  s = s.replace(/```([\s\S]*?)```/g, (_, c) => `<pre class="bg-black/40 rounded p-2 my-1 overflow-auto">${c}</pre>`);
  s = s.replace(/`([^`]+)`/g, '<code class="bg-black/30 px-1 rounded">$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\n/g, '<br/>');
  return s;
}


// ---- Per-user behavioral preferences -----------------------------------------
// Loaded from /auth/me at chat startup (see chat.js init) and saved via POST
// /api/prefs. Defaults below are what the UI does when a key is absent, so this
// object is always safe to read even before the server round-trip completes.
//
// IMPORTANT: plain top-level `var`/function declarations on purpose — never
// top-level const/let here. A script aborts entirely if an earlier statement
// throws, and let/const bindings would then stay in TDZ, so every later use in
// chat.js would die with "PREFS is not defined" (a real regression we shipped
// once). var/window bindings degrade to defaults instead of poisoning the page.
var PREF_DEFAULTS = {
  enter_sends: true,        // Enter sends; Shift+Enter newline. false inverts.
  auto_scroll: true,        // keep transcript pinned to bottom while streaming.
  show_timestamps: false,   // time chips on message bubbles.
};
var PREFS = Object.assign({}, PREF_DEFAULTS);

function applyPrefs(obj) {
  for (const k of Object.keys(PREF_DEFAULTS)) {
    if (obj && typeof obj[k] === 'boolean') PREFS[k] = obj[k];
  }
}

// Fire-and-save a pref change; returns a promise so callers can await if needed.
async function savePref(key, value) {
  PREFS[key] = !!value;
  try {
    await api('/api/prefs', { method: 'POST', body: JSON.stringify({ [key]: !!value }) });
  } catch (e) { /* keep the local value; next load refetches from server */ }
}
