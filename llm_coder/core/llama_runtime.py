"""
QProcess-based manager for a local llama-server process, plus config for
connecting to an already-running OpenAI-compatible endpoint instead.

ServerConfig mirrors the flags exposed by `llama-server --help` on build
b06aa774c (version 10287). Only non-default values are emitted as CLI args,
matching the original style, so a fresh ServerConfig() produces a minimal
command line and every field with a value different from the llama.cpp
default gets passed through explicitly.
"""
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any
from PySide6.QtCore import QObject, QProcess, Signal, QTimer
import requests
import re


KV_CACHE_TYPES = ["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"]
FLASH_ATTN_MODES = ["auto", "on", "off"]
NUMA_MODES = ["", "distribute", "isolate", "numactl"]
LOAD_MODES = ["mmap", "none", "mlock", "mmap+mlock", "dio"]
SPLIT_MODES = ["layer", "none", "row", "tensor"]
ROPE_SCALING_MODES = ["", "none", "linear", "yarn"]
REASONING_FORMAT_OPTIONS = ["auto", "none", "deepseek", "deepseek-legacy"]
REASONING_MODES = ["auto", "on", "off"]

# --spec-type accepts a comma-separated list of these (default: none).
SPEC_TYPE_OPTIONS = [
    "none",
    "draft-simple",   # generic external draft model (requires -md)
    "draft-mtp",      # built-in MTP heads (no separate model needed)
    "draft-eagle3",   # EAGLE-3 external draft model (requires -md)
    "draft-dflash",   # DFlash block diffusion (requires -md)
    "draft-dspark",   # DSpark semi-autoregressive (requires -md)
    "ngram-cache",
    "ngram-simple",
    "ngram-map-k",
    "ngram-map-k4v",
    "ngram-mod",
]

# Speculative types that can work without an external draft model (-md).
# NOTE: "draft-mtp" is *capable* of running without -md, but only when the MTP
# tensors live inside the same GGUF passed to -m. Several releases ship the MTP
# head as a separate sidecar GGUF (e.g. unsloth's Qwen3.8-Flash-Next and
# Qwen3.6, under an MTP/ subfolder), which llama.cpp does NOT auto-discover --
# for those, -md is mandatory even though the type is draft-mtp. Hence this set
# is only advisory ("does not *require* one"), never a gate on emitting -md.
SPEC_TYPES_NO_DRAFT_MODEL = {
    "none", "draft-mtp", "ngram-cache", "ngram-simple",
    "ngram-map-k", "ngram-map-k4v", "ngram-mod",
}


@dataclass
class ServerConfig:
    managed: bool = True
    binary_path: str = "llama-server"
    model_path: str = ""
    mmproj_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    context_size: int = 32768
    extra_args: str = "--jinja"
    external_base_url: str = "http://127.0.0.1:8080/v1"
    gpu_layers: int = -1
    kv_cache_type_k: str = "f16"
    kv_cache_type_v: str = "f16"
    flash_attn: str = "auto"  # "auto" | "on" | "off"

    # ---- speculative decoding (MTP / external draft models / n-gram) ----
    speculative_type: str = "none"  # comma-joined subset of SPEC_TYPE_OPTIONS
    speculative_draft_n_max: int = 3
    speculative_draft_n_min: int = 0
    speculative_draft_model: str = ""     # -md, for draft-simple/eagle3/dflash/dspark
    speculative_draft_hf: str = ""        # --spec-draft-hf, alternative to -md

    # ---- device / offload placement ----
    device: str = ""              # -dev, comma-separated device list
    split_mode: str = "layer"     # -sm
    tensor_split: str = ""        # -ts, comma-separated proportions
    override_tensor: str = ""     # -ot, tensor-name=buffer-type pattern
    cpu_moe: bool = False         # -cmoe, keep ALL MoE weights on CPU
    n_cpu_moe: int = -1           # -ncmoe, -1 = not passed

    # ---- memory / loading ----
    load_mode: str = "mmap"       # -lm: none | mmap | mlock | mmap+mlock | dio
    no_kv_offload: bool = False   # -nkvo

    # ---- threads / batching ----
    numa_mode: str = ""
    threads: int = -1
    threads_batch: int = -1
    batch_size: int = -1
    ubatch_size: int = -1
    parallel_slots: int = -1      # -np, -1 = auto
    cont_batching: bool = True    # -cb (llama.cpp default: enabled)

    # ---- context behavior ----
    n_predict: int = -1           # -n, -1 = infinity (server-side default)
    keep_tokens: int = 0          # --keep
    context_shift: bool = False  # --context-shift (default: disabled)
    rope_scaling: str = ""       # {none, linear, yarn}
    rope_scale: float = 0.0      # 0 = unset
    rope_freq_base: float = 0.0
    rope_freq_scale: float = 0.0
    yarn_orig_ctx: int = 0

    # ---- reasoning ----
    reasoning_preserve: bool = False
    reasoning_format: str = "auto"   # none | deepseek | deepseek-legacy | auto
    reasoning_mode: str = "auto"     # on | off | auto (-rea)
    reasoning_budget: int = -1       # -1 = unrestricted
    # Message injected before the end-of-thinking tag when the budget is
    # exhausted. Only meaningful when reasoning_budget >= 0. The default is a
    # short, neutral transition that nudges the model to answer from what it
    # has already reasoned about, rather than a jarring cutoff.
    reasoning_budget_message: str = "I've reached the limit for thinking, so I'll stop here and answer based on what I've reasoned so far."

    # ---- sampling defaults (server-side defaults for requests w/o overrides) ----
    temperature: float = 0.80
    top_k: int = 40
    top_p: float = 0.95
    min_p: float = 0.05
    typical_p: float = 1.00
    repeat_last_n: int = 64
    repeat_penalty: float = 1.00
    presence_penalty: float = 0.00
    frequency_penalty: float = 0.00
    dry_multiplier: float = 0.00
    dry_base: float = 1.75
    dry_allowed_length: int = 2
    dry_penalty_last_n: int = 64
    mirostat: int = 0             # 0=off, 1=Mirostat, 2=Mirostat 2.0
    mirostat_lr: float = 0.10
    mirostat_ent: float = 5.00
    seed: int = -1

    # ---- LoRA adapters: [{"path": str, "scale": float}, ...] ----
    lora_adapters: List[Dict[str, Any]] = field(default_factory=list)

    # ---- security / server exposure ----
    api_key: str = ""
    api_key_file: str = ""
    cors_origins: str = "*"
    enable_metrics: bool = False        # --metrics (prometheus endpoint)
    disable_webui: bool = False         # --no-webui
    disable_slots_endpoint: bool = False  # --no-slots

    def base_url(self) -> str:
        if self.managed:
            return f"http://{self.host}:{self.port}/v1"
        return self.external_base_url.rstrip("/")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ServerConfig":
        base = ServerConfig()
        for k, v in d.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base

    def build_args(self) -> list:
        args = [
            "-m", self.model_path,
            "--host", self.host,
            "--port", str(self.port),
            "-c", str(self.context_size),
        ]
        if self.mmproj_path.strip():
            args += ["--mmproj", self.mmproj_path.strip()]

        # ---- GPU / device placement ----
        if self.gpu_layers is not None and self.gpu_layers >= 0:
            args += ["-ngl", str(self.gpu_layers)]
        if self.device.strip():
            args += ["--device", self.device.strip()]
        if self.split_mode != "layer":
            args += ["--split-mode", self.split_mode]
        if self.tensor_split.strip():
            args += ["--tensor-split", self.tensor_split.strip()]
        if self.override_tensor.strip():
            args += ["--override-tensor", self.override_tensor.strip()]
        if self.cpu_moe:
            args += ["--cpu-moe"]
        if self.n_cpu_moe is not None and self.n_cpu_moe >= 0:
            args += ["--n-cpu-moe", str(self.n_cpu_moe)]

        # ---- KV cache / flash attention ----
        if self.kv_cache_type_k != "f16":
            args += ["--cache-type-k", self.kv_cache_type_k]
        if self.kv_cache_type_v != "f16":
            args += ["--cache-type-v", self.kv_cache_type_v]
        if self.flash_attn != "auto":
            args += ["--flash-attn", self.flash_attn]
        if self.no_kv_offload:
            args += ["--no-kv-offload"]

        # ---- memory / loading ----
        if self.load_mode != "mmap":
            args += ["-lm", self.load_mode]
        if self.numa_mode:
            args += ["--numa", self.numa_mode]

        # ---- threads / batching ----
        if self.threads is not None and self.threads > 0:
            args += ["--threads", str(self.threads)]
        if self.threads_batch is not None and self.threads_batch > 0:
            args += ["--threads-batch", str(self.threads_batch)]
        if self.batch_size is not None and self.batch_size > 0:
            args += ["--batch-size", str(self.batch_size)]
        if self.ubatch_size is not None and self.ubatch_size > 0:
            args += ["--ubatch-size", str(self.ubatch_size)]
        if self.parallel_slots is not None and self.parallel_slots > 0:
            args += ["--parallel", str(self.parallel_slots)]
        if not self.cont_batching:
            args += ["--no-cont-batching"]

        # ---- context behavior ----
        if self.n_predict is not None and self.n_predict != -1:
            args += ["-n", str(self.n_predict)]
        if self.keep_tokens:
            args += ["--keep", str(self.keep_tokens)]
        if self.context_shift:
            args += ["--context-shift"]
        if self.rope_scaling:
            args += ["--rope-scaling", self.rope_scaling]
        if self.rope_scale:
            args += ["--rope-scale", str(self.rope_scale)]
        if self.rope_freq_base:
            args += ["--rope-freq-base", str(self.rope_freq_base)]
        if self.rope_freq_scale:
            args += ["--rope-freq-scale", str(self.rope_freq_scale)]
        if self.yarn_orig_ctx:
            args += ["--yarn-orig-ctx", str(self.yarn_orig_ctx)]

        # ---- reasoning ----
        if self.reasoning_preserve:
            args += ["--reasoning-preserve"]
        if self.reasoning_format != "auto":
            args += ["--reasoning-format", self.reasoning_format]
        if self.reasoning_mode != "auto":
            args += ["--reasoning", self.reasoning_mode]
        if self.reasoning_budget is not None and self.reasoning_budget != -1:
            args += ["--reasoning-budget", str(self.reasoning_budget)]
        # Only emit the budget-exhaustion message when it's set. A blank value
        # is treated as "use llama.cpp's built-in default (none)".
        if (self.reasoning_budget_message or "").strip():
            args += ["--reasoning-budget-message", self.reasoning_budget_message.strip()]

        # ---- sampling defaults ----
        if abs(self.temperature - 0.80) > 1e-9:
            args += ["--temp", str(self.temperature)]
        if self.top_k != 40:
            args += ["--top-k", str(self.top_k)]
        if abs(self.top_p - 0.95) > 1e-9:
            args += ["--top-p", str(self.top_p)]
        if abs(self.min_p - 0.05) > 1e-9:
            args += ["--min-p", str(self.min_p)]
        if abs(self.typical_p - 1.00) > 1e-9:
            args += ["--typical", str(self.typical_p)]
        if self.repeat_last_n != 64:
            args += ["--repeat-last-n", str(self.repeat_last_n)]
        if abs(self.repeat_penalty - 1.00) > 1e-9:
            args += ["--repeat-penalty", str(self.repeat_penalty)]
        if abs(self.presence_penalty) > 1e-9:
            args += ["--presence-penalty", str(self.presence_penalty)]
        if abs(self.frequency_penalty) > 1e-9:
            args += ["--frequency-penalty", str(self.frequency_penalty)]
        if abs(self.dry_multiplier) > 1e-9:
            args += ["--dry-multiplier", str(self.dry_multiplier)]
            if abs(self.dry_base - 1.75) > 1e-9:
                args += ["--dry-base", str(self.dry_base)]
            if self.dry_allowed_length != 2:
                args += ["--dry-allowed-length", str(self.dry_allowed_length)]
            if self.dry_penalty_last_n != 64:
                args += ["--dry-penalty-last-n", str(self.dry_penalty_last_n)]
        if self.mirostat:
            args += ["--mirostat", str(self.mirostat)]
            args += ["--mirostat-lr", str(self.mirostat_lr)]
            args += ["--mirostat-ent", str(self.mirostat_ent)]
        if self.seed != -1:
            args += ["--seed", str(self.seed)]

        # ---- LoRA adapters ----
        if self.lora_adapters:
            parts = []
            for a in self.lora_adapters:
                p = (a.get("path") or "").strip()
                if not p:
                    continue
                scale = a.get("scale", 1.0)
                try:
                    scale = float(scale)
                except (TypeError, ValueError):
                    scale = 1.0
                parts.append(f"{p}:{scale}")
            if parts:
                args += ["--lora-scaled", ",".join(parts)]

        # ---- security / server exposure ----
        if self.api_key.strip():
            args += ["--api-key", self.api_key.strip()]
        if self.api_key_file.strip():
            args += ["--api-key-file", self.api_key_file.strip()]
        if self.cors_origins.strip() and self.cors_origins.strip() != "*":
            args += ["--cors-origins", self.cors_origins.strip()]
        if self.enable_metrics:
            args += ["--metrics"]
        if self.disable_webui:
            args += ["--no-webui"]
        if self.disable_slots_endpoint:
            args += ["--no-slots"]

        # ---- speculative decoding ----
        if self.speculative_type and self.speculative_type != "none":
            args += ["--spec-type", self.speculative_type]
            if self.speculative_draft_n_max > 0:
                args += ["--spec-draft-n-max", str(self.speculative_draft_n_max)]
            if self.speculative_draft_n_min > 0:
                args += ["--spec-draft-n-min", str(self.speculative_draft_n_min)]
            # Emit the draft model whenever the user supplied one, independent
            # of the spec type. Gating this on "needs_draft_model" silently
            # dropped -md for --spec-type draft-mtp, which breaks every model
            # whose MTP head ships as a separate sidecar GGUF (llama.cpp does
            # not auto-discover sidecars in subfolders, and quant variants such
            # as lmstudio-community's do not embed the MTP tensors at all).
            if self.speculative_draft_model.strip():
                args += ["--spec-draft-model", self.speculative_draft_model.strip()]
            hf_val = self.speculative_draft_hf.strip()
            if hf_val:
                args += ["--spec-draft-hf=" + hf_val]

        if self.extra_args.strip():
            args += self.extra_args.strip().split()
        return args


def probe_endpoint(base_url: str, timeout: float = 2.0) -> bool:
    """Returns True if something is already responding at this OpenAI-compatible base URL."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def list_devices(binary_path: str, timeout: float = 10.0) -> str:
    """Run `llama-server --list-devices` synchronously and return its output.

    Used by the Settings dialog's "Detect devices" button. Raises on failure
    so the caller can show the error directly to the user.
    """
    import subprocess
    result = subprocess.run(
        [binary_path, "--list-devices"],
        capture_output=True, text=True, timeout=timeout,
    )
    return (result.stdout or "") + (result.stderr or "")


# Regex to parse llama.cpp performance log lines like:
# "llama_perf_context: .............. perf =  61.74 token/s ... prompt_per_second = 61.74 ..."
_LLAMA_PERF_RE = re.compile(
    r"perf\s*=\s*([\d.]+)\s*token/s"
)
_LLAMA_TIMING_FIELD_RE = re.compile(
    r"(prompt_per_second|predicted_per_second|prompt_per_token_ms|predicted_per_token_ms|prompt_ms|predicted_ms)\s*=\s*([\d.]+)"
)
# Regex to parse llama.cpp live prompt-processing progress lines, e.g.
# "slot print_timing: id  0 | task 56 | prompt processing, n_tokens =  91424, progress = 0.73, t = 131.16 s / 697.05 tokens per second"
_LLAMA_PROMPT_PROGRESS_RE = re.compile(
    r"prompt processing\s*,\s*n_tokens\s*=\s*(\d+)\s*,\s*progress\s*=\s*([\d.]+)"
)


class LlamaServerRuntime(QObject):
    log_line = Signal(str)
    state_changed = Signal(str)  # "stopped" | "starting" | "running" | "error"
    starting_phase = Signal(str)
    perf_metrics = Signal(dict)
    prompt_progress = Signal(int, float)  # n_tokens_processed, progress_fraction (0.0-1.0)

    _PHASE_PATTERNS = [
        (re.compile(r"loading model", re.IGNORECASE), lambda m: "Loading model..."),
        (re.compile(r"offloading.*layers?\s+to\s+GPU", re.IGNORECASE), lambda m: "Offloading to GPU..."),
        (re.compile(r"offloading.*non.?repeating\s+layers?\s+to\s+GPU", re.IGNORECASE), lambda m: "Offloading non-repeating layers to GPU..."),
        (re.compile(r"llama_new_context_with_model", re.IGNORECASE), lambda m: "Preparing KV cache..."),
        (re.compile(r"model\s+loaded", re.IGNORECASE), lambda m: "Model loaded"),
        (re.compile(r"listening\s+on", re.IGNORECASE), lambda m: "Listening on port..."),
        (re.compile(r"server\s+is\s+entering\s+sleeping", re.IGNORECASE), lambda m: "Server entering sleep mode..."),
        (re.compile(r"server\s+is\s+exiting\s+sleeping", re.IGNORECASE), lambda m: "Server waking up..."),
    ]

    def __init__(self, parent=None, headless: bool = False):
        """`parent` is the owning QObject. Pass one whenever this runtime is
        created on the GUI thread: it keeps the object (and the QProcess it
        owns) alive for the owner's whole lifetime, so a re-spawn can't let
        Python garbage-collect a QProcess whose child is still running
        ("QProcess: Destroyed while process ... is still running").

        `headless=True` skips creating the readiness QTimer. Qt6 refuses
        timers on a thread without a running QEventLoop, and PySide6 aborts
        the process (SIGSEGV) when such a timer is touched or destroyed from
        another thread. Anything that only needs to *track* an endpoint that
        is already up — see `adopt_external()` — must be built headless, and
        must be built on the GUI thread anyway."""
        super().__init__(parent)
        self.process: Optional[QProcess] = None
        self._state = "stopped"
        self._starting_phase = "Initializing..."
        self._headless = bool(headless)
        self._poll_timer: Optional[QTimer] = None
        if not self._headless:
            self._poll_timer = QTimer()
            self._poll_timer.setInterval(500)
            self._poll_timer.timeout.connect(self._poll_endpoint)
        self._model_path = ""
        self._poll_base_url = "http://127.0.0.1:8080/v1"

    def state(self) -> str:
        return self._state

    def _set_state(self, s: str):
        self._state = s
        self.state_changed.emit(s)

    def _stop_poll_timer(self):
        """Stop the readiness poll if one exists. A headless runtime never
        owns a QTimer, so every call site goes through here."""
        if self._poll_timer is not None:
            self._poll_timer.stop()

    def _set_phase(self, phase: str):
        self._starting_phase = phase
        self.starting_phase.emit(phase)

    def start(self, config: ServerConfig):
        if not config.managed:
            self._set_state("running")
            self._set_phase("Ready")
            self.log_line.emit("External endpoint mode: not starting a local process.")
            return

        if self.process is not None:
            self.stop()

        self._model_path = config.model_path or "(no model path set)"
        self._poll_base_url = config.base_url()
        self._set_phase(f"Loading {_short_model_name(self._model_path)}...")

        self.process = QProcess()
        args = config.build_args()

        self.process.setProgram(config.binary_path)
        self.process.setArguments(args)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.started.connect(self._on_process_started)
        self.process.errorOccurred.connect(self._on_error)
        self.process.finished.connect(self._on_process_finished)

        self._set_state("starting")
        self.log_line.emit(f"Starting: {config.binary_path} {' '.join(args)}")
        self.process.start()

    def _on_process_started(self):
        self.log_line.emit("Process started — waiting for server to be ready...")
        if self._poll_timer is not None:
            self._poll_timer.start()

    def _on_process_finished(self, code, status):
        if self.process is None:
            return
        was_starting = self._state == "starting"
        self.process = None
        if was_starting:
            self._stop_poll_timer()
            self._set_state("error")
            self._set_phase("Server process exited")
            self.log_line.emit("Server process exited before becoming ready.")
        elif self._state == "running":
            self._stop_poll_timer()
            self._set_state("stopped")
            self._set_phase("Stopped")

    def _poll_endpoint(self):
        if self._poll_timer is None:
            return
        if self._state != "starting" or self.process is None:
            self._poll_timer.stop()
            return
        try:
            resp = requests.get(f"{self.base_url_for_polling()}/models", timeout=2.0)
            if resp.status_code == 200:
                self._poll_timer.stop()
                self._set_state("running")
                self._set_phase("Ready")
                self.log_line.emit("Server is ready and responding.")
                return
        except Exception:
            pass
        if self.process is not None and self.process.state() == QProcess.NotRunning:
            self._poll_timer.stop()
            self._set_state("error")
            self._set_phase("Server process exited")
            self.log_line.emit("Server process exited before becoming ready.")

    def base_url_for_polling(self) -> str:
        return self._poll_base_url

    def adopt_external(self):
        self._stop_poll_timer()
        self._set_state("running")
        self._set_phase("Ready")
        self.log_line.emit("Attached to already-running server (not managed by this app).")

    def stop(self):
        self._stop_poll_timer()
        if self.process is not None:
            self.process.terminate()
            if not self.process.waitForFinished(3000):
                self.process.kill()
            self.process = None
        self._set_state("stopped")
        self._set_phase("Stopped")

    def _on_stdout(self):
        if self.process is None:
            return
        data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            self._handle_log_line(line)

    def _on_stderr(self):
        if self.process is None:
            return
        data = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            self._handle_log_line(line)

    def _handle_log_line(self, line: str):
        self.log_line.emit(line)
        perf = self._parse_perf_metrics(line)
        if perf:
            self.perf_metrics.emit(perf)
        progress = self._parse_prompt_progress(line)
        if progress:
            self.prompt_progress.emit(*progress)
        if self._state != "starting":
            return
        new_phase = self._detect_phase(line)
        if new_phase:
            self._set_phase(new_phase)

    def _parse_perf_metrics(self, line: str) -> Optional[dict]:
        metrics = {}
        m = _LLAMA_PERF_RE.search(line)
        if m:
            metrics["token_s"] = float(m.group(1))
        for fm in _LLAMA_TIMING_FIELD_RE.finditer(line):
            metrics[fm.group(1)] = float(fm.group(2))
        return metrics if metrics else None

    def _parse_prompt_progress(self, line: str) -> Optional[tuple]:
        """Return (n_tokens, progress_fraction) when a live prompt-processing
        progress line is detected, else None. Only fires while the server is
        actually up (i.e. not during the initial startup phase, where these
        lines would be meaningless)."""
        if self._state != "running":
            return None
        m = _LLAMA_PROMPT_PROGRESS_RE.search(line)
        if m:
            try:
                return int(m.group(1)), float(m.group(2))
            except (TypeError, ValueError):
                return None
        return None

    def _detect_phase(self, line: str) -> Optional[str]:
        for pattern, formatter in self._PHASE_PATTERNS:
            if pattern.search(line):
                return formatter(line)
        return None

    def _on_error(self, error):
        self._stop_poll_timer()
        self._set_state("error")
        self._set_phase("Process error")
        self.log_line.emit(f"Process error: {error}")


def _short_model_name(path: str) -> str:
    if not path:
        return "model"
    import os
    return os.path.basename(path) or "model"
