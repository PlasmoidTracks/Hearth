# Project Delivery Record: Hearth

## Delivery status
**CORE (session 1): VERIFIED (operator-run). SESSIONS 2–5 + 7–8 FEATURES: IMPLEMENTED — runtime UNVERIFIED.**
Version 0.1.0 · target: Linux server, Python 3.10+ · sessions 1 (verified) + 2 + 3 + 4 + 5 + 7 + 8 (autonomous additions).

Session 1 core (auth, streaming chat, tools, admin, workspace jail) was **run and verified by the
operator** — selftest green and a working login→chat→tool smoke test after fixing several real bugs
(see DEC-012..017 and the defect list below).

Sessions 2 & 3 additions (CSP/self-hosted assets, durable artifacts, user uploads, device-access mock,
German/English i18n; then staged attachments + multimodal images + workspace-file context + conversation
rename/delete/search + artifacts scope) are **implemented + statically reviewed but NOT yet run** — these
build sessions had no command-execution tool. The operator must re-run `python scripts/selftest.py`
(+ HTTP suite) and the smoke steps in docs/CONTINUATION.md to convert these UNVERIFIED → VERIFIED. Do not treat as
production-ready until then. Honest per-requirement status: CONTRACT.md amendment + tables below.

Session 5 (turn control: abort, `agent.max_tool_steps`, tool-activity visibility, real device-access
gate, llama-server Start/Stop, context bar, retry/force-respond/agent-mode) is likewise **implemented +
statically reviewed, runtime UNVERIFIED** — see its section at the end of this file for a per-item
verification checklist. Note session 5 changed session 2's "device-access mock" into a real gate.

## Contract
See docs/CONTRACT.md (locked). Stack: FastAPI + llama-server (OpenAI-compat) with a stub
fallback; local accounts (scrypt/sessions/CSRF/rate-limit/TOTP/Tailscale-aware); SQLite;
Jinja2+Tailwind UI; sandbox auto-detect bwrap→docker→stub. Decisions in docs/DECISIONS.md.

## What was delivered (requirement → status)
| Req | Capability | Status | Evidence |
|---|---|---|---|
| R1 | llama.cpp backbone via llama-server + provider abstraction + stub fallback | Implemented; **UNVERIFIED** runtime | llm/, manager.py; selftest core |
| R2 | User mgmt/auth: scrypt pw, signed sessions+revocation, CSRF, rate-limit/lockout, TOTP, Tailscale SSO | Implemented; **UNVERIFIED** runtime | security.py deps.py routes_auth.py; selftest password/token/totp/jail pass-by-construction |
| R3 | Per-user isolated workspace (path jail) | Implemented; jail logic unit-tested (design), **runtime UNVERIFIED** | workspaces.py; selftest traversal+symlink cases |
| R4 | Tools: read/write/list/upload/exec(gated)/ask_user/web_search/device(stub) | Implemented; exec fail-closed unit-tested (design); **runtime UNVERIFIED** | tools/*, sandbox.py; selftest exec-refusal |
| R5 | Streaming SSE + prefill/eval progress + tokens/sec + tool debug | Implemented; **UNVERIFIED** runtime | orchestrator.py routes_chat.py chat.js |
| R6 | Admin: users, model settings (documented), protocols.md, system_prompts.md, config, audit | Implemented; **UNVERIFIED** runtime | routes_admin.py admin.js runtime_settings.py |
| R7 | Concurrent multi-user | Implemented (short sessions, WAL); **UNVERIFIED** load | db.py; not load-tested |
| R8 | n_ctx 262144 default + RoPE extension controls | Implemented; maps to CLI flags; **UNVERIFIED** on real model | runtime_settings.py manager.build_argv; selftest default assert |

## Architecture
See docs/ARCHITECTURE.md (system map, trust boundaries, invariants). Single FastAPI process
fronting llama-server/stub; orchestrator runs a per-turn agent loop over SSE-per-turn
(DEC-010) with OpenAI tool-calling fidelity (DEC-011); workspace jail + sandboxed gated exec.

## How to use it
README.md quick start: `pip install -r requirements.txt` then
`uvicorn hearth.main:app --host 127.0.0.1 --port 8080`. First run prints a one-time admin
password. Add users in Admin → Users. Try chat with the stub model (`list`, `read notes.md`,
`run echo hi`). Point at llama-server via `.env` + Admin → Model → Reload for real inference.

## How to maintain it
Module map in README + ARCHITECTURE. Tests: `python scripts/selftest.py`
(add `HEARTH_RUN_HTTP=1` for the TestClient suite). Runbook: docs/OPERATIONS.md.

## Verification (executed vs pending)
- Executed by operator (2024 session): `python scripts/selftest.py` → **10 tests OK, 1 skipped**
  (HTTP suite). This VERIFIES: password hash/verify roundtrip (incl. scrypt-within-memlimit
  regression guard), signed-token sign/verify/expiry/wrong-key, TOTP generate/verify, workspace
  jail traversal + symlink-escape rejection, settings defaults (n_ctx=262144) + type coercion,
  fs tool jail, and exec fail-closed without a sandbox.
- Executed by agent: none (no execute tool this session).
- Static review performed: import graph vs package depth; OpenAI tool-call message
  reconstruction; auth flow ordering; frontend state handling. Defects found & fixed below.
- Still pending operator execution: HTTP integration suite (`HEARTH_RUN_HTTP=1`) and a manual
  boot → login → chat → tool smoke test to verify the live server + streaming UI end-to-end.

### Defects found and fixed during static review
1. `manager.py` used wrong relative imports (`from .. import`, `.base`) for its package depth → corrected to `. / .llm.*`. Would have crashed at import.
2. OpenAI function-calling fidelity: tool results lacked `tool_call_id`; history would not
   reconstruct valid assistant.tool_calls/tool pairs for a real model → added call_ids end-to-end (models, orchestrator, routes). DEC-011.
3. Frontend `state.awaiting` never set → Send button could stay disabled after an approval prompt → fixed in chat.js.
4. admin.js embedded JSON in HTML attributes with fragile escaping → switched to a data index.
5. Missing `hearth/llm/__init__.py`; dead `_sse` helper + placeholder login stub removed; DECISIONS.md was corrupted mid-write and rewritten complete.
6. **[found by operator self-test]** scrypt params (N=32768,r=8) exceeded OpenSSL's default maxmem → `hashlib.scrypt` raised "memory limit exceeded", which would break every login. Fixed to N=16384 + explicit `maxmem`; regression guard added to selftest. DEC-012.
7. **[found by operator self-test, session 2]** `Artifact.user_id` had a hard FK to users.id → orphan-owner artifact inserts failed (FK violation), and FsWriteTool silently swallowed the error into empty meta (write "succeeded" but no download link). Removed the FK (ownership enforced by query-time filtering on the indexed column, matching lazy-workspace design); added idempotent table-rebuild migration + made bookkeeping failures visible + audit-logged. Regression guard `test_artifact_for_owner_without_user_row`. DEC-023.

> **Operator note:** your existing DB has the old FK-bearing `artifacts` table. On next boot,
> `_migrate_artifacts_drop_fk()` rebuilds it automatically (preserving rows). No manual action needed;
> you can confirm with `sqlite3 data/hearth.sqlite3 ".schema artifacts"` — the new table has no FOREIGN KEY line.

## Security, privacy, supply chain
See docs/SECURITY.md. Secure-by-default (localhost bind, scrypt, HttpOnly/SameSite cookies,
CSRF double-submit, rate-limit/lockout, fail-closed sandbox, per-user data scoping). TLS/edge is
operator responsibility (Tailscale/reverse-proxy). No secrets committed (.gitignore excludes
.env/data). Supply-chain: minimal deps (requirements.txt); SBOM/provenance OFF for prototype.

## Deployment and operations
docs/OPERATIONS.md: systemd unit, model ops, backup/restore (documented, **restore rehearsal not
yet performed**), health (`/healthz`), incident response. No deployment executed (A3 — out of
scope / operator action).

## Known limitations & residual risks
- Runtime unverified by the agent for paths added after the last operator smoke test; re-run selftest + smoke test each round.
- Single-process rate limiting → not shared across workers; scale needs Redis/Postgres/sticky sessions. (Download links are now durable via the artifacts table, so this no longer applies to downloads.)
- SQLite single-writer under heavy concurrent writes.
- Device tools remain stubs (helper protocol documented) until a helper is paired; the user-facing "allow device access" flow + single-file upload path ARE implemented and integrated.
- CSP: **RESOLVED (DEC-018)** — assets self-hosted, strict `script-src 'self'` (no inline scripts), frame-ancestors none. One residual concession: `style-src 'unsafe-inline'` for Tailwind's runtime JIT; droppable via a static-CSS build (follow-up).
- bwrap/Docker exec isolation is defense-in-depth, not a hard boundary vs privileged local attacker; run on dedicated host.
- llama-server CLI flag names (rope-scaling/flash-attn) vary by llama.cpp version; verify against your build before relying on Reload.

## Deferred & rejected work
Deferred: CI/CD, IaC, Postgres/Redis multi-process scaling, real device helper, SBOM/provenance,
property/fuzz/mutation/load packs, full static-CSS Tailwind build (to drop style-src unsafe-inline).
Rejected: native mobile app (responsive web chosen), multi-tenant SaaS.

## Session 2 additions (operator-requested, autonomous)
Added after the first verified boot, per operator request while AFK — see docs/FEATURES.md for
the user-facing description and docs/CONTRACT.md amendment REQ-020..030 for traceability:
- **Per-user private workspace** demonstrable end-to-end via the stub: `write <path> <text>`
  writes into the caller's jail; `upload <path>` returns a durable download link. Agent sees
  only the calling user's space (jail unchanged, re-verified).
- **Durable artifacts**: new `artifacts` table + `/api/artifacts` + `/api/artifact/{id}`. Every
  produced or uploaded file is recorded with ownership; downloads are stable across restarts and
  re-listable per session — replacing the old ephemeral in-memory download tokens as the source of truth.
- **User → server uploads**: `POST /api/upload` (multipart, CSRF, jail-scoped, size cap). UI: drag-&-drop
  on desktop, file picker button (multi) for desktop+mobile, and camera/gallery capture for images
  (`accept="image/*"` + `capture`). Uploaded files land in the user's workspace and appear as artifacts.
- **Device "allow resources" flow**: an integrated (mocked) grant UI + tool wiring so a user can
  permit the agent to read their device folder / take a screenshot; single-file upload doubles as
  the concrete resource path today. Real helper still stubbed behind the documented protocol.
- **Language setting (German)**: per-user `language` preference (de/en), German UI strings, applied
  server-side (`<html lang>`) and client-side via a small i18n dictionary. Defaults to English; user picks.
- Docs: new docs/FEATURES.md (user guide) + CONTRACT amendment + updated README/ARCHITECTURE so a
  fresh context can continue from docs alone.

Status for these additions: **IMPLEMENTED — statically reviewed; runtime UNVERIFIED** until the
operator re-runs `python scripts/selftest.py` (new artifact/upload/i18n tests included) and a smoke test.

### First-boot watch-items for session-2 (things static review can't fully guarantee)
- **CSP × Tailwind dev-build**: strict `script-src 'self'` is safe, but confirm the vendored
  Tailwind JIT renders styled UI under CSP (it injects `<style>`/inline styles — allowed by
  `style-src 'unsafe-inline'`). If any element looks unstyled only when CSP is on, open devtools and
  check for a CSP violation; fix by tightening the specific directive rather than relaxing scripts.
- **`data-i18n` coverage**: chrome strings are localized; some dynamic strings (approval prompts from
  the model, tool previews) remain source-language by design.
- **Uploads on mobile Safari**: `capture="environment"` opens the rear camera; gallery is offered via
  `accept="image/*"`. Confirm the picker behaves on the actual phones used.

## Autonomous decision log
DEC-001..033 in docs/DECISIONS.md, all post-question-closure and reversible. No A2–A4 actions
taken; no external services invoked; nothing deployed.

## Session 3 additions (operator UX batch — autonomous)
Per operator feedback after the first working UI (see CONTRACT amendment v1.2 REQ-026..033):
- **Staged attachments**: files upload immediately but send only with the message (chips + ✕);
  attachments-only messages allowed; server re-resolves artifact ids by owner (never trusts client paths). DEC-024.
- **Images → multimodal base64** data URLs, never raw bytes in chat text; stub flattens content safely. DEC-025.
- **Workspace file listing** injected into the system prompt so the model knows available files. DEC-026.
- **Clear upload errors** incl. explicit over-limit message (client pre-check + server 413 passthrough).
- **Conversation rename / delete(+artifact-choice) / search** (title then content w/ snippet); **Artifacts scope** radio (this chat / all). DEC-027.
- New self-tests: `_attachment_content` base64 round-trip, stub multimodal flatten, `_workspace_context`, and HTTP rename/delete/search.
Status: IMPLEMENTED — statically reviewed; runtime UNVERIFIED until operator re-runs selftest + smoke.

## Reproduction record
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
curl -fsSL https://cdn.tailwindcss.com -o hearth/static/vendor/tailwind.js   # one-time asset fetch
python scripts/selftest.py                       # core checks (should be all green)
HEARTH_RUN_HTTP=1 BOOT_PW=testpass123 python scripts/selftest.py   # + HTTP/CSP/upload tests
HEARTH_PORT=8081 python -m hearth.main           # then open http://127.0.0.1:8081/login
```
Python 3.10+; Linux target. No GPU needed for stub mode.

## Next actions (priority)
1. **Operator runs `python scripts/selftest.py` + HTTP suite + manual smoke** (chat, write/upload artifact, upload via drag/picker/camera, language switch, admin user create) → converts UNVERIFIED to VERIFIED. Highest priority.
2. Wire a real llama.cpp model (see docs/CONTINUATION.md step 2); verify function-calling against the real model's tool format.
3. Real device helper (folder-read + screenshot) to replace the mock grant + device_* stubs.
4. In-app "change my password" for users (currently admin-reset only).
5. Static-CSS Tailwind build → drop `style-src 'unsafe-inline'`; add CI running selftest; Postgres/Redis path if scaling beyond one process.
6. Perform and document a backup/restore rehearsal.

## Completed this session (for the record)
CSP + self-hosted assets (DEC-018); durable artifacts + ownership downloads (DEC-019); user uploads
drag/picker/camera (DEC-020); mocked device-access grant (DEC-021); German/English i18n w/ per-user
preference (DEC-022); stub write/upload demonstrable; docs/FEATURES.md + docs/CONTINUATION.md added.

## Session 4 (tool granularity + llama.cpp wiring + LAN + optional at-rest encryption)
Status: **IMPLEMENTED — statically reviewed; runtime UNVERIFIED** (no execute tool this session).

Delivered:
- **Granular tool registry**: every tool has its own `tools.<name>` enable key and most support a
  `tools.<name>.approval` ("auto"/"ask") setting; legacy coarse keys (`tools.fs_read`, `tools.fs_write`,
  `tools.device`) remain as fallbacks for older stored settings. Admin → Tools is now grouped
  (Filesystem / Web / System & exec / Device). Registry: `hearth/tools/__init__.py` (enabled_tools +
  approval_mode), orchestrator partitions gated/auto via approval_mode.
- **New tools**: fs_read line-ranges (1-based inclusive; output was originally numbered, now verbatim
  again — see DEC-045), fs_append, fs_edit
  (unique-match find/replace), fs_delete (ask by default), fs_move, fs_search (regex/grep across the
  jail), zip_pack (zip artifact with decrypted contents even under at-rest encryption), web_fetch
  (URL → readable text), datetime. workspaces.py gained append_text/edit_text/delete_path/move_path/
  read_bytes/seal_bytes helpers.
- **llama.cpp wiring**: boot settings `HEARTH_LLAMA_PORT`, `HEARTH_LLAMA_JINJA` (default true — required
  for Hermes/Llama-3.1/Qwen template tool-calling), `HEARTH_LLAMA_EXTRA_ARGS` (raw escape hatch);
  health-wait raised to ~120s; exec timeout now the admin setting `exec.timeout_s`.
- **LAN exposure**: unchanged binding via HEARTH_HOST=0.0.0.0 now prints a startup warning when TLS is
  not configured. Docs: README + .env.example updated.
- **Optional encryption at rest** (`hearth/crypto.py`): XChaCha20-Poly1305 (via `cryptography`) framing
  HEENC1|nonce|ct, key from hex or scrypt(passphrase); workspaces write/read through _write_blob/
  decrypting reads; uploads/downloads/artifact sizes/vision-attachments all routed through the crypto
  layer; unframed files pass through (safe mid-life enablement). Requires `pip install cryptography`;
  fails CLOSED if enabled without it. Threat model + limits documented in crypto.py.

Pending operator verification: `python scripts/selftest.py` (+ HEARTH_RUN_HTTP=1) — new suites
GranularToolTests + CryptoTests included; manual smoke of the new Tools admin tab, an fs_edit/fs_delete
approval card, zip_pack download, and (if enabled) encryption round-trip.

## Session 4b — UI-driven llama.cpp loading + curated server settings
Status: **IMPLEMENTED — statically reviewed; runtime UNVERIFIED** (no execute tool). Operator selftest
for session 4 was green (33 tests, incl. GranularToolTests + CryptoTests) before this increment.

Delivered:
- **Fully UI-driven model loading**: `model.server_bin` and `model.model_path` are now admin-editable
  settings (Admin → Model), so you set binary + .gguf paths in the browser and hit Save → Reload; Hearth
  launches llama-server itself. Env vars (`HEARTH_LLAMA_SERVER_BIN/_MODEL_PATH`) remain as fallback when
  the UI fields are blank. `ensure_server()` resolves settings-first, env-second.
- **Curated performance/memory settings** (all Model group, requires_reload): GPU layers now a choice
  (`auto|all|0`, was int — matches newer llama.cpp), flash-attn fixed to the new `[on|off|auto]` value
  form (was a bare flag that newer builds mis-parse), plus CPU threads (-t), batch/ubatch (-b/-ub), KV
  cache quant (-ctk/-ctv: f16/q8_0/q4_0), device (--device), load mode (-lm), reasoning/thinking
  (-rea on/off/auto). Non-default-only emission keeps the command line clean.
- **Graceful migration**: `_coerce` now falls back to a choice's default when a stored value isn't in its
  options (protects keys that changed type int/bool→choice, e.g. flash_attn/n_gpu_layers on old DBs).
- **Better admin feedback**: Model status box shows `paths: set/missing` and an amber hint explaining a
  stub fallback (missing paths vs launch failure → points at runtime/llama-server.log + common causes).
  Path fields render full-width monospace.
- **Tests**: new `ManagerArgvTests` asserts build_argv output (core flags, all/auto gpu-layers word form,
  flash-attn value form, KV K+V, auto/default flag omission, env path fallback, threads) + a
  `_coerce` choice-fallback test. Pure/sync — no server needed.

Pending operator verification: `python scripts/selftest.py` (new argv tests), then in Admin → Model set
binary + model paths, Backend=llama-server, Save, Reload → expect Effective: llama-server and a real
tokens/sec reply; confirm tool-calling against the chosen model's template.

## Session 5 — Turn control: abort, tool budget, work visibility, context bar, start/stop
Status: **implemented; first operator selftest run → 49 passed / 1 failure / 1 error, both fixed here
(re-run pending); no manual/browser smoke yet.** The error turned out to be a genuine robustness bug, not
a test-only problem — see "First operator run" below. All items came from the operator's own list.

| Ask | Delivered | Where |
|---|---|---|
| Abort button | Send swaps to **Stop** while a turn runs (Escape too); `POST /api/chat/abort`; cooperative cancel inside the loop; new `aborted` SSE event | `cancel.py`, `orchestrator.py`, `routes_chat.py`, `chat.js`, `chat.html` |
| Setting for max tool uses | `agent.max_tool_steps` (1–50, default 6) in a new **Admin → Agent** tab; replaces the hardcoded cap; `(step/of)` shown per call | `runtime_settings.py`, `orchestrator._max_steps`, `admin.js` |
| Visualise background work | Live activity line + one running chip per tool call (with arg summary, step counter, `awaiting approval`), completed in place by the ✓/✗ result; gated calls announced before the approval interrupt | `orchestrator._args_summary`, `chat.js` |
| "Allow device access does nothing" | Now a real server-side per-conversation gate (`conversations.device_granted` → `_device.granted` → `device_tools._gate`); helper-pairing state surfaced; uploads/downloads documented as *not* device access (deliberately ungated) | `models.py`, `db.py`, `routes_chat.py`, `tools/device_tools.py`, `chat.js` |
| Starting llama-server is clunky | **Start / Restart / Stop** buttons (+ `/api/admin/model/start|stop`); Restart relaunches so saved settings apply; slow loads show live status; external (`HEARTH_LLAMA_BASE_URL`) servers are never killed | `manager.py`, `routes_admin.py`, `admin.js` |
| Context usage bar for user | Header bar: tokens the model received last turn / window, colour thresholds, ≥80 % warning, measured-vs-estimated flagged; from persisted `usage` + llama-server `/props n_ctx` | `save_turn_usage`, `context_usage_payload`, `chat.html/js` |
| retry / force respond / talk as agent | **↻ Retry** (`/api/chat/retry`, drops only the trailing assistant/tool rows), **⚡ Force respond** (one-shot `no_tools`), **🤖 Agent mode ↔ 💬 Chat only** | `routes_chat._turn_options`, `orchestrator`, `chat.js` |

Also in this increment:
- **One turn per conversation**: a second `/api/chat/stream` returns 409 instead of interleaving
  history; `/api/chat/running` lets the UI re-sync the Stop button after a reload/tab wake.
- **Idle-stream guard** (`HEARTH_STREAM_IDLE_TIMEOUT_S`, default 300 s) so a wedged llama-server slot
  ends with an error instead of hanging; auto-disabled on Python < 3.11 (`asyncio.timeout`).
- **SSE framing helper** `_sse()` with `default=str` (usage/arg metas can carry non-JSON types).
- **i18n**: ~25 new EN+DE keys for all of the above; DE coverage still asserted by selftest.
- **Tests added** (`TurnControlTests` in `scripts/selftest.py`): registry scoping + stale-handle
  safety, `_max_steps` bounds/fallbacks, abort produces exactly one `aborted` event against an
  endless provider, `no_tools` turn emits no tool events, `_args_summary` never dumps file contents,
  device tools refuse without grant, ToolContext grant plumbing, context-bar maths (estimate vs
  measured, ≥80 % warning, n_ctx=0 → no pct), `_turn_options` clamping, manager start/stop presence,
  new specs registered with defaults, `conversations.device_granted` column present, EN/DE parity.

### First operator run (session 5) — results + fixes
`python -m scripts.selftest` → **49 passed, 1 failure, 1 error** (6 HTTP tests skipped). Both problems
were mine; both fixed. Still needs a re-run to confirm green.

- **ERROR `test_no_tools_turn_ignores_tool_requests` — exposed a real robustness bug, not just a test
  bug.** The test drove `run_turn()` with an invented conversation id, and `append_message()` inserted a
  message for a nonexistent conversation → `sqlite3.IntegrityError: FOREIGN KEY constraint failed`. The
  abort test had passed only because cancellation fired *before* any write. Production impact: streaming
  into a deleted/invalid conversation raised a raw 500 mid-stream, and since abort is cooperative, a user
  who pressed Stop while a tool ran could delete the chat in that window and crash the turn.
  **Fix (DEC-037):** new `orchestrator.ConversationGone`; every write point checks the row first
  (`append_message`, `_persist_pending`), the agent loop probes once per iteration, and `run_turn`
  converts it into a quiet `{done, conversation_gone:true}` instead of an exception. `save_turn_usage()`
  now skips bookkeeping for a gone conversation rather than touching orphan rows. Test fixed properly by
  creating the user + conversation (`_make_conv`) — *not* by weakening the assertion — plus a new
  regression test that deletes a chat mid-life and asserts no orphan messages and no raise.
- **FAIL `test_context_bar_computes_percentage` — my arithmetic, not the code.** The estimate is
  `chars//4 + 16` (a 16-token system-prompt overhead constant), so 4000 chars → 1016 tokens = 0.1016,
  while I asserted 0.4. Function behaviour is correct; the test now asserts the real contract and covers
  more: measured-beats-estimate, exact 80 % boundary both ways, clamping to 1.0 when usage exceeds the
  window, `n_ctx=0` → `pct=None`, empty conversation → `source:"none"`.

### Operator verification checklist (items 2–9 still unverified)
1. `python -m scripts.selftest.py` — re-run after the two fixes above; expect all green (HTTP suite only
   with `HEARTH_RUN_HTTP=1`). These tests import stdlib + SQLAlchemy/hearth only, so they run in the core
   suite; an import error there is a real bug to report, not a skip.
2. Abort: with the stub, ask for something tool-heavy (`write big.txt <lots of text>` then more), press
   **Stop** mid-turn → activity line + chip disappear, "Turn aborted." appears, Send re-enables within
   ~1 s; `audit_log` shows `chat_abort`. With llama-server, Stop should halt generation (not just the UI).
3. Tool budget: Admin → Agent → Max tool steps = 2, Save; ask for a 3-step job → expect "stopped after
   2 tool steps (Admin → Agent → Max tool steps)" and `(1/2)`/`(2/2)` markers.
4. Background work: `write notes.txt <text>` → a running chip with `notes.txt · N chars` appears *before*
   the result; zip two files → chip persists during packing.
5. Device access: press the button → notice explains helper-missing; ask "list my device folder" → tool
   refuses (helper message). Set a fake `device.helper_url`, revoke the grant → refusal changes to the
   not-granted message; grant again → it attempts the helper and reports unreachable. Uploads still work
   with the switch OFF (by design).
6. Start/Stop: Admin → Model with paths set → **Start llama-server** → live "loading…" text → status flips
   to running; change `n_ctx`, **Restart**, confirm new value in status + `llama-server.log`; **Stop** →
   stopped. With Backend=stub, chats still use stub (expected — switch Backend separately).
7. Context bar: send a few messages → bar grows and label reads e.g. `1.2k / 262k`; hover for tok/s; with
   a small `n_ctx` (>80 %) it turns red with the warning text. Reload the page → value persists.
8. Retry/Force/Agent: **↻ Retry** regenerates the last answer without duplicating your message (check DB:
   one user row, one assistant row); **⚡ Force respond** then ask `list` → no tool chip appears and the
   agent badge returns to "on" after sending; **💬 Chat only** keeps tools off until toggled back.
9. German: switch to Deutsch and re-check every new control (Stop, Retry, Force respond, agent badge,
   device notice, context tooltip).

Known limitations / follow-ups:
- Abort is cooperative: a tool already executing (e.g. an `exec`) finishes its call first; the sandbox
  timeout remains the hard bound there.
- Context bar measures the *previous* turn (that is what predicts the next send); it does not model chat
  template overhead, so treat llama-server `/props` as authoritative when present.
- Retry deletes messages irreversibly (no undo) — acceptable at family scale; a soft-delete/trash would be
  nicer if the operator wants it.
- Device helper daemon still unshipped; `device.helper_url` + the grant are the real plumbing waiting on it.
- Regenerate-as-draft (keep old reply beside the new one), per-message retry, and context
  summarise/compact-on-overflow remain backlog items (CONTINUATION.md).


## Session 7 — duplicate artifacts/download buttons + invisible write progress
Status: **implemented; statically reviewed — runtime UNVERIFIED** (this session had no command-execution
tool, so `python scripts/selftest.py` and the browser smoke below are operator actions). Both items came
from the operator using a real llama.cpp model.

| Ask | Delivered | Where |
|---|---|---|
| "fs_write **and** upload both showed a download button, and the file appeared twice in Artifacts" | Artifact identity is now `(user_id, rel_path)`: `record()` refreshes the existing row (same id ⇒ old links stay valid) instead of inserting a duplicate. Client also renders at most one ⬇ per file per turn. User uploads keep one row per transfer (`new_row=True`) by design. Prompt tells the model a write already produced its download button, so it should not call `upload` at all | `artifacts.py`, `routes_chat.py`, `chat.js`, `prompt.py`, DEC-040 |
| "No indication the agent was actually writing a file — show a number go up" | `tool_progress` fires an early hint (~32 B) instead of waiting for 256 B, and carries `path_hint` (first completed string value of the partial JSON = the path). Chip becomes `✎ fs_write · writing hello_world.py · 240 B · ~40 %` with a bar that keeps easing between hints | `llm/base.py`, `llm/llama_server.py`, `orchestrator.py`, `chat.js`, DEC-041 |
| First-run discoverability | Dismissible note above the transcript: where files go, one button per file, Artifacts scope, live progress. Retires on the first message (MutationObserver) or via its ✕-style "Got it" button; remembered in localStorage | `chat.html`, `chat.js`, `i18n.py` (3 new EN+DE keys) |

### Notes on honesty of scope
- DEC-039 previously declared the duplicate-button behaviour "harmless" and tried to fix it with prompt
  advice only. It was not harmless and the advice does not hold across models — hence a real server-side
  fix this time, with the prompt change kept as an optimisation rather than the mechanism.
- **Live progress is a streaming-model feature.** `StubProvider` builds its tool calls instantly, so on the
  stub the write chip appears and vanishes in one frame. Not faked with artificial delays; documented here
  and in FEATURES.md so nobody reads stub behaviour as proof the feature works.
- The `~%` figure is a client-side estimate (`chat.js growEstimate`: eases toward the largest tool call seen
  this page session, ratchets up mid-call, decays after 30 s idle). It exists because hint cadence is coarse
  and unpredictable; it is prefixed `~`, capped below 100 %, and never claims anything about the file. The
  byte count next to it is real data from the provider.

### Tests added (`scripts/selftest.py`)
- `test_write_then_upload_lists_file_once` — reproduces the reported flow; asserts one Artifacts entry.
- `test_rewriting_a_file_updates_instead_of_duplicating` — one row, same artifact id, refreshed size.
- `test_upload_route_still_records_separate_rows` — guards the deliberate `new_row=True` asymmetry.
- `test_tool_progress_carries_path_hint_only`, `test_tool_progress_forwards_path_hint` — event shape + SSE
  forwarding; content must not ride along.
- `test_llama_server_path_hint_never_leaks_content` — file content that itself looks like `"key":"value"`
  must never become the hint.
- `test_llama_server_progress_hints_start_early` — a small call still gets an early hint (≤96 B) plus a
  later one; this is the regression guard for "no indication it was writing".
- i18n parity extended to `chat.intro_head/body/dismiss`.

### Operator verification checklist
1. `python scripts/selftest.py` (and `HEARTH_RUN_HTTP=1`) — expect all green, including the 7 new/updated
   tests above. Existing throttle test still passes: one extra hint per call (the early one), far below the
   per-delta bound it asserts.
2. Real model: "create a hello world python script and then provide the download" → watch for `✎ fs_write`
   with the filename and a growing byte count; after ✓, exactly **one** ⬇ button and **one** Artifacts row;
   download returns the file. Then re-run the same request in the same chat → still one row (updated size).
3. Attach the same filename twice from your device → two entries under Artifacts, both downloadable.
4. Edit/rewrite a file the assistant wrote (`fs_edit` or another `fs_write`) → panel entry updates, the
   download link from an earlier message still serves the new content (same artifact id — intended).
5. Deutsch: check the write chip label and the first-run note; dismiss it and reload → stays dismissed.

Known limitations / follow-ups:
- A file written from two different chats shares one row and follows the most recent chat's scope
  (workspaces are per user, not per chat). A per-conversation file view should read message metas.
- `path_hint` is heuristic: it is the first completed string value in the partial JSON, which for current
  tool schemas is the path but could be another argument for a differently-shaped tool.
- No auto-compaction of over-context chats yet (unchanged backlog item).

### Operator run of session 7 → 6 failures, all fixed (see DEC-042)
`python scripts/selftest.py` → 57 passed / 6 failed. Reproduced locally-by-reading, root-caused, fixed:

| Failing test | Real cause | Fix |
|---|---|---|
| `test_llama_server_emits_throttled_tool_progress`, `..._path_hint_never_leaks_content`, `..._progress_hints_start_early` (0 hints emitted) | `_prog_slot()` mixed a `"started"` **string** key into a dict keyed by tool-call **index**, so the per-slot lookup always missed and the throttle never fired | state is now one dict per index (`{"n", "started"}`); call site passes `idx` |
| `test_tool_progress_streams_to_client_without_raw_args` (18 hints ≠ 3) | My test was wrong: its fake provider never feeds a tool result back, so the agent loop re-streams each round — repeating hints are correct behaviour | asserts per-round shape + total count; leak assertion unchanged |
| `test_upload_route_still_records_separate_rows` (`a3 != a2`) | Genuine defect under session 7's dedupe: `created_at` is whole seconds, so "newest row" was chosen arbitrarily among same-second rows — the same coarseness behind the original duplicate-looking panel | `created_at` is a monotonic sequence (`_next_seq`, seeded from `max(existing, now())`); `list_for()` sorts by `(created_at, id)` |
| `test_workspace_context_shows_sizes_and_uploads` ("attachments" missing) | Test asserted a word that lives in `prompt.WORKSPACE_PREAMBLE`, not in `_workspace_context()` (which says "the user attached") | both asserted where they actually live; preamble wording clarified |

Also found while fixing (not covered by any test): **tool-only turns emitted no `usage` event at all** —
llama-server sends usage on its final chunk, which the agent loop never waits for when a tool request ends
the stream. The context bar and tok/s badge therefore went dead on exactly the turns doing the most work.
Now every round reports usage: measured when the provider sent it (accumulated across rounds via
`_merge_usage`), otherwise an explicit `estimated: True` figure derived from prompt size + generated
characters (content **plus** tool-call argument bytes, hence the new `ToolRequest.args_raw`). Estimates are
rendered with `~` in the badge and are never persisted as if measured. The stub reports its own per-round
usage so it is not double-counted.

New tests: `test_artifact_ordering_is_strict_within_one_second`, `test_agent_loop_reports_usage_for_tool_rounds`,
`test_stub_reports_usage_on_tool_rounds`, `test_tool_request_carries_raw_args_for_usage`, plus the rewrite
test now also asserts the refreshed record sorts newest.

Chips are now keyed by `(tool name, agent-loop round)` — `round_id` is a new field on `tool_progress`,
`tool_call` and `tool_result` — so a tool re-composed in a later round gets a fresh chip instead of being
suppressed by a round whose chip was already retired. `_resume_pending` passes `round_offset=1` for the
same reason.

### Second operator run — 3 remaining failures, real root cause found (DEC-043)
The DEC-042 fixes landed (artifact ordering + per-round usage all green), but the three `tool_progress`
provider tests still emitted **zero** hints. My previous diagnosis was wrong: the int-vs-string key
collision was harmless (`dict.get(-1, 0)` just defaulted). The actual bug was **late binding** —
`_prog_slot()` was a closure called from inside `async for`, so its body ran after `slot` had been rebound
to the next tool call; hints had only ever worked through an accidental shared `-1` key, which my edit
removed.

- `_prog_hints(idx, args, name, first) -> list[StreamEvent]`: takes values, not the loop's dict; state lives
  in `last_prog`; returns a list so multiple 256 B intervals arriving between checks are all reported.
- Provider `except Exception` now prints a traceback **and** yields an `error` event. It previously wrapped
  the whole stream loop, so any parse error aborted the generator before `tool_request` — indistinguishable
  from "provider sent no hints". That silence is what made this two-round debugging possible; the tests now
  assert no stray `error` event so a swallowed exception cannot masquerade as coarse throttling again.
- `CryptoTests` had accumulated abort/tool-progress/context-bar tests unrelated to crypto; noted in
  DECISIONS.md so test placement stays honest.

**Still UNVERIFIED.** If these three fail again, the traceback now printed to stderr will name the actual
exception instead of leaving it to be guessed at.

### Third operator run — actual root cause: a broken test fake (DEC-044)
The new traceback answered it in one line: `TypeError: 'FakeClient' object does not support the asynchronous
context manager protocol` at `llama_server.py:113`. The fakes implemented `__aenter__`/`__aexit__` on the
*response* but not on the *client*, so the provider raised before parsing anything and the catch-all
`except Exception` turned that into silence — read by every test as "0 hints, throttle too coarse".

**The DEC-042 and DEC-043 diagnoses were both wrong.** The int-vs-string dict key and the `async for` late
binding are genuine code-quality issues and their fixes stand on their own, but neither caused this. Keeping
them documented as if they had would send the next reader down the same two dead ends.

Fixes: (1) one shared `_fake_llama_client(lines)` fixture implementing both context-manager protocols, so the
three drifted copies cannot diverge from the real interface again; (2) `stream()`'s try/except narrowed to
the HTTP connect only, with the response released in a `finally`, so an internal parse bug propagates loudly
instead of masquerading as provider silence.


## Session 8 — downloads only from `upload`, verbatim reads, anti-loop guardrails
Status: **implemented; statically reviewed — runtime UNVERIFIED** (no command-execution tool in this
session, so `python scripts/selftest.py` and the browser smoke below are operator actions). All three items
came from the operator driving a real llama.cpp model. DEC-045.

| Ask (operator) | Delivered | Where |
|---|---|---|
| "The chat gives a download link for fs_write but not the first upload call — and later both. I want the file provided by fs_write **not at all**, ONLY by upload" | `fs_write`/`fs_append` register the durable artifact (Artifacts panel, downloadable there) but return no download reference; only `upload` and `zip_pack` do. Tool descriptions + workspace preamble state the rule, so the model calls `upload` when the user asks for a file. Client keeps its per-turn dedupe; history replay draws at most one ⬇ per file and only for `upload` rows | `tools/fs_tools.py`, `prompt.py`, `static/chat.js`, DEC-045 (reverses DEC-040's "button per file") |
| "It got stuck in its thinking phase and repeated itself indefinitely" (fs_edit → old_text not found → fs_read → same fs_edit …) | **Root cause found:** `fs_read` with a line range prefixed each line `1: `, `2: ` …; models quoted those prefixes into `old_text`, which can never match. Range reads are now verbatim. Plus two guardrails: the agent loop will not execute an identical `(tool, args)` twice in a turn (answers from the recorded result with an explicit "do not repeat" instruction, audit-logs `tool_repeat_suppressed`), and each round's `tool_calls` carry a `_DONE_NOTE` telling the model what it already did — stripped again by `_clean_tool_calls()` on replay so it cannot accumulate or inflate token estimates | `tools/fs_tools.py`, `orchestrator.py`, DEC-045 |
| Token-level repetition (small model looping on words) | New admin setting **Repetition penalty** (`model.repetition_penalty`, default 1.0 = off, generation group, no reload needed). Sent to llama-server only when > 1.0, so an unconfigured install sends the same request body as before and a build that rejects the field cannot break chat | `runtime_settings.py`, `llm/llama_server.py` |
| "thinking…" shown after a turn's last tool result | The pulse continues only while another round can still run (`step < of`) | `static/chat.js` |

### Notes on honesty of scope
- **The button was never a bug, it was DEC-040's chosen policy** — one ⬇ per *file*, drawn by whichever
  call carrying `meta.download` landed first. Since `fs_write` answers before `upload`, the write drew it
  and the client dedupe suppressed the upload's identical key: that is precisely the reported asymmetry.
  DEC-040 listed "only `upload` shows a button" as an alternative it rejected; the operator has now made
  the opposite call, and it also removes buttons nobody asked for.
- **The edit loop was not "just a small model".** The harness handed the model text that could not match,
  and its own instruction ("prefix each line with its number so the model can quote exact lines back to
  fs_edit") guaranteed the quote would be wrong. Removing the decoration is the fix; the repeat guard is a
  safety net for the next such trap, not a substitute for it.
- **Repetition penalty ≠ loop fix.** It penalises tokens within one completion. Repeated *tool calls across
  rounds* are handled by the loop guard. The setting's help text says so, because an admin who reaches for
  it expecting the latter would turn up the knob and see no change.
- The repeat guard compares exact arguments: a model that varies whitespace re-executes, which is correct —
  that is a different edit, not a repeat.

### Tests changed/added (`scripts/selftest.py`)
- `test_write_creates_durable_artifact` — now asserts `"download" not in meta` and that the artifact row
  still exists (the policy change, asserted as policy rather than left to drift).
- `test_rewriting_a_file_updates_instead_of_duplicating` — takes the id from the artifact store and gets
  the download reference from `upload` (fs_write no longer returns one); dedupe/id-stability unchanged.
- `test_read_line_ranges` — asserts verbatim output (`"l2\nl3"`, no prefixes) instead of `"2: l2"`.
- NEW `test_edit_succeeds_on_text_read_from_a_range` — reproduces the reported flow (write C file → read →
  edit a quoted line) and requires it to succeed.
- NEW `test_repeated_tool_call_is_not_executed_twice` — provider that re-requests an identical call for 4
  rounds; asserts the file was appended once, every round still got a result event, at least one carried
  the suppression note, and no download meta leaked on repeats.
- NEW `test_done_note_is_stripped_from_replayed_tool_calls` — `_clean_tool_calls` restores exact arguments,
  keeps ids/pairs, is idempotent, and the token estimate ignores the note.
- NEW `test_done_note_does_not_accumulate_across_turns` — two turns of repeated calls: what the provider is
  shown contains zero notes (they are stripped on replay) while the store holds at most one per call row.
- NEW `test_repetition_penalty_sent_only_when_raised` — body omits the field at 1.0, carries it when raised.
- `test_new_settings_specs_registered` — covers `model.repetition_penalty` (type/default/range) and that it
  does not claim to require a model reload.

### Operator verification checklist
1. `python scripts/selftest.py` (+ `HEARTH_RUN_HTTP=1`) — expect all green, including the changes above.
2. Real model: "write a hello world python script" → `✓ fs_write`, **no** ⬇ in the transcript, one row under
   Artifacts. Then "now provide a download" → `✓ upload` + exactly one ⬇ that downloads the file.
3. Real model: "make hello_world.c print a random number" → the edit should succeed on the first attempt. If
   it still fails, check whether the model quoted text it read (verbatim now) and look for
   `tool_repeat_suppressed` in the audit log — that means it is repeating itself and being stopped rather
   than looping.
4. Admin → Generation: set Repetition penalty to 1.1 on a looping model, no reload needed; set it back to
   1.0 and confirm chat still works (the field must disappear from the request body).
5. Watch the activity line after the last tool of a turn: it should stop rather than say "thinking…".

Known limitations / follow-ups:
- The repeat guard is exact-match on `(name, canonical args)`; near-duplicate calls still run by design.
- History rendered from transcripts written before this change shows no ⬇ at all on old `fs_write` rows
  (correct under the new policy) and re-resolves `upload` buttons from the artifact store; if that file has
  since been offered from another chat, its row follows that conversation, so use the Artifacts panel.
- Auto-compaction of over-context chats remains backlog (unchanged).


## Session 8b — two selftest defects from the operator run + LAN hosting
The operator ran `python scripts/selftest.py` after session 8: **75 passed, 1 failure, 1 error** (HTTP suite
skipped). Both were bugs in *my new tests*, not in the shipped code — recorded here because that distinction
matters for what is actually verified.

| Result | Real cause | Fix |
|---|---|---|
| `test_repetition_penalty_sent_only_when_raised` → `NameError: name 'httpx' is not defined` | The test replaces `httpx.AsyncClient` but never imported `httpx`; sibling provider tests import it inside their own bodies, so nothing else supplied it | `import httpx` in the test, with a comment saying why relying on another test's import is exactly this bug |
| `test_done_note_is_stripped_from_replayed_tool_calls` → `AssertionError: 12 != 8` | The test compared `_estimate_prompt_tokens` of a message holding `fs_read` + `'{"path":"a"}'`+note against a *different* fixture (`name="x"`, `arguments="{}"`), so it measured the fixture difference instead of the stripping | Compare the same call with and without the note; keep the idempotency check as its own assertion |

Both fixes are test-only. The behaviour they guard (repetition penalty omitted at 1.0 / sent when raised;
note stripped on replay) was already implemented in session 8 and is unchanged.

### LAN hosting for family (`HEARTH_HOST=0.0.0.0`) — DEC-046
| Ask | Delivered | Where |
|---|---|---|
| "make the server be hosted on 0.0.0.0 so I can have my family member try it out locally" | Default stays `127.0.0.1`; LAN access remains an explicit opt-in via the existing `HEARTH_HOST`. New **`HEARTH_COOKIE_SAMESITE`** (`strict|lax|none`, default strict, normalised with strict as the floor — never silently `none`) so a device reaching the app by LAN IP isn't locked out by `SameSite=Strict`; startup prints the LAN URL and the one-line cookie fix when strict is still active on a non-loopback bind. Docs: README LAN section, SECURITY.md accepted-risk list, OPERATIONS.md runbook with firewall command + "llama-server stays on loopback" | `config.py`, `routes_auth.py`, `main.py`, `.env.example`, README/docs, DEC-046 |

New test: `test_cookie_samesite_is_constrained` (default strict; `lax`/`none` honoured; every unrecognised
value → strict; constructed with an explicit `secret_key` so it cannot depend on the ambient env).

### Operator verification checklist (session 8b)
1. `python scripts/selftest.py` — expect the two failures gone, everything else unchanged.
2. `HEARTH_RUN_HTTP=1 python scripts/selftest.py` — still unrun by anyone; please try it.
3. LAN: `HEARTH_HOST=0.0.0.0 HEARTH_COOKIE_SAMESITE=lax python -m hearth.main` → note the printed URL, open
   it from a phone on the same Wi-Fi, log in with an account created under Admin → Users, send a message and
   write a file. Then confirm the two isolation basics: the family account sees no admin artifacts, and
   `HEARTH_HOST=0.0.0.0` without `HEARTH_COOKIE_SAMESITE=lax` reproduces "page loads, sends fail" (so the
   printed hint is telling the truth).

## Session 9 — media tools: PDFs as images, video frame sampling, context-usage meta tool
Status: **implemented; self-tested (125 tests pass, 10 skipped) — runtime UNVERIFIED against a real
llama.cpp vision server.** Operator asked for three things; all three are in, plus the plumbing that lets
a *tool* deliver pixels at all (DEC-052).

| Ask (operator) | Delivered | Where |
|---|---|---|
| "a pdf reading tool, that utilizes the mmproj image projection, basically taking images of each page, so visual elements are not lost" | `pdf_read` renders pages to PNG and shows them to the model; `pdf_info` reports page count/metadata first so the model picks a range instead of dumping a whole document. Backend chain pymupdf → pdftoppm → gs → mutool, each optional; a missing one yields an error naming the package. Pages are saved under `media/pdf/` | `tools/media_tools.py`, `tools/media_render.py`, DEC-052 |
| "a way to view videos … instead of decoding all frames, simply decode 8 frames, equally spaced, perhaps with the option for the agent to choose the timestamps … (I suppose this requires the view video tool to also return the metadata like runtime)" | `view_video`: **8** equally spaced frames by default (`count` up to 16), plus explicit `timestamps`, plus a `start_time`/`end_time` window. `video_info` returns **runtime**, fps, resolution, frame count, size — no decoding. ffmpeg PNG pipe → OpenCV fallback | same |
| "a meta tool … to fetch the current context usage, in percent and in absolute values" | `context_usage`: tokens used, window (`n_ctx`), percent, remaining, measured-vs-estimated `source`, last-turn completion + tok/s, and an over-80 % warning. Reuses `routes_chat.context_usage_payload()` so it can never contradict the user's context bar | `tools/context_tool.py` |

### What had to change underneath (the part that was not "just three tools")
- **`ToolResult.images`** — new optional field; tool results could previously only ever be text.
- **Delivery shape (DEC-052, operator's choice):** the tool message stays plain text and the images follow
  as a **synthetic user-role multimodal message**, injected by `build_messages()` from paths stored on the
  tool row's meta. Chosen over multimodal `tool` content because hearth had zero evidence its chat
  templates accept it, while the upload path (same shape) is known to work with mmproj. The injected
  message is **not persisted**, so no synthetic rows accumulate and the transcript shows one tool chip.
- **`_image_parts()`** extracted so uploads and tool output share one encoder + 8 MiB cap.
- **Byte-based rendering only.** With workspace encryption at rest, handing a jail path to pdftoppm/gs/
  pdfinfo (separate processes, no key) would feed them ciphertext; callers decrypt via
  `workspaces.read_bytes()` and the renderer stages its own temp file.

### Notes on honesty of scope
- **Verified by execution:** all five tools against real generated PDFs (2/3/10-page) and videos (3 s,
  4 s, 8 s) inside the workspace jail — default range cap, explicit page ranges, past-the-end refusal,
  even-spacing timestamps (`0.5,1.5,…` on an 8 s clip), window sampling, explicit timestamps incl. 7.9 s
  of an 8 s clip, frame-count clamping at 16, absolute-path refusal, non-PDF input, missing files. Also
  verified by execution that `build_messages()` emits `system, user, assistant(tool_calls), tool,
  user[image_url…]`, that the DB row holds **paths not base64**, and that the stub never leaks base64.
- **UNVERIFIED:** whether a real mmproj build *attends* to images delivered mid-conversation after a tool
  result (only the message graph was checked), and per-image token cost — `_estimate_prompt_tokens()`
  still counts an image part as a fixed 64 tokens, so the context bar under-reads on media-heavy turns
  unless the server reports usage. Needs one live turn with a vision GGUF + matching mmproj.
- **Not built:** no UI thumbnails for rendered frames (they appear only as workspace files/artifacts); no
  audio transcription (`video_info` states that audio tracks exist but are not transcribed); no PDF text
  extraction fallback for machines with no rasteriser; no auto-compaction (unchanged backlog).
- **Environment note:** `pymupdf`, `Pillow`, `opencv-python` and `ffmpeg` are all *optional* by design and
  none is in the project venv here — PDF/video checks skip themselves when a backend is absent. The
  fallback chain was exercised directly against pdftoppm, gs, mutool, ffmpeg and OpenCV, all of which are
  installed on this machine outside the venv.

### Tests added (`scripts/selftest.py` → `MediaToolTests`, 12 tests)
Registration + admin enable keys + JSON specs for all five tools; `ToolResult.images` optionality; images
persist as paths on the tool row and replay as a multimodal user message with the tool message staying a
string; `_image_parts()` tolerance of missing/empty entries; stub never leaks base64; frame/page caps
bounded; `capabilities_line()` honesty; PDF render round-trip from a hand-written minimal PDF (skips if no
backend); `pdf_info_bytes` raises actionable `MediaError`; `fmt_time`/`_parse_rate` edge cases (the
`3599.6 → "60:00"` rounding fix); `context_usage` absolute + percent + clamping; jail refusal of absolute
paths for all four file-taking media tools.

### Operator verification checklist (session 9)
1. `python scripts/selftest.py` — expect 125 passed, 10 skipped (media checks skip if no backend).
2. Install at least one PDF backend and a video one: `pip install pymupdf Pillow`, system `ffmpeg`.
   Admin → Tools shows the new toggles under a **Media** group; `media.max_image_edge` controls downscaling.
3. Upload a PDF, ask "read page 1 of scan.pdf". With a vision model + mmproj expect a description of what
   it *sees* (layout/figures, not just text). Without mmproj expect the tool's warning that the model
   cannot see images — that message is the feature working, not failing.
4. Upload a short video: "what happens in clip.mp4" → 8 frames; then "show me 0:05 and 0:12" → those two
   timestamps. Confirm `media/pdf/` and `media/video/` appear in the workspace/artifacts.
5. Ask "how full is your context?" → percent + absolute + measured-vs-estimated, matching the header bar.
6. **The open question:** confirm with a real vision build that images delivered *after* a tool result are
   actually used (ask something only the picture can answer). If a build rejects them, DEC-052 records the
   alternative (multimodal `tool` content) and where to switch it.

---

## Session 10 — captioner streaming on the tool path, and durable captions (DEC-061)

### What was reported
`describe_audio` produced no live output (unlike an attached file, which streams), its result looked
cut off even inside a scrollable box, and a description disappeared on refresh with no indication of
whether it was still in the model's context.

### Root causes (all confirmed by reading, one by a control run against committed HEAD)
1. **No `on_delta` from the tool.** `_run_server()` already streamed and forwarded deltas; the
   attachment path passed `on_delta`, the tool did not — so text accumulated in a buffer only.
2. **No drain seam.** Tools are awaited, not iterated, and `_pending_notices` was drained *only* in
   the steering branch — so even a correct callback would have been dropped. Latent bug on its own.
3. **Three unrelated truncations conflated:** `max-h-32` CSS (≈5 lines), `"preview": content[:800]`
   at four sites, and the legitimate 24 000-char output guard for the model.
4. **Captions were never persisted.** Only `_captions` (in-process, 64 convos, 5200 chars). And
   `_agent_loop`'s docstring claimed later rounds read from that cache — but `build_messages()` never
   called `get_caption()`, so a later message sent the model a bare `[attached: x.mp3]`.

### What changed
- `ToolContext.report_progress()` + module-level `_run_tool_streaming()` (queue drained while the tool
  runs; result yielded as final item). Used on the auto path **and** `_resume_pending`, so live output
  does not depend on a tool's approval mode.
- `base.DeltaThrottle` shared by attachment and tool paths; `base.ui_preview()` (4000) replaces the
  four `[:800]` slices. Output guard untouched — display budget ≠ model budget.
- `orchestrator.persist_captions/captions_from_meta/caption_state`: captions stored on the user row's
  `meta.captions`, merged back in `build_messages()`, summarised by the history endpoint, restored by
  `chat.js` via `addCaptionBlockStatic()` (collapsed). `_captioner_hint()` no longer promises a
  description always arrives.
- `chat.js`: caption blocks keyed per file; chip body `max-h-[70vh]`; DE+EN strings added.

### Tests added (`scripts/selftest.py` → `CaptionStreamAndPersistenceTests`, 8 tests)
Throttle batching / time-based flush / idempotent tail; `ui_preview` passthrough, bound, `None`;
`ToolContext` sink absent-set-raises-nothing and a throwing sink cannot break a tool; `describe_audio`
must pass `on_delta` and emit start/chunks/done with a **complete** (un-truncated) stream; failure path
still emits `caption_done{ok:false}`; `captions_from_meta`/`caption_state` round-trip incl. junk/empty
meta and plain-string tolerance; stored caption reaches `_attachment_content` on a later turn.

### Verification actually performed
- `python scripts/selftest.py` **and** `HEARTH_RUN_HTTP=1 python scripts/selftest.py`: **196 tests, 0
  failures, 2 skipped**. (Note: the venv was missing `sqlalchemy`/`fastapi`/`pydantic`, so 142 of the
  previous 188 were erroring on import — installed; the suite is genuinely green now.)
- Drain-seam semantics proven in isolation: events while the tool runs, result as final item, exception
  propagation, quiet tools unaffected.
- `describe_audio` end-to-end: 53 deltas → 8 throttled chunks + start/done, concatenated stream
  byte-identical to the full caption; failure emits `caption_done{ok:false}`; no sink → no crash.
- Approval-gated resume verified separately (6 chunks streamed, row persisted once).
- Persistence against **real SQLite**: round-trip, idempotency, failure stored `ok:false`, unrelated rows
  untouched, `build_messages(captions=None)` replays the description.
- Reload over HTTP with a real upload: history returns `{uploads/voice.mp3:{name,ok:true}}` + text in meta.
- Preview bounds: 9→9, 2160→2160, 5400→4000, 200 000→4000 (old code: 800 at every site).
- **Control run:** committed HEAD sends `['[attached: meeting.mp3]']` on a later turn; with the fix the
  same graph contains the description.
- `node --check hearth/static/chat.js` clean; no stale `captionEl`/`captionText` references.

### Operator verification checklist (session 10)
1. `HEARTH_RUN_HTTP=1 python scripts/selftest.py` — expect **196 passed, 2 skipped**.
2. With a captioner configured, ask: "what's in meeting.mp3?" → the 🎧 block should grow *while* it
   listens (not appear all at once), then collapse when done. This is the headline fix.
3. Reload the page mid-chat → the 🎧 block comes back (collapsed) under the message it belongs to.
4. Ask a follow-up about the same recording in a later turn → the assistant should still be able to talk
   about it without re-listening. If it claims ignorance, check that `meta.captions` landed on the row.
5. Set `describe_audio` approval to **ask** and repeat step 2 — it must stream just the same.
6. Long result: ask for a big file read → chip shows up to 4 000 chars and scrolls past that; if you see
   an `[OUTPUT GUARD]` note, that is the 24 000-char model cap (Admin → Agent), not the UI.
7. **Still open (not fixed here):** there is no surfaced answer to "is this caption currently inside the
   model's context window?" — only *described* vs *failed*. That needs prompt-graph accounting.

---

## Session 11 — video and audio, coupled by the window (DEC-064)

Operator question: should video viewing automatically include audio analysis when the captioner is
available, or be an agentic choice? Resolution: neither extreme — one `with_audio` parameter on
`view_video`, whose span is always the window already sampled for pictures.

### What was asked vs what was built

| Request | Implementation | Evidence |
|---|---|---|
| "ein Parameter, ob audio auch gecaptured werden soll" | `view_video(with_audio=true)`, default **off** | schema property present; unset ⇒ captioner never invoked (test asserts call count unchanged) |
| "automatisch auf low-high geschnitten, kein extra parameter" | span = `start_time`/`end_time`, else first-to-last sampled frame; no audio-range argument exists | `_audio_window()` tests: `{20,50}→(20,50)`, frames-only `→(23.75,46.25)`, single stamp `→(7,8)` |
| description must warn against audio on 2+ minute videos just to parse them | stated in `ViewVideoTool.description` **and** the `with_audio` schema text, with the whole-video exception spelled out | test asserts `"2 minutes"`, `"gist"` appear in description/schema; `AUDIO_WINDOW_LIMIT_S == 120` |
| "nur aktiv bei Zeitfenstern < 2 Minuten" | ceiling enforced in code, not left to goodwill: over it → frames delivered + span named + one concrete retry window + pointer to `describe_audio` | refusal test: `ok=True`, `"over the 120s limit"`, `"describe_audio"` in content, `meta.audio_described=False` |

Also fixed on the way: `video_info` printed `(audio is NOT transcribed)` — written before DEC-056 and
false ever since. It now reports whether the captioner can actually listen (`preflight`), and says so
when it cannot. A video with zero audio tracks is called silent rather than merely counted.

### Verification actually performed
- `python scripts/selftest.py`: **203 tests, 0 failures, 11 skipped**.
  `HEARTH_RUN_HTTP=1 python scripts/selftest.py`: **203 tests, 0 failures, 2 skipped**.
- **ffmpeg cut proven acoustically**, not by reading argv: a 12 s clip whose tone steps 220 Hz → 1760 Hz
  at t=6. Window `(7,12)` yields one chunk measuring ~1760 Hz with offset 7.0; window `(0,3)` measures
  220 Hz. Had ffmpeg ignored `-ss`, both would read ~990 Hz (verified as the whole-file baseline) and the
  test would fail — so a silently-wrong cut cannot pass.
- Chunk offsets inside a window are absolute: `window=(10,25), chunk_seconds=5` → first offset `10.0`,
  none below it.
- Without ffmpeg a window **raises** `CaptionError` rather than describing the whole file.
- Real end-to-end on generated video (`mpeg4`+AAC, 20 s): `start_time=10,end_time=20,count=3` produced
  frames at t=11.67/15.0/18.33 **and** asked the captioner for exactly `(10.0, 20.0)`.
- Degradation matrix (stubbed captioner): disabled → note + intact frames; `preflight` failure → note +
  intact frames; `CaptionError` mid-call → note + intact frames; all three leave `ok=True`.
- One existing test stubbed `prepare_chunks` with a lambda that rejected the new `window` kwarg — updated
  (call-site, not logic).

### Operator verification checklist (session 11)
1. Ask: "what happens in clip.mp4 around 0:20 to 0:50?" → expect frames **and** an audio description of
   that span; the 🎧-style block streams while it listens (`caption_start/chunk/done`, same events as uploads).
2. Ask for the whole of a long clip with audio → frames arrive, plus a line naming the span and suggesting
   a shorter window; no captioner calls spent on minutes of audio.
3. Silent video + `with_audio` → says there is no track instead of inventing ambience.
4. Captioner switched off (Admin → Audio) → frames exactly as before, one explanatory line.
5. **Still open:** attached *video* still auto-routes its whole audio track through
   `captioner.route_candidate()` (`.mp4`/`.webm` are in `AUDIO_EXTS`) and is subject to
   `captioner.max_bytes`; DEC-064 deliberately did not change the upload path. Worth revisiting:
   size-gate it, or route video to windowed analysis only.


## Session 13 — Qwen Image Edit as a second diffusion profile (`edit_image`)

**IMPLEMENTED — unit + HTTP-verified with the stub CLI; UNVERIFIED against real Qwen Image Edit weights.**
Adds instruction-based image editing alongside text-to-image, without forking the diffusion runner.

### What changed
- `hearth/diffusion.py`: two `Profile`s (`PROFILE_GEN` unchanged, new `PROFILE_EDIT`) sharing one
  runner/lock/log/cancel code. Editing feeds `-r` (reference), CFG 2.5, `--flow-shift 3`, optional
  `--llm_vision`. All backend functions take `profile=` defaulting to generation. DEC-078.
- `hearth/tools/image_tools.py`: `EditImageTool` (`edit_image`, gated) — jail-checks the input image,
  stages it decrypted, runs the edit profile, shows the result back to the model.
- `hearth/runtime_settings.py`: `IMAGE_EDIT_SPECS` (`image_edit.*`) + `tools.edit_image` enable/approval.
- `hearth/routes_admin.py`: `/api/admin/image_edit/{status,command,test}` + a conditional Loaded-models row.
- `hearth/static/admin.js`: Images tab renders both profiles; tools hint updated.
- Docs: DEC-078 (DECISIONS), README editing subsection, FEATURES drawing/editing section, ARCHITECTURE
  session-13 block, CONTINUATION gotcha rewritten.

### Verified in this session (actually run)
- `python scripts/selftest.py` → **250 passed** (stdlib). With `HEARTH_RUN_HTTP=1` → **262 passed**.
- New `DiffusionEditProfileTests` (12): profile isolation, edit folder discovery of the Edit DiT by
  name, optional mmproj not-required-but-emitted, `-r` + CFG 2.5 + `--flow-shift 3` and no `-i`,
  generation argv unchanged (no flow-shift/`-r`, CFG 4.5), missing-file refusal naming the profile,
  disabled wording, shared-binary fallback, settings surface, tool registration/gating, edit_image
  requiring both image and prompt. All 19 pre-existing diffusion tests still pass unchanged.
- Live admin endpoints via `TestClient`: `/api/admin/image_edit/status` returns the profile shape with
  `label`; `/command` builds; `/test` refuses actionably when disabled; the `image_edit` Loaded-models
  row appears only after enabling editing.

### Operator verification checklist (session 13) — needs real Qwen Image Edit weights + sd-cli
1. Admin → Images → *Image editing*: point *Edit model folder* at a folder with an Edit DiT, a
   Qwen2.5-VL encoder, and the VAE; leave CLI path blank (reuses generation binary). Enable, Save.
   Expect three green file tiles + a grey "optional" mmproj tile; **✂️ Test an edit** returns ✓ in seconds.
2. Ask: "edit uploads/photo.png — remove the red car." Approve → an `edit_*` PNG lands in `generated/`,
   is shown back, and the assistant describes what actually changed and whether the rest held.
3. With only generation configured (editing off), ask for an edit → the tool names the missing edit
   setup; it must not fall through to `generate_image` or invent a result.
4. 2509 checkpoint: set the optional vision mmproj path → `Copy the sd-cli command` shows `--llm_vision`.

## Session 14 — admin path fields get a server-side 📁 Browse picker (DEC-080)

Operator request: "It's really annoying how I have to copy and paste the paths for all the
applications and ggufs… a button that opens the explorer would be better. And when the textbox
already has a file, it should open the folder that file resides in — starting from where I left off."

### What is verified (executed here)
- **`scripts/selftest.py`: 286 tests, OK (skipped=2)** with `HEARTH_RUN_HTTP=1`; and **OK (skipped=14)**
  without it. Was 271 before this session; the 15 new ones are `PathBrowseTests` (12) +
  `PathBrowseHttpTests` (3).
- **Real Chromium against a live server** (`scratch/browse_picker_smoke.py`, Playwright; starts Hearth
  on :8137 with a throwaway data dir). **28/28 checks pass**, covering: Browse buttons render only on
  path fields (`model.device` correctly excluded); an empty field opens at the filesystem root; typing
  a path in the address box navigates; folders sort above files; clicking a file writes it into the
  textbox and closes; **a field holding `…/alpha.gguf` opens listing its folder, with the sibling
  `.gguf` one click away** (the headline ask) and a "Showing the folder containing …" note; the name
  filter narrows to one row; Escape and backdrop-click dismiss; **choosing does not save** — the
  server still reports the old value afterwards; ↑ moves to the parent; **⌂ ~ lands on the server's own
  home** and ⌂ root clears back to the filesystem root; a permission-denied path shows an explanation
  and stays usable; Audio, Images and Project tabs all carry the button; the dialog fits a 390 px
  viewport; no JS console/page errors. Screenshots: `/tmp/browse_desktop.png`, `/tmp/browse_mobile.png`.

### Bugs found and fixed while verifying (all browser-observed, none by inspection)
1. **The listing never appeared.** `#pathPickList` was a child of `#pathPickBody`, but the loading
   spinner replaced `#pathPickBody.innerHTML`, destroying it; `drawPathEntries()` then queried a null
   and returned silently — the dialog sat on "Loading…" forever. Drawing now creates the container if
   absent instead of assuming it.
2. **Browse was dead on every tab except Model.** The tab handlers called `TABS[name]()` without
   awaiting, so `wireBrowseButtons()` ran against a panel that had not been drawn yet. It worked on
   Model purely because that render is fast. Introduced `showTab()`, which awaits `loadSettings()` and
   the render before wiring, and rewired every deferred re-render
   (`renderModel/renderAudio/renderProject` + their `setTimeout(..., 900)` variants). **This latent
   fire-and-forget pattern predates this session** — anything else added to those tabs inherits it, so
   prefer `await showTab(...)`/`.then(wireBrowseButtons)`.
3. Attribute-injection hazard on paths: `esc()` does not escape quotes, so a filename containing `"`
   would break out of `data-nav`/`data-pick`. Those attributes now carry `encodeURIComponent` output,
   decoded at click time.

### Two gotchas worth remembering (both hit during this session)
- **`selftest.py` grew by 13 tests that silently did not run.** The classes were appended *after* the
  `if __name__ == "__main__"` block, so `unittest.main()` collected 271 and reported green. The file's
  own gotcha list already warned about this; the run count is the tell — check it after appending.
- **A full-suite timeout is not a hang.** `python3 scripts/selftest.py` was observed "timing out" at
  110 s while finishing in ~18 s of test time plus interpreter/DB start-up; re-runs with a longer
  timeout completed with `OK`. Do not conclude a hang from one short-timeout run.

### Files touched
`hearth/pathbrowse.py` (new), `hearth/routes_admin.py` (`GET /api/admin/browse-path`; `is_path` on the
settings payload), `hearth/static/admin.js` (Browse button in `field()`, the modal, `showTab()` fix),
`scripts/selftest.py` (+13 tests, runner block moved to EOF), docs: DEC-080, FEATURES, SECURITY,
CONTINUATION, README layout.

### Operator checklist (nothing pending on my side; these need your machine)
1. Admin → Model → **📁 Browse** next to *llama-server binary path* → navigate to your `llama-server`
   and click it → the path appears in the box → **Save**.
2. Reopen Browse on that same field → it opens in the folder the binary lives in.
3. Point *Model (.gguf) path* at a `.gguf` in a different directory, then Browse again → you land in
   *that* directory, not the previous one.
4. Paste a path with `~` into the dialog's address box → it resolves (a pasted shell path works).
