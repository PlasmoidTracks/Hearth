"""
Subagent model server: a second, standalone llama-server child process that
runs a small text model for deployed subagents (see core/subagent.py), on its
own port, alongside the main model.

Why a second process at all: a subagent must not inherit the main model's
settings. The main agent's ServerConfig carries a roleplay-tuned sampler, a
huge context, speculative decoding, an mmproj, reasoning flags, and a GPU
placement chosen for a model that routinely spills onto the CPU. None of that
is what you want for a tiny worker model, and none of it can be overridden
per-request — it is all baked into the server's command line. So the subagent
gets its own llama-server with its own ServerConfig on its own port, and
SubagentRunner is simply handed an LLMClient pointed at it.

A second llama-server child, built on the shape established by
core/llama_runtime.py:
  * `SubagentPolicy`         — persisted settings (QSettings key "subagent_policy").
  * `SubagentServerRuntime`  — lazily starts/kills a dedicated LlamaServerRuntime
    built from a derived ServerConfig, with a GUI-thread/worker-thread split, a
    detached-subprocess fallback, and an endpoint-probe readiness check.
  * `SubagentStartWorker`    — QThread wrapper so "Start Server" does not block
    the UI while the model loads.

Device placement is an explicit user setting rather than an inherited or
hardcoded choice: `device` is "gpu" (all weights offloaded) or "ram" (no
offload at all). Both emit -ngl explicitly, so llama.cpp is never allowed to
pick a partial offload — a mixed placement is not reachable through this policy.

Nothing in here runs unless a subagent is actually deployed with a model
configured, and no failure in here raises into the agent loop: every error
path ends as `last_error()` text meant to be read verbatim by the caller.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QProcess, QThread, Signal

from .llama_runtime import (
    LlamaServerRuntime, ServerConfig, probe_endpoint, KV_CACHE_TYPES,
)
from .app_settings import _as_bool

# Number of layers to request for a "fully on GPU" subagent model. The model is
# expected to be small, so a number far above any real layer count is the
# portable way to say "everything": llama.cpp clamps -ngl to the model's actual
# depth instead of erroring, and it keeps working if the user swaps in a deeper
# checkpoint. 0 is the mirror-image answer for "fully in RAM".
GPU_LAYERS_ALL = 9999
GPU_LAYERS_NONE = 0

DEVICE_CHOICES = ("gpu", "ram")


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------
# A deployment is a blocking call that runs on the AgentWorker thread with no UI
# feedback, so a slow/failed startup is indistinguishable from a hang from the
# outside. _dlog stamps each decision point with wall-clock + elapsed time so the
# Server Log tab shows exactly where a call stalled. To silence:
# set LLMCODER_SUBAGENT_DEBUG=0.
_SUBAGENT_DEBUG = os.environ.get("LLMCODER_SUBAGENT_DEBUG", "1") != "0"
_log_t0: Optional[float] = None
_log_hook: Optional[Callable[[str], None]] = None
_log_hook_lock = threading.Lock()


def set_debug_hook(fn: Optional[Callable[[str], None]]) -> None:
    """Register a sink (e.g. MainWindow._append_log) for subagent diagnostics.
    Safe to call repeatedly; None detaches. Lines are always echoed to stderr
    too, so running `python main.py` from a terminal captures them even when the
    GUI is closed or the log widget never gets flushed."""
    global _log_hook
    with _log_hook_lock:
        _log_hook = fn


def _dlog(msg: str) -> None:
    """Emit one timestamped diagnostics line to stderr + the registered sink.
    Never raises: debugging must not be able to break the deployment itself."""
    if not _SUBAGENT_DEBUG:
        return
    global _log_t0
    if _log_t0 is None:
        _log_t0 = time.time()
    elapsed = time.time() - _log_t0
    line = (f"[subagent-dbg {time.strftime('%H:%M:%S')} +{elapsed:6.2f}s "
            f"{threading.current_thread().name}] {msg}")
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    with _log_hook_lock:
        hook = _log_hook
    if hook is None:
        return
    # A sink here (MainWindow._append_log) writes into a QWidget, and widgets may
    # only be touched from the thread that created them. Off the GUI thread we
    # post the line to the GUI thread's event loop rather than call the sink
    # directly, which is the cross-thread QTextDocument corruption that crashes
    # on the NEXT repaint. On the GUI thread (or with no app) just call it.
    try:
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is not None and threading.current_thread() is not app.thread():
            app.callLater(_dlog, msg)
            return
    except Exception:
        pass
    try:
        hook(line)
    except Exception:
        pass


def _nonblocking_read_fd(fd: int, max_bytes: int = 65536) -> str:
    """Read whatever is immediately available on a file descriptor without ever
    blocking. Returns "" when nothing is ready, the fd is invalid, or the stream
    reached EOF.

    Used to drain the subagent llama-server's stderr pipe directly from the
    worker thread. LlamaServerRuntime normally forwards that pipe via
    readyReadStandardError, but that Qt signal needs the owning thread's event
    loop — and the server can be spawned on the AgentWorker thread, which has
    none — so its output would otherwise be silently lost. POSIX only; returns
    "" elsewhere."""
    if os.name != "posix":
        return ""
    import fcntl
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except (OSError, ValueError):
        return ""
    out = b""
    while len(out) < max_bytes:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            break              # nothing available right now — that's normal
        except (OSError, ValueError):
            break              # EOF or closed fd
        if not chunk:
            break              # EOF
        out += chunk
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except (OSError, ValueError):
        pass
    return out.decode("utf-8", errors="replace") if out else ""


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

@dataclass
class SubagentPolicy:
    """Persisted under the QSettings key "subagent_policy"; to_dict/from_dict
    mirror ServerConfig's style so unknown/missing keys are tolerated
    (core/app_settings.py already fills in defaults for all of these)."""

    enabled: bool = False
    model_path: str = ""           # small text GGUF the subagent should run
    binary_path: str = ""          # "" -> reuse the main server's binary
    host: str = "127.0.0.1"
    port: int = 8082               # != main server's 8080
    context_size: int = 4096
    device: str = "gpu"            # "gpu" = all weights on GPU | "ram" = none
    startup_timeout: int = 120     # seconds to wait for the model to load
    extra_args: str = "--jinja"    # --jinja is what makes tool calling work

    # ---- inference knobs the deployment reads (Agent._deploy_subagent) ----
    # A worker is usually a small, non-reasoning checkpoint, for which
    # chat_template_kwargs{"enable_thinking": true} is at best wasted prompt
    # structure and at worst makes the template wrap the answer in <think>
    # blocks a clean one-shot transcript does not want. So this is a per-subagent
    # switch, deliberately NOT inherited from the main agent's policy: the main
    # model's thinking setting and the worker's have no reason to agree.
    enable_thinking: bool = True
    preserve_thinking: bool = False

    # ---- KV cache quantization, own to this server ----
    # "" means "inherit the main server's value" (see build_server_config).
    # Any other value must be a KV_CACHE_TYPES member and is emitted as THIS
    # child's own --cache-type-k/--cache-type-v. Only a separate process can do
    # this at all: the KV buffer's type is fixed when the model is loaded, so
    # the OpenAI endpoint offers no per-request override.
    kv_cache_type_k: str = ""
    kv_cache_type_v: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SubagentPolicy":
        base = SubagentPolicy()
        for k, v in (d or {}).items():
            if hasattr(base, k):
                setattr(base, k, v)
        if base.device not in DEVICE_CHOICES:
            base.device = "gpu"
        # QSettings round-trips booleans as strings in some backends, and a
        # stray "False" is truthy — coerce the way load_subagent_policy does.
        base.enable_thinking = _as_bool(base.enable_thinking, default=True)
        base.preserve_thinking = _as_bool(base.preserve_thinking, default=False)
        # A KV type that is neither empty (inherit) nor a type llama.cpp knows
        # would be passed straight onto the command line and could kill the
        # child at startup, so fall back to inheriting rather than trusting it.
        for name in ("kv_cache_type_k", "kv_cache_type_v"):
            val = str(getattr(base, name) or "").strip()
            setattr(base, name, val if val in KV_CACHE_TYPES else "")
        return base

    @property
    def gpu_layers(self) -> int:
        """-ngl for this policy: an explicit all-or-nothing answer.

        Never -1 ("let llama.cpp decide"), because that is precisely how a
        partial/mixed offload would sneak in — and mixed placement is excluded
        by design for the subagent model."""
        return GPU_LAYERS_ALL if self.device == "gpu" else GPU_LAYERS_NONE

    def is_configured(self) -> bool:
        """True only when the feature is on AND a model path is set. Every
        entry point checks this so a half-configured policy can never cause a
        spawn attempt."""
        return bool(self.enabled and (self.model_path or "").strip())

    def signature(self) -> tuple:
        """The subset of settings baked into the child's command line at spawn
        time. Two policies with the same signature produce the same argv, so a
        change here means the running child is serving with stale settings and
        must be restarted; a change anywhere else does not.

        Deliberately excludes `enable_thinking`/`preserve_thinking`: those are
        per-request chat_template_kwargs applied by the deployment, so they take
        effect on the next deployment without any restart. Restarting a model
        over a thinking toggle would be pure collateral damage."""
        return (
            self.model_path.strip(), self.binary_path.strip(), self.host,
            int(self.port), int(self.context_size), self.device, self.extra_args,
            (self.kv_cache_type_k or "").strip(), (self.kv_cache_type_v or "").strip(),
        )

    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"


# --------------------------------------------------------------------------
# Server runtime (2nd llama-server child, independent of the main one)
# --------------------------------------------------------------------------

class SubagentServerRuntime:
    """Owns a dedicated LlamaServerRuntime for the subagent model.

    Not a QObject itself: it composes one. That keeps Qt off the critical path
    (a deployment happens on the AgentWorker thread) while still reusing the
    startup polling, log parsing, and teardown that LlamaServerRuntime already
    implements. The threading rules below exist because a QProcess/QTimer may
    only be created on the GUI thread — building one on a worker thread with no
    Qt event loop and letting the GUI thread destroy it is a hard SIGSEGV."""

    def __init__(self, policy: SubagentPolicy, main_config: Optional[ServerConfig] = None):
        self.policy = policy
        self.main_config = main_config
        self.runtime: Optional[LlamaServerRuntime] = None
        # Command-line signature of the child currently running (see
        # SubagentPolicy.signature). Recorded only for a child we actually
        # spawned, never for an adopted endpoint we do not own — an adopted
        # server's argv is unknown, so it must never be restarted over it.
        self._spawned_signature: Optional[tuple] = None
        self._last_error: str = ""
        # Exact argv of the most recent spawn, for failure diagnostics.
        self._last_spawn_argv: List[str] = []
        # PID of a detached (plain-subprocess) server, started from a worker
        # thread where a QProcess cannot be created. None otherwise.
        self._detached_pid: Optional[int] = None
        # True while a *managed* (QProcess) child of ours is alive on
        # policy.port. Without this, a second spawn could not tell that the
        # port was already taken by its own managed child.
        self._detached_alive: bool = False
        # True once an already-live endpoint has been reported. Pure
        # bookkeeping so is_ready()/start() can answer without touching any Qt
        # object from a worker thread.
        self._adopted = False

    # ---- threading ----

    @staticmethod
    def _on_gui_thread() -> bool:
        """Whether the caller is on the thread that owns the Qt GUI objects.

        QProcess/QTimer belong to the thread that created them and need that
        thread's event loop. Deployments run on AgentWorker, which has neither,
        so every place that creates a LlamaServerRuntime must check this first:
        building one on a loop-less thread and letting the GUI thread destroy it
        is a hard crash (SIGSEGV) in PySide6, not a warning."""
        try:
            from PySide6.QtCore import QCoreApplication, QThread
            app = QCoreApplication.instance()
            if app is None:
                return True          # no Qt app at all (tests / CLI)
            return QThread.currentThread() == app.thread()
        except Exception:
            return False

    def set_main_config(self, main_config: Optional[ServerConfig]) -> None:
        """Attach the main agent's ServerConfig (called from the GUI thread
        whenever settings change) and adopt an already-live endpoint.

        Adoption happens here, on the GUI thread, rather than inside start(), so
        a worker thread never has to build a LlamaServerRuntime."""
        self.main_config = main_config
        if self._adopted or self.runtime is not None:
            return
        if self.policy.is_configured() and probe_endpoint(self.policy.base_url(), timeout=1.5):
            _dlog("subagent set_main_config(): endpoint already answering, adopting (GUI thread)")
            self._adopt_live_endpoint()

    def _adopt_live_endpoint(self) -> None:
        """Mark an already-running endpoint as attached. Only call this on the
        GUI thread, or when `self.runtime` already exists."""
        self._adopted = True
        if self.runtime is not None:
            self.runtime.adopt_external()

    # ---- config ----

    def build_server_config(self) -> ServerConfig:
        """Derive the subagent server's config from the main agent's config.

        Inherited on purpose: binary path, host, flash-attn mode, threads, and
        load mode. Those are cheap, per-device quality knobs the user has
        already tuned, and applying them to a second model costs nothing.

        Inherited *by default, overridable per field*: the KV cache K/V type.
        An empty value in the policy means inherit; any KV_CACHE_TYPES member
        (validated in from_dict) replaces the main server's. This is the one
        memory knob worth overriding here, because the type is baked into the
        KV buffer at model-load time and therefore cannot be changed per
        request — a second process is the ONLY way to give the worker a
        different quantization than the main model, which is exactly what makes
        it worth exposing.

        Deliberately NOT inherited: model path, mmproj, the whole
        speculative-decoding stack, sampling defaults (a subagent wants the
        model's own defaults, not the main agent's roleplay-tuned sampler),
        reasoning flags, n_predict, parallel slots, and — the important one —
        the main model's GPU placement. `gpu_layers`/`device`/`split_mode` come
        from THIS policy instead, because a main model that spills onto the CPU
        is exactly the wrong template for a small worker model."""
        main = self.main_config or ServerConfig()
        cfg = ServerConfig()
        cfg.managed = True
        cfg.binary_path = (self.policy.binary_path or "").strip() or main.binary_path
        cfg.model_path = self.policy.model_path.strip()
        cfg.mmproj_path = ""
        cfg.host = self.policy.host or "127.0.0.1"
        cfg.port = int(self.policy.port)
        cfg.context_size = int(self.policy.context_size)
        # "" -> inherit the main server's value; anything else (already
        # validated against KV_CACHE_TYPES by from_dict) -> this child's own.
        cfg.kv_cache_type_k = (self.policy.kv_cache_type_k or "").strip() \
            or main.kv_cache_type_k
        cfg.kv_cache_type_v = (self.policy.kv_cache_type_v or "").strip() \
            or main.kv_cache_type_v
        cfg.flash_attn = main.flash_attn
        # The explicit all-or-nothing placement (see SubagentPolicy.gpu_layers).
        # An explicit -ngl is emitted in BOTH cases so llama.cpp can never fall
        # back to its own partial offload, and -nkvo on the CPU path stops KV
        # from being pushed back onto a GPU the model itself was kept off.
        cfg.gpu_layers = self.policy.gpu_layers
        cfg.device = ""
        cfg.split_mode = "none" if self.policy.device == "ram" else main.split_mode
        cfg.no_kv_offload = (self.policy.device == "ram")
        cfg.load_mode = main.load_mode
        cfg.threads = main.threads
        cfg.threads_batch = main.threads_batch
        # Single slot: a deployment is synchronous and never concurrent.
        cfg.parallel_slots = 1
        cfg.disable_webui = True
        cfg.speculative_type = "none"
        cfg.extra_args = self.policy.extra_args or ""
        # Reset the reasoning block + n_predict to neutral defaults so
        # build_args() emits none of them: they are tuned for the main chat
        # model, and on strict llama.cpp builds an unsupported flag is fatal
        # ("unrecognized argument") rather than ignored.
        cfg.reasoning_preserve = False
        cfg.reasoning_format = "auto"
        cfg.reasoning_mode = "auto"
        cfg.reasoning_budget = -1
        cfg.reasoning_budget_message = ""
        cfg.n_predict = -1
        # Whether the KV flags actually reach the command line is decided by
        # ServerConfig.build_args(), which omits them when the type is "f16"
        # (llama.cpp's own default). Log the resolved pair because "inherit"
        # means the effective value is the main server's, which is otherwise
        # invisible when reading a subagent-only log line.
        _dlog(f"subagent config: KV cache resolved to k={cfg.kv_cache_type_k} "
              f"v={cfg.kv_cache_type_v} "
              f"(policy k={self.policy.kv_cache_type_k or '<inherit>'} "
              f"v={self.policy.kv_cache_type_v or '<inherit>'}, "
              f"main k={main.kv_cache_type_k} v={main.kv_cache_type_v}); "
              f"note quantized KV only pays off with flash attention on "
              f"(currently {cfg.flash_attn})")
        return cfg

    # ---- lifecycle ----

    def is_ready(self) -> bool:
        if not self.policy.is_configured():
            return False
        # Off the GUI thread, `self.runtime.state()` is untrustworthy (nothing
        # updates it without an event loop) — the endpoint probe is the only
        # reliable signal there, and it is also the cheapest one on the GUI
        # thread, so it decides readiness everywhere.
        ready = probe_endpoint(self.policy.base_url(), timeout=1.5)
        _dlog(f"subagent is_ready: probe {self.policy.base_url()} -> {ready} "
              f"(runtime={'yes' if self.runtime is not None else 'no'}, "
              f"gui_thread={self._on_gui_thread()})")
        if not ready and self._adopted:
            self._adopted = False
        return ready

    def state(self) -> str:
        return self.runtime.state() if self.runtime is not None else "stopped"

    def last_error(self) -> str:
        return self._last_error

    # ---- spawn helpers ----

    def _binary_for_spawn(self) -> str:
        """The subagent server binary: the policy's own if set, else the main
        agent's. Shared by the QProcess path and the detached-subprocess path so
        both agree on what they are launching."""
        main = self.main_config or ServerConfig()
        return (self.policy.binary_path or "").strip() or main.binary_path

    def _prevalidate_model_path(self) -> bool:
        """Set a specific `_last_error` and return False when the configured
        model path can never load (missing file, or the LM Studio folder
        mistakenly selected instead of the .gguf inside it)."""
        model = self.policy.model_path.strip()
        if model and os.path.isdir(model):
            self._last_error = (
                f"The subagent model path is a directory, not a .gguf file: {model!r}. "
                "Open Settings → Backend → Subagent model and select the actual "
                "*.gguf file (for an LM Studio download, the file inside that "
                "folder) — not the folder itself."
            )
            _dlog(f"subagent start(): refused — model path is a directory: {model!r}")
            return False
        if model and not os.path.exists(model):
            self._last_error = f"The subagent model file does not exist: {model!r}."
            _dlog(f"subagent start(): refused — model file missing: {model!r}")
            return False
        return True

    def _render_spawn_argv(self) -> Optional[List[str]]:
        """Build the subagent server command line, storing it for diagnostics.

        Returns None (with `_last_error` set) when the binary is missing, so a
        bad path surfaces as an actionable message instead of a bare
        FileNotFoundError deep inside subprocess."""
        binary = self._binary_for_spawn()
        if not binary or not os.path.exists(binary):
            self._last_error = (
                f"The subagent server binary does not exist: {binary!r}. Set "
                "'Subagent llama-server binary' under Settings → Backend → "
                "Subagent model server to your llama-server executable."
            )
            _dlog(f"subagent start(): refused — binary missing: {binary!r}")
            return None
        try:
            cfg = self.build_server_config()
            argv = [cfg.binary_path] + cfg.build_args()
            self._last_spawn_argv = list(argv)
            _dlog("subagent start(): full argv = " + " ".join(argv))
            return argv
        except Exception as _e:  # build_args must never break the log path
            self._last_spawn_argv = []
            self._last_error = f"Could not build the subagent server command line: {_e}"
            _dlog(f"subagent start(): could not render argv: {_e}")
            return None

    def _port_collision(self) -> Optional[str]:
        """Return an error message when the main server shares our port, else None.

        This is the failure mode most likely to bite HERE specifically: the
        subagent model is deliberately brought up at the same time as the main
        model, so if the user has pointed it at the main port the two
        llama-server processes race for one socket. One of them dies with
        "address already in use", and because the readiness check is just a
        /models probe, the survivor answers and this app happily talks to the
        WRONG model while reporting success. Comparing the ports up front turns
        that into an actionable message instead. A live endpoint on OUR OWN port
        is not a collision — that is the adopt-already-running case."""
        main = self.main_config or ServerConfig()
        try:
            port = int(self.policy.port)
        except (TypeError, ValueError):
            return "The subagent server port is not a valid number."
        try:
            main_port = int(main.port)
        except (TypeError, ValueError):
            main_port = -1
        # Only meaningful when the app actually owns the main port: in external
        # mode main.port is whatever was last typed and the main server lives
        # wherever external_base_url points.
        if main.managed and port == main_port:
            return (
                f"The subagent port is the same as the main server's port "
                f"({port}). Both llama-server processes would compete for one "
                "socket, and whichever answered first would silently serve the "
                "wrong model. Set a different port under Settings → Backend → "
                "Subagent model server (the default is 8082)."
            )
        return None

    def _diagnose_dead_child(self, argv: List[str]) -> str:
        """Actionable text for 'the subagent llama-server exited before it was
        ready'. The most common real causes are a directory instead of a .gguf
        file, and an unsupported CLI flag inherited from the main server."""
        model = (argv[argv.index("-m") + 1] if "-m" in argv
                 and argv.index("-m") + 1 < len(argv) else self.policy.model_path)
        lines = [
            "The subagent llama-server exited before it became ready.",
            f"Model path passed to -m: {model!r}",
            f"Requested placement: device={self.policy.device!r} "
            f"(-ngl {self.policy.gpu_layers}).",
        ]
        if model and os.path.isdir(model):
            lines.append(
                "That path is a DIRECTORY, not a model file. Pick the actual "
                ".gguf file under Settings → Backend → Subagent model."
            )
        if self.policy.device == "gpu":
            lines.append(
                "If the log shows a CUDA/Vulkan out-of-memory error, the GPU has "
                "no room left for this model on top of the main one — switch the "
                "subagent device to 'RAM (CPU, no offload)' rather than shrinking "
                "the main model."
            )
        lines.append(
            "Also check the llama-server output below for 'unrecognized argument' "
            "(a flag this build rejects) or 'failed to load model'."
        )
        return "\n".join(lines)

    def _spawn_detached(self, argv: List[str]) -> Optional[int]:
        """Start the subagent llama-server as a plain child process, Qt-free.

        This is the path used when a deployment runs on a worker thread: a
        QProcess may only be created on the GUI thread, but a plain subprocess
        is thread-agnostic, so spawning here works from anywhere. The child is
        detached (its own session) and its output goes to a log file rather than
        a Qt pipe, so nothing in this path touches a Qt object.

        Returns the child PID, or None if it could not be started.
        """
        log_path = os.path.join(tempfile.gettempdir(), "llmcoder_subagent_server.log")
        try:
            with open(log_path, "ab", buffering=0) as log_file:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                    close_fds=True,
                )
            self._detached_pid = proc.pid
            _dlog(f"subagent _spawn_detached(): launched pid={proc.pid} -> log {log_path}")
            return proc.pid
        except Exception as e:
            _dlog(f"subagent _spawn_detached(): FAILED to launch: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _child_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True

    def _read_detached_log_tail(self, lines: int = 40) -> List[str]:
        log_path = os.path.join(tempfile.gettempdir(), "llmcoder_subagent_server.log")
        try:
            with open(log_path, "rb") as f:
                data = f.read()
            return data.decode("utf-8", errors="replace").splitlines()[-lines:]
        except Exception:
            return []

    def _terminate_detached(self) -> None:
        pid = self._detached_pid
        if not pid:
            return
        try:
            if self._child_is_alive(pid):
                os.kill(pid, 15)   # SIGTERM, mirroring QProcess.terminate()
        except Exception:
            pass
        self._detached_pid = None

    # ---- readiness waits ----

    def _wait_until_ready(self, timeout: float) -> Tuple[str, str, List[str]]:
        """Block until the subagent server answers, without relying on Qt signals.

        Same reasoning as LlamaServerRuntime's own poll: the readiness QTimer
        and the QProcess.finished signal both need a running event loop on the
        owning thread, and a deployment runs on a thread that has none. So probe
        the endpoint directly AND drain the child's stderr pipe each tick, so
        the real llama.cpp output (load progress or the fatal error) reaches the
        log instead of being lost.

        Outcomes: ("ready", "", log) | ("failed", why, log) | ("timeout", "", log)

        The child is deliberately left running on timeout: MainWindow's main
        event loop still owns it, so a slow model may finish loading and be
        adopted by a later call rather than being killed and re-spawned.
        """
        gui = self._on_gui_thread()
        proc = None
        if gui and self.runtime is not None:
            proc = self.runtime.process
        child_tid = proc.threadId() if proc is not None else 0

        # Raw stderr handle from the QProcess (an int OS file descriptor). Read
        # non-blocking each tick; never via the Qt readyRead signal, which is
        # dead on this thread.
        err_fd = None
        if proc is not None:
            try:
                err_fd = int(proc.processChannel(proc.StandardError))
            except Exception:
                err_fd = None

        captured: List[str] = []

        def _drain_stderr() -> None:
            if err_fd is None:
                return
            text = _nonblocking_read_fd(err_fd)
            if not text:
                return
            for ln in text.splitlines():
                ln = ln.rstrip()
                if ln:
                    captured.append(ln)
                    _dlog(f"subagent llama-server> {ln}")

        def _child_is_dead() -> bool:
            """Whether the child we launched is gone. Prefer a plain PID check
            over the Qt state() call, which is both unreliable and unsafe from a
            thread that does not own the QProcess.

            A child that no longer exists is *not* evidence of a crash: when the
            server was started on the GUI thread and this wait runs on a worker
            thread, `proc` stays None by design, so there is nothing to check
            and we keep polling the endpoint until the deadline."""
            if proc is None:
                return False
            if child_tid and os.name == "posix":
                try:
                    os.kill(child_tid, 0)
                    return False
                except ProcessLookupError:
                    return True
                except PermissionError:
                    return False   # exists, just not ours to signal
                except OSError:
                    return False   # keep waiting on any other OS ambiguity
            try:
                return proc.state() == QProcess.NotRunning
            except Exception:
                return False

        deadline = time.monotonic() + max(5, float(timeout))
        while time.monotonic() < deadline:
            _drain_stderr()
            if probe_endpoint(self.policy.base_url(), timeout=1.5):
                _drain_stderr()
                return ("ready", "", captured[-20:])
            if _child_is_dead():
                _drain_stderr()  # final flush before the pipe closes
                return ("failed", self._diagnose_dead_child(self._last_spawn_argv),
                        captured[-40:])
            time.sleep(0.5)

        _drain_stderr()
        return ("timeout", "", captured[-40:])

    def _wait_for_detached(self, timeout: float) -> Tuple[str, str, List[str]]:
        """Readiness wait for a detached (non-Qt) child process. Same three
        outcomes as _wait_until_ready, but liveness is a plain PID check and the
        log comes from the file the child writes to, so no Qt object is touched."""
        pid = self._detached_pid
        deadline = time.monotonic() + max(5, float(timeout))
        while time.monotonic() < deadline:
            if probe_endpoint(self.policy.base_url(), timeout=1.5):
                return ("ready", "", self._read_detached_log_tail(20))
            if pid and not self._child_is_alive(pid):
                return ("failed", self._diagnose_dead_child(self._last_spawn_argv),
                        self._read_detached_log_tail(40))
            time.sleep(0.5)
        return ("timeout", "", self._read_detached_log_tail(40))

    def _await_existing(self, on_log: Optional[Callable[[str], None]] = None,
                        spawn: bool = True, detached: bool = False) -> bool:
        """Shared readiness wait used by the fresh-spawn and still-starting
        paths. Uses an endpoint probe + PID liveness rather than
        runtime.state(), which never updates on a thread without an event loop.

        `spawn=False` means we did not launch the child ourselves in this call,
        so a dead/absent process is not a spawn failure and its stderr is not
        ours to diagnose. `detached=True` means the child is a plain subprocess
        we started (no QProcess), so liveness is a PID check and its output
        comes from the log file."""
        _t0 = time.monotonic()
        timeout = max(30, int(self.policy.startup_timeout))
        _dlog(f"subagent start(): waiting for readiness up to {timeout}s")
        if detached:
            outcome, why, log = self._wait_for_detached(timeout)
        else:
            outcome, why, log = self._wait_until_ready(timeout)

        if outcome == "ready":
            self._last_error = ""
            self._adopted = True
            _dlog(f"subagent start(): READY in {time.monotonic() - _t0:.1f}s")
            # Best-effort: reflect readiness on the runtime object. Safe even
            # off the GUI thread for these plain setters; harmless if not.
            try:
                if self.runtime is not None:
                    self.runtime._set_state("running")
                    self.runtime._set_phase("Ready")
            except Exception:
                pass
            return True

        if outcome == "failed" and spawn:
            self._last_error = why or (
                "The subagent llama-server exited before it became ready."
            )
            _dlog(f"subagent start(): child EXITED after {time.monotonic() - _t0:.1f}s")
            for ln in (log or []):
                _dlog(f"subagent start(): llama-server> {ln}")
            if log:
                self._last_error += "\n\nllama-server output:\n" + "\n".join(log)
            # Drop the dead runtime so the next call starts clean.
            try:
                if self.runtime is not None:
                    self.runtime.stop()
            except Exception:
                pass
            self.runtime = None
            self._detached_alive = False
            return False

        self._last_error = (
            f"The subagent server did not become ready within "
            f"{self.policy.startup_timeout}s. The model may still be loading "
            f"(a CPU-resident model reads the whole file from disk first) or "
            f"this llama.cpp build does not support it. Try a larger 'Model load "
            f"timeout', keep the app open and retry, or check the llama-server "
            f"output for errors."
        )
        _dlog(f"subagent start(): TIMED OUT after {self.policy.startup_timeout}s "
              "(child still alive; leaving it running so a retry can adopt it)")
        return False

    # ---- start / stop ----

    def start(self, on_log: Optional[Callable[[str], None]] = None) -> bool:
        """Start the subagent server and block until it reports ready.

        Blocking is intentional: a deployment is a synchronous call that cannot
        do anything useful until the model is loaded. Returns True on success,
        False (with `last_error()` populated) otherwise — it never raises, so a
        failure here can't propagate into the agent loop.

        Qt constraint: creating a LlamaServerRuntime (a QObject with a QTimer)
        is only legal on the GUI thread. Off the GUI thread this still spawns a
        real server, just as a plain detached subprocess (see _spawn_detached)
        instead of a QProcess."""
        if not self.policy.is_configured():
            self._last_error = (
                "No subagent model configured. Ask the user to set a small "
                "text GGUF under Settings → Backend → Subagent model and tick "
                "'Enable subagent model'."
            )
            _dlog("subagent start(): refused — policy not configured")
            return False

        gui = self._on_gui_thread()
        if not gui and self.runtime is not None:
            # A child was already spawned by an earlier GUI-thread call. Wait on
            # the endpoint from here, touching no Qt object.
            return self._await_existing(on_log, spawn=False)

        if probe_endpoint(self.policy.base_url(), timeout=1.5):
            # Already up (started earlier, or launched manually) — adopt it.
            _dlog(f"subagent start(): {self.policy.base_url()} already answering, adopting")
            if not gui:
                # Worker thread: just record that the endpoint is live. Building
                # LlamaServerRuntime here would be a SIGSEGV (no Qt event loop).
                self._adopted = True
                self._last_error = ""
                return True
            if self.runtime is None:
                self.runtime = LlamaServerRuntime(parent=self, headless=True)
            self.runtime.adopt_external()
            self._adopted = True
            self._last_error = ""
            return True

        # Nothing is listening on our port. Before spawning anything, make sure
        # we are not about to collide with the main server: see
        # _port_collision for why this is checked here rather than left for
        # llama.cpp's "address already in use".
        collision = self._port_collision()
        if collision:
            self._last_error = collision
            _dlog(f"subagent start(): refused — {collision}")
            return False

        if not gui:
            # Nothing is listening and we are on a worker thread. A QProcess
            # cannot be built here, but a plain subprocess can: spawn detached
            # and wait on the endpoint. This is what makes the first deployment
            # self-healing instead of telling the user to go press a button.
            _dlog("subagent start(): off GUI thread with no endpoint — spawning "
                  "detached server via subprocess")
            if not self._prevalidate_model_path():
                return False
            argv = self._render_spawn_argv()
            if argv is None:
                return False
            if self._spawn_detached(argv) is None:
                self._last_error = (
                    "Could not start the subagent llama-server. Check that the "
                    f"server binary exists: {self._binary_for_spawn()!r}"
                )
                _dlog(f"subagent start(): FAILED — {self._last_error}")
                return False
            self._detached_alive = True
            self._spawned_signature = self.policy.signature()
            return self._await_existing(on_log, spawn=True, detached=True)

        # A previous attempt is still loading (state stuck at "starting" from the
        # old event-loop-dependent poll). Do NOT spawn a second child on the same
        # port — wait on the one we already own instead.
        if self.runtime is not None and self.runtime.state() == "starting":
            _dlog("subagent start(): a previous spawn is still loading — waiting "
                  "on it instead of spawning a second child")
            return self._await_existing(on_log, spawn=False)

        # A managed (QProcess) child of ours is alive on policy.port — the normal
        # state after pressing "Start Server", which starts the main model AND
        # the subagent model. /models stays silent while it is still loading, so
        # the probe above missed it and the detached-PID guard below cannot see
        # it either. Spawning anyway would put a second llama-server on a port we
        # already own, and the readiness wait could then be satisfied by whichever
        # server answered first rather than by our own model.
        if self._detached_alive:
            _dlog(f"subagent start(): a managed child we spawned is still alive on "
                  f"port {self.policy.port} — waiting on it instead of spawning a "
                  "second server on the same port")
            return self._await_existing(on_log, spawn=False, detached=True)

        # Same guard for a server launched detached earlier in this session.
        if self._detached_pid and self._child_is_alive(self._detached_pid):
            _dlog(f"subagent start(): detached child pid={self._detached_pid} still "
                  "loading — waiting on it instead of spawning a second child")
            return self._await_existing(on_log, spawn=False, detached=True)

        if not self._prevalidate_model_path():
            return False

        cfg = self.build_server_config()
        _dlog(f"subagent start(): spawning server. binary={cfg.binary_path!r} "
              f"model={cfg.model_path!r} port={cfg.port} ctx={cfg.context_size} "
              f"ngl={cfg.gpu_layers} device={self.policy.device}")
        try:
            argv = [cfg.binary_path] + cfg.build_args()
            self._last_spawn_argv = list(argv)
            _dlog("subagent start(): full argv = " + " ".join(argv))
        except Exception as _e:  # build_args must never break the log path
            self._last_spawn_argv = []
            _dlog(f"subagent start(): could not render argv: {_e}")

        # Tear down any previous child BEFORE replacing the reference: this
        # object is not a QObject, so nothing else stops the old QProcess, and
        # letting it be garbage-collected while its llama-server is still running
        # prints "QProcess: Destroyed while process ... is still running" and
        # leaves an orphan holding the port.
        if self.runtime is not None:
            try:
                self.runtime.stop()
            except Exception:
                pass
            self.runtime = None

        # parent=self keeps the runtime (and its QProcess) alive as long as this
        # SubagentServerRuntime does. Reached only on the GUI thread.
        runtime = LlamaServerRuntime(parent=self)
        if on_log is not None:
            runtime.log_line.connect(on_log)
        self.runtime = runtime
        # Mark this child as ours *before* starting it, so a concurrent or later
        # start() — on any thread, where runtime.state() never advances for want
        # of an event loop — sees that policy.port is already claimed by a live
        # child of ours and waits rather than binding a second llama-server to it.
        self._detached_alive = True
        self._spawned_signature = self.policy.signature()
        runtime.finished.connect(self._on_managed_child_finished)
        runtime.start(cfg)
        return self._await_existing(on_log, spawn=True)

    def _on_managed_child_finished(self, *_args) -> None:
        """Managed child exited: it no longer holds policy.port, so a later call
        may spawn a replacement. Connected to the runtime's `finished` signal,
        which fires on the thread owning the QProcess."""
        self._detached_alive = False
        self._spawned_signature = None

    def spawned_signature(self) -> Optional[tuple]:
        """The command-line signature of the child WE started, or None if the
        current endpoint is adopted / nothing is running. Callers use it to
        decide whether a settings change requires a restart (see
        SubagentPolicy.signature)."""
        return self._spawned_signature

    def stop(self):
        self._adopted = False
        self._detached_alive = False
        self._spawned_signature = None
        self._terminate_detached()
        if self.runtime is not None:
            try:
                self.runtime.stop()
            except Exception:
                pass
            self.runtime = None


# --------------------------------------------------------------------------
# Off-UI-thread startup helper
# --------------------------------------------------------------------------

class SubagentStartWorker(QThread):
    """Runs SubagentServerRuntime.start() off the GUI thread.

    The model load blocks for as long as startup_timeout seconds, so it cannot
    run in a button handler. The deployment path does not need this: it already
    executes on AgentWorker's thread.
    """

    finished_start = Signal(bool, str)  # (ok, message)

    def __init__(self, runtime: SubagentServerRuntime,
                 log_fn: Optional[Callable[[str], None]] = None, parent=None):
        super().__init__(parent)
        self._runtime = runtime
        self._log_fn = log_fn

    def run(self):
        """Runs the blocking startup off the GUI thread.

        start() detects that it is not on the GUI thread and confines itself to
        plain `requests` probes plus, when nothing is listening, a detached
        plain-subprocess spawn — never a LlamaServerRuntime (QObject + QTimer +
        Signals), which may only be created on the GUI thread."""
        try:
            ok = self._runtime.start(on_log=self._log_fn)
            msg = "" if ok else (self._runtime.last_error() or
                                 "subagent server failed to start")
        except Exception as e:  # never let a startup crash propagate as a Qt warning
            ok, msg = False, f"subagent server startup raised {type(e).__name__}: {e}"
        self.finished_start.emit(ok, msg)
