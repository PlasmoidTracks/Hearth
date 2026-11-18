"""ModelManager — selects a Provider, optionally launches/monitors llama-server,
and applies admin model settings as CLI flags on (re)load (DEC-001, DEC-008).

Selection logic for get_provider():
  1. backend == 'llama-server' and reachable -> LlamaServerProvider
  2. else -> StubProvider (always available; keeps app usable)

Launching llama-server is best-effort: if the binary is missing or fails to come
up, we log and fall back to stub rather than crashing the whole server.
"""
from __future__ import annotations
import asyncio
import shlex
import time
from typing import Optional

import httpx

from . import paths
from . import runtime_settings as rs
from .llm.base import Provider
from .llm.llama_server import LlamaServerProvider
from .llm.stub import StubProvider


class ModelManager:
    def __init__(self):
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._log_path = paths.RUNTIME_DIR / "llama-server.log"
        # Diagnosis from the most recent ensure_server() failure ("port occupied by
        # a zombie", "child died", …). Surfaced through start() so Admin shows the
        # real reason instead of a generic "not reachable".
        self._last_hint: str = ""

    # --- provider selection --------------------------------------------------
    async def get_provider(self) -> Provider:
        settings = rs.get_all()
        if settings.get("model.backend") == "llama-server":
            base_url = await self.ensure_server(settings)
            if base_url:
                p = LlamaServerProvider(base_url)
                h = await p.health()
                if h.get("ok"):
                    return p
        return StubProvider()

    # --- llama-server lifecycle ---------------------------------------------
    def _default_base(self, bs) -> str:
        return bs.llama_base_url or f"http://127.0.0.1:{bs.llama_port}"

    async def probe(self) -> dict:
        """Read-only liveness snapshot for the chat UI's model badge.

        Unlike status() this NEVER launches anything and never consults admin
        settings: it asks whether a llama-server answers right now and, if so,
        what context window it serves. Chat pages poll it on load; a launch on
        page view is not acceptable behaviour (DEC-048).

        A server that answers /health but not the llama-server API (/props) is a
        wedged occupant, not a usable model — report it as such so the badge reads
        "not running" instead of lying about a live backend.
        """
        from .config import get_boot_settings
        bs = get_boot_settings()
        base_url = self._default_base(bs)
        n_ctx = None
        reachable = await self._reachable(base_url) and await self._is_llama_server(base_url)
        if reachable:
            try:
                async with httpx.AsyncClient(timeout=3) as c:
                    p = await c.get(f"{base_url}/props")
                    if p.status_code == 200:
                        j = p.json()
                        n_ctx = j.get("n_ctx") or j.get("context") or None
            except Exception:  # noqa: BLE001 — a badge must never break the page
                pass
        return {"server_reachable": reachable, "base_url": base_url, "n_ctx": n_ctx}

    async def ensure_server(self, settings: dict, force: bool = False) -> str:
        """Return the base URL of a reachable llama-server, launching one if needed.

        force=True restarts so freshly-saved settings (ctx size, GPU layers, …)
        apply — this is what Admin → Model → Start / Restart does. An externally
        managed server (HEARTH_LLAMA_BASE_URL set) is never launched or killed.

        Liveness is judged by /props (_is_llama_server), not just /health: a wedged
        process that still answers HTTP but no longer serves the API made Start
        report "running" while every chat stream died with ReadError. Such an
        occupant is reported ("zombie") instead of bind-fought; our child's own
        startup failure (port taken, VRAM) always lands in runtime/llama-server.log.
        """
        from .config import get_boot_settings
        bs = get_boot_settings()
        base_url = self._default_base(bs)
        external = bool(bs.llama_base_url)

        async def _live() -> bool:      # truly serving the llama-server API?
            return await self._reachable(base_url) and await self._is_llama_server(base_url)

        if not force:
            # A healthy server answers; nothing on the port is fine too — both fall
            # through to launching our own. ONLY a live-but-wedged occupant errors.
            if await self._reachable(base_url):
                if await _live():
                    return base_url
                self._last_hint = (
                    f"Something occupies port {bs.llama_port}, answers HTTP, but not the "
                    f"llama-server API (/props) — likely a wedged old instance. Find it with "
                    f"'ss -tlnp | grep :{bs.llama_port}', kill it, then Start again.")
                return ""
        else:
            if external:
                # We don't own that process; restarting it isn't ours to do.
                if await _live():
                    return base_url
                self._last_hint = (f"Nothing healthy at {base_url} — that is "
                                   f"HEARTH_LLAMA_BASE_URL, a server Hearth does not start. "
                                   f"Restart it yourself.")
                return ""
            owned = self._proc is not None and self._proc.returncode is None
            await self.stop()
            if not owned and await self._reachable(base_url) and not await _live():
                # Someone else's listener survived our stop() and isn't a real
                # llama-server — launching ours would just fail to bind.
                self._last_hint = (
                    f"Port {bs.llama_port} is held by a process Hearth does not own, and it "
                    f"does not answer /props — likely a wedged old instance. Find it with "
                    f"'ss -tlnp | grep :{bs.llama_port}', kill it, then Start again.")
                return ""
        # resolve binary + model: admin settings win, env is the fallback.
        bin_path, model_path, mmproj_path = self._resolve_launch_paths(settings, bs)
        if not bin_path or not model_path:
            return ""
        argv = self.build_argv(bs, settings, bin_path=bin_path, model_path=model_path,
                               mmproj_path=mmproj_path)
        try:
            logf = open(self._log_path, "ab")
            self._proc = await asyncio.create_subprocess_exec(
                *argv, stdout=logf, stderr=logf)
        except Exception as e:  # noqa: BLE001
            print(f"[manager] failed to launch llama-server: {e}")
            return ""
        # wait up to ~120s for the API to come up (large models + full 262k ctx are
        # slow). /props is the readiness gate, so a half-dead occupant never counts.
        for _ in range(240):
            if await _live():
                return base_url
            if self._proc.returncode is not None:
                break  # child died (port conflict, bad flag, OOM) — its log explains
            await asyncio.sleep(0.5)
        self._last_hint = ("llama-server did not become ready. Its own output is in "
                           "runtime/llama-server.log — look for 'couldn't bind HTTP server "
                           "socket' (port taken: stop the old process first) or a CUDA/VRAM error.")
        return ""

    async def start(self, force: bool = False) -> dict:
        """Bring llama-server up (force=True restarts it) and report status.

        Note: this does NOT flip model.backend. Use Admin → Model → Backend to
        choose which provider chats use; starting only makes the server reachable.
        get_boot_settings() is cached, so changing HEARTH_LLAMA_BASE_URL still needs
        a Hearth restart — say so rather than looking like the button was ignored.
        """
        settings = rs.get_all()
        self._last_hint = ""    # fresh diagnosis per Start attempt
        base_url = await self.ensure_server(settings, force=force)
        st = await self.status()
        st["started"] = bool(base_url)
        if not base_url and not st.get("hint"):
            st["hint"] = self._last_hint or (
                "Could not reach llama-server. Check the binary + model paths "
                "(Admin → Model) and runtime/llama-server.log — a failed launch usually "
                "explains itself there (bad path, ctx larger than VRAM, wrong build).")
        elif not base_url:
            # ensure_server diagnosed something specific (zombie port / dead child);
            # prefer it over the generic status() hint, which only knows "unreachable".
            st["hint"] = self._last_hint or st.get("hint") or (
                "Could not reach llama-server. Check the binary + model paths "
                "(Admin → Model) and runtime/llama-server.log.")
        return st

    async def stop_server(self) -> dict:
        """Stop a llama-server we launched. An external server is left alone."""
        from .config import get_boot_settings
        bs = get_boot_settings()
        owned = self._proc is not None and self._proc.returncode is None
        await self.stop()
        st = await self.status()
        st["stopped"] = True
        if bs.llama_base_url:
            st["hint"] = ("HEARTH_LLAMA_BASE_URL points at a server Hearth does not own — "
                          "stop that process yourself; Hearth only manages servers it launched.")
        elif not owned:
            st["hint"] = ("No llama-server was started by Hearth, so there is nothing to stop. "
                          "If one answers anyway, it runs outside Hearth's control.")
        return st

    @staticmethod
    def _push_int(a: list[str], flag: str, val, extra_val=None) -> None:
        """Append `flag N` (and optionally a second positional value) if val is a valid int."""
        try:
            iv = int(val)
        except (TypeError, ValueError):
            return
        a += [flag, str(iv)]
        if extra_val is not None:
            a.append(str(extra_val))

    @staticmethod
    def _probe_flag(bin_path: str, flag: str) -> bool:
        """Does this llama-server binary understand `flag`? (via --help, 5 s cap)

        Used for options newer than the user's build (e.g. --kv-unified) so we can
        add them opportunistically without breaking startup on older binaries.
        Any failure (missing binary, timeout, weird output) counts as "unsupported".
        """
        import subprocess
        try:
            out = subprocess.run([bin_path, "--help"], capture_output=True,
                                 text=True, timeout=5).stdout
            return flag in out
        except Exception:  # noqa: BLE001 — probe failure just means "don't add the flag"
            return False

    @staticmethod
    def _slot_n_ctx(n_ctx, n_parallel) -> int:
        """Context each slot actually gets when kv-unified is OFF (the llama.cpp
        default): the server divides total ctx across --parallel slots."""
        try:
            p = max(1, int(n_parallel))
            return max(512, int(n_ctx) // p)
        except (TypeError, ValueError):
            return int(n_ctx or 0)

    def build_argv(self, bs, settings: dict, *, bin_path=None, model_path=None,
                   mmproj_path=None) -> list[str]:
        bin_path = bin_path or bs.llama_server_bin
        model_path = model_path or bs.llama_model_path
        a = [bin_path, "--model", model_path]

        # Vision projector (mmproj): pass only when a path is configured. Older
        # llama-server builds may not know --mmproj, so probe for it first — a
        # text-only build must still start cleanly rather than die on an unknown flag.
        mmproj = mmproj_path if mmproj_path is not None else (
            (settings.get("model.mmproj_path") or "").strip()
            or getattr(bs, "llama_mmproj_path", ""))
        if mmproj and self._probe_flag(bin_path, "--mmproj"):
            a += ["--mmproj", mmproj]

        # context
        self._push_int(a, "--ctx-size", settings.get("model.n_ctx", 262144))

        # GPU layers. The word 'auto' is NOT a valid llama.cpp value — it parses as
        # -2, which makes common_fit_params ABORT ("n_gpu_layers already set by user
        # to -2") and silently fall back to CPU tensor overrides: the model loads but
        # decodes at CPU speed (the 2 t/s vs 20 t/s symptom). 'auto' therefore means
        # "omit the flag" so llama.cpp auto-fits, matching a bare `llama-server -m …`.
        # 'all' maps to -1 (offload every layer that fits) — also not passed as the
        # literal word. Numeric values pass through unchanged.
        ngl = str(settings.get("model.n_gpu_layers", "auto")).strip().lower()
        if ngl in ("", "auto"):
            pass  # omit --n-gpu-layers entirely; llama.cpp auto-fits what fits VRAM
        elif ngl == "all":
            a += ["--n-gpu-layers", "-1"]
        else:
            self._push_int(a, "--n-gpu-layers", ngl)

        # parallel slots
        self._push_int(a, "--parallel", settings.get("model.n_parallel", 2))

        # host/port (Hearth always binds llama-server to loopback; it fronts it)
        a += ["--host", "127.0.0.1", "--port", str(int(bs.llama_port))]

        # --jinja enables the model's embedded chat template, which modern
        # instruct models (Hermes/Llama-3.1/Qwen tool templates) REQUIRE for
        # native function calling via the OpenAI-compatible API.
        if bs.llama_jinja:
            a += ["--jinja"]

        # Slot-context fairness: with --parallel >1 and kv-unified OFF (the build
        # default), each slot gets n_ctx/parallel — 262144/2 = 131072 per slot — so
        # two chats can OOM a GPU that would comfortably serve ONE full-context
        # request, and a family server rarely decodes two turns at once. Ask for
        # unified KV; if this binary doesn't know the flag (added ~b5156), retry
        # without it so an older llama-server still starts.
        if int(settings.get("model.n_parallel", 2) or 1) > 1:
            if self._probe_flag(bin_path, "--kv-unified"):
                a += ["--kv-unified"]

        # RoPE scaling
        rope = settings.get("model.rope_scaling", "none")
        if rope and rope != "none":
            a += ["--rope-scaling", rope]
        a += ["--rope-freq-base", str(settings.get("model.rope_freq_base", 10000.0)),
              "--rope-freq-scale", str(settings.get("model.rope_freq_scale", 1.0))]

        # flash attention — newer llama.cpp takes [on|off|auto]; older only knows the
        # bare --flash-attn flag. Pass the value form; if a very old build rejects it,
        # EXTRA_ARGS is the escape hatch. 'auto' is the binary default so we can skip it.
        fa = str(settings.get("model.flash_attn", "auto")).strip().lower()
        if fa in ("on", "off"):
            a += ["--flash-attn", fa]

        # performance: threads + batch sizes (only when explicitly set / non-default)
        self._push_int(a, "--threads", settings.get("model.threads", -1))
        self._push_int(a, "--batch-size", settings.get("model.batch_size", 2048))
        self._push_int(a, "--ubatch-size", settings.get("model.ubatch_size", 512))

        # KV cache quantization (both K and V get the same type)
        kvt = str(settings.get("model.kv_cache_type", "f16")).strip().lower()
        if kvt and kvt != "f16":
            a += ["--cache-type-k", kvt, "--cache-type-v", kvt]

        # device selection + load mode
        dev = str(settings.get("model.device", "")).strip()
        if dev:
            a += ["--device", dev]
        lm = str(settings.get("model.load_mode", "auto")).strip().lower()
        if lm and lm != "auto":
            a += ["--load-mode", lm]

        # reasoning / thinking (only force when not auto)
        rea = str(settings.get("model.reasoning", "auto")).strip().lower()
        if rea in ("on", "off"):
            a += ["--reasoning", rea]

        if bs.llama_extra_args:
            try:
                a += shlex.split(bs.llama_extra_args)
            except ValueError:
                print("[manager] ignoring malformed HEARTH_LLAMA_EXTRA_ARGS")
        return a

    @staticmethod
    def _resolve_launch_paths(settings: dict, bs):
        """Resolve (bin, model, mmproj) paths the same way ensure_server() does:
        admin settings win, env (.env / HEARTH_LLAMA_*) is the fallback. Shared by
        launch and preview so the copied command can never diverge from what we run.
        """
        bin_path = (settings.get("model.server_bin") or "").strip() or bs.llama_server_bin
        model_path = (settings.get("model.model_path") or "").strip() or bs.llama_model_path
        mmproj_path = (settings.get("model.mmproj_path") or "").strip() or bs.llama_mmproj_path
        return bin_path, model_path, mmproj_path

    def launch_command_preview(self) -> dict:
        """The exact `llama-server ...` command Hearth launches right now.

        Debugging aid (Admin → Model → Copy launch command): lets an operator run
        the identical invocation by hand to see why it works elsewhere but not via
        Hearth. Built from build_argv(), so flags stay in sync with the real launch.

        Caveat surfaced honestly: --mmproj/--kv-unified are probed against the real
        binary, so the copy reflects the server Hearth would start HERE; running it
        on a different machine/build may need those adjusted. Returns ok=False with a
        note (no half-built command) when the binary or model path is unset.
        """
        from .config import get_boot_settings
        bs = get_boot_settings()
        settings = rs.get_all()
        bin_path, model_path, mmproj_path = self._resolve_launch_paths(settings, bs)
        if not bin_path or not model_path:
            return {"ok": False, "command": "", "argv": [], "configured": False,
                    "note": ("No server binary + model path set, so there is no command to copy yet. "
                             "Fill in the paths (Admin → Model) and Save first.")}
        argv = self.build_argv(bs, settings, bin_path=bin_path, model_path=model_path,
                               mmproj_path=mmproj_path)
        return {"ok": True, "command": shlex.join(argv), "argv": argv,
                "configured": True, "note": ""}

    async def _reachable(self, base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"{base_url}/health")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _is_llama_server(base_url: str) -> bool:
        """Does something on this port actually speak the llama-server API?

        /props is llama-server-specific and cheap. A half-dead process (e.g. an old
        instance that was SIGTERM'd mid-request, or any service that grabbed the
        port) can keep accepting connections while its request handling is wedged:
        health alone then lies, get_provider() builds a provider against it, and
        every chat stream dies with ReadError — which is exactly what happened here.
        """
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                p = await c.get(f"{base_url}/props")
                if p.status_code != 200:
                    return False
                j = p.json()
                return isinstance(j, dict) and bool(
                    "model_path" in j or "total_slots" in j
                    or "default_generation_settings" in j)
        except Exception:  # noqa: BLE001 — unreadable props = not ours to trust
            return False

    async def status(self) -> dict:
        settings = rs.get_all()
        backend = settings.get("model.backend")
        from .config import get_boot_settings
        bs = get_boot_settings()
        base_url = self._default_base(bs)
        reachable = await self._reachable(base_url)
        bin_path, model_path, mmproj_path = self._resolve_launch_paths(settings, bs)
        configured = bool(bin_path and model_path)
        owned = bool(self._proc and self._proc.returncode is None)
        # A clear reason for a stub fallback, so the admin UI can explain itself.
        hint = ""
        if backend == "llama-server" and not reachable:
            if not configured:
                hint = ("No server binary + model path set. Fill in 'llama-server binary path' and "
                        "'Model (.gguf) path' below (or the HEARTH_LLAMA_* env vars), then Start.")
            elif not owned:
                hint = ("Configured but not running — press Start, or see the launch log at "
                        "runtime/llama-server.log (common causes: wrong path, ctx too large for "
                        "VRAM, incompatible build).")
        return {
            "backend": backend,
            # Chats use llama-server only when the backend is set AND it answers.
            "effective": "llama-server" if (backend == "llama-server" and reachable) else "stub",
            "server_reachable": reachable,
            "base_url": base_url,
            "proc_running": owned,
            # Whether a Start/Stop press would do anything here.
            "startable": configured or reachable,
            "managed_by_hearth": owned or not bool(bs.llama_base_url),
            "n_ctx": settings.get("model.n_ctx"),
            # What one chat slot can actually hold — with --parallel>1 and no
            # kv-unified this is n_ctx/parallel, NOT the configured total. The UI
            # context bar uses the live /props value; this explains the difference.
            "slot_n_ctx": self._slot_n_ctx(settings.get("model.n_ctx"),
                                           settings.get("model.n_parallel", 2)),
            "paths_configured": configured,
            # Vision projector configured? The admin UI shows a vision badge so it is
            # obvious whether image attachments will actually be seen.
            "vision": bool(mmproj_path),
            "hint": hint,
        }

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def read_log(self, max_bytes: int = 65536) -> dict:
        """Tail of the llama-server stdout/stderr log for Admin → Server Log.

        Reads at most `max_bytes` from the END of the file (model loads are
        verbose; the interesting part is always the newest). Returns text plus a
        `truncated` flag and the byte offset, so the UI can honestly say "showing
        the last N KB". Purely informational — never throws: a missing/unreadable
        log becomes ok=False with an explanatory note.
        """
        import os
        max_bytes = max(1024, min(int(max_bytes or 65536), 1_048_576))
        p = self._log_path
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                off = max(0, size - max_bytes)
                f.seek(off)
                data = f.read()
            text = data.decode("utf-8", "replace")
            # Drop a leading partial line so the tail starts on a real line.
            if off > 0 and "\n" in text:
                cut = text.index("\n") + 1
                text, off = text[cut:], off + cut
            return {"ok": True, "path": str(p), "size": size, "offset": off,
                    "truncated": off > 0, "text": text}
        except FileNotFoundError:
            return {"ok": False, "path": str(p), "size": 0, "offset": 0,
                    "truncated": False, "text": "",
                    "note": "No log yet — it appears once Hearth launches llama-server "
                            "(Admin → Model → Start). If you run llama-server yourself, "
                            "its output goes wherever you started it."}
        except OSError as e:
            return {"ok": False, "path": str(p), "size": 0, "offset": 0,
                    "truncated": False, "text": "", "note": f"Could not read log: {e}"}

    # --- live perf / prefill progress (log tail) --------------------------------
    def poll_log_perf(self, state: dict | None = None) -> tuple[list[dict], dict]:
        """Read NEW llama-server log lines and return the metrics they carry.

        THE source for live progress with a real model. llama.cpp prints its own numbers —
        ``prompt processing, n_tokens = 91424, progress = 0.73, ... / 697.05 tokens per
        second`` while it reads a prompt, and ``n_gen = 262, tg = 2.76 t/s, tg_3s = ...``
        while it decodes — and the OpenAI endpoint streams no such thing. So the only way to
        show prefill percentage and an honest ETA during a turn is to read them from here.
        ``hearth.perf`` parses; this method only advances the file cursor and forwards what
        parsing returned, deriving nothing of its own.

        ``state`` is the caller's cursor (``{"offset": int}``); pass None to start at the END
        of the file — a fresh turn must not replay an old prompt's progress lines. Returns
        ``(events, state)`` where each event is one of:

          {"kind": "prefill", "n_tokens": int, "progress": float, "rate_tps": float|None}
          {"kind": "perf", "prefill_tps": ..., "decode_tps": ...}   # from perf.py fields

        Safe by construction: no log (external server, or none launched yet) yields an empty
        list forever. Never raises — a log that cannot be read is not a chat error. The file
        is opened in NON-binary-append-free read mode with no truncation assumption beyond
        the shrink check below.
        """
        import os
        from . import perf as perf_mod
        st = {"offset": int((state or {}).get("offset", -1))}
        out: list[dict] = []
        try:
            size = os.path.getsize(self._log_path)
            # A cursor of -1 means "start at the END" — a fresh turn must not replay an old
            # prompt's progress lines. Resolved HERE, inside the guarded block, because on a
            # cold start the log does not exist yet: getsize() raises, and doing it outside
            # this try propagated FileNotFoundError to the caller (whose blanket except then
            # disabled live progress for good — every later poll re-raised the same way).
            if st["offset"] < 0:
                st["offset"] = size
            # Log rotated or truncated: our cursor is now past EOF and would sit there
            # reading nothing forever. Restart from 0 rather than go silently stale.
            if size < st["offset"]:
                st["offset"] = 0
            if size == st["offset"]:
                return [], st
            with open(self._log_path, "rb") as f:
                f.seek(st["offset"])
                chunk = f.read()
            # Do NOT consume a trailing partial line: llama.cpp may still be writing it, and
            # half a print_timing line parses to nothing (or worse, to the wrong numbers).
            # Leave it for the next poll.
            end = chunk.rfind(b"\n")
            if end < 0:
                return [], st
            st["offset"] += end + 1
            for raw in chunk[:end].decode("utf-8", "replace").split("\n"):
                if not raw.strip():
                    continue
                pp = perf_mod.parse_prompt_progress(raw)
                if pp is not None:
                    n, frac, rate = pp
                    out.append({"kind": "prefill", "n_tokens": n, "progress": frac,
                                "rate_tps": rate})
                    continue
                m = perf_mod.parse_log_line(raw)
                if m is not None and m.has_data():
                    ev = m.as_dict()
                    ev["kind"] = "perf"
                    out.append(ev)
        except OSError:
            return [], st
        return out, st


_manager: Optional[ModelManager] = None


def get_manager() -> ModelManager:
    global _manager
    if _manager is None:
        _manager = ModelManager()
    return _manager
