"""Rendering helpers behind the media tools: PDF pages and video frames as PNGs.

Why images at all: a PDF's layout, figures and scanned pages do not survive text
extraction, and Hearth already has a working path for getting pixels to the model
(mmproj + `image_url` parts). So `pdf_read` rasterises pages instead of parsing them,
and `view_video` samples a handful of frames instead of decoding a whole clip.

Backend policy — every step degrades, nothing is assumed present:
  PDF pages   PyMuPDF (pip install pymupdf) -> pdftoppm (poppler) -> gs -> mutool
  Video meta  ffprobe                      -> OpenCV (cv2)
  Frames      ffmpeg (one pass, PNG pipe)  -> OpenCV seek
PIL is used only to downscale what a backend produced, and is itself
optional. A missing tool never raises: callers get ("", reason) style results or a
MediaError with a message the *model* can repeat to the user, including which
package would fix it.

Subprocesses are invoked with argument lists (never a shell), with a timeout, and
only against paths the caller already resolved inside the workspace jail.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess


class MediaError(Exception):
    """Raised when nothing can render the input. Message is model/user-facing."""


# ---- capability probing ------------------------------------------------------

def have(cmd: str) -> bool:
    """Is an external binary on PATH? Cached — tools probe once per process."""
    if cmd not in _HAVE_CACHE:
        _HAVE_CACHE[cmd] = bool(shutil.which(cmd))
    return _HAVE_CACHE[cmd]


_HAVE_CACHE: dict[str, bool] = {}


def _pymupdf():
    """Return the `fitz` module or None (PyMuPDF is optional)."""
    try:
        import fitz  # type: ignore
        return fitz
    except Exception:  # noqa: BLE001 — not installed / broken install
        return None


def _cv2():
    try:
        import cv2  # type: ignore
        return cv2
    except Exception:  # noqa: BLE001
        return None


def capabilities() -> dict:
    """What this machine can actually render — used in tool output so failures are
    explainable rather than mysterious."""
    return {
        "pymupdf": _pymupdf() is not None,
        "pdftoppm": have("pdftoppm"),
        "gs": have("gs"),
        "mutool": have("mutool"),
        "ffmpeg": have("ffmpeg"),
        "ffprobe": have("ffprobe"),
        "opencv": _cv2() is not None,
        "pillow": _pillow() is not None,
    }


def capabilities_line() -> str:
    c = capabilities()
    on = [k for k, v in c.items() if v]
    return "render backends available: " + (", ".join(on) if on else "NONE")


# ---- image post-processing (optional Pillow) ---------------------------------

def _pillow():
    try:
        from PIL import Image  # type: ignore
        return Image
    except Exception:  # noqa: BLE001
        return None


def optimise_png(data: bytes, max_edge: int = 1280, grayscale: bool = False) -> bytes:
    """Shrink a rendered PNG before it becomes base64 in a prompt.

    A full-resolution colour page is easily 1-2 MB of data URL — hundreds of tokens
    the model cannot use any better than from a smaller image, and enough to blow the
    context bar on a 6-page PDF. Long edge is capped (default 1280 px, roughly what a
    vision encoder downsamples to anyway).

    Colour is KEPT. Grayscaling used to be the default and was removed at the
    operator's request: it saved only *bytes*, while a frame's real cost is vision
    tokens, which depend on pixel dimensions and are identical grey or colour. So the
    old default traded away information (a red warning light, a coloured chart, a
    face's skin tone) for no gain in prefill or context. Pass grayscale=True only if
    bandwidth genuinely matters more than colour.

    Without Pillow this is a no-op passthrough — the tool still works, just heavier.
    """
    Image = _pillow()
    if Image is None or not data:
        return data
    try:
        im = Image.open(io.BytesIO(data))
        im = im.convert("L" if grayscale else "RGB")
        edge = max(1, int(max_edge))
        if max(im.size) > edge:
            im.thumbnail((edge, edge))
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        # Never trade a working image for a smaller broken one.
        return out or data
    except Exception:  # noqa: BLE001 — an odd render still beats a hard failure
        return data


# ---- PDF ---------------------------------------------------------------------

def pdf_info(abs_path: str) -> dict:
    """Page count + document metadata. PyMuPDF when present, else `pdfinfo`.

    Raises MediaError if neither can read it (a corrupt or encrypted PDF is a real
    answer the model should relay, not a crash).

    NOTE: reads the path directly, so with workspace encryption at rest this works only
    through PyMuPDF-on-decrypted-bytes... which is why callers pass decrypted bytes to
    `pdf_info_bytes`. This variant exists for ad-hoc use and keeps its signature.
    """
    try:
        with open(abs_path, "rb") as fh:
            return pdf_info_bytes(fh.read())
    except OSError:
        return pdf_info_bytes(b"")


def pdf_info_bytes(data: bytes) -> dict:
    """Same as pdf_info() but over in-memory PDF bytes.

    Byte-based on purpose: hearth's workspace can be encrypted at rest, and the CLI
    backends (`pdfinfo`, pdftoppm, gs) are separate processes that would otherwise be
    handed ciphertext. The caller reads through workspaces.read_bytes(), which decrypts.
    """
    if not data:
        raise MediaError("no PDF data to read (empty or unreadable file)")
    fitz = _pymupdf()
    if fitz is not None:
        try:
            with fitz.open(stream=data, filetype="pdf") as doc:
                pages = doc.page_count
                meta = dict(doc.metadata or {})
                sizes = []
                try:
                    for i in range(min(pages, 10)):
                        r = doc[i].rect
                        sizes.append((int(r.width), int(r.height)))
                except Exception:  # noqa: BLE001 — sizes are a nicety
                    pass
            out = {"pages": int(pages)}
            for k in ("title", "author", "subject", "creationDate"):
                if meta.get(k):
                    out[k] = str(meta[k])[:200]
            if sizes:
                w, h = sizes[0]
                out["page_size_pts"] = f"{w}x{h}"
            return out
        except Exception as e:  # noqa: BLE001 — fall through to pdfinfo
            last_err = str(e)[:160]
    else:
        last_err = "PyMuPDF not installed"

    if have("pdfinfo"):
        try:
            r = subprocess.run(["pdfinfo", "-"], input=data, capture_output=True,
                               timeout=30)
            if r.returncode == 0:
                out2: dict = {}
                stdout = r.stdout or b""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", "replace")
                for line in stdout.splitlines():
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    k, v = k.strip(), v.strip()
                    if k.lower() == "pages":
                        try:
                            out2["pages"] = int(v)
                        except ValueError:
                            pass
                    elif k.lower() in ("title", "author", "subject"):
                        out2[k.lower()] = v[:200]
                    elif k.startswith("Page ") and k.endswith(" size:"):
                        out2.setdefault("page_size_pts", v)
                    elif k.lower() == "encrypted":
                        out2["encrypted"] = v.lower().startswith("yes")
                if out2.get("pages"):
                    return out2
            # A non-zero exit here usually means "this is not a PDF at all" — surface the
            # backend's own words (e.g. "Syntax Error: Couldn't read xref table") because
            # that is what tells the model the file type was wrong, not our code.
            detail = _clean_stderr(r.stderr) or f"pdfinfo exited {r.returncode}"
            raise MediaError(f"not a readable PDF ({detail}). This tool reads PDFs only — "
                             f"use fs_read for text files or view_video for video.")
        except FileNotFoundError:
            pass
    raise MediaError(f"could not read this PDF ({last_err}). Install pymupdf or poppler-utils "
                     f"(provides pdfinfo/pdftoppm), then try again.")


def render_pdf_pages(abs_path: str, page_indexes: list[int], dpi: int = 130) -> dict[int, bytes]:
    """Convenience path variant of `render_pdf_bytes` (reads the file itself)."""
    try:
        with open(abs_path, "rb") as fh:
            return render_pdf_bytes(fh.read(), page_indexes, dpi=dpi)
    except OSError as e:
        raise MediaError(f"could not read the PDF: {str(e)[:160]}")


def render_pdf_bytes(data: bytes, page_indexes: list[int], dpi: int = 130) -> dict[int, bytes]:
    """Rasterise the given 0-based pages of a PDF (as bytes) to PNG. {page_index: bytes}.

    Tries PyMuPDF first (in-process, no temp files), then pdftoppm/gs/mutool — each fed
    the PDF through a temp file we create from these decrypted bytes, so encryption at
    rest in the workspace never reaches an external process. Pages a backend cannot
    produce are simply absent from the result; callers report that rather than failing
    the whole call.
    """
    pages = sorted({int(p) for p in page_indexes if int(p) >= 0})
    if not pages:
        return {}
    if not data:
        raise MediaError("no PDF data to render")
    dpi = max(48, min(int(dpi), 300))

    fitz = _pymupdf()
    if fitz is not None:
        out: dict[int, bytes] = {}
        try:
            with fitz.open(stream=data, filetype="pdf") as doc:
                zoom = dpi / 72.0
                for i in pages:
                    try:
                        pix = doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                        out[i] = pix.tobytes("png")
                    except Exception:  # noqa: BLE001 — one bad page must not kill the rest
                        continue
            if out:
                return out
        except Exception:  # noqa: BLE001 — fall through to CLI backends
            pass

    import tempfile
    with tempfile.TemporaryDirectory(prefix="hearth-pdf-") as tmp:
        src = os.path.join(tmp, "doc.pdf")
        try:
            with open(src, "wb") as fh:
                fh.write(data)
        except OSError as e:
            raise MediaError(f"could not stage the PDF for rendering: {str(e)[:120]}")
        for name in ("pdftoppm", "gs", "mutool"):
            if not have(name):
                continue
            try:
                out = {}
                for i in pages:
                    one = _render_pdf_cli_page(name, src, i, dpi, tmp)
                    if one:
                        out[i] = one
                if out:
                    return out
            except Exception:  # noqa: BLE001 — try the next backend
                continue
    raise MediaError(
        "cannot render PDF pages to images. Install one of: pymupdf (pip install pymupdf), "
        "poppler-utils (pdftoppm), ghostscript (gs), or mupdf-tools (mutool). "
        + capabilities_line() + ".")


def _render_pdf_cli_page(backend: str, abs_path: str, idx: int, dpi: int, tmp: str) -> bytes | None:
    """Render ONE page (0-based) with a CLI backend; None if it failed."""
    p1 = idx + 1   # every CLI here is 1-based
    stem = os.path.join(tmp, f"p{idx}")
    dest = stem + ".png"
    if backend == "pdftoppm":
        # -singlefile keeps the name predictable (otherwise it appends the page number).
        cmd = ["pdftoppm", "-png", "-r", str(dpi), "-f", str(p1), "-l", str(p1),
               "-singlefile", abs_path, stem]
    elif backend == "gs":
        cmd = ["gs", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=png16m", "-r" + str(dpi),
               f"-dFirstPage={p1}", f"-dLastPage={p1}", f"-sOutputFile={dest}", abs_path]
    elif backend == "mutool":
        cmd = ["mutool", "draw", "-r", str(dpi), "-o", dest, abs_path, str(p1)]
    else:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if not os.path.isfile(dest):
        return None
    try:
        with open(dest, "rb") as fh:
            return fh.read() or None
    except OSError:
        return None


# ---- video -------------------------------------------------------------------

def _parse_rate(s: str) -> float:
    """'25/1', '29.97', '30000/1001' -> float fps (0.0 when unparseable)."""
    try:
        s = str(s or "").strip()
        if not s or s in ("0/0", "N/A"):
            return 0.0
        if "/" in s:
            a, b = s.split("/", 1)
            b = float(b)
            return float(a) / b if b else 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def video_info(abs_path: str) -> dict:
    """Duration/fps/size/frame count. ffprobe first, OpenCV as fallback.

    `duration_s` is the headline number: view_video needs it to space frames evenly
    and to clamp a requested time window, which is why the info tools report it too.
    """
    if have("ffprobe"):
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                 "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,"
                 "r_frame_rate,nb_frames,duration", abs_path],
                capture_output=True, text=True, timeout=45)
            if r.returncode == 0 and r.stdout.strip():
                import json
                d = json.loads(r.stdout)
                fmt = d.get("format") or {}
                vstreams = [s for s in (d.get("streams") or [])
                            if s.get("codec_type") == "video"]
                vs = vstreams[0] if vstreams else {}
                dur = _to_float(fmt.get("duration")) or _to_float(vs.get("duration"))
                fps = _parse_rate(vs.get("r_frame_rate"))
                nbf = _to_int(vs.get("nb_frames"))
                if not nbf and dur and fps:
                    nbf = int(round(dur * fps))
                out = {"path": abs_path, "duration_s": dur, "fps": fps,
                       "frames": nbf, "width": _to_int(vs.get("width")),
                       "height": _to_int(vs.get("height")),
                       "codec": vs.get("codec_name") or "",
                       "size_bytes": _to_int(fmt.get("size")),
                       "audio_streams": sum(1 for s in (d.get("streams") or [])
                                            if s.get("codec_type") == "audio")}
                if dur or nbf:
                    return out
        except Exception:  # noqa: BLE001 — fall back to OpenCV
            pass

    cv2 = _cv2()
    if cv2 is not None:
        cap = None
        try:
            cap = cv2.VideoCapture(abs_path)
            if cap.isOpened():
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                return {"path": abs_path, "duration_s": (n / fps) if (n and fps) else 0.0,
                        "fps": fps, "frames": n,
                        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
                        "codec": "", "size_bytes": os.path.getsize(abs_path),
                        "audio_streams": 0, "backend": "opencv"}
        except Exception:  # noqa: BLE001
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass

    raise MediaError(
        "could not read this video's metadata. Install ffmpeg (provides ffprobe) or the "
        "opencv-python package. " + capabilities_line() + ".")


def extract_frames_even(abs_path: str, count: int, duration_s: float,
                        window: tuple[float, float] | None = None) -> list[tuple[float, bytes]]:
    """`count` equally spaced frames inside [window], as [(timestamp_s, png_bytes)].

    One ffmpeg pass with `fps=count/window_length`, which spaces them exactly and
    costs one decode of the file rather than N seeks. Falls back to OpenCV seeks when
    ffmpeg is unavailable. Frames that cannot be decoded are skipped, so a short or
    slightly corrupt clip still returns what it can.
    """
    count = max(1, min(int(count), 16))
    lo, hi = (0.0, float(duration_s or 0.0)) if window is None else (
        float(window[0]), float(window[1]))
    if hi <= lo:
        lo, hi = 0.0, max(lo, float(duration_s or 0.0))

    if have("ffmpeg"):
        length = max(0.001, hi - lo)
        vf = f"fps={count}/{length:.6f}"
        cmd = ["ffmpeg", "-v", "error"]
        # -ss before -i seeks cheaply; keep a hair of slack past the end so the last
        # requested frame is still reachable on short clips.
        if lo > 0:
            cmd += ["-ss", f"{lo:.3f}"]
        cmd += ["-t", f"{length:.3f}", "-i", abs_path, "-vf", vf, "-frames:v", str(count),
                "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=180)
            blobs = _split_pngs(r.stdout)
            if blobs:
                step = length / len(blobs)
                return [(round(lo + step * (k + 0.5), 2), b) for k, b in enumerate(blobs)]
        except (subprocess.TimeoutExpired, OSError):
            pass

    stamps = [lo + (hi - lo) * (k + 0.5) / count for k in range(count)] if hi > lo else \
        [0.0] * count
    return extract_frames_at(abs_path, stamps)


def extract_frames_at(abs_path: str, timestamps: list[float]) -> list[tuple[float, bytes]]:
    """Frames at explicit timestamps (seconds), one ffmpeg seek each.

    Used for the model's own `timestamps` argument — 'show me 0:41 and 1:12' cannot be
    expressed as an evenly-spaced sample. OpenCV seeks are the fallback.
    """
    stamps = [max(0.0, float(t)) for t in timestamps]
    out: list[tuple[float, bytes]] = []

    if have("ffmpeg"):
        for t in stamps:
            cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", abs_path,
                   "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=90)
            except (subprocess.TimeoutExpired, OSError):
                continue
            blobs = _split_pngs(r.stdout)
            if blobs:
                out.append((round(t, 2), blobs[0]))
        if out:
            return out

    cv2 = _cv2()
    if cv2 is not None:
        cap = None
        try:
            cap = cv2.VideoCapture(abs_path)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) if cap.isOpened() else 0.0
            for t in stamps:
                try:
                    if fps:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(t * fps)))
                    else:
                        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        continue
                    from PIL import Image  # local: optional dep
                    # BGR -> RGB, colour preserved to match optimise_png()'s default.
                    im = Image.fromarray(frame[..., ::-1])
                    buf = io.BytesIO()
                    im.save(buf, format="PNG", optimize=True)
                    out.append((round(t, 2), buf.getvalue()))
                except Exception:  # noqa: BLE001 — skip an unreadable timestamp
                    continue
        except Exception:  # noqa: BLE001
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
    if not out:
        raise MediaError(
            "could not decode any frame from this video. Install ffmpeg or opencv-python. "
            + capabilities_line() + ".")
    return out


def _split_pngs(stream: bytes) -> list[bytes]:
    """Split a concatenated PNG stream (image2pipe output) into individual images."""
    if not stream:
        return []
    magic = b"\x89PNG"
    idxs = [i for i in range(len(stream) - 4) if stream.startswith(magic, i)]
    # Collapse runs of overlapping matches (magic can only repeat at real boundaries).
    bounds: list[int] = []
    for i in idxs:
        if not bounds or i - bounds[-1] > 3:
            bounds.append(i)
    out = []
    for n, start in enumerate(bounds):
        end = bounds[n + 1] if n + 1 < len(bounds) else len(stream)
        blob = stream[start:end]
        if len(blob) > 100:
            out.append(blob)
    return out


# ---- small parsers -----------------------------------------------------------

def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _clean_stderr(err) -> str:
    s = (err or "").strip() if isinstance(err, str) else b""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    return s.strip().splitlines()[-1][:200] if s.strip() else ""


def fmt_time(seconds) -> str:
    """Seconds -> 'm:ss' for timestamps the model and user both read easily."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 0:
        s = 0.0
    # Round the TOTAL seconds, not just the remainder: `int(round(s % 60))` alone made
    # 3599.6 render as "60:00" and 59.9 as "1:00".
    total = int(round(s))
    return f"{total // 60}:{total % 60:02d}"
