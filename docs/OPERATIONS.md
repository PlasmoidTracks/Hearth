# Operations runbook

## Install & run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set HEARTH_SECRET_KEY for production
uvicorn hearth.main:app --host 127.0.0.1 --port 8080
```

## Let family devices in (LAN)
```bash
HEARTH_HOST=0.0.0.0 HEARTH_COOKIE_SAMESITE=lax python -m hearth.main
# firewall (pick ONE): sudo ufw allow from 192.168.0.0/24 to any port 8080 proto tcp
```
Startup prints the LAN URL. Then **Admin → Users** → add an account per person; each gets
their own workspace, artifact list, and language, and cannot see anyone else's files.

Checklist before you hand out the address:
- Set `HEARTH_SECRET_KEY` (a stable one — otherwise every restart invalidates sessions).
- Give each account a real password; enable per-user TOTP from the account menu where it matters.
- Plain HTTP on the LAN means unencrypted passwords/chat. OK-ish at home; for anything beyond
  the router use Tailscale + HTTPS and `HEARTH_COOKIE_SECURE=true` (see SECURITY.md).
- Leave llama-server on loopback: Hearth launches it on `127.0.0.1:8081` and proxies it, so the
  model port never needs to be exposed.
- With `SameSite=Strict` (the default) a device that reaches the app by LAN IP may load the page
  but fail every send — that's the cookie policy doing its job, not a broken server; use `lax`.

## systemd unit (Linux)
`/etc/systemd/system/hearth.service`:
```ini
[Unit]
Description=Hearth LLM harness
After=network-online.target

[Service]
Type=simple
User=hearth
WorkingDirectory=/opt/hearth
EnvironmentFile=/opt/hearth/.env
ExecStart=/opt/hearth/.venv/bin/uvicorn hearth.main:app --host 127.0.0.1 --port 8080
Restart=on-failure
# hardening (adjust paths to your sandbox needs)
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
`sudo systemctl enable --now hearth`. Front with Tailscale or a TLS reverse proxy.

## Model operations
- **Status**: Admin → Model shows backend, effective provider (stub/llama-server), whether a server is
  running, n_ctx, and whether binary+model paths are set. Also `GET /healthz`.
- **Start / Restart / Stop** (Admin → Model) manage the llama-server Hearth launches:
  - **Start** launches it with the saved paths/settings and waits for `/health` (up to ~120 s).
  - **Restart** stops then relaunches — this is how model-affecting settings (`n_ctx`, RoPE, GPU layers,
    `n_parallel`, KV quant, threads/batch) actually reach llama-server. Prefer it over restarting Hearth.
  - **Stop** shuts down a server Hearth started. A server you run yourself (pointed at by
    `HEARTH_LLAMA_BASE_URL`) is never launched or killed by Hearth — stop it yourself.
  - These buttons do **not** change which provider chats use: that is the **Backend** dropdown
    (`stub` vs `llama-server`). `/api/admin/model/reload` still exists for compatibility (status only).
- **Failed start?** The panel shows why (paths missing vs launch failure) and points at
  `data/runtime/llama-server.log`. Common: wrong path, context too large for VRAM, incompatible build.
- **OOM**: lower `model.n_ctx`, reduce `n_parallel`, or enable quantization at model load. RoPE
  scaling extends effective context without retraining but can reduce quality — see inline help.
- **Stuck generation**: users can abort their own turn from the chat (Stop / Escape). If a slot is wedged
  server-side, Restart llama-server. An idle stream is auto-ended after
  `HEARTH_STREAM_IDLE_TIMEOUT_S` (300 s default).

## Chat turn control (operator view)
- One active turn per conversation; a second request for the same chat gets HTTP 409 rather than
  interleaving history. Aborts are cooperative: the loop stops between events and before tools, so an
  already-running `exec` finishes its call first (sandbox timeout still bounds it).
- **Admin → Agent**: `Max tool steps per turn` (default 6) limits how long a model can tool-loop before
  it must answer; `Require device-access grant` decides whether the chat-header button is mandatory
  before `device_*` tools run.
- Audit actions to know about: `chat_abort`, `chat_retry`, `usage_save_failed`,
  `device_access_grant|revoke`, `model_start|model_restart|model_stop`.


## Backup & restore
All state lives under `data/` (SQLite + workspaces). Back it up while the app is idle or use
SQLite's online backup:
```bash
# safe snapshot of the DB (WAL-aware)
sqlite3 data/hearth.sqlite3 ".backup 'backup/hearth-$(date +%F).sqlite3'"
tar czf backup/data-$(date +%F).tar.gz data/
```
Restore: stop the service, replace `data/`, start. Test restores periodically (documented but
**not yet executed** in this prototype — do a restore rehearsal before relying on it).

## Health & logs
- `GET /healthz` → `{ok, model:{server_reachable, base_url, n_ctx}}`. Uptime/liveness probe
  against it. **Read-only**: it never launches llama-server (DEC-048) — the chat page calls it
  on every load, and a page view must not start a model. Use Admin → Model → Start, or send a
  chat message with Backend=`llama-server`, to bring one up.
- llama-server stdout/stderr → `data/runtime/llama-server.log`.
- Security events → audit table (Admin → Audit). Application logs to stdout under uvicorn.

## Incident response
1. Disable a user: Admin → Users → disable (immediate; their sessions fail on next request —
   for instant revoke, also "log out everywhere" or clear their session rows).
2. Kill runaway exec: exec is bounded by `exec.timeout_s` (60 s default) + sandbox resource limits; if
   llama-server misbehaves, Admin → Model → **Stop** (or systemctl / kill the pid in `data/runtime/`).
   A user's own runaway turn they can abort themselves (Stop), which also frees that conversation.
3. Rotate signing key (`HEARTH_SECRET_KEY`) to invalidate all sessions (logs everyone out).
4. Someone else aborted your turn / chat feels locked? Only one turn per conversation runs; a wedged
   handle clears when the stream ends or the process restarts, and `/api/chat/running` reports truthfully.

## Scaling notes
Designed for family scale on one process. To go bigger: move rate limiting, download tokens, and the
in-flight turn/abort registry (`cancel.py`) to Redis, switch DB to Postgres, run llama-server separately
with more slots/GPU, and use multiple Hearth workers behind a proxy with sticky sessions (Abort must reach
the worker holding the stream). These are tracked as follow-ups.
