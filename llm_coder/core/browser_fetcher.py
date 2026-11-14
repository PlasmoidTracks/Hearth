"""
Browser-based page fetching via Playwright (Chromium).

Used when the web access policy mode is "open_web". Launches a headless
Chromium instance, navigates to the target URL, waits for network idle,
then extracts visible text and links — mimicking what a real user would
see in their browser.

Dependencies: playwright (pip install playwright).
After installation, run: playwright install chromium.

Lazy-import: if playwright is not installed, the module returns graceful
fallbacks so the rest of the app keeps working.
"""

import re
import os
import platform
import threading
from typing import Optional

# ── Global state ──────────────────────────────────────────────────────────
# We keep a single Playwright + browser instance alive across fetches to
# avoid the ~3-second startup penalty on every request.  Thread-safe via
# a lock because the agent executor may call us from a background thread.

_lock = threading.Lock()
_playwright = None       # playwright.async_api.Playwright
_browser = None          # playwright.async_api.Browser  (or sync counterpart)
_session_cookie_path: Optional[str] = None  # optional cookie jar path
_cookies = []           # accumulated cookies to persist across sessions

# Try to import playwright; if unavailable, all functions return a
# descriptive error message so the app doesn't crash.
try:
    from playwright.sync_api import sync_playwright as _get_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


def _ensure_playwright():
    """Lazy-initialise Playwright + a persistent-context browser (singleton)."""
    global _playwright, _browser, _session_cookie_path, _cookies

    with _lock:
        if _browser is not None:
            return True  # already initialised

        try:
            pw = _get_playwright().start()
        except Exception:
            return False

        # Cookie jar: store in the app's config dir so they persist across runs
        import os
        from core.app_settings import AppSettings
        cfg = AppSettings()
        cookie_dir = cfg.config_dir()
        _session_cookie_path = os.path.join(cookie_dir, "playwright_cookies.json")

        # Persistent context — keeps cookies, storage, etc. across sessions
        context_kwargs: dict = {}
        if os.path.exists(_session_cookie_path) and _cookies:
            context_kwargs["storage_state"] = {"cookies": _cookies, "origins": []}

        try:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ]
            # --no-sandbox is only needed on Linux; on Windows/macOS it produces a warning.
            if platform.system() == "Linux":
                launch_args.insert(0, "--no-sandbox")
            browser = pw.chromium.launch(
                headless=True,
                args=launch_args,
            )
        except Exception:
            if hasattr(pw, "stop"):
                pw.stop()
            return False

        # Use a persistent context to cache cookies, localStorage, etc.
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                **context_kwargs,
            )
        except Exception:
            browser.close()
            if hasattr(pw, "stop"):
                pw.stop()
            return False

        _playwright = pw
        _browser = browser
        return True


def _save_cookies():
    """Persist accumulated cookies to disk for the next session."""
    global _session_cookie_path, _cookies
    if _session_cookie_path is not None:
        try:
            state = {"cookies": _cookies, "origins": []}
            import json
            with open(_session_cookie_path, "w") as f:
                json.dump(state, f)
        except Exception:
            pass


def _load_cookies():
    """Load previously-saved cookies so the browser has a warmer state."""
    global _cookies
    if _session_cookie_path is None:
        return
    if not os.path.exists(_session_cookie_path):
        return
    try:
        import json
        with open(_session_cookie_path, "r") as f:
            state = json.load(f)
        _cookies = state.get("cookies", [])
    except Exception:
        _cookies = []


def _extract_text(html: str, max_chars: int = 8000) -> str:
    """Strip scripts, styles, and tags; extract visible text and links."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _extract_links(html: str) -> list:
    """Extract human-readable links from HTML."""
    links = []
    for m in re.finditer(
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE
    ):
        href, text = m.groups()
        if not href.startswith("http"):
            continue
        text_clean = re.sub(r"<[^>]+>", "", text).strip()
        if text_clean:
            links.append(f"{text_clean} -> {href}")
    return links


def fetch_page(url: str, timeout_ms: int = 30000,
               max_fetch_policy: Optional[dict] = None) -> str:
    """
    Fetch a URL using a headless Chromium browser (Playwright).

    Returns extracted plain text + links, similar to the old fetch_text_url
    but with full JS rendering and a real-browser User-Agent / cookies.

    If Playwright is not installed or the browser fails, returns an error
    string (never raises).

    ``max_fetch_policy`` controls limits:
        {"max_text_chars": 8000, "max_links": 30}
    """
    mfp = max_fetch_policy or {}
    max_chars = mfp.get("max_text_chars", 8000)
    max_links = mfp.get("max_links", 30)
    truncated_reasons = []

    _load_cookies()

    ok = _ensure_playwright()
    if not ok:
        return (
            "Error: Playwright is not installed or could not initialise. "
            "Install it with:  pip install playwright  &&  playwright install chromium  "
            "Then retry the fetch."
        )

    try:
        # Use synchronous Playwright API (simpler for a background thread)
        # _browser is already a sync_api.Browser
        page = _browser.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except Exception as e:
            # Page may still have partial content — don't fail entirely
            pass

        # Wait a tiny bit more for lazy-loaded content
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Read cookies and update global store
        try:
            new_cookies = page.context.cookies()
            # Merge: keep existing cookies, add/update with new ones
            existing = {c["name"]: c for c in _cookies}
            for c in new_cookies:
                existing[c["name"]] = c
            _cookies = list(existing.values())
        except Exception:
            pass

        html = page.content()
        page.close()

    except Exception as e:
        return f"Error fetching URL via browser: {e}"

    _save_cookies()

    text = _extract_text(html, max_chars)
    if len(text) >= max_chars:
        truncated_reasons.append("extracted text")
    links = _extract_links(html)

    link_block = "\n".join(links[:max_links])
    truncated = ""
    if len(links) > max_links:
        truncated_reasons.append("links")
        link_block = "\n".join(links[:max_links]) + f"\n... ({len(links) - max_links} more links truncated)"
    return (
        f"--- Extracted text (via headless browser) ---\n{text}\n\n"
        f"--- Links (first {max_links}) ---\n{link_block}"
    )


def close_browser():
    """Shut down the persistent browser and Playwright instance."""
    global _browser, _playwright
    _save_cookies()
    with _lock:
        if _browser is not None:
            try:
                _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright is not None:
            try:
                if hasattr(_playwright, "stop"):
                    _playwright.stop()
                elif hasattr(_playwright, "stop_async"):
                    _playwright.stop_async()
            except Exception:
                pass
            _playwright = None


def kill_active_browser() -> bool:
    """Force-close the active Playwright browser (singleton).

    Used before a new web operation to ensure a clean slate when the
    existing browser may be in a zombie/stuck state.

    Returns True if a browser was closed, False if none was active.
    """
    global _browser, _playwright
    _save_cookies()
    with _lock:
        if _browser is not None:
            try:
                _browser.close()
            except Exception:
                pass
            _browser = None
            browser_was_active = True
        else:
            browser_was_active = False

        if _playwright is not None:
            try:
                if hasattr(_playwright, "stop"):
                    _playwright.stop()
                elif hasattr(_playwright, "stop_async"):
                    _playwright.stop_async()
            except Exception:
                pass
            _playwright = None
        return browser_was_active


def playwright_available() -> bool:
    """Return True if Playwright is installed and initialises successfully."""
    if not _PLAYWRIGHT_AVAILABLE:
        return False
    return _ensure_playwright()
