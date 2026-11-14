"""
Workspace: safe, sandboxed access to a single project folder.
All paths are resolved and checked to remain inside the project root.
"""
import os
import re
from pathlib import Path
from typing import List, Optional

TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css", ".scss",
    ".json", ".md", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".c", ".h", ".cpp", ".hpp", ".java", ".rs", ".go", ".sh", ".xml",
    ".svg", ".sql", ".gitignore", ".env",
}

# Binary media that genuinely cannot be read as text. SVG is deliberately absent:
# it is XML source and belongs in TEXT_EXTENSIONS, even though view_image can also
# render it visually.
BINARY_MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
}

IGNORE_DIRS = {".agent", ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

MAX_FILE_BYTES = 2_000_000


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise WorkspaceError(f"{root} is not a directory")

    def _resolve(self, rel_path: str) -> Path:
        candidate = (self.root / rel_path).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise WorkspaceError(f"Path escapes project root: {rel_path}")
        return candidate

    def is_text_file(self, path: Path) -> bool:
        if path.suffix.lower() in TEXT_EXTENSIONS:
            return True
        if path.name in TEXT_EXTENSIONS:
            return True
        return False

    def list_files(self, max_files: int = 2000) -> List[str]:
        results = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(self.root)
                results.append(str(rel))
                if len(results) >= max_files:
                    return sorted(results)
        return sorted(results)

    def list_directory(self, rel_path: str = ".") -> List[str]:
        path = self._resolve(rel_path)
        if not path.exists() or not path.is_dir():
            raise WorkspaceError(f"Directory not found: {rel_path}")
        entries = []
        for entry in sorted(path.iterdir()):
            if entry.name in IGNORE_DIRS or entry.name.startswith("."):
                continue
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{entry.name}{suffix}")
        return entries

    def read_file(self, rel_path: str) -> str:
        path = self._resolve(rel_path)
        if not path.exists():
            raise WorkspaceError(f"File not found: {rel_path}")
        if path.suffix.lower() in BINARY_MEDIA_EXTENSIONS:
            raise WorkspaceError(
                f"{rel_path} is an image/video file and cannot be read as text. Use the view_image tool instead."
            )
        if path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError(f"File too large to read: {rel_path}")
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise WorkspaceError(f"Could not read {rel_path}: {e}")

    def file_size(self, path: str) -> int:
        full = self._resolve(path) if hasattr(self, "_resolve") else None
        import os
        target = full if full else os.path.join(self.root, path)
        return os.path.getsize(target)

    def write_file(self, rel_path: str, content: str) -> None:
        path = self._resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def append_file(self, rel_path: str, content: str) -> None:
        """Append *content* to the end of an existing file."""
        path = self._resolve(rel_path)
        if not path.exists():
            raise WorkspaceError(f"File not found: {rel_path}")
        path.write_text(path.read_text(encoding="utf-8") + content, encoding="utf-8")

    def prepend_file(self, rel_path: str, content: str) -> None:
        """Prepend *content* to the beginning of an existing file."""
        path = self._resolve(rel_path)
        if not path.exists():
            raise WorkspaceError(f"File not found: {rel_path}")
        path.write_text(content + path.read_text(encoding="utf-8"), encoding="utf-8")

    def delete_file(self, rel_path: str) -> None:
        path = self._resolve(rel_path)
        if path.exists():
            path.unlink()

    def exists(self, rel_path: str) -> bool:
        return self._resolve(rel_path).exists()

    def search(self, query: str, max_results: int = 100) -> List[dict]:
        results = []
        query_lower = query.lower()
        for rel in self.list_files():
            full = self.root / rel
            if not self.is_text_file(full):
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if query_lower in line.lower():
                    results.append({"file": rel, "line": i, "text": line.strip()})
                    if len(results) >= max_results:
                        return results
        return results

    def search_regex(self, pattern: str, max_results: int = 100) -> List[dict]:
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise WorkspaceError(f"Invalid regex pattern: {e}")
        results = []
        for rel in self.list_files():
            full = self.root / rel
            if not self.is_text_file(full):
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    results.append({"file": rel, "line": i, "text": line.strip()})
                    if len(results) >= max_results:
                        return results
        return results

    def compile_check(self, rel_path: str) -> str:
        if not rel_path.endswith(".py"):
            raise WorkspaceError(f"{rel_path} is not a .py file.")
        source = self.read_file(rel_path)
        try:
            compile(source, rel_path, "exec")
            return f"OK — {rel_path} compiles with no syntax errors."
        except SyntaxError as e:
            pointer = ""
            if e.text:
                pointer = e.text.rstrip("\n") + "\n" + " " * max(0, (e.offset or 1) - 1) + "^"
            return (
                f"SyntaxError in {rel_path}, line {e.lineno}, column {e.offset}: {e.msg}\n{pointer}"
            )

    VIEWABLE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
    VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    def _resolve_media(self, path_str: str) -> Path:
        """Resolve an image/video path for view_image/attachments.

        Accepts absolute paths that live outside the project root (e.g. images
        attached from elsewhere on disk) as long as the file exists; relative
        paths resolve against the project root exactly like _resolve().
        """
        p = Path(path_str)
        if p.is_absolute():
            resolved = p.resolve()
            if not resolved.exists():
                raise WorkspaceError(f"File not found: {path_str}")
            return resolved
        return self._resolve(path_str)

    def load_viewable_image(self, rel_path: str) -> bytes:
        """Returns PNG bytes for any supported static image/vector format, converting as needed."""
        path = self._resolve_media(rel_path)
        if not path.exists():
            raise WorkspaceError(f"File not found: {rel_path}")
        ext = path.suffix.lower()
        if ext == ".svg":
            import cairosvg
            return cairosvg.svg2png(url=str(path))
        if ext in self.VIEWABLE_IMAGE_EXTS:
            from PIL import Image
            import io
            img = Image.open(path)
            if getattr(img, "is_animated", False):
                img.seek(0)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return buf.getvalue()
        raise WorkspaceError(f"Unsupported image type: {ext}")

    def extract_video_frames(self, rel_path: str, max_frames: int = 8,
                             start_time: Optional[float] = None,
                             end_time: Optional[float] = None) -> List[bytes]:
        """Sample evenly-spaced PNG frames from a video.

        start_time/end_time (seconds) restrict sampling to that window; they
        are clamped to the file's own duration, so out-of-range values shrink
        to what exists rather than failing. Every failure mode raises
        WorkspaceError with an actionable message (missing cv2, unreadable
        file, empty/zero-frame window, no decodable frames).
        """
        path = self._resolve_media(rel_path)
        if not path.exists():
            raise WorkspaceError(f"File not found: {rel_path}")
        try:
            import cv2
        except ImportError as e:
            raise WorkspaceError(
                f"Video frame extraction needs opencv-python, which is not "
                f"installed ({e}). Install it with:  pip install opencv-python")
        try:
            max_frames = int(max_frames)
        except (TypeError, ValueError):
            raise WorkspaceError(f"max_frames must be an integer, got {max_frames!r}")
        if max_frames < 1:
            max_frames = 1
        try:
            cap = cv2.VideoCapture(str(path))
        except Exception as e:
            raise WorkspaceError(f"Could not open video '{rel_path}': {e}")
        if not cap.isOpened():
            cap.release()
            raise WorkspaceError(
                f"Could not open video '{rel_path}'. It may be corrupt or use a "
                "codec OpenCV cannot decode.")
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
            duration = (total / fps) if (total > 0 and fps > 0) else 0.0
            if duration <= 0:
                # Some containers stream a bogus frame count/fps; fall back to
                # the current playback-position property for a duration hint.
                ms = cap.get(getattr(cv2, "CAP_PROP_POS_MSEC", 0.0)) or 0.0
                duration = float(ms) / 1000.0
        finally:
            cap.release()
        if total <= 0 and duration <= 0:
            raise WorkspaceError(
                f"Could not read frame count or duration for '{rel_path}' — it "
                "may not be a video file OpenCV can decode.")
        lo = 0.0 if start_time is None else float(start_time)
        hi = duration if end_time is None else float(end_time)
        if duration > 0:
            lo = min(max(lo, 0.0), duration)
            hi = min(max(hi, 0.0), duration)
        if hi <= lo:
            raise WorkspaceError(
                f"Empty time window for '{rel_path}': start={lo:.2f}s must be "
                f"before end={hi:.2f}s (video duration: {duration:.2f}s).")
        lo_sec, hi_sec = lo, hi
        if total > 0 and duration > 0:
            lo = int(round(lo / duration * total))
            hi = int(round(hi / duration * total))
        lo = max(0, lo)
        hi = total if total > 0 else hi
        hi = max(hi, lo + 1)
        span = hi - lo
        count = min(max_frames, span if total > 0 else max_frames)
        count = max(1, count)
        indices = [lo + int(i * span / count) for i in range(count)]
        frames: List[bytes] = []
        try:
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                raise WorkspaceError(f"Could not re-open video '{rel_path}' for frame extraction.")
            if total > 0:
                for idx in indices:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    ok2, buf = cv2.imencode(".png", frame)
                    if ok2:
                        frames.append(buf.tobytes())
            else:
                # No usable frame index: seek by timestamp instead.
                cap.set(cv2.CAP_PROP_POS_MSEC, lo_sec * 1000.0)
                step = (hi_sec - lo_sec) / count
                for i in range(count):
                    cap.set(cv2.CAP_PROP_POS_MSEC, (lo_sec + step * i) * 1000.0)
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    ok2, buf = cv2.imencode(".png", frame)
                    if ok2:
                        frames.append(buf.tobytes())
        finally:
            cap.release()
        if not frames:
            raise WorkspaceError(
                f"No frames could be decoded from '{rel_path}' between "
                f"{lo_sec:.2f}s and {hi_sec:.2f}s.")
        return frames

    def media_info(self, path_str: str) -> dict:
        """Collect technical metadata for an image/video file via OpenCV/Pillow.

        Never raises for a readable file — unknown fields are simply omitted,
        and a missing/corrupt file or missing dependency raises WorkspaceError.
        Returns {"path", "kind" ("image"|"video"|"other"), ...fields...}.
        """
        path = self._resolve_media(path_str)
        if not path.exists():
            raise WorkspaceError(f"File not found: {path_str}")
        if not path.is_file():
            raise WorkspaceError(f"{path_str} is not a file.")
        ext = path.suffix.lower()
        info: dict = {"path": path_str, "size_bytes": path.stat().st_size}
        if ext in self.VIDEO_EXTS:
            info["kind"] = "video"
            try:
                import cv2
                cap = cv2.VideoCapture(str(path))
                if not cap.isOpened():
                    raise WorkspaceError(
                        f"Could not open '{path_str}' — unknown or unsupported codec.")
                try:
                    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
                finally:
                    cap.release()
                info["resolution"] = f"{w}x{h}" if w and h else "unknown"
                info["width"], info["height"] = w, h
                info["fps"] = round(fps, 3) if fps > 0 else "unknown"
                info["frame_count"] = total if total > 0 else "unknown"
                if fps > 0 and total > 0:
                    secs = total / fps
                    info["duration_seconds"] = round(secs, 2)
                    info["duration"] = (f"{int(secs // 60):02d}:"
                                        f"{int(secs % 60):02d}.{int((secs % 1) * 100):02d}")
                if fourcc_int:
                    info["codec_fourcc"] = "".join(
                        chr((fourcc_int >> (8 * i)) & 0xFF) for i in range(4))
            except ImportError as e:
                raise WorkspaceError(
                    f"Video metadata needs opencv-python, which is not installed ({e}). "
                    "Install it with:  pip install opencv-python")
            except WorkspaceError:
                raise
            except Exception as e:
                raise WorkspaceError(f"Could not read video metadata for '{path_str}': {e}")
            return info
        try:
            from PIL import Image
            with Image.open(path) as img:
                info["kind"] = "image"
                info["format"] = img.format or ext.lstrip(".") or "unknown"
                info["mode"] = img.mode
                info["width"], info["height"] = img.size
                info["resolution"] = f"{img.size[0]}x{img.size[1]}"
                if getattr(img, "n_frames", 1) > 1:
                    info["frames"] = img.n_frames
                dpi = img.info.get("dpi")
                if dpi:
                    try:
                        info["dpi"] = int(round(dpi[0]))
                    except Exception:
                        pass
        except ImportError as e:
            raise WorkspaceError(
                f"Image metadata needs Pillow, which is not installed ({e}). "
                "Install it with:  pip install pillow")
        except WorkspaceError:
            raise
        except Exception as e:
            raise WorkspaceError(
                f"Could not read image metadata for '{path_str}': {e}. "
                "Note: SVG metadata is not supported here — read_file shows its XML source.")
        return info

    def read_context_docs(self) -> str:
        candidates = ["AGENT.md", "README.md", "NOTES.md", "CONTEXT.md"]
        chunks = []
        for name in candidates:
            path = self.root / name
            if path.exists() and path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    chunks.append(f"--- {name} ---\n{text.strip()}")
                except Exception:
                    continue
        return "\n\n".join(chunks)

    def find_mmproj_candidates(self, model_path: str) -> List[str]:
        """
        Search for likely mmproj projector files near a given GGUF model path.
        Looks in the model's own directory and a sibling "mmproj" subfolder.
        These files live outside the sandboxed project root (alongside the
        model itself), so absolute paths are returned rather than relative ones.
        """
        model_dir = Path(model_path).resolve().parent
        candidates: List[str] = []
        search_dirs = [model_dir, model_dir / "mmproj"]
        for d in search_dirs:
            if not d.exists() or not d.is_dir():
                continue
            for entry in sorted(d.iterdir()):
                if not entry.is_file():
                    continue
                name_lower = entry.name.lower()
                if name_lower.endswith(".gguf") and "mmproj" in name_lower:
                    candidates.append(str(entry.resolve()))
        return candidates

