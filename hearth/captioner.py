"""Audio captioner — a SECOND, independent llama.cpp model that listens.

Hearth's main model is usually text-only (or vision-only via mmproj), so an uploaded
recording was previously dead weight: `video_info` could say "audio tracks: 1 (audio is
NOT transcribed)" and nothing else. This module adds a second model — typically
Qwen-Omni, which describes what it HEARS rather than merely decoding words: timbre,
voices, emotion, music, ambience, overlapping speakers. Its description comes back as
plain text, so the main model can reason about a recording without ever needing audio
support of its own.

Two backends, mirroring the operator's proven harness:

  server   Hearth launches/owns a dedicated llama-server on its own port and keeps the
           weights loaded between calls (fast; the default). Audio goes to
           /v1/chat/completions as an OpenAI `input_audio` content part.
  cli      One `llama-mtmd-cli --audio <file>` process per chunk. Slower (weights reload
           every time) but needs no port and no supervising, so it is the fallback when
           a server cannot be started or VRAM is tight.

Design constraints worth knowing before editing:

  * Independent of hearth/manager.py on purpose. Two processes with separate ports, logs
    and lifecycles: restarting the captioner must never disturb the chat model, and
    vice versa. Same discipline as manager.py (probe /health + /props before trusting a
    port, never bind-fight a wedged occupant), reimplemented rather than shared because
    the settings namespace and lifecycle differ.
  * Heavy work (ffmpeg, subprocesses, base64) is synchronous and runs in a worker thread;
    async callers use asyncio.to_thread so chat keeps streaming.
  * Every failure returns a readable CaptionError whose message names the missing binary,
    the port, or the log file. A captioner that cannot run must degrade to "I could not
    listen", never to a broken chat turn and never to silence.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import paths
from . import runtime_settings as rs

# Accepted upload extensions. Kept as an explicit list rather than mimetypes.guess_type:
# a browser reports audio/mpeg for a .mp3 that is really AAC, and llama.cpp cares about
# the container extension it is handed, not a sniffed MIME string.
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".webm", ".mp4")

# Video containers are in the list above because they carry the audio track people
# actually want described — but only when one is present (see has_audio_stream()).
# The instruction sent alongside every chunk. The single source of truth is
# runtime_settings.DEFAULT_CAPTIONER_PROMPT so the admin UI's displayed default and the
# value actually sent can never drift apart; re-exported here for callers/tests.
DEFAULT_PROMPT = rs.DEFAULT_CAPTIONER_PROMPT

# Safety clamp on the admin-set cap: an unbounded chunk_seconds would hand the captioner
# an hour of audio in one request and stall a chat turn for many minutes.
_MAX_CHUNK_SECONDS = 900


class CaptionError(Exception):
    """Captioning is unavailable or failed. Message is model/user-facing."""


def _cancel_requested(cancel) -> bool:
    """Has a cancel-like object been set? Never raises.

    Callers hand us one of two shapes: a ``threading.Event`` (or the ToolContext adapter,
    what the describe_audio TOOL passes), and hearth.cancel.CancelHandle — asyncio-based,
    exposing ``.cancelled`` — which is what orchestrator.prepare_captions passes for an
    attachment described before the turn starts. Accepting only ``.is_set()`` used to break
    that second path with AttributeError on the first chunk of every directly-attached
    recording. Duck-typing both here keeps captioner independent of hearth.cancel (see the
    module docstring) and makes a missing/unfamiliar object read as 'cannot be aborted'.
    """
    if cancel is None:
        return False
    try:
        if hasattr(cancel, "is_set"):
            return bool(cancel.is_set())
        return bool(getattr(cancel, "cancelled", False))
    except Exception:  # noqa: BLE001 — a dead handle reads as "not cancelled"
        return False


def fmt_time(seconds) -> str:
    """Seconds -> 'm:ss' for chunk headers the model quotes back to the user."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60}:{s % 60:02d}"


def _num(settings: dict, key: str, typ, default):
    """Read a numeric setting defensively (a junk stored value must not raise)."""
    spec_default = rs.SPEC_BY_KEY[key].default if key in rs.SPEC_BY_KEY else default
    try:
        raw = settings.get(key, spec_default)
        return typ(raw)
    except (TypeError, ValueError):
        return spec_default if typ(spec_default) is not None else default


def _bool(settings: dict, key: str, default: bool = False) -> bool:
    v = settings.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# ---- capability probing ------------------------------------------------------

def have(cmd: str) -> bool:
    """Is an external binary available? ('ffmpeg' or an absolute path.)"""
    if not cmd:
        return False
    if os.path.isabs(cmd):
        return os.path.isfile(cmd) and os.access(cmd, os.X_OK)
    return shutil.which(cmd) is not None


def capabilities() -> dict:
    """What this machine can do for audio. Surfaced in admin status and in failures."""
    return {
        "ffmpeg": have("ffmpeg"),
        "ffprobe": have("ffprobe"),
        "captioner_enabled": False,   # filled in by status()
    }


# ---- configuration -----------------------------------------------------------

def resolve_paths(settings: dict) -> dict:
    """Resolve the captioner's paths/ports from runtime settings with env fallbacks.

    Settings win; HEARTH_CAPTIONER_* env vars are the deployment-level fallback, exactly
    as HEARTH_LLAMA_* is for the main model. Split out so status(), argv building and the
    preview can never disagree about what Hearth would actually run.
    """
    def sget(key: str, env: str) -> str:
        return str(settings.get(key) or "").strip() or os.environ.get(env, "")

    bin_path = sget("captioner.server_bin", "HEARTH_CAPTIONER_SERVER_BIN") \
        or sget("model.server_bin", "HEARTH_LLAMA_SERVER_BIN")
    cli_bin = sget("captioner.cli_bin", "HEARTH_CAPTIONER_CLI_BIN")
    if not cli_bin:
        # Derive from the server binary when only that is configured: sibling binaries in
        # the same build directory are the norm (build/bin/llama-server, .../mtmd-cli).
        cli_bin = _sibling_or_name(bin_path, "llama-mtmd-cli")
    return {
        "enabled": _bool(settings, "captioner.enabled", False),
        "backend": str(settings.get("captioner.backend") or "server").strip().lower(),
        "bin": bin_path,
        "cli_bin": cli_bin,
        "model": sget("captioner.model_path", "HEARTH_CAPTIONER_MODEL_PATH"),
        "mmproj": sget("captioner.mmproj_path", "HEARTH_CAPTIONER_MMPROJ_PATH"),
        "port": _num(settings, "captioner.port", int, 8091),
        "base_url": os.environ.get("HEARTH_CAPTIONER_BASE_URL", "").strip(),
    }


def _sibling_or_name(bin_path: str, name: str) -> str:
    """Sibling `name` next to bin_path if it exists there, else the bare name (PATH)."""
    if bin_path:
        sib = os.path.join(os.path.dirname(os.path.abspath(bin_path)), name)
        if have(sib):
            return sib
    return name


def _ngl_value(settings: dict) -> str:
    """GPU layers for the captioner, normalised like manager.build_argv does.

    'auto' must mean "omit the flag" — passing the literal word parses as -2 in llama.cpp
    and silently disables GPU tensor overrides. 'all' maps to -1. A number passes through.
    The default is 0 (CPU only) because these models are MoE: they want lots of RAM, not a
    partial VRAM residency that thrashes.
    """
    ngl = str(settings.get("captioner.n_gpu_layers", "0")).strip().lower()
    if ngl in ("", "auto"):
        return ""
    if ngl == "all":
        return "-1"
    try:
        return str(int(float(ngl)))
    except (TypeError, ValueError):
        return "0"


def build_argv(settings: dict) -> list[str]:
    """The llama-server command for the captioner. Raises CaptionError if unlaunchable."""
    p = resolve_paths(settings)
    if not p["bin"]:
        raise CaptionError("no llama-server binary configured for the captioner "
                           "(Admin → Audio → 'Captioner llama-server binary')")
    if not p["model"]:
        raise CaptionError("no captioner model path set (Admin → Audio → 'Captioner model .gguf')")
    if not have(p["bin"]):
        raise CaptionError(f"captioner llama-server binary not found: {p['bin']}")
    if not os.path.isfile(p["model"]):
        raise CaptionError(f"captioner model file not found: {p['model']}")

    a = [p["bin"], "--model", p["model"]]
    if p["mmproj"]:
        if not os.path.isfile(p["mmproj"]):
            raise CaptionError(f"captioner mmproj file not found: {p['mmproj']}")
        # Omni audio needs the multimodal projector; an older build without --mmproj
        # would die on an unknown flag, so probe before adding it.
        if _probe_flag(p["bin"], "--mmproj"):
            a += ["--mmproj", p["mmproj"]]
    ctx = _num(settings, "captioner.n_ctx", int, 8192)
    a += ["--ctx-size", str(max(512, min(ctx, 262144)))]
    ngl = _ngl_value(settings)
    if ngl:
        a += ["--n-gpu-layers", ngl]
    # One slot is enough: captioning is serialised by a lock anyway, and each extra slot
    # would duplicate KV-cache memory for no gain.
    a += ["--parallel", "1"]
    a += ["--host", "127.0.0.1", "--port", str(p["port"])]
    if settings.get("captioner.flash_attn"):
        fa = str(settings["captioner.flash_attn"]).strip().lower()
        if fa in ("on", "off"):
            a += ["--flash-attn", fa]
    threads = _num(settings, "captioner.threads", int, -1)
    if threads > 0:
        a += ["--threads", str(threads)]
    # mmap matters for MoE CPU inference: keep it unless the admin asked otherwise.
    lm = str(settings.get("captioner.load_mode") or "auto").strip().lower()
    if lm and lm != "auto":
        a += ["--load-mode", lm]
    dev = str(settings.get("captioner.device") or "").strip()
    if dev:
        # 'none' is llama.cpp's own spelling for CPU-only on a GPU build.
        a += ["--device", dev]
    extra = str(settings.get("captioner.extra_args") or "").strip()
    if extra:
        try:
            a += shlex.split(extra)
        except ValueError:
            raise CaptionError(f"captioner extra args cannot be parsed: {extra!r}")
    return a


def _probe_flag(bin_path: str, flag: str) -> bool:
    """Does this binary understand `flag`? (via --help, 5 s cap; failure = unsupported)"""
    try:
        out = subprocess.run([bin_path, "--help"], capture_output=True, text=True,
                             timeout=5).stdout
        return flag in out
    except Exception:  # noqa: BLE001 — a probe failure just means "don't add the flag"
        return False


def launch_command_preview(settings: dict) -> dict:
    """The exact command Hearth would start, for copying into a terminal (Admin UI)."""
    try:
        argv = build_argv(settings)
    except CaptionError as e:
        return {"ok": False, "command": "", "note": str(e)}
    return {"ok": True, "command": shlex.join(argv), "argv": argv, "note": ""}


# ---- audio preprocessing -----------------------------------------------------

def is_audio(filename: str, mime: str = "") -> bool:
    """Does this upload look like something the captioner can listen to?

    Extension-first (reliable, no file read); MIME only as a fallback for a nameless
    upload. Note that .mp4/.webm pass here because they often carry the audio people want
    described — a caller dealing with video should check has_audio_stream() too.
    """
    name = str(filename or "").lower()
    if name.endswith(AUDIO_EXTS):
        return True
    m = str(mime or "").lower()
    return m.startswith("audio/")


def audio_info(abs_path: str) -> dict:
    """Duration / sample rate / channel / audio-stream count via ffprobe.

    Never raises: without ffprobe we report {} plus a `note`, because the caller has a
    perfectly good file to send and ffmpeg is only a convenience here.
    """
    if not have("ffprobe"):
        return {"note": "ffprobe not installed — duration unknown"}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
             "format=duration:stream=codec_type,codec_name,sample_rate,channels", abs_path],
            capture_output=True, text=True, timeout=45)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"note": f"ffprobe failed: {e}"}
    if r.returncode != 0 or not (r.stdout or "").strip():
        return {"note": "ffprobe returned nothing for this file"}
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return {"note": "ffprobe output was not valid JSON"}
    astreams = [s for s in (d.get("streams") or []) if s.get("codec_type") == "audio"]
    fmt = d.get("format") or {}
    dur = 0.0
    try:
        dur = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    first = astreams[0] if astreams else {}
    return {
        "duration_s": dur,
        "audio_streams": len(astreams),
        "codec": first.get("codec_name") or "",
        "sample_rate": first.get("sample_rate") or "",
        "channels": first.get("channels") or "",
    }


def has_audio_stream(path: str) -> bool:
    """Whether a container actually carries audio.

    Only meaningful when ffprobe answered; with no ffprobe we assume yes rather than
    refusing to try (a silent refusal is worse than one wasted caption attempt)."""
    info = audio_info(path)
    if "audio_streams" not in info:
        return True
    return int(info.get("audio_streams") or 0) > 0


def preflight(settings: dict) -> str:
    """Return a reason the captioner CANNOT run, or "" when it plausibly can.

    Checked cheaply and synchronously so a tool call fails immediately with an actionable
    message instead of after ffmpeg has churned through a file. Deliberately does not probe
    the network — reachability is the launcher's business and belongs in status().

    Order matters: a missing model file outranks everything (it is the one thing that must
    exist for either backend), then the backend-specific requirements.
    """
    p = resolve_paths(settings)
    if not p["enabled"]:
        return "the audio captioner is disabled (Admin → Audio)"
    if not p["model"]:
        return "no captioner model path is set (Admin → Audio → 'Captioner model (.gguf) path')"
    if not os.path.isfile(p["model"]):
        return f"captioner model file not found on the server: {p['model']}"
    if p["backend"] == "cli":
        if not have(p["cli_bin"]):
            return f"llama-mtmd-cli not found at '{p['cli_bin']}' (Admin → Audio → " \
                   f"'Captioner llama-mtmd-cli path')"
        if not p["mmproj"]:
            return "the CLI backend needs a projector: set 'Captioner mmproj path' " \
                   "(Admin → Audio)"
        if not os.path.isfile(p["mmproj"]):
            return f"captioner mmproj file not found: {p['mmproj']}"
    else:
        if not p["bin"]:
            return "no captioner llama-server binary is set (Admin → Audio)"
        if not have(p["bin"]):
            return f"captioner llama-server binary not found: {p['bin']}"
    return ""


def audio_info_for_workspace(user_id: str, rel_path: str) -> dict:
    """audio_info() over a jail-relative path, via workspaces (encryption at rest aware).

    ffprobe is a separate process and must never be handed ciphertext, so the bytes are read
    through workspaces.read_bytes() and staged in a private temp file. Returns {} plus a
    `note` on any read failure — callers report it, they do not crash.
    """
    from . import workspaces as ws
    try:
        data = ws.read_bytes(user_id, rel_path)
    except ws.JailEscape as e:
        return {"note": f"path refused: {e}"}
    except FileNotFoundError:
        return {"note": "file not found"}
    except OSError as e:
        return {"note": f"could not read the file: {e}"}
    if not data:
        return {"note": "file is empty"}
    suffix = os.path.splitext(rel_path)[1] or ".bin"
    with tempfile.TemporaryDirectory(prefix="hearth-ainfo-") as tmp:
        staged = os.path.join(tmp, f"input{suffix}")
        try:
            with open(staged, "wb") as fh:
                fh.write(data)
        except OSError as e:
            return {"note": f"could not stage the file: {e}"}
        return audio_info(staged)


def _clamp_chunk(chunk_seconds) -> int:
    """Chunk length clamped to [5, 900] seconds; 0/None means 'do not split'.

    A single clamp point matters because the same value is used for three things — the
    ffmpeg segment_time, the per-chunk offset labels, and the header text shown to the
    model — and they must agree. Values below 5 s would shred a recording into pieces too
    short to caption meaningfully, so they are raised rather than obeyed.
    """
    try:
        n = int(chunk_seconds)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return max(5, min(n, _MAX_CHUNK_SECONDS))


def prepare_chunks(abs_path: str, tmpdir: str, *, resample: bool, chunk_seconds: int,
                   window: tuple[float, float] | None = None,
                   log=lambda m: None) -> list[tuple[str, float]]:
    """Convert/split the input into caption-sized WAV chunks: [(path, start_offset_s)].

    16 kHz mono PCM is what audio encoders expect, and chunking bounds both the captioner's
    context and how long one request can stall a turn. Without ffmpeg the original file is
    passed through untouched (the harness behaviour) and the caller says so — sending an
    unmodified file is usually fine for llama.cpp, which decodes common containers itself.

    `window` = (start_s, end_s) cuts the input to that span FIRST, then chunks within it —
    this is how a video's audio gets described for exactly the section whose frames are being
    shown (DEC-064). The returned offsets stay ABSOLUTE: chunk 0 of a window starting at 90 s
    reports 90.0, so every caption label quotes a timestamp that means something in the
    original file rather than in the excerpt. Offsets come from `step` plus the window start,
    never from the chunk index alone.

    A window with no ffmpeg cannot be honoured, so it is refused loudly rather than answered
    with a description of the whole file — silently captioning more than was asked for is
    worse than failing, because the caller (and the user reading the chat) would believe they
    got just that segment.
    """
    ff = shutil.which("ffmpeg")
    lo, hi = (float(window[0]), float(window[1])) if window else (None, None)
    if window and (hi is None or hi <= lo or lo < 0):
        raise CaptionError(f"invalid audio window {window}")
    if not ff:
        if window:
            raise CaptionError("cutting audio to a time window needs ffmpeg "
                               "(the segment could not be described for just that part)")
        if resample or chunk_seconds:
            log("ffmpeg not found — sending the file as-is (no resample/chunking)")
        return [(abs_path, 0.0)]

    do_chunk = _clamp_chunk(chunk_seconds) > 0
    step = _clamp_chunk(chunk_seconds)
    out_pattern = os.path.join(tmpdir, "chunk_%03d.wav")
    cmd = [ff, "-y", "-loglevel", "error"]
    # -ss BEFORE -i seeks by keyframe (cheap); -t then bounds the read. Both go before the
    # input so ffmpeg never decodes the part of a long video nobody asked about — that is
    # the whole reason this parameter exists.
    if lo:
        cmd += ["-ss", f"{lo:.3f}"]
    if hi is not None:
        span = hi - (lo or 0.0)
        cmd += ["-t", f"{max(0.05, span):.3f}"]
    cmd += ["-i", abs_path, "-vn"]
    if resample:
        cmd += ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "pcm_s16le"]
    if do_chunk:
        cmd += ["-f", "segment", "-segment_time", str(step), "-reset_timestamps", "1"]
        cmd.append(out_pattern)
    else:
        # A literal filename, not the %03d pattern — ffmpeg would otherwise create a file
        # actually named "chunk_%03d.wav".
        cmd.append(out_pattern % 0)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise CaptionError("ffmpeg timed out converting the audio (over 15 min)")
    except OSError as e:
        raise CaptionError(f"could not run ffmpeg: {e}")
    if r.returncode != 0:
        detail = (r.stderr or "").strip()[:400] or f"ffmpeg exited {r.returncode}"
        raise CaptionError(f"could not decode this audio file: {detail}")
    files = sorted(Path(tmpdir).glob("chunk_*.wav"))
    if not files:
        raise CaptionError("produced no audio from this input (is it empty or unsupported?)")
    # Absolute offsets: the window start is added so a caption for a clip cut at 90 s says
    # "[1:30 - ...]" and not "[0:00 - ...]". Without this the model quotes timestamps that
    # point at the wrong moment of the original file.
    base = float(lo or 0.0)
    return [(str(f), round(base + i * step, 2)) for i, f in enumerate(files)]


# ---- the captioner process / HTTP client ------------------------------------

class Captioner:
    """Owns one captioner llama-server (or none, in cli mode) and runs captions.

    A single global instance (get_captioner()). All entry points funnel through
    caption_sync(), which holds a lock: two family members asking at once would
    otherwise interleave on one --parallel 1 slot, and a MoE model decoding two
    jobs concurrently is slower than the two of them in series.
    """

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._log_path = paths.RUNTIME_DIR / "captioner.log"
        self._lock = threading.Lock()
        self._last_hint = ""
        # The llama-server child process of the caption request now in flight (server
        # backend only, and only a Hearth-owned one). A user-facing Stop interrupts the
        # HTTP read cooperatively (see _run_server), which does NOT stop llama.cpp from
        # decoding the rest of its slot — it keeps burning CPU/GPU on a caption nobody is
        # waiting for any more. stop_active() kills THIS request's process so "Stop" really
        # stops the audio model, not just the chat. Kept per-request rather than as
        # self._proc because the admin Stop/start buttons also own self._proc; touching that
        # from a cancelled caption would let one user's abort tear down a server someone else
        # is using.
        self._active_proc: subprocess.Popen | None = None
        # A Stop can arrive while caption_sync is still BLOCKED acquiring self._lock — i.e.
        # before _run_server has recorded _active_proc, so there would be nothing to kill and
        # the audio model would keep decoding (the exact "captioner still running after Stop"
        # complaint). This flag lets stop_active() record that intent even with no process yet;
        # _set_active honours it the instant a process is recorded. Guarded by _lock_state so a
        # Stop on one thread and the caption worker on another cannot race.
        self._stop_requested = False
        self._lock_state = threading.Lock()

    def stop_active(self) -> bool:
        """Terminate the caption request in flight, if any; otherwise record the intent.

        Only ever touches a Hearth-owned child process tied to the current caption (never an
        externally-run HEARTH_CAPTIONER_BASE_URL server). Leaves self._proc pointing at the
        same process so status() reflects reality: after this the captioner shows as stopped,
        and the next request relaunches it. Safe to call when nothing is running.

        Returns True if a live process was terminated. When no caption process exists yet —
        e.g. Stop pressed while caption_sync waits on the lock — this still sets the kill flag
        (so the very next process is torn down) and returns False.
        """
        with self._lock_state:
            self._stop_requested = True
            p = self._active_proc
        if p is None or p.poll() is not None:
            return False
        try:
            p.terminate()
            for _ in range(20):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if p.poll() is None:
                p.kill()
        except OSError:
            return False
        return True

    def clear_stop(self) -> None:
        """Forget a pending kill request (called when a fresh caption begins).

        Without this, one Stop could tear down the NEXT user's caption: the flag would still
        be set from an earlier abort and _set_active would kill an unrelated request. Cleared at
        the top of caption_sync so the intent applies only to the request that was running (or
        about to run) when Stop happened.
        """
        with self._lock_state:
            self._stop_requested = False

    def _set_active(self, proc):
        """Record (or clear, with None) the process backing the in-flight caption.

        If a Stop already arrived while no process was recorded, honour it now by killing this
        one — that is the path where Stop landed mid-lock-wait and there was nothing to kill yet.
        """
        self._active_proc = proc
        if proc is not None:
            with self._lock_state:
                pending = self._stop_requested
            if pending:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        for _ in range(20):
                            if proc.poll() is not None:
                                break
                            time.sleep(0.1)
                        if proc.poll() is None:
                            proc.kill()
                except OSError:
                    pass


    # -- lifecycle ------------------------------------------------------------
    def _base_url(self, settings: dict) -> str:
        p = resolve_paths(settings)
        if p["base_url"]:
            return p["base_url"].rstrip("/")
        return f"http://127.0.0.1:{p['port']}"

    def _owned(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    async def ensure_server(self, settings: dict, force: bool = False) -> str:
        """Return a reachable captioner base URL, launching one if needed ('' on failure).

        Mirrors manager.ensure_server's discipline: liveness is judged by /props as well as
        /health, because a process that answers HTTP but not the llama-server API is a wedged
        occupant and bind-fighting it just produces a second dead listener. An externally
        provided base URL (HEARTH_CAPTIONER_BASE_URL) is never launched or killed.
        """
        p = resolve_paths(settings)
        external = bool(p["base_url"])
        base = self._base_url(settings)

        if not await self._reachable(base):
            pass  # nothing there: fall through to launching ours
        elif await self._is_llama_server(base):
            return base
        else:
            # Live-but-wedged occupant: report it instead of bind-fighting it. Quote the
            # port with a temp variable — nesting p['port'] inside this f-string needs
            # backslashes on older Pythons, and readability beats cleverness here.
            port = p["port"]
            self._last_hint = (f"Something occupies port {port} and answers HTTP but not the "
                               f"llama-server API — likely a wedged old captioner. Find it with "
                               f"'ss -tlnp | grep :{port}', kill it, then Start again.")
            return ""

        if force and external:
            self._last_hint = (f"{base} is HEARTH_CAPTIONER_BASE_URL — a server Hearth does not "
                               f"own. Restart it yourself.")
            return ""
        if force:
            await self.stop_server()

        try:
            argv = build_argv(settings)
        except CaptionError as e:
            self._last_hint = str(e)
            return ""

        try:
            paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            logf = open(self._log_path, "ab")
            self._proc = subprocess.Popen(argv, stdout=logf, stderr=logf,
                                          stdin=subprocess.DEVNULL)
        except OSError as e:
            self._last_hint = f"could not launch the captioner server: {e}"
            return ""

        timeout = max(30.0, float(_num(settings, "captioner.startup_timeout_s", int, 300)))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self._is_llama_server(base):
                return base
            if self._proc.poll() is not None:
                break
            await asyncio.sleep(0.5)
        rc = self._proc.poll() if self._proc else None
        tail = self.read_log_tail(1200)
        self._last_hint = (
            f"the captioner server did not become ready within {int(timeout)} s"
            + (f" (it exited with code {rc})" if rc is not None else "")
            + f". Its output is in {self._log_path}"
            + (f"; last lines: {tail}" if tail else "")
            + " — a MoE model needs enough RAM; try fewer GPU layers or a smaller context.")
        return ""

    async def start(self, settings: dict | None = None, force: bool = False) -> dict:
        """Start (or restart with force=True) the captioner and report status."""
        settings = settings if settings is not None else rs.get_all()
        self._last_hint = ""
        p = resolve_paths(settings)
        st = await self.status(settings)
        if p["backend"] == "cli":
            st["started"] = False
            st["hint"] = ("Backend is 'cli', so there is no server to start — each caption runs "
                          "llama-mtmd-cli on its own. Switch Backend to 'server' to keep the "
                          "model loaded between captions.")
            return st
        base = await self.ensure_server(settings, force=force)
        st = await self.status(settings)
        st["started"] = bool(base)
        if not base and not st.get("hint"):
            st["hint"] = self._last_hint or "could not reach the captioner server"
        elif not base:
            st["hint"] = self._last_hint or st.get("hint")
        return st

    async def stop_server(self) -> dict:
        """Stop a captioner server Hearth launched. An external one is left alone."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                for _ in range(40):
                    if self._proc.poll() is not None:
                        break
                    await asyncio.sleep(0.25)
                if self._proc.poll() is None:
                    self._proc.kill()
            except OSError:
                pass
        self._proc = None
        st = await self.status()
        st["stopped"] = True
        return st

    async def _reachable(self, base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
                r = await c.get(f"{base_url}/health")
                return r.status_code == 200
        except Exception:  # noqa: BLE001 — unreachable is the answer, not an error
            return False

    @staticmethod
    async def _is_llama_server(base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
                p = await c.get(f"{base_url}/props")
                if p.status_code != 200:
                    return False
                j = p.json()
                return isinstance(j, dict) and bool(
                    "model_path" in j or "total_slots" in j
                    or "default_generation_settings" in j)
        except Exception:  # noqa: BLE001
            return False

    def read_log_tail(self, max_bytes: int = 2000) -> str:
        """Last bytes of the captioner log, one line, for embedding in error hints."""
        try:
            size = os.path.getsize(self._log_path)
            with open(self._log_path, "rb") as f:
                f.seek(max(0, size - max(256, int(max_bytes))))
                data = f.read()
            text = " ".join(data.decode("utf-8", "replace").split())
            return text[-600:]
        except OSError:
            return ""

    # -- status ---------------------------------------------------------------
    async def status(self, settings: dict | None = None) -> dict:
        """Everything the admin UI shows about this second model. Never raises."""
        settings = settings if settings is not None else rs.get_all()
        p = resolve_paths(settings)
        base = self._base_url(settings)
        reachable = await self._reachable(base)
        configured = bool(p["bin"] and p["model"])
        props: dict = {}
        model_name = ""
        if reachable:
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
                    r = await c.get(f"{base}/props")
                    if r.status_code == 200 and isinstance(r.json(), dict):
                        props = r.json()
                        model_name = str(props.get("model_path") or "")
            except Exception:  # noqa: BLE001 — a badge must never break the page
                pass
        return {
            "kind": "captioner",
            "enabled": p["enabled"],
            "backend": p["backend"],
            "reachable": reachable,
            "base_url": base if not p["base_url"] else p["base_url"],
            "proc_running": self._owned(),
            "managed_by_hearth": not bool(p["base_url"]),
            "configured": configured,
            "model_path": p["model"],
            "mmproj_path": p["mmproj"],
            # What is actually loaded, from the live server — not what Admin says.
            "loaded_model": model_name,
            "n_ctx": props.get("n_ctx") or None,
            "cpu_only": _ngl_value(settings) in ("0", ""),
            "paths": {"bin": p["bin"], "model": p["model"], "mmproj": p["mmproj"]},
            "capabilities": {"ffmpeg": have("ffmpeg"), "ffprobe": have("ffprobe"),
                             "cli_bin": p["cli_bin"], "cli_present": have(p["cli_bin"])},
            "hint": self._last_hint if not reachable else "",
        }

    # -- captioning -----------------------------------------------------------
    def caption_sync(self, audio_path: str, settings: dict | None = None,
                     extra_prompt: str = "", log=lambda m: None,
                     on_delta=None, cancel=None,
                     window: tuple[float, float] | None = None) -> dict:
        """Describe one audio file. SYNCHRONOUS — call via asyncio.to_thread from async code.

        Returns {"caption": str, "chunks": int, "duration_s": float|None,
                 "backend": str, "notes": [str]}. Raises CaptionError with a message the
        main model can repeat verbatim. Serialized by self._lock (see class docstring).

        `on_delta(text)` is called with each streamed piece of the description as it arrives
        (server backend), so the caller can show live progress. `cancel` is a cancel.Event-like
        object checked between chunks; when set, captioning raises rather than starting the
        next chunk. Both are optional — existing callers pass neither and behave unchanged.

        `window` = (start_s, end_s) describes only that span, which is how `view_video` pairs
        a caption with the frames it shows (DEC-064). The reported `duration_s` is then the
        WINDOW's length, not the file's — a caller that wants the runtime of the whole clip
        asks ffprobe for it separately, and conflating the two would make chunk labels lie.
        """
        settings = settings if settings is not None else rs.get_all()
        p = resolve_paths(settings)
        if not p["enabled"]:
            raise CaptionError("the audio captioner is disabled — an admin can enable it under "
                               "Admin → Audio")
        # This request begins now, so a Stop from some EARLIER turn must not tear it down.
        self.clear_stop()

        # Check the backend's own prerequisites BEFORE touching ffmpeg: a missing CLI or
        # projector is a configuration fault, and reporting "could not decode this audio file"
        # for it would send the user hunting through their recordings instead of Admin.
        problem = preflight(settings)
        if problem:
            raise CaptionError(problem)

        chunk_seconds = _clamp_chunk(_num(settings, "captioner.chunk_seconds", int, 30))
        resample = _bool(settings, "captioner.resample", True)
        info = audio_info(audio_path)
        duration = info.get("duration_s") or None
        if duration == 0.0:
            duration = None
        # Inside a window the excerpt is what was listened to, so it is the honest number for
        # chunk-count maths and for the header text; the file's own runtime stays in `info`.
        if window:
            duration = max(0.05, float(window[1]) - float(window[0]))

        with self._lock:
            notes: list[str] = []
            with tempfile.TemporaryDirectory(prefix="hearth-audio-") as tmp:
                chunks = prepare_chunks(audio_path, tmp, resample=resample,
                                        chunk_seconds=chunk_seconds, window=window, log=log)
                if not info.get("audio_streams") and "audio_streams" in info:
                    notes.append("ffprobe reported no audio stream — the description below may "
                                 "be of silence")
                backend = p["backend"]
                parts: list[str] = []
                for i, (path_, start) in enumerate(chunks):
                    # Between-chunk cancel: a Stop must not just abandon THIS chunk but stop
                    # the whole recording from being listened to further.
                    if _cancel_requested(cancel):
                        raise CaptionError("captioning was stopped")
                    log(f"captioning chunk {i + 1}/{len(chunks)} "
                        f"(from {fmt_time(start)})")
                    if backend == "cli":
                        text = self._run_cli(path_, settings, p, extra_prompt, log)
                    else:
                        text = self._run_server(path_, settings, p, extra_prompt,
                                                on_delta=on_delta, cancel=cancel)
                        if text is None:     # server unusable -> documented fallback
                            notes.append(f"captioner server unavailable for chunk {i + 1}; used "
                                         f"llama-mtmd-cli instead")
                            text = self._run_cli(path_, settings, p, extra_prompt, log)
                    if not text.strip():
                        notes.append(f"chunk {i + 1} (from {fmt_time(start)}) produced no "
                                     f"description")
                        continue
                    if len(chunks) > 1:
                        parts.append(f"[{fmt_time(start)} - {fmt_time(start + chunk_seconds)}] "
                                     f"(chunk {i + 1}/{len(chunks)})\n{text.strip()}")
                    elif window:
                        # A single chunk of a window still needs its span labelled, or the
                        # model cannot tell this excerpt apart from a description of the
                        # whole clip and may generalise a 20-second caption to all ten minutes.
                        parts.append(f"[{fmt_time(window[0])} - {fmt_time(window[1])}]\n"
                                     f"{text.strip()}")
                    else:
                        parts.append(text.strip())
            # This caption is done; forget which process served it so a later Stop cannot tear
            # down an idle server the next request happens to be using.
            self._set_active(None)
        caption = "\n\n".join(parts).strip()

        if not caption:
            raise CaptionError("the captioner model returned an empty description for this audio")
        return {"caption": caption, "chunks": len(chunks), "duration_s": duration,
                "backend": p["backend"], "notes": notes}

    # -- backends -------------------------------------------------------------
    def _run_server(self, wav: str, settings: dict, p: dict, extra_prompt: str,
                    on_delta=None, cancel=None) -> str | None:
        """POST /v1/chat/completions with an `input_audio` part. None => fall back to CLI.

        STREAMS by default (DEC-057): the caption arrives token by token and each piece is
        handed to ``on_delta`` so the chat can show the audio model working, exactly like the
        chat model's reasoning. A caption is a slow block of text — without live output the
        wait reads as a hang even with a "listening…" label. When ``on_delta`` is None (the
        admin test endpoint) it still streams but reports nothing; pass
        ``captioner.stream=false`` to fall back to one non-streamed read, which keeps simple,
        status-code-based error handling for debugging.

        Cancellation: ``cancel`` is a cancel.Event-like object (see hearth/cancel.py adapted
        for threads). When set, the read loop raises so the request unwinds immediately; the
        in-flight Hearth-owned child process is also terminated (stop_active) so llama.cpp
        does not keep decoding a caption nobody wants. A mid-stream stop is NOT retried via
        the CLI fallback — that would undo the abort — it surfaces as CaptionError.
        """
        base = self._base_url(settings)
        if not self._owned() and not p["base_url"]:
            # Nobody is listening and we do not own a process: try to bring one up. This is
            # a sync path, so "bring up" means launch + poll without an event loop.
            if not self._ensure_server_sync(settings, p):
                return None
        # Remember which server this request is talking to so a Stop can kill it. Only when
        # Hearth owns the process — never adopt an externally-run base_url server.
        if self._owned() and not p["base_url"]:
            self._set_active(self._proc)
        stream = _bool(settings, "captioner.stream", True)
        fmt = Path(wav).suffix.lstrip(".").lower() or "wav"
        try:
            b64 = base64.b64encode(Path(wav).read_bytes()).decode()
        except OSError as e:
            raise CaptionError(f"could not read the prepared audio chunk: {e}")
        content: list[dict] = [
            {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}]
        prompt = (extra_prompt or "").strip() or str(
            settings.get("captioner.prompt") or DEFAULT_PROMPT).strip()
        if prompt:
            content.append({"type": "text", "text": prompt})
        body = {
            "messages": [{"role": "user", "content": content}],
            "stream": bool(stream),
            "temperature": _num(settings, "captioner.temperature", float, 0.6),
            "top_p": _num(settings, "captioner.top_p", float, 0.95),
            "max_tokens": int(_num(settings, "captioner.max_tokens", int, 1024)),
        }
        top_k = _num(settings, "captioner.top_k", int, 20)
        if top_k > 0:
            body["top_k"] = top_k
        seed = _num(settings, "captioner.seed", int, -1)
        if seed >= 0:
            body["seed"] = seed
        timeout = float(_num(settings, "captioner.request_timeout_s", int, 600))

        if not stream:
            return self._run_server_once(base, body, timeout)

        # Streaming. Timeout units matter: httpx's scalar is a TOTAL including body read, so
        # it would kill a long caption regardless of progress (the exact ReadError trap noted
        # for the chat provider). Cap only connect; leave the streamed read unbounded — Stop
        # and the model's own max_tokens are what end it.
        to = httpx.Timeout(connect=min(15.0, timeout) if timeout > 0 else 15.0,
                           read=None, write=timeout or None, pool=timeout or None)
        pieces: list[str] = []

        def _emit(text: str) -> None:
            if not text:
                return
            pieces.append(text)
            if on_delta is not None:
                try:
                    on_delta(text)
                except Exception:  # noqa: BLE001 — progress must never break a caption
                    pass

        aborted = False
        try:
            with httpx.Client(timeout=to, trust_env=False) as c:
                with c.stream("POST", f"{base}/v1/chat/completions", json=body) as r:
                    if r.status_code >= 400:
                        detail = (r.read() or b"").decode("utf-8", "replace")[:400]
                        # A build without audio support says so in its own words — surface it
                        # rather than pretending the file was bad, and let the CLI path try.
                        if "input_audio" in detail.lower() or "audio" in detail.lower():
                            raise CaptionError(
                                f"the captioner server rejected the audio part (HTTP {r.status_code}): "
                                f"{detail} — this usually means the loaded model has no audio encoder, or "
                                f"its mmproj projector is missing/mismatched. Check Admin → Audio and "
                                f"{self._log_path}")
                        raise CaptionError(f"captioner server error HTTP {r.status_code}: {detail}")
                    for line in r.iter_lines():
                        if _cancel_requested(cancel):
                            aborted = True
                            break
                        if not line:
                            continue
                        # SSE lines are prefixed "data:"; anything else (event:, id:, comments,
                        # or a plain-JSON body from a server that ignored stream:true) is skipped.
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except ValueError:
                            continue
                        for ch in (obj.get("choices") or []):
                            delta = ch.get("delta") or {}
                            # A captioner with thinking enabled streams its thoughts on a
                            # separate field; show those too (same as the chat reasoning path).
                            rc = delta.get("reasoning_content") or delta.get("reasoning")
                            if rc:
                                _emit(rc)
                            piece = delta.get("content")
                            if isinstance(piece, str):
                                _emit(piece)
        except CaptionError:
            # A server-side rejection with a specific reason: let caption_sync decide the CLI
            # fallback (it only fires when this returns None), but never on an abort.
            if aborted:
                self.stop_active()
            raise
        except (httpx.TimeoutException, httpx.TransportError, OSError) as e:
            # Mid-stream failure. If the user stopped us, honour it — do NOT fall back to the
            # CLI and keep listening. Otherwise this is a genuine transport error: fall back
            # to CLI by returning None (the documented behaviour), but only if nothing was
            # streamed yet so no partial caption is doubled.
            if aborted:
                self.stop_active()
                raise CaptionError("captioning was stopped before the description finished")
            if pieces:
                raise CaptionError(
                    f"captioner stream failed partway through ({type(e).__name__}: {e}) — "
                    f"server-side audio decoding can be slow; raise 'Captioner request timeout' "
                    f"or lower 'Chunk length'")
            return None
        if aborted:
            self.stop_active()
            raise CaptionError("captioning was stopped before the description finished")
        text = "".join(pieces)
        if not text.strip():
            # Nothing usable came back on the stream (a server that ignored stream:true, or a
            # build that only fills message.content). Retry once WITHOUT streaming so those
            # builds still produce a caption.
            return self._run_server_once(base, dict(body, stream=False), timeout)
        return text

    def _run_server_once(self, base: str, body: dict, timeout: float) -> str:
        """One NON-streamed /v1/chat/completions read; returns the assistant text.

        The fallback for captioner.stream=false and for servers that ignore stream:true. Same
        error attribution as the streaming path so a missing audio encoder is never blamed on
        the user's file.
        """
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as c:
                r = c.post(f"{base}/v1/chat/completions", json=body)
        except (httpx.TimeoutException, httpx.TransportError, OSError) as e:
            raise CaptionError(f"captioner server request failed: {type(e).__name__}: {e} "
                               f"(server-side audio decoding can be slow; raise 'Captioner "
                               f"request timeout' or lower 'Chunk length')")
        if r.status_code >= 400:
            detail = (r.text or "")[:400]
            if "input_audio" in detail.lower() or "audio" in detail.lower():
                raise CaptionError(
                    f"the captioner server rejected the audio part (HTTP {r.status_code}): "
                    f"{detail} — this usually means the loaded model has no audio encoder, or "
                    f"its mmproj projector is missing/mismatched. Check Admin → Audio and "
                    f"{self._log_path}")
            raise CaptionError(f"captioner server error HTTP {r.status_code}: {detail}")
        try:
            j = r.json()
        except ValueError:
            raise CaptionError("captioner server returned a response that was not JSON")
        try:
            msg = (j.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content")
            if not isinstance(text, str):
                # Some builds put the answer in reasoning_content when thinking is enabled.
                text = msg.get("reasoning_content") or ""
            return str(text or "")
        except (AttributeError, IndexError, TypeError):
            raise CaptionError(f"unexpected captioner response shape: {str(j)[:200]}")

    def _ensure_server_sync(self, settings: dict, p: dict) -> bool:
        """Launch + wait for readiness WITHOUT an event loop.

        Needed because caption_sync() runs in a worker thread (no running loop), while the
        async ensure_server() is what admin buttons use. Deliberately minimal: launch, poll
        /props with a blocking sleep, report. Any failure leaves a hint, returns False.
        """
        base = self._base_url(settings)
        if self._owned():
            try:
                with httpx.Client(timeout=3, trust_env=False) as c:
                    return c.get(f"{base}/props").status_code == 200
            except Exception:  # noqa: BLE001
                return False
        try:
            argv = build_argv(settings)
        except CaptionError as e:
            self._last_hint = str(e)
            return False
        try:
            paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "ab") as logf:
                self._proc = subprocess.Popen(argv, stdout=logf, stderr=logf,
                                              stdin=subprocess.DEVNULL)
                # This request brought the server up, so it is the process a Stop should be
                # able to tear down if the user aborts mid-caption. _run_server re-records it
                # after the readiness poll (covers an already-running owned server too).
                self._set_active(self._proc)
        except OSError as e:
            self._last_hint = f"could not launch the captioner server: {e}"
            return False
        deadline = time.time() + max(30.0, float(_num(settings, "captioner.startup_timeout_s",
                                                       int, 300)))
        while time.time() < deadline:
            try:
                with httpx.Client(timeout=3, trust_env=False) as c:
                    if c.get(f"{base}/props").status_code == 200:
                        return True
            except Exception:  # noqa: BLE001 — keep polling until the deadline
                pass
            if self._proc.poll() is not None:
                break
            time.sleep(0.5)
        tail = self.read_log_tail(1200)
        self._last_hint = ("the captioner server did not come up in time"
                           + (f"; last log lines: {tail}" if tail else ""))
        return False

    def _run_cli(self, wav: str, settings: dict, p: dict, extra_prompt: str,
                 log=lambda m: None) -> str:
        """One llama-mtmd-cli process for one chunk (the no-server fallback)."""
        cli = p["cli_bin"]
        if not have(cli):
            raise CaptionError(
                f"no captioner server is reachable and the CLI fallback binary "
                f"({cli}) is not installed. Point 'Captioner llama-mtmd-cli path' at a build "
                f"of llama-mtmd-cli, or start the captioner server (Admin → Audio).")
        if not os.path.isfile(p["mmproj"]):
            raise CaptionError(
                f"llama-mtmd-cli needs the multimodal projector: set 'Captioner mmproj path' "
                f"in Admin → Audio to the mmproj-*.gguf that pairs with "
                f"{os.path.basename(p['model'])} (expected: {p['mmproj'] or 'not set'})")
        prompt = (extra_prompt or "").strip() or str(
            settings.get("captioner.prompt") or DEFAULT_PROMPT).strip()
        cmd = [cli, "-m", p["model"], "--mmproj", p["mmproj"], "--audio", wav,
               "--temp", str(_num(settings, "captioner.temperature", float, 0.6)),
               "--top-p", str(_num(settings, "captioner.top_p", float, 0.95)),
               "-n", str(int(_num(settings, "captioner.max_tokens", int, 1024))),
               "-c", str(int(_num(settings, "captioner.n_ctx", int, 8192)))]
        ngl = _ngl_value(settings)
        if ngl:
            cmd += ["-ngl", ngl]
        top_k = _num(settings, "captioner.top_k", int, 20)
        if top_k > 0:
            cmd += ["--top-k", str(top_k)]
        seed = _num(settings, "captioner.seed", int, -1)
        if seed >= 0:
            cmd += ["-s", str(seed)]
        if prompt:
            cmd += ["-p", prompt]
        extra = str(settings.get("captioner.extra_args") or "").strip()
        if extra:
            try:
                cmd += shlex.split(extra)
            except ValueError:
                raise CaptionError(f"captioner extra args cannot be parsed: {extra!r}")
        log("$ " + shlex.join(cmd))
        timeout = float(_num(settings, "captioner.request_timeout_s", int, 600))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise CaptionError(f"llama-mtmd-cli timed out after {int(timeout)} s on one chunk")
        except OSError as e:
            raise CaptionError(f"could not run {cli}: {e}")
        if r.returncode != 0:
            detail = (r.stderr or "").strip()[-400:] or f"exit code {r.returncode}"
            raise CaptionError(f"llama-mtmd-cli failed: {detail} — check that the model and "
                               f"mmproj match and that this build supports --audio")
        return r.stdout or ""


# ---- async entry points ------------------------------------------------------

def get_captioner() -> Captioner:
    """The process-wide captioner instance."""
    global _captioner
    if _captioner is None:
        _captioner = Captioner()
    return _captioner


_captioner: Captioner | None = None


async def describe_file(user_id: str, rel_path: str, extra_prompt: str = "",
                        settings: dict | None = None, on_delta=None, cancel=None,
                        window: tuple[float, float] | None = None) -> dict:
    """Caption a workspace file by jail-relative path. Raises CaptionError/CaptionSkip.

    Reads the bytes through workspaces so encryption at rest is honoured, then stages them
    in a private temp dir — an external ffmpeg/llama process must never be handed
    ciphertext, and nothing extra is written into the user's jail.

    `on_delta(text)` streams the description as it is produced (see caption_sync); `cancel`
    is a cancel.Event-like object checked between chunks so a Stop abandons the recording
    instead of listening to the end of it.

    `window` = (start_s, end_s) captions only that span (DEC-064, used by `view_video`). The
    size cap is NOT applied to a windowed request: it exists to stop one model call from
    swallowing an hour of audio, and a 30-second excerpt of a large .mp4 is well inside its
    spirit even though the container on disk is big. ffprobe still bounds what can be asked
    for (see `max_window_seconds`), so the clamp cannot simply be removed.
    """
    from . import workspaces as ws
    settings = settings if settings is not None else rs.get_all()
    try:
        data = ws.read_bytes(user_id, rel_path)
    except ws.JailEscape as e:
        raise CaptionError(f"path refused: {e}")
    except FileNotFoundError:
        raise CaptionError(f"file not found in the workspace: {rel_path}")
    except OSError as e:
        raise CaptionError(f"could not read {rel_path}: {e}")
    if not data:
        raise CaptionError(f"{rel_path} is empty")
    cap_bytes = int(_num(settings, "captioner.max_bytes", int, 64 * 1024 * 1024))
    if len(data) > cap_bytes and not window:
        raise CaptionError(f"{rel_path} is {len(data) // 1024 // 1024} MB, over the captioner's "
                           f"{cap_bytes // 1024 // 1024} MB limit (Admin → Audio → 'Maximum "
                           f"audio size')")
    suffix = os.path.splitext(rel_path)[1] or ".wav"
    with tempfile.TemporaryDirectory(prefix="hearth-cap-") as tmp:
        staged = os.path.join(tmp, f"input{suffix}")
        try:
            with open(staged, "wb") as fh:
                fh.write(data)
        except OSError as e:
            raise CaptionError(f"could not stage the audio for captioning: {e}")
        cap = get_captioner()
        return await asyncio.to_thread(
            lambda: cap.caption_sync(staged, settings, extra_prompt, on_delta=on_delta,
                                     cancel=cancel, window=window))


class CaptionSkip(Exception):
    """Not a failure — this file simply is not something to caption.

    Kept separate from CaptionError so callers never show an error card for, say, a video
    with no audio track or a disabled captioner.
    """


def route_candidate(settings: dict, filename: str, mime: str = "") -> bool:
    """Should an attachment be auto-routed to the captioner?

    Cheap and synchronous (extension + settings only, no file read), so the chat route can
    ask it before deciding whether a turn may take a few seconds longer.
    """
    if not _bool(settings, "captioner.enabled", False):
        return False
    if not _bool(settings, "captioner.auto_route", True):
        return False
    return is_audio(filename, mime)
