"""Image generation through stable-diffusion.cpp — an optional external binary.

Hearth's chat model cannot draw. This module lets it *ask* for a picture by running
`sd-cli` (from stable-diffusion.cpp) as a subprocess and handing the resulting PNG back
into the workspace, where the ordinary artifact machinery lists and downloads it.

Why the CLI rather than `sd-server`: the operator's build ships both, and the CLI needs
no port, no supervising process, and no lifecycle to keep out of the chat model's way.
The trade-off is honest and documented in DEC-067 — a cold model load per call — which
is why one call generates up to `diffusion.max_images` pictures at once (sd-cli repeats
the load across them) and why `diffusion.timeout_s` defaults to ten minutes.

Design constraints, mirroring hearth/captioner.py (DEC-056) where the analogy holds:

  * Nothing is assumed installed. Until an admin points `diffusion.cli_bin` at a real
    binary, `preflight()` names the exact setting to fix and no tool pretends the
    capability exists. A refusal that can be acted on beats a stack trace — and beats,
    far more importantly, a model describing an image it never got.
  * The prompt goes to the subprocess as an argv element (`-p <text>`), never through a
    shell, so nothing in a user's or an injected document's text can be interpreted as a
    command. Model-supplied numbers are coerced and clamped; model-supplied paths must
    resolve inside the caller's jail (workspaces.py), which also keeps encryption at rest
    intact — the jail is plaintext-capable, so `sd-cli` can read what we hand it.
  * Generation is CPU/GPU-bound for minutes, so callers run it with asyncio.to_thread and
    pass a cancel handle: pressing Stop kills the child rather than letting it finish a
    picture nobody is waiting for.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from . import paths
from . import runtime_settings as rs


class DiffusionError(Exception):
    """Generation is unavailable or failed. Message is model/user-facing."""


# What a single call may produce, and how big the numbers may get. Clamped in code
# rather than trusted: `steps=10000` or `batch=50` from an eager model would otherwise
# tie up the machine for an hour inside one chat turn.
MIN_STEPS, MAX_STEPS = 1, 60
MIN_CFG, MAX_CFG = 0.0, 30.0
MIN_EDGE, MAX_EDGE = 64, 2048
MAX_BATCH_PER_IMAGE = 4


def _num(settings: dict, key: str, typ, default):
    """Read a numeric setting defensively (a junk stored value must not raise)."""
    spec_default = rs.SPEC_BY_KEY[key].default if key in rs.SPEC_BY_KEY else default
    try:
        raw = settings.get(key, spec_default)
        return typ(raw)
    except (TypeError, ValueError):
        try:
            return typ(spec_default)
        except (TypeError, ValueError):
            return default


def _bool(settings: dict, key: str, default: bool = False) -> bool:
    v = settings.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def have(cmd: str) -> bool:
    """Is the diffusion binary available? Absolute path or bare name on PATH."""
    if not cmd:
        return False
    if os.path.isabs(cmd):
        return os.path.isfile(cmd) and os.access(cmd, os.X_OK)
    return shutil.which(cmd) is not None


# ---- configuration ----------------------------------------------------------

def resolve_paths(settings: dict) -> dict:
    """Resolve the binary/model paths from runtime settings, with env fallbacks.

    Settings win; `HEARTH_DIFFUSION_*` is the deployment-level fallback, exactly as
    `HEARTH_LLAMA_*` and `HEARTH_CAPTIONER_*` are for the other two models. Split out so
    status(), argv building and preflight can never disagree about what would run.
    """
    def sget(key: str, env: str) -> str:
        return str(settings.get(key) or "").strip() or os.environ.get(env, "")

    bin_path = sget("diffusion.cli_bin", "HEARTH_DIFFUSION_CLI_BIN")
    model = sget("diffusion.model_path", "HEARTH_DIFFUSION_MODEL_PATH")
    clip_l = sget("diffusion.clip_l", "HEARTH_DIFFUSION_CLIP_L")
    clip_g = sget("diffusion.clip_g", "HEARTH_DIFFUSION_CLIP_G")
    t5xxl = sget("diffusion.t5xxl", "HEARTH_DIFFUSION_T5XXL")
    vae = sget("diffusion.vae", "HEARTH_DIFFUSION_VAE")
    return {
        "enabled": _bool(settings, "diffusion.enabled", False),
        "bin": bin_path,
        "model": model,
        # Text encoders / VAE that live outside the main .gguf (Flux and friends). Empty
        # entries are simply omitted from argv — a plain SD1.5/SDXL single file needs none.
        "encoders": {k: v for k, v in (("clip_l", clip_l), ("clip_g", clip_g),
                                       ("t5xxl", t5xxl), ("vae", vae)) if v},
        "lora_dir": sget("diffusion.lora_dir", "HEARTH_DIFFUSION_LORA_DIR"),
        "backend": sget("diffusion.backend", "HEARTH_DIFFUSION_BACKEND"),
        "preview": _bool(settings, "diffusion.preview", True),
    }


def preflight(settings: dict) -> str:
    """Return a reason generation CANNOT run, or "" when it plausibly can.

    Cheap and synchronous (no subprocess), so a tool call fails immediately with an
    actionable message instead of after the model has already promised the user a
    picture. Order matters: the switch first, then the binary, then the model file —
    the two things that must exist for any generation at all.
    """
    p = resolve_paths(settings)
    if not p["enabled"]:
        return "image generation is disabled (Admin → Images)"
    if not p["bin"]:
        return ("no stable-diffusion.cpp CLI binary is set (Admin → Images → "
                "'stable-diffusion.cpp CLI path')")
    if not have(p["bin"]):
        return f"diffusion CLI binary not found on the server: {p['bin']}"
    if not p["model"]:
        return "no diffusion model path is set (Admin → Images → 'Diffusion model (.gguf)')"
    if not os.path.isfile(p["model"]):
        return f"diffusion model file not found on the server: {p['model']}"
    for flag, path in p["encoders"].items():
        if not os.path.isfile(path):
            return f"diffusion {flag} file not found on the server: {path}"
    return ""


# ---- argv --------------------------------------------------------------------

def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_float(value, lo: float, hi: float, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def build_argv(settings: dict, prompt: str, out_dir: str, *, width: int = 0,
               height: int = 0, steps: int = 0, cfg: float = 0.0, seed=None,
               negative: str = "", batch: int = 1, init_image: str = "",
               strength: float | None = None) -> list[str]:
    """The `sd-cli` command for one generation. Raises DiffusionError if unlaunchable.

    Paths are staged outside the jail by the caller, so every path here is inside a private
    temp directory — the one exception is `init_image`, an absolute path the tool resolved
    inside the user's jail and decrypted into that same temp dir (see StagedImage).

    Output uses `%03d` deliberately, and the naming is not a guess: `examples/cli/main.cpp`
    matches `-o` against `(%\d{0,3}d)` and runs the specifier through `snprintf` verbatim with
    `output_begin_idx + i`, begin_idx defaulting to 0 when a pattern is present. So
    `hearth_%03d.png` yields exactly `hearth_000.png`, `hearth_001.png`, … Naming files after the
    call rather than globbing keeps discovery exact — no stale file can be mistaken for this run's.
    """
    p = resolve_paths(settings)
    if not p["bin"]:
        raise DiffusionError("no stable-diffusion.cpp CLI binary configured "
                             "(Admin → Images → 'stable-diffusion.cpp CLI path')")
    if not have(p["bin"]):
        raise DiffusionError(f"diffusion CLI binary not found: {p['bin']}")
    if not p["model"]:
        raise DiffusionError("no diffusion model path set (Admin → Images)")
    if not os.path.isfile(p["model"]):
        raise DiffusionError(f"diffusion model file not found: {p['model']}")

    w = _clamp_int(width or _num(settings, "diffusion.width", int, 768), MIN_EDGE, MAX_EDGE, 768)
    h = _clamp_int(height or _num(settings, "diffusion.height", int, 768), MIN_EDGE, MAX_EDGE, 768)
    st = _clamp_int(steps or _num(settings, "diffusion.steps", int, 20), MIN_STEPS, MAX_STEPS, 20)
    g = _clamp_float(cfg or _num(settings, "diffusion.cfg_scale", float, 4.5), MIN_CFG, MAX_CFG, 4.5)
    n = max(1, min(int(batch or 1), MAX_BATCH_PER_IMAGE))

    a = [p["bin"], "--model", p["model"]]
    for flag, path in p["encoders"].items():
        a += [f"--{flag}", path]
    if p["lora_dir"]:
        a += ["--lora-model-dir", p["lora_dir"]]
    if p["backend"]:
        a += ["--backend", p["backend"]]

    a += ["-p", str(prompt or "")]
    neg = str(negative or "").strip()
    if neg:
        a += ["-n", neg]
    a += ["-W", str(w), "-H", str(h), "--steps", str(st), "--cfg-scale", f"{g:g}"]

    seed_spec = _num(settings, "diffusion.seed", int, -1) if seed is None else int(seed)
    # sd-cli takes `-s` with an optional value: omitting it asks for a random seed. Passing
    # -1 instead would be parsed as an argument to the next flag, so it is omitted too.
    if seed_spec >= 0:
        a += ["-s", str(seed_spec)]

    if init_image:
        a += ["-i", init_image]
        s_val = (strength if strength is not None
                 else _num(settings, "diffusion.strength", float, 0.75))
        a += ["--strength", f"{_clamp_float(s_val, 0.0, 1.0, 0.75):g}"]

    # `--preview none` is not optional: sd-cli defaults to --preview proj AND writes
    # ./preview.png (and step previews alongside it). Without pinning this, every call
    # would litter whatever working directory Hearth happens to run in.
    a += ["--preview", ("vae" if p["preview"] else "none")]

    a += ["-b", str(n), "-o", os.path.join(out_dir, "hearth_%03d.png"),
          "--preview-path", os.path.join(out_dir, "preview_%03d.png")]

    extra = str(settings.get("diffusion.extra_args") or "").strip()
    if extra:
        try:
            a += shlex.split(extra)
        except ValueError:
            raise DiffusionError(f"diffusion extra args cannot be parsed: {extra!r}")
    return a


def launch_command_preview(settings: dict) -> dict:
    """A representative command line for copying into a terminal (Admin UI).

    Uses a placeholder prompt and temp paths so the admin sees the model/encoder flags
    that actually matter without Hearth claiming to have generated anything.
    """
    try:
        argv = build_argv(settings, "a lighthouse at dusk", "/tmp", steps=0)
    except DiffusionError as e:
        return {"ok": False, "command": "", "note": str(e)}
    return {"ok": True, "command": shlex.join(argv), "argv": argv, "note": ""}


# ---- the runner --------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen, grace_s: float = 2.0) -> bool:
    """Terminate a child AND its descendants; True if something had to be killed.

    `terminate()` alone is not enough and this was proven, not assumed: with the real
    sd-cli driven through a shell wrapper, SIGTERM to the direct child left the grandchild
    running — i.e. Hearth would have reported "timed out" while a phantom process went on
    holding the GPU and blocking the next generation. sd-cli spawns helpers (and a user
    could reasonably point diffusion.cli_bin at a wrapper script), so the whole group goes.

    Uses its own process group (`start_new_session=True` below), which is what makes
    killpg() safe: we only ever signal a group Hearth created itself, never Hearth's own.
    """
    killed = False
    try:
        if proc.poll() is None:
            proc.terminate()
            _killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            proc.kill()
            _killpg(proc.pid, signal.SIGKILL)
            killed = True
        except OSError:
            pass
    return killed


def _killpg(pid: int, sig: int) -> None:
    """Signal pid's process group, if it still has one. Never raises."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (OSError, ProcessLookupError):
        pass


class Diffusion:
    """Serialises `sd-cli` runs and reports what this machine can do.

    One global instance (get_diffusion()). Runs are serialised on a lock for the same
    reason the captioner's are: generation is memory-bandwidth-bound, so two at once make
    both slower and risk OOM-ing the chat model that shares the box. A queue would be
    kinder than an error, but a silently-queued minute-long wait with no explanation reads
    as a hang — so `busy_timeout_s` bounds the wait and says what happened.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._log_path = paths.RUNTIME_DIR / "diffusion.log"
        self._last_hint = ""
        # The sd-cli child of the generation now in flight, plus a lock-guarded kill flag so
        # a Stop arriving before the process exists still tears down the next one. Same
        # shape as captioner.Captioner (DEC-057/058): cooperative cancel alone cannot
        # interrupt a blocking subprocess.run.
        self._active_proc: subprocess.Popen | None = None
        self._stop_requested = False
        self._lock_state = threading.Lock()

    # -- cancellation ---------------------------------------------------------
    def stop_active(self) -> bool:
        """Kill the generation in flight, or record the intent if it has not started yet.

        Tears down the whole process group via _kill_tree — terminate() alone was shown to
        leave a grandchild alive, which would mean reporting 'stopped' while a phantom sd-cli
        kept the GPU and blocked the next request.
        """
        with self._lock_state:
            self._stop_requested = True
            p = self._active_proc
        if p is None or p.poll() is None:
            return False
        _kill_tree(p)
        return p.poll() is not None

    def clear_stop(self) -> None:
        """Forget a pending kill (called at the start of each new generation)."""
        with self._lock_state:
            self._stop_requested = False

    def _set_active(self, proc) -> None:
        """Record the running child; honour a Stop that arrived before it existed."""
        self._active_proc = proc
        if proc is not None:
            with self._lock_state:
                pending = self._stop_requested
            if pending:
                _kill_tree(proc)

    # -- generation -----------------------------------------------------------
    def generate_sync(self, settings: dict, prompt: str, *, width: int = 0, height: int = 0,
                      steps: int = 0, cfg: float = 0.0, seed=None, negative: str = "",
                      batch: int = 1, init_image: str = "", strength: float | None = None,
                      cancel=None, log=lambda m: None) -> dict:
        """Run one `sd-cli` generation. SYNCHRONOUS — call via asyncio.to_thread.

        `init_image` must be an already-resolved absolute path readable by the subprocess
        (staged outside the jail by generate_file(), so encryption at rest cannot hand it
        ciphertext). Returns {"images": [(name, bytes)], "seed": int|None,
        "seconds": float, "argv": [...], "notes": [str]}. Raises DiffusionError.

        Files are read back from the temp dir rather than trusted by name: sd-cli's own
        numbering decides the suffix, so the caller must not guess it — but since the
        directory is ours and freshly made, whatever appears in it IS this run's output.
        """
        reason = preflight(settings)
        if reason:
            raise DiffusionError(reason)

        timeout = max(30.0, float(_num(settings, "diffusion.timeout_s", int, 600)))
        busy_timeout = max(0.0, float(_num(settings, "diffusion.busy_timeout_s", int, 900)))
        self.clear_stop()

        if not self._lock.acquire(timeout=busy_timeout):
            raise DiffusionError(
                f"another image is still being generated and the queue waited "
                f"{int(busy_timeout)} s for it (Admin → Images → 'Queue wait'). Ask again "
                f"in a moment, or lower steps/size so generations are quicker.")
        try:
            if _cancel_requested(cancel):
                raise DiffusionError("cancelled before generating anything")

            with tempfile.TemporaryDirectory(prefix="hearth-sd-") as tmp:
                out_dir = os.path.join(tmp, "out")
                os.makedirs(out_dir, exist_ok=True)
                argv = build_argv(settings, prompt, out_dir, width=width, height=height,
                                  steps=steps, cfg=cfg, seed=seed, negative=negative,
                                  batch=batch, init_image=init_image, strength=strength)
                log("$ " + shlex.join(argv))
                t0 = time.time()
                try:
                    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                    with open(self._log_path, "ab") as logf:
                        proc = subprocess.Popen(argv, stdout=logf, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, cwd=tmp,
                                                # Own process group so a kill reaches everything
                                                # sd-cli spawns (see _kill_tree).
                                                start_new_session=True)
                except OSError as e:
                    raise DiffusionError(f"could not run the diffusion CLI: {e}")
                self._set_active(proc)
                if _cancel_requested(cancel):
                    self.stop_active()
                try:
                    rc = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # Kill the whole process group so a child that outlived the wait cannot
                    # keep burning the GPU after the tool has already answered.
                    self.stop_active()
                    raise DiffusionError(
                        f"image generation exceeded its {int(timeout)} s limit "
                        f"(Admin → Images → 'Generation timeout'). Lower the step count or "
                        f"resolution, or raise the limit; the model may also still be "
                        f"loading — see {self._log_path}")
                finally:
                    self._set_active(None)
                seconds = round(time.time() - t0, 1)

                if rc != 0:
                    tail = self.read_log_tail(600)
                    raise DiffusionError(
                        f"the diffusion CLI exited with code {rc} after {seconds}s"
                        + (f"; last output: {tail}" if tail else "")
                        + f" — full output: {self._log_path}")

                images = self._collect(out_dir)
                if not images:
                    tail = self.read_log_tail(600)
                    raise DiffusionError(
                        "the diffusion CLI exited successfully but wrote no image"
                        + (f"; last output: {tail}" if tail else "")
                        + f" — check {self._log_path} (a mismatched model/projector pair or "
                          f"an unparseable --backend usually looks exactly like this)")
                notes = []
                if len(images) < max(1, int(batch or 1)):
                    notes.append(f"asked for {int(batch)} image(s), got {len(images)}")
                return {"images": images, "seed": self._seed_from_argv(argv),
                        "seconds": seconds, "argv": argv, "notes": notes}
        finally:
            self._lock.release()

    @staticmethod
    def _seed_from_argv(argv: list[str]) -> int | None:
        """The explicit seed Hearth passed, or None when sd-cli chose one at random.

        Reported as `requested` rather than `used`: with `-s` omitted the real seed lives in
        the image's embedded metadata (sd-cli writes generation parameters by default), so
        claiming a number we do not know would be a lie the model then repeats to the user.
        """
        for i, a in enumerate(argv):
            if a == "-s" and i + 1 < len(argv):
                try:
                    return int(argv[i + 1])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _collect(out_dir: str) -> list[tuple[str, bytes]]:
        """Every image sd-cli wrote into our private dir, main outputs before previews.

        `hearth_*.png` is the requested output; `preview_*.png` is only used when the real
        output is missing (a model that failed at the very last step still produced
        *something* worth showing, labelled as a preview by the caller's text).
        """
        try:
            names = sorted(os.listdir(out_dir))
        except OSError:
            return []
        main = [n for n in names if n.startswith("hearth_") and n.lower().endswith(".png")]
        fallback = [n for n in names if n.lower().endswith(".png") and n not in main]
        chosen = main or fallback[:1]
        out: list[tuple[str, bytes]] = []
        for n in chosen[:MAX_BATCH_PER_IMAGE]:
            try:
                with open(os.path.join(out_dir, n), "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if data:
                out.append((n, data))
        return out

    def read_log_tail(self, max_chars: int = 600) -> str:
        """Last bytes of the diffusion log, one line, for embedding in failure hints."""
        try:
            with open(self._log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_chars))
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            return ""
        return " ".join(raw.split())[-max_chars:]

    # -- status ---------------------------------------------------------------
    def status(self, settings: dict | None = None) -> dict:
        """What the admin UI shows about this capability. Never raises."""
        settings = settings if settings is not None else rs.get_all()
        p = resolve_paths(settings)
        configured = bool(p["bin"] and p["model"])
        return {
            "kind": "diffusion",
            "enabled": p["enabled"],
            "configured": configured,
            # There is no server: a binary that exists and runs IS the whole story.
            "reachable": have(p["bin"]),
            "managed_by_hearth": True,
            "model_path": p["model"],
            "paths": {"bin": p["bin"], "model": p["model"], **p["encoders"]},
            "hint": preflight(settings) if p["enabled"] else "",
            "log": str(self._log_path),
            "capabilities": {"cli_present": have(p["bin"]),
                             "model_present": bool(p["model"]) and os.path.isfile(p["model"]),
                             "backend": p["backend"],
                             "busy": self._lock.locked()},
        }


def _cancel_requested(cancel) -> bool:
    """Has a cancel-like object been set? Never raises.

    Accepts both shapes used in this codebase — `.is_set()` (threading.Event, and the
    ToolContext adapter) and `.cancelled` (hearth.cancel.CancelHandle) — exactly like
    captioner._cancel_requested, so diffusion never has to import hearth.cancel.
    """
    if cancel is None:
        return False
    try:
        if hasattr(cancel, "is_set"):
            return bool(cancel.is_set())
        return bool(getattr(cancel, "cancelled", False))
    except Exception:  # noqa: BLE001 — an unfamiliar handle reads as "cannot be aborted"
        return False


# ---- async entry points ------------------------------------------------------

def get_diffusion() -> Diffusion:
    """The process-wide diffusion runner."""
    global _diffusion
    if _diffusion is None:
        _diffusion = Diffusion()
    return _diffusion


_diffusion: Diffusion | None = None


async def generate(settings: dict, prompt: str, **kwargs) -> dict:
    """Generate images off the event loop (sd-cli blocks for minutes)."""
    return await asyncio.to_thread(get_diffusion().generate_sync, settings, prompt, **kwargs)
