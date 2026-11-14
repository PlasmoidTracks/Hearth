"""
Optional image-to-text fallback for when the loaded GGUF has no mmproj vision
projector, so the model cannot actually see images. A small local captioner
(BLIP / BLIP-2 / ViT-GPT2) or a CLIP zero-shot tagger turns the image into a
short text description instead.

Off by default; configured under Settings -> Vision. Every description handed to
the model is wrapped in an explicit warning, because these models are far weaker
than real VLM vision: they routinely miss on-screen text, layout, colors and
fine detail, and they guess confidently when unsure.

Heavy imports (torch/transformers/huggingface_hub) happen lazily inside the
functions below, so importing this module stays cheap even when the fallback is
disabled.
"""
import io
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

DEVICES = ["auto", "cpu", "cuda"]

# transformers refuses to torch.load() .bin checkpoints on older torch (CVE-2025-32434),
# so models that ship no safetensors weights need at least this torch version.
MIN_TORCH_FOR_BIN_WEIGHTS = (2, 6)

# Weight formats we never need. Skipping them avoids downloading each model twice
# (most repos ship .bin, .safetensors, TF and Flax copies of the same weights).
BASE_IGNORE_PATTERNS = [
    "*.msgpack", "*.h5", "*.tflite", "*.onnx", "*.ot",
    "*tf_model*", "*flax_model*", "open_clip*",
]


class CaptionError(Exception):
    pass


@dataclass
class CaptionModelSpec:
    key: str
    label: str
    repo_id: str
    kind: str          # "caption" (image-to-text) | "clip" (zero-shot tagging)
    approx_mb: int
    has_safetensors: bool
    note: str = ""
    heavy: bool = False


CAPTION_MODELS: List[CaptionModelSpec] = [
    CaptionModelSpec(
        key="blip-large",
        label="BLIP large (captioning)",
        repo_id="Salesforce/blip-image-captioning-large",
        kind="caption",
        approx_mb=1880,
        has_safetensors=True,
        note="Good default. Short but usable captions; CPU-viable, faster on GPU.",
    ),
    CaptionModelSpec(
        key="git-large-coco",
        label="GIT large COCO (captioning)",
        repo_id="microsoft/git-large-coco",
        kind="caption",
        approx_mb=1580,
        has_safetensors=True,
        note="Alternative captioner, often more concrete than BLIP on photos and product shots.",
    ),
    CaptionModelSpec(
        key="clip-tagger",
        label="CLIP zero-shot tagger (labels, not sentences)",
        repo_id="openai/clip-vit-large-patch14",
        kind="clip",
        approx_mb=1710,
        has_safetensors=True,
        note="Scores the image against a fixed vocabulary (screenshot / diagram / chart / UI mockup / photo ...). "
             "Tells you what KIND of image it is, not what is in it.",
    ),
    CaptionModelSpec(
        key="blip2-opt-2.7b",
        label="BLIP-2 OPT-2.7b (captioning, heavy)",
        repo_id="Salesforce/blip2-opt-2.7b",
        kind="caption",
        approx_mb=15000,
        has_safetensors=True,
        note="Best descriptions of the set, but a ~15 GB download and effectively GPU-only. "
             "Expect long load times and high memory use.",
        heavy=True,
    ),
    CaptionModelSpec(
        key="blip-base",
        label="BLIP base (captioning, small)",
        repo_id="Salesforce/blip-image-captioning-base",
        kind="caption",
        approx_mb=990,
        has_safetensors=False,
        note="Smallest and fastest, but this repo ships only .bin weights, which transformers "
             "refuses to load unless torch is 2.6 or newer.",
    ),
]

DEFAULT_MODEL_KEY = "blip-large"

# Vocabulary for the CLIP tagger, weighted toward what shows up in a coding project.
CLIP_LABEL_VOCAB = [
    "a screenshot of a graphical user interface",
    "a screenshot of a website in a web browser",
    "a screenshot of a mobile app",
    "a screenshot of source code in a text editor",
    "a screenshot of a terminal or command line",
    "a screenshot of an error message or dialog box",
    "a UI mockup or wireframe",
    "a software architecture diagram",
    "a flowchart",
    "a bar chart",
    "a line chart",
    "a pie chart",
    "a table of data",
    "a logo or icon",
    "an illustration or drawing",
    "a photograph of a person",
    "a photograph of a place or landscape",
    "a photograph of an object or product",
    "a scanned document or page of text",
    "a map",
    "a mostly blank or empty image",
]

CLIP_TOP_K = 5
MAX_NEW_TOKENS = 40

WARNING_TEXT = (
    "WARNING — READ BEFORE USING THIS DESCRIPTION: the text above was NOT produced by your "
    "own vision. No vision projector (mmproj) is loaded, so the image was passed through a "
    "small local image-to-text model as a fallback. Such descriptions are generic and are "
    "often wrong or incomplete: on-screen text, exact layout, colors, spacing, counts and "
    "small details are usually missed or invented. Take it with a grain of salt. Do NOT make "
    "decisions that depend on how the image actually looks (visual bugs, layout/design review, "
    "reading text or numbers off a screenshot, comparing before/after images) based on this "
    "description alone — instead say plainly what you cannot see, ask the user to describe the "
    "relevant detail, or suggest they load a vision-capable GGUF plus its matching mmproj file "
    "so the image can be shown to you directly."
)


def get_spec(key: str) -> CaptionModelSpec:
    for spec in CAPTION_MODELS:
        if spec.key == key:
            return spec
    raise CaptionError(f"Unknown caption model: {key}")


def spec_or_default(key: str) -> CaptionModelSpec:
    try:
        return get_spec(key)
    except CaptionError:
        return get_spec(DEFAULT_MODEL_KEY)


# ---------- environment probing ----------

def deps_status() -> Tuple[bool, str]:
    """Returns (ok, human-readable status) for the optional captioning dependencies."""
    missing = []
    for mod, pkg in (("torch", "torch"), ("transformers", "transformers"), ("huggingface_hub", "huggingface_hub")):
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if missing:
        return False, ("Missing packages: " + ", ".join(missing) +
                       "  —  install with:  pip install " + " ".join(missing))
    try:
        import torch
        import transformers
        return True, f"torch {torch.__version__}, transformers {transformers.__version__}"
    except Exception as e:
        return False, f"Dependencies present but unusable: {e}"


def cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def torch_version() -> Optional[Tuple[int, int]]:
    try:
        import torch
        parts = torch.__version__.split("+")[0].split(".")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


def compatibility_problem(spec: CaptionModelSpec) -> Optional[str]:
    """
    Reason this model cannot be loaded with the installed packages, or None if it can.
    The .bin-only case is the common one: transformers blocks torch.load on torch < 2.6.
    """
    ok, status = deps_status()
    if not ok:
        return status
    if spec.has_safetensors:
        return None
    tv = torch_version()
    if tv is not None and tv < MIN_TORCH_FOR_BIN_WEIGHTS:
        need = ".".join(str(p) for p in MIN_TORCH_FOR_BIN_WEIGHTS)
        import torch
        return (
            f"{spec.repo_id} ships only .bin weights, and transformers refuses to load those on "
            f"torch {torch.__version__} (needs torch >= {need}, CVE-2025-32434). "
            f"Pick a model marked as safetensors, or upgrade torch:  pip install -U 'torch>={need}'"
        )
    return None


def resolve_device(preference: str) -> str:
    if preference == "cuda":
        return "cuda" if cuda_available() else "cpu"
    if preference == "cpu":
        return "cpu"
    return "cuda" if cuda_available() else "cpu"


# ---------- download handling ----------

def ignore_patterns_for(spec: CaptionModelSpec) -> List[str]:
    """Skip duplicate weight formats — and .bin entirely when safetensors exist."""
    patterns = list(BASE_IGNORE_PATTERNS)
    if spec.has_safetensors:
        patterns.append("*.bin")
    return patterns


def is_downloaded(spec: CaptionModelSpec) -> bool:
    """True if the model's files are already in the local Hugging Face cache."""
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        return False
    try:
        snapshot_download(repo_id=spec.repo_id, ignore_patterns=ignore_patterns_for(spec),
                          local_files_only=True)
        return True
    except Exception:
        return False


def _progress_tqdm_class(progress_cb: Callable[[int, int], None]):
    """A tqdm subclass that reports aggregate downloaded/total bytes to a callback."""
    from tqdm.auto import tqdm as base_tqdm

    state = {"done": 0, "total": 0}
    lock = threading.Lock()

    class _ReportingTqdm(base_tqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            with lock:
                state["total"] += int(self.total or 0)

        def update(self, n=1):
            ret = super().update(n)
            with lock:
                state["done"] += int(n or 0)
                done, total = state["done"], state["total"]
            try:
                progress_cb(done, total)
            except Exception:
                pass
            return ret

    return _ReportingTqdm


def download(spec: CaptionModelSpec, progress_cb: Optional[Callable[[int, int], None]] = None) -> str:
    """Fetch the model into the local HF cache. Returns the snapshot path."""
    problem = compatibility_problem(spec)
    if problem:
        raise CaptionError(problem)
    from huggingface_hub import snapshot_download

    kwargs = {"repo_id": spec.repo_id, "ignore_patterns": ignore_patterns_for(spec)}
    if progress_cb is not None:
        try:
            kwargs["tqdm_class"] = _progress_tqdm_class(progress_cb)
        except Exception:
            pass  # older huggingface_hub / no tqdm: download without progress reporting
    try:
        return snapshot_download(**kwargs)
    except TypeError:
        kwargs.pop("tqdm_class", None)
        return snapshot_download(**kwargs)


# ---------- inference ----------

_MODEL_CACHE = {}
_LOAD_LOCK = threading.Lock()


def _load_model_and_processor(spec: CaptionModelSpec, device: str):
    """
    Load processor + model explicitly rather than through transformers' pipeline
    helper, so weight-format and dtype problems surface as readable errors.
    """
    import torch
    from transformers import AutoProcessor

    dtype = torch.float16 if device == "cuda" else torch.float32
    load_kwargs = {"torch_dtype": dtype}
    if spec.has_safetensors:
        load_kwargs["use_safetensors"] = True

    try:
        # Deliberately not use_fast=True: transformers 4.52's fast CLIP image
        # processor breaks CLIPProcessor.__call__ (missing _valid_processor_keys).
        processor = AutoProcessor.from_pretrained(spec.repo_id)
    except Exception as e:
        raise CaptionError(f"Could not load the processor for {spec.repo_id}: {e}")

    try:
        if spec.kind == "clip":
            from transformers import CLIPModel
            model = CLIPModel.from_pretrained(spec.repo_id, **load_kwargs)
        else:
            from transformers import AutoModelForVision2Seq
            try:
                model = AutoModelForVision2Seq.from_pretrained(spec.repo_id, **load_kwargs)
            except (ValueError, KeyError):
                # Some captioners (e.g. GIT) are registered as causal-LM architectures.
                from transformers import AutoModelForCausalLM
                model = AutoModelForCausalLM.from_pretrained(spec.repo_id, **load_kwargs)
    except Exception as e:
        raise CaptionError(f"Could not load {spec.label} ({spec.repo_id}) on {device}: {e}")

    model.to(device)
    model.eval()
    return processor, model


def unload_all():
    """Drop cached models (frees RAM/VRAM when the fallback is turned off)."""
    with _LOAD_LOCK:
        _MODEL_CACHE.clear()


class ImageCaptioner:
    """Lazily-loaded wrapper around one captioning / tagging model."""

    def __init__(self, model_key: str = DEFAULT_MODEL_KEY, device_preference: str = "auto"):
        self.spec = spec_or_default(model_key)
        self.device_preference = device_preference if device_preference in DEVICES else "auto"

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def device(self) -> str:
        return resolve_device(self.device_preference)

    def is_ready(self) -> bool:
        return is_downloaded(self.spec)

    def compatibility_problem(self) -> Optional[str]:
        return compatibility_problem(self.spec)

    def _loaded(self):
        problem = compatibility_problem(self.spec)
        if problem:
            raise CaptionError(problem)
        cache_key = (self.spec.key, self.device)
        with _LOAD_LOCK:
            entry = _MODEL_CACHE.get(cache_key)
            if entry is None:
                entry = _load_model_and_processor(self.spec, self.device)
                _MODEL_CACHE[cache_key] = entry
        return entry

    def _open(self, png_bytes: bytes):
        from PIL import Image
        return Image.open(io.BytesIO(png_bytes)).convert("RGB")

    def describe_one(self, png_bytes: bytes) -> str:
        """Short text description of a single PNG image."""
        import torch

        processor, model = self._loaded()
        image = self._open(png_bytes)

        if self.spec.kind == "clip":
            inputs = processor(text=CLIP_LABEL_VOCAB, images=image, return_tensors="pt", padding=True)
            inputs = self._cast_inputs(inputs, model)
            with torch.no_grad():
                probs = model(**inputs).logits_per_image.softmax(dim=1)[0]
            top = torch.topk(probs, min(CLIP_TOP_K, len(CLIP_LABEL_VOCAB)))
            return "; ".join(
                f"{CLIP_LABEL_VOCAB[i]} ({float(p) * 100:.0f}%)"
                for p, i in zip(top.values.tolist(), top.indices.tolist())
            )

        inputs = processor(images=image, return_tensors="pt")
        inputs = self._cast_inputs(inputs, model)
        with torch.no_grad():
            generated = model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS)
        decoded = processor.batch_decode(generated, skip_special_tokens=True)
        text = (decoded[0] if decoded else "").strip()
        return text or "(model returned an empty caption)"

    def _cast_inputs(self, inputs, model):
        """Move inputs to the model's device, matching its dtype for float tensors only."""
        import torch

        inputs = inputs.to(model.device)
        if getattr(model, "dtype", None) in (torch.float16, torch.bfloat16):
            for key, value in list(inputs.items()):
                if torch.is_tensor(value) and value.is_floating_point():
                    inputs[key] = value.to(model.dtype)
        return inputs


def describe_media(captioner: ImageCaptioner, frames: List[bytes], path: str, is_video: bool) -> str:
    """
    Build the full tool-result text for one image or a set of sampled video
    frames, including the accuracy warning. Raises CaptionError on failure.
    """
    if not frames:
        raise CaptionError("no frames to describe")

    kind_word = "Zero-shot labels" if captioner.spec.kind == "clip" else "Caption"
    lines = []
    if is_video and len(frames) > 1:
        for i, frame in enumerate(frames, start=1):
            lines.append(f"Frame {i}/{len(frames)} — {kind_word.lower()}: {captioner.describe_one(frame)}")
    else:
        lines.append(f"{kind_word}: {captioner.describe_one(frames[0])}")

    header = (
        f"[Text description of '{path}' generated locally by {captioner.spec.label} "
        f"({captioner.spec.repo_id}, device: {captioner.device}) because no vision projector "
        f"(mmproj) is loaded — you are NOT seeing the image itself.]"
    )
    return header + "\n\n" + "\n".join(lines) + "\n\n" + WARNING_TEXT
