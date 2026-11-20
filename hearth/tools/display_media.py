"""display_media — show a workspace file to the USER in its own chat bubble.

The gap this fills: every other media path in Hearth points at the *model*.
`ToolResult.images` becomes a synthetic user-role multimodal message (DEC-052), so
pdf_read frames and generated pictures reach an mmproj projector and never the
screen — the user sees a tool chip and a ⬇ button. This is the opposite direction:
pixels for the human, not the model.

What it deliberately does NOT do:

  * It does not send file bytes anywhere. The result carries workspace-relative
    paths plus artifact URLs; the browser fetches them from `/api/artifact/{id}`,
    which enforces ownership and decrypts at rest. Nothing new is written to disk.
  * It does not gate. Showing a file the user can already download is not a
    destructive act, so `gated = False` — an approval card per picture would just
    train the user to mash Approve.
  * It does not invent a viewer. Rendering (thumbnails, full screen, playback) is
    plain HTML elements driven off `meta.media`; see chat.js `addMediaBubble`.

Files the browser cannot present (PDFs, text, archives) are refused by name rather
than drawn as a broken element — a broken image teaches the user nothing about what
they asked for.
"""
from __future__ import annotations

import mimetypes
import os

from .base import Tool, ToolResult
from .. import workspaces as ws

# How many files one call may put on screen. A model that wants to show a whole
# folder should zip it or name a handful; a wall of thumbnails is not a message.
MAX_FILES_PER_CALL = 8


def media_kind(rel_path: str) -> str:
    """'image' | 'audio' | 'video' | '' for a workspace-relative path.

    Decided by extension via mimetypes, never by reading the file: this runs on paths
    the model supplied, and it must stay cheap enough to call per file per turn. The
    orchestrator uses the same function so the server's idea of what is displayable and
    the client's rendering cannot drift apart.
    """
    mime = mimetypes.guess_type(os.path.basename(str(rel_path or "")))[0] or ""
    for role in ("image", "audio", "video"):
        if mime.startswith(role + "/"):
            return role
    return ""


class DisplayMediaTool(Tool):
    name = "display_media"
    label = "Display media in chat"
    description = (
        "Show existing files from the user's workspace TO THE USER as media bubbles in the "
        "conversation — images inline, audio and video with playback controls, each with full "
        "screen and download. This is the tool for making a file *visible*: after generating, "
        "downloading, or being asked about a picture, call it with the workspace-relative path "
        "so the human actually sees the thing rather than reading your description of it. It "
        "does not send the file to you — if you also need to look at an image yourself, use "
        "pdf_read/view_video or attach it. Accepts images, audio and video (other file types are "
        "refused; offer `upload` for those instead). Files must already exist in the workspace. "
        "Pass several paths in ONE call to show a set together, and say in `caption` why they are "
        "on screen — the caption is what the user reads next to them.")

    enable_key = "tools.display_media"
    gated = False     # read-only presentation; no approval card per picture

    def parameters(self):
        return {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": f"workspace-relative files to show, in the order they "
                                     f"should appear (1-{MAX_FILES_PER_CALL} per call). "
                                     f"Images, audio or video only."},
            "caption": {"type": "string",
                        "description": "optional one-line label shown above the media — what it "
                                       "is or why you are showing it. Keep it short; it is UI "
                                       "text, not a report."}},
            "required": ["paths"]}

    async def run(self, args, ctx):
        raw = args.get("paths")
        if isinstance(raw, str):
            raw = [raw]          # models sometimes send one path as a bare string
        if not isinstance(raw, list) or not [p for p in raw if str(p).strip()]:
            return ToolResult(False, "`paths` needs at least one workspace-relative file path")

        paths = [str(p).strip() for p in raw if str(p).strip()]
        extra = len(paths) - MAX_FILES_PER_CALL
        if extra > 0:
            paths = paths[:MAX_FILES_PER_CALL]

        caption = str(args.get("caption") or "").strip()

        items, refused = [], []
        for rel in paths:
            try:
                ws.resolve_in_jail(ctx.user_id, rel, allow_missing=False)
            except ws.JailEscape as e:
                refused.append(f"{rel}: refused ({e})")
                continue
            except FileNotFoundError:
                refused.append(f"{rel}: not found in the workspace")
                continue

            kind = media_kind(rel)
            if not kind:
                refused.append(f"{rel}: not an image, audio or video file, so there is nothing "
                               f"to display — offer it with `upload` instead")
                continue

            # Register as an artifact so the browser can fetch it through the same
            # ownership-checked, decrypt-at-rest route as every other download — no new
            # file route, no new token, nothing to authorise separately. record() keeps
            # ONE row per workspace path (DEC-040), so displaying a file that `fs_write`
            # already registered reuses its id rather than piling up duplicates; the side
            # effect is that the row's conversation follows the chat it was last shown in,
            # which is what "this chat" in the Files panel means for a shared file.
            url, art_id, name = "", "", os.path.basename(rel)
            try:
                from .. import artifacts
                art = artifacts.record(ctx.user_id, rel,
                                       conversation_id=ctx.conversation_id, kind="produced")
                url, art_id, name = art["download_url"], art["id"], art["filename"]
            except Exception as e:  # noqa: BLE001 — bookkeeping must not lose the display
                from .. import audit
                audit.log(ctx.user_id, "artifact_record_failed",
                          {"path": rel, "err": str(e)[:200]})

            items.append({"path": rel, "kind": kind, "url": url,
                          "artifact_id": art_id, "name": name})

        if not items:
            return ToolResult(False, "nothing could be displayed:\n" + "\n".join(refused))

        lines = [f"Showing {len(items)} file(s) to the user"
                 + (f" ({caption})" if caption else "") + ":"]
        for it in items:
            lines.append(f"- {it['path']} ({it['kind']})")
        if extra > 0:
            lines.append(f"note: only the first {MAX_FILES_PER_CALL} of the {len(paths)} paths "
                         f"you passed were shown — make a second call for the rest")
        for r in refused:
            lines.append(f"not shown — {r}")

        # Files the model was not given: anything that is not an image (see below).
        unseen = [it["kind"] for it in items if it["kind"] != "image"]

        # What the model is told about its own perception. Without this, "the user can see
        # this" reads to a model as "this was shown, so I may describe it", and models will
        # narrate a clip they never perceived. Wording depends on what is actually invisible:
        # with images only there is nothing unseen to warn about, and claiming otherwise
        # would contradict the pixels build_messages does send for them.
        if unseen:
            lines.append("The user can see these now. You have NOT been given them — you cannot "
                         "see or hear anything in this result, so describe only what you already "
                         "know about each file (its name, how you made it, what you wrote into "
                         "it). To perceive one yourself: describe_audio for audio, view_video for "
                         "video. Never invent what is in a file you have not looked at.")
        else:
            lines.append("The user can see these now. The images themselves are being added to "
                         "your context — look at them before describing them, and say what is "
                         "actually there rather than what the prompt asked for.")

        meta = {"media": items}
        # The caption rides in meta as well as in the model-facing text, so the bubble's
        # label survives to the live SSE event AND to the persisted row. Reading it back out
        # of `preview` instead would be fragile (that text is truncated for display), and
        # tool_result events carry no `args`, so there is nothing else to read it from.
        if caption:
            meta["caption"] = caption[:300]
        # NOT `images`. That channel is for files a tool RENDERED for the model to see
        # (pdf_read pages, view_video frames) — here the file already exists untouched, and
        # build_messages folds meta.display's images into that same follow-up message. Filling
        # both made replay encode every displayed picture twice: two identical data URLs in
        # one request, doubling image tokens for a file the model is looking at once.
        # Audio/video are excluded from the model entirely (see _display_image_items).
        return ToolResult(True, "\n".join(lines), meta=meta)
