# Decision Records

## DEC-001 — Orchestration stack
Question: Backend/orchestrator language + how it talks to llama.cpp.
Decision: Python 3 + FastAPI orchestrator; llama.cpp used via `llama-server` subprocess(es) exposing OpenAI-compatible HTTP (`/v1/chat/completions`). A pluggable provider layer adds a StubProvider so the app runs without llama.cpp.
Options considered: (A) FastAPI+llama-server [chosen]; (B) llama-cpp-python in-process; (C) Node full-stack.
Evidence: llama-server offers stable OpenAI-compat SSE + metrics, isolates native crashes per process, and supports parallel slots for multi-user. In-process bindings couple the interpreter to the native lifecycle and complicate concurrency.
Rationale: Robustness to llama.cpp version churn, clean streaming, process isolation, easy multi-slot. User selected A.
Reversibility: High — provider interface abstracts the backend.
Revisit trigger: Need for tight token-level control (logit biasing, grammar) that the HTTP API cannot expose.

## DEC-002 — Auth model + edge security
Question: Identity mechanism given external access.
Decision: Local accounts (username + password), `hashlib.scrypt` hashing (argon2 auto if available), HMAC-signed stateful session tokens with DB revocation, CSRF on mutations, per-account+per-IP rate limit + lockout, optional per-user TOTP 2FA (RFC6238 via hmac stdlib), optional Tailscale identity SSO header (`Tailscale-User-Login`) trusted only from tailnet/proxy. Bind to 127.0.0.1 by default; operator fronts with Tailscale/TLS.
Rationale: One identity model works LAN and external via tunnel without exposing a public port; hardening is transport-layer. User selected A + Tailscale-aware + TOTP.
Reversibility: High. Trusted-header mode shipped but disabled by default.

## DEC-003 — Minimal-dependency / stdlib-crypto policy
Question: Heavy auth libs vs stdlib.
Decision: Use stdlib `hashlib.scrypt`, `hmac`, `secrets`, and a compact HMAC-signed token; optional upgrades (argon2-cffi, pyotp) auto-detected but never required. Hard runtime deps: fastapi, uvicorn, jinja2, httpx, python-multipart, sqlalchemy, pydantic(+settings).
Rationale: Guarantees the prototype imports/boots in constrained environments; reduces supply-chain surface (Rule 11).
Reversibility: Medium.

## DEC-004 — Sandbox for gated command execution
Question: How to run shell commands safely.
Decision: `sandbox.py` auto-detects backend at runtime: bubblewrap (`bwrap`) → Docker/OCI → stub(refuse). Commands run with cwd = user workspace, no network (bwrap `--unshare-net`), read-only system mounts + writable workspace, dropped capabilities, timeout + output caps. Every exec is **gated**: requires explicit per-call user approval; admin can enable/disable globally. If NO sandbox exists we fail closed (refuse to run).
Rationale: Defense in depth + least privilege; graceful degradation when no sandbox present. User selected auto-detect.
Reversibility: High.

## DEC-005 — Workspace isolation model
Decision: Each user gets `data/workspaces/<user_id>/`. All fs tools resolve paths through a jail helper (`workspaces.py`) that rejects absolute paths, `..` traversal, and symlink targets outside the root (canonicalize + containment check). Uploads = LLM writes a file then emits a short-lived signed download URL scoped to that workspace.
Rationale: Meets "own workspace llm can see" + blocks cross-user access (Unacceptable-outcome guard).

## DEC-006 — Streaming + progress/debugging
Decision: SSE endpoint streams typed events: `token`, `tool_call`, `tool_result`, `action_required`, `progress` (prefill/eval), `usage` (tokens/s, prompt tokens), `error`, `done`. For llama-server, token counts come from the response `usage`; stub emits synthetic prefill progress so the debug panel is exercised without a model.
Rationale: Satisfies streaming + "how far it got with prefill" + tool visibility requirement.

## DEC-007 — Admin-managed protocols & system prompts
Decision: Protocols and system prompts are markdown documents stored in DB (editable via admin UI). Users can attach a protocol/system-prompt doc to a conversation; it is injected into the system prompt at build time. Model settings stored as typed key/value with help text + safe ranges, applied on model (re)load.
Rationale: Matches request; keeps prompts data-driven & auditable.

## DEC-008 — Context / RoPE defaults
Decision: Default `n_ctx = 262144`. Expose `rope_scaling` (`none|linear|yarn`), `rope_freq_base`, `rope_freq_scale`, `flash_attn`, `n_gpu_layers`, `n_parallel(slots)` as admin settings with inline documentation. Values map to llama-server CLI flags on (re)load.
Revisit trigger: OOM on target → lower n_ctx or enable quantization; documented in README.

## DEC-009 — Verification under no-execute tooling
Decision: This session lacks a command-execution tool, so runtime boot/tests are NOT executed by the agent. Deliverable includes `scripts/selftest.py` (stdlib unittest + optional FastAPI TestClient) and exact commands for the operator. Requirement statuses reflect this honestly (Delivery Record).
Rationale: Rule 4 / "tool unavailable → reduce the claim".

## DEC-010 — Client-driven agent loop over SSE-per-turn
Question: How to stream while supporting gated tools / ask_user that require human latency (mobile connections drop).
Decision: One SSE stream per *turn*. Server runs the model once and streams `token` events. Auto-approved tools (fs read/list, web search) execute server-side within the same turn; the loop continues silently. When a **gated** tool (exec) or `ask_user` is requested, the server persists a pending interaction, emits `action_required{pending_id,...}`, and closes the stream. The client resolves via POST `/api/chat/resolve`, which opens a NEW SSE turn continuing the conversation with the tool result appended.
Rationale: Avoids holding an open connection across unbounded human think-time (fragile on mobile/tunnel); every step is resumable; simpler retry semantics than a single long-lived stream. Reversibility: medium.

## DEC-011 — OpenAI tool-calling protocol fidelity
Question: How to store history so a real llama-server sees valid function-call messages.
Decision: Persist assistant tool-request rows with `meta.tool_calls` (id/type/function) and each tool-result row with `meta.tool_call_id`. `build_messages()` reconstructs the OpenAI message graph from these metas. The stub ignores ids; the real client relies on them. PendingActions carry `call_id` so a resumed gated call replies against the correct id.
Rationale: Correct function-calling semantics for llama.cpp's chat API without a separate event store.

## DEC-012 — scrypt memory parameters (bug fix from operator self-test)
Question: Why did password hashing raise `ValueError: memory limit exceeded`?
Cause: original params N=32768, r=8 need ~32 MiB, exactly at/over OpenSSL's default maxmem in Python's `hashlib.scrypt`, so it errored instead of hashing — which would have broken every login.
Decision: lower to N=16384, r=8, p=1 (~16 MiB, strong interactive workfactor) and pass an explicit `maxmem` (>= 128·N·r·p + headroom, floored at 32 MiB) in both hash and verify so stricter builds never error or silently fall back. Added a self-test regression guard asserting the params hash under the default limit.
Reversibility: High — params are stored per-hash in the encoding, so old hashes still verify; new hashes use the lower N.

## DEC-013 — Starlette TemplateResponse new signature (bug fix from operator boot)
Symptom: `GET /login` → 500 `TypeError: unhashable type: 'dict'` inside jinja2 cache lookup.
Cause: code used the legacy positional form `TemplateResponse(name, {"request": req})`. In current Starlette the first positional arg is `request`; passing the context dict as the name/cache-key broke it.
Fix: switched all three page routes to `templates.TemplateResponse(request, name, context)`.
Follow-up: pin a known-good fastapi/starlette version range in requirements.txt once smoke-tested (currently unpinned >=0.110). This is why runtime verification matters — static review missed an API-version drift the live server caught immediately.

## DEC-014 — Buried relative import in manager.status()/ensure_server() (bug fix from operator boot)
Symptom: `GET /healthz` → 500 `ImportError: attempted relative import beyond top-level package`.
Cause: two function-local `from ..config import get_boot_settings` inside `hearth/manager.py` (which is at `hearth/`, so `..` over-reaches). My earlier import fix only corrected module-top imports and missed these.
Fix: changed both to `from .config import …`. Then grep-verified NO remaining `from ..` in any top-level `hearth/*.py`; the only ones left are inside `hearth/tools/` where `..`=hearth is correct.
Lesson (Rule 4 honesty): my prior "import graph verified against package depth" claim was too narrow — it covered module-top imports only, not function-local imports. Runtime boot caught what static review missed. Going forward: scan for relative-import depth across ALL import statements (top + local), and pin fastapi/starlette versions after smoke test.

## DEC-015 — Stub provider KeyError on OpenAI tool specs (bug fix from operator live test)
Symptom: every assistant reply rendered as `'name'` while SSE/prefill/usage events streamed fine.
Cause: `StubProvider.stream` did `{t["name"] for t in tools}` but tools are OpenAI specs (`{"type":"function","function":{"name":...}}`). The KeyError escaped the async generator and was serialized by the route's error handler as bare `'name'`.
Fix: read the nested name via `t.get("function", {}).get("name")`. Also hardened both SSE routes to log full tracebacks server-side + emit `Stream/Resolve error: <Type>: <msg>` instead of a cryptic string. Added two self-test regression guards (stub streams with real registry specs; stub detects fs_list tool from spec names).
Lesson: static review missed this because the stub was never exercised against actual tool_specs() output until now — hence the new self-tests that build specs via the real registry.

## DEC-016 — `undefined` reply prefix + stub tool re-request loop (bugs from operator live test)
Symptom A: assistant replies began with literal `undefined`. Cause: chat.js token handler did `state.text += ev.text` but `state = {}` never initialized `.text`, so first concatenation produced `"undefined…"`. Fix: `state.text = (state.text||'') + ev.text`.
Symptom B: `list`/`read notes.md` repeated the same tool 6× then "reached max tool loops". Cause: StubProvider re-detected the keyword from the unchanged last user message on every agent-loop iteration and re-emitted the tool_request; the orchestrator correctly capped at MAX_TOOL_LOOPS but a well-behaved model shouldn't repeat. Fix: stub now checks whether a `tool` result already exists for the current turn (after the last user msg) and, if so, summarizes that result instead of re-requesting — mirroring how a real model sees its own prior tool output. Added self-test `test_stub_does_not_reloop_after_tool_result`.
Note: this is stub-specific scaffolding; a real llama.cpp model avoids it naturally by observing tool results in context. The MAX_TOOL_LOOPS cap remains as a safety net for any provider.

## DEC-017 — Self-test used non-existent setting key `tools.fs_list` (test bug, not prod)
Symptom: `test_stub_tool_detection` failed with `specs missing fs_list; got {'ask_user'}`.
Root cause: `FsListTool.setting_key == "tools.fs_read"` (fs list is gated under the same read toggle as fs read — by design). The test passed `{"tools.fs_list": True}`, a key nothing reads, so `enabled_tools()` correctly excluded fs_list and only the always-on `ask_user` remained.
Resolution: this is CORRECT production behavior — in the app `rs.get_all()` returns `tools.fs_read=True` (default), so fs_list IS enabled (confirmed by the working chat). Fixed the TEST to pass `{"tools.fs_read": True}`. Corrected a sibling test that had the same wrong key.
Lesson: the self-diagnosing assertion (`got {'ask_user'}`) pinpointed this in one run — worth keeping that pattern for future opaque failures.

## DEC-018 — Self-host frontend assets + Content-Security-Policy (hardening)
Question: How to remove third-party runtime dependencies and add XSS hardening?
Decision:
- Vendor Tailwind locally at `hearth/static/vendor/tailwind.js` (fetched once via curl; not committed — README documents the command + sha256 pinning). Removed the `https://cdn.tailwindcss.com` script tag. HTMX was never actually used (plain fetch in app.js/chat.js), so it is intentionally not vendored.
- Moved inline `<style>` from base.html → `hearth/static/base.css`, and login's inline `<script>` → `hearth/static/login.js`, and removed the static `style="width:0%"` attribute, so no HTML contains inline script/style that a strict CSP would block.
- Added pure-ASGI security-header middleware (`_add_security_headers`) setting on every response: CSP (`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'`), X-Content-Type-Options nosniff, Referrer-Policy same-origin, X-Frame-Options DENY, and HSTS when cookie_secure (TLS).
Options considered: keep CDN + whitelist it in CSP (rejected — keeps third-party trust + privacy leak); full static Tailwind CSS build to drop 'unsafe-inline' on styles (deferred — adds a Node/build step, conflicts with the no-build-step decision; tracked as follow-up).
Rationale: strict script-src is the primary XSS win and is achievable now; style 'unsafe-inline' is a low-risk, well-understood concession to Tailwind's runtime JIT. Self-hosting kills per-page-load third-party calls (privacy + supply chain) and makes rendering independent of any CDN being up.
Reversibility: High.
Trade-off / residual: Tailwind dev-build JS (~400KB, JIT in browser) is heavier than a static CSS bundle; acceptable for family scale. If stricter CSP desired later, do the static-CSS build and remove 'unsafe-inline' from style-src.
Verification: new HttpTests assert CSP present with `script-src 'self'`, no CDN origin in policy or page HTML (runs under HEARTH_RUN_HTTP=1).

## DEC-019 — Durable artifacts (per-user private files, re-downloadable)
Question: How to make agent-produced and user-uploaded files persistently downloadable per session, replacing ephemeral in-memory download tokens?
Decision: New `artifacts` table (id, user_id, conversation_id, kind∈{produced,uploaded}, filename, rel_path, size, mime, created_at). Downloads via `/api/artifact/{id}` enforce ownership at read time (no secret token needed; id is opaque but authz-checked). `fs_write` and the `upload` tool now record artifacts; `/api/artifacts?conversation_id=` lists them. Ephemeral `/api/download/{token}` kept only for backward-compat.
Rationale: Satisfies "artifacts of their sessions to download afterwards again" durably across restarts; ownership-scoped so users never see each other's files (extends the workspace-jail invariant to the download surface).
Reversibility: High. Trade-off: file bytes still live in the workspace; deleting an artifact removes only the row (documented).

## DEC-020 — User → server uploads (drag/drop, picker, camera)
Decision: `POST /api/upload` accepts multipart with 1..N files, CSRF-guarded, streamed to disk in 1 MiB chunks with a 25 MiB/file cap and filename sanitization, stored under `uploads/<safe-name>` inside the caller's jail, each recorded as an uploaded Artifact. UI: desktop drag-&-drop overlay on the whole window, a picker button (`<input type=file multiple>`) for desktop+mobile, and a camera button (`accept="image/*" capture="environment"`) that opens camera/gallery on mobile.
Rationale: Operator requested drag-drop (desktop essential) + multi-file picker + image-from-gallery/camera (mobile). Jail + size cap + sanitize mitigate upload abuse. Reversibility: High. Residual: no AV scanning of uploads (family trust boundary; noted).

## DEC-021 — Device "allow resources" grant (mocked, integrated) — SUPERSEDED by DEC-034
Decision: A header toggle ("Allow device access") records a client-side session grant and messages the transcript; single-file upload serves as the concrete resource path today. The existing device_* tools remain stubs behind the documented helper protocol. This wires the UX/flow now so real helpers can drop in without UI changes.
Rationale: Operator wanted the flow integrated before agents arrive, even if mocked. Honest labeling: it is a mock grant, not real device access. Reversibility: High.
**Superseded (session 5, DEC-034):** the client-side mock was replaced by a server-stored per-conversation grant that the device tools actually enforce; uploads remain ungated deliberately.

## DEC-022 — Lightweight i18n (German + English), per-user preference
Decision: `hearth/i18n.py` flat key→string tables (en/de) with fallback EN→key; per-user `language` column ("de"|"en", default en) added via idempotent migration. Server `/api/i18n` returns the user's table; client `i18n.js` applies to `data-i18n*` attributes and exposes `I18N.t()` for dynamic strings (approval buttons, etc.); `/auth/me` returns language; `<html lang>` set. A self-test asserts every EN key has a DE translation.
Rationale: Family is German-speaking; no framework needed at this scale; missing-key fallback prevents blank UI. Reversibility: High. Residual: model *output* language is not forced (that's prompt-level); the setting localizes the app chrome only.

## DEC-023 — Remove hard FK on Artifact.user_id; surface silent bookkeeping errors (bug from operator self-test)
Symptom: `test_upload_tool_returns_artifact_link` → `sqlite3.IntegrityError: FOREIGN KEY constraint failed`; `test_write_creates_durable_artifact` → write succeeded but `meta == {}` (no download link).
Root cause: `Artifact.user_id` was declared `ForeignKey("users.id")`. But workspaces/artifacts are keyed by user-id STRING and can exist without a users row (lazy workspace creation is intentional). The FK rejected orphan owner ids, and FsWriteTool's broad `except Exception` swallowed the failure into a bare `{}` — meaning in production an artifact could silently fail to register.
Fix:
1. Dropped the FK on `Artifact.user_id` (kept it indexed). Ownership isolation is enforced by filtering on that column at query time (artifacts.get/list_for require matching user_id), NOT by referential integrity — which is the correct boundary here and matches how workspaces already behave.
2. Added idempotent migration `_migrate_artifacts_drop_fk()` in db.py: detects an existing FK-bearing `artifacts` table, rebuilds it without the FK preserving rows + indexes (SQLite can't ALTER-drop constraints). Runs at init_db; safe on fresh DBs (no-op).
3. FsWriteTool no longer silently swallows artifact errors: on bookkeeping failure it still returns success for the write but sets `meta.note="artifact_record_failed"`, adds a visible message, and audit-logs — so silent drops can't recur unnoticed.
4. New self-test `test_artifact_for_owner_without_user_row` guards this exact case.
Lesson: an FK that couples durability to account existence contradicted the lazy-workspace design; and a broad except that returns success with empty meta hid a real failure. Both are "silent wrongness" — now made loud or impossible.

## DEC-024 — Stage attachments client-side; send with the message (not per-file)
Question: Uploaded files shouldn't trigger a model reply each time.
Decision: Files are uploaded immediately (to obtain an owned artifact_id + land in workspace) but held in `S.attachments` and only referenced when the user hits Send. The message payload carries `attachments:[{artifact_id,name,mime}]`; server re-resolves them via `artifacts.get(user.id, …)` (never trusts a client path) and records them on the user message meta. An attachments-only message (no text) is allowed. A staging strip shows removable chips.
Rationale: Matches operator request; avoids the model answering every file; keeps upload durable + ownership-checked. Reversibility: High.

## DEC-025 — Images as multimodal base64 (data URLs), never raw bytes
Decision: When building provider messages, user messages with image attachments become OpenAI multimodal content: `[{type:text}, {type:image_url, image_url:{url:"data:<mime>;base64,..."}}]`. Non-images stay text. The stub flattens any content list to text (images → `[image]`) so it never dumps base64 and keyword detection still works. Size-capped at 8 MiB/image read.
Rationale: Operator explicitly required images be reformatted for vision models (mmproj), not raw bytes in the chat. Reversibility: High. Note: only meaningful with a vision-capable llama.cpp build + mmproj; text-only models ignore image parts gracefully.

## DEC-026 — Workspace file context injected into system prompt
Decision: `build_messages` appends a compact listing of the user's workspace files to the system prompt (`# Workspace files (readable via fs_read)`), so both stub and real model know what exists in the conversation's user space without a tool round-trip. Capped at 80 entries; failures degrade to no-op.
Rationale: Operator asked that the mock model "have access to all files within the conversation." Reversibility: High.

## DEC-027 — Conversation rename / delete(+artifact choice) / search; artifacts scope toggle
Decision: New endpoints — `rename` (title), `delete` (messages always removed; artifacts deleted only if `delete_artifacts`, else detached to conversation_id="" so they persist under "all"), `search?q=` (primary title ilike + secondary message-content ilike with snippet). Client: search box (debounced), Rename/Delete header buttons (delete confirms twice: delete? then also-delete-files?), and an Artifacts scope radio (this chat / all).
Rationale: Operator requested all of these. Deleting artifacts is opt-in and destructive → double confirm. Reversibility: High. Bulk message delete uses synchronize_session=False to avoid SQLAlchemy flush-ordering issues; conversation row deleted after its messages to respect FK order.

## DEC-028 — Cooperative turn cancellation via a per-conversation registry (`cancel.py`)
Question: no Abort button; a long or runaway turn could only be waited out.
Decision: `hearth/cancel.py` holds one `CancelHandle` (an `asyncio.Event`) per **conversation**, registered by `_stream_turn` in routes_chat and released on every exit path (normal end, abort, exception, client disconnect). The orchestrator checks `cancel.cancelled` at safe points — between provider events and before each tool — and raises `TurnAborted`, which `run_turn` converts into exactly one `aborted` SSE event. Client: Send swaps to **Stop** while busy, Escape also aborts; it POSTs `/api/chat/abort` *and* aborts the local fetch so the UI reacts instantly even if the server is slow.
Alternatives considered: cancelling the ASGI task only (leaves tool side effects unannounced and history half-written with no event to say so); a per-user registry (aborting one chat would be ambiguous). Reversibility: High.
Residual: in-process state, same assumption as the rate limiter — after a restart nothing is running anyway. A tool already inside `run()` (e.g. an exec) is not interrupted mid-call; it finishes, then the turn stops. `GET /api/chat/running` lets the UI re-sync after a reload instead of guessing.

## DEC-029 — Tool-loop cap becomes an admin setting (`agent.max_tool_steps`)
Decision: the hardcoded `MAX_TOOL_LOOPS = 6` is now `agent.max_tool_steps` (int, 1–50, default 6) in a new `agent` settings group; `_max_steps()` reads it defensively (garbage → default, never "no cap"). `tool_call` events carry `step`/`of` so the UI shows `(2/6)` and the over-limit error names the setting.
Rationale: multi-step jobs (write several files, search-then-read) legitimately need more than 6 round-trips, while a chatty model needs fewer; this is exactly the kind of knob the contract says should be admin-editable with inline help. Reversibility: High.

## DEC-030 — Announce every tool call before it runs (including gated ones)
Decision: `tool_call` events are emitted for **all** requested calls up front — auto and gated alike — each with a compact human `summary` (`_args_summary`: path + char/line counts, clipped command, query; never file contents) plus `awaiting_approval`. The client keeps one "running" chip per call in a FIFO list keyed by tool name, replaces it with the ✓/✗ result chip when `tool_result` arrives, and shows a pulsing activity line meanwhile.
Rationale: operator reported "asked it to create a file and it showed nothing". Silence during tools reads as a hang; gated calls previously produced *no* hint at all before the approval card. Reversibility: High. Note: chips are ephemeral UI — history still renders completed tool chips on reload (unchanged).

## DEC-031 — Per-turn generation options (`gen`), one-shot Force respond
Decision: `{no_tools, max_tokens}` in the request body become a `gen` dict threaded through `run_turn`/`_agent_loop`. `no_tools` sends an empty tool spec list for that turn; `max_tokens` is clamped to `[16, model.max_tokens]`. The composer's **⚡ Force respond** arms `no_tools` for the *next send only*, then restores Agent mode; **🤖 Agent mode / 💬 Chat only** is a persistent switch for subsequent messages.
Rationale: "force respond" and "talk as agent" are opposite ends of the same control. Making Force respond one-shot avoids the classic trap where a user believes tools are still available on the next message. Honouring `no_tools` from the client is safe because it can only *reduce* what the model may do — it cannot enable a tool, skip an approval gate, or grant device access. Reversibility: High.

## DEC-032 — Retry = drop the trailing assistant/tool rows and re-stream
Decision: `POST /api/chat/retry` finds the last `user` message and deletes non-user rows created at/after it (excluding any later user row), then streams a fresh turn without appending a new user message. It refuses with 409 while a turn is running; `/api/chat/stream` likewise rejects a second concurrent turn per conversation.
Rationale: regenerate-style retry without duplicating the prompt or losing attachments. Deleting only assistant/tool rows keeps the OpenAI tool_call/tool pairing valid (a dangling `tool` row whose `assistant.tool_calls` parent was removed would break llama-server). Reversibility: High. Residual: same-second timestamps make `created_at >=` ordering coarse; ids and role filters keep it safe in practice.

## DEC-033 — Context bar from persisted usage, not local tokenisation
Decision: each turn's `usage` is written onto the newest assistant message row (`save_turn_usage`, run in the SSE generator's `finally` so aborts still record it). `/api/chat/context-usage` returns used/n_ctx via a pure `context_usage_payload()` (unit-tested): real `prompt_tokens` when recorded, else a ~4 chars/token estimate flagged `source:"estimate"`; `n_ctx` prefers llama-server's live `/props` value over the configured setting; ≥80 % sets `warning`.
Rationale: Hearth has no tokenizer and would guess wrong for chat templates/tool schemas; the previous turn's actual prompt size is the number that matters ("what will the next send cost"). Persisting on the row (not memory) survives restarts and needs no new table. Reversibility: High.

## DEC-034 — Device access becomes a real, server-side per-conversation gate
Question: operator noted "Allow device access does nothing? I could still upload files without it."
Decision: two deliberate parts. (1) The button now flips `conversations.device_granted` via `/api/chat/device-access`; routes inject it into the settings copy as `_device.granted` (read from OUR DB, never the request body) and `tools/device_tools._gate()` refuses unless granted — so the switch actually gates `device_list`/`device_screenshot`. Admins can drop the requirement (`agent.require_device_grant`) or pair a helper (`device.helper_url`, env fallback). (2) Uploads/downloads are **not** device access and stay ungated by design: the user chose those bytes and that destination. The UI states this explicitly under the button, and reports whether a helper is paired so "nothing happens" is explained rather than mysterious.
Alternatives considered: gating uploads behind the switch (would break the primary file flow for a semantic misunderstanding); leaving it a pure mock (dishonest). Reversibility: High. New column added via idempotent ALTER (`db._migrate_add_columns`); existing chats start un-granted.

## DEC-035 — Start/Stop llama-server instead of a no-op "Reload"
Decision: `manager.start(force)` / `manager.stop_server()` with `POST /api/admin/model/start` (`{restart?}`) and `/stop`; the admin Model panel shows **Start**, or **Restart + Stop** when something is up, with live status text during slow loads. `force=True` stops then relaunches so saved settings apply; `status()` now reports reachability regardless of backend plus `startable`/`managed_by_hearth`, and a server pointed at by `HEARTH_LLAMA_BASE_URL` is never launched or killed. `/model/reload` stays for compatibility.
Rationale: the old "Reload model" button only re-read status — it never launched anything, so changing `n_ctx` looked like it did nothing. Starting/stopping is what admins actually mean. Reversibility: High. Chats still pick a provider via **Backend**; Start alone does not switch a stub chat to llama-server (the panel says so).

## DEC-036 — Idle-stream guard with a Python-version escape hatch
Decision: `_stream_checked()` wraps the provider stream in `asyncio.timeout(HEARTH_STREAM_IDLE_TIMEOUT_S, default 300)` and emits an error+done event on timeout; each forwarded event also re-checks cancellation. Because `asyncio.timeout` is 3.11+, the guard degrades to plain forwarding (Abort still works) when unavailable or when `HEARTH_STREAM_IDLE_GUARD=false`.
Rationale: a wedged llama-server slot previously left the browser spinning forever with no way out except reload. Reversibility: High.

## DEC-037 — Turns must survive their conversation being deleted (FK crash found by operator selftest)
Symptom (operator run): `test_no_tools_turn_ignores_tool_requests` → `sqlite3.IntegrityError: FOREIGN KEY
constraint failed` inserting into `messages`. The test had invented a conversation id; `append_message()`
wrote a message with a dangling `conversation_id`. The sibling abort test passed only by luck — its
cancellation fired before any write, so it never reached the same path.
Why this is not just a bad test: the same sequence happens in production. A turn can outlive its own
conversation, because **abort is cooperative** — the user presses Stop while a tool is executing, the tool
finishes (by design), and in that window they delete the chat; or a stale tab streams into an id that no
longer exists. Previously that surfaced as a raw 500 out of the SSE generator.
Fix:
1. New `orchestrator.ConversationGone`. Every write point checks the row first — `append_message()`,
   `_persist_pending()` (whose `conversation_id` is a real FK) — and raises it; `_agent_loop` probes once
   per iteration before asking the model anything; `run_turn` converts it to a quiet
   `{type:"done", conversation_gone:true}` instead of an exception.
2. `routes_chat.save_turn_usage()` gained a `_conversation_exists()` guard, so post-stream bookkeeping
   never writes onto rows belonging to a deleted chat (it runs in `finally`, i.e. on every exit path).
3. Test fixed at the cause: it now creates the user + conversation (`_make_conv`) instead of weakening the
   assertion, plus a new regression test that deletes the conversation mid-life and asserts no raise and
   zero orphan messages.
Lesson: an FK failure seen only in a test is worth reading as a production signal — here the "test setup
bug" was the only thing standing between a real user action (delete during abort) and a crashed stream.
Reversibility: High. Cost: one extra `s.get()` per loop iteration and per write; negligible next to a
model round-trip.


## DEC-038 — Live tool_progress events while the model composes a tool call
Symptom (operator, real llama-server): "write me a python script" → the UI showed only "thinking…" for
the entire time the model generated the `fs_write` arguments (tens of seconds for a ~1.4 KB file), then
the write chip + result appeared instantly. With no tokens streaming (the completion is pure tool-call
JSON) there was zero visible feedback — it looked like a hang.
Decision: new provider event type `tool_progress` (`StreamEvent.tool_progress(name, args_so_far)` in
`llm/base.py`). `LlamaServerProvider.stream()` emits one per ≥256 B of newly accumulated argument bytes
(throttled `_prog_slot`; the stub never emits it — its calls are instant). The orchestrator converts it
to an SSE dict `{type:"tool_progress", name, chars}` — **only the tool name and the byte count cross the
wire; the raw JSON fragment (which contains the whole file being written) never leaves the server**.
The client (`chat.js toolProgress`) renders a ✎ chip per in-flight call showing
`writing · 1.4 KB cool_script.py` — the filename appears once a `"key": "value"` pair has arrived in the
streaming JSON (the client regexes the first string value out of the fragment; until then the chip shows
just name + size). The chip is superseded by the normal pending chip on
`tool_call` and removed on `tool_result`/abort/error (`finishProgress`, `clearPending`).
Rationale: showing the actual content would mean streaming file bytes through SSE before the tool even
runs (and re-sending them inside the final `tool_call` args) — a live size counter gives the same
"it's working" assurance without duplicating payload or leaking partial content into the transcript.
Reversibility: High — dropping the event type restores the old silent gap; nothing else depends on it.

## DEC-039 — Workspace listing with sizes + "don't re-upload what you just wrote"
Two operator observations from the same session: (a) the agent should be able to see its local files,
especially user uploads; (b) after `fs_write` produced a file, the model ALSO called `upload`, yielding a
duplicate chip and a second download button for the identical file.
Findings: (a) was already mostly covered — `fs_list` exists (with sizes) and `_workspace_context()`
appends a file listing to the system prompt every turn. Gap: the listing had NO sizes, so it was not an
ls -l equivalent. Fix: sizes added (`name (N B)`), header now says "ls -l equivalent; read with fs_read,
list a folder with fs_list" and notes that `uploads/` holds the user's attachments. (b) is model
behaviour, not a bug — but it is promptable: the workspace preamble in `prompt.py` now states that
fs_write/fs_append already register a downloadable artifact and that `upload` is only for offering an
EXISTING file (e.g. one of the user's uploads). This is advisory; small models may still double-call,
which is harmless (a second identical download button) but no longer the taught behaviour.
Reversibility: High — prompt text + listing format only; no protocol or schema change.


## DEC-040 — One artifact per workspace file; one download button per file
Symptom (operator, real model): "create a hello world python script and then provide the download" →
the agent called `fs_write` **and** `upload`, and the file appeared **twice** in the Artifacts panel with
**two** identical download buttons. DEC-039 had tried to fix this with prompt advice only, and explicitly
called the duplicate "harmless". It is not: it reads as a bug, and the panel is the one place users look
for their files.

Findings: `artifacts.record()` unconditionally inserted a new row, so every tool call that registered the
same path (`fs_write`, then `upload`; or writing a file twice) added another row for one file. The client
additionally rendered a download button for *every* `tool_result` carrying `meta.download`.

Decision (three parts):
1. **Identity by path.** `artifacts.record()` now looks up an existing row for `(user_id, rel_path)` and
   refreshes it in place — same id, updated size/kind/filename/conversation — instead of inserting a
   duplicate. Paths are normalised (`_norm_rel`) so `./a.txt`, `dir//a.txt` and `dir/a.txt` cannot create
   two records. Keeping the id stable means download links handed out earlier stay valid and keep pointing
   at the (now updated) content; the panel shows the file once, at its newest size.
2. **Deliberate asymmetry for user uploads.** `/api/upload` passes `new_row=True`: attaching the same
   filename twice is two transfers the user made, and each message's attachment should stay fetchable even
   though the workspace path was overwritten in between. Only *tool-side* registration dedupes.
3. **Client de-dupes the transcript.** `chat.js` keeps a per-turn `Set` of `artifact_id|name` and renders
   at most one download button per file, so a redundant `upload` after a write cannot stack buttons even
   against an older server build.

Also: the model is told harder — the workspace preamble now says that writing a file already produced its
download button, that it is *done*, and that `upload` is only for files it did not just write. This remains
advisory; parts 1–3 make the outcome correct regardless of what the model does.

Alternatives considered: making `upload` a no-op after a write (breaks offering an existing file, e.g. one
of the user's uploads); suppressing the button for `fs_write` results so only `upload` shows one (the
operator phrased it that way, but it is worse UX — writing a file *is* producing a file, and users would
then get no button unless the model happened to make a second call); client-side-only dedupe (leaves the
panel duplicated). Reversibility: Medium-high — the dedupe is one query in `record()`; reverting restores
append-only rows, but existing refreshed rows have already collapsed duplicates.

Residual: two *different* conversations writing the same workspace path now share one artifact row and the
row follows the most recent conversation (`conversation_id` is re-stamped), so it appears under that
chat's "this chat" scope. Accepted — workspaces are per user, not per chat; a future per-conversation file
view should read message metas rather than lean on this column.


## DEC-041 — Make write progress unmissable (path in the chip + smoothed counter)
Symptom (operator, real llama-server): asking for a script produced no visible indication that a file was
being written — DEC-038's `tool_progress` exists, but with a ~22-token/s model and a 22-byte file the call
completes in ~1 s and its single hint fires only after ≥256 B of arguments, i.e. never. The user's words:
"there was no indication that the agent was actually writing a file… show a number go up".

Decision:
1. **Early first hint.** `_prog_slot()` now emits as soon as ~32 B of arguments exist (then keeps the
   ≥256 B throttle). A short write gets a chip immediately instead of nothing; SSE volume is unchanged for
   long writes, and the client smooths between hints anyway.
2. **Which file?** `tool_progress` gains `first_name`, scraped server-side from the *partial* JSON with a
   tolerant regex (`_FIRST_STR_RE`) — the first completed `"key": "value"` string, which in practice is the
   path. The orchestrator forwards it as `path_hint`. Only that one already-complete scalar crosses the
   wire; content never does (a regression test feeds content that itself looks like `"key": "value"` and
   asserts it cannot become the hint). This replaces DEC-038's client-side regex over a fragment that was
   never actually sent.
3. **A counter that moves.** `chat.js` now renders the chip as `✎ fs_write · writing hello_world.py · 240 B
   · ~40 %` with a bar. Because hint cadence is coarse and unpredictable, the displayed completion eases
   towards an estimated target size (largest tool call seen this page session, ratcheting up mid-call,
   decaying after 30 s idle so one huge zip cannot pin later chips at "nearly done"), and always creeps
   forward a little even if no further hint arrives. It is labelled `~` and capped below 100 %: it signals
   liveness, never claims facts about the file. The real byte count is what is shown as the number.
4. **The stub cannot help.** `StubProvider` calls are instant, so progress there is inherently a flash;
   live progress is a streaming-model feature. Documented rather than faked — inventing delays in the stub
   would misrepresent it as verified behaviour.

Alternatives considered: streaming the actual file text into the chip (doubles payload and puts partial
content in the transcript before the tool ran — DEC-038 rejected this and still does); a fake spinner with
no byte count (ignores the real signal available); per-delta hints (SSE spam for no visible gain).
Reversibility: High — `path_hint` is additive on an existing event; removing the easing restores the raw
step-wise counter.


## DEC-042 — Per-round usage events; artifact ordering must not depend on wall-clock seconds
Operator ran `scripts/selftest.py` after session 7 → 6 failures. Four were my new/edited tests being
wrong or the code behind them genuinely broken; two exposed defects that had nothing to do with the
changes they surfaced. All six are fixed here, and each is worth recording because none of them were
obvious from reading the code.

**(a) Tool-only turns emitted no `usage` event at all — context bar froze, tok/s badge blank.**
`_agent_loop` yielded `usage` only inside the provider stream, but llama-server attaches usage to its
FINAL chunk (after `finish_reason`) and the loop breaks as soon as tool requests arrive. So every turn
that did work reported nothing; only the final text reply did. That also made session 7's write-progress
feature look worse than it was: on a write-heavy turn the header went dead exactly while the model was
busy. Fix: each round tracks `prompt_tokens_est` (new `_estimate_prompt_tokens`, multimodal-safe — an
image part is counted as one part, never its base64 length) plus generated characters (content **and**
tool-call argument fragments, which required the new `ToolRequest.args_raw`), and synthesises a usage
event **only when the provider reported none** (`turn_usage is None`). The stub now reports its own
per-round usage instead, so nothing is double-counted. Estimates carry `estimated: True`, are shown as
`~` in the badge, and are deliberately never merged into the value persisted for the context bar — a
chars/4 guess about tool-call JSON is not comparable to llama-server's measurement, and
`save_turn_usage()` cannot tell them apart.

**(b) `artifacts.created_at` was too coarse to order anything.** The reported "file listed twice" had a
second cause underneath the duplicate rows: `now()` is whole seconds, so `fs_write` then `upload` created
two rows with identical timestamps and `order_by(created_at desc)` chose between them arbitrarily — which
is also *which* row dedupe would refresh. Fix: `created_at` is now a monotonic counter (`_next_seq`, stored
under the `artifacts.seq` settings key), seeded from `max(existing created_at, now())` so new artifacts
still sort after legacy epoch rows instead of sinking to the bottom of the panel; `list_for()` gained a
secondary `id` sort for stable output. `models.SCHEMA_VERSION` bumped to 2 to mark the change of meaning.

**(c) Test bugs of my own, now corrected rather than papered over:**
- `_prog_slot()` kept its throttle state in a dict keyed by tool-call index but I also wrote a `"started"`
  **string** key into it — so `last_prog.get(-1, 0)` never matched and the provider emitted *zero* hints.
  Three tests failed at once from that one line; state is now a per-index dict `{"n", "started"}`.
- `test_tool_progress_streams_to_client_without_raw_args` asserted a flat `[100,500,900]`, but its fake
  provider never lets a tool result reach the model, so the agent loop legitimately re-streams until the
  step budget runs out — six rounds of the same hints. The test now asserts per-round shape + count and
  keeps the leak assertion, which is what it was ever guarding.
- `test_workspace_context_shows_sizes_and_uploads` asserted `"attachments"` in `_workspace_context()`,
  but that word lives in `prompt.WORKSPACE_PREAMBLE`; the listing says "the user attached". Both are now
  asserted where they actually live — the prompt's "read attachments before asking the user to re-upload"
  guidance was also reworded so the wording and the assertion match.

Reversibility: (a) high — delete the estimate path and the header goes back to silent tool turns;
(b) medium — reverting leaves a stale `artifacts.seq` setting and monotonic timestamps that still sort
correctly, just no longer epoch-shaped.


## DEC-043 — Late binding in `stream()` silently killed every progress hint (and why two diagnoses were wrong)
Second operator run after DEC-042: the artifact and usage fixes passed, but the same three
`tool_progress` tests still reported **zero** hints. My first explanation ("a `"started"` string key
polluted an int-keyed dict") was not the cause — that dict lookup defaulted harmlessly to 0.

Real cause: `_prog_slot(idx, slot)` was a closure defined in `stream()` and called from *inside*
`async for`. Its body therefore ran **after** the enclosing loop had rebound `slot` to the next tool call,
so per-slot state was unreachable through normal paths; what actually made hints fire (before my change)
was the bogus `last_prog[-1]` key — an accidental shared counter that my "cleanup" broke. Any read via a
real index returned `{}`/default, so `n - 0 < 256` and no hint was ever emitted.

Fix: `_prog_hints(idx, args, name, first) -> list[StreamEvent]` takes **values**, not the loop's dict, keeps
all state in `last_prog`, and returns a list so a fast provider that delivers several 256 B intervals
between checks emits them all instead of one.

Two process lessons, because both are things I got wrong out loud rather than in code:
1. **A swallowed exception can look like "no output".** The provider's `except Exception` wrapped the whole
   stream loop, so any error thrown while parsing aborted the generator *before* `tool_request` was emitted
   and produced an event list indistinguishable from a provider that sent nothing. It now prints the
   traceback to stderr and yields an `error` event. The three tests assert no stray `error` event, so a
   swallowed failure can never again masquerade as "the throttle is too coarse".
2. **Do not change code you have not reproduced.** Two consecutive rounds of confident root-causing on this
   one function cost the operator two test runs. The cheap move that should have happened first was to make
   the failure self-reporting (above), not to rewrite the logic again.

Also: `scripts/selftest.py` classes had drifted so abort/tool-progress/context-bar tests ran under
`CryptoTests` — misleading in exactly this kind of debugging session. Test-class placement should be kept
honest when tests are added or moved.


## DEC-044 — The real cause of "zero tool_progress hints": a broken test fake, hidden by a catch-all `except`
Third operator run. The traceback that DEC-043 added printed the answer immediately:

```
File "hearth/llm/llama_server.py", line 113, in stream
    async with httpx.AsyncClient(...) as c:
TypeError: 'FakeClient' object does not support the asynchronous context manager protocol
```

**Root cause: the test fakes were wrong, and had been for a long time.** `FakeResponse` defined
`__aenter__`/`__aexit__`, but the provider enters the *client* with `async with httpx.AsyncClient()` first.
The fake client had no such protocol, so the generator raised at line 113 — before parsing a single line —
and the provider's catch-all `except Exception` swallowed it into an `error` event. The tests then asserted
on `tool_progress` counts and reported "0 hints, throttle too coarse".

DEC-042's diagnosis (int-vs-string dict key) and DEC-043's (late binding in `async for`) were **both wrong**.
They are real code-quality concerns and the fixes are worth keeping on their own merits, but neither caused
this. The only reason it took three rounds is that the failure was invisible: a broad `except` around a whole
generator converted "raised at line 113" into "produced nothing", which every test read as a tuning problem.

Fixes:
1. **Shared, correct fixture** `_fake_llama_client(lines)` in `scripts/selftest.py` — implements the client
   *and* response context-manager protocols, and restores `httpx.AsyncClient` via the returned original. The
   three copies that previously existed had drifted from the real interface independently; one helper cannot.
2. **Narrowed the try/except in `LlamaServerProvider.stream()`** to cover only the HTTP connect/enter, so a
   parse-loop bug can no longer be mistaken for provider silence. The stream response is released in a
   `finally`, including on client abort and on a raised parse error (which now propagates).

Process note, because it is the transferable lesson: when a test reports "feature produced nothing", make it
*self-reporting* before changing the feature. Two rounds of confident root-causing were spent reasoning about
throttle arithmetic while the code was dying on line 113. `traceback.print_exc()` in a swallowed-exception
path costs one line and settles the question outright.


## DEC-045 — Downloads come from `upload` only; verbatim reads; anti-loop guardrails
Operator report, real llama-server, two screenshots. (a) "The chat provides a downloadable link for the
fs_write, but not the first upload tool call — and later it provided both. I actually don't want the file
provided by fs_write at all, ONLY by upload." (b) A `fs_edit` that kept failing `old_text not found`,
re-reading the same file and re-issuing the identical edit until "stopped after 6 tool steps", then sitting
on "thinking…".

Findings:
1. **The button was a policy, not a bug** — DEC-040 deliberately rendered one ⬇ per *file*, drawn by
   whichever call carrying `meta.download` landed first. Because `fs_write` answers before `upload`, the
   write drew it and the client's per-turn dedupe (`artifact_id|name`) then suppressed the upload's
   identical key. That is exactly why "the first upload produced no button but fs_write did" — and why a
   later turn, where the model only called `upload`, showed one for the upload. Confusing by construction.
2. **The edit loop had a concrete harness cause.** `fs_read` with a line range returned lines prefixed
   `1: `, `2: ` … ("keep the harness convention"), while a whole-file read returned raw text. Models quote
   what they were shown, so `old_text` carried the numeric prefixes, `ws.edit_text` matched literally, and
   every attempt failed — re-read, same prefixes, same failure, until the step budget ran out. A small model
   makes that loop tight; the harness put a gun on the table.
3. The activity line said "thinking…" after *every* tool result, including the last one of a turn, which is
   how a finished turn reads as a hang.

Decision (four parts):
1. **Only file-offering tools return `meta.download`.** `fs_write` / `fs_append` still register the durable
   artifact row (so the file appears in the Artifacts panel immediately and stays downloadable there), but
   no longer return a download reference; `upload` and `zip_pack` — whose entire purpose is handing a file
   over — keep it. Tool descriptions and the workspace preamble now state this, so the model calls `upload`
   when (and only when) the user asked to receive a file. This reverses DEC-040's "button per file" choice,
   whose rejected alternative ("suppress the button for fs_write results so only upload shows one") is now
   the rule: the operator owns that product decision, and it removes both the duplicate-button confusion and
   the unrequested buttons. `chat.js` keeps its per-turn dedupe as a belt for older servers; history replay
   renders at most one ⬇ per file and only for `upload` rows (older transcripts still carry write metas).
2. **Reads are verbatim.** `fs_read` line ranges return exactly the file's lines — no numbering. A range
   read is now byte-identical to the corresponding slice of a whole-file read, which is the property that
   makes "read then edit" work. `fs_edit`'s description and the preamble say so explicitly.
3. **The agent loop refuses to repeat itself.** `_agent_loop` tracks `(name, canonical args) -> result` for
   the turn. A second identical auto-tool call is *not executed*: the recorded result is appended again with
   an explicit "already done — do not repeat this call" instruction, and `tool_repeat_suppressed` is
   audit-logged. Each round also appends a `_DONE_NOTE` to the `arguments` string of its own tool_calls so
   the model's history states what it already did; `_clean_tool_calls()` strips that note wherever stored
   tool_calls are replayed (message graph *and* token estimate), so it cannot accumulate or be counted as
   model output. Guarding execution rather than just prompting is deliberate: a prompt hint can be ignored,
   a file being appended to 6× per turn cannot.
4. **`model.repetition_penalty`** (Admin → Generation, default **1.0 = off**) is forwarded to llama-server
   only when raised above 1.0. It addresses token-level repetition, which is a different failure from #3;
   the help text says so, because an admin who reaches for it expecting a fix for repeated *tool calls*
   would be disappointed (the loop guard is that fix). Kept off by default: penalising tokens degrades
   quality on models that were not looping, and omitting the field at 1.0 means an unconfigured install
   sends exactly the request body it always sent.

Also: after a tool result the pulse continues only while another round can still run (`step < of`), instead
of claiming "thinking…" on a turn that has reached its budget.

Alternatives considered: making `fs_edit` whitespace/line-number tolerant (rejected — silently forgiving
matching hides a wrong quote and can hit the wrong occurrence); hard-capping identical consecutive calls per
turn (rejected in favour of "don't execute, instruct"); enabling repetition_penalty by default (rejected for
quality + compatibility). Reversibility: Medium — part 1 is two tool bodies plus client rendering; part 2 one
line; part 3 the guard and note can be removed independently; part 4 a no-op at its default.

Residual: the repeat guard compares exact arguments, so a model that varies whitespace slightly still
re-executes (correct — that is a different edit); `zip_pack` keeps its button by design; the "stuck thinking"
screenshot's last turn may also have been plain small-model repetition, which only part 3+4 mitigate rather
than solve.


## DEC-046 — LAN exposure is a supported operator choice (`HEARTH_HOST=0.0.0.0`)
Operator request: "make the server be hosted on 0.0.0.0 so I can have my family member try it out locally".

Findings: binding was already env-driven (`HEARTH_HOST`), and `main()` already warned about plain HTTP —
so "host it on the LAN" needed no code at all. What *did* need changing is that the guidance assumed the
operator knew two things they could not be expected to know: how to find the URL, and that
`SameSite=Strict` will make a reachable-but-unusable page out of exactly this setup (cookie set on one
origin, app re-fetched from another → sends fail while the page loads fine).

Decision:
1. **Keep the default at `127.0.0.1`.** Not exposing by default is the correct posture; LAN access stays
   an explicit opt-in. No new switch, no auto-detect — the operator asked for it, so they set it.
2. **`HEARTH_COOKIE_SAMESITE` (`strict|lax|none`, default `strict`)** replaces the hardcoded `"strict"` on
   both cookies. `lax` is the documented option for LAN exposure: it still blocks cross-site subrequests
   and cross-site POSTs, so the double-submit CSRF token remains the actual defence. The value is
   normalised (`normalized_cookie_samesite`) with **strict as the floor** — an unrecognised string must
   never become `none`, because `SameSite=None` without `Secure` is rejected by browsers *and* removes CSRF
   protection outright, i.e. a typo in `.env` would be simultaneously broken and unsafe. Explicit `none` is
   honoured (operator's own choice) rather than silently overridden.
3. **Startup prints the LAN URL** (best-effort local-socket route lookup; no packets leave) and, when
   strict cookies are still in effect on a non-loopback bind, prints the one-line fix. This turns "family
   member can open the page but nothing works" into an explained condition instead of a mystery.
4. Docs: README gets a LAN section next to the Tailscale one (which stays the recommendation *beyond* the
   LAN), SECURITY.md states the accepted risks plainly (unencrypted credentials on the segment, per-IP
   rate limiting concentrated behind one NAT address, login page reachable by anyone on the LAN — so real
   passwords + TOTP, and never "they can't see the port" as a control), OPERATIONS.md gets the runbook with
   the firewall line and the llama-server-stays-on-loopback note.

Alternatives considered: defaulting to `0.0.0.0` (rejected — silently widens exposure on every existing
install); dropping `SameSite` to `lax` unconditionally (rejected — Strict is right on localhost and free
there); a per-origin cookie store (overkill for a family server). Reversibility: High — one env var plus a
print.

Residual: HTTP-on-LAN remains plain HTTP; the honest fix is TLS (Tailscale serve / Caddy), which this does
not do for you. `HEARTH_SECRET_KEY` should be set explicitly once LAN users exist, or every restart logs
everyone out.

## DEC-047 — Ask the server for real token usage (`stream_options.include_usage`)
Symptom (operator report): the header context bar did not update the way llm_coder's does — it sat near
zero and barely moved, while llm_coder's meter tracked the conversation closely.

Root cause: the OpenAI-compat protocol sends `usage` on a **streamed** response only when the client asks
for it with `stream_options: {"include_usage": true}`. Hearth never sent it, so `prompt_tokens` was always
`None` from llama-server. Three consequences stacked up: (1) `_usage_event({})` fell back to a chars/4
guess; (2) on tool rounds the loop emitted an estimate but deliberately never merged it into `turn_usage`,
so nothing real reached `save_turn_usage`; (3) `/api/chat/context-usage` therefore always used its own
chars/4 estimate over **stored message text only** — which excludes the system prompt, the workspace
listing, tool-call JSON and tool results, i.e. most of what a real prompt contains. The bar was not frozen;
it was measuring a small fraction of the truth, so it crept.

Decision:
1. **Send `stream_options.include_usage` by default** (`LlamaServerProvider.stream`). Admin → Model →
   "Ask the server for token usage" turns it off (env fallback `HEARTH_MODEL__INCLUDE_USAGE=0`) for a server
   build that rejects the field; with it off, behaviour is exactly as before.
2. **Read `usage` from any chunk**, not only from a chunk with empty `choices`, and accept llama.cpp's own
   `prompt_eval_count`/`completion_eval_count` names. Builds differ; the old code looked in one place after
   tool-call flushing had already begun.
3. **Measured outranks estimated.** `_merge_usage()` keeps a provider-reported `prompt_tokens` from being
   displaced by a later estimate, and a tool round now keeps measured numbers instead of overwriting them
   with its own guess. New `measured` flag on the provider's usage payload carries that provenance.
4. **Never persist an estimate as if it were measured.** `save_turn_usage()` stores `prompt_tokens` only for
   a measured turn (completion/timing still save), and `context_usage_payload()` reports `source` for the
   *prompt* figure: `"usage"` only when a real prompt size exists, otherwise `"estimate"`/`"none"`. Previously
   any stored dict claimed `"usage"`, which could pin a heuristic to that label permanently.
5. **Client high-water mark** (`chat.js` `S.ctxFloor`, per conversation): context only grows within a chat
   (nothing auto-compacts yet), so a lower reading is always a weaker source. Holding the peak stops the bar
   flickering down mid-turn — which previously read as "it never updates".

Alternatives considered: shipping a tokenizer (rejected — DEC-033's reasoning holds, and Hearth has no
tokenizer for arbitrary chat templates); polling `/props` for slot usage (does not report per-slot prompt
size on most builds); keeping the estimate path and simply inflating it with a bigger constant (would be a
bigger lie, not a smaller one). Reversibility: High — one request field plus flags.

Residual: the *first* turn of a brand-new chat still shows an estimate until the first response completes;
`n_ctx` remains the slot window from `/props`, and with `--parallel>1` + no kv-unified that is n_ctx/parallel
(unchanged, already surfaced in the admin panel as `slot_n_ctx`).

## DEC-048 — Chat page load must never launch llama-server (passive `/healthz` probe)

Reported by the operator: *every time I enter the chat, the system automatically starts
the server if available.* Traced in code, not guessed: `chat.js init()` fetches `/healthz`
for the model badge → `main.healthz()` calls `get_manager().status()`, which resolves the
configured backend — and resolving that backend is what reaches `ensure_server()`, which
launches llama-server when nothing answers yet. So merely viewing a page could load several
gigabytes of weights: GPU occupancy, minutes of disk read, no user intent behind it.

Decision: split the two questions that were conflated.
1. **`ModelManager.probe()`** (new): pure liveness — is something answering the llama-server
   API on the configured port, and what `n_ctx` does it report? It reads `/health` + `/props`
   only, never consults admin settings, never calls `ensure_server()`, never spawns a process.
2. **`/healthz`** now returns `probe()` instead of `status()`. The chat badge shows
   `model: llama-server` when a server answers and `model: not running` (tooltip points at
   Admin → Model) when none does. It no longer claims to know the backend chats use, because
   that is an admin-config fact the badge has no business asserting passively.
3. **Starting stays explicit**: Admin → Model → Start / Restart, or sending a chat message with
   Backend = `llama-server` (`get_provider()` unchanged — that is real intent). No new toggle:
   the requested default (never launch on page view) is what the code now does unconditionally,
   and an "auto-start on page load" switch would only re-offer the behaviour being removed.

Also verified while fixing: `status()` itself never launched anything, so Admin → Model still
reports truthfully; the only launch paths are `get_provider()` (chat turn) and `start()`.
`model.server_bin` help text updated — it used to promise "Hearth launches this itself when
it can't reach an already-running server", which is exactly the behaviour being narrowed.

Regression test: `test_healthz_never_launches_llama_server` patches `ensure_server` to record
calls and asserts neither `probe()` nor `status()` touches it, and that `probe()` exposes no
provider-choice field. Alternatives considered: a client-side flag to skip the badge request
(rejected — the server would still launch for any other caller of `/healthz`); making
`ensure_server()` lazy via a background task (rejected — still launches without user intent).
Reversibility: High — restore `status()` in `healthz()` and the old behaviour returns.

## DEC-049 — Linked project folder: copy-in + baseline manifest + gated apply-back (design)

**Question:** Can a user point their workspace at a real folder on disk — a project of theirs — and
how do agent changes get back to that folder without losing the isolation guarantee (DEC-005) or the
user's uncommitted work?

**Scope correction (operator, explicit):** this is a **generic project-collaboration feature**. It is
deliberately *content-agnostic*: Hearth does not know or care what kind of files the linked folder
holds. Self-modification of Hearth's own source is **not** the design's primary use case and must not
be presented as one, and there is **no guard-path deny-list** — see "Rejected: guard-path list" below.

**Status: DESIGN ONLY — nothing implemented.** No code in `hearth/` was changed for this decision;
the ledger below is what was verified by reading. This entry records scope and the reasoning that
narrows the options, so implementation starts from a checked picture rather than a guess.

### Verified starting conditions (read, not assumed)

| # | Finding | Evidence |
|---|---|---|
| 1 | The jail has exactly **one root chokepoint**: `root_for(user_id)` → `paths.WORKSPACES_DIR/<uid>`; *every* fs operation resolves through `resolve_in_jail()`. An alternate root is therefore a change in one function, not a rewrite of the tools. | `workspaces.py:41-44`, callers listed at `workspaces.py:53,72,84`, `tools/__init__.py:36` |
| 2 | All 10 fs tools take **workspace-relative** paths and only ever pass `ctx.user_id`. They are already root-agnostic — a linked folder can be invisible to the model. | `tools/fs_tools.py` (all `run()` bodies) |
| 3 | Per-call, reload-durable approval already exists: gated calls become a `PendingAction` row (`kind/tool_name/call_id/payload/status`), stream stops with `action_required`, `/api/chat/resolve` resumes. New gated tools inherit it. | `orchestrator.py:527-536`, `_persist_pending()` at 681, `models.PendingAction:87` |
| 4 | Fail-closed cross-platform degradation is already house style: `bwrap → docker → stub(refuse)` with an actionable refusal string. The "unsupported OS ⇒ denial" requirement has a working template. | `sandbox.py:detect_backend()`, `run_command()` stub branch |
| 5 | An **unshipped helper protocol** already exists (`GET {helper_url}/folder`, `/screenshot`, gated by `device.helper_url` + per-conversation grant, polite refusal when unpaired). Precedent for a future on-device bridge. | `tools/device_tools.py:1-49` |
| 6 | **Encryption at rest is a landmine:** `_write_blob()` transparently encrypts when workspace encryption is on (`HEENC1` framing). A naive mirror would write ciphertext into the user's real project. | `workspaces.py:_read_blob/_write_blob/seal_bytes`; `.env.example:38-40` |
| 7 | No OS dispatch exists anywhere except one module; nothing Android-aware at all. Cross-OS backends are new ground, with `platform.system()` branching + "no path found" fallback as the local precedent. | `tools/browser_connector.py:90,150,246`; no `os.name`/`sys.platform` elsewhere in `hearth/` |
| 8 | This feature is **not** planned anywhere in the repo — absent from CONTRACT REQ-001..040, DECISIONS DEC-001..048, DELIVERY_RECORD "Deferred & rejected work", CONTINUATION next-steps/backlog. Genuinely new scope. | greps across `docs/` |
| 9 | Absolute paths are refused outright today (`JailEscape("absolute paths are not allowed")`), so no tool can name a path outside the jail in any form. The feature must **add** a resolution layer, not loosen an existing one. | `workspaces.py:56-58` |

### Options considered

- **(0) Symlink the jail root.** Verified mechanically by replaying the jail's own logic
  (`root_for()` calls `.resolve()`, so containment is checked against the *resolved* target):
  pointing `data/workspaces/<uid>` at a real project makes every tool operate on it immediately,
  zero code. Rejected as a product: it silently deletes isolation (`fs_delete` runs at approval
  `auto`), breaks encryption-at-rest, and is admin-privilege-only on Windows / meaningless on
  Android. Kept as a documented **operator experiment only**, not a supported path.
- **(A) Separate helper/bridge process** on the user's machine (extends finding 5). The only
  option that works when Hearth is *remote* from the files, and the only sane place to absorb
  Android's scoped-storage rules. Deferred: needs auth, packaging, a transfer protocol.
- **(B) Copy-in + baseline manifest + explicit apply-back.** **CHOSEN.** Import a project into
  the jail recording a per-file baseline (hash+mtime); the model works entirely inside the jail
  with today's tools and zero new risk; one new gated tool propagates an approved diff.
- **(C) Real-time mirror / shadow mode** (every write propagates instantly). Rejected: no review
  point, no atomic revert, and a model loop becomes disk churn — worst possible guarding.
- **(D) Git-native linked repo** (branch/PR review). Excellent affordance, but adds a git
  dependency against the minimal-dependency policy (DEC-003) and fails on non-repo folders.
  Kept as an optional *complement* under B, not a foundation.

### Decision

Adopt **(B)**, with these properties:

1. **One active link per user**, admin-visible, stored in a new `project_links` table (not in
   boot env): `target_abs`, `mode`, `rel_prefix`, `enabled`. The jail resolves the active link and
   *virtualizes* it under a fixed prefix (e.g. `project/…`) so relative-path tools,
   artifact bookkeeping (`artifacts.record()` keys on `(user, rel_path)`), and prompt-injected
   listings all keep working unmodified.
2. **Baseline manifest, not live links.** Import snapshots each file's hash + size + mtime into a
   manifest inside the jail. "Link per file" is deliberately dropped: an unchanged file *is*
   equivalent to a link (byte-identical to baseline), while per-file symlinks are rejected by the
   current jail, need privileges on Windows, and do not exist in Android-safe storage.
3. **Apply is a separate, gated tool.** `sync_preview` (read-only, returns diffstat +
   conflict list) and `sync_apply` (`gated = True`, approval `ask`). Apply refuses unless every
   target's current on-disk hash still equals the baseline; any user-side edit is a
   **conflict → refuse**, never a silent merge. Pre-apply backup copies make revert one action (the
   pattern already proven in `llm_coder/core/snapshots.py`). The protection model is
   *user-intent* based — approve each apply, detect concurrent edits, allow revert — not
   *path-content* based.
4. **Fail-closed per OS**, in `sandbox.py` style: one `apply_backend` interface auto-detected as
   Linux → Windows → (Android: refuse, or delegate to a future helper) → `stub(refuse)` with an
   actionable message. Refusal when encryption-at-rest is enabled is explicit, not incidental.
5. **Nothing about the model's tool surface changes** except two new tools. No existing tool gains
   knowledge of absolute paths; `resolve_in_jail()` keeps its containment contract.

### Rejected: guard-path list (do not re-add)

An earlier draft of this decision proposed refusing agent-applied changes to Hearth's own
authz/jail/approval/settings files. The operator rejected it: the feature is generic project tooling,
and special-casing "internal" files would be extra machinery that contradicts the agnostic design (it
would also make the tool behave differently depending on whether a user happens to keep a Hearth
checkout in their linked folder). **If Hearth's own source is ever linked, it is treated exactly like
any other project folder** — protection comes from per-apply approval, conflict detection, backups and
revert, which apply uniformly. Future sessions: do not reintroduce path-based guarding here; the
generic safeguards above are the intended answer.

### Open risks carried into implementation (not resolvable by reading)

- Whether a symlinked jail root works end-to-end at *runtime* (finding 0 was proven by replaying
  the resolution logic in isolation, not by booting Hearth). Settle with `python scripts/selftest.py`
  plus one live turn before building anything on top.
- Deployment topology: B assumes Hearth and the target folder share a filesystem. README's
  Tailscale/LAN guidance implies family devices may be remote — then only (A) applies. Operator
  must confirm; it decides whether A becomes mandatory later.
- Whole-project import vs context limits: `_workspace_context()` lists ≤80 entries and `fs_search`
  caps at 400 files / 60 matches. A real repo will blow past both, so the link needs a scoped
  `rel_prefix` (subtree) rather than an entire tree. Untested against a real model's behaviour.

**Reversibility:** High. New table + two new tools + one resolution branch; disabling the link and
deleting the rows returns exactly today's behaviour, since no existing tool is modified.

---

## DEC-052 — Media tools deliver images via a follow-up *user-role* multimodal message

**Date:** session 4 (media tools). **Status:** implemented, runtime-unverified against a real server.

### Context

The operator asked for three capabilities: read PDFs through the mmproj image projection so visual
elements are not lost, view video by sampling frames (8 equally spaced, or model-chosen timestamps,
with runtime reported), and a meta tool reporting context usage in percent and absolute values.

Two of those need to put **pixels in front of the model**, and hearth had exactly one such path:
`orchestrator._attachment_content()`, which turns *user* message attachments into OpenAI
`image_url` data-URL parts. Tool results were plain strings end-to-end — `ToolResult.content` →
`Message(role="tool", content=str)` → replayed as `{"role":"tool","content":str}` — and
`ToolResult.meta` is deliberately never sent to the model (only UI source-links ride it).

### Options considered

- **(A) multimodal content list on the tool message** (`[{type:text},{type:image_url}]`). Faithful
  to OpenAI's spec, but hearth has no evidence its chat templates accept a *list* as `tool` content;
  llama.cpp template support for non-string tool content is uneven across builds, and a rejection is a
  hard HTTP 400 that kills the whole turn — i.e. the failure mode is total, not graceful.
- **(B) text stays in the tool message; images follow as a synthetic `user` multimodal message.**
  Reuses the one shape proven to reach an mmproj projector (the upload path). Costs a small role
  impurity: the model sees a user turn it did not get from the user.
- **(C) A behind an admin setting, default B.** More knobs, and an untested default-either way.

### Decision

**(B).** `ToolResult` gains an optional `images: list[{path, mime}]`. The orchestrator persists those
as **paths** on the tool row's meta (`_tool_row_meta`) — never base64 in SQLite — and
`build_messages()` injects a user-role multimodal message immediately after that tool message on every
replay. The injected message is **transient**: it is not stored as its own `Message` row, so the
transcript still shows one tool chip, `/api/chat/context-usage` char sums are unaffected, and the DB
never accumulates synthetic user messages. `_image_parts()` is shared with `_attachment_content()` so
uploads and tool output cannot drift apart in shape or in their 8 MiB per-image cap.

**Rationale:** shipping the proven wire shape beats shipping the theoretically correct one that may
400 on the operator's actual build; the impurity is invisible to the user and explainable to the model
(the text part says which tool produced the images). **Reversibility:** high — dropping `images` from
meta and deleting the injection branch returns today's behaviour exactly; no existing tool sets it.

### Related choices made here

- **Rendering is byte-based, never path-based** (`pdf_info_bytes`, `render_pdf_bytes`). The workspace
  can be encrypted at rest, and pdftoppm/gs/mutool/pdfinfo are *separate processes* with no key —
  handing them a jail path would feed them ciphertext. Callers read via
  `workspaces.read_bytes()` (decrypts) and the renderer stages a temp file itself.
- **Backends degrade, and say what is missing**: pymupdf → pdftoppm → gs → mutool for pages;
  ffprobe → OpenCV for video metadata; ffmpeg PNG pipe → OpenCV seek for frames. `requirements.txt`
  documents them as optional because none are assumed present.
- **Media tools use `ModelManager.probe()`, not `get_provider()`**, to report vision availability —
  per DEC-048 a read-only question must never launch llama-server as a side effect.
- **Context tool reuses `routes_chat.context_usage_payload()`** rather than reimplementing the maths,
  so the model can never be told something that contradicts the user's context bar (measured-vs-estimate
  `source` is preserved).
- **Hard caps**: 6 pages per `pdf_read` call, 16 frames per `view_video`, images downscaled to
  `media.max_image_edge` (default 1280 px) — a 10-page colour PDF at render resolution would otherwise
  dominate the window the context tool exists to monitor.
- **Greyscale removed** (operator request, supersedes the greyscale half of the bullet above): pages and
  frames are now sent in colour by default. The original rationale was byte savings for documents, but
  a frame's cost is vision tokens, which scale with pixel *dimensions* — identical grey or colour — so
  greyscaling bought no prefill or context win while destroying colour information outright. The
  `grayscale` parameter on `optimise_png()` remains for anyone who wants bandwidth over fidelity.

### Open risks (not resolvable by reading)

- Whether an mmproj-equipped llama-server *attends* to images attached to a user message that follows
  a tool result, mid-conversation, as well as it does to upload-time images. Verified only up to the
  message graph (`build_messages` output inspected: `system, user, assistant(tool_calls), tool,
  user[multimodal]`) and that the stub never leaks base64. Needs one live turn with a real vision model.
- Token cost per image is model-dependent; `_estimate_prompt_tokens()` counts an image part as a fixed
  64 tokens, so the context bar under-reads on media-heavy turns unless the server reports usage.
- Whether poppler/gs render the operator's *specific* PDFs (encrypted, form-heavy, CJK) — tested here
  against generated PDFs and a hand-written minimal one; a real-world corpus was not available.


---

## DEC-056 — Audio is described by a *second, independent* model, not by the chat model

**Date:** session 5 (audio captioner). **Status:** implemented, runtime-unverified against a real Omni model.

### Context

The operator supplied a working standalone harness (`qwen_omni_captioner_tk.py`) that drives a
Qwen-Omni GGUF through llama.cpp and gets back a description of *what it hears* — timbre, voices,
emotion, music, ambience — not merely a transcript. They wanted that inside Hearth: audio files
routed to such a model, the description handed to the main model, CPU offload available (their
model is MoE), and the admin UI showing every loaded model rather than one.

Until now audio was dead weight. `video_info` could only report "audio tracks: 1 (audio is NOT
transcribed)", and `_attachment_content()` skipped anything that was not `image/*`, so an uploaded
recording reached the model as a filename.

### Options considered

- **(A) Make the chat model audio-capable** — require an Omni GGUF as the *main* model and send
  `input_audio` parts from `_attachment_content()`. One model, no orchestration. Rejected: it
  forces every family conversation onto a slow MoE model to support an occasional voice note, and
  throws away the chat model's own tool-calling quality.
- **(B) A second llama-server with its own port, log, settings namespace and lifecycle.** Chat
  keeps whatever model suits it; audio is described by a specialist that can be started, restarted
  or stopped independently. Costs a second process and a second block of admin settings.
- **(C) In-process transformers (whisper/BLIP-style).** Rejected: pulls torch into a llama.cpp
  product, duplicates work llama.cpp already does, and cannot run the operator's GGUF at all.

**Decision: (B)**, mirroring how `manager.py` treats the chat model but deliberately *not* sharing
that class — two lifecycles that must not interfere are easier to reason about as two objects than
as one with a `which_model` parameter.

### The interface between the two models is plain text

A caption is inserted into the user message as text, never as an audio part:

- the chat model usually has no audio encoder, and a base64 blob it cannot read is pure context waste;
- text survives history replay, `/api/chat/search`, summarisation and export for free, whereas a
  multimodal part does not;
- it makes the second model swappable (Omni today, something better tomorrow) without touching the
  chat path.

`_attachment_content()` appends captions to the *text* of the message rather than adding parts, so
the common audio-only case still returns a plain string — the shape DEC-052 established as the safe one.

### Where the wait happens (the part that could have ruined the feature)

Captioning is not free: ffmpeg conversion plus a model that must listen takes seconds to minutes.
Doing it synchronously before the response began would have looked exactly like a hang — precisely
the complaint `user.txt` already logs about invisible tool work. So:

- `orchestrator.prepare_captions()` runs **inside** the SSE stream, in a worker thread, and emits
  `notice{kind: caption_start|caption_done}` events that chat.js renders as an activity line;
- the abort handle is registered around it and checked on every drain tick, so Stop works during
  captioning instead of appearing to ignore the user for the length of a recording;
- results are cached per **(user, conversation, path)** in memory. `build_messages()` runs once per
  agent round; re-listening on each round would cost minutes per turn for a description that cannot
  change. Keyed by user because one family member's recording must never be quoted into another's.

### CPU offload is the default, not an option added afterwards

`captioner.n_gpu_layers` defaults to **0** (CPU only) and `captioner.load_mode` to `auto` (mmap).
For a 30 B-active-3 B MoE this is the correct first configuration: partial VRAM residency thrashes,
and the chat model needs the GPU. `'auto'` maps to *omitting* the flag exactly as
`manager.build_argv()` does — passing the literal word parses as `-2` in llama.cpp and silently
disables GPU tensor overrides (the 2 t/s trap already documented there).

### Two backends, same reason as the operator's harness

`server` keeps weights loaded between captions; `cli` (`llama-mtmd-cli --audio`) reloads per call but
needs no port and frees memory when idle. `server` falls back to `cli` mid-caption with a note if its
server becomes unreachable, because half a description beats none.

### Error handling is the feature

Every failure path names the thing to change, because an admin reads these while debugging:

- nothing configured → "the audio captioner is disabled (Admin → Audio)", and the model is told it
  was *not* analysed so it cannot invent what a recording said;
- missing binary/model/mmproj → checked by `preflight()` **before** ffmpeg runs, so a configuration
  fault never surfaces as "could not decode this audio file";
- wedged port occupant → detected via `/props` (not just `/health`) and reported with the `ss -tlnp`
  command, same discipline as `manager.ensure_server`;
- failed launch → exit code plus the tail of `runtime/captioner.log` embedded in the hint;
- server rejecting the audio part → attributed to a missing/mismatched audio encoder or projector,
  not blamed on the user's file;
- every caption handed over carries an explicit "this is not your own perception and can be wrong
  about words, counts or identities" caveat — the lesson `llm_coder/core/caption_fallback.py` learned.

### Deliberate limits

- **64 MiB per attachment** (`captioner.max_bytes`) — a two-hour recording would stall a turn for many
  minutes and produce a description too long to be useful. Refused with the limit named.
- **Chunk floor 5 s, ceiling 900 s**, one clamp function feeding all three uses (ffmpeg
  `segment_time`, per-chunk offsets, header text) so they cannot disagree.
- **Serialised by a lock**: two people captioning at once on one `--parallel 1` slot is slower than
  the two in series.
- **Video containers are accepted** (`.mp4/.webm`) because that is where voice recordings often live;
  `audio_info` reports when there is no audio track so the model says so instead of describing silence.

### Verification actually performed

24 unit tests (`CaptionerTests`) + 5 HTTP tests (`CaptionerHttpTests`). Beyond the suite, against the
operator's real `llama-server` binary and real ffmpeg: argv built correctly with `-ngl 0`, loopback
and `--parallel 1`; a live fake llama-server confirmed the wire shape (`input_audio` + text parts,
base64 payload, `temperature/top_p/max_tokens/top_k` sent, `seed` omitted at −1); 12–13 s of real
sine-wave audio split into 3 chunks labelled `[0:00 - 0:05]`, `[0:05 - 0:10]`, `[0:10 - 0:15]`; a bad
model file failed in 0.6 s reporting llama.cpp's own `invalid magic characters` from the log; and a
wedged occupant on a port was reported rather than bind-fought.

### Open risks (not resolvable without an Omni model)

- **Unverified against real Qwen-Omni weights.** The request shape follows the operator's working
  harness, but whether their build wants `--mmproj`, `--audio` semantics in mtmd-cli, or extra flags
  for the audio encoder is not checkable here. Admin → Audio → **Test with a tone** exists to find out
  in one click: it captions a generated 440 Hz sine, and a description claiming speech over a beep is
  direct evidence of a mis-wired model or a prompt that needs tightening.
- Caption quality/prompt fit for the operator's actual use case (transcription vs description) — the
  default prompt asks for description; they will likely retune it.
- The caption cache is in-process: a restart re-listens once per conversation. Fine at family scale;
  a durable cache would belong in the artifacts table if it ever hurts.


## DEC-057 — Stream the captioner's response, and make Stop reach the audio model

Two operator complaints about the second model (DEC-056): the wait to describe an attached
recording still felt dead, and pressing **Stop** did not actually stop the captioner — it kept
running after the turn ended. Both are fixed here; neither changes what the chat model receives.

### Streaming the caption like reasoning

`_run_server` now asks llama-server for a streamed completion (`stream: true`) and forwards each
`delta.content` (and `delta.reasoning_content`, for a captioner with thinking on) as it arrives.
The pieces ride to the client as `notice{kind: caption_chunk}` events, throttled server-side to
about one per 48 chars or 0.3 s so a long caption does not spam SSE, with a guaranteed flush of any
tail before `caption_done`. `chat.js` renders them into one collapsible block (like the reasoning
block) that grows live and collapses when the description finishes — the answer bubble carries the
real result, so a permanently-open transcript block would push the reply off screen.

- **Opt-out:** `captioner.stream` (bool, default true). False restores the old single non-streamed
  read (`_run_server_once`). A server that ignores `stream:true` and returns nothing usable on the
  stream is retried once without streaming, so those builds still produce a caption.
- **Timeout units matter:** httpx's scalar timeout is a *total* including body read, which would
  kill a long streamed caption regardless of progress — the same ReadError trap documented for the
  chat provider. The streaming path uses `httpx.Timeout(connect=…, read=None)`: cap connect only,
  leave the streamed read unbounded (Stop and the model's own `max_tokens` end it).
- A mid-stream transport failure falls back to the CLI backend **only if nothing streamed yet**, so
  a partial caption is never doubled.

### Stop now reaches the audio model

The gap was two-fold: cancelling the asyncio task does not unblock a worker thread sitting in a
blocking httpx read, and `caption_sync` holds a lock so nothing could touch the process until the
lock was acquired — after `_run_server` had already returned. Fixes:

- **Cooperative mid-stream cancel:** `_run_server` checks a `.is_set()` cancel object on every SSE
  line; `prepare_captions` and `caption_sync` check it between files/chunks. A Stop abandons the
  recording instead of listening to the end of it. An aborted stream surfaces as `CaptionError`
  and is **not** retried via the CLI fallback (that would undo the abort).
- **Kill the process too:** interrupting the HTTP read does not stop llama.cpp from decoding the
  rest of its slot, so `stop_active()` terminates the Hearth-owned child process. It records a
  kill *intent* (`_stop_requested`) even when no process is registered yet — covering the case where
  Stop lands while `caption_sync` is still blocked on the lock — and `_set_active` honours it the
  instant a process appears. `clear_stop()` at the top of each caption keeps one user's abort from
  tearing down the next request. Only ever touches a Hearth-owned child, never an external
  `HEARTH_CAPTIONER_BASE_URL` server. The route calls it in the caption-drain abort branch.
- **The `describe_audio` tool** had no cancellation at all (and is never re-run after an abort, due
  to the repeat-suppress cache), so Stop simply ignored it. `ToolContext` now carries the turn's
  CancelHandle (`attach_cancel`/`cancel_event`, a `.is_set()` adapter) and the tool passes it down.

### Verification actually performed

8 new tests (`CaptionerStreamTests`) on top of the existing 24 + HTTP: deltas emitted in order
(content **and** reasoning_content), `stream:true` sent when enabled and `false` when disabled, a
cancel flag flipping mid-stream raises rather than finishing, `prepare_captions` forwards chunk
notices and skips remaining files once cancelled, and the `ToolContext` cancel adapter. Full suite
184 tests green (core + `HEARTH_RUN_HTTP=1`). The kill-path and real streaming are wired to a fake
llama-server here; against real Omni weights they remain operator-verifiable via Admin → Audio.

## DEC-058 — One cancel predicate; the two cancel interfaces must both work everywhere

**Status:** accepted (bug fix). **Date:** follows DEC-057.

**Symptom.** Attaching an `.mp3` directly always failed with
`AttributeError: 'CancelHandle' object has no attribute 'is_set'`; asking the model to run
`describe_audio` on the same file always worked. So the feature looked broken for the common
path and healthy for the uncommon one, which made it look like an audio problem rather than a
wiring one.

**Cause.** Two cancellation interfaces grew up side by side:

| interface | who exposes it | who polls it |
| --- | --- | --- |
| `.cancelled` (property) | `cancel.CancelHandle`, asyncio-based | the orchestrator's own safe points |
| `.is_set()` (method) | `threading.Event`, and `tools._CancelAdapter` | `captioner.caption_sync` between chunks, `_run_server` per SSE line |

The `describe_audio` tool passes `ToolContext.cancel_event()` — the adapter, which speaks
`.is_set()`. The attachment path (`routes_chat.gen_sse` → `orchestrator.prepare_captions` →
`captioner.describe_file`) passed the **raw** `CancelHandle`, which had only `.cancelled`. Its
first `cancel.is_set()` raised on chunk 1 — before any audio was listened to, hence "always" and
"never with the tool". The DEC-057 note "*a `.is_set()` adapter*" documented one side of the seam
and hid the other.

**Decision.** Stop assuming a single shape at every boundary that can receive either:

- `captioner._cancel_requested(cancel)` is now the one predicate used at both captioner check
  sites: prefer `.is_set()`, fall back to `.cancelled`, never raise (an unknown object reads as
  "cannot be aborted"). `captioner` still does not import `hearth.cancel`, keeping the module
  independence required by its docstring.
- `orchestrator._cancel_requested(cancel)` does the same for the batch loop in
  `prepare_captions`. The orchestrator's *own* safe points keep using `cancel.cancelled`: those
  are typed `CancelHandle | None` and never see another shape.
- `CancelHandle.is_set()` now exists as a documented threading-compatible alias, so anything
  handed the raw handle directly works too. Belt and braces on purpose: the next caller that
  passes a handle straight through — the most likely mistake, since it is the object at hand —
  cannot reintroduce this class of bug.

**Rule for future edits.** Anything that blocks long enough to be worth aborting must accept
both shapes; anything typed as `CancelHandle` may use `.cancelled`. When in doubt call a
normaliser rather than poking an attribute.

**Verification actually performed.** 3 new tests in `CaptionerStreamTests`: `CancelHandle`
satisfies both interfaces; `_cancel_requested` is correct for `None`, an unset handle/event, an
unknown object, a cancelled handle/event and a `.cancelled`-only duck type; and `caption_sync`
run end-to-end with a raw `CancelHandle` produces a caption when uncancelled and a
"captioning was stopped" `CaptionError` when cancelled (previously AttributeError). Full suite
**187 tests green**, core and `HEARTH_RUN_HTTP=1`. Not re-run against real Omni weights — the
fake-server harness covers the wiring; the operator path remains Admin → Audio.


## DEC-059 — Close the DEC-048 gap in `/api/chat/context-usage`; and never trust `.pyc` when source and behaviour disagree

**Symptom (operator):** after unloading the model in Admin → Model, entering the Chat UI from
Admin loaded it again. DEC-048 had already made passive page views launch-free, so either that
invariant was wrong or something bypassed it.

**Cause.** `GET /api/chat/context-usage` resolved the provider to read the live context window:

```python
prov = await get_manager().get_provider()   # -> ensure_server() -> launches llama-server
h = await prov.health()
```

The endpoint is not passive-only in intent — it is called from `chat.js loadContextUsage()`, which
runs on **every conversation open** (line 347), and `init()` auto-opens the first conversation. So
Admin → "← Chat" restarted a model the admin had just stopped. DEC-048's own wording recorded the
invariant as *"the only launch paths are `get_provider()` (chat turn) and `start()`"* — but
`get_provider()` has four callers, and this one is a read, not a turn. The DEC-048 regression test
covers `probe()`/`status()` only, and the payload tests cover pure maths only, so **nothing
exercised this route** and the suite stayed green.

**Decision.** `chat_context_usage` uses `ModelManager.probe()`, guarded on `server_reachable`:

```python
h = await get_manager().probe()
if h.get("server_reachable"):
    provider_n_ctx = h.get("n_ctx") or None
```

Behaviour is unchanged when a server *is* up (live `/props` still beats the configured setting —
the reason the call exists) and falls back to `model.n_ctx` when it is not, which is what the route
already did whenever the provider call failed. This matches the pattern already used by
`context_tool.py` and `media_tools.py`. `chat_stream`/`chat_retry`/`chat_resolve` keep
`get_provider()`: sending a message is real intent (DEC-048 unchanged). Making Stop *strictly*
sticky for retry/resolve was considered and **not** taken — it would break the documented
"send a message with Backend=llama-server" start path; raise it separately if wanted.

**Second finding, and why this took far longer than it should have.** Verification kept reporting a
launch *after* the fix, with a stack trace showing `probe()` at the fixed line landing inside
`get_provider`. That is impossible for the source on disk: the route's **bytecode** still listed
`get_provider`/`health` in `co_names` while line 798 read `.probe()`. Stale `__pycache__` was
shadowing the source, so every earlier "still launches" result — including the apparent end-to-end
confirmation — measured a stale build. `rm -rf hearth/__pycache__ hearth/*/__pycache__` resolved it;
freshly compiled bytecode calls `probe` and no launch occurs.

Lesson: when observed behaviour contradicts the source you are reading, check
`fn.__code__.co_names` against the file before believing either. A stack trace's displayed source
line is re-read from disk at print time, so it can label a frame with text that was never executed —
which is how "probe() calls get_provider" got manufactured.

**Regression test.** `test_context_usage_never_launches_llama_server` drives the route directly and
traps `ensure_server`, makes `get_provider` raise, and forces `probe()`'s probes to fail so reaching
any launch-capable path fails loudly instead of probing whatever llama-server happens to be up. Two
phases: nothing answering (must not launch, must still report the configured window) and a server
answering (live `/props` value must win). It passes a **real `conversation_id`** — the route returns
before touching the manager when it is empty, and an empty-id test passes against the buggy code too.
Verified to fail on the pre-fix code (`[('get_provider', (), {})] != []`) and pass after.

**Also noted, not fixed (out of scope).** `MediaToolTests.test_context_usage_tool_reports_absolute_
and_percent` asserts `meta["n_ctx"] == 10_000` but `context_tool` prefers the *live* server's window,
so it fails whenever a llama-server answers on the configured port. Pre-existing and independent of
this change (reproduced with the fix reverted); it needs its reachability stubbed to be hermetic.

Reversibility: High — restoring `get_provider()` in that one block returns the old behaviour.


## DEC-061 — Stream the captioner on the tool path too, and make captions durable

Three operator complaints about the second model, all traceable to two missing wires:
`describe_audio` produced no live output; its result looked truncated; and a description
vanished on refresh — with no way to tell whether it was still in context at all.

### The streaming gap was one argument, plus a drain seam that did not exist

`captioner._run_server()` already streams (`stream: true`) and forwards every delta to
`on_delta` (DEC-057). The attachment path passed `on_delta`; **the tool never did** — so the
captioner streamed into a local buffer and the browser saw nothing until the last chunk
finished. Fixing that exposed a second problem: inside `_agent_loop` tools are *awaited*, not
iterated, so a tool has no way to yield into the generator. `_emit_notice` queued into
`_pending_notices`, which was drained **only** in the steering branch — meaning any progress
queued from elsewhere in the loop was silently dropped (a latent bug on its own).

- `ToolContext.report_progress(**ev)` is now the tool-facing sink; `_run_tool_streaming()`
  owns the queue and drains it while the tool runs, then yields the `ToolResult` as its final
  item (an async generator cannot `return` a value). Same task/queue shape routes_chat already
  uses around `prepare_captions`.
- Applied on **both** execution paths — auto tools *and* `_resume_pending`. An approval-gated
  `describe_audio` takes exactly as long as an automatic one, so streaming only the auto path
  would make live output depend on an admin's approval toggle.
- The 48-char / 0.3 s throttle moved out of `prepare_captions` into `base.DeltaThrottle`, shared
  by both callers — two copies would drift exactly the way the two cancel interfaces did (DEC-058).
- `chat.js` keys caption blocks **per file** (`state.captions[ev.file]`). With two sources of
  these events in one turn, a single shared element would interleave two recordings' text.

### The cutoff was three different things; only one was intended

| limit | what it did |
|---|---|
| `chat.js` chip `max-h-32` | ~5 visible lines on text the user needed — the literal "cut off despite the scrollbar" |
| `"preview": content[:800]` ×4 sites | the live SSE frame carried 800 characters, full stop |
| `agent.tool_output_max_chars` (24 000) | clips what the **model** sees; legitimate, audited, left alone |

- Preview is now `base.ui_preview()` → 4 000 chars, bounded so one careless call still cannot put
  a megabyte in an SSE frame. Chip body grows to `max-h-[70vh]` and only scrolls past that.
- **This does not change what the model receives.** The display budget and the guard budget are
  deliberately separate knobs; conflating them would have either hidden text or inflated prompts.

### Persistence: a caption lived in a dict, and the "cache" claim was false

Captions existed only in `_captions` — in-process, capped at 64 conversations, truncated to 5200
chars. Two consequences worth stating plainly:

1. A refresh showed **nothing**. `/api/chat/conversations/{id}` returns `Message` rows, and a
   caption had no representation in any row.
2. `_agent_loop`'s docstring said *"later rounds read them from the module cache"* — but
   `build_messages()` never called `get_caption()`. It only used the turn's dict, so **any later
   message in the same chat sent the model a bare `[attached: x.mp3]`**, and a restart lost even
   the current one. Confirmed against committed HEAD before changing anything (control run below).

Fix follows how `meta.reasoning` already survives: store on the user message row, no schema change.

- `persist_captions()` writes `meta.captions = {rel: {text, name, ok}}` onto the row that owns
  the attachments (matched by `artifact_id`, so an older same-named file is not overwritten).
- `build_messages()` merges each row's stored captions into the map, so descriptions replay on
  every later turn and after restarts. `meta` is never sent to the model — only the folded-in
  text is — so persistence cannot inflate a prompt by itself.
- The history endpoint adds a per-row `captions` summary (`name`, `ok`) so the client can render
  the block and distinguish *no description* from *description failed*; `chat.js` restores it via
  `addCaptionBlockStatic()`, collapsed by default.
- `_captioner_hint()` now tells the model what to do if a description is genuinely absent, rather
  than asserting one always arrives — the old wording invited confident invention.

### Verification actually performed

Full suite green: **196 tests, 0 failures** (`HEARTH_RUN_HTTP=1`), including 8 new ones in
`CaptionStreamAndPersistenceTests`. Beyond the suite, against the real modules:

- Drain-seam semantics proven independently: progress delivered *while* the tool runs, result as
  final item, exception propagation intact, quiet tools unaffected.
- `describe_audio` end-to-end: 53 deltas → 8 throttled `caption_chunk` events + start/done, and
  the concatenated stream is **byte-identical** to the full caption (no dropped tail). Failure
  path emits `caption_done{ok:false}`; a tool with no sink attached does not crash.
- Approval-gated resume verified separately: 6 chunks streamed, result persisted once.
- Persistence against **real SQLite**: round-trip, idempotency, failure notes stored `ok:false`,
  unrelated rows untouched, and `build_messages(captions=None)` replaying the description.
- Reload over HTTP with a real upload + stream: history exposes
  `{uploads/voice.mp3: {name, ok:true}}` and the text in meta.
- Preview bounds across sizes: 9 → 9, 2160 → 2160, 5400 → 4000, 200 000 → 4000 (old code: 800).
- **Control run**: committed HEAD sends `['[attached: meeting.mp3]']` to the model on a later
  turn; with this change the same graph contains the description. That is the regression pinned.
- `chat.js` passes `node --check`; no stale `captionEl`/`captionText` references remain.

Note for whoever runs this next: the venv was missing `sqlalchemy`/`fastapi`/`pydantic`, so 142
of 188 tests were erroring on import rather than failing logic. They are installed now and the
suite is genuinely green instead of mostly-skipped.

### Deliberate limits

- **No new setting.** The preview budget is a constant; an admin knob nobody asked for would be
  another thing to mis-tune. Change `UI_PREVIEW_MAX_CHARS` if a real case needs more.
- **Captions are not re-generated on read.** Stored text is replayed verbatim, including one
  produced under an older prompt — cheap, and honest about being that earlier model's answer.
- **The in-memory cache stays** as the fast path for repeat rounds; durability now comes from the
  row, so eviction and restart are no longer loss events.
- Item 3 of the operator's list (an explicit "kept in context?" badge) is only partly served: the
  UI can say *described* vs *failed*, but there is still no surfaced answer to "is this caption
  currently inside the model's context window". That needs prompt-graph accounting, not a field.


## DEC-064 — `view_video` can hear the section it is looking at

The question was whether audio analysis should follow video viewing automatically (the captioner
exists, and video usually has sound) or stay an agentic choice. The interesting part turned out not
to be *when* to couple them but **how much** audio one call may buy — and the existing code made
that coupling accidentally lopsided.

### What was actually happening

`.mp4`/`.webm` are in `captioner.AUDIO_EXTS`, so `route_candidate()` already auto-captioned an
attached video — its **entire** audio track, chunk by chunk, before the first token — while its
pictures were ignored unless the model separately called `view_video`. Meanwhile `video_info` still
printed `(audio is NOT transcribed)`, written before DEC-056 and false ever since: a model that
believes it tells the user there is no way to hear the video.

The cost asymmetry decides the design. Frames are one ffmpeg pass for ~8 images. Audio is one
*model request per chunk* (`captioner.chunk_seconds`, default 30 s), so ten minutes of clip is twenty
captioner calls — and `captioner.max_bytes` (64 MiB) rejected most real videos before any of it.

### Options considered

- **(A) Auto-caption video audio whenever the captioner is available.** Rejected: pays the expensive
  path on every attachment including silent clips and ones where only the picture was asked about,
  scales with clip length rather than interest, and hits the size cap on ordinary phone video.
- **(B) Purely agentic — a separate `describe_audio` call.** Rejected: two calls over the same file
  never share a time reference, so "what did she say while pointing at this" cannot be answered; the
  model must stitch whole-file audio to 8 scattered frames by guessing.
- **(C) Couple them at the window — one parameter on `view_video`.** Chosen.

**Decision: (C)**, with a single `with_audio` boolean and **no separate audio range**: the span is
whatever was sampled for pictures (`start_time`/`end_time`, else first-to-last sampled frame). Two
independent ranges would drift, and a caption whose span does not match the frames shown invites the
model to attribute a sound to the wrong moment — worse than no caption.

### The 2-minute ceiling is enforced, not just advertised

The operator asked for guidance against transcribing audio just to parse a long video, with an
exception when a recording genuinely must be understood whole. That is in the tool description **and**
in `ViewVideoTool.AUDIO_WINDOW_LIMIT_S = 120`, because models request what a schema lets them request:

- over the limit → frames are still delivered, plus the actual span in seconds, one concrete shorter
  window to retry, and a pointer to `describe_audio` for the whole-file case;
- under it → caption text is appended to the tool's own message with an explicit "same span as the
  frames, from a separate model" caveat.

`describe_file()` skips the byte cap when a window is given: that limit exists to stop one request
swallowing an hour of audio, and a 30-second excerpt of a large `.mp4` honours it even though the
container on disk does not.

### Offsets stay absolute

A window cut at 90 s reports chunk offsets from **90**, not 0 — otherwise caption labels quote
timestamps that point at the wrong moment and correlation with the frames silently breaks. A single
chunk of a window is labelled `[m:ss - m:ss]` too, so an excerpt can never be mistaken for a
description of the whole clip. Without ffmpeg the window is **refused** rather than answered with the
full file: describing more than was asked for, quietly, is the one wrong answer here.

Audio failure never costs the frames — `preflight` problems, caption errors and unexpected exceptions
all degrade to a note beside intact images.

### Deliberate limits

- **The attachment path is unchanged.** Auto-routing whole-file audio for uploads stays as DEC-056
  left it; this change only adds the on-demand, windowed coupling. Whether an attached *video* should
  listen at all is still open (see DELIVERY_RECORD follow-ups).
- **No new setting.** The ceiling is a constant beside the frame caps (`MAX_FRAMES`), tuned to the
  same intuition as "8 frames, not the whole clip".
- **Runtime-unverified against a real Omni model** — ffmpeg cutting and chunk labelling are verified
  by execution; the captioner call itself is stubbed in tests.

## DEC-065 — Every transcript bubble caps at one scroll window, and every one collapses

**Ask (operator):** fixed maximum height for the tool bubbles (~10 lines), *never* cut content
short, collapsible so the transcript can be tidied — and the thinking blocks may open expanded.

### The two knobs are separate, and conflating them is what went wrong before

DEC-061 established that a display budget must not clip text the user needs. This change keeps
that: **nothing is removed, only scrolled.** One shared cap on the *body* of each transcript
block, `overflow-y: auto`, content always reachable by scrolling inside the bubble.

| block | before | after |
|---|---|---|
| tool chip body | `max-h-[70vh]` (~⅔ viewport), not collapsible | one-line header + ~10-line scroll window, `<details>` collapsed on click |
| reasoning (live + history) | **no cap at all** — long thinking pushed the answer away; collapsed by default | same cap, **expanded by default**, still click-collapsible |
| audio caption (live + history) | `max-h-[70vh]` / `max-h-[60vh]` (two different numbers) | same shared cap; live stays open-while-streaming then collapses on `caption_done` (unchanged) |
| web-source lists inside a chip | own `max-h-48` / `max-h-32` scrollers | no inner cap — they flow inside the chip's single scroll window |
| running / composing chips | uncapped, long `path_hint` could grow them | capped at the same height (no collapse: transient, replaced by the result chip) |

**Nested scrollers were the bug, not the cap.** A list with its own `max-h-48 overflow-auto`
inside a bubble that also scrolls is precisely what reads as "cut off despite the scrollbar", so
the inner caps went away rather than being tuned.

### The whole tool chip became a `<details>`

`toolChip()` now builds `<details open>` with the header (tool name + ✓/✗) as the `<summary>`.
Consequences worth recording:

- `attachWebSources(chip, …)` and the download-button logic keep receiving the same element, so
  no call site changed; sources land inside the scrollable body.
- A `<details>` that carries its own padding would clip the last visible line *into its own
  border*, so the chip's horizontal padding moved to the summary row and the vertical padding to
  the body wrapper (see `base.css` → `details.tool-chip`).

### Why raw CSS rather than Tailwind utilities

The height lives in `base.css` under `#messages .bubble-body, #messages details > :not(summary)`,
with a `--bubble-max-h` custom property as the single tuning knob (15rem ≈ 10 lines of 11px/1.5
chip text). Two reasons:

- The vendored build is JIT-in-browser: a utility on dynamically inserted markup can be compiled
 *after* first paint, and `max-h-*` derives from `--spacing`, which the JIT's base layer sets. A
 chip whose cap arrives late flickers; one capped by a stylesheet already on the page does not.
- The live builders and the history re-render must agree exactly (the `text-[12px]` lesson in
  `chat.js`). One CSS rule covers both by construction instead of relying on two class strings
  staying in sync.

The `details > :not(summary)` selector is deliberate belt-and-braces: any transcript block built
as a `<details>` gets capped whether or not its author remembered `.bubble-body`. Verified with
the vendored runtime injected — reasoning blocks are capped through that selector alone (their
markup carries no utility class for it).

### Verified by measurement, not by eye

Headless Chromium against `scratch/bubble_check.html`, which reproduces the exact chip/reasoning/
transient markup plus `base.css` and the real `vendor/tailwind.js`:

| case | expected | measured |
|---|---|---|
| 120-line tool result | capped, scrollable to the end | body 253px (cap 240 + padding), scrollHeight 1898, `scrollTop` reaches end, all 120 lines present |
| short tool result | natural height, not padded out | 62px chip / 29px body |
| long reasoning block | capped via the `details` selector, `open` | 252px body, scrollHeight 2071, `max-height: 240px`, `overflow-y: auto` |
| collapse interaction | header stays, body hides | chip 287px → 34px on `open=false` |
| chip padding | CSS beats the JIT utilities | details L/R = 0, summary 8/12, body 4/12 |

**Still runtime-unverified:** the live SSE path (a streaming caption collapsing on
`caption_done`, progress chips retiring) needs a running server and was not exercised.


## DEC-066 — Files panel: one row is a link plus a ⋮ menu, and Delete really deletes the file

**Ask (operator):** in the Files tab, replace the download button with a three-dots menu offering more
options — download, delete, "send to attachment" (attach the file into the next message), and anything else
worth having. Follow-up report while testing: *"deleting a file didn't delete it from the workspace."*

### The row already had the download; the second one was noise

The filename in `#artifactList` has always been an `<a href="/api/artifact/{id}" download>`, and the row also
carried an explicit ⬇. That duplicate cost horizontal space on the narrowest surface in the app (an 85vw
sidebar drawer on a phone) precisely where the truncated filename needed it. So: **the link stays as the only
download affordance on the row**, and every action — including download, for people who prefer menus to
targeted clicks — moves into `⋮`.

Reusing the conversation-row menu pattern (`openConvMenu`) rather than inventing a second popup style keeps
one dismissal model: outside-click, Escape, scroll, resize, plus right-click and long-press on touch. The
item list is data-driven (`FILE_MENU_ITEMS` with an optional `visible(art)` predicate) exactly like the
composer's `ATTACH_ITEMS`, so conditional items (image preview) and future actions are one entry each.

Two details worth keeping:
- **The menu header repeats the filename.** Several rows look identical once truncated, and a menu whose
  only warning is the word "Delete" gives no chance to notice you opened the wrong row.
- **Handlers delegate on `#artifactList`, not per row.** `loadArtifacts()` rebuilds every `<li>` wholesale, so
  per-row listeners silently vanish on refresh; `findArtifact()` reads name/URL back out of the live DOM
  instead of caching a list at load time, which cannot desync from a re-render.

### "Send to attachment" is not an upload

`stageArtifact()` pushes the **existing** artifact id into `S.attachments`, where a normally-staged upload
would sit. `/api/chat/stream` already re-resolves each id through `artifacts.get(user.id, …)` (DEC-024), so
referencing an owned file is the whole feature — no transfer, no duplicate row, and it composes with the
existing send path including steering (`_resolve_steer_attachments`). Deduped by id: pressing the item twice
must not stack two identical chips.

### The delete report was stale code, but behind it a real gap

The reported symptom reproduced against a server process started **before** the patch — i.e. it was old code
in memory, not a bug in the new handler. Worth recording anyway, because the underlying endpoint genuinely
did the wrong thing: `POST /api/artifact/{id}/delete` called `artifacts.delete()`, which is documented as
"Remove the DB row only (leaves the workspace file in place)". Nothing else ever passed a purge flag, so from
the UI's point of view deleting a file left the bytes in the jail unreachable — listed by nothing, downloadable
by nothing, occupying the workspace forever.

Resolution: the endpoint takes `{purge:bool}`. The row always goes; `purge:true` also removes the file via
`ws.resolve_in_jail()` (never a raw path) and reports `file_deleted`. **The UI sends `purge:true`** — from a
user's mouth, "delete this file" means the file. `purge:false` stays the default so any other caller keeps the
old row-only semantics rather than being surprised by data loss. Audit action `artifact_delete` records path
and purged flag. Deletion asks first (`confirm`), mirroring conversation delete.

### Verified

| Check | Method | Result |
|---|---|---|
| Row markup has ⋮ and exactly one link | DOM assertion in Playwright probe | 1 anchor, no separate ⬇ |
| Menu items + order | `data-act` list from open menu | download, attach, copylink, delete (preview only for images) |
| Attach stages without re-upload, deduped | chip count after 1 and 2 clicks | 1 then 1 |
| Row-only delete keeps bytes (default) | `HttpTests.test_artifact_delete_purges_file` | `file_deleted:false`, file still readable |
| `purge:true` removes the file | same test + live curl | `file_deleted:true`, download then 404 |
| CSRF guards the mutation | POST without token | 403 |
| No regressions | full selftest | 204 passed, 2 skipped |

**Still runtime-unverified:** the browser-level click-through never completed here — uploads issued from
Playwright in this sandbox failed with `ERR_ALPN_NEGOTIATION_FAILED` (an environment proxy issue unrelated to
the change), so the probe could not seed a row. The DOM/menu assertions above that *did* run came from markup
and handler inspection plus the API-level tests, not from a human-speed session in a browser.
`scratch/files_menu_probe.py` is left in place to finish that pass.

---

# Audit remediation (DEC-067 … DEC-073)

Seven records for the work done against `AUDIT_SILENT_DECISIONS.md` — an audit of silent
caps, swallowed failures, hard-coded policies and rigid seams. Each entry states what the
code did before, what it does now, and which test locks it down. Findings are cited by
their audit number. Full report: `AUDIT_SILENT_DECISIONS.md`; progress log:
`AUDIT_PROGRESS.md`.

## DEC-067 — The workspace listing says how incomplete it is, in the prompt and in `fs_list`
Question: What should the model be told when we cap what it can see of the user's files?
Decision: One setting governs the per-prompt listing (`agent.workspace_listing_entries`,
default 80 as before); the listing itself appends `…and more (the listing stops here — use
fs_list for the rest)`; and the environment preamble is *generated* from that same number
rather than asserting a fixed claim. `fs_list` gains its own setting
(`agent.fs_list_entries`, default 500) and now names its cap in its result. Both learn
whether anything was omitted from a new `EntryList.truncated` flag on `workspaces.list_tree`.
Evidence: The old code capped at 80 silently while `WORKSPACE_PREAMBLE` told the model "The
workspace listing in this prompt shows every file" — an active misinstruction, because a
model that believes the list is complete never calls `fs_list`, so a 312-file workspace was
invisible to agent and user alike (audit #1 / B3). `fs_list` then capped at 500 with no note,
so both layers lied at once (#3). `fs_search` already announced its cap; this matches it.
Reversibility: High — defaults reproduce the previous limits; only the wording changed.
Note for whoever revisits this: the marker deliberately says "and more", **not** "and N
more". A capped listing cannot know how many entries it omitted, and an intermediate version
of this fix counted one probe entry past the cap and reported "1 more" for a directory with
308 hidden files — a fabricated number in the system prompt, worse than no number. If exact
counts are ever wanted, `list_tree` must count the whole directory (uncapped scan) rather
than infer it from the truncated read.
Tests: `test_workspace_listing_cap_is_settings_driven_and_announced`,
`AuditRegressionTests.test_fs_list_announces_its_cap`.

## DEC-068 — A prompt section that fails says "failed", never silence
Question: When building a prompt section goes wrong, what should the model read?
Decision: An empty string is banned as an error result for these sections. The workspace
listing renders `(listing FAILED — do NOT conclude the workspace is empty; use fs_list with
'.' to try again)` and the project block renders `(A linked project folder EXISTS but its
details could not be read right now…)`, each audit-logged (`workspace_listing_failed`,
`project_context_failed`).
Evidence: Both were wrapped in `except Exception: return ""`. Absence of a section reads to
a model as absence of data, so a jail error or DB fault made a populated workspace look empty
and Hearth told the user their files were gone (#5). DEC-026 called this "degrade to no-op";
a no-op that falsifies the contents of someone's workspace is not neutral.
Reversibility: High.

## DEC-069 — Upload replacement is reported, and the size cap is an admin setting
Question: Does re-attaching `report.pdf` silently destroy the first one?
Decision: It still replaces (DEC-040 §2 stands — two attachments are two transfers, and each
message's artifact stays fetchable), but every upload response now carries `replaced` and
`replaced_different_content`, computed by hashing the pre-existing file before truncating it,
and `upload_replaced` is audit-logged with both digests. The chat shows an amber warning when
different bytes were destroyed. Separately, the 25 MiB cap moved into
`uploads.max_bytes`; `/api/upload` returns `max_bytes` and `chat.js` caches it instead of
carrying its own literal.
Evidence: The upload path wrote unconditionally with no existence check (#2), and the limit
was a source constant under a "Configurable later" comment, duplicated at `chat.js:857` (#8).
DEC-040's justification only holds when the two files are identical — which is precisely the
case that can now be detected cheaply.
Reversibility: Medium — response gains fields; nothing removed.
Tests: `test_upload_reports_when_it_replaced_different_bytes`,
`test_upload_limit_is_an_admin_setting`.

## DEC-070 — Provider selection is a registry, and a stub fallback is announced in chat
Question: What happens when the configured backend is not answering?
Decision: `manager.PROVIDERS` maps `model.backend` values to provider classes; adding a
backend is one entry plus its module. Capabilities are declared as data
(`Provider.capabilities()` → `exposes_log_progress`, `measured_usage`, `is_stub`) instead of
being sniffed by comparing `provider.name` to a string literal. Whenever the effective
provider is not the configured one, `last_fallback_note` is set and `_stream_turn` emits a
`provider_fallback` notice that renders as a persistent amber bubble.
Evidence: `get_provider()` ended in an unconditional `return StubProvider()`, so a llama-server
that died mid-session meant talking to a fake model whose answers look real, with nothing said
(#7); and the prefill-progress source was gated on `getattr(provider,"name","")=="llama-server"`.
The stub-as-default itself remains Accepted (DEC-001) — the silence was the defect.
Tests: `test_provider_registry_and_stub_fallback_note`,
`test_provider_capabilities_replace_name_sniffing`.

## DEC-071 — Approvals expire; stale ones are refused rather than executed blind
Question: Is an approval card still valid a day after it was shown?
Decision: No. Past `agent.pending_max_age_s` (default 3600 s, 0 disables) an approved gated
call is not executed: the tool row records that the approval expired, the user is told to re-run
the tool for a fresh preview, and `pending_approval_expired` is audit-logged. Rows with no
timestamp are never treated as stale — an approval is not destroyed by a missing field.
Evidence: `_resume_pending` executed approved calls without any freshness check (#4), while
`project_apply.py` already refuses exactly this hazard for sync ("approval of an older preview
cannot slip a stale overwrite past a [change]"). The step-budget exemption documented there is
unaffected: an approval still never counts against `agent.max_tool_steps`.
Reversibility: High — set the age to 0 for the previous behaviour.
Test: `test_stale_approval_is_not_executed`.

## DEC-072 — Single-process is stated loudly, and contradicts no more
Question: Is `uvicorn --workers` a scaling path?
Decision: No, and README now says so in those words. Hearth warns at boot when
`WEB_CONCURRENCY > 1`, naming the four per-process structures that break — the abort registry
(`cancel.py`), the steering queue (`steering.py`), the caption cache and per-app download
tokens — and what the user will see: Stop answering "nothing is running", steering never
arriving, captions missing on the next turn. The caption cache itself moved from insertion-order
eviction to least-recently-used (touched on read).
Evidence: README recommended `uvicorn --workers` for "many concurrent users" while all four
correctness-critical structures were module-level dicts (#11) — advice that produces failures
looking like model bugs. The caption cache's eviction was documented as "crude but bounded"
while its actual failure mode was the model receiving `[attached: x.mp3]` with no description
as the UI still displayed one (#12).
Tests: `test_single_worker_assumption_is_warned_about`,
`test_caption_cache_evicts_least_recently_used`.

## DEC-073 — Truncation is labelled wherever it happens; tool labels derive from tools
Question: Can a user tell truncated output from complete output?
Decision: Not any more. `ui_preview()` appends `(truncated for display — N of M characters not
shown; the model may have seen more)` when it clips, keeping the display/model budget split
(DEC-061) but labelling which one the user is looking at. `zip_pack` reports omitted paths
(`tools.zip_pack.max_files`, default 200) instead of saying "packed 200" about a 250-file
request. `pdf_read` states that a wider range was requested and where to continue. History
replay resolves download buttons by persisted artifact id, with the old filename scrape kept
only as a fallback for rows written before ids were stored. Admin tool labels derive from
`Tool.label` via `tools.TOOL_LABELS_BY_NAME`, so a new tool cannot be added without one.
Evidence: #13 (4 000-char chip vs 24 000-char model budget, unlabelled), #18 (files vanishing
from a zip under a truthful-looking count), #6 (a 40-page request answered with 6 pages and no
complaint), #10 (two same-named artifacts sharing one download link; renaming a file losing its
button), #17 (labels hand-synced in a second registry). B2's 43 dead lines duplicating
`_progress_event` after `_merge_progress`'s return are deleted, with an AST test that fails if
unreachable statements reappear.
Tests: `test_ui_preview_is_generous_but_bounded` (updated),
`test_zip_pack_reports_omitted_paths`, `test_pdf_read_announces_its_page_clamp`,
`test_tool_labels_are_derived_from_the_classes`, `test_no_dead_duplicate_of_progress_event`.

### Deliberately not changed

* **`pdf_read.MAX_PAGES` / `view_video.MAX_FRAMES` are not admin settings.** Unlike a listing
  cap, these multiply image tokens per call by whatever a model happens to request; the audit's
  "one setting plus one sentence" is half right — the sentence is worth adding (done), the knob
  is not safe to hand out. Both now quote their own limit in the tool description and result.
* **`UI_PREVIEW_MAX_CHARS` stays separate from `agent.tool_output_max_chars`.** DEC-061's
  reasoning is sound; the missing piece was a label, not a shared budget.
* **The four per-process dicts are not moved to Redis.** That is a real change requiring a
  deployment decision; the honest interim step is a boot warning plus a README that no longer
  recommends the configuration that breaks them.
* **`data/workspaces/<uid>/project/`, the stale full copy of this tree, is not deleted** — it
  lives inside a live user's workspace and deleting it is destructive. Documented in
  `data/README-do-not-search.md`, with `.gitignore` entries so ripgrep skips it; the audit's own
  searches were misled by it, and mine were too.

# Image generation is Qwen Image, on purpose (DEC-074 … DEC-075)

## DEC-074 — Image generation supports one architecture: Qwen Image / Qwen Image 2.1
Question: `hearth/diffusion.py` tried to serve SD1.5, SDXL, Flux and SD3 as well as Qwen
Image; what should it support instead?
Decision: **Qwen Image only**, and the settings/UI are reshaped around exactly that. Three
required files, each with its own flag — `--diffusion-model` (the DiT .gguf), `--llm` (the
Qwen3-VL text encoder), `--vae` (`diffusion_pytorch_model.safetensors`) — resolved from one
"Model folder" setting by filename, with three optional overrides for unusually named files.
Sampling is pinned to what works: `--sampling-method euler`, `--diffusion-fa`,
`--preview none`. Removed outright: `clip_l`, `clip_g`, `t5xxl`, `lora_dir`, the preview
toggle, and `-m/--model` as a way to name the main model.
Evidence (all reproduced on this machine with sd-cli `master-1106-532d2c1`):
* The old `build_argv()` passed the main model as `-m/--model` and had no `--llm` flag at
  all, so **no setting combination could ever have launched Qwen Image.** Passing its DiT
  .gguf through `--model` fails with `get sd version from file failed` — which reads like a
  corrupt model, not a wrong flag.
* A missing `--vae` exits 1 after ~240 lines of `[E] VAE tensor '…' not in model metadata`;
  a missing `--llm` exits 1 with `Conditioner model tensor 'text_encoders.llm.…'`. Neither
  mentions the setting at fault, which is how this feature sat broken unnoticed.
* Working, verified end to end through the rewritten module: 640×640/6 steps in 17 s; batch
  of 2 with `-b` and `hearth_%03d.png`; `-s 0` honoured as a real seed (not random);
  image-to-image via `-i … --strength 0.6`.
Reversibility: Medium — the file contract is small and documented, but adding another
architecture now means a new backend module with its own flag set, not widening this one.
That is the intended cost: every unused encoder knob was also a way to configure a broken
run, and "broad support" here meant a UI full of fields that all produced the same failure.
Tests: `DiffusionQwenImageTests` (16) in `scripts/selftest.py` — folder discovery, the
`qwen`/`qwen3vl` name collision that must not hand the DiT file to the encoder role,
explicit-path precedence, legacy-key fallback, per-file refusal wording, argv flag contract
(including "never `--model`"), seed 0 vs omitted random, clamping, log-signature
translation, per-file status, and a guard that the removed settings stay removed.

## DEC-075 — A refused generation names the file; sd-cli's own failure is translated
Question: When generation cannot run or fails, what do the model and the admin read?
Decision: `preflight()` requires all three files and reports **each** missing one by its
setting label ("'VAE (.safetensors)' does not exist: /path — set it in Admin → Images"),
before any subprocess starts. When sd-cli itself fails, `explain_failure()` matches the log
tail against known signatures (`get sd version from file failed`, `VAE tensor`,
`text_encoders.llm`, `not in model metadata`) and puts the plain-language cause first, the
raw tail second, the log path last. The Images tab shows one tile per required file with its
filename and **where it came from** (folder / your explicit path / environment / older
setting), because "Model file: missing" never said which of three was wrong.
Evidence: the failure message is what a small chat model repeats to the user, and what an
admin acts on; the old text was one generic string for four distinct causes. Reproduced
output with the LLM .gguf put in the VAE slot: *"the diffusion CLI exited with code 1 after
0.7s — the VAE is missing or wrong for this model: set 'VAE (.safetensors)' to the
diffusion_pytorch_model.safetensors that ships with the Qwen Image files; last output: …"*.
Note for whoever revisits this: the hint table matches substrings, so it must stay ordered
most-specific-first and any new architecture needs its own signatures — a wrong-but-known
hint is worse than no hint, because the model will repeat it confidently.

## DEC-076 — Stop reaches the running sd-cli; it had never reached anything
Question: `Diffusion.stop_active()` looked like it killed the generation in flight. Did it?
Decision: No — and the fix is a two-line branch, kept with its own tests because the shape of
the bug is easy to re-introduce. `_active_proc` holds the running child or `None`, so
`if p is None or p.poll() is None: return False` returned early for **every running** child
(`Popen.poll()` is `None` while the process lives). Stop recorded intent and signalled
nothing; the intent was then honoured by `_set_active()` on the *next* generation, killing a
process that had already exited. Now: `None` → record intent and return False; anything else →
`_kill_tree(p)` and report whether it is really gone. Related fixes made in the same pass:

  * `_kill_tree()`'s grace went 2 s → 15 s. Measured here, a mid-generation sd-cli (1024×1024,
    30 steps) exits on SIGTERM in well under 20 s and releases VRAM as it goes; escalating to
    SIGKILL sooner risks leaving driver-side allocations behind for a process that was already
    dying. `killed` now means "it ignored SIGTERM", which is the useful signal.
  * A stopped run no longer blames the model files. Signal exits land in the same `rc != 0`
    branch as real failures, so pressing Stop produced *"the diffusion CLI exited with code -15
    — the model files must all come from the same Qwen Image package"*, which sends the user
    hunting a config problem that does not exist. Negative exit codes now read
    "stopped by Hearth (Stop)" / "killed by its timeout limit".

Evidence: before the change, measured against a real Qwen Image generation in flight —
`stop_active()` returned `False` in **0.00 s** having sent no signal, and the child was still
running 42 s later (`pgrep` + `/proc/<pid>/pgid` confirmed it was Hearth's own process group;
earlier readings that suggested otherwise were an artefact of `pgrep -f "sd-cli"` matching the
shell that ran it). After: `stop_active() -> True in 0.5 s`, process group empty, thread
unwound with "image generation was stopped by Hearth (Stop) after 20.9s".
Tests: `DiffusionCancellationTests` (3) — Stop kills a running child *and its group* and takes
longer than 0.05 s to return (an instant return is the bug), a Stop racing model load is not
lost, and a stopped run never carries the "same package" hint. `scratch/diffusion_runtime_probe.py`
was also corrected: its orphan check used `pgrep -f "sleep 60"`, which matches the invoking
shell too, and its wrapper let dash optimise `sleep 60 & wait` into an exec — so the probe
could pass without ever testing group teardown. Both now assert on Hearth-created process
groups only, and TIMEOUT/CANCEL/BUSY all PASS.

## DEC-077 — `display_media`: showing a file to the user, and keeping those bytes out of the model
Question: The tools could render, describe, and hand out files — but could the assistant put a
picture on the user's screen? Decision: No, and `display_media` is that missing direction. Every
existing media path points at the *model*: `ToolResult.images` becomes a synthetic user-role
multimodal message (DEC-052), so `pdf_read` pages and generated PNGs reached an mmproj projector
and never the browser — the user saw a tool chip plus a ⬇. The new tool takes workspace paths,
classifies them by extension into image/audio/video, refuses anything a browser cannot present
(naming it, and pointing at `upload`), registers each as an artifact, and returns
`meta.media = [{path, kind, url, artifact_id, name}]`. Files then load from `/api/artifact/{id}` —
the route that already enforces ownership and decrypts at rest — so no new endpoint, no token,
nothing to authorise separately. Rendering is native HTML (`<img>`/`<video controls>`/
`<audio controls>`) driven by `addMediaBubble`, live from the SSE event and again from the
persisted row, so a refresh does not quietly un-display what was shown.

Two boundaries did the real work:

  * **The model must not be fed what it cannot perceive.** `meta["images"]` is encoded as
    `image_url` data URLs on *every* later turn, and an mmproj projector is an image encoder — so
    listing a clip there would base64 tens of megabytes into each request for nothing. Displayed
    files therefore live under their own persisted key (`display`), and `build_messages` derives
    from it only the subset whose extension is genuinely an image. Audio/video arrive as one short
    text line naming them, which is also what stops the model narrating a video it merely put on
    screen (`describe_audio` / `view_video` are named in its result for that reason).
  * **One source of truth per file.** The first version put an image in *both* `images` and
    `display`; replay then encoded the same PNG twice — two identical data URLs, double the image
    tokens. Measured: 2 `image_url` parts for one displayed picture. Now `display_media` sets
    neither `images` field; the display list is authoritative and replay yields exactly one part.

Full screen is the browser's own `requestFullscreen()` on the live element — no viewer to build,
so playback state survives, Esc works, and an odd container degrades to whatever Chrome/Safari do
rather than to a broken custom overlay. iOS Safari does not support element fullscreen at all, so
a rejection (or a promise that resolves without anything being fullscreened) falls back to opening
the artifact URL in its own tab: a click can never be a dead end. Download is always a *separate*
`<a download>` rather than a wrapper around the media — nesting an anchor round an `<img>` makes
clicking-to-view save the file instead, and nested interactive content is invalid HTML anyway.

CSP gained `media-src 'self' blob:` (DEC-018's policy otherwise inherits `default-src 'self'`).
This widens no source; it exists because some browsers re-check a fullscreened element's
subresource loads against the top-level document's policy, which is how a same-origin `<video>`
turns black-and-frozen exactly when full screen is pressed.

Admin: `tools.display_media`, **on** by default, approval **auto** — showing a file the user can
already download is not an act worth an approve card per picture, and gating it would only train
users to mash Approve. Off simply removes the ability to show things.

Tests: `DisplayMediaToolTests` (12) — registration/enable/approval, kind detection incl. refusal of
pdf/md/zip, jail escape + missing file + non-media each named in the reply, nothing-displayable is a
failure not a quiet success, no audio/video ever enters the model image channel, exactly one
`image_url` part on replay (the double-encode regression), video-only display still reaches the
model as text, `./`-prefixed paths survive (`resolve_in_jail()` returns a `Path`, and
`bool(Path("."))` is `False` — an earlier revision used it as a truth value and silently dropped
every such path), missing files skipped rather than announced, per-call cap stated when hit.


# Image editing is a second profile in the same runner (DEC-078)

## DEC-078 — Qwen Image Edit shares the diffusion runner as a second profile, not a new module
Question: DEC-074 said "supporting another architecture means adding a new backend module".
Qwen Image Edit arrived — is it that new module, or something else?
Decision: **It is not a new architecture; it is a second *profile* of the same one.** Qwen Image
Edit runs on the very same `sd-cli` binary and needs the very same three-file shape (DiT + text
encoder + VAE). What differs is which weights fill those slots, how the input image enters, and
what sampling is pinned. So rather than fork `diffusion.py`, it now carries two **profiles** —
`PROFILE_GEN` (`diffusion.*`, unchanged) and `PROFILE_EDIT` (`image_edit.*`) — that share one
runner, one lock, one log, and all the cancel/kill code. A `Profile` object holds everything
architecture-specific: setting prefix, env prefix, role list + flags, folder-discovery patterns,
required-role set, CFG default, optional `--flow-shift`, and a `zero_cond_t` switch.

The two are configured as **independent slots** so an operator can run either or both:
  * Generation: DiT + **Qwen3-VL** encoder + VAE, CFG ~4.5 (unchanged from DEC-074).
  * Editing: Edit DiT + **Qwen2.5-VL** encoder + VAE, an optional fourth `--llm_vision` mmproj for
    the 2509 checkpoint, CFG **2.5**, and `--flow-shift 3`. The reference image goes in as `-r`
    (reference conditioning through the VLM + VAE encoder), **not** `-i` — img2img is a different
    mechanism that perturbs with noise; Qwen Image Edit's whole point is preserving the unedited
    regions, which reference conditioning does and img2img does not.

Evidence (upstream `stable-diffusion.cpp` `docs/models/qwen_image_edit.md`, commit 5dcfa798):
the CLI examples pass `--diffusion-model <Edit .gguf> --vae <qwen_image_vae.safetensors>
--llm <Qwen2.5-VL>` with `-r <input.png> -p "<instruction>" --cfg-scale 2.5 --sampling-method
euler --flow-shift 3`; the 2509 recipe adds `--llm_vision <...mmproj...gguf>`; and 2511 needs
`--model-args qwen_image_zero_cond_t=true`. That is why `llm_vision` is an **optional** role
(emitted only when a path resolves) rather than required — the original checkpoint and 2511 fold
vision into the single `--llm` file, so demanding it would refuse setups that work.

Why not two modules: the runner's hard-won parts (process-group kill, DEC-076's stop polarity, the
busy lock, log-tail translation, temp-dir staging) are identical for both, and duplicating them is
how those fixes drift out of sync. The one thing that must NOT be shared is a model load — so they
share the **lock** (a draw and an edit never overlap; they compete for the same GPU) but keep
separate settings and file slots.

Backward compatibility: `PROFILE_GEN` reproduces the old module byte-for-byte — same setting keys,
same env fallbacks (`HEARTH_DIFFUSION_*`), same legacy keys (`diffusion.model_path`, `diffusion.vae`),
same argv (no flow-shift, no `-r`, CFG 4.5). Every function (`resolve_paths`, `preflight`,
`build_argv`, `status`, `generate_sync`) takes an optional `profile=` defaulting to generation, so
existing callers and all 19 pre-existing diffusion tests pass unchanged.

UI: the Images tab renders both profiles from one shared block helper — a tile per file (an unset
**optional** role reads grey "optional", not amber "missing"), a per-profile test button ("Draw a
test image" / "Test an edit"; the edit probe synthesises a throwaway PNG via Pillow because editing
needs an input), and a copy-command button each. Loaded models gains an `image_edit` row that only
appears when editing is enabled or configured, so nobody gets a permanent red row for a capability
they never turned on.

Env fallbacks: `HEARTH_IMAGE_EDIT_{CLI_BIN,MODEL_DIR,DIT,LLM,VAE,LLM_VISION,BACKEND}`. The edit CLI
binary falls back to the generation one when unset — same executable, only files differ.

Tests: `DiffusionEditProfileTests` (12) — profile isolation (edit settings never leak into
generation resolution), folder discovery of the Edit DiT by name, optional mmproj not required but
emitted when set, `-r` + CFG 2.5 + `--flow-shift 3` and no `-i`, generation argv unchanged (no
flow-shift/`-r`, CFG 4.5), missing-file refusal naming the profile, disabled-switch wording, shared
binary fallback, settings surface, tool registration/gating, and edit_image requiring both image and
prompt.
