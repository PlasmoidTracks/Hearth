# CONTINUATION GUIDE — pick this project up from a fresh context

> Read this + docs/CONTRACT.md + docs/DELIVERY_RECORD.md and you can continue without any prior
> conversation. Everything is in the repo; nothing depends on chat memory.

## What this project is
**Hearth** — a self-hosted, multi-user LLM harness on llama.cpp for one family. FastAPI backend +
Jinja2/Tailwind UI + SQLite. Runs with a built-in **stub model** (no GPU/llama.cpp needed) and can be
pointed at a real `llama-server`. Full scope in docs/CONTRACT.md; user features in docs/FEATURES.md.

## Run it
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
curl -fsSL https://cdn.tailwindcss.com -o hearth/static/vendor/tailwind.js   # one-time asset fetch
# pick a free port (8080 is often taken by the dev tool):
HEARTH_PORT=8081 python -m hearth.main        # or: uvicorn hearth.main:app --port 8081
```
First run prints a one-time admin password (username `admin`). DB lives in `data/hearth.sqlite3`.

## Test it
```bash
python scripts/selftest.py                       # core suite (no web deps) — should be all green
HEARTH_RUN_HTTP=1 python scripts/selftest.py     # + FastAPI TestClient HTTP/CSP/upload tests
```

## Verify the interactive features (manual smoke)
1. Login as admin → chat loads, `model: stub`.
2. Type `list` → one fs_list chip + summary (no loop). `read x` → not found. `write notes.txt Hello` → artifact appears in sidebar + download works. `upload notes.txt` → download button.
3. Attach a file via drag / 📎 menu (Attach files · Camera / Photo) → it stages as a chip (✕ removes), and only sends when you press Send (no per-file reply). Then it appears under Artifacts and downloads back correctly. An image is sent to the model as base64 (needs vision build to "see"). With a project linked, the 📎 menu also offers **Uploads → project** (checkable) instead of the old composer toggle.
4. Toggle language to Deutsch → UI relabels; persists across reload (stored per user).
5. Sidebar: click between chats → the highlighted row follows the selection (`highlightActive()` + `data-cid`). Search box matches titles then message content. Rename / Delete live on each chat row via the ⋮ button (also right-click on desktop, long-press on touch) — works on any row, not just the active one; Delete asks whether to also delete that chat's files.
5b. **Files panel**: the filename downloads; the row's ⋮ offers Download · Attach to message · Open preview (images) · Copy link · Delete. Check that Attach stages a chip in the composer without re-uploading (twice = still one chip), that sending with it reaches the model, and that Delete confirms and then removes the file from `data/workspaces/<user>/` too — not just the row.
6. **Session 4**: Admin → Tools shows grouped per-tool cards (Filesystem / Web / System & exec / Device), each with enable + approval(auto/ask). Try: `fs_edit` ("change X to Y in file" — ambiguous matches must be refused with a helpful message), `fs_delete` (should show an approve card), zip two files ("zip a.txt and b.txt" → download button), web_fetch, datetime. If `HEARTH_WORKSPACE_ENCRYPTION=true` (+key/pass + `pip install cryptography`): write/read/upload/download round-trips still work while on-disk files start with `HEENC1`.
7. Admin → Users: add family member; log in as them; confirm they cannot see admin's artifacts/files.

## Where things live (orientation)
- Backend entry: `hearth/main.py` (app factory, bootstrap admin, security-header middleware/CSP).
- Chat + tools streaming: `hearth/orchestrator.py`, providers in `hearth/llm/` (stub + llama_server).
  Turn cancellation lives in `hearth/cancel.py` (per-conversation handle; routes own its lifetime).
- Tools: `hearth/tools/` (fs read/write/list/upload, exec [gated+sandbox], ask_user, web_search, device stubs).
  `tools/display_media.py` also holds the file-kind classifier (`media_kind`, `non_text_kind`,
  `unreadable_note`) that `fs_read` uses to refuse media and name the right tool (DEC-079).
- Files/jail/artifacts: `hearth/workspaces.py` (path jail), `hearth/artifacts.py` (durable files + ownership download).
- Auth/security: `hearth/security.py`, `hearth/deps.py`, `hearth/ratelimit.py`.
- Admin config: `hearth/runtime_settings.py` (typed settings w/ help) + `routes_admin.py`.
- **Picking server paths in the UI**: `hearth/pathbrowse.py` + `GET /api/admin/browse-path`, driven by
  the 📁 Browse modal at the bottom of `static/admin.js`. Lists the *server's* filesystem for admin
  settings fields (`*_path`/`*_bin`/`*_dir`, flagged as `spec.is_path`), opens on the current value's
  folder, read-only and audited. Deliberately **not** jailed — see DEC-080 before reusing it anywhere
  a non-admin can reach.
- **Second model (audio captioner)**: `hearth/captioner.py` — its own llama-server lifecycle, port,
  log (`runtime/captioner.log`), settings namespace (`captioner.*`) and admin tab. Deliberately NOT
  shared with `manager.py`: two lifecycles that must never interfere (DEC-056). Tools in
  `hearth/tools/audio_tools.py`; attachment routing in `orchestrator.prepare_captions()`.
- **Video + audio together (DEC-064)**: `ViewVideoTool` in `hearth/tools/media_tools.py` takes
  `with_audio` and cuts the captioner's span to whatever window it sampled for frames. The plumbing is
  a single `window=(start,end)` kwarg through `captioner.describe_file()` → `caption_sync()` →
  `prepare_chunks()`; offsets returned are **absolute** (a cut at 90 s reports 90) so caption labels line
  up with frame timestamps. Ceiling: `ViewVideoTool.AUDIO_WINDOW_LIMIT_S` (120 s), enforced — over it the
  frames still arrive with advice. `prepare_chunks()` raises instead of falling back to whole-file when
  ffmpeg is missing, since describing more than was asked for is the one wrong answer here.
- i18n: `hearth/i18n.py` (en/de tables) + `static/i18n.js`; per-user `User.language`.
- Routes: `routes_auth.py`, `routes_chat.py` (chat/upload/artifacts/language), `routes_admin.py`.
- UI: `templates/*.html` (base/login/chat/admin), `static/*.js` (app/chat/admin/login/i18n).

## Session 5 — turn control (abort / budget / visibility / context bar / start-stop)
1. **Abort**: send a tool-heavy message, press **Stop** (or Escape) mid-turn → chip + activity line clear,
   "Turn aborted." appears, Send re-enables; audit shows `chat_abort`. Second tab/turn on the same chat
   gets 409 (`a turn is already running in this chat`). After a page reload during a live turn, the Stop
   button comes back on its own.
2. **Max tool steps**: Admin → Agent → set 2 → Save; a 3-step job stops with "stopped after 2 tool steps"
   and chips show `(1/2)`, `(2/2)`.
3. **Background work**: `write notes.txt hello` shows a running chip (`notes.txt · 5 chars`) *before* the
   ✓ result chip; long tools keep it visible (no silent hang).
4. **Device access**: button + notice reflect helper pairing; device tools refuse until granted AND a helper
   is set (Admin → Tools → Device → Device helper URL); uploads work regardless (by design — see DEC-034).
5. **Model start/stop**: Admin → Model → Start (live "loading…" text) → Restart after changing n_ctx → Stop.
6. **Context bar** in the header updates per turn and persists across reload; ≥80 % turns red + warns.
7. **↻ Retry / ⚡ Force respond / 🤖 Agent-mode↔💬 Chat-only** under the composer (see DEC-031/032).

## Session 6 — live tool-call progress + workspace awareness
1. **Live write progress**: with llama-server, ask "write a big python file" → while the model is still
   COMPOSING the `fs_write` call (args streaming in token by token), a ✎ chip grows:
   `writing · 1.4 KB cool_script.py` (the filename appears once its `"path": "…"` pair has streamed in;
   before that it is just name + size). Hints are throttled (~one per 256 B of new args); only tool name +
   byte count cross the SSE wire — the raw JSON fragment (which contains the whole file) never leaves the
   server. When the full call arrives the ✎ chip is replaced by the normal pending chip; on the result, ✓.
   New SSE event: `tool_progress {name, chars}` (see DEC-038).
2. **ls -l equivalent**: the system prompt's workspace listing now includes file SIZES and marks
   `uploads/` as the user's attachments — the model knows its local files without a tool round-trip.
   `fs_list` was already there (and still is) for fresh listings; it shows sizes too.
3. **No redundant upload**: prompt preamble now tells the model fs_write/fs_append ALREADY register a
   downloadable artifact, so it should only call `upload` to offer an EXISTING file — the double chip +
   double download button from the previous behaviour is gone (model-dependent; small models may still do it).

## Session 7 — one artifact per file + write progress you can actually see
1. **No duplicate files / buttons**: ask "create a hello world python script then provide the download" →
   the Artifacts panel lists `hello_world.py` ONCE even though the model calls `fs_write` and `upload`, and
   only one ⬇ button appears in the transcript. Rewriting a file also keeps a single entry (same id, so old
   links stay valid). Files *you* attach are still one row per upload (`new_row=True` on `/api/upload`).
2. **Live write progress**: with llama-server, `✎ fs_write · writing hello_world.py · 240 B · ~40 %` appears
   within the first ~32 B of arguments (not after 256 B as before) and keeps moving. `tool_progress` now
   carries `path_hint`; only that one already-complete scalar crosses SSE — content never does. The `~%` is
   a client-side estimate (`chat.js growEstimate`) so the bar moves even between coarse hints.
3. First-run note above the transcript explains the file/download flow; it has a dismiss button and also
   retires itself once messages appear (localStorage, so it never reappears).
4. New tests: write+upload lists once, rewrite keeps one id + new size, upload route keeps separate rows,
   `path_hint` forwarded + never content, provider emits an EARLY hint, EN/DE parity for `chat.intro_*`.

## Session 8 — downloads only from `upload`, verbatim reads, anti-loop guardrails
1. **Downloads**: `fs_write`/`fs_append` save + register the artifact but return **no** `meta.download`;
   only `upload` and `zip_pack` do, and those are the only tools whose result draws an ⬇ in the transcript.
   Ask "…and give me the file" → model calls `upload` → one button. The Artifacts panel is where any written
   file is downloadable regardless. DEC-045 reverses DEC-040's "button per file".
2. **Reads are verbatim**: a line-range read returns exactly those lines of the file — no `1: ` prefixes.
   Assert byte-equality with the whole-file read, never a decorated string.
3. **No double execution**: an identical `(tool, args)` request later in the same turn is answered from the
   recorded result with "already done — do not repeat this call" and audit-logs `tool_repeat_suppressed`.
   `_DONE_NOTE` is appended to `arguments` in stored `tool_calls`; anything that replays stored tool_calls
   MUST go through `_clean_tool_calls()` (message graph + token estimate already do) or the note accumulates.
4. **Admin → Generation → Repetition penalty** (`model.repetition_penalty`, 1.0 = off): forwarded to
   llama-server only when > 1.0. It fixes token-level repetition, not repeated tool calls.
5. The activity pulse after a tool result continues only while `step < of`; do not restore the unconditional
   "thinking…", it read as a hang on a finished turn.

## Known gotchas / conventions (learned the hard way)
- **`selftest.py` must end with its `if __name__ == "__main__"` block.** Test classes appended after it
  are never collected — they silently do not run. Appending a class means moving that block back to the
  bottom afterwards. Verify with `./venv/bin/python scripts/selftest.py CaptionerTests`, and **check the
  `Ran N tests` line actually grew** — this bit again in session 14: 13 appended classes ran as green
  while collecting only the old 271.
- **Captioner (`captioner.*`) is a SECOND model**, independent of the chat model: own port (8091), own
  log, own start/stop. Do not fold it into `manager.py`. Audio never reaches the chat model as bytes —
  only the captioner's TEXT does, appended to the message text by `_attachment_content()` (DEC-056).
- **Captioning happens inside the SSE stream**, in a worker thread, with notices drained on a queue and
  the abort handle checked per tick (`routes_chat.chat_stream.gen_sse`). Doing it before the response
  starts looks exactly like a hang. Captions are cached per (user, conv, path) because
  `build_messages()` runs once per agent round — re-listening each round would cost minutes/turn.
- **`'auto'` is not a valid llama.cpp value anywhere.** It parses as `-2` and silently disables GPU
  tensor overrides. Both `manager.build_argv()` and `captioner.build_argv()` map `auto`→omit the flag,
  `all`→`-1`. Never pass the word through.
- **Relative imports**: files directly in `hearth/` use `from .x import`; only `hearth/tools/*` may use `from ..x`. Function-local imports count too — grep `from \.\.` when touching manager/orchestrator.
- **Starlette TemplateResponse** uses the NEW signature: `templates.TemplateResponse(request, name, context)`.
- **scrypt params**: N=16384,r=8,p=1 with explicit `maxmem` (see DEC-012); don't raise N past OpenSSL default or logins break. Self-test guards this.
- **SQLite autoincrement**: PK must be plain `Integer`, not `BigInteger` (DEC on AuditLog). New columns need the idempotent ALTER in `db._migrate_add_columns()` because `create_all` won't alter existing tables.
- **Artifacts.user_id is NOT a FK** — it's an owner key, indexed; isolation is enforced by filtering on it at query time (matches lazy-workspace design). Re-adding a FK breaks orphan-owner inserts (DEC-023). `_migrate_artifacts_drop_fk()` rebuilds old FK-bearing tables on boot.
- **Attachments are staged client-side** (`S.attachments`) and sent only with the message; server re-resolves each `artifact_id` via `artifacts.get(user.id,…)` — never trust a client path (DEC-024). Images → multimodal `image_url` data URLs in `orchestrator._attachment_content`; stub flattens content lists to text (`_flatten_content`) so base64 never leaks into keyword parsing.
- **Conversation delete** removes messages first, then the conversation row (FK order); bulk `.delete()` uses `synchronize_session=False`. Artifacts are deleted only if `delete_artifacts`, else detached to `conversation_id=""` so they persist under "all".
- **Sidebar highlight**: every conv button carries `data-cid`; `openConversation` calls `highlightActive()` to move the highlight without a re-fetch. If you add a new place that renders conversations, set `data-cid` and call `highlightActive()`.
- **Tool specs** are OpenAI-shaped: name lives at `spec["function"]["name"]`. Stub reads tools accordingly.
- **Stub loop guard**: stub only emits a tool if no `tool` result already exists for the turn (prevents MAX_TOOL_LOOPS). Real models behave naturally; the cap stays as a safety net — it is now the admin setting `agent.max_tool_steps`, read via `orchestrator._max_steps()` (never fall back to "no cap").
- **CSP is strict on scripts** (`script-src 'self'`, no inline JS): all page JS must be in external files under `/static`. Styles allow `'unsafe-inline'` (Tailwind JIT). Don't add inline `<script>`/`onclick=""` to templates.
- **Turns write real rows**: anything driving `orchestrator.run_turn()` in a test must create the `User`
  and `Conversation` first (see `TurnControlTests._make_conv`) — `messages.conversation_id` is an FK, so an
  invented id crashes on the first `append_message`. Production has the same shape: a turn can outlive its
  conversation (delete during a cooperative abort), which is why write points raise `ConversationGone`
  instead of letting the FK error escape (DEC-037). Don't "fix" such failures by deleting the FK.
- **Context-bar estimate has overhead baked in**: `chars // 4 + 16`, so it is never exactly `chars/4`.
  Assert `source` + the formula, not round numbers.
- **Never wrap a whole generator in `except Exception`.** `LlamaServerProvider.stream()` used to, which made
  "raised at line 113" indistinguishable from "the model sent nothing" and cost three debugging rounds (three
  wrong root-causes) on what was a broken test fake. Only the HTTP connect/enter is wrapped now; parse errors
  propagate, and the response is released in `finally`. If you add error handling around a stream, keep it
  narrow and let internal bugs surface.
- **Provider test fakes must implement BOTH context-manager protocols.** `_fake_llama_client(lines)` in
  `scripts/selftest.py` stands in for `async with httpx.AsyncClient()` *and* `c.stream(...)`. A fake missing
  the client-side `__aenter__` fails silently under the old catch-all. Use that helper; do not hand-roll a
  new fake per test — three copies drifted independently.
- **Vendor asset**: `hearth/static/vendor/tailwind.js` is NOT committed; fetch per its README or the UI is unstyled (still functional).
- **Abort handles are owned by the route generator, not the orchestrator** (`routes_chat._stream_turn`):
  registered before the first event, cancelled + unregistered in `finally`, so a client disconnect can't
  leave a phantom "running" turn that blocks `/api/chat/stream` (409) or Retry forever. `cancel.end()`
  only unregisters if the handle is still the current one — never unregister someone else's turn.
- **Retry deletes messages**: it must remove ONLY assistant/tool rows after the last user row, keeping any
  later user row. Deleting a `tool` row whose parent `assistant.tool_calls` survives (or vice versa) leaves
  an invalid OpenAI pair and llama-server rejects the request.
- **Device grant travels inside the settings dict** as `_device.granted` (underscore = injected, not a
  SettingSpec key). It is set by `_with_device_grant()` from the DB; never read it from a request body, and
  don't feed `settings` straight into a tool without that wrapper or the gate silently sees "not granted".
- **Stream guard = true idle timer** (`orchestrator._stream_checked`): waits per-event with
  `asyncio.wait_for`, deadline reset on every event, works on Python 3.10. It previously used
  `asyncio.timeout` (3.11+), which is a hard wall-clock cap for the WHOLE completion — a small
  `HEARTH_STREAM_IDLE_TIMEOUT_S` then cancelled every chat request at exactly N s and surfaced as
  "llama-server connection lost (ReadError)" while llama-server logged `stop: cancel task` +1.000 s.
  If that symptom returns, check the env/`.env` value before blaming llama.cpp.
- **Context bar numbers are last-turn measurements**, not a live tokenizer; `source:"estimate"` means
  ~4 chars/token. Prefer llama-server `/props.n_ctx` for the window (it beats the configured value).
- **tool_progress is provider-side throttled** (`llama_server._prog_slot`: one EARLY hint at ~32 B, then
  one per ≥256 B of new argument bytes) and carries ONLY `{name, chars, path_hint}` — never the raw JSON
  fragment (it contains whole file contents; the selftest asserts it can't leak into SSE events, including
  content that itself looks like `"key": "value"`). `path_hint` is the first completed string value of the
  partial JSON (the path in practice), scraped server-side by `_FIRST_STR_RE`; do not move that scraping to
  the client — the fragment never leaves the server. The client (`chat.js toolProgress`) renders a ✎ chip
  per in-flight call and `finishProgress`/`clearPending` remove it when the real
  `tool_call`/`tool_result` lands; `growEstimate` only smooths the bar (never factual claims). New i18n
  keys: `chat.generating/writing/composing_cmd`, `chat.intro_head/body/dismiss` (EN+DE parity asserted).
- **Artifacts are identified by `(user_id, rel_path)`** — `artifacts.record()` refreshes the existing row
  instead of inserting a duplicate (DEC-040), so `fs_write` + `upload` on one file lists it once and keeps
  the same artifact id. Do NOT "fix" duplicates by filtering at display time; that hides them in the panel
  while `/api/artifacts` keeps growing. User uploads pass `new_row=True` deliberately (two transfers of the
  same filename are two records). Re-stamping `conversation_id` means a file written from two chats shows
  under the most recent one — accepted, since workspaces are per user.
- **Registering an artifact is not offering a download** (DEC-045): `fs_write`/`fs_append`/`fs_edit` call
  `artifacts.record()` so the file appears in the panel, but must NOT return `meta.download`. Only `upload`
  and `zip_pack` return that key, and `chat.js` renders a ⬇ for any result carrying it. Add `download` meta
  to another tool and you re-introduce the unrequested-button behaviour the operator explicitly rejected.
- **The Files panel row is a link + a ⋮ menu, not a link + buttons** (Session 9): the filename already
  downloads, so a second ⬇ on the row was pure noise eating width a long name needs. Other actions live in
  `FILE_MENU_ITEMS` (`openFileMenu`) and are delegated once on `#artifactList`, because `loadArtifacts()`
  rebuilds every row — per-row listeners would need re-attaching each refresh. "Attach to message"
  (`stageArtifact`) pushes the **existing artifact id** into `S.attachments`; it must not re-upload, since
  `/api/chat/stream` re-resolves ids by owner (DEC-024). Delete sends `{purge:true}` — row-only deletion was
  the original behaviour and left unreachable bytes in the jail, which read to the operator as "delete is
  broken". Keep the default `purge:false` for any caller that only wants the entry gone.
- **Reads must stay verbatim** (DEC-045): `fs_read` returns exactly what is in the file, including for line
  ranges. It used to prefix ranged output with `"N: "`, which models copied into `fs_edit`'s `old_text` —
  a literal matcher can never match it, so the model looped read→edit→read until max tool steps.
- **`fs_read` refuses media before reading it** (DEC-079): `unreadable_note()` in `tools/display_media.py`
  classifies by extension and names the tool that works (`view_video` / `describe_audio` / `pdf_read` /
  `display_media`). Keep it there — one classifier for "what is this file". Do not sniff bytes, do not
  refuse unknown suffixes (real text hides behind odd names), and keep `.svg` readable (it is XML). Ordering
  is load-bearing: `resolve_in_jail()` first so a missing `gone.mp4` says "not found", media gate second,
  `read_text()` last — decoding is what this prevents (`errors="replace"` means noise, not an exception).
- **Stored `tool_calls` may carry a harness note** (`orchestrator._DONE_NOTE`) inside `function.arguments`.
  Anything that replays them (message graph, token estimates, exports) must pass them through
  `_clean_tool_calls()`, or the note accumulates across turns and inflates token counts. The repeat guard in
  `_agent_loop` keys on `_call_key(tool)` = name + canonical args; keep those two in sync if either changes.
- **`artifacts.created_at` is a monotonic SEQUENCE, not a wall-clock timestamp** (DEC-042b): it decides
  which row counts as "newest" for both dedupe and panel order, and `now()` (whole seconds) made that a
  coin flip between rows written in the same tick. New values come from `_next_seq()` (settings key
  `artifacts.seq`, seeded from `max(existing created_at, now())` so it stays epoch-shaped and never sorts
  new work below legacy rows). Never set `created_at` from `now()` again, and don't render it as a date —
  it is an ordering key that happens to live in a column called created_at.
- **Every agent-loop round must emit exactly one `usage` event.** Providers put usage on their final chunk;
  the loop breaks early on tool requests, so `_agent_loop` synthesises one when the provider sent none
  (`turn_usage is None`) and accumulates across rounds via `_merge_usage`. Estimates carry
  `estimated: True`, are never persisted as measured, and depend on `ToolRequest.args_raw` — drop that
  field and write-heavy turns under-report tokens again.
- **`round_id` correlates chips** (`r{loop+round_offset}` on `tool_progress`/`tool_call`/`tool_result`).
  Client keys progress chips by `name|round`; if you add a new entry point into `_agent_loop`, pass a
  `round_offset` that doesn't collide with rounds already retired, or its chips silently never appear.
- **Image generation is Qwen Image, editing is Qwen Image Edit — two PROFILES in one runner**
  (DEC-074, DEC-078). `hearth/diffusion.py` carries `PROFILE_GEN` (`diffusion.*`) and
  `PROFILE_EDIT` (`image_edit.*`) sharing one runner/lock/log. Generation: `--diffusion-model`
  (DiT .gguf) + `--llm` (Qwen3-VL encoder) + `--vae`, CFG ~4.5. Editing: Edit DiT + **Qwen2.5-VL**
  encoder + VAE (+ optional `--llm_vision` mmproj for 2509), reference image via `-r` (**not** `-i`
  img2img), CFG **2.5**, `--flow-shift 3`. Passing the DiT through `-m/--model` fails as `get sd
  version from file failed`, and a missing required file fails as hundreds of `… not in model
  metadata` lines. Each profile takes ONE folder plus optional overrides; only its REQUIRED files are
  demanded (edit's llm_vision is optional). Sampling is pinned per profile (euler, `--diffusion-fa`,
  `--preview none`) — do not "generalise" the flag set without a third architecture to justify it, and
  add `_LOG_HINTS` entries for any you do add. New backend functions take `profile=` defaulting to
  generation; keep that default so existing callers/tests stay byte-identical.
- **`Popen.poll() is None` means RUNNING** (DEC-076). `Diffusion.stop_active()` once read
  `if p is None or p.poll() is None: return False`, which therefore returned immediately for every
  live child and killed nothing. When you see a "stop/abort does nothing" report, check the guard's
  polarity against that fact first. Also: never prove process teardown with `pgrep -f "<pattern>"`
  from a shell — the pattern matches the invoking `/bin/sh -c`, so it reports orphans that aren't
  there (and hides real ones). Assert on the PGID of the child you created; and note dash optimises
  `sleep & wait` into an **exec**, so a fake "grandchild" harness needs `( … & wait )` to fork.

## Verification discipline (this session had NO execute tool)
The agent could not run the server/tests — the operator ran them and caught real bugs (imports,
TemplateResponse, scrypt memlimit, stub KeyError/loop, test-key mismatch), all fixed + regression-tested.
Every change since is labelled **IMPLEMENTED — statically reviewed; runtime UNVERIFIED** until the
operator re-runs selftest + smoke. Keep that honesty in DELIVERY_RECORD.md.

## Suggested next steps (priority)
1. Operator runs `python scripts/selftest.py` (+ `HEARTH_RUN_HTTP=1`) and the manual smoke above — covering
   **sessions 2, 3, 5, 7 AND 8** → mark VERIFIED. Highest priority; nothing is truly verified until run. Session-5
   specifically: abort mid-turn, `(n/of)` chips, device-tool refusal messages, Start/Restart llama-server,
   context bar growth, Retry not duplicating the user message. Session-7 specifically: one Artifacts row +
   one ⬇ per file after write+upload, and the ✎ chip naming the file with a moving byte count on a real model.
   Session-8 specifically (DEC-045): `fs_write` alone shows **no** ⬇ but the file is in Artifacts; "and give
   me the file" → `upload` → exactly one ⬇; an edit of text just read succeeds first try; a model that repeats
   an identical call stops looping (audit `tool_repeat_suppressed`); Repetition penalty 1.0 → field absent,
   1.1 → applied without a model reload.
2. Wire a real llama.cpp model: build/point `llama-server`, set paths in Admin → Model (or `.env`
   fallback), press **Start**, Backend=llama-server, tune n_ctx/RoPE, **Restart**. Verify tool-calling
   against the real model's function-call format; confirm Abort actually halts generation server-side and
   that `/props` supplies `n_ctx` to the context bar. For vision: set Admin → Model → "Vision projector
   (mmproj) path" (or `HEARTH_LLAMA_MMPROJ_PATH`) with the projector matching the model, Restart, and
   confirm image_url parts are seen (`--mmproj` is passed only when set + the binary supports it; a
   "Vision: mmproj set" badge shows in the Model panel).
3. Real device helper (folder-read + screenshot) over tailnet — the grant gate, `device.helper_url`, and
   refusal messages are already in place waiting for it.
4. In-app "change my password" for users (currently admin-reset only). Rename prompt now pre-fills the current title (row lookup in `renameConversation`). Possible follow-up: replace the native rename/delete confirm dialogs with an in-page modal.
5. Static-CSS Tailwind build → drop `style-src 'unsafe-inline'` from CSP.
6. Optional hardening: pin fastapi/starlette versions after smoke; AV/scan uploads; Redis+Postgres for multi-process scale (the abort registry and rate limiter both need to move out of process memory first).
7. Context overflow UX: the bar now *warns*; next step is acting on it — auto-summarise or drop oldest
   turns in `build_messages`, plus a "compact this chat" action.

## Ideas backlog (operator brainstorm, not started)
Per-message regenerate / edit-and-resend · show attachment chips under each sent message (click→download) · folder upload (webkitdirectory) · artifact previews beyond images (code highlight, PDF/office inline) — the Files ⋮ menu already has an image **Open preview** item to grow · starred/pinned chats + tags on top of search · conversation export (Markdown/PDF) · keep the previous reply beside a retry (draft/compare) · per-message token/cost tooltip from persisted usage. Rename/move a file from the Files ⋮ menu (`ws.move_path` exists; would also need to re-stamp the artifact row's `rel_path`, which is its identity — see DEC-040).

## Doc map
CONTRACT.md (scope + amendments v1.1/v1.2) · DECISIONS.md (DEC-001..036+ why) · ARCHITECTURE.md (system map/trust/invariants, sessions 2/3/5 components)
· SECURITY.md (authz/CSP/sandbox/threats) · OPERATIONS.md (deploy/backup/runbook) · FEATURES.md (user guide incl. attachments/vision/convo mgmt/turn controls)
· DELIVERY_RECORD.md (status/evidence/limitations, session-5 checklist). This file = fastest on-ramp. Read this first, then CONTRACT + DELIVERY_RECORD.
