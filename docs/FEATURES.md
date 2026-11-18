# Hearth — Features & User Guide

Audience: end users (family) + anyone continuing development from docs alone.
For internals see ARCHITECTURE.md; for decisions DECISIONS.md; for status DELIVERY_RECORD.md.

## For family members

### Sign in
Open the address your admin gives you, enter your username + password. No public signup —
ask the admin to create an account. If you enabled 2FA, enter your code too.

### Chat
Type a message and send (Enter sends; Shift+Enter = newline). The assistant streams its reply.
The header shows which model backend is active (`stub` = built-in demo, `llama-server` = real) and a
**context bar** — how many tokens the model received last turn versus the configured window
(red near 90 %; hover for details, including whether the number is measured or estimated).

While a reply is being produced you can press **Stop** (or Escape) to abort the turn. The server stops
generating immediately; partial text stays in the chat and is marked "Turn aborted".

Below the composer:
- **🤖 Agent mode / 💬 Chat only** — whether the assistant may use tools at all on your next message.
- **⚡ Force respond** — one-shot: answer the *next* message directly with no tool calls, then agent
  mode comes back on its own. Use it when the model is stuck in a tool loop and you just want words.
- **↻ Retry** — discard the assistant's last reply (and any half-finished tool chatter) and generate
  it again from the same question. Your message and attachments are untouched.

Your assistant can use **tools**. In stub/demo mode these keywords trigger them so you can try:
- `list` — list files in your private workspace
- `read notes.txt` — read a file you have
- `write notes.txt Some text here` — the assistant writes that file into **your** workspace
- `upload notes.txt` — gives you a download button for a file
- `run echo hello` — runs a shell command in a sandbox (needs admin to enable + your approval)

### Your private space
Every user has a private workspace on the server. The assistant can only see *your* files, never
anyone else's. Files it writes and files you upload are yours.

### Uploading files (device → server)
Three ways, all into your private workspace:
- **Drag & drop** anywhere on the chat page (desktop).
- **📎 menu → Attach files** — pick one or many files (desktop + mobile).
- **📎 menu → Camera / Photo** — take a photo or choose from your gallery (mobile; opens camera/gallery).

The composer carries a single 📎 button whose popup holds these actions (and future ones), so the
input row stays uncluttered. When a project folder is linked, the menu also gets an
**Uploads → project** check item: on, uploads land in the project copy instead of attaching to the chat.

Uploaded files land under `uploads/` in your workspace and appear in the Artifacts panel.

### Artifacts panel
The left sidebar lists files produced by the assistant or uploaded by you, for the current
conversation — with download links that keep working after you log out and back in. Click ↻ to refresh.
Switch between **this chat** and **all** above the list.

The filename **is** the download link; there is no separate ⬇ button per row. Everything else lives in the
row's **⋮ menu** (also via right-click, or long-press on touch):

| Item | What it does |
|---|---|
| ⬇ Download | Saves the file (same as clicking the name). |
| 📎 Attach to message | Puts the file into the composer as an attachment for your **next** message — no re-upload, and pressing it twice does not stack a second copy. Handy for "what is in this file?" or continuing work on something the assistant wrote earlier. |
| 👁 Open preview | Opens image files in a new tab (shown only for images). |
| ⧉ Copy link | Copies the download URL to the clipboard. |
| 🗑 Delete | Forgets the entry **and deletes the file from your workspace** — it asks first, because this is not recoverable. |

The menu is titled with the filename, so a mistap on the wrong row is visible before you commit to a
destructive item. Confirmation and status notes appear briefly at the bottom of the composer.

A workspace file appears **once**: writing a file twice, or writing it and then offering it, still leaves
one entry with one link (the entry follows the most recent change). Files you attach yourself are
kept per upload, so re-attaching `report.pdf` twice leaves both transfers downloadable.

### Downloads: writes save, `upload` offers
Writing a file (`fs_write` / `fs_append` / `fs_edit`) **saves** it and lists it under Artifacts — that is
where you always pick it up again. It does not put a ⬇ button in the conversation. A ⬇ button appears only
when the assistant actually offers a file: the `upload` tool, or a `zip_pack` bundle. So ask for a download
when you want one ("…and give me the file"), and you will get exactly one button per offered file.

### Seeing what it is doing
Tools are not silent. While the model works you see:
- a **write chip while the file is still being composed** — `✎ fs_write · writing hello_world.py · 240 B ·
  ~40 %`. It appears within the first few bytes, names the file as soon as the model has streamed its
  path, and counts the bytes written so far. The `~%` is an estimate shown only to make progress legible;
  the byte count is real. (Live progress needs a streaming model — with the built-in stub, calls are
  instant, so this chip is a flash.)
- a pulsing **activity line** (`⚙ tool: fs_write (1/6) — notes/a.txt · 812 chars`) that stays up for
  as long as the tool runs, so writing a file or building a zip never looks like a frozen page;
- a **running chip per call**, replaced by a ✓/✗ result chip with an output preview when it finishes;
- **clickable web sources** under web-tool chips — after `web_search`, the actual results (title → link,
  host, snippet); after `web_fetch`, a "read page: host" link plus the page's main links, and which backend
  served it (`duckduckgo-html`, `http (tor)`, `real-browser`). Links open in a new tab and stay checkable
  when you reopen the chat — so any answer built on the web can be verified at its source;
- `(step/of)` counters so you can see how much of the turn's tool budget is used;
- `awaiting approval` on calls that are waiting for your Approve/Deny card.

File contents are summarised (path + size/line count), never dumped into the transcript.

### Tidying the transcript: every bubble has a ceiling and a header
Long output cannot take over the page, and nothing is ever hidden from you by accident (DEC-065):

- **One height budget.** Tool-result chips, reasoning blocks, 🎧 caption blocks and the transient
  running/composing chips all stop at about **10 lines** and scroll *inside* the bubble. Long text is
  always reachable — scroll within the block; it is never trimmed or clipped away.
- **Click a header to fold it away.** Every result chip shows `tool: name ✓/✗` as its header, and
  clicking that line collapses the output to a single row — useful after a long chain of file reads.
  Click again to reopen. Reasoning and 🎧 blocks work the same way.
- **Reasoning opens expanded.** The model's thinking block is open by default (live and when you
  reopen a chat), so reading it is the norm rather than something you have to hunt for; collapse it
  when you do not care. Audio captions stream open while listening, then fold themselves up once the
  description is done, so the answer stays in view.
- **One scrollbar per bubble.** Web-tool source lists no longer carry their own inner scroll window —
  nested scrollers were what made output look "cut off despite a scrollbar".

### Why it stops repeating itself
A model that does not like a tool result can ask for the *same* call again. Hearth no longer runs it twice:
the second identical request in a turn is answered with the recorded result plus an instruction to change
approach or just reply, and the file is touched only once. Reads are byte-exact (no line numbers), so
"read a file, then edit what you read" matches on the first try. If a model repeats *words* rather than tool
calls, Admin → Generation → **Repetition penalty** (default off) nudges llama.cpp sampling — see DEC-045 for
what each of these does and does not fix.

### Allow device access
The header button grants the assistant access to a **paired device helper** (folder listing,
screenshots) for *this conversation only*. It is stored on the server, not in the page, so the model
cannot be talked past it and refreshing does not silently keep or drop the grant. Press it again to
revoke.

Two separate things are needed before a device tool can succeed, and the notice under the button tells
you which one is missing:
1. an admin must set the **device helper URL** (Admin → Tools → Device). No helper ships yet, so today
   the tools report honestly that there is nothing to talk to;
2. you must have pressed **Allow device access**. An admin can drop requirement 2 (Admin → Agent).

**Attaching and downloading files never needs this switch.** Those are your own file transfers — you
chose the bytes and the destination — not access to your device, so they work regardless.

### Language
Bottom of the sidebar: choose **English** or **Deutsch**. It's saved to your account and applies
to the interface (buttons, labels). The assistant's reply language follows what you type / the model,
not this setting.

## For admins (additions)
- Create/disable users, reset passwords, promote/demote — Admin → Users.
- Enable **Command execution** in Admin → Tools (still requires per-command approval + a sandbox).
- Edit markdown **protocols** and **system prompts** in Admin → Docs; users attach them per chat.
- Model settings (context length, RoPE scaling, GPU layers, sampling) in Admin → Model, each with inline help.
- **Start / Restart / Stop llama-server** buttons in Admin → Model. *Start* launches it with the saved
  binary + model paths; *Restart* relaunches so changed settings (`n_ctx`, GPU layers, KV quant…) take
  effect; *Stop* shuts down a server Hearth launched (one you run yourself via `HEARTH_LLAMA_BASE_URL`
  is never touched). Loading can take minutes — the panel says so instead of hanging. Note these buttons
  do not change which provider chats use: that is the **Backend** dropdown.
- Admin → **Agent**: **Max tool steps per turn** (how many times a model may call tools before it must
  answer; default 6) and **Require device-access grant**.

## Endpoint reference (new this round)
| Method | Path | Purpose | Auth |
|---|---|---|---|
| POST | `/api/upload` | multipart upload 1..N files into caller's jail (CSRF) | user |
| GET | `/api/artifacts?conversation_id=` | list caller's artifacts (omit param = ALL) | user |
| GET | `/api/artifact/{id}` | download an owned artifact | owner |
| POST | `/api/artifact/{id}/delete` | forget artifact row; body `{purge:bool}` — if true the workspace file is deleted too, else only the entry (the UI sends `purge:true`) | owner |
| POST | `/api/language` (form `language`) | set UI language preference | user |
| GET | `/api/i18n` | string table for current language | any |
| POST | `/api/chat/conversations/{id}/rename` | rename (body `{title}`) | owner |
| POST | `/api/chat/conversations/{id}/delete` | delete chat; body `{delete_artifacts:bool}` — if false, its files are kept under "all" | owner |
| GET | `/api/chat/search?q=` | search chats by title then message content (snippet) | user |
| POST | `/api/chat/abort` | abort the in-flight turn; body `{conversation_id}` | owner |
| GET | `/api/chat/running?conversation_id=` | is a turn streaming right now? | owner |
| POST | `/api/chat/retry` | drop the last reply and re-stream it; body `{conversation_id, no_tools?}` | owner |
| GET | `/api/chat/context-usage?conversation_id=` | tokens used vs window (for the context bar) | owner |
| POST | `/api/chat/device-access` | grant/revoke device access for one chat; body `{conversation_id, granted}` | owner |
| GET | `/api/chat/device-access?conversation_id=` | current grant + whether a helper is paired | owner |
| POST | `/api/admin/model/start` | launch llama-server; body `{restart?:bool}` to relaunch with new settings | admin |
| POST | `/api/admin/model/stop` | stop a Hearth-launched llama-server | admin |
| GET | `/api/admin/models` | **every** loaded model in one call: chat model + audio captioner, each with configured/running state and its own hint | admin |
| POST | `/api/admin/captioner/start` | start the captioner's own llama-server; body `{restart?:bool}` | admin |
| POST | `/api/admin/captioner/stop` | stop a Hearth-launched captioner server (external ones untouched) | admin |
| GET | `/api/admin/captioner/test` | caption a generated 2 s test tone — end-to-end wiring check, no user audio involved | admin |
| GET | `/api/admin/captioner/command` | the exact captioner `llama-server …` command, shell-quoted | admin |

A turn's SSE stream can now also emit `aborted` (user pressed Stop) and `notice` events with
`kind: caption_start | caption_chunk | caption_done` while a recording is being described — both for
an attached file **and** for a `describe_audio` tool call. `caption_chunk` carries the captioner
model's own words as they stream in (DEC-057, DEC-061), shown in a collapsible block like the chat
model's reasoning; toggle `captioner.stream` off to get one silent wait instead. Pressing **Stop**
now also terminates the captioner request mid-listening (and its
Hearth-owned server process), so it no longer keeps running after the turn ends. `tool_call`
events carry `summary`, `step`, `of`, and `awaiting_approval`; `usage` carries `n_ctx`. Tool
result chips show up to 4 000 characters of output rather than a fixed 800, and scroll only when
there is genuinely more than fits (DEC-061) — the scroll window itself is now one shared ~10-line
ceiling for every transcript bubble, and each chip collapses by clicking its header (DEC-065).

## Attachments & images (how it behaves now)
- **Staging:** attaching a file uploads it immediately but does NOT send a message. Chips appear above the composer; ✕ removes one. Everything is sent together when you press Send — so the assistant answers once, not per file. You can also send attachments with no text.
- **Images:** sent to the model as base64 data URLs (multimodal), never raw bytes in the chat text. To actually "see" them, configure a vision-capable model **and** its mmproj projector in Admin → Model → "Vision projector (mmproj) path", then Restart. Text-only models just note an image was attached.
- **Too-large uploads** now report the file name and the 25 MB limit instead of a generic failure.
- **Model awareness:** the assistant is told which files exist in your workspace, so it can offer to read them.

## Media: PDFs and video (how it behaves now)
The assistant can look at documents and video **as images**, so visual information is not lost.

| Tool | What it does | Needs on the server |
|---|---|---|
| `pdf_info` | page count + title/author/page size, no rendering | pymupdf **or** poppler-utils (`pdfinfo`) |
| `pdf_read` | renders chosen pages to PNG and *shows them* — scans, figures, tables, layout survive | pymupdf → pdftoppm → gs → mutool (first available) |
| `video_info` | runtime, fps, resolution, frame count, size, and whether it carries audio — no decoding | ffmpeg (`ffprobe`) **or** opencv-python |
| `view_video` | decodes a few frames and *shows them*: 8 equally spaced by default, or `count`, `start_time`/`end_time` window, or explicit `timestamps`. With `with_audio=true` it also describes **what is heard over that same span** | ffmpeg → opencv-python (+ captioner for audio) |
| `context_usage` | tokens used / window size / percent / remaining, and whether that was measured or estimated | nothing (bookkeeping only) |

- **Video can be watched *and* heard in one call.** `view_video` with `with_audio=true` cuts the
  audio to exactly the window it sampled for pictures — there is no separate audio range to get out of
  sync — and returns the captioner's description alongside the frames, so the assistant can say *"at
  0:41 she raises her hand, and that is when she says it"*. Off by default, and **capped at 2 minutes of
  span per call**: describing audio costs a model request every few seconds of clip, so for a long video
  the right move is to understand it from frames first and then re-call with `with_audio` on the short
  part that matters. Ask for more than 2 minutes and the frames still arrive, plus an explanation of how
  to get the audio affordably — if a recording genuinely must be understood as a whole, `describe_audio`
  covers it (slowly). Needs the captioner under Admin → Audio; without it the frames are unaffected and
  one line says why there is no description.

- **Vision required to actually see them.** Rendered pages/frames go to the model through the same
  multimodal path as your image uploads, so they need a vision-capable GGUF **plus its mmproj**
  (Admin → Model). Without one the tools still work and save the files — they just tell you the model
  could not see them. Rendered images land under `media/pdf/` and `media/video/` in your workspace, so
  they are re-readable and downloadable like any other file.
- **Context cost is bounded on purpose:** `pdf_read` renders at most 6 pages per call (it tells the
  model where to continue), `view_video` at most 16 frames, and images are downscaled to
  `media.max_image_edge` pixels (default 1280) before being sent. Each tool is separately toggleable in
  Admin → Tools; nothing runs unless a backend for it exists.
- **Colour is preserved.** Pages and frames are sent as colour PNGs — no greyscale conversion. That
  costs roughly 3× the *bytes* of a greyscale image but exactly the same number of vision tokens, so
  nothing about prefill or context occupancy changes; only the size of the request payload does.
- Install what you want it to read: `pip install pymupdf Pillow` and/or a system `ffmpeg`. Missing
  backends produce an error naming the package, never a silent failure.

## Audio: what a recording actually contains (how it behaves now)
Hearth can run a **second model that listens**. Upload a voice note, a meeting recording, a song
snippet — and instead of seeing only a filename, the assistant gets a description of what the audio
model *heard*: how many people speak, their voices and emotion, the words where they are clear,
background sounds, music, room acoustics. This works even though your chat model has no audio support,
because what comes back is plain text.

| Tool | What it does | Needs on the server |
|---|---|---|
| `audio_info` | duration, codec, sample rate, channels — and whether a video file has an audio track at all | ffmpeg (`ffprobe`) |
| `describe_audio` | asks the captioner model what it hears; optional `focus` ("transcribe verbatim", "only the music") | captioner configured in Admin → Audio |

- **Automatic:** attach an audio file and Hearth describes it as the message is sent. You'll see
  *listening to …* while that happens — a long recording genuinely takes a while, and the silence
  would otherwise look like a hang. Stop/Abort works during it. Switch this off in Admin → Audio
  ("Automatically describe attached audio") if you'd rather the assistant ask first.
- **The description streams live either way.** Whether Hearth described your attachment
  automatically or the assistant called `describe_audio` itself, the audio model's words appear
  as they are produced — one collapsible 🎧 block per file — instead of one long silent wait.
- **Descriptions survive a refresh, and stay in context.** A caption is stored with the message it
  belongs to, so reloading the page brings the 🎧 block back (click its header to read) instead of
  losing it. It also keeps working on later messages in the same chat — earlier recordings remain
  described for the assistant rather than decaying into a bare filename. If a description failed,
  the block says so rather than looking like nothing was ever done.
- **Long recordings are cut into timed chunks** (default 30 s) and each part is labelled with its
  start time, so the assistant can say "at 1:30 she laughs". Needs `ffmpeg`; without it the file is
  sent whole and says so.
- **It is a separate model, on its own port.** Starting, restarting or stopping it never touches the
  chat model — see Admin → Model → **Loaded models**, which lists both with their real status.
- **CPU offload by default.** These audio models are usually Mixture-of-Experts: they want lots of
  RAM and gain little from sitting half on a GPU, so `Captioner GPU layers` starts at 0 (CPU only).
  Set it to `all` if the whole model fits in VRAM, or 8–24 to park just a few layers there.
- **Honest about its limits.** Every description arrives labelled as coming from another model that
  can be wrong about words, counts and identities — so the assistant quotes it as a description
  rather than treating it as a transcript. If nothing is configured you are told so plainly; nobody
  invents what your recording said.
- **Setup:** Admin → Audio → enable, set *Captioner model (.gguf)* (e.g. a Qwen-Omni GGUF) and its
  matching *mmproj*, keep the port at 8091 (must differ from the chat model's), then **Start
  captioner**. Weights can take minutes to load. **Test with a tone** captions a generated 2-second
  beep to check the whole chain — if it describes speech over a beep, something is misconfigured.

## Conversation management
Search box (matches titles, then message contents). Rename and Delete live on each chat row: tap the ⋮ at its right edge (or right-click / long-press the row) for a small menu. Deleting asks whether to also delete that chat's files; choosing "no" keeps them under the **all** artifacts view.

## Security notes relevant to these features
- Uploads: size-capped (25 MiB/file), streamed (not fully buffered), filename-sanitized, jail-scoped. No AV scanning (family trust boundary).
- Artifacts/downloads enforce **ownership** at read time — a guessed id from another user returns 404. Attachments in a message are re-resolved server-side by owner; client-supplied paths are never trusted.
- Device access is a real per-conversation server-side flag gating the `device_*` tools, but no helper
  daemon ships yet, so those tools report that nothing is paired — no real device data leaves your
  device via Hearth.
- Abort and Retry only ever act on conversations you own (`cancel.py` keys handles by conversation and
  checks the owner; `/api/chat/retry` re-checks ownership before deleting rows).
- `no_tools` from the client can only *remove* capabilities from a turn; it cannot enable tools, lower
  an approval gate, or grant device access. The device grant is read from the DB, never from a request.
