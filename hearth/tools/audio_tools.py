"""Audio tools — let a text model find out what a recording contains.

  audio_info       duration / codec / sample rate (cheap, ffprobe only, no model)
  describe_audio   ask the captioner model what it HEARS and return that as text

Why a second model rather than transcription: transcription recovers words and throws away
everything else — who is speaking, how they sound, the music, the room, whether two people
talk over each other. Those are exactly what makes "what's in this recording?" an interesting
question, and an Omni-class model answers them. The result is plain text, so the main model
needs no audio support of its own (DEC-056).

Both tools degrade rather than fail loudly when their dependency is missing: ffprobe for
`audio_info`, a configured captioner for `describe_audio`. A refusal that names the setting
to change is far more useful to the model (and to the family member reading the chat) than a
stack trace or, worse, a confident invented description.

Files are read through hearth/workspaces.py so encryption at rest is honoured and the path
stays inside the caller's jail; nothing here hands a workspace path to a subprocess.
"""
from __future__ import annotations

import asyncio

from .base import Tool, ToolResult, DeltaThrottle
from .. import captioner as cap
from .. import workspaces as ws


def _jail_path(ctx, args) -> tuple[str, str]:
    """Validate the requested path. Returns (rel, error); error "" means OK."""
    rel = str(args.get("path", ""))
    if not rel:
        return rel, "a `path` is required (workspace-relative audio file)"
    try:
        ws.resolve_in_jail(ctx.user_id, rel, allow_missing=False)
    except ws.JailEscape as e:
        return rel, f"refused: {e}"
    except FileNotFoundError:
        return rel, f"not found: {rel}"
    return rel, ""


def _looks_like_audio(rel_path: str, mime: str = "") -> bool:
    """Audio-ish by extension or MIME — a hint for the model, not a hard gate.

    A .mp4 may be a recording with sound and a .wav may be silence; audio_info settles it. So
    this only exists to warn about the obvious mismatch (a text file sent to a listener)."""
    if cap.is_audio(rel_path, mime):
        return True
    return str(rel_path or "").lower().endswith((".mkv", ".mov", ".avi"))


class AudioInfoTool(Tool):
    name = "audio_info"
    description = ("Report an audio file's technical metadata — duration, codec, sample rate, "
                   "channel count, and whether a video file carries an audio track at all — "
                   "WITHOUT sending it to any model. Call this before describe_audio on a long "
                   "recording so you know how long it is and can warn the user it will take a "
                   "while.")
    enable_key = "tools.audio_info"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "workspace-relative audio (or video) file path"}},
            "required": ["path"]}

    async def run(self, args, ctx):
        rel, err = _jail_path(ctx, args)
        if err:
            return ToolResult(False, err)
        info = await asyncio.to_thread(cap.audio_info_for_workspace, ctx.user_id, rel)
        if "note" in info and not info.get("duration_s"):
            return ToolResult(False, f"could not read audio metadata for {rel}: {info['note']}"
                                     f" (install ffmpeg to provide ffprobe)")
        lines = [f"Audio: {rel}"]
        dur = info.get("duration_s") or 0.0
        lines.append(f"duration: {cap.fmt_time(dur)} ({round(dur, 2)}s)" if dur
                     else "duration: unknown")
        if info.get("codec"):
            lines.append(f"codec: {info['codec']}")
        if info.get("sample_rate"):
            lines.append(f"sample rate: {info['sample_rate']} Hz")
        if info.get("channels"):
            lines.append(f"channels: {info['channels']}")
        streams = info.get("audio_streams")
        if streams is not None:
            lines.append(f"audio tracks: {streams}")
            if not streams:
                lines.append("this file has NO audio track — describe_audio would describe "
                             "silence; use view_video for its pictures instead")
        lines.append("next: describe_audio hands it to the captioner model, which describes "
                     "voices, emotion, music and ambience (not only words)")
        return ToolResult(True, "\n".join(lines), meta={"duration_s": round(dur, 2)})


class DescribeAudioTool(Tool):
    name = "describe_audio"
    description = ("Find out what an audio file (or the audio track of a video) actually "
                   "contains by sending it to the captioner model. Returns a text description: "
                   "number and character of speakers, tone and emotion, the words spoken where "
                   "clear, background sounds, music, acoustics. Long recordings are processed in "
                   "timed chunks, each labelled with its start time. Use audio_info first for a "
                   "long file. Pass `focus` to steer it, e.g. 'transcribe verbatim with speaker "
                   "labels' or 'describe only the music'. Requires the captioner model to be "
                   "configured (Admin → Audio); if it is not, say so — never invent a description.")
    enable_key = "tools.describe_audio"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "workspace-relative audio (or video) file path"},
            "focus": {"type": "string",
                      "description": "optional extra instruction for the captioner, e.g. "
                                     "'verbatim transcription', 'only the music', "
                                     "'how many people argue and how'"}},
            "required": ["path"]}

    async def run(self, args, ctx):
        rel, err = _jail_path(ctx, args)
        if err:
            return ToolResult(False, err)
        settings = ctx.settings()
        p = cap.resolve_paths(settings)
        if not p["enabled"]:
            return ToolResult(False,
                "the audio captioner is switched off (Admin → Audio → 'Enable the audio "
                f"captioner'). Tell the user you cannot listen to {rel} rather than guessing.")
        # Fail with a precise reason BEFORE spending time on ffmpeg/model startup.
        preflight = cap.preflight(settings)
        if preflight:
            return ToolResult(False, f"cannot describe {rel}: {preflight}")

        focus = str(args.get("focus") or "").strip()
        # A non-audio extension is worth flagging but not worth refusing: ffprobe/llama.cpp
        # accept more containers than our extension list, so a wrong guess here must not
        # block a legitimate file. audio_info is the authority on whether it has sound.
        mismatch_note = "" if _looks_like_audio(rel) else (
            "note: this does not look like an audio file by its extension — attempting anyway; ")

        # STREAM the description to the chat while it is being produced, exactly like the
        # upload path does (DEC-057 added streaming for attachments; DEC-061 connects it
        # here). Without on_delta the captioner still streams internally and the text just
        # sits in a buffer, so minutes pass with nothing on screen. The sink is queued by
        # the agent loop and drained into SSE while this call is still running.
        throttle = DeltaThrottle()

        def _on_delta(piece: str) -> None:
            ready = throttle.add(piece)
            if ready:
                ctx.report_progress(type="notice", kind="caption_chunk",
                                    file=rel, text=ready)

        # Same start/done pair the attachment path emits, so the live block looks identical
        # whichever route reached the captioner. `file` lets the client label the block.
        ctx.report_progress(type="notice", kind="caption_start", file=rel,
                            text=f"listening to {rel}…")
        try:
            result = await cap.describe_file(ctx.user_id, rel, extra_prompt=focus,
                                             settings=settings, cancel=ctx.cancel_event(),
                                             on_delta=_on_delta)
        except cap.CaptionSkip as e:
            ctx.report_progress(type="notice", kind="caption_done", file=rel, ok=False,
                                text=f"could not describe {rel}")
            return ToolResult(False, f"{rel}: {e}")
        except cap.CaptionError as e:
            # The message already names the missing binary/path/port/log; pass it through so
            # the model can relay an actionable reason instead of a vague failure.
            ctx.report_progress(type="notice", kind="caption_done", file=rel, ok=False,
                                text=f"could not describe {rel}")
            return ToolResult(False, f"could not describe {rel}: {mismatch_note}{e}")
        except Exception as e:  # noqa: BLE001 — a captioner fault must never break the turn
            ctx.report_progress(type="notice", kind="caption_done", file=rel, ok=False,
                                text=f"could not describe {rel}")
            return ToolResult(False, f"could not describe {rel}: {mismatch_note}"
                                     f"{type(e).__name__}: {str(e)[:200]}")

        # Flush whatever is below the throttle threshold so the streamed block ends with the
        # complete description — the client appends pieces, so a dropped tail truncates it.
        tail = throttle.flush()
        if tail:
            ctx.report_progress(type="notice", kind="caption_chunk", file=rel, text=tail)
        ctx.report_progress(type="notice", kind="caption_done", file=rel, ok=True,
                            text=f"described {rel}")

        dur = result.get("duration_s") or 0.0
        chunks = int(result.get("chunks") or 1)
        header = [f"Captioner description of {rel} "
                  f"(duration {cap.fmt_time(dur) if dur else 'unknown'}, {chunks} chunk"
                  f"{'s' if chunks != 1 else ''}, backend {result.get('backend', 'server')}):"]
        for note in result.get("notes") or []:
            header.append(f"note: {note}")
        body = str(result.get("caption") or "").strip()
        caveat = ("\n\nThis description came from a separate captioner model, not from your own "
                  "perception — it can be wrong about words, counts and identities. Quote it as a "
                  "description, and say plainly if the user needs an exact transcript.")
        return ToolResult(True, "\n".join(header) + "\n\n" + body + caveat,
                          meta={"chunks": chunks, "duration_s": round(dur, 2),
                                "backend": result.get("backend", "")})
