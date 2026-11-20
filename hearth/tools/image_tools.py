"""Image tools — ask Qwen Image (stable-diffusion.cpp) to draw or edit a picture.

  generate_image   text -> PNG(s), optionally starting from an existing workspace image
  edit_image       existing workspace image + instruction -> edited PNG(s) (Qwen Image Edit)

The two use different models in different slots (see hearth/diffusion.py profiles): generation
draws from text, editing transforms a picture you already have through reference conditioning.

Everything about these tools exists so the model cannot bluff about a picture:

  * The result carries `images`, so the generated pixels come BACK to the model through the
    same multimodal path `pdf_read`/`view_video` use (DEC-052). A model that sees what it
    drew can say "the sign in the corner is garbled — want me to retry with less text in the
    prompt?". A model that never saw it would be guessing, and models guess.
  * Every failure names the setting or the file that is missing. A refusal the model can
    relay ("the VAE path your admin set does not exist") is useful; "generation failed"
    invites a vague apology — or an invented description of an image that does not exist.
  * Approval defaults to `ask` (`gated = True`): each call occupies this machine for
    minutes, which is the user's resource to give, not the model's to spend.

Encryption at rest is honoured by reading input images through workspaces.py and staging
them in a private temp dir — an external process must never be handed ciphertext, and
nothing extra is written into the user's jail (the same discipline as captioner.describe_file).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from .base import Tool, ToolResult
from .. import diffusion as sd
from .. import workspaces as ws


def _slug(text: str, max_len: int = 40) -> str:
    """A filesystem-safe, human-readable stem from a prompt.

    Names files after what was asked for rather than `img_7f3a1b.png`, because the name is
    what the user sees in the Artifacts panel and what the model quotes back. Lowercased,
    non-alphanumerics collapsed to `_`, so no path or shell meaning can attach to it.
    """
    out: list[str] = []
    for ch in str(text or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_") and (not out or out[-1] != "_"):
            out.append("_")
    return ("".join(out).strip("_")[:max_len].rstrip("_")) or "image"


def _store_images(ctx, blobs: list[tuple[str, bytes]], subdir: str) -> list[dict]:
    """Write PNGs into the caller's jail; returns [{"path", "mime"}] for what landed.

    Deterministic names (stem + timestamp + index) so the model can quote a path and a
    repeat call makes a new file rather than silently overwriting one it liked. write_bytes
    applies encryption at rest — that stays a workspaces concern and never leaks in here.
    """
    out: list[dict] = []
    for name, data in blobs:
        try:
            stored = ws.write_bytes(ctx.user_id, f"{subdir}/{name}", data)
            out.append({"path": stored, "mime": "image/png"})
        except Exception:  # noqa: BLE001 — one unwritable file must not lose the rest
            continue
    return out


class StagedImage:
    """One workspace image, decrypted into a private temp dir for the subprocess.

    A context manager because the plaintext copy must not outlive the generation: sd-cli
    needs a readable path (so it cannot be handed ciphertext), and the file has to go away
    afterwards whether generation succeeded, failed, or was cancelled.
    """

    def __init__(self, user_id: str, rel: str):
        self._user_id = user_id
        self._rel = rel
        self._dir = ""
        self.path = ""

    async def __aenter__(self) -> str:
        data = await asyncio.to_thread(ws.read_bytes, self._user_id, self._rel)
        self._dir = tempfile.mkdtemp(prefix="hearth-sd-in-")
        self.path = os.path.join(self._dir, "init.png")
        await asyncio.to_thread(self._write_now, self.path, data)
        return self.path

    async def __aexit__(self, *exc) -> None:
        if self._dir:
            import shutil
            await asyncio.to_thread(shutil.rmtree, self._dir, True)

    @staticmethod
    def _write_now(path: str, data: bytes) -> None:
        with open(path, "wb") as fh:
            fh.write(data)


class GenerateImageTool(Tool):
    name = "generate_image"
    label = "Image generation (Qwen Image)"
    description = (
        "Draw an image with Qwen Image (stable-diffusion.cpp) and save it in the user's "
        "workspace. Write `prompt` as a VISUAL description (subject, setting, style, lighting, "
        "composition) — not a request: 'a watercolour of a red fox in snow, soft morning light', "
        "never 'please make me a picture'. This model follows long, prose-like prompts well and "
        "renders legible text when you quote it ('a sign reading \"OPEN\"'). The finished image is "
        "shown back to you, so look at it before describing it. This costs real time (tens of "
        "seconds to minutes) and needs the user's approval: call it once, judge the result, then "
        "decide — never speculatively and never in a loop. `batch` returns several candidates from "
        "one model load, much cheaper than calling this tool several times. `init_image` starts "
        "from an existing workspace image (variation or restyle). If the tool says generation is "
        "not configured, pass that reason on and NEVER describe or imagine an image you did not "
        "receive.")

    enable_key = "tools.generate_image"
    gated = True      # minutes of machine time per call — the user decides

    def parameters(self):
        return {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "what the picture should LOOK like: subject, setting, "
                                      "style, lighting, mood. Concrete beats poetic; this model "
                                      "handles full sentences and on-image text."},
            "negative_prompt": {"type": "string",
                                "description": "what to avoid, e.g. 'text, watermark, blurry, "
                                               "extra fingers'. Optional."},
            "width": {"type": "integer",
                      "description": "width in pixels (64-2048; multiples of 64, and roughly "
                                     "512-1328 is where this model looks best). Default comes "
                                     "from the admin's setting."},
            "height": {"type": "integer",
                       "description": "height in pixels (64-2048). Portrait, e.g. 640x896."},
            "steps": {"type": "integer",
                      "description": "sampling steps (1-60); time scales with it. The admin "
                                     "default (~20) is usually right for this model."},
            "cfg_scale": {"type": "number",
                          "description": "how strictly to follow the prompt: ~3 looser, 4.5 is "
                                         "this model's sweet spot, above ~7 burns contrast."},
            "batch": {"type": "integer",
                      "description": "images from ONE call (max 4) — far cheaper than several "
                                     "calls because the model loads once. Use it when you want "
                                     "choices rather than one attempt."},
            "seed": {"type": "integer",
                     "description": "fix this to reproduce or refine a specific result; omit for "
                                    "a random one. Tell the user the seed when they may want the "
                                    "same picture again."},
            "init_image": {"type": "string",
                           "description": "optional workspace-relative image to start FROM "
                                          "(variation/restyle); pair with `strength`."},
            "strength": {"type": "number",
                         "description": "with init_image: 0.2 barely changes it, 0.6 keeps the "
                                        "composition while restyling, 0.9 replaces most of it. "
                                        "Ignored without init_image."}},
            "required": ["prompt"]}

    async def run(self, args, ctx):
        settings = ctx.settings()
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(False, "a `prompt` is required — describe the picture visually")

        # Refuse BEFORE asking the user to approve something that cannot possibly run.
        problem = sd.preflight(settings)
        if problem:
            return ToolResult(False, f"cannot generate an image: {problem}")

        init_rel = str(args.get("init_image") or "").strip()
        if init_rel:
            try:
                ws.resolve_in_jail(ctx.user_id, init_rel, allow_missing=False)
            except ws.JailEscape as e:
                return ToolResult(False, f"refused: {e}")
            except FileNotFoundError:
                return ToolResult(False, f"init_image not found in your workspace: {init_rel} "
                                         f"(attach it or list the file first)")

        try:
            batch = max(1, min(int(args.get("batch") or 1), sd.MAX_BATCH_PER_IMAGE))
        except (TypeError, ValueError):
            batch = 1

        gen_kwargs = dict(
            width=_as_int(args.get("width")),
            height=_as_int(args.get("height")),
            steps=_as_int(args.get("steps")),
            cfg=_as_float(args.get("cfg_scale")),
            negative=str(args.get("negative_prompt") or ""),
            batch=batch,
            cancel=ctx.cancel_event(),
        )
        seed_arg = _as_int(args.get("seed"))
        if seed_arg is not None:
            gen_kwargs["seed"] = seed_arg

        ctx.report_progress(type="notice", kind="gen_start",
                            text=f"drawing {batch} image{'s' if batch != 1 else ''}…")
        try:
            if init_rel:
                async with StagedImage(ctx.user_id, init_rel) as staged:
                    result = await sd.generate(settings, prompt, init_image=staged,
                                               **gen_kwargs)
            else:
                result = await sd.generate(settings, prompt, **gen_kwargs)
        except sd.DiffusionError as e:
            ctx.report_progress(type="notice", kind="gen_done", ok=False,
                                text="could not generate an image")
            return ToolResult(False, f"image generation failed: {str(e)[:500]}")
        except Exception as e:  # noqa: BLE001 — a broken run must not break the turn
            ctx.report_progress(type="notice", kind="gen_done", ok=False,
                                text="could not generate an image")
            return ToolResult(False, f"image generation failed: {type(e).__name__}: "
                                     f"{str(e)[:200]}")

        blobs = result.get("images") or []
        stem = _slug(prompt)
        stamp = time.strftime("%H%M%S")
        multi = len(blobs) > 1
        names = [(f"{stem}_{stamp}{f'_{i + 1}' if multi else ''}.png", data)
                 for i, (_, data) in enumerate(blobs)]
        images = _store_images(ctx, names, "generated")
        if not images:
            ctx.report_progress(type="notice", kind="gen_done", ok=False,
                                text="the image could not be saved")
            return ToolResult(False, "an image was generated but could not be saved to your "
                                     "workspace (disk full or a permissions problem)")

        # Register it as a durable artifact: listed in the Artifacts panel, downloadable after
        # a restart. A download BUTTON is offered for the first image only — this tool exists
        # because the user asked for a picture, so a button is wanted here (unlike fs_write,
        # DEC-045), but a 4-image batch must not stack four ⬇ into the transcript.
        from .. import artifacts
        art = None
        try:
            art = artifacts.record(ctx.user_id, images[0]["path"],
                                   conversation_id=ctx.conversation_id, kind="produced")
        except Exception as e:  # noqa: BLE001 — bookkeeping must not lose the picture
            from .. import audit
            audit.log(ctx.user_id, "artifact_record_failed",
                      {"path": images[0]["path"], "err": str(e)[:200]})

        n = len(images)
        secs = result.get("seconds", "?")
        ctx.report_progress(type="notice", kind="gen_done", ok=True,
                            text=f"drew {n} image{'s' if n != 1 else ''} in {secs}s")

        lines = [f"Generated {n} image(s) with Qwen Image in {secs}s. "
                 f"Prompt: {prompt[:160]}"]
        for it in images:
            lines.append(f"- {it['path']}")
        if len(names) > n:
            lines.append(f"note: {len(names) - n} of them could not be saved")
        seed = result.get("seed")
        lines.append(f"seed: {seed}" if seed is not None else
                     "seed: chosen randomly by the model (the exact value is embedded in the "
                     "PNG's generation-metadata chunk)")
        for note in result.get("notes") or []:
            lines.append(f"note: {note}")
        if init_rel:
            lines.append(f"started from {init_rel}")
        lines.append("\nThe image(s) follow this message. Look at them before describing them: "
                     "say what is actually there, including anything wrong (garbled text, extra "
                     "limbs, a detail you were asked for and did not include), and offer one "
                     "specific fix rather than retrying on your own.")

        meta: dict = {"images": n, "seconds": secs}
        if seed is not None:
            meta["seed"] = seed
        if art:
            meta["download"] = {"name": art["filename"], "url": art["download_url"],
                                "artifact_id": art["id"]}
        return ToolResult(True, "\n".join(lines), images=images, meta=meta)


class EditImageTool(Tool):
    name = "edit_image"
    label = "Image editing (Qwen Image Edit)"
    description = (
        "EDIT an existing picture with Qwen Image Edit (stable-diffusion.cpp) and save the result "
        "in the user's workspace. Give it `image` — a workspace-relative path to a picture that "
        "already exists (one you generated, or one the user uploaded/attached) — and `prompt`, an "
        "INSTRUCTION describing the change: 'remove the red car on the left', 'change the sky to a "
        "sunset', 'rotate the vase 90 degrees', 'replace the sign text with \"CAFE\"'. This is not "
        "text-to-image: it needs an input image, and it preserves everything you did not ask to "
        "change far better than redrawing would. For a brand-new picture from nothing, use "
        "generate_image instead. The edited image is shown back to you, so look at it before "
        "describing it. This costs real time (tens of seconds to minutes) and needs the user's "
        "approval: call it once, judge the result, then decide — never in a loop. If the tool says "
        "editing is not configured, pass that reason on and NEVER describe or imagine a result you "
        "did not receive.")

    enable_key = "tools.edit_image"
    gated = True      # minutes of machine time per call — the user decides

    def parameters(self):
        return {"type": "object", "properties": {
            "image": {"type": "string",
                      "description": "workspace-relative path to the image to edit (required) — an "
                                     "existing file in this user's workspace. Attach or generate one "
                                     "first if there is none."},
            "prompt": {"type": "string",
                       "description": "the EDIT INSTRUCTION: what to change, in plain language. Be "
                                      "specific about what to alter and leave the rest implied — the "
                                      "model keeps unmentioned regions. 'add a wooden bench in the "
                                      "foreground', 'make it look like a watercolour'."},
            "negative_prompt": {"type": "string",
                                "description": "what to avoid, e.g. 'text, watermark, blurry'. "
                                               "Optional."},
            "width": {"type": "integer",
                      "description": "output width in pixels (64-2048). Usually leave unset to keep "
                                     "the input's proportions; set it to compose a different size."},
            "height": {"type": "integer",
                       "description": "output height in pixels (64-2048)."},
            "steps": {"type": "integer",
                      "description": "sampling steps (1-60); time scales with it. ~20 is a good "
                                     "default for editing."},
            "cfg_scale": {"type": "number",
                          "description": "how strictly to follow the instruction: ~2.5 is Qwen Image "
                                         "Edit's tuned value; higher can repaint regions you wanted "
                                         "kept."},
            "batch": {"type": "integer",
                      "description": "edited images from ONE call (max 4) — cheaper than several "
                                     "calls because the model loads once. Use it for choices."},
            "seed": {"type": "integer",
                     "description": "fix this to reproduce or refine a specific result; omit for a "
                                    "random one. Tell the user the seed when relevant."}},
            "required": ["image", "prompt"]}

    async def run(self, args, ctx):
        settings = ctx.settings()
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(False, "a `prompt` is required — describe the edit you want")
        img_rel = str(args.get("image") or "").strip()
        if not img_rel:
            return ToolResult(False, "an `image` (workspace path to edit) is required — pass an "
                                     "existing image, or use generate_image for a new picture")

        # Refuse BEFORE asking the user to approve something that cannot possibly run.
        problem = sd.preflight(settings, sd.PROFILE_EDIT)
        if problem:
            return ToolResult(False, f"cannot edit an image: {problem}")

        try:
            ws.resolve_in_jail(ctx.user_id, img_rel, allow_missing=False)
        except ws.JailEscape as e:
            return ToolResult(False, f"refused: {e}")
        except FileNotFoundError:
            return ToolResult(False, f"image not found in your workspace: {img_rel} "
                                     f"(attach it or list the file first)")

        try:
            batch = max(1, min(int(args.get("batch") or 1), sd.MAX_BATCH_PER_IMAGE))
        except (TypeError, ValueError):
            batch = 1

        gen_kwargs = dict(
            width=_as_int(args.get("width")),
            height=_as_int(args.get("height")),
            steps=_as_int(args.get("steps")),
            cfg=_as_float(args.get("cfg_scale")),
            negative=str(args.get("negative_prompt") or ""),
            batch=batch,
            profile=sd.PROFILE_EDIT,
            cancel=ctx.cancel_event(),
        )
        seed_arg = _as_int(args.get("seed"))
        if seed_arg is not None:
            gen_kwargs["seed"] = seed_arg

        ctx.report_progress(type="notice", kind="gen_start",
                            text=f"editing {batch} image{'s' if batch != 1 else ''}…")
        try:
            # Decrypt the input through workspaces into a private temp dir — sd-cli needs a
            # readable path and must never be handed ciphertext (same discipline as generation).
            async with StagedImage(ctx.user_id, img_rel) as staged:
                result = await sd.generate(settings, prompt, ref_image=staged, **gen_kwargs)
        except sd.DiffusionError as e:
            ctx.report_progress(type="notice", kind="gen_done", ok=False,
                                text="could not edit the image")
            return ToolResult(False, f"image editing failed: {str(e)[:500]}")
        except Exception as e:  # noqa: BLE001 — a broken run must not break the turn
            ctx.report_progress(type="notice", kind="gen_done", ok=False,
                                text="could not edit the image")
            return ToolResult(False, f"image editing failed: {type(e).__name__}: "
                                     f"{str(e)[:200]}")

        blobs = result.get("images") or []
        stem = _slug(prompt)
        stamp = time.strftime("%H%M%S")
        multi = len(blobs) > 1
        names = [(f"edit_{stem}_{stamp}{f'_{i + 1}' if multi else ''}.png", data)
                 for i, (_, data) in enumerate(blobs)]
        images = _store_images(ctx, names, "generated")
        if not images:
            ctx.report_progress(type="notice", kind="gen_done", ok=False,
                                text="the edited image could not be saved")
            return ToolResult(False, "an edit was produced but could not be saved to your "
                                     "workspace (disk full or a permissions problem)")

        from .. import artifacts
        art = None
        try:
            art = artifacts.record(ctx.user_id, images[0]["path"],
                                   conversation_id=ctx.conversation_id, kind="produced")
        except Exception as e:  # noqa: BLE001 — bookkeeping must not lose the picture
            from .. import audit
            audit.log(ctx.user_id, "artifact_record_failed",
                      {"path": images[0]["path"], "err": str(e)[:200]})

        n = len(images)
        secs = result.get("seconds", "?")
        ctx.report_progress(type="notice", kind="gen_done", ok=True,
                            text=f"edited {n} image{'s' if n != 1 else ''} in {secs}s")

        lines = [f"Edited {n} image(s) with Qwen Image Edit in {secs}s. "
                 f"Instruction: {prompt[:160]}"]
        for it in images:
            lines.append(f"- {it['path']}")
        if len(names) > n:
            lines.append(f"note: {len(names) - n} of them could not be saved")
        seed = result.get("seed")
        lines.append(f"seed: {seed}" if seed is not None else
                     "seed: chosen randomly by the model (the exact value is embedded in the "
                     "PNG's generation-metadata chunk)")
        for note in result.get("notes") or []:
            lines.append(f"note: {note}")
        lines.append(f"edited from {img_rel}")
        lines.append("\nThe edited image(s) follow this message. Look at them before describing "
                     "them: check that the requested change actually happened AND that the parts "
                     "you meant to keep are still there, say what is actually present including "
                     "anything wrong, and offer one specific fix rather than retrying on your own.")

        meta: dict = {"images": n, "seconds": secs}
        if seed is not None:
            meta["seed"] = seed
        if art:
            meta["download"] = {"name": art["filename"], "url": art["download_url"],
                                "artifact_id": art["id"]}
        return ToolResult(True, "\n".join(lines), images=images, meta=meta)


def _as_int(value):
    """An int argument or None — absent and junk both mean 'use the default'."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value):
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
