"""Media tools — see PDFs and videos as images instead of as lost text.

  pdf_info      page count + metadata (cheap, no rendering)
  pdf_read      rasterise chosen pages -> images the model actually sees
  video_info    runtime/duration, fps, resolution, size (no frame decoding)
  view_video    a few equally spaced frames, or frames at explicit timestamps

Both `pdf_read` and `view_video` go through *pixels*: a PDF's figures, scans and
two-column layout do not survive text extraction, and a video has no text at all. The
rendering backends and their fallbacks live in media_render.py; nothing here assumes
pymupdf/ffmpeg/OpenCV exist — a missing backend produces a message naming the package
that would fix it.

How the images reach the model (DEC-052): `content` stays the tool's text, and the
files listed in ToolResult.images are attached by the orchestrator as a multimodal
user-role message immediately after the tool result — the same shape hearth's upload
path already uses successfully with an mmproj projector. The images are also written
into the workspace under media/ so they can be re-read, downloaded, or referenced by
path later. Requires a vision-capable model + mmproj; with a text-only model the tool
still returns its text summary and says the pixels were not seen.
"""
from __future__ import annotations

import os

from .base import Tool, ToolResult
from . import media_render as mr
from .. import workspaces as ws


async def _provider_health(ctx) -> tuple[dict, str]:
    """Ask the model server about itself, once, WITHOUT launching anything.

    Returns (health_dict, error_note). Uses ModelManager.probe(), documented as never
    starting a server and never writing settings — `get_provider()` would call
    ensure_server() and could spawn llama-server just because a tool wanted to know
    whether vision is available. A read-only question must not have side effects.

    probe() answers {server_reachable, base_url, n_ctx} plus the /props fields it
    copied through (model_path, mmproj, ...) — but it has no `ok` key, so treat
    server_reachable as the truth and fall back to a direct health() call when the
    manager cannot provide one.
    """
    try:
        from ..manager import get_manager
        m = get_manager()
    except Exception:  # noqa: BLE001 — a broken manager must not break the tool
        return {}, "note: could not reach the model server, so vision availability is unknown"
    try:
        h = dict(await m.probe())
    except Exception:
        return {}, "note: could not reach the model server, so vision availability is unknown"
    if not h.get("server_reachable"):
        return {}, ("note: no model server is answering, so these images are stored but not seen; "
                    "start llama-server (Admin → Model) with a vision-capable model + mmproj")
    # probe() does not always surface the projector field (older/leaner builds of it
    # return only three keys), so ask the provider directly when it did not.
    if "mmproj" not in h:
        try:
            ph = await m.get_provider().health()
            if ph.get("mmproj"):
                h["mmproj"] = ph["mmproj"]
        except Exception:  # noqa: BLE001 — best effort only
            pass
    return h, ""


async def _vision_state(ctx) -> tuple[bool, str]:
    """(can_the_model_see_images, note). Best-effort: never fails the tool.

    Checks the live server for a loaded projector rather than only the configured
    path, so "mmproj set" in Admin is not claimed when the running server never
    actually loaded it. When nothing answers we say that instead of guessing.
    """
    h, note = await _provider_health(ctx)
    if not h:
        return False, note
    if h.get("mmproj"):
        return True, ""
    return False, ("warning: the running llama-server reports NO vision projector (mmproj), so "
                   "it may not see these images — set Admin → Model → Vision projector path and "
                   "restart the server. The files are saved under media/ regardless.")


def _store_images(ctx, blobs: list[tuple[str, bytes]], subdir: str) -> list[dict]:
    """Write rendered PNGs into the workspace jail and return ToolResult.images items.

    Paths are deterministic (stem + index) so a repeat call on the same file overwrites
    rather than piling up, and so the model can quote them back. write_bytes applies
    encryption at rest when it is enabled — we never bypass that workspaces concern.
    """
    out: list[dict] = []
    for name, data in blobs:
        rel = f"media/{subdir}/{name}"
        try:
            stored = ws.write_bytes(ctx.user_id, rel, data)
            out.append({"path": stored, "mime": "image/png"})
        except Exception:  # noqa: BLE001 — an unwritable frame must not lose the rest
            continue
    return out


def _truthy(v) -> bool:
    """Model-supplied booleans arrive as true/false, 1/0, "yes", or absent.

    Absent and empty mean OFF — this gates a costly optional extra (an audio captioner run),
    so an unparseable value must not silently buy the expensive path.
    """
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _max_edge(ctx) -> int:
    try:
        return max(320, min(int(ctx.setting("media.max_image_edge", 1280)), 4096))
    except (TypeError, ValueError):
        return 1280


class PdfInfoTool(Tool):
    name = "pdf_info"
    label = "PDF info (pages/metadata)"
    description = ("Report a PDF's page count and metadata (title/author/page size/encrypted) "
                   "WITHOUT rendering anything. Call this before pdf_read so you know how many "
                   "pages there are and can pick a sensible range — reading every page of a long "
                   "document wastes context.")
    enable_key = "tools.pdf_info"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "workspace-relative .pdf path"}},
            "required": ["path"]}

    async def run(self, args, ctx):
        rel = str(args.get("path", ""))
        try:
            ws.resolve_in_jail(ctx.user_id, rel, allow_missing=False)
        except ws.JailEscape as e:
            return ToolResult(False, f"refused: {e}")
        except FileNotFoundError:
            return ToolResult(False, f"not found: {rel}")
        # Bytes, not the path: with encryption at rest an external `pdfinfo` process
        # would otherwise be handed ciphertext.
        try:
            pdf_bytes = ws.read_bytes(ctx.user_id, rel)
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"could not read {rel}: {str(e)[:160]}")
        try:
            info = mr.pdf_info_bytes(pdf_bytes)
        except mr.MediaError as e:
            return ToolResult(False, str(e)[:400])
        lines = [f"PDF: {rel}", f"pages: {info.get('pages', '?')}"]
        for k, label in (("title", "title"), ("author", "author"), ("subject", "subject"),
                         ("page_size_pts", "page size (pts)"), ("encrypted", "encrypted")):
            if info.get(k):
                lines.append(f"{label}: {info[k]}")
        pages = int(info.get("pages") or 0)
        if pages:
            lines.append("next: call pdf_read with page_start/page_end (1-based, inclusive) — "
                         "keep it to a few pages at a time")
        return ToolResult(True, "\n".join(lines), meta={"pages": pages})


class PdfReadTool(Tool):
    name = "pdf_read"
    label = "PDF reading (pages as images)"
    description = ("Read a PDF by rendering its pages to IMAGES and showing them to you — so "
                   "figures, scanned pages, tables and layout are NOT lost (fs_read on a PDF only "
                   "yields the embedded text stream). Call pdf_info first for the page count, then "
                   "request a small range (page_start/page_end, 1-based inclusive; at most 6 pages "
                   "per call — ask for more and the result says so and tells you where to continue). "
                   "Each rendered page is also saved under media/ in your workspace.")
    enable_key = "tools.pdf_read"

    # Per call; more would flood the context window. Deliberately a class attribute rather
    # than an admin setting: unlike the listing caps, raising this multiplies image tokens by
    # however many pages a model happens to request, so it is not a safe knob to hand out.
    # The tool description and the result note both quote it, so they cannot drift.
    MAX_PAGES = 6
    MAX_TOTAL_IMAGES = 12

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "workspace-relative .pdf path"},
            "page_start": {"type": "integer",
                           "description": "first page (1-based, inclusive). Default 1."},
            "page_end": {"type": "integer",
                         "description": f"last page (1-based, inclusive). Default: up to "
                                        f"{self.MAX_PAGES} pages."},
            "dpi": {"type": "integer",
                    "description": "Render resolution 48-300 (default 130). Raise only for "
                                   "small print; higher dpi costs context tokens."}},
            "required": ["path"]}

    async def run(self, args, ctx):
        rel = str(args.get("path", ""))
        try:
            abs_path = ws.resolve_in_jail(ctx.user_id, rel, allow_missing=False)
        except ws.JailEscape as e:
            return ToolResult(False, f"refused: {e}")
        except FileNotFoundError:
            return ToolResult(False, f"not found: {rel}")

        try:
            pdf_bytes = ws.read_bytes(ctx.user_id, rel)
        except Exception as e:  # noqa: BLE001 — unreadable file is a real answer
            return ToolResult(False, f"could not read {rel}: {str(e)[:160]}")
        try:
            info = mr.pdf_info_bytes(pdf_bytes)
        except mr.MediaError as e:
            return ToolResult(False, str(e)[:400])
        total = int(info.get("pages") or 0)

        try:
            start = max(1, int(args.get("page_start") or 1))
        except (TypeError, ValueError):
            start = 1
        try:
            end = int(args.get("page_end")) if args.get("page_end") is not None else None
        except (TypeError, ValueError):
            return ToolResult(False, "page_end must be an integer")
        if end is None:
            end = min(total or start, start + self.MAX_PAGES - 1)
        requested_end = end
        end = max(start, min(end, start + self.MAX_PAGES - 1))
        clamped = requested_end > end
        if total and start > total:
            return ToolResult(False, f"page_start {start} is past the end of a {total}-page PDF")
        try:
            dpi = int(args.get("dpi") or 130)
        except (TypeError, ValueError):
            dpi = 130

        indexes = list(range(start - 1, min(end, total or end)))
        if len(indexes) > self.MAX_TOTAL_IMAGES:
            indexes = indexes[:self.MAX_TOTAL_IMAGES]
        # `pdf_bytes` was read above through workspaces.read_bytes(), i.e. DECRYPTED —
        # the renderer takes bytes rather than a path so no external process (pdftoppm,
        # gs, mutool) can ever be handed ciphertext from an encrypted-at-rest jail.
        try:
            rendered = mr.render_pdf_bytes(pdf_bytes, indexes, dpi=dpi)
        except mr.MediaError as e:
            return ToolResult(False, str(e)[:400])
        if not rendered:
            return ToolResult(False, f"rendered no pages from {rel} (range {start}-{end})")

        edge = _max_edge(ctx)
        stem = os.path.basename(rel).rsplit(".", 1)[0] or "document"
        blobs = [(f"{stem}_p{i + 1}.png", mr.optimise_png(rendered[i], max_edge=edge))
                 for i in sorted(rendered)]
        images = _store_images(ctx, blobs, "pdf")
        if not images:
            return ToolResult(False, "pages rendered but could not be saved to the workspace")

        _seen, note = await _vision_state(ctx)
        got = sorted(rendered)
        lines = [f"Rendered {len(images)} page(s) of {rel} as images (pages "
                 f"{got[0] + 1}-{got[-1] + 1} of {total or '?'})."]
        if clamped:
            # A request for pages 1-40 used to be answered with 1-6 and no complaint, so a
            # model that asked for the whole document believed it had received it. Say what
            # was refused and why, next to what was delivered (AUDIT #6).
            lines.append(f"note: you asked for pages up to {requested_end}; only "
                         f"{self.MAX_PAGES} per call are rendered (context budget) — "
                         f"continue with page_start={got[-1] + 2}")
        for it in images:
            lines.append(f"- page image: {it['path']}")
        if len(images) < len(got):
            # A frame that could not be written must be named, not silently dropped —
            # otherwise the model believes it saw pages 1-6 of a run where page 4 failed.
            lines.append(f"note: {len(got) - len(images)} rendered page(s) could not be saved")
        if not note:
            lines.append("The page images follow this message — read them visually.")
        else:
            lines.append(note)
        last_seen = got[-1] + 1
        if total and last_seen < total:
            lines.append(f"more pages remain: call pdf_read again with page_start={last_seen + 1}")
        return ToolResult(True, "\n".join(lines), images=images,
                          meta={"pages": [i + 1 for i in got], "total_pages": total})


def _captioner_ready(ctx) -> bool:
    """Can the audio captioner actually run right now? Never raises.

    Used to decide what to *advertise*, so it must reflect reality: claiming a capability
    that is switched off invites the model to describe a video's sound from imagination,
    which is the one failure mode this pairing must not have. Deliberately not async and
    not a network probe — `preflight()` is the same cheap config/filesystem check the audio
    tools use before spending time on ffmpeg.
    """
    try:
        from .. import captioner as cap
        settings = ctx.settings()
        if not bool(settings.get("captioner.enabled")):
            return False
        return not cap.preflight(settings)
    except Exception:  # noqa: BLE001 — an availability check is never worth breaking a tool
        return False


class VideoInfoTool(Tool):
    name = "video_info"
    label = "Video info (runtime/fps)"
    description = ("Report a video's technical metadata — duration (runtime), fps, resolution, "
                   "frame count, size, and whether it carries audio — WITHOUT decoding it. Call "
                   "this before view_video so you know the runtime and can choose timestamps to "
                   "sample.")
    enable_key = "tools.video_info"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "workspace-relative video path"}},
            "required": ["path"]}

    async def run(self, args, ctx):
        rel = str(args.get("path", ""))
        try:
            abs_path = ws.resolve_in_jail(ctx.user_id, rel, allow_missing=False)
        except ws.JailEscape as e:
            return ToolResult(False, f"refused: {e}")
        except FileNotFoundError:
            return ToolResult(False, f"not found: {rel}")
        try:
            i = mr.video_info(str(abs_path))
        except mr.MediaError as e:
            return ToolResult(False, str(e)[:400])
        dur = i.get("duration_s") or 0.0
        lines = [f"Video: {rel}",
                 f"runtime: {mr.fmt_time(dur)} ({round(dur, 2)}s)",
                 f"fps: {round(i.get('fps') or 0, 3)}",
                 f"frames: {i.get('frames') or '?'}"]
        if i.get("width"):
            lines.append(f"resolution: {i['width']}x{i['height']}")
        if i.get("codec"):
            lines.append(f"codec: {i['codec']}")
        if i.get("size_bytes"):
            lines.append(f"size: {i['size_bytes'] // 1024} KiB")
        if i.get("audio_streams"):
            # Was once a flat "(audio is NOT transcribed)". Since the captioner exists that
            # statement is false whenever it is configured, and a model that believes it will
            # tell the user there is no way to hear the video. Say what is true instead.
            if _captioner_ready(ctx):
                lines.append(f"audio tracks: {i['audio_streams']} — view_video can describe "
                             f"the sound of the section it shows (with_audio=true)")
            else:
                lines.append(f"audio tracks: {i['audio_streams']} — no audio captioner is "
                             f"configured, so the sound cannot be described yet")
        elif i.get("audio_streams") == 0:
            lines.append("audio tracks: 0 — this video is silent; with_audio would only "
                         "describe nothing")
        lines.append("next: view_video samples frames — default 8 equally spaced, or pass "
                     "timestamps / start_time+end_time")
        return ToolResult(True, "\n".join(lines), meta={"duration_s": round(dur, 2)})


class ViewVideoTool(Tool):
    name = "view_video"
    label = "Video viewing (sampled frames)"
    description = ("Look at a video by decoding a SMALL number of frames and showing them to you "
                   "as images (never the whole clip). Default: 8 frames equally spaced over the "
                   "whole runtime. Pass `timestamps` (seconds) to see specific moments, or "
                   "`count` plus `start_time`/`end_time` to sample a window densely. Use video_info "
                   "first if you do not know the runtime. Frames are saved under media/ in your "
                   "workspace. AUDIO: pass with_audio=true to also get a description of what is "
                   "HEARD over exactly the span being sampled (no separate audio range — it "
                   "follows start_time/end_time, or first-to-last sampled frame). Use it when the "
                   "sound carries the answer: speech, who talks, tone, music, an off-screen noise. "
                   "Do NOT use it just to get the general gist of a long video — 2 minutes is the "
                   "practical ceiling per call, because audio costs a separate model request every "
                   "few seconds of clip and a long caption crowds out everything else. For a long "
                   "video, first understand it from frames alone, then re-call with_audio on the "
                   "short window you actually care about. Only turn it on for a span over 2 "
                   "minutes when the video genuinely must be understood as a whole (a recording "
                   "whose meaning is in what was said throughout) — and tell the user beforehand "
                   "that it will take a while.")
    enable_key = "tools.view_video"

    DEFAULT_FRAMES = 8
    MAX_FRAMES = 16
    # Above this, a caption is describing minutes of audio to summarise a handful of
    # pictures — the cost/benefit inverts. Enforced in code, not just in the description:
    # models ask for what the schema allows them to ask for.
    AUDIO_WINDOW_LIMIT_S = 120.0

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "workspace-relative video path"},
            "count": {"type": "integer",
                      "description": f"frames to sample, equally spaced (default "
                                     f"{self.DEFAULT_FRAMES}, max {self.MAX_FRAMES})."},
            "timestamps": {"type": "array", "items": {"type": "number"},
                           "description": "explicit moments in SECONDS to grab one frame each "
                                          "(e.g. [12.5, 40, 95]). Overrides count/start_time/end_time."},
            "start_time": {"type": "number",
                           "description": "sample only from this second (optional; clamped)"},
            "end_time": {"type": "number",
                         "description": "sample only up to this second (optional; clamped)"},
            "with_audio": {"type": "boolean",
                           "description": "ALSO describe what is HEARD in the exact same span as "
                                          "the frames, via the audio captioner model (off by "
                                          "default). Use it when the sound matters — speech, "
                                          "who is talking, tone, music, an off-screen noise. The "
                                          "window is taken from start_time/end_time, or from the "
                                          f"first-to-last sampled frame; there is no separate "
                                          f"audio range. Only for spans under about "
                                          f"{int(self.AUDIO_WINDOW_LIMIT_S)}s: describing audio "
                                          f"is one model call per {int(self.AUDIO_WINDOW_LIMIT_S // 4)}s "
                                          "of clip, so leave it off when merely getting the gist "
                                          "of a long video. If you genuinely must understand the "
                                          "whole of a long video, turn it on anyway and warn the "
                                          "user it will take a while."}},
            "required": ["path"]}

    @staticmethod
    def _audio_window(args, frames, dur):
        """Span to caption, or None. Mirrors exactly what was sampled for pictures.

        A staticmethod so the derivation is testable without a tool instance or a ctx — it is
        pure policy over (args, frames, runtime).

        Explicit `timestamps` have no window argument at all, so the honest equivalent is the
        first-to-last sampled moment — that is genuinely the part of the clip the model is
        looking at, and it stops a 3-frame look at 0:10/0:20/0:30 from captioning an hour.
        """
        lo = hi = None
        if args.get("start_time") is not None or args.get("end_time") is not None:
            try:
                lo = float(args.get("start_time") or 0.0)
                hi = (float(args["end_time"]) if args.get("end_time") is not None else dur)
            except (TypeError, ValueError):
                return None
        elif frames:
            stamps = [ts for ts, _ in frames]
            lo, hi = min(stamps), max(stamps)
        if lo is None or hi is None:
            return None
        lo = max(0.0, lo)
        hi = float(hi)
        if dur:
            hi = min(hi, dur)
        if hi <= lo + 0.05:      # a single timestamp would give a zero-length window
            hi = lo + 1.0
        return (round(lo, 2), round(hi, 2))

    async def _describe_window(self, ctx, rel, window):
        """Caption `window` of this file. Returns (text|None, note). Never raises.

        Audio is a BONUS on top of the frames: whatever goes wrong here, the pictures still
        reach the model with a line explaining what could not be heard. Failing the whole
        tool because a second model was unhappy would be worse than a partial answer.
        """
        from .. import captioner as cap
        from .base import DeltaThrottle

        settings = ctx.settings()
        if not bool(settings.get("captioner.enabled")):
            return None, ("audio not described: the audio captioner is switched off "
                          "(Admin → Audio)")
        problem = cap.preflight(settings)
        if problem:
            return None, f"audio not described: {problem}"

        # Same event names as the describe_audio tool and the attachment path, so one client
        # renderer handles all three sources of a caption.
        throttle = DeltaThrottle()

        def _on_delta(piece: str) -> None:
            ready = throttle.add(piece)
            if ready:
                ctx.report_progress(type="notice", kind="caption_chunk",
                                    file=rel, text=ready)

        ctx.report_progress(type="notice", kind="caption_start", file=rel,
                            text=f"listening to {mr.fmt_time(window[0])}-{mr.fmt_time(window[1])} "
                                 f"of {rel}…")
        try:
            res = await cap.describe_file(ctx.user_id, rel, settings=settings,
                                          on_delta=_on_delta, cancel=ctx.cancel_event(),
                                          window=window)
        except cap.CaptionSkip as e:
            ctx.report_progress(type="notice", kind="caption_done", file=rel, ok=False,
                                text=f"could not describe the audio of {rel}")
            return None, f"audio not described: {e}"
        except Exception as e:  # noqa: BLE001 — no captioner fault may lose the frames
            ctx.report_progress(type="notice", kind="caption_done", file=rel, ok=False,
                                text=f"could not describe the audio of {rel}")
            return None, f"audio not described: {type(e).__name__}: {str(e)[:200]}"

        tail = throttle.flush()
        if tail:
            ctx.report_progress(type="notice", kind="caption_chunk", file=rel, text=tail)
        ctx.report_progress(type="notice", kind="caption_done", file=rel, ok=True,
                            text=f"described the audio of {rel} "
                                 f"({mr.fmt_time(window[0])}-{mr.fmt_time(window[1])})")
        body = str(res.get("caption") or "").strip()
        if not body:
            return None, "the captioner returned no description for this section"
        chunks = int(res.get("chunks") or 1)
        head = (f"Audio of {rel} from {mr.fmt_time(window[0])} to {mr.fmt_time(window[1])} "
                f"({chunks} chunk{'s' if chunks != 1 else ''}, backend "
                f"{res.get('backend', 'server')}):")
        return head + "\n\n" + body, ""

    async def run(self, args, ctx):
        rel = str(args.get("path", ""))
        try:
            abs_path = ws.resolve_in_jail(ctx.user_id, rel, allow_missing=False)
        except ws.JailEscape as e:
            return ToolResult(False, f"refused: {e}")
        except FileNotFoundError:
            return ToolResult(False, f"not found: {rel}")
        try:
            info = mr.video_info(str(abs_path))
        except mr.MediaError as e:
            return ToolResult(False, str(e)[:400])
        dur = info.get("duration_s") or 0.0

        raw_stamps = args.get("timestamps")
        if isinstance(raw_stamps, (list, tuple)) and raw_stamps:
            try:
                stamps = [float(t) for t in list(raw_stamps)[:self.MAX_FRAMES]]
            except (TypeError, ValueError):
                return ToolResult(False, "timestamps must be numbers (seconds)")
            if dur:
                stamps = [min(max(0.0, t), max(0.0, dur - 0.05)) for t in stamps]
            try:
                frames = mr.extract_frames_at(str(abs_path), stamps)
            except mr.MediaError as e:
                return ToolResult(False, str(e)[:400])
            mode = "explicit timestamps"
        else:
            try:
                count = int(args.get("count") or self.DEFAULT_FRAMES)
            except (TypeError, ValueError):
                count = self.DEFAULT_FRAMES
            count = max(1, min(count, self.MAX_FRAMES))
            window = None
            if args.get("start_time") is not None or args.get("end_time") is not None:
                try:
                    lo = float(args.get("start_time") or 0.0)
                    hi = float(args.get("end_time")) if args.get("end_time") is not None else dur
                except (TypeError, ValueError):
                    return ToolResult(False, "start_time/end_time must be numbers (seconds)")
                lo = max(0.0, min(lo, max(dur, 0.0)))
                hi = max(lo + 0.001, min(hi, dur or hi))
                window = (lo, hi)
            try:
                frames = mr.extract_frames_even(str(abs_path), count, dur, window=window)
            except mr.MediaError as e:
                return ToolResult(False, str(e)[:400])
            mode = ("equally spaced" if window is None
                    else f"equally spaced in {mr.fmt_time(window[0])}-{mr.fmt_time(window[1])}")

        # Audio BEFORE the images are attached: a caption is just text, so it belongs in the
        # tool's own message, while pixels travel separately (DEC-052). Order matters for the
        # model too — it reads "what was heard" alongside the list of frames it is about to see.
        audio_text = ""
        audio_note = ""
        if _truthy(args.get("with_audio")):
            a_window = self._audio_window(args, frames, dur)
            if not info.get("audio_streams"):
                # ffprobe said there is no track, or (no ffprobe) we cannot tell. Saying so
                # costs one line and stops the model hunting for sound in a silent clip.
                audio_note = ("note: this video reports no audio track, so nothing was "
                              "described from it")
            elif a_window is None:
                audio_note = "note: could not work out a time span to listen to"
            elif (a_window[1] - a_window[0]) > self.AUDIO_WINDOW_LIMIT_S:
                # The limit the description promises, enforced rather than hoped for. The
                # refusal still hands over the frames and explains how to get the audio in a
                # way that is affordable — which is the useful answer, not an error.
                mid = a_window[0] + self.AUDIO_WINDOW_LIMIT_S / 2.0
                lo_s, hi_s = mid - self.AUDIO_WINDOW_LIMIT_S / 2.0, mid + self.AUDIO_WINDOW_LIMIT_S / 2.0
                audio_note = (
                    f"audio NOT described: that span is {round(a_window[1] - a_window[0])}s, over "
                    f"the {int(self.AUDIO_WINDOW_LIMIT_S)}s limit for one view_video call — "
                    f"captioning audio costs a model request every few seconds of clip. The "
                    f"frames are still shown. If the sound matters, ask again with a shorter "
                    f"start_time/end_time (e.g. {round(lo_s, 1)}-{round(hi_s, 1)}s covers the "
                    f"middle); if you truly must understand the WHOLE video this way, call "
                    f"describe_audio on it and warn the user first that it will take a while.")
            else:
                audio_text, fail = await self._describe_window(ctx, rel, a_window)
                if not audio_text:
                    audio_note = fail

        edge = _max_edge(ctx)
        stem = os.path.basename(rel).rsplit(".", 1)[0] or "video"
        # NB: the timestamp is the loop variable here — naming it `t` would shadow nothing
        # but reading the PNG bytes needs the second element, so unpack both explicitly.
        blobs = [(f"{stem}_t{int(round(ts * 100))}cs.png", mr.optimise_png(data, max_edge=edge))
                 for ts, data in frames]
        images = _store_images(ctx, blobs, "video")
        if not images:
            return ToolResult(False, "frames decoded but could not be saved to the workspace")
        # `images` can be shorter than `frames` if a write failed, so report only the
        # pairs that actually exist — otherwise the text lists timestamps whose file
        # is not there and the model quotes a path that does not resolve.
        pairs = list(zip(frames, images))[:len(images)]

        _seen, note = await _vision_state(ctx)
        lines = [f"Decoded {len(pairs)} frame(s) of {rel} "
                 f"(runtime {mr.fmt_time(dur)}, {mode})."]
        for (ts, _), it in pairs:
            lines.append(f"- t={mr.fmt_time(ts)} ({round(ts, 2)}s): {it['path']}")
        if dur and mode == "equally spaced" and len(pairs) >= 2:
            step = (pairs[-1][0][0] - pairs[0][0][0]) / max(1, len(pairs) - 1)
            lines.append(f"spacing ≈ {round(step, 2)}s; anything between these moments is not shown — "
                         f"re-sample with timestamps for a closer look")
        if audio_note:
            lines.append(audio_note)
        lines.append(note or "The frames follow this message — read them visually.")
        content = "\n".join(lines)
        if audio_text:
            # The caption is presented as another model's report, not as the assistant's own
            # perception — the same caveat describe_audio gives, and needed for the same
            # reason: a captioner invents words confidently.
            content += ("\n\n" + audio_text +
                        "\n\nThe audio above describes the SAME span as the frames, from a "
                        "separate captioner model — use it to correlate what is seen with what "
                        "is heard at the same timestamp. It can be wrong about exact words, "
                        "counts and who is speaking.")
        return ToolResult(True, content, images=images,
                          meta={"duration_s": round(dur, 2), "frames": len(images),
                                "audio_described": bool(audio_text)})
