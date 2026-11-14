// Harness for the Admin → Model panel (scratch/admin_model_harness.mjs).
// Loads hearth/static/admin.js in jsdom with a stubbed fetch, renders the Model
// tab, and asserts: action bar at the top, status tiles distinct, Start press
// visibly busy + elapsed counter, single-flight, re-render after response,
// back-to-top. Run from the project root:
//   node --experimental-strip-types scratch/admin_model_harness.mjs  (plain .mjs is fine)
import { JSDOM } from '/tmp/harness/node_modules/jsdom/lib/api.js';
import fs from 'fs';

const specs = JSON.parse(fs.readFileSync('scratch/specs.json', 'utf8'));
const STOPPED = {
  backend: 'stub', effective: 'stub', server_reachable: false, base_url: 'http://127.0.0.1:8081',
  proc_running: false, startable: true, managed_by_hearth: true, n_ctx: 262144, slot_n_ctx: 131072,
  paths_configured: false, vision: false, hint: 'No server binary + model path set.',
};

const dom = new JSDOM('<!doctype html><body><section id="panel"></section></body>', { runScripts: 'outside-only' });
const { window } = dom;

let releaseStart = null;   // lets a test hold the /model/start response open
let status = { ...STOPPED };
window.fetch = async (url, opts) => {
  const path = String(url);
  let body;
  if (path.endsWith('/api/admin/settings') && (opts?.method || 'GET') === 'GET') body = { specs, values: {} };
  else if (path.endsWith('/model/status')) body = status;
  else if (path.endsWith('/model/command')) body = { ok: true, command: 'llama-server -m x.gguf' };
  else if (path.endsWith('/api/admin/settings')) body = { ok: true, needs_reload: true };
  else if (path.endsWith('/model/start')) {
    await new Promise(r => { releaseStart = r; });
    // The server really is up by the time this responds — reflect that in status.
    status = { ...status, server_reachable: true, proc_running: true, backend: 'llama-server', effective: 'llama-server' };
    body = status;
  }
  else throw new Error('unstubbed ' + path);
  return { status: 200, ok: true, json: async () => body };
};
window.alert = () => {};

// admin.js calls api()/esc() from app.js as bare globals; expose both, then the
// controller, and hand back the entry points the test needs.
const bundle = `
  ${fs.readFileSync('hearth/static/app.js', 'utf8')}
  ${fs.readFileSync('hearth/static/admin.js', 'utf8')}
  ;globalThis.__R = {renderModel};
`;
window.eval(bundle);

let failed = 0;
const assert = (cond, msg) => { console.log((cond ? 'PASS  ' : 'FAIL  ') + msg); if (!cond) failed++; };

await window.__R.renderModel();
const panel = window.document.getElementById('panel');
const $ = id => window.document.getElementById(id);

// --- 1. action bar first ----------------------------------------------------
const bar = panel.firstElementChild;
assert(bar.className.includes('sticky'), 'action bar is the first element and sticky');
const ids = [...panel.querySelectorAll('button')].map(b => b.id).filter(Boolean);
console.log('      button order:', ids.join(', '));
assert(ids[0] === 'saveModel', 'Save is the first button in the panel');
assert(ids.includes('startModel'), 'Start present (server stopped)');
assert(!ids.includes('stopModel'), 'Stop hidden while the server is stopped');
const pos = $('modelSections').compareDocumentPosition(bar);
assert(!!(pos & window.Node.DOCUMENT_POSITION_PRECEDING), 'action bar precedes the settings fields');

// --- 2. status tiles --------------------------------------------------------
const tiles = [...panel.querySelectorAll('.stat-k')].map(e => e.textContent.trim());
console.log('      tiles:', tiles.join(' | '));
assert(tiles.length === 5 && JSON.stringify(tiles) === JSON.stringify(['Server', 'Chats use', 'Context', 'Paths', 'Vision']),
  'five labelled status tiles');

// --- 3. pressing Start is visible immediately -------------------------------
const start = $('startModel');
start.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const busy = $('modelBusy');
assert(start.disabled === true, 'Start disabled the instant it is clicked');
assert(!!start.querySelector('.spinner'), 'spinner rendered inside the pressed button');
assert(/Starting/.test(start.textContent), 'label swapped → "' + start.textContent.trim() + '"');
assert(!busy.classList.contains('hidden') && /s elapsed/.test(busy.textContent), 'elapsed counter visible → ' + busy.textContent);
assert(start.getAttribute('aria-busy') === 'true', 'aria-busy set for screen readers');

// re-entrant click must not fire a second request
let calls = 0;
const inner = window.fetch;
window.fetch = (u, o) => { calls++; return inner(u, o); };
start.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
assert(calls === 0, 'second click suppressed while the first is in flight');

// --- 4. response settles, panel re-renders ----------------------------------
releaseStart();
await new Promise(r => setTimeout(r, 60));
assert(/llama-server is up/.test(busy.textContent), 'result line after response → ' + busy.textContent);
await new Promise(r => setTimeout(r, 1300));
assert(!!$('restartModel'), 'panel re-rendered with Restart once the server answers');

// --- 5. back to top ---------------------------------------------------------
const toTop = $('toTop');
assert(!!toTop, 'Back-to-top button exists at the bottom of the tab');
let scrolled = null;
window.scrollTo = o => { scrolled = o; };
toTop.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
assert(scrolled && scrolled.top === 0, 'Back-to-top scrolls to top');

// --- 6. no field lost or duplicated ----------------------------------------
const keys = [...panel.querySelectorAll('[data-key]')].map(e => e.dataset.key);
const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
const expected = specs.filter(s => ['model', 'generation'].includes(s.group));
assert(dupes.length === 0, 'no duplicated fields: ' + dupes);
assert(keys.length === expected.length, `all ${expected.length} model+generation settings rendered (${keys.length})`);
console.log('      sections:', [...panel.querySelectorAll('h3')].map(h => h.textContent.trim()).join(' | '));

// --- 7. running state shows Restart + Stop ---------------------------------
status.server_reachable = true; status.proc_running = true;
await window.__R.renderModel();
const ids2 = [...panel.querySelectorAll('button')].map(b => b.id).filter(Boolean);
assert(ids2.includes('restartModel') && ids2.includes('stopModel'), 'running state shows Restart + Stop at the top');
assert([...panel.querySelectorAll('.stat-k')][0].parentElement.textContent.includes('running'), 'Server tile reports running');

// --- 8. backend-mismatch trap: server up but chats on stub -----------------
status.backend = 'stub'; status.effective = 'stub';
await window.__R.renderModel();
const useLlama = $('useLlamaBackend');
assert(!!useLlama, 'mismatch warning + one-click fix shown when server runs but Backend=stub');
let posted = null;
const inner2 = window.fetch;
window.fetch = (u, o) => { if (String(u).endsWith('/api/admin/settings') && o?.method === 'POST') posted = o.body; return inner2(u, o); };
useLlama.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await new Promise(r => setTimeout(r, 60));
assert(posted && JSON.parse(posted)['model.backend'] === 'llama-server', 'one-click fix POSTs model.backend=llama-server');

// --- 9. Save posts the field values and shows a busy state -----------------
status = { ...STOPPED };
await window.__R.renderModel();
const saveBtn = $('saveModel');
saveBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
assert(saveBtn.disabled === true && !!saveBtn.querySelector('.spinner'), 'Save shows spinner + disables while saving');

console.log(failed ? `\n${failed} CHECK(S) FAILED` : '\nALL CHECKS PASSED');
process.exit(failed ? 1 : 0);
