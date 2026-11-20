"""Image generation AND editing through stable-diffusion.cpp — deliberately Qwen only.

Hearth's chat model cannot draw. This module lets it *ask* for a picture by running
`sd-cli` (from stable-diffusion.cpp) as a subprocess and handing the resulting PNG back
into the workspace, where the ordinary artifact machinery lists and downloads it.

Two **profiles** share all of the machinery here — one runner, one lock, one log, one set
of cancel/kill code — and differ only in which settings resolve their files, which roles
they require, and what sampling defaults they pin:

  * ``PROFILE_GEN`` — text-to-image with **Qwen Image / Qwen Image 2.1** (settings prefix
    ``diffusion.*``). Three required files; CFG ~4.5.
  * ``PROFILE_EDIT`` — instruction-based editing with **Qwen Image Edit** (prefix
    ``image_edit.*``). Same three-file shape, but the reference image goes to sd-cli as
    ``-r`` (reference conditioning through the VLM + VAE encoder), not ``-i`` (img2img noise
    perturbation), and sampling is pinned lower — CFG ~2.5 with ``--flow-shift 3`` — because
    that is what the Edit checkpoints are tuned for. Its text encoder is Qwen2.5-VL rather
    than the Qwen3-VL that generation uses, so the edit profile has its own file slots; a
    fourth optional ``llm_vision`` slot covers the 2509 checkpoint's separate vision mmproj.

The two are configured independently on purpose: an operator may run only one, and the Edit
weights and text encoder differ from the generation ones even though the CLI binary is the
same. The runner still serialises them — they share one machine and one GPU, so a generation
and an edit must not overlap any more than two generations may.

Scope stays on purpose: **Qwen Image / Qwen Image 2.1 for drawing, Qwen Image Edit for
editing, and nothing else.** An earlier version tried to speak for SD1.5, SDXL, Flux and SD3
too — a `--model` flag plus optional clip_l/clip_g/t5xxl encoders — and that breadth was the
bug rather than a limitation:

  * Qwen Image does not take `-m/--model`. Its diffusion weights go through
    ``--diffusion-model``, its text encoder is an LLM (Qwen3-VL) through ``--llm``, and
    its decoder is a standalone ``--vae`` (a ``.safetensors`` file, not a .gguf). None of
    those three existed in the old flag set except the VAE, so this model could never be
    launched correctly no matter how the settings were filled in.
  * Passing the diffusion .gguf through ``--model`` fails with
    ``get sd version from file failed`` — it looks like a broken model, not a wrong flag.
  * A missing ``--llm`` or ``--vae`` does not say so: sd-cli prints hundreds of
    ``VAE tensor '…' not in model metadata`` lines and exits 1. Config that cannot work
    looked mysterious, which is how this feature rotted unnoticed.

So the three files are required here, checked before anything launches, with a message
naming the setting to fix; sampling flags are pinned to what works for Qwen Image
(euler, flash attention on the diffusion model, no step previews); and the admin UI asks
for one folder plus an optional override of each of the three files. Supporting another
architecture means adding a new backend module — not widening this one.

Design constraints inherited from hearth/captioner.py (DEC-056) where the analogy holds:

  * Nothing is assumed installed. Until `diffusion.enabled` is on and all three files
    resolve, `preflight()` names the exact setting to fix and no tool pretends the
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

# The three files Qwen Image needs, in the order the UI lists them. Each entry is
# (role, human name, sd-cli flag); `role` keys resolve_paths()/status() and matches the
# `<prefix>.<role>_path` setting, so a rename here cannot silently desync argv from UI.
MODEL_ROLES = (
    ("dit", "Diffusion model (.gguf)", "--diffusion-model"),
    ("llm", "Text-encoder LLM (.gguf)", "--llm"),
    ("vae", "VAE (.safetensors)", "--vae"),
)

# Qwen Image Edit uses the same three files, PLUS an optional fourth: some checkpoints
# (2509) ship a separate vision encoder passed as `--llm_vision`. It is optional because
# the original Qwen-Image-Edit and 2511 fold vision into the single `--llm` file — requiring
# it would refuse setups that work, which is the opposite of the lesson in DEC-074. The
# role's flag is only emitted when the path resolves, so omitting it changes nothing for
# checkpoints that do not need it.
EDIT_MODEL_ROLES = (
    ("dit", "Edit diffusion model (.gguf)", "--diffusion-model"),
    ("llm", "Text-encoder LLM (.gguf)", "--llm"),
    ("vae", "VAE (.safetensors)", "--vae"),
    ("llm_vision", "Vision encoder mmproj (optional, 2509)", "--llm_vision"),
)

# Which roles MUST be present before anything launches. `llm_vision` is deliberately absent:
# it is a per-checkpoint extra, not a universal requirement.
EDIT_REQUIRED_ROLES = ("dit", "llm", "vae")

# File-name fingerprints used to auto-discover the files inside one folder, in the order
# they are tried, keyed by profile prefix. Patterns for a role must not match another role's
# file: `qwen2` (not `qwen`) keeps the Qwen3-VL encoder from claiming the diffusion .gguf,
# and the VAE is unambiguous because it is the only .safetensors in the folder. For the edit
# folder the DiT patterns lead on `edit` so an Edit .gguf is found by name; a generation
# folder's names never carry `edit`, which keeps the two profiles from grabbing each other's
# weights even if an admin points both folders at the same place.
_DISCOVERY_GEN = (
    ("dit", ("diffusion_model", "diffusion-model", "qwen_image", "qwen-image", "qwen_image_2")),
    ("llm", ("qwenvl", "qwen3vl", "qwen2vl", "qwenvision", "llm")),
    ("vae", ("diffusion_pytorch_model", "ae.safetensors", "ae-f16", "ae_fp", "vae")),
)

_DISCOVERY_EDIT = (
    # `edit` first: an Edit DiT is named ...Edit...; a plain qwen_image DiT should not be
    # picked up as the edit model, so the edit patterns are stricter than the generation ones.
    ("dit", ("edit", "diffusion_model", "diffusion-model")),
    ("llm_vision", ("mmproj", "vision", "visual", "vit")),
    # The Qwen2.5-VL encoder names vary widely: qwen2.5-vl, qwen_2.5_vl_7b, Qwen2.5VL, etc.
    # `vl` alone is the reliable marker (the DiT never contains it and vision already claimed
    # mmproj files), with `2.5`/`25`/`instruct` as narrower hints tried first.
    ("llm", ("qwen2.5-vl", "qwen2.5_vl", "qwen25vl", "qwen_2.5_vl", "2.5-vl", "vl-7b",
             "vl_7b", "instruct", "vl", "llm")),
    ("vae", ("diffusion_pytorch_model", "ae.safetensors", "ae-f16", "ae_fp", "vae")),
)



class Profile:
    """One Qwen Image backend: which settings resolve its files, and how it samples.

    Two instances exist (see PROFILE_GEN / PROFILE_EDIT). Everything generation-specific in
    this module — the setting-key prefix, env-var names, required roles, discovery patterns,
    and the pinned sampling defaults — lives here, so adding a second profile was a matter
    of declaring one more Profile rather than duplicating the runner. `prefix` keys both the
    `<prefix>.*` settings and the identity used in status()/logs; `env_prefix` builds the
    `HEARTH_<env_prefix>_*` fallbacks.
    """

    def __init__(self, name, label, prefix, env_prefix, roles, discovery, *,
                 required_roles=None, legacy=None, cfg_default=4.5, flow_shift=None,
                 zero_cond_t=False, needs_image=False):
        self.name = name                    # "gen" | "edit" — used in status/logs
        self.label = label                  # human-facing ("Qwen Image", "Qwen Image Edit")
        self.prefix = prefix                # settings prefix: "diffusion" | "image_edit"
        self.env_prefix = env_prefix        # "HEARTH_DIFFUSION" | "HEARTH_IMAGE_EDIT"
        self.roles = roles                  # ordered (role, label, flag) tuples
        self.discovery = discovery          # ordered (role, patterns) for folder auto-find
        # Roles that must resolve before launching. Defaults to every declared role; edit
        # overrides this so its optional llm_vision is not demanded.
        self.required_roles = tuple(required_roles) if required_roles else tuple(
            r for r, _l, _f in roles)
        # Legacy setting keys that still feed a role (older installs), by role.
        self.legacy = legacy or {}
        self.cfg_default = cfg_default      # pinned CFG when neither caller nor setting gives one
        self.flow_shift = flow_shift        # int → emit `--flow-shift N`; None → omit
        self.zero_cond_t = zero_cond_t      # True → emit `--model-args qwen_image_zero_cond_t=true`
        self.needs_image = needs_image      # edit: a reference image is required


# Text-to-image (Qwen Image / 2.1). Everything here matches the historical behaviour of this
# module exactly — same prefix, same CFG default, no flow-shift, no zero_cond_t — so existing
# settings, tests, and generated commands are unchanged by the refactor.
PROFILE_GEN = Profile(
    "gen", "Qwen Image", "diffusion", "HEARTH_DIFFUSION", MODEL_ROLES, _DISCOVERY_GEN,
    legacy={"dit": ("diffusion.model_path",), "vae": ("diffusion.vae",)},
    cfg_default=4.5,
)

# Instruction-based editing (Qwen Image Edit). Same binary and runner; different file slots,
# a lower CFG the Edit checkpoints expect, `--flow-shift 3`, and an input image that goes in
# as `-r` (reference) rather than `-i` (img2img). See docs/models/qwen_image_edit.md upstream.
PROFILE_EDIT = Profile(
    "edit", "Qwen Image Edit", "image_edit", "HEARTH_IMAGE_EDIT", EDIT_MODEL_ROLES,
    _DISCOVERY_EDIT, required_roles=EDIT_REQUIRED_ROLES, cfg_default=2.5, flow_shift=3,
    needs_image=True,
)

PROFILES = {PROFILE_GEN.name: PROFILE_GEN, PROFILE_EDIT.name: PROFILE_EDIT}


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

def _discover(folder: str, profile: "Profile" = None) -> dict:
    """Best guess at a profile's model files inside `folder`. Never raises.

    Explicit paths always win over this — discovery only fills gaps — so a folder whose
    files are named unusually is a naming problem the admin can solve with explicit paths
    rather than a dead end. Only plain files are considered; subdirectories are skipped,
    which also means a stray `.safetensors` one level down cannot be mistaken for the VAE.

    `profile` selects which discovery patterns apply (generation vs edit); it defaults to
    generation so callers that predate profiles keep working unchanged.
    """
    profile = profile or PROFILE_GEN
    out: dict = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return {}
    files: list[str] = []
    for n in names:
        full = os.path.join(folder, n)
        try:
            if os.path.isfile(full):
                files.append(n)
        except OSError:
            continue
    # Track files already claimed by a higher-priority role so two roles in the same folder
    # never resolve to one file (e.g. an edit DiT named "...edit.safetensors" and the VAE).
    claimed: set = set()
    for role, patterns in profile.discovery:
        suffixes = (".gguf", ".safetensors") if role != "vae" else (".safetensors", ".gguf")
        for n in files:
            if n in claimed:
                continue
            low = n.lower()
            if not low.endswith(suffixes):
                continue
            if any(p in low for p in patterns):
                out[role] = os.path.join(folder, n)
                claimed.add(n)
                break
    return out


def resolve_paths(settings: dict, profile: "Profile" = None) -> dict:
    """Resolve the binary and a profile's model files from settings + folder.

    `profile` defaults to generation (PROFILE_GEN), which keeps every historical behaviour:
    same setting keys (`diffusion.*`), same env fallbacks (`HEARTH_DIFFUSION_*`), same legacy
    keys. The edit profile reads `image_edit.*` and `HEARTH_IMAGE_EDIT_*` instead, so the two
    can be configured side by side without colliding.

    Precedence per file: its own `<prefix>.<role>_path` setting, then a name match inside
    `<prefix>.model_dir`, then the env fallback, then a legacy key (generation only). Settings
    win over env, as with `HEARTH_LLAMA_*` and `HEARTH_CAPTIONER_*`. Split out so status(),
    argv building and preflight can never disagree about what would run.

    The CLI binary is shared hardware: if the edit profile has no `image_edit.cli_bin`, it
    falls back to the generation `diffusion.cli_bin` (and its env var) before giving up — one
    sd-cli build runs both architectures, and asking an admin to paste the same path twice
    invites them to drift apart.

    Also reports where each resolved path came from (`sources`) — the UI says "found in the
    model folder" versus "your explicit path", because an admin who set paths and then moved
    the files needs to know which one is stale.
    """
    profile = profile or PROFILE_GEN
    pfx = profile.prefix

    def sget(*keys: str) -> str:
        for k in keys:
            v = str(settings.get(k) or "").strip()
            if v:
                return v
        return ""

    bin_env = f"{profile.env_prefix}_CLI_BIN"
    bin_path = sget(f"{pfx}.cli_bin") or os.environ.get(bin_env, "")
    # The edit profile shares the generation binary when it has none of its own — same
    # executable, only the model files differ. Only falls back for non-generation profiles so
    # generation behaviour is untouched.
    if not bin_path and profile is not PROFILE_GEN:
        bin_path = (sget("diffusion.cli_bin")
                    or os.environ.get("HEARTH_DIFFUSION_CLI_BIN", ""))
    folder = sget(f"{pfx}.model_dir") or os.environ.get(f"{profile.env_prefix}_MODEL_DIR", "")
    if folder:
        folder = os.path.abspath(os.path.expanduser(folder))
    found = _discover(folder, profile) if folder else {}

    # Env var name per role, derived from the profile's env prefix (upper-cased role).
    env_roles = {role: f"{profile.env_prefix}_{role.upper()}" for role, _l, _f in profile.roles}
    legacy = profile.legacy

    paths_out: dict = {}
    sources: dict = {}
    for role, _label, _flag in profile.roles:
        explicit = sget(f"{pfx}.{role}_path")
        if explicit:
            paths_out[role] = os.path.abspath(os.path.expanduser(explicit))
            sources[role] = "setting"
        elif role in found:
            paths_out[role] = found[role]
            sources[role] = "folder"
        elif env_roles[role] in os.environ:
            paths_out[role] = os.path.abspath(os.path.expanduser(os.environ[env_roles[role]]))
            sources[role] = "env"
        else:
            old = sget(*legacy.get(role, ()))
            if old:
                paths_out[role] = os.path.abspath(os.path.expanduser(old))
                sources[role] = "legacy"
            else:
                paths_out[role] = ""
                sources[role] = ""

    return {
        "enabled": _bool(settings, f"{pfx}.enabled", False),
        "bin": bin_path,
        "model_dir": folder,
        "models": paths_out,
        "sources": sources,
        "backend": (sget(f"{pfx}.backend")
                    or os.environ.get(f"{profile.env_prefix}_BACKEND", "")),
    }


def _missing_files_msg(p: dict, profile: "Profile" = None) -> str:
    """One actionable sentence listing every required file that is not readable.

    Only a profile's REQUIRED roles are checked — an unset optional one (the edit profile's
    `llm_vision`) is never listed as missing, or the tool would refuse setups that work fine
    without it. Each item names the setting label it belongs to and where to fix it, so a
    single problem reads "'VAE (.safetensors)' does not exist: … — set it (or 'Model folder')
    in Admin → Images" rather than a generic 'not configured'. Wording is built per item
    because the fix differs between "you never set it" and "the path you set has no file".
    """
    profile = profile or PROFILE_GEN
    required = set(profile.required_roles)
    bad = []
    for role, label, _flag in profile.roles:
        if role not in required:
            continue
        path = p["models"].get(role, "")
        if not path:
            bad.append(f"'{label}' is not set")
        elif not os.path.isfile(path):
            bad.append(f"'{label}' does not exist: {path}")
    if not bad:
        return ""
    tab = "Admin → Images"
    where = (f"Fill in {tab} → '{profile.label}' → 'Model folder' (the directory holding its "
             f"files), or the individual paths below." if len(bad) > 1
             else f"Set it in {tab} → '{profile.label}' — or set that profile's 'Model folder' "
                  f"and let Hearth find it.")
    return "; ".join(bad) + " — " + where


def preflight(settings: dict, profile: "Profile" = None) -> str:
    """Return a reason this profile CANNOT run, or "" when it plausibly can.

    Cheap and synchronous (no subprocess), so a tool call fails immediately with an
    actionable message instead of after the model has already promised the user a picture.
    Every required file is checked: sd-cli's own failure for a missing one is hundreds of
    `… not in model metadata` lines, which teaches nobody anything.
    """
    profile = profile or PROFILE_GEN
    p = resolve_paths(settings, profile)
    if not p["enabled"]:
        return (f"{profile.label.lower()} is disabled (Admin → Images → "
                f"'{profile.label}')")
    if not p["bin"]:
        return (f"no stable-diffusion.cpp CLI binary is set for {profile.label} (Admin → "
                f"Images → 'stable-diffusion.cpp CLI path')")
    if not have(p["bin"]):
        return f"diffusion CLI binary not found on the server: {p['bin']}"
    return _missing_files_msg(p, profile)


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
               strength: float | None = None, ref_image: str = "",
               profile: "Profile" = None) -> list[str]:
    """The `sd-cli` command for one generation (PROFILE_GEN) or edit (PROFILE_EDIT).

    `profile` defaults to generation, and the generation path is byte-for-byte what it was
    before profiles existed — same setting keys, same flags, same order. The differences for
    editing are: sampling CFG/flow-shift come from the edit profile, an input image goes in
    as `-r <path>` (reference conditioning) rather than `-i` (img2img), and the optional
    `--llm_vision` role is emitted only when it resolves.

    Paths are staged outside the jail by the caller, so every path here is inside a private
    temp directory — the one exception is `init_image`/`ref_image`, an absolute path the tool
    resolved inside the user's jail and decrypted into that same temp dir (see StagedImage).

    Output uses `%03d` deliberately, and the naming is not a guess: `examples/cli/main.cpp`
    matches `-o` against `(%\d{0,3}d)` and runs the specifier through `snprintf` verbatim with
    `output_begin_idx + i`, begin_idx defaulting to 0 when a pattern is present. So
    `hearth_%03d.png` yields exactly `hearth_000.png`, `hearth_001.png`, … Naming files after the
    call rather than globbing keeps discovery exact — no stale file can be mistaken for this run's.
    """
    profile = profile or PROFILE_GEN
    pfx = profile.prefix
    p = resolve_paths(settings, profile)
    if not p["bin"]:
        raise DiffusionError(f"no stable-diffusion.cpp CLI binary configured for "
                             f"{profile.label} (Admin → Images → 'stable-diffusion.cpp CLI path')")
    if not have(p["bin"]):
        raise DiffusionError(f"diffusion CLI binary not found: {p['bin']}")
    missing = _missing_files_msg(p, profile)
    if missing:
        raise DiffusionError(missing)

    w = _clamp_int(width or _num(settings, f"{pfx}.width", int, 768), MIN_EDGE, MAX_EDGE, 768)
    h = _clamp_int(height or _num(settings, f"{pfx}.height", int, 768), MIN_EDGE, MAX_EDGE, 768)
    st = _clamp_int(steps or _num(settings, f"{pfx}.steps", int, 20), MIN_STEPS, MAX_STEPS, 20)
    # CFG default comes from the profile (generation 4.5, edit 2.5) so a chatty model that
    # omits it still gets the value this architecture is tuned for, not the other one's.
    g = _clamp_float(cfg or _num(settings, f"{pfx}.cfg_scale", float, profile.cfg_default),
                     MIN_CFG, MAX_CFG, profile.cfg_default)
    n = max(1, min(int(batch or 1), MAX_BATCH_PER_IMAGE))

    # Each file with its own flag — never `-m/--model`, which Qwen Image does not accept (it
    # reads the diffusion weights as an SD UNet and dies on version lookup). Optional roles
    # (edit's llm_vision) are emitted only when a path resolved.
    required = set(profile.required_roles)
    a = [p["bin"]]
    for role, _label, flag in profile.roles:
        path = p["models"].get(role, "")
        if not path and role not in required:
            continue
        a += [flag, path]
    if p["backend"]:
        a += ["--backend", p["backend"]]

    # zero_cond_t is needed by 2511-class checkpoints (edit quality degrades badly without it)
    # but NOT by the original Qwen-Image-Edit, so it defaults off and an admin turns it on for
    # 2511 weights via `image_edit.zero_cond_t`. Part of argv building rather than a per-call knob
    # because it is a property of the weights, not of the request.
    if profile.zero_cond_t or _bool(settings, f"{pfx}.zero_cond_t", False):
        a += ["--model-args", "qwen_image_zero_cond_t=true"]

    a += ["-p", str(prompt or "")]
    neg = str(negative or "").strip()
    if neg:
        a += ["-n", neg]
    a += ["-W", str(w), "-H", str(h), "--steps", str(st), "--cfg-scale", f"{g:g}"]

    # sd-cli takes `-s` with an optional value; a negative means "random" and would be
    # parsed as the next flag's argument, so it is omitted rather than passed. 0 is a real
    # seed and must be passed (verified: `seed 0` appears in the run log).
    seed_spec = _num(settings, f"{pfx}.seed", int, -1) if seed is None else int(seed)
    if seed_spec >= 0:
        a += ["-s", str(seed_spec)]

    # Reference-image editing (edit profile): `-r` feeds the image through the VLM + VAE
    # encoder as conditioning, which is how Qwen Image Edit preserves identity and layout.
    # This is NOT img2img; strength does not apply to reference editing, so it is not passed.
    if ref_image:
        a += ["-r", ref_image]

    # img2img (generation profile's init_image): perturb an existing image with noise at a
    # given strength. Kept distinct from `-r` because they mean different things to sd-cli.
    if init_image:
        a += ["-i", init_image]
        s_val = (strength if strength is not None
                 else _num(settings, f"{pfx}.strength", float, 0.6))
        a += ["--strength", f"{_clamp_float(s_val, 0.0, 1.0, 0.6):g}"]

    # Pinned rather than tuned per call: euler is what these models are used to, and flash
    # attention on the diffusion transformer is a large speed-up (the deprecated
    # `--diffusion-fa` spelling, which this build still accepts and which cannot conflict
    # with a backend that has FA disabled).
    a += ["--sampling-method", "euler", "--diffusion-fa"]

    # Qwen Image Edit is tuned with a flow-shift of 3 (upstream docs); generation omits it,
    # matching the previous behaviour exactly.
    if profile.flow_shift is not None:
        a += ["--flow-shift", str(profile.flow_shift)]

    # `--preview none` is not optional: sd-cli defaults to --preview proj AND writes
    # ./preview.png (and step previews alongside it). Without pinning this, every call
    # would litter whatever working directory Hearth happens to run in. It also costs VAE
    # decodes per step for pictures nobody in a chat turn ever sees.
    a += ["--preview", "none"]

    a += ["-b", str(n), "-o", os.path.join(out_dir, "hearth_%03d.png"),
          "--preview-path", os.path.join(out_dir, "preview_%03d.png")]

    extra = str(settings.get(f"{pfx}.extra_args") or "").strip()
    if extra:
        try:
            a += shlex.split(extra)
        except ValueError:
            raise DiffusionError(f"diffusion extra args cannot be parsed: {extra!r}")
    return a


def launch_command_preview(settings: dict, profile: "Profile" = None) -> dict:
    """A representative command line for copying into a terminal (Admin UI).

    Uses a placeholder prompt and temp paths so the admin sees the file flags that actually
    matter without Hearth claiming to have generated anything. For the edit profile a
    placeholder reference image is included, since `-r` is required there.
    """
    profile = profile or PROFILE_GEN
    ref = "/path/to/input.png" if profile.needs_image else ""
    try:
        argv = build_argv(settings, "a lighthouse at dusk", "/tmp", steps=0,
                          ref_image=ref, profile=profile)
    except DiffusionError as e:
        return {"ok": False, "command": "", "note": str(e)}
    return {"ok": True, "command": shlex.join(argv), "argv": argv, "note": ""}



# ---- the runner --------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen, grace_s: float = 15.0) -> bool:
    """Terminate a child AND its descendants; True if SIGKILL was needed.

    `terminate()` alone is not enough and this was proven, not assumed: with the real
    sd-cli driven through a shell wrapper, SIGTERM to the direct child left the grandchild
    running — i.e. Hearth would have reported "timed out" while a phantom process went on
    holding the GPU and blocking the next generation. sd-cli spawns helpers (and a user
    could reasonably point diffusion.cli_bin at a wrapper script), so the whole group goes.

    Uses its own process group (`start_new_session=True` below), which is what makes
    killpg() safe: we only ever signal a group Hearth created itself, never Hearth's own.

    The grace period is generous because sd-cli is worth waiting for: measured on this
    machine, a mid-generation sd-cli (1024×1024, 30 steps) exits cleanly on SIGTERM in well
    under 20 s — it releases gigabytes of VRAM as it goes, and killing it earlier with
    SIGKILL can leave driver-side allocations behind. So TERM the group, wait, and only
    then escalate. `killed` therefore means "it ignored SIGTERM", which is worth knowing.
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


# What sd-cli's own failure output means, in the order worth checking. Each entry is
# (substring to look for in the log tail, plain-language cause). The point: a wrong or
# missing file makes sd-cli print hundreds of tensor lines, and an admin reading that log
# learns nothing — so Hearth translates the signature it recognises and names the setting.
_LOG_HINTS = (
    ("get sd version from file failed",
     "the file passed as the diffusion model is not one sd-cli can read as a diffusion "
     "network — Qwen Image needs --diffusion-model, and the Qwen3-VL text encoder must NOT "
     "be given to --model/--diffusion-model (check Admin → Images paths)"),
    ("VAE tensor",
     "the VAE is missing or wrong for this model: set 'VAE (.safetensors)' to the "
     "diffusion_pytorch_model.safetensors that ships with the Qwen Image files"),
    ("text_encoders.llm",
     "the Qwen3-VL text encoder is missing or wrong: set 'Text-encoder LLM (.gguf)' to the "
     "Qwen3VL .gguf that ships with the model"),
    ("not in model metadata",
     "one of the three model files does not match the others — they must come from the same "
     "Qwen Image package"),
)


def explain_failure(log_tail: str) -> str:
    """Translate sd-cli's last output into a likely cause, or "" if unrecognised."""
    low = (log_tail or "").lower()
    for needle, hint in _LOG_HINTS:
        if needle.lower() in low:
            return hint
    return ""


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
        """Kill the generation in flight; True once nothing of it is running any more.

        Tears down the whole process group via _kill_tree — terminate() on the direct child
        alone was shown to leave a spawned helper alive, which would mean reporting "stopped"
        while a phantom sd-cli kept the GPU and blocked the next request.

        The old shape of this method was `if p is None or p.poll() is None: return False`,
        which returned early for exactly the case it exists to handle: a **running** child has
        `poll() is None`, so Stop did nothing at all except remember the intent, and the next
        generation's `_set_active()` killed a process that was already gone. Measured on this
        machine with a real Qwen Image run in flight: stop_active() returned in 0.00 s having
        sent no signal, and the child stayed alive for its full remaining minutes. Hence the
        explicit branch below — `None` means "not started yet, remember it"; anything else
        means "kill it now", and SIGKILL escalation is deliberately allowed to block so a
        reported stop is real rather than requested.
        """
        with self._lock_state:
            self._stop_requested = True
            p = self._active_proc
        if p is None:
            return False          # nothing launched yet; _set_active() honours the intent
        _kill_tree(p)
        return p.poll() is not None

    def clear_stop(self) -> None:
        """Forget a pending kill (called at the start of each new generation)."""
        with self._lock_state:
            self._stop_requested = False

    def _set_active(self, proc) -> None:
        """Record the running child; honour a Stop that arrived before it existed.

        `None` (generation over) must not trigger anything — a Stop for a run that has
        already finished is satisfied by the fact that no process is left.
        """
        self._active_proc = proc
        if proc is None:
            return
        with self._lock_state:
            pending = self._stop_requested
        if pending:
            _kill_tree(proc)

    # -- generation -----------------------------------------------------------
    def generate_sync(self, settings: dict, prompt: str, *, width: int = 0, height: int = 0,
                      steps: int = 0, cfg: float = 0.0, seed=None, negative: str = "",
                      batch: int = 1, init_image: str = "", strength: float | None = None,
                      ref_image: str = "", profile: "Profile" = None,
                      cancel=None, log=lambda m: None) -> dict:
        """Run one `sd-cli` generation or edit. SYNCHRONOUS — call via asyncio.to_thread.

        `profile` selects which backend runs it (default PROFILE_GEN). The runner itself is
        shared: one lock serialises generations AND edits together, because they compete for
        the same machine and a draw overlapping an edit would OOM the chat model just as two
        draws would.

        `init_image` (img2img) and `ref_image` (reference editing) must each be an
        already-resolved absolute path readable by the subprocess (staged outside the jail by
        the tool, so encryption at rest cannot hand it ciphertext). Returns {"images":
        [(name, bytes)], "seed": int|None, "seconds": float, "argv": [...], "notes": [str]}.
        Raises DiffusionError.

        Files are read back from the temp dir rather than trusted by name: sd-cli's own
        numbering decides the suffix, so the caller must not guess it — but since the
        directory is ours and freshly made, whatever appears in it IS this run's output.
        """
        profile = profile or PROFILE_GEN
        reason = preflight(settings, profile)
        if reason:
            raise DiffusionError(reason)

        timeout = max(30.0, float(_num(settings, f"{profile.prefix}.timeout_s", int, 600)))
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
                                  batch=batch, init_image=init_image, strength=strength,
                                  ref_image=ref_image, profile=profile)
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
                    # A killed child is not a broken model: Stop and the timeout path both
                    # end up here with a signal exit code, and claiming "the model files must
                    # all come from the same package" after the user pressed Stop would send
                    # them off chasing a config problem that does not exist.
                    if rc < 0:
                        sig = -rc
                        why = ("stopped by Hearth (Stop)" if self._stop_requested
                               else "killed by its timeout limit" if sig == signal.SIGTERM
                               else f"killed by signal {sig}")
                        raise DiffusionError(
                            f"image generation was {why} after {seconds}s — nothing was drawn. "
                            f"Nothing is wrong with the model files; ask again when you want "
                            f"the picture.")
                    tail = self.read_log_tail(1200)
                    hint = explain_failure(tail) or (
                        "the model files must all come from the same Qwen Image package")
                    raise DiffusionError(
                        f"the diffusion CLI exited with code {rc} after {seconds}s — {hint}"
                        + (f"; last output: {tail[-300:]}" if tail else "")
                        + f" — full output: {self._log_path}")

                images = self._collect(out_dir)
                if not images:
                    tail = self.read_log_tail(1200)
                    raise DiffusionError(
                        "the diffusion CLI exited successfully but wrote no image"
                        + (f"; last output: {tail[-300:]}" if tail else "")
                        + f" — check {self._log_path}")
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
        *something* worth showing, labelled as a preview by the caller's text). Hearth asks
        for `--preview none`, so the fallback path exists only for an override.
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
    def status(self, settings: dict | None = None, profile: "Profile" = None) -> dict:
        """What the admin UI shows about one profile (generation or edit). Never raises.

        `configured` counts only the REQUIRED roles, so an edit setup without the optional
        vision mmproj still reads as ready rather than perpetually broken. Every declared role
        is reported in `present`, though, so the UI can show the optional file's state too.
        """
        profile = profile or PROFILE_GEN
        settings = settings if settings is not None else rs.get_all()
        p = resolve_paths(settings, profile)
        required = set(profile.required_roles)
        present = {role: bool(path) and os.path.isfile(path)
                   for role, path in p["models"].items()}
        configured = all(present.get(role, False) for role in required) if required else False
        return {
            "kind": "diffusion",
            "profile": profile.name,
            "label": profile.label,
            "enabled": p["enabled"],
            "configured": configured,
            # There is no server: a binary that exists and runs IS the whole story.
            "reachable": have(p["bin"]),
            "managed_by_hearth": True,
            # `model_path` stays for the shared loaded-models row (admin.js shows one name).
            "model_path": p["models"].get("dit", ""),
            "model_dir": p["model_dir"],
            "paths": dict(p["models"]),
            "sources": p["sources"],
            "labels": {role: label for role, label, _f in profile.roles},
            "required": list(profile.required_roles),
            "present": present,
            "hint": preflight(settings, profile) if p["enabled"] else "",
            "log": str(self._log_path),
            "capabilities": {"cli_present": have(p["bin"]),
                             "model_present": configured,
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
    """Generate images off the event loop (sd-cli blocks for minutes).

    Pass `profile=sd.PROFILE_EDIT` (and `ref_image=`) in kwargs to run an edit instead of a
    generation; the same runner and lock serve both.
    """
    return await asyncio.to_thread(get_diffusion().generate_sync, settings, prompt, **kwargs)

